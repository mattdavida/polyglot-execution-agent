"""
Execution Agent — FastAPI backend.

Routes:
    GET  /api/health               — heartbeat
    POST /api/trade                — submit a trade, start the LangGraph
    GET  /api/trade/{thread_id}    — current state (strategy + C++ metrics + gate)
    POST /api/resume/{thread_id}   — resume with trader decision (approve/revise/abort)

Every input that can change a number is validated here BEFORE the graph runs,
with the same rules the graph applies (backend.pipeline.plan). The graph
re-checks them, so neither layer alone can dispatch on bad numbers.

Run:
    uvicorn backend.main:app --reload --port 3001
"""

import logging
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from backend.config import ALLOWED_ORIGINS, CHECKPOINTS_DB, ENABLE_API_DOCS
from backend.pipeline.graph import build_graph
from backend.pipeline.plan import MAX_SLICES, apply_override, approval_block_reason
from backend.tools.book_loader import INSTRUMENTS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MAX_ORDER_SIZE = 100_000   # contracts — a sanity bound, not a risk limit

# ── Application state ─────────────────────────────────────────────────────────
_checkpointer: Optional[SqliteSaver] = None
_graph = None

# One decision at a time per trade. A second concurrent resume on the same
# thread gets 409 instead of racing the first through the checkpointer.
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _lock_for(thread_id: str) -> threading.Lock:
    with _thread_locks_guard:
        return _thread_locks.setdefault(thread_id, threading.Lock())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create the SQLite checkpointer and compile the graph."""
    global _checkpointer, _graph
    logger.info("Execution Agent starting up...")
    # check_same_thread=False: sync routes run in FastAPI's threadpool.
    _conn         = sqlite3.connect(CHECKPOINTS_DB, check_same_thread=False)
    _checkpointer = SqliteSaver(_conn)
    _graph = build_graph(_checkpointer)
    logger.info(f"LangGraph compiled. Checkpointer: {CHECKPOINTS_DB}")
    yield
    logger.info("Shutting down.")


app = FastAPI(
    title="Execution Agent",
    description="Agentic HITL trading desk — LangGraph + C++20 LOB compute core (reference POC).",
    version="0.4.0",
    lifespan=lifespan,
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ── Pydantic request models ───────────────────────────────────────────────────
# Strict ints: "200", 200.0 and true are rejected, not coerced.

class TradeRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt:       str = Field(default="", max_length=2000)   # free text for the LLM only
    instrument:   str = Field(max_length=16)
    side:         Literal["buy", "sell"]                      # explicit — never inferred, never defaulted
    total_shares: Annotated[StrictInt, Field(gt=0, le=MAX_ORDER_SIZE)]
    deadline:     str = Field(default="end of day", max_length=64)

    @field_validator("instrument")
    @classmethod
    def _registered_instrument(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in INSTRUMENTS:
            raise ValueError(
                f"unsupported instrument {v!r}; this POC has a book for: {', '.join(INSTRUMENTS)}"
            )
        return v


class OverrideParams(BaseModel):
    """Trader slice override — the same bounds as the LLM's output, enforced here."""
    model_config = ConfigDict(extra="forbid")   # approach / reasoning are not editable

    num_slices:       Optional[Annotated[StrictInt, Field(ge=1, le=MAX_SLICES)]] = None
    shares_per_slice: Optional[Annotated[StrictInt, Field(ge=1, le=MAX_ORDER_SIZE)]] = None

    @model_validator(mode="after")
    def _at_least_one(self):
        if self.num_slices is None and self.shares_per_slice is None:
            raise ValueError("set num_slices and/or shares_per_slice")
        return self


class ResumePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action:          Literal["approve", "revise", "abort"]
    override_params: Optional[OverrideParams] = None

    @model_validator(mode="after")
    def _override_only_on_revise(self):
        if self.action == "revise" and self.override_params is None:
            raise ValueError("revise requires override_params")
        if self.action != "revise" and self.override_params is not None:
            raise ValueError("override_params is only allowed with action=revise")
        return self


# ── Graph status helpers ──────────────────────────────────────────────────────

def _pending_interrupt(snapshot) -> bool:
    if getattr(snapshot, "interrupts", None):
        return True
    return any(getattr(t, "interrupts", None) for t in (getattr(snapshot, "tasks", None) or ()))


def _derive_status(snapshot) -> str:
    """
    Status comes from the graph, never from the request:
      awaiting_approval — paused at the HITL interrupt
      failed            — a node raised; the run stopped without a pause
      completed         — ran to END after an approve
      aborted           — ran to END after an abort
    """
    if _pending_interrupt(snapshot):
        return "awaiting_approval"
    if snapshot.next:
        return "failed"
    action = (snapshot.values.get("human_feedback") or {}).get("action")
    return {"approve": "completed", "abort": "aborted"}.get(action, "failed")


def _get_snapshot_or_404(thread_id: str):
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = _graph.get_state(config)
    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail=f"No trade with thread_id={thread_id}")
    return config, snapshot


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health() -> dict:
    return {
        "status":      "ok",
        "graph":       "ready" if _graph else "not initialized",
        "instruments": list(INSTRUMENTS),
    }


@app.post("/api/trade", status_code=202)
def submit_trade(body: TradeRequestBody) -> dict:
    """
    Submit a new trade request.

    Runs strategy_node (LLM) → simulation_node (C++) → hitl_node (pause), then
    returns the thread_id. Deliberately a sync `def`: graph.invoke() blocks for
    the LLM call, and FastAPI runs sync endpoints in its threadpool so the
    event loop stays free for polling and health checks.
    """
    thread_id = str(uuid.uuid4())
    config    = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "trade_request": {
            "prompt":       body.prompt,
            "instrument":   body.instrument,
            "side":         body.side,
            "total_shares": body.total_shares,
            "deadline":     body.deadline,
        },
        "thread_id":           thread_id,
        "revision_count":      0,
        "errors":              [],
        "decision_log":        [],
        "simulation_warnings": [],
        "simulation_error":    None,
    }

    try:
        _graph.invoke(initial_state, config=config)
    except Exception:
        logger.exception(f"[/api/trade] graph error thread_id={thread_id}")
        raise HTTPException(status_code=500, detail=f"Graph execution failed (thread_id={thread_id}); see server logs")

    status = _derive_status(_graph.get_state(config))
    logger.info(f"[/api/trade] thread_id={thread_id} status={status}")
    return {"thread_id": thread_id, "status": status}


@app.get("/api/trade/{thread_id}")
def get_trade_state(thread_id: str) -> dict:
    """
    Current state for a trade: status, strategy, C++ metrics, warnings, the
    approval gate's verdict and the decision log.
    """
    _, snapshot = _get_snapshot_or_404(thread_id)
    state  = snapshot.values
    status = _derive_status(snapshot)
    block  = approval_block_reason(state) if status == "awaiting_approval" else None

    return {
        "thread_id":           thread_id,
        "status":              status,
        "trade_request":       state.get("trade_request"),
        "strategy":            state.get("strategy"),
        "slippage_metrics":    state.get("slippage_metrics"),
        "simulation_error":    state.get("simulation_error"),
        "simulation_warnings": state.get("simulation_warnings", []),
        "can_approve":         status == "awaiting_approval" and block is None,
        "approval_blocked_reason": block,
        "revision_count":      state.get("revision_count", 0),
        "decision_log":        state.get("decision_log", []),
        "errors":              state.get("errors", []),
    }


@app.post("/api/resume/{thread_id}")
def resume_trade(thread_id: str, body: ResumePayload) -> dict:
    """
    Resume the paused graph with the trader's decision.

    Refusals happen here, before the graph runs:
      404 — unknown thread
      409 — trade is not awaiting a decision (finished, aborted, failed),
            another decision for it is in flight, or approve is blocked by the gate
      422 — malformed payload, or an override that breaks the plan rules
    """
    lock = _lock_for(thread_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another decision for this trade is in progress")
    try:
        config, snapshot = _get_snapshot_or_404(thread_id)
        current = _derive_status(snapshot)
        if current != "awaiting_approval":
            raise HTTPException(
                status_code=409,
                detail=f"Trade is not awaiting a decision (status={current})",
            )

        state = snapshot.values
        override = body.override_params.model_dump(exclude_none=True) if body.override_params else None

        if body.action == "approve":
            block = approval_block_reason(state)
            if block:
                raise HTTPException(status_code=409, detail=f"Approval refused: {block}")

        if body.action == "revise":
            try:
                apply_override(
                    state.get("strategy") or {},
                    override,
                    (state.get("trade_request") or {}).get("total_shares", 0),
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"Invalid override: {exc}")

        feedback = {"action": body.action, "override_params": override}
        try:
            # Command(resume=feedback) restores the checkpoint for this thread
            # and passes `feedback` as the return value of interrupt().
            _graph.invoke(Command(resume=feedback), config=config)
        except Exception:
            logger.exception(f"[/api/resume] graph error thread_id={thread_id}")
            raise HTTPException(status_code=500, detail=f"Graph execution failed (thread_id={thread_id}); see server logs")

        status = _derive_status(_graph.get_state(config))
        logger.info(f"[/api/resume] thread={thread_id} action={body.action} → {status}")
        return {"thread_id": thread_id, "status": status, "action": body.action}
    finally:
        lock.release()
