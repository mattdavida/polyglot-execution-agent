"""
Execution node — POC stub for trade dispatch.

In production, this node would send the approved trade to an OMS (Order
Management System) via FIX protocol, REST API, or message queue. For the POC,
it logs the approved order to stdout and writes a JSON record to output/.

This is intentionally kept minimal — the architectural value of the POC is
in the HITL decision loop, not the downstream execution plumbing.

Input state keys:  trade_request, strategy, slippage_metrics, human_feedback
Output state keys: errors
"""

import json
import logging
import pathlib
from datetime import datetime, timezone

from backend.pipeline.state import TradeState

logger = logging.getLogger(__name__)

_OUTPUT_DIR = pathlib.Path(__file__).parent.parent.parent.parent / "output"


def run(state: TradeState) -> dict:
    """
    Log the approved trade and write a JSON record to output/.

    The output record contains everything a real OMS ticket would need:
    - Original trade request (what the trader asked for)
    - LLM strategy (how the AI proposed to execute it)
    - C++ metrics (the exact cost the C++ engine computed)
    - Human feedback (what the trader approved/modified)
    - Timestamp
    """
    errors: list[str] = list(state.get("errors", []))

    trade    = state.get("trade_request", {})
    strategy = state.get("strategy", {})
    metrics  = state.get("slippage_metrics", {})
    feedback = state.get("human_feedback", {})
    revision = state.get("revision_count", 0)

    record = {
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "status":           "APPROVED",
        "revision_count":   revision,
        "trade_request":    trade,
        "strategy":         strategy,
        "slippage_metrics": metrics,
        "human_feedback":   feedback,
    }

    # ── Log to stdout (visible in uvicorn console) ────────────────────────────
    logger.info("=" * 60)
    logger.info("[execution_node] TRADE APPROVED — dispatching order")
    logger.info(f"  Instrument   : {trade.get('instrument', 'N/A')}")
    logger.info(f"  Side         : {trade.get('side', 'sell').upper()}")
    logger.info(f"  Total shares : {trade.get('total_shares', 0):,}")
    logger.info(f"  Strategy     : {strategy.get('approach')} — {strategy.get('num_slices')} slices")
    logger.info(f"  Avg fill     : {metrics.get('avg_fill_price', 0):.2f} (price units)")
    logger.info(f"  Fill ratio   : {metrics.get('fill_ratio', 0):.1%}")
    logger.info(f"  Slippage     : {metrics.get('slippage_bps', 0):.2f} bps")
    logger.info(f"  Slippage cost: ${metrics.get('total_cost_usd', 0):,.2f}")
    logger.info(f"  C++ latency  : {metrics.get('simulation_latency_us', 0)} µs")
    logger.info(f"  Revisions    : {revision}")
    logger.info("=" * 60)

    # ── Write JSON record to output/ ─────────────────────────────────────────
    try:
        _OUTPUT_DIR.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        instrument = trade.get("instrument", "UNKNOWN").replace(" ", "_")
        path = _OUTPUT_DIR / f"trade_{instrument}_{ts}.json"
        path.write_text(json.dumps(record, indent=2))
        logger.info(f"[execution_node] Record written to {path}")
    except Exception as exc:
        msg = f"execution_node: failed to write record — {exc}"
        logger.warning(msg)
        errors.append(msg)

    return {"errors": errors}
