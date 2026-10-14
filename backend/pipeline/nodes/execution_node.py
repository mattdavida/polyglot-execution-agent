"""
Execution node — POC stub for trade dispatch.

In production, this node would send the approved trade to an OMS (Order
Management System) via FIX protocol, REST API, or message queue. For the POC,
it logs the approved order and writes a JSON record to output/.

This is intentionally kept minimal — the architectural value of the POC is
in the HITL decision loop, not the downstream execution plumbing.

The JSON record is a convenience artefact, not a compliance-grade audit log
(no user identity, local file, mutable). See docs/known-gaps.md.

Input state keys:  trade_request, strategy, slippage_metrics, human_feedback,
                   decision_log, thread_id
Output state keys: errors
"""

import json
import logging
import os
import pathlib
from datetime import datetime, timezone

from backend.pipeline.plan import approval_block_reason
from backend.pipeline.state import TradeState
from backend.tools.book_loader import INSTRUMENTS

logger = logging.getLogger(__name__)

_OUTPUT_DIR = pathlib.Path(
    os.getenv("OUTPUT_DIR", pathlib.Path(__file__).parent.parent.parent.parent / "output")
)


def run(state: TradeState) -> dict:
    """
    Log the approved trade and write a JSON record to output/.

    The record contains:
    - Original trade request (what the trader asked for)
    - Strategy in force, including who chose its slice parameters (origin)
    - C++ metrics (per slice) and the revision they were computed for
    - The full decision log (every revise / refused approve / approve)
    - Instrument metadata used for the units conversion
    """
    # Defence in depth: the router only sends approved, gated states here.
    # If that ever changes, fail loudly rather than dispatch.
    blocked = approval_block_reason(state)
    if blocked:
        raise RuntimeError(f"execution_node reached without a valid approval: {blocked}")

    errors: list[str] = list(state.get("errors", []))

    trade     = state["trade_request"]
    strategy  = state["strategy"]
    metrics   = state["slippage_metrics"]
    thread_id = state.get("thread_id", "unknown")

    record = {
        "timestamp":          datetime.now(timezone.utc).isoformat(),
        "status":             "APPROVED",
        "thread_id":          thread_id,
        "revision_count":     state.get("revision_count", 0),
        "trade_request":      trade,
        "instrument_meta":    INSTRUMENTS.get(trade["instrument"]),
        "strategy":           strategy,
        "slippage_metrics":   metrics,
        "metrics_scope":      "per slice (one child order of shares_per_slice)",
        "simulation_warnings": state.get("simulation_warnings", []),
        "decision_log":       state.get("decision_log", []),
    }

    # ── Log to stdout (visible in uvicorn console) ────────────────────────────
    logger.info("=" * 60)
    logger.info("[execution_node] TRADE APPROVED — dispatching order (POC stub)")
    logger.info(f"  Thread       : {thread_id}")
    logger.info(f"  Instrument   : {trade['instrument']}")
    logger.info(f"  Side         : {trade['side'].upper()}")
    logger.info(f"  Total qty    : {trade['total_shares']:,}")
    logger.info(f"  Strategy     : {strategy['approach']} — {strategy['num_slices']} x "
                f"{strategy['shares_per_slice']:,} (origin={strategy.get('origin')})")
    logger.info(f"  Avg fill     : {metrics['avg_fill_price']:.2f} (price units, per slice)")
    logger.info(f"  Fill ratio   : {metrics['fill_ratio']:.1%}")
    logger.info(f"  Slippage     : {metrics['slippage_bps']:.2f} bps")
    logger.info(f"  Slippage cost: ${metrics['total_cost_usd']:,.2f} (per slice)")
    logger.info(f"  C++ latency  : {metrics['simulation_latency_ns']} ns")
    logger.info(f"  Decisions    : {len(record['decision_log'])}")
    logger.info("=" * 60)

    # ── Write JSON record to output/ ─────────────────────────────────────────
    # Filename is derived from the server-generated thread_id (a UUID), never
    # from user input, so it cannot traverse out of output/ or collide.
    try:
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = _OUTPUT_DIR / f"trade_{thread_id}.json"
        path.write_text(json.dumps(record, indent=2))
        logger.info(f"[execution_node] Record written to {path.name}")
    except Exception as exc:
        msg = f"execution_node: failed to write record ({type(exc).__name__})"
        logger.exception(msg)
        errors.append(msg)

    return {"errors": errors}
