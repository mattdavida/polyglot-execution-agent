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


def get_simulator() -> "_engine.ExecutionSimulator":
    """
    Return a fresh ExecutionSimulator instance.

    Each LangGraph simulation_node call gets its own instance — no shared
    mutable state between concurrent requests. Thread safety via ownership.
    """
    return _engine.ExecutionSimulator()
