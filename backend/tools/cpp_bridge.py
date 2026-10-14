"""
C++ pybind11 bridge loader.

Adds the build output directory to sys.path once, then imports and
returns the compiled execution_engine module. The module is cached after
the first import — subsequent calls return the same module object at zero cost.

Why a dedicated module?
  - Keeps the sys.path manipulation in one place rather than in every node.
  - Provides a clean fallback if the .pyd hasn't been built yet (raises
    a clear ImportError with instructions rather than a cryptic path error).
  - Makes it easy to swap in a mock for unit tests.
"""

import sys
import pathlib
import logging

logger = logging.getLogger(__name__)

# Resolve the build directory relative to the repo root regardless of cwd.
# __file__ is backend/tools/cpp_bridge.py → go up 2 levels → repo root.
_REPO_ROOT  = pathlib.Path(__file__).parent.parent.parent
_BUILD_DIR  = _REPO_ROOT / "cpp" / "build-ninja"

if str(_BUILD_DIR) not in sys.path:
    sys.path.insert(0, str(_BUILD_DIR))

try:
    import execution_engine as _engine  # type: ignore
    logger.info(f"[cpp_bridge] execution_engine loaded from {_BUILD_DIR}")
except ImportError as exc:
    raise ImportError(
        f"Could not import execution_engine from {_BUILD_DIR}.\n"
        f"Run .\\cpp\\build.ps1 to compile the C++ module first.\n"
        f"Original error: {exc}"
    ) from exc

# ── Stale-build guard ─────────────────────────────────────────────────────────
# The compiled module is not rebuilt automatically when the C++ source changes.
# A stale .pyd/.so imports fine and then fails mid-request on a missing field,
# so check the interface this backend depends on once, at import, and refuse
# to start with a clear instruction instead.
_REQUIRED = {
    "SimulationResult": ("simulation_latency_ns", "total_filled", "total_cost"),
    "Side": ("BUY", "SELL"),
}
_missing = [
    f"{cls}.{attr}"
    for cls, attrs in _REQUIRED.items()
    for attr in attrs
    if not hasattr(getattr(_engine, cls, None), attr)
]
if _missing or hasattr(_engine.ExecutionSimulator, "calculate_impact"):
    raise ImportError(
        f"execution_engine at {getattr(_engine, '__file__', _BUILD_DIR)} is a stale build "
        f"(missing: {', '.join(_missing) or 'none'}; legacy API present: "
        f"{hasattr(_engine.ExecutionSimulator, 'calculate_impact')}).\n"
        f"Rebuild it: .\\cpp\\build.ps1 (Windows) or cmake --build cpp/build (Linux)."
    )


def get_simulator() -> "_engine.ExecutionSimulator":
    """
    Return a fresh ExecutionSimulator instance.

    Each LangGraph simulation_node call gets its own instance — no shared
    mutable state between concurrent requests. Thread safety via ownership.
    """
    return _engine.ExecutionSimulator()


def get_side(side: str) -> "_engine.Side":
    """
    Map the API-level side string to the C++ Side enum.

    "buy"  → Side.BUY  (sweeps asks)
    "sell" → Side.SELL (sweeps bids — the liquidation path)
    Anything else raises — direction is never defaulted or guessed.
    """
    normalised = side.lower() if isinstance(side, str) else None
    if normalised == "buy":
        return _engine.Side.BUY
    if normalised == "sell":
        return _engine.Side.SELL
    raise ValueError(f"unknown side: {side!r} (expected 'buy' or 'sell')")
