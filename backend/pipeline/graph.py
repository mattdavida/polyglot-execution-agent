"""
LangGraph StateGraph definition for the execution agent.

Graph structure:
    strategy_node → simulation_node → hitl_node [INTERRUPT]
                                           │
                          ┌────────────────┼───────────────┐
                          ▼                ▼               ▼
                    approve→         revise→          abort→
                    execution_node   simulation_node  END
                          │                │
                         END          hitl_node [INTERRUPT again]

HITL PATTERN
─────────────
This graph uses a SQLite checkpointer and interrupt() to PAUSE mid-execution
and wait for a human decision. The graph is resumed via Command(resume=feedback)
from the FastAPI /api/resume/{thread_id} endpoint.

This is the synchronous HITL pattern — not async polling of a completed result.

HOW THE CHECKPOINTER WORKS
────────────────────────────
Every graph.invoke() call receives a config with a unique thread_id:
    config = {"configurable": {"thread_id": "some-uuid"}}

The SQLite checkpointer serializes the entire TradeState at each node boundary.
When interrupt() is called in hitl_node, the state is checkpointed and invoke()
returns. When graph.invoke(Command(resume=...), config) is called later with the
same thread_id, the checkpointer restores the state and resumes from the interrupt.

This means graph state survives across HTTP requests — the graph is long-lived
in the DB, not in memory.
"""

import logging
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command   # noqa: F401 — re-exported for main.py convenience

from backend.pipeline.state import TradeState
from backend.pipeline.nodes import strategy_node, simulation_node, hitl_node, execution_node

logger = logging.getLogger(__name__)


# ── Routing ───────────────────────────────────────────────────────────────────

def _route_after_hitl(state: TradeState) -> str:
    """
    Conditional edge function called after hitl_node writes human_feedback.

    Returns the name of the next node. The mapping in add_conditional_edges
    translates these strings to actual node references.

    Three paths:
      approve → execution_node (trade dispatched, graph ends)
      revise  → simulation_node (re-run C++ with trader-modified params)
      abort   → END (trade cancelled, graph ends)
    """
    action = (state.get("human_feedback") or {}).get("action", "abort")
    logger.info(f"[graph] routing after hitl_node: action={action}")

    if action == "approve":
        return "execution_node"
    elif action == "revise":
        return "simulation_node"   # cycle back — hitl_node will pause again after re-sim
    else:
        return END


# ── Graph construction ────────────────────────────────────────────────────────

def build_graph(checkpointer: BaseCheckpointSaver) -> StateGraph:
    """
    Construct and compile the execution agent StateGraph.

    The checkpointer is passed in (not created here) so the same SQLite
    connection is shared across the application lifetime. Creating a new
    connection per graph.invoke() would work but is wasteful.

    Returns a compiled graph. Call with:
        config = {"configurable": {"thread_id": str(uuid4())}}
        graph.invoke({"trade_request": ..., "thread_id": thread_id}, config)
    """
    workflow = StateGraph(TradeState)

    # ── Node registration ─────────────────────────────────────────────────────
    # Each node is a module with a run() function.
    workflow.add_node("strategy_node",   strategy_node.run)
    workflow.add_node("simulation_node", simulation_node.run)
    workflow.add_node("hitl_node",       hitl_node.run)
    workflow.add_node("execution_node",  execution_node.run)

    # ── Entry point ───────────────────────────────────────────────────────────
    workflow.set_entry_point("strategy_node")

    # ── Linear edges (first pass) ─────────────────────────────────────────────
    workflow.add_edge("strategy_node",   "simulation_node")
    workflow.add_edge("simulation_node", "hitl_node")

    # ── Conditional routing after HITL decision ───────────────────────────────
    # approve → execution_node → END
    # revise  → simulation_node (cycle — simulation_node → hitl_node → pause again)
    # abort   → END
    workflow.add_conditional_edges(
        "hitl_node",
        _route_after_hitl,
        {
            "execution_node": "execution_node",
            "simulation_node": "simulation_node",
            END: END,
        },
    )

    workflow.add_edge("execution_node", END)

    # ── Compile with SQLite checkpointer ──────────────────────────────────────
    # The checkpointer is what enables the synchronous HITL pause:
    #   - It serializes state at each node boundary
    #   - interrupt() in hitl_node writes the pause point to the DB
    #   - Command(resume=...) restores state and continues from the interrupt
    return workflow.compile(checkpointer=checkpointer)
