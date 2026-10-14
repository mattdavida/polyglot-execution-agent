r"""
Shared pytest fixtures.

The C++ execution_engine module must be built before running these tests:
    Windows:  .\cpp\build.ps1                       (→ cpp/build-ninja/)
    Linux:    cmake -S cpp -B cpp/build -G Ninja \
                -Dpybind11_DIR="$(python -m pybind11 --cmakedir)" \
              && cmake --build cpp/build            (→ cpp/build/)
Set EXECUTION_ENGINE_BUILD_DIR to use a module built somewhere else.

No test needs Azure credentials or network: the LLM is replaced by a fake
(see `fake_llm`), and the API tests run against a temporary checkpoint DB and
a temporary output directory.
"""

import os
import pathlib
import sys
import tempfile
import types

import pytest

# Make the repo root and the compiled module importable regardless of cwd.
_REPO_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))
for _build in ("build", "build-ninja"):
    sys.path.insert(0, str(_REPO_ROOT / "cpp" / _build))
if os.getenv("EXECUTION_ENGINE_BUILD_DIR"):
    sys.path.insert(0, os.environ["EXECUTION_ENGINE_BUILD_DIR"])

# Dummy credentials so backend.config imports. They are set BEFORE any backend
# import and override a developer's real .env, so a test can never reach Azure.
os.environ["AZURE_OPENAI_API_KEY"] = "test-not-a-key"
os.environ["AZURE_OPENAI_ENDPOINT"] = "https://example.invalid/"
os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"] = "test-deployment"
os.environ["CHECKPOINTS_DB"] = str(pathlib.Path(tempfile.mkdtemp()) / "checkpoints.db")


# ── C++ engine ────────────────────────────────────────────────────────────────

HAND_ASKS = [(100.0, 10), (101.0, 10), (102.0, 5)]
HAND_BIDS = [(99.0, 10), (98.0, 10), (97.0, 5)]


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
    sim.load_book(asks=HAND_ASKS, bids=HAND_BIDS)
    return sim


# ── Fake LLM ──────────────────────────────────────────────────────────────────

class FakeLLM:
    """Stands in for AzureChatOpenAI. Configure per test via the fixture."""

    def __init__(self):
        self.approach = "TWAP"
        self.num_slices = 8
        self.reasoning = "Steady schedule into the close."
        self.fail = False

    def with_structured_output(self, schema):
        outer = self

        class _Structured:
            def invoke(self, _messages):
                if outer.fail:
                    raise ConnectionError("simulated LLM outage")
                return schema(
                    approach=outer.approach,
                    num_slices=outer.num_slices,
                    reasoning=outer.reasoning,
                )

        return _Structured()


@pytest.fixture()
def fake_llm(monkeypatch):
    llm = FakeLLM()
    module = types.ModuleType("backend.tools.llm_client")
    module.get_chat_llm = lambda temperature=0.2: llm
    monkeypatch.setitem(sys.modules, "backend.tools.llm_client", module)
    return llm


# ── API client ────────────────────────────────────────────────────────────────

@pytest.fixture()
def api(fake_llm, monkeypatch, tmp_path):
    """
    FastAPI TestClient over the real graph, with:
      - the fake LLM
      - the hand-verifiable book instead of the market-data CSV (deterministic)
      - approval records written to a temp directory
    Yields (client, fake_llm, output_dir).
    """
    from fastapi.testclient import TestClient

    import backend.main as main
    from backend.pipeline.nodes import execution_node, simulation_node

    monkeypatch.setattr(simulation_node, "load_zn_book", lambda: (HAND_ASKS, HAND_BIDS))
    monkeypatch.setattr(execution_node, "_OUTPUT_DIR", tmp_path)

    with TestClient(main.app) as client:
        yield client, fake_llm, tmp_path
