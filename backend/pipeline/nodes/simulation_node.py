"""
Simulation node — C++ LOB compute core.

This is where the two systems meet: Python passes the LLM's strategy into
the C++20 execution_engine via pybind11, and gets back deterministic, precise
slippage metrics in microseconds.

The LLM never sees these numbers until after the C++ has computed them.
The human trader sees both side-by-side in the HITL review panel.

This node handles BOTH the initial simulation AND the revise path:
  - Initial: strategy arrives fresh from strategy_node
  - Revise:  human_feedback.override_params may modify strategy before re-running

Market data:
  Uses a real ZN (10-Year Treasury Note Futures) Level 2 book reconstructed
  from Bloomberg tick data (2016-12-23) via book_loader.py. Falls back to a
  hardcoded dummy book if the CSV file is not available (e.g. CI environments).

Input state keys:  strategy, human_feedback (optional — only on revise path)
Output state keys: slippage_metrics, revision_count, human_feedback (cleared), errors
"""

import logging
from backend.pipeline.state import TradeState
from backend.tools.cpp_bridge import get_simulator
from backend.tools.book_loader import load_zn_book

logger = logging.getLogger(__name__)

# ── Fallback book (used only if CSV is unavailable) ───────────────────────────
# Kept for CI/CD environments where data/ is not present.
_FALLBACK_ASKS = [
    (6987.0, 6),
    (6987.5, 4),
    (6988.0, 9),
    (6988.5, 3),
    (6989.0, 12),
]
_FALLBACK_BIDS = [
    (6986.5, 6),
    (6986.0, 3),
    (6985.5, 5),
    (6985.0, 11),
    (6984.5, 4),
]


def _get_book() -> tuple[list, list]:
    """
    Return the best available order book.

    Tries to load the real ZN L2 book from the Bloomberg CSV first.
    Falls back to the hardcoded approximation if the file is missing.
    The lru_cache in load_zn_book() means the CSV is only parsed once
    across the entire application lifetime.
    """
    try:
        asks, bids = load_zn_book()
        return asks, bids
    except FileNotFoundError:
        logger.warning(
            "[simulation_node] ZN CSV not found — using fallback dummy book. "
            "Copy data/2016_12_23.csv to use real market depth."
        )
        return _FALLBACK_ASKS, _FALLBACK_BIDS


def run(state: TradeState) -> dict:
    """
    Run the C++ LOB simulation for one slice of the strategy.

    On the revise path, override_params from human_feedback are applied to
    the strategy before simulation runs. revision_count is incremented so
    the UI can show "Revision 1", "Revision 2", etc.
    """
    errors: list[str] = list(state.get("errors", []))
    strategy       = dict(state.get("strategy", {}))
    human_feedback = state.get("human_feedback")
    revision_count = state.get("revision_count", 0)

    # ── Revise path: apply trader override params ──────────────────────────────
    # When the trader modifies num_slices or shares_per_slice and clicks MODIFY,
    # human_feedback.override_params carries the new values. We merge them into
    # strategy before re-running the simulation.
    if human_feedback and human_feedback.get("action") == "revise":
        override = human_feedback.get("override_params") or {}
        if override:
            strategy.update(override)
            logger.info(f"[simulation_node] revise path — applying overrides: {override}")
        revision_count += 1

    order_size = strategy.get("shares_per_slice", 50)
    if order_size <= 0:
        errors.append(f"simulation_node: invalid order_size={order_size}")
        return {"errors": errors}

    try:
        # ── Market data ───────────────────────────────────────────────────────
        # Load the real ZN order book (parsed from Bloomberg CSV, lru_cached).
        # This is the book the C++ engine will sweep to compute slippage.
        asks, bids = _get_book()

        # ── C++ compute ────────────────────────────────────────────────────────
        # get_simulator() returns a fresh ExecutionSimulator (no shared state).
        # load_book() populates the pre-allocated intrusive linked-list LOB.
        # simulate() is the zero-allocation sweep — returns in microseconds.
        sim = get_simulator()
        sim.load_book(asks=asks, bids=bids)
        cpp_result = sim.simulate(order_size=order_size)

        metrics = {
            "avg_fill_price":        cpp_result.avg_fill_price,
            "slippage_bps":          cpp_result.slippage_bps,
            "market_impact_bps":     cpp_result.market_impact_bps,
            "total_cost_usd":        cpp_result.total_cost_usd,
            "simulation_latency_us": cpp_result.simulation_latency_us,
        }

        logger.info(
            f"[simulation_node] order={order_size:,} contracts  "
            f"avg_fill={cpp_result.avg_fill_price:.1f}  "
            f"slippage={cpp_result.slippage_bps:.4f}bps  "
            f"latency={cpp_result.simulation_latency_us}us"
        )

        return {
            "strategy":         strategy,
            "slippage_metrics": metrics,
            "revision_count":   revision_count,
            "human_feedback":   None,   # clear — fresh pause at hitl_node
            "errors":           errors,
        }

    except Exception as exc:
        msg = f"simulation_node: C++ engine error — {exc}"
        logger.exception(msg)
        errors.append(msg)
        return {"errors": errors}
