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

Input state keys:  strategy, slippage_metrics
Output state keys: human_feedback
"""

import logging
from langgraph.types import interrupt

from backend.pipeline.state import TradeState

logger = logging.getLogger(__name__)


def run(state: TradeState) -> dict:
    """
    Pause the graph and wait for the trader's decision.

    The dict passed to interrupt() is what the LangGraph runtime returns
    as the 'interrupt_value' in the StateSnapshot — the FastAPI GET
    /api/trade/{thread_id} endpoint surfaces this to the frontend.

    When resumed with Command(resume=feedback), interrupt() returns `feedback`
    which is then written to state as human_feedback.
    """
    strategy        = state.get("strategy", {})
    slippage_metrics = state.get("slippage_metrics", {})
    revision_count  = state.get("revision_count", 0)

    # This call pauses the graph. The dict is the "awaiting payload" shown
    # to the trader. graph.invoke() returns here to the FastAPI caller.
    feedback = interrupt({
        "status":          "awaiting_approval",
        "revision":        revision_count,
        "strategy":        strategy,
        "slippage_metrics": slippage_metrics,
    })

    # Execution resumes here when Command(resume=feedback) is called.
    # `feedback` is whatever the trader sent: {"action": "approve"|"revise"|"abort", ...}
    logger.info(f"[hitl_node] trader decision received: action={feedback.get('action')}")

    return {"human_feedback": feedback}
