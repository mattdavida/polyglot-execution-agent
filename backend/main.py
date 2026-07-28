"""
Execution Agent — FastAPI backend.

Routes:
    GET  /api/health               — heartbeat
    POST /api/trade                — submit a trade, start the LangGraph
    GET  /api/trade/{thread_id}    — poll paused state (strategy + C++ metrics)
    POST /api/resume/{thread_id}   — resume with trader decision (approve/revise/abort)

Run:
    uvicorn backend.main:app --reload --port 3001
"""

import logging
import sqlite3
import uuid
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel

from backend.config import ALLOWED_ORIGINS, API_PORT, CHECKPOINTS_DB
from backend.pipeline.graph import build_graph

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Application state ─────────────────────────────────────────────────────────
# The checkpointer and compiled graph are created once at startup and shared
# across all requests. SqliteSaver manages its own connection; build_graph()
# returns a compiled LangGraph StateGraph.
_checkpointer: Optional[SqliteSaver] = None
_graph = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create the SQLite checkpointer and compile the graph."""
    global _checkpointer, _graph
    logger.info("Execution Agent starting up...")
    # SqliteSaver requires a raw sqlite3 connection (check_same_thread=False
    # so FastAPI's async threads can access the same connection).
    _conn         = sqlite3.connect(CHECKPOINTS_DB, check_same_thread=False)
    _checkpointer = SqliteSaver(_conn)
    _graph = build_graph(_checkpointer)
    logger.info(f"LangGraph compiled. Checkpointer: {CHECKPOINTS_DB}")
    yield
    logger.info("Shutting down.")


app = FastAPI(
    title="Execution Agent",
    description="Agentic HITL trading desk — LangGraph + C++20 LOB compute core.",
    version="0.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic request/response models ──────────────────────────────────────────

class TradeRequestBody(BaseModel):
    prompt: str                   # natural language: "Liquidate 50,000 TSLA by EOD"
    instrument: str               # ticker or ISIN
    side: Literal["buy", "sell"] = "sell"   # explicit direction — a liquidation is a sell
    total_shares: int
    deadline: str = "end of day"


class ResumePayload(BaseModel):
    action: Literal["approve", "revise", "abort"]
    override_params: Optional[dict] = None   # e.g. {"num_slices": 3, "shares_per_slice": 16700}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health() -> dict:
    return {
        "status":    "ok",
        "phase":     "3 — LangGraph + C++ LOB wired",
        "graph":     "ready" if _graph else "not initialized",
        "checkpoint": CHECKPOINTS_DB,
    }


@app.post("/api/trade", status_code=202)
def submit_trade(body: TradeRequestBody) -> dict:
    """
    Submit a new trade request.

    Starts the LangGraph: strategy_node (LLM) → simulation_node (C++) → hitl_node (pause).
    Returns immediately with thread_id when the graph pauses at the HITL interrupt.

    NOTE — deliberately a sync `def`, not `async def`: graph.invoke() blocks for
    the duration of the LLM call (seconds). FastAPI runs sync endpoints in its
    threadpool, so the event loop stays free to serve health checks and polling
    while a trade is being processed. An `async def` here would block the whole
    server for every LLM round-trip.

    The frontend uses thread_id to:
      1. Poll GET /api/trade/{thread_id} for the paused state (strategy + metrics)
      2. POST /api/resume/{thread_id} with the trader's decision
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
        "thread_id":     thread_id,
        "revision_count": 0,
        "errors":        [],
    }

    try:
        # graph.invoke() runs until the interrupt in hitl_node, then returns.
        # It does NOT block until graph completion — the pause IS the return.
        _graph.invoke(initial_state, config=config)
        logger.info(f"[/api/trade] graph paused at HITL — thread_id={thread_id}")
    except Exception as exc:
        logger.exception(f"[/api/trade] graph error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "thread_id": thread_id,
        "status":    "awaiting_approval",
    }


@app.get("/api/trade/{thread_id}")
def get_trade_state(thread_id: str) -> dict:
    """
    Return the current state for a trade — paused, completed, or aborted.

    Called by the frontend to retrieve the LLM strategy and C++ metrics
    that were computed before the HITL pause. The response feeds the
    HITL Review Panel in the Next.js dashboard.

    Sync `def` (threadpool) — get_state() reads SQLite synchronously.
    """
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = _graph.get_state(config)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Thread not found: {exc}")

    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail=f"No state for thread_id={thread_id}")

    state = snapshot.values

    # Derive the real status from the graph, not a hardcoded string:
    #   snapshot.next non-empty → the graph is paused at a node (the HITL interrupt)
    #   otherwise the graph ran to END — approve→completed, abort→aborted
    if snapshot.next:
        status = "awaiting_approval"
    else:
        action = (state.get("human_feedback") or {}).get("action")
        status = "completed" if action == "approve" else "aborted"

    return {
        "thread_id":        thread_id,
        "status":           status,
        "trade_request":    state.get("trade_request"),
        "strategy":         state.get("strategy"),
        "slippage_metrics": state.get("slippage_metrics"),
        "revision_count":   state.get("revision_count", 0),
        "errors":           state.get("errors", []),
    }


@app.post("/api/resume/{thread_id}")
def resume_trade(thread_id: str, body: ResumePayload) -> dict:
    """
    Resume the paused graph with the trader's decision.

    Sync `def` (threadpool) — graph.invoke() blocks while the graph runs
    (C++ re-simulation on revise, execution node on approve). Same event-loop
    reasoning as submit_trade.

    Command(resume=feedback) passes `feedback` as the return value of
    interrupt() inside hitl_node. The graph then routes based on action:
      approve → execution_node → END
      revise  → simulation_node → hitl_node (pauses again) → return 202
      abort   → END

    For the revise path, the response includes status="awaiting_approval" again
    so the frontend knows to poll for the new simulation results.
    """
    config   = {"configurable": {"thread_id": thread_id}}
    feedback = {
        "action":          body.action,
        "override_params": body.override_params,
    }

    try:
        # Command(resume=feedback) is the LangGraph mechanism to:
        #   1. Restore the checkpointed state for this thread_id
        #   2. Pass `feedback` as the return value of interrupt() in hitl_node
        #   3. Continue graph execution from that point
        _graph.invoke(Command(resume=feedback), config=config)
        logger.info(f"[/api/resume] thread={thread_id} action={body.action}")
    except Exception as exc:
        logger.exception(f"[/api/resume] graph error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    if body.action == "revise":
        # Graph paused again at hitl_node after re-simulation.
        # Return 202 so the frontend re-polls for updated metrics.
        return {"thread_id": thread_id, "status": "awaiting_approval"}

    return {
        "thread_id": thread_id,
        "status":    "completed",
        "action":    body.action,
    }
