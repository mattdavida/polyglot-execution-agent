"""
Simulation node — C++ LOB compute core.

This is where the two systems meet: Python passes the current plan into
the C++20 execution_engine via pybind11, and gets back deterministic, precise
slippage metrics.

The LLM never sees these numbers until after the C++ has computed them.
The human trader sees both side-by-side in the HITL review panel.

This node handles BOTH the initial simulation AND the revise path:
  - Initial: strategy arrives fresh from strategy_node
  - Revise:  human_feedback.override_params is applied through
             backend.pipeline.plan.apply_override — the same validation the API
             ran — so the plan stays internally consistent

What is simulated (POC scope — see docs/known-gaps.md):
  ONE child order of `shares_per_slice` contracts, swept immediately against a
  static book snapshot. The approach label (VWAP / TWAP / Sweep / Iceberg) does
  not change the numbers; only the slice size does. Metrics are PER SLICE.

Failure semantics (the HITL gate depends on these):
  - On any failure for the current plan, slippage_metrics is set to None and
    simulation_error is set. Stale metrics from an earlier plan are never left
    in state, so the trader cannot approve a plan on another plan's numbers.
  - Metrics carry `for_revision`, the revision they were computed for.

Market data:
  Uses a real-world ZN (10-Year Treasury Note Futures) Level 2 book
  reconstructed from a tick dataset via book_loader.py. Falls back to a
  hardcoded synthetic book if the CSV is not available (e.g. CI) — and says so
  in simulation_warnings.

Units contract (the integration boundary):
  The C++ engine is instrument-agnostic — it returns avg_fill_price and
  total_cost in the *price units of the loaded book*. THIS node owns the
  conversion to currency via the instrument registry in book_loader. In
  production this is exactly where the firm's instrument reference data plugs in.

Input state keys:  trade_request, strategy, human_feedback (revise path), revision_count
Output state keys: strategy, slippage_metrics, simulation_error, simulation_warnings,
                   revision_count, human_feedback (cleared), errors
"""

import logging

from backend.pipeline.plan import apply_override
from backend.pipeline.state import TradeState
from backend.tools.book_loader import INSTRUMENTS, load_zn_book, price_units_to_usd
from backend.tools.cpp_bridge import get_side, get_simulator

logger = logging.getLogger(__name__)

# ── Fallback book (used only if the CSV is unavailable) ───────────────────────
# Synthetic — for CI/CD environments where data/ is not present. Its use is
# surfaced to the trader as a warning; numbers from it are not market numbers.
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


def _get_book(instrument: str) -> tuple[list, list, list[str]]:
    """
    Return (asks, bids, warnings) for the instrument.

    Only ZN has a book source. The API rejects other instruments; this check
    is defence in depth so an unregistered instrument can never be priced
    against the ZN book.
    """
    if instrument not in INSTRUMENTS:
        raise ValueError(f"no book source for instrument {instrument!r}")
    try:
        asks, bids = load_zn_book()
        return asks, bids, []
    except FileNotFoundError:
        logger.warning("[simulation_node] ZN CSV not found — using synthetic fallback book.")
        return _FALLBACK_ASKS, _FALLBACK_BIDS, [
            "Using the synthetic fallback book (market data file not present). "
            "These are not market numbers."
        ]


def _failed(errors: list[str], message: str, strategy: dict, revision_count: int) -> dict:
    """State update for a failed simulation of the CURRENT plan."""
    errors.append(f"simulation_node: {message}")
    return {
        "strategy":            strategy,
        "slippage_metrics":    None,     # never leave another plan's numbers in place
        "simulation_error":    message,
        "simulation_warnings": [],
        "revision_count":      revision_count,
        "human_feedback":      None,
        "errors":              errors,
    }


def run(state: TradeState) -> dict:
    """
    Run the C++ LOB simulation for one slice of the current plan.

    On the revise path, override_params are applied (validated) and
    revision_count is incremented so the UI can show "Revision 1", "Revision 2".
    """
    errors: list[str] = list(state.get("errors", []))
    trade          = state.get("trade_request") or {}
    strategy       = dict(state.get("strategy") or {})
    human_feedback = state.get("human_feedback")
    revision_count = state.get("revision_count", 0)

    # ── Revise path: apply the trader's override through the plan rules ───────
    if human_feedback and human_feedback.get("action") == "revise":
        try:
            strategy = apply_override(
                strategy,
                human_feedback.get("override_params") or {},
                trade.get("total_shares", 0),
            )
        except ValueError as exc:
            # The API validates overrides before resuming, so this is defence
            # in depth. The previous plan and its metrics are still consistent
            # with each other, so they are kept; the rejection is surfaced.
            msg = f"override rejected: {exc}"
            errors.append(f"simulation_node: {msg}")
            return {
                "human_feedback":      None,
                "simulation_warnings": list(state.get("simulation_warnings") or []) + [msg],
                "errors":              errors,
            }
        revision_count += 1
        logger.info(
            f"[simulation_node] revise → revision {revision_count}: "
            f"{strategy['num_slices']}x{strategy['shares_per_slice']}"
        )

    try:
        order_size = strategy.get("shares_per_slice")
        if not isinstance(order_size, int) or isinstance(order_size, bool) or order_size <= 0:
            raise ValueError(f"invalid slice size {order_size!r}")

        # Order direction comes from the trade request — deterministic, never
        # inferred by the LLM, never defaulted. A liquidation is a sell and
        # must sweep the bid side.
        side_str   = trade.get("side")
        side       = get_side(side_str)
        instrument = trade.get("instrument")

        # ── Market data ───────────────────────────────────────────────────────
        asks, bids, warnings = _get_book(instrument)

        # ── C++ compute ───────────────────────────────────────────────────────
        # get_simulator() returns a fresh ExecutionSimulator (no shared state):
        # simulate() releases the GIL, so an instance must never be reloaded
        # while another thread may be sweeping it.
        sim = get_simulator()
        sim.load_book(asks=asks, bids=bids)     # validates the book (raises ValueError)
        cpp_result = sim.simulate(order_size=order_size, side=side)

        # ── Unit conversion: price units → USD (instrument registry) ─────────
        total_cost_usd = price_units_to_usd(cpp_result.total_cost, instrument)
        fill_ratio     = cpp_result.total_filled / order_size

        metrics = {
            "avg_fill_price":        cpp_result.avg_fill_price,
            "slippage_bps":          cpp_result.slippage_bps,
            "market_impact_bps":     cpp_result.market_impact_bps,
            "total_cost_usd":        total_cost_usd,
            "total_filled":          cpp_result.total_filled,
            "fill_ratio":            fill_ratio,
            "simulation_latency_ns": cpp_result.simulation_latency_ns,
            "for_revision":          revision_count,
        }

        if fill_ratio < 1.0:
            # Book exhausted before the slice completed — the trader must see this.
            warnings.append(
                f"Partial fill: only {cpp_result.total_filled:,} of {order_size:,} "
                f"contracts filled ({fill_ratio:.1%}). Book depth is insufficient "
                f"for this slice size."
            )

        logger.info(
            f"[simulation_node] {side_str.upper()} order={order_size:,} contracts  "
            f"filled={cpp_result.total_filled:,} ({fill_ratio:.1%})  "
            f"avg_fill={cpp_result.avg_fill_price:.1f}  "
            f"slippage={cpp_result.slippage_bps:.4f}bps  "
            f"cost=${total_cost_usd:,.2f}  "
            f"latency={cpp_result.simulation_latency_ns}ns"
        )

        return {
            "strategy":            strategy,
            "slippage_metrics":    metrics,
            "simulation_error":    None,
            "simulation_warnings": warnings,
            "revision_count":      revision_count,
            "human_feedback":      None,   # clear — fresh pause at hitl_node
            "errors":              errors,
        }

    except Exception as exc:
        logger.exception("[simulation_node] simulation failed")
        return _failed(errors, f"{type(exc).__name__}: {exc}", strategy, revision_count)
