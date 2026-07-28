r"""
Shared pytest fixtures.

The C++ execution_engine module must be built before running these tests:
    .\cpp\build.ps1
"""

import pathlib
import sys

import pytest

# Make the repo root importable regardless of where pytest is invoked from.
_REPO_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "cpp" / "build-ninja"))


@pytest.fixture(scope="session")
def engine():
    """The compiled C++ pybind11 module."""
    import execution_engine
    return execution_engine


@pytest.fixture()
def simulator(engine):
    """A fresh ExecutionSimulator loaded with a small, hand-verifiable book.

    Asks (ascending): 100.0 x 10, 101.0 x 10, 102.0 x 5   (25 total)
    Bids (descending): 99.0 x 10,  98.0 x 10,  97.0 x 5   (25 total)

    Deliberately asymmetric prices so a test that sweeps the wrong side
    produces obviously wrong numbers.
    """
    sim = engine.ExecutionSimulator()
    sim.load_book(
        asks=[(100.0, 10), (101.0, 10), (102.0, 5)],
        bids=[(99.0, 10), (98.0, 10), (97.0, 5)],
    )
    return sim
