"""
HITL node — Human-In-The-Loop synchronous pause point.

This node is where the graph stops and waits for the trader.

HOW THE INTERRUPT WORKS
────────────────────────
LangGraph's interrupt() function is the key mechanism:

  1. When simulate() is done, the graph enters this node.
  2. interrupt() serializes the entire graph state to the SQLite checkpointer
     and raises an internal exception that causes graph.invoke() to return
     control to the FastAPI caller (NOT the node itself raising — LangGraph
     handles this internally).
  3. The FastAPI /api/trade endpoint receives the paused state and returns
     {thread_id, status: "awaiting_approval", strategy, slippage_metrics}
     to the frontend.
  4. The trader reviews and POSTs to /api/resume/{thread_id}.
  5. graph.invoke(Command(resume=feedback), config) resumes execution HERE —
     interrupt() returns `feedback` as its value.
  6. The node writes human_feedback to state and returns.

WHY interrupt() NOT interrupt_before
──────────────────────────────────────
interrupt_before=["hitl_node"] stops BEFORE the node runs. That means
human_feedback can't be written by the node — the node never runs on the
first pass. Using interrupt() INSIDE the node lets us:
  a) Write human_feedback to state atomically with the pause
  b) Pass a rich payload (strategy + metrics) to the interrupt value
     that the API layer can surface to the frontend without a separate
     state query

Input state keys:  strategy, slippage_metrics, simulation_error, revision_count
Output state keys: human_feedback, decision_log (append-only)

DECISION LOG
────────────
Every resume appends one entry to decision_log (reducer = list append), so the
approval record carries the whole history — including revisions that failed
and approvals the gate refused — not just the last decision.
"""

import logging
from datetime import datetime, timezone

from langgraph.types import interrupt

from backend.pipeline.plan import approval_block_reason
from backend.pipeline.state import TradeState

logger = logging.getLogger(__name__)

_ACTIONS = {"approve", "revise", "abort"}


def run(state: TradeState) -> dict:
    """
    Pause the graph and wait for the trader's decision.

    The dict passed to interrupt() is the payload surfaced while paused.
    When resumed with Command(resume=feedback), interrupt() returns `feedback`
    which is validated, logged and written to state as human_feedback.
    """
    revision_count = state.get("revision_count", 0)

    # This call pauses the graph. graph.invoke() returns here to the caller.
    feedback = interrupt({
        "status":           "awaiting_approval",
        "revision":         revision_count,
        "strategy":         state.get("strategy"),
        "slippage_metrics": state.get("slippage_metrics"),
        "simulation_error": state.get("simulation_error"),
    })

    # Execution resumes here when Command(resume=feedback) is called. The API
    # validates the payload, but the graph does not trust its caller: anything
    # malformed becomes an "invalid" decision, which the router answers by
    # pausing again rather than by guessing.
    if not isinstance(feedback, dict) or feedback.get("action") not in _ACTIONS:
        feedback = {"action": "invalid", "override_params": None}

    action  = feedback["action"]
    outcome = "accepted"
    blocked = approval_block_reason(state) if action == "approve" else None
    if blocked:
        outcome = f"refused: {blocked}"
    elif action == "invalid":
        outcome = "refused: malformed decision payload"

    entry = {
        "at":              datetime.now(timezone.utc).isoformat(),
        "revision":        revision_count,
        "action":          action,
        "override_params": feedback.get("override_params"),
        "outcome":         outcome,
        "metrics_present": bool(state.get("slippage_metrics")),
    }
    logger.info(f"[hitl_node] trader decision: action={action} outcome={outcome}")

    return {"human_feedback": feedback, "decision_log": [entry]}
