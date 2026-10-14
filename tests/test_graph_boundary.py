"""
Boundary tests for the HITL contract: the API and the graph, end to end.

These attack the seam rather than the math. Each test is a probe a hostile
reviewer would try: bypass the slice bounds through the human override,
approve without numbers, resume a trade that does not exist, approve twice,
or ride an LLM outage through to dispatch.

The LLM is a fake (conftest.fake_llm) and the book is the hand-verifiable one
(conftest.HAND_ASKS / HAND_BIDS: 25 lots per side), so every expected number
below can be checked by hand.
"""

import json
import math

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

TRADE = {"prompt": "Sell 20 ZN by end of day", "instrument": "ZN", "side": "sell",
         "total_shares": 20, "deadline": "end of day"}


def submit(client, **overrides):
    r = client.post("/api/trade", json={**TRADE, **overrides})
    assert r.status_code == 202, r.text
    return r.json()["thread_id"]


def state(client, thread_id):
    r = client.get(f"/api/trade/{thread_id}")
    assert r.status_code == 200, r.text
    return r.json()


def resume(client, thread_id, **payload):
    return client.post(f"/api/resume/{thread_id}", json=payload)


# ── Happy path ────────────────────────────────────────────────────────────────

def test_submit_review_approve_writes_one_record(api):
    client, llm, out = api
    llm.num_slices = 4
    t = submit(client)

    s = state(client, t)
    assert s["status"] == "awaiting_approval"
    assert s["can_approve"] is True
    assert s["strategy"]["origin"] == "llm"
    assert (s["strategy"]["num_slices"], s["strategy"]["shares_per_slice"]) == (4, 5)
    assert s["slippage_metrics"]["for_revision"] == 0
    # SELL 5 lots sweeps the best bid only: 5 @ 99 → zero slippage.
    assert s["slippage_metrics"]["avg_fill_price"] == pytest.approx(99.0)

    r = resume(client, t, action="approve")
    assert r.status_code == 200 and r.json()["status"] == "completed"

    records = list(out.iterdir())
    assert [p.name for p in records] == [f"trade_{t}.json"]
    record = json.loads(records[0].read_text())
    assert record["status"] == "APPROVED"
    assert [d["action"] for d in record["decision_log"]] == ["approve"]


# ── "No LLM arithmetic": the LLM's count is normalised, size is computed ──────

def test_llm_slice_count_is_normalised_so_no_slice_is_empty(api):
    client, llm, _ = api
    llm.num_slices = 8                       # 20 lots / 8 → size 3 → only 7 slices needed
    s = state(client, submit(client))["strategy"]
    assert s["shares_per_slice"] == 3
    assert s["num_slices"] == 7
    assert s["num_slices"] == math.ceil(20 / s["shares_per_slice"])


# ── B1: the human override is bounded exactly like LLM output ────────────────

@pytest.mark.parametrize("override", [
    {"num_slices": 999},
    {"num_slices": 0},
    {"num_slices": "4"},
    {"num_slices": 4.0},
    {"num_slices": 4.5},
    {"num_slices": True},
    {"shares_per_slice": -5},
    {"approach": "YOLO"},
    {"reasoning": "injected rationale"},
    {"num_slices": 4, "origin": "llm"},
    {},
])
def test_invalid_overrides_are_rejected_before_the_graph_runs(api, override):
    client, llm, _ = api
    llm.num_slices = 4
    t = submit(client)
    before = state(client, t)

    r = resume(client, t, action="revise", override_params=override)
    assert r.status_code == 422, r.text

    after = state(client, t)
    assert after["strategy"] == before["strategy"]          # nothing rewritten
    assert after["revision_count"] == 0
    assert after["status"] == "awaiting_approval"           # thread still usable
    assert resume(client, t, action="approve").status_code == 200


def test_revise_without_override_and_approve_with_override_are_rejected(api):
    client, _, _ = api
    t = submit(client)
    assert resume(client, t, action="revise").status_code == 422
    assert resume(client, t, action="approve", override_params={"num_slices": 2}).status_code == 422


def test_num_slices_only_override_recomputes_size(api):
    client, llm, _ = api
    llm.num_slices = 4
    t = submit(client)

    r = resume(client, t, action="revise", override_params={"num_slices": 2})
    assert r.status_code == 200 and r.json()["status"] == "awaiting_approval"

    s = state(client, t)
    assert (s["strategy"]["num_slices"], s["strategy"]["shares_per_slice"]) == (2, 10)
    assert s["strategy"]["origin"] == "trader"
    assert s["strategy"]["reasoning"] == llm.reasoning          # LLM rationale untouched
    assert s["revision_count"] == 1
    assert s["slippage_metrics"]["for_revision"] == 1
    assert s["can_approve"] is True


def test_shares_only_override_recomputes_count(api):
    client, _, _ = api
    t = submit(client)
    assert resume(client, t, action="revise", override_params={"shares_per_slice": 6}).status_code == 200
    s = state(client, t)["strategy"]
    assert (s["num_slices"], s["shares_per_slice"]) == (4, 6)    # 6+6+6+2


@pytest.mark.parametrize("override", [
    {"num_slices": 2, "shares_per_slice": 5},     # 2 x 5 covers 10 of 20
    {"num_slices": 5, "shares_per_slice": 10},    # 10 per slice only needs 2 slices
])
def test_inconsistent_or_uncovering_plans_are_rejected(api, override):
    client, _, _ = api
    t = submit(client)
    assert resume(client, t, action="revise", override_params=override).status_code == 422


def test_slice_size_needing_more_than_max_slices_is_rejected(api):
    client, _, _ = api
    t = submit(client, total_shares=21)
    # 21 lots at 1 per slice needs 21 slices > MAX_SLICES (20)
    assert resume(client, t, action="revise", override_params={"shares_per_slice": 1}).status_code == 422
    # 20 lots at 1 per slice is exactly 20 — allowed
    t = submit(client, total_shares=20)
    assert resume(client, t, action="revise", override_params={"shares_per_slice": 1}).status_code == 200


def test_more_slices_than_lots_is_rejected(api):
    client, _, _ = api
    t = submit(client, total_shares=3)
    assert resume(client, t, action="revise", override_params={"num_slices": 5}).status_code == 422


# ── Trade request validation and the instrument registry (B5) ────────────────

@pytest.mark.parametrize("patch", [
    {"total_shares": 0},
    {"total_shares": -200},
    {"total_shares": "200"},
    {"total_shares": 20.5},
    {"total_shares": 10_000_000},
    {"instrument": "TSLA"},
    {"instrument": "EUR/USD"},
    {"instrument": "../../etc"},
    {"side": "short"},
    {"prompt": "x" * 2001},
    {"unexpected": 1},
])
def test_bad_trade_requests_are_rejected(api, patch):
    client, _, _ = api
    r = client.post("/api/trade", json={**TRADE, **patch})
    assert r.status_code == 422, r.text


def test_side_is_required_not_defaulted(api):
    client, _, _ = api
    body = {k: v for k, v in TRADE.items() if k != "side"}
    assert client.post("/api/trade", json=body).status_code == 422


def test_instrument_is_normalised(api):
    client, _, _ = api
    t = submit(client, instrument=" zn ")
    assert state(client, t)["trade_request"]["instrument"] == "ZN"


# ── B4: the resume API reports what happened, nothing else ───────────────────

def test_unknown_thread_is_404(api):
    client, _, _ = api
    assert client.get("/api/trade/does-not-exist").status_code == 404
    assert resume(client, "does-not-exist", action="approve").status_code == 404


def test_double_approve_is_409_and_dispatches_once(api):
    client, _, out = api
    t = submit(client)
    assert resume(client, t, action="approve").status_code == 200
    r = resume(client, t, action="approve")
    assert r.status_code == 409 and "completed" in r.json()["detail"]
    assert len(list(out.iterdir())) == 1


def test_abort_reports_aborted_and_cannot_be_approved_later(api):
    client, _, out = api
    t = submit(client)
    r = resume(client, t, action="abort")
    assert r.status_code == 200 and r.json()["status"] == "aborted"
    assert state(client, t)["status"] == "aborted"
    assert resume(client, t, action="approve").status_code == 409
    assert list(out.iterdir()) == []


# ── B2: approval needs current numbers; LLM outage fails closed ──────────────

def test_llm_outage_falls_back_but_cannot_be_approved_until_a_trader_confirms(api):
    client, llm, out = api
    llm.fail = True
    t = submit(client)

    s = state(client, t)
    assert s["strategy"]["origin"] == "fallback"
    assert s["can_approve"] is False
    assert "fallback" in s["approval_blocked_reason"].lower()
    assert all("simulated LLM outage" not in e for e in s["errors"])   # no provider detail leaks

    assert resume(client, t, action="approve").status_code == 409
    assert list(out.iterdir()) == []

    assert resume(client, t, action="revise", override_params={"num_slices": 5}).status_code == 200
    assert state(client, t)["can_approve"] is True
    assert resume(client, t, action="approve").status_code == 200


def test_engine_failure_clears_metrics_and_blocks_approval(api, monkeypatch):
    client, _, out = api
    t = submit(client)
    assert state(client, t)["slippage_metrics"] is not None

    from backend.pipeline.nodes import simulation_node
    real_get_simulator = simulation_node.get_simulator

    class Broken:
        def load_book(self, **_):
            pass

        def simulate(self, **_):
            raise RuntimeError("engine fault")

    monkeypatch.setattr(simulation_node, "get_simulator", lambda: Broken())
    assert resume(client, t, action="revise", override_params={"num_slices": 2}).status_code == 200

    s = state(client, t)
    assert s["status"] == "awaiting_approval"
    assert s["slippage_metrics"] is None                    # old numbers are NOT kept
    assert "engine fault" in s["simulation_error"]
    assert s["can_approve"] is False
    assert resume(client, t, action="approve").status_code == 409
    assert list(out.iterdir()) == []

    monkeypatch.setattr(simulation_node, "get_simulator", real_get_simulator)   # engine healthy again
    assert resume(client, t, action="revise", override_params={"num_slices": 4}).status_code == 200
    assert state(client, t)["can_approve"] is True


def test_partial_fill_is_surfaced_as_a_warning(api):
    client, llm, _ = api
    llm.num_slices = 1
    t = submit(client, total_shares=100)          # one 100-lot slice vs 25 lots of bids
    s = state(client, t)
    assert s["slippage_metrics"]["fill_ratio"] == pytest.approx(0.25)
    assert any("Partial fill" in w for w in s["simulation_warnings"])


def test_decision_log_records_every_decision_in_order(api):
    client, _, out = api
    t = submit(client)
    resume(client, t, action="revise", override_params={"num_slices": 2})
    resume(client, t, action="revise", override_params={"num_slices": 5})
    resume(client, t, action="approve")
    record = json.loads((out / f"trade_{t}.json").read_text())
    assert [(d["action"], d["revision"]) for d in record["decision_log"]] == [
        ("revise", 0), ("revise", 1), ("approve", 2),
    ]


# ── The graph defends itself even if the API is bypassed ─────────────────────

def _graph(monkeypatch, tmp_path):
    from backend.pipeline.graph import build_graph
    from backend.pipeline.nodes import execution_node, simulation_node
    from tests.conftest import HAND_ASKS, HAND_BIDS

    monkeypatch.setattr(simulation_node, "load_zn_book", lambda: (HAND_ASKS, HAND_BIDS))
    monkeypatch.setattr(execution_node, "_OUTPUT_DIR", tmp_path)
    return build_graph(InMemorySaver())


def _start(graph, thread_id="t1", total=20):
    config = {"configurable": {"thread_id": thread_id}}
    graph.invoke({
        "trade_request": {**TRADE, "total_shares": total},
        "thread_id": thread_id, "revision_count": 0, "errors": [],
        "decision_log": [], "simulation_warnings": [], "simulation_error": None,
    }, config=config)
    return config


# (Command(resume=None) is not a resume in LangGraph itself, so it is not probed here.)
@pytest.mark.parametrize("payload", ["garbage", 42, {"action": "launch"}, {"no": "action"}])
def test_graph_repauses_on_malformed_resume(fake_llm, monkeypatch, tmp_path, payload):
    graph = _graph(monkeypatch, tmp_path)
    config = _start(graph)
    graph.invoke(Command(resume=payload), config=config)
    snap = graph.get_state(config)
    assert snap.next == ("hitl_node",)
    assert snap.values["decision_log"][-1]["outcome"].startswith("refused")
    assert list(tmp_path.iterdir()) == []


def test_graph_refuses_approve_of_unconfirmed_fallback(fake_llm, monkeypatch, tmp_path):
    fake_llm.fail = True
    graph = _graph(monkeypatch, tmp_path)
    config = _start(graph)
    graph.invoke(Command(resume={"action": "approve", "override_params": None}), config=config)
    snap = graph.get_state(config)
    assert snap.next == ("hitl_node",)                       # paused again, not dispatched
    assert "refused" in snap.values["decision_log"][-1]["outcome"]
    assert list(tmp_path.iterdir()) == []
