"""
Tests for the Python side of the C++/LLM boundary:
  - deterministic slice sizing (arithmetic never comes from the LLM)
  - price-unit → USD conversion via instrument tick metadata
  - side-string → C++ enum mapping
"""

import pytest

from backend.pipeline.nodes.strategy_node import _slice_size, StrategyOutput
from backend.tools.book_loader import price_units_to_usd, ZN_METADATA


# ── Slice sizing ───────────────────────────────────────────────────────────────

class TestSliceSize:
    def test_even_division(self):
        assert _slice_size(50_000, 5) == 10_000

    def test_ceil_division_always_covers_the_order(self):
        # 100 / 3 → 34 per slice; 3 x 34 = 102 >= 100.
        assert _slice_size(100, 3) == 34
        for total in (1, 7, 99, 200, 50_000):
            for slices in (1, 2, 3, 5, 7, 20):
                assert _slice_size(total, slices) * slices >= total

    def test_single_slice_is_full_order(self):
        assert _slice_size(200, 1) == 200


class TestStrategySchema:
    def test_llm_schema_has_no_arithmetic_fields(self):
        # shares_per_slice must NOT be an LLM output — it is computed in code.
        assert "shares_per_slice" not in StrategyOutput.model_fields

    def test_num_slices_bounds_enforced_by_pydantic(self):
        with pytest.raises(Exception):
            StrategyOutput(approach="VWAP", num_slices=0, reasoning="x")
        with pytest.raises(Exception):
            StrategyOutput(approach="VWAP", num_slices=21, reasoning="x")
        assert StrategyOutput(approach="VWAP", num_slices=20, reasoning="x").num_slices == 20


# ── Unit conversion ────────────────────────────────────────────────────────────

class TestUnitConversion:
    def test_one_tick_one_contract(self):
        # An adverse move of exactly one tick on one contract = tick_value.
        one_tick = ZN_METADATA["tick_size"]
        assert price_units_to_usd(one_tick) == pytest.approx(ZN_METADATA["tick_value"])

    def test_scales_linearly_with_contracts(self):
        # 2 ticks adverse x 100 contracts, expressed as price units:
        cost_price_units = 2 * ZN_METADATA["tick_size"] * 100
        expected = 2 * ZN_METADATA["tick_value"] * 100
        assert price_units_to_usd(cost_price_units) == pytest.approx(expected)

    def test_zero_cost_is_zero_dollars(self):
        assert price_units_to_usd(0.0) == 0.0

    def test_documented_tick_value_matches_conversion(self):
        # One tick = 0.5 price units = $15.625 per ZN contract (book_loader UNITS).
        assert ZN_METADATA["tick_size"] == 0.5
        assert price_units_to_usd(0.5) == pytest.approx(15.625)

    def test_unregistered_instrument_has_no_units(self):
        with pytest.raises(KeyError):
            price_units_to_usd(1.0, "TSLA")


# ── Side mapping ───────────────────────────────────────────────────────────────

class TestSideMapping:
    def test_side_strings_map_to_engine_enum(self):
        import execution_engine as ee
        from backend.tools.cpp_bridge import get_side

        assert get_side("buy") == ee.Side.BUY
        assert get_side("BUY") == ee.Side.BUY
        assert get_side("sell") == ee.Side.SELL
        assert get_side("Sell") == ee.Side.SELL

    @pytest.mark.parametrize("bad", ["", "short", "hold", None, 1])
    def test_unknown_side_raises_instead_of_defaulting(self, bad):
        from backend.tools.cpp_bridge import get_side
        with pytest.raises(ValueError):
            get_side(bad)


# ── Plan rules shared by the API and the graph (backend.pipeline.plan) ─────────

from backend.pipeline.plan import (  # noqa: E402
    MAX_SLICES,
    apply_override,
    approval_block_reason,
    plan_from_slice_count,
)

BASE = {"approach": "TWAP", "num_slices": 4, "shares_per_slice": 5, "reasoning": "r", "origin": "llm"}


class TestPlanFromSliceCount:
    @pytest.mark.parametrize("total", [1, 3, 7, 10, 20, 99, 200, 50_000])
    @pytest.mark.parametrize("requested", [1, 2, 3, 5, 6, 7, 8, 20])
    def test_invariant_no_empty_slice_and_full_coverage(self, total, requested):
        n, s = plan_from_slice_count(total, requested)
        assert 1 <= n <= MAX_SLICES
        assert n * s >= total                 # covers the order
        assert (n - 1) * s < total            # last slice is not empty

    def test_six_slices_of_ten_lots_becomes_five(self):
        assert plan_from_slice_count(10, 6) == (5, 2)


class TestApplyOverride:
    def test_num_slices_only(self):
        out = apply_override(BASE, {"num_slices": 2}, 20)
        assert (out["num_slices"], out["shares_per_slice"], out["origin"]) == (2, 10, "trader")
        assert out["reasoning"] == "r" and out["approach"] == "TWAP"
        assert BASE["num_slices"] == 4        # input not mutated

    def test_shares_only(self):
        out = apply_override(BASE, {"shares_per_slice": 6}, 20)
        assert (out["num_slices"], out["shares_per_slice"]) == (4, 6)

    @pytest.mark.parametrize("override", [
        {}, {"approach": "VWAP"}, {"reasoning": "x"}, {"origin": "llm"},
        {"num_slices": 0}, {"num_slices": 21}, {"num_slices": "4"}, {"num_slices": 4.0},
        {"num_slices": True}, {"shares_per_slice": 0}, {"shares_per_slice": 21},
        {"num_slices": 2, "shares_per_slice": 5}, {"num_slices": 25},
    ])
    def test_rejected(self, override):
        with pytest.raises(ValueError):
            apply_override(BASE, override, 20)

    def test_more_slices_than_lots(self):
        with pytest.raises(ValueError):
            apply_override(BASE, {"num_slices": 5}, 3)


class TestApprovalGate:
    METRICS = {"for_revision": 0}

    def ok_state(self, **kw):
        return {"strategy": dict(BASE), "slippage_metrics": dict(self.METRICS),
                "simulation_error": None, "revision_count": 0, **kw}

    def test_clean_state_is_approvable(self):
        assert approval_block_reason(self.ok_state()) is None

    @pytest.mark.parametrize("patch", [
        {"strategy": None},
        {"strategy": {**BASE, "origin": "fallback"}},
        {"slippage_metrics": None},
        {"simulation_error": "boom"},
        {"revision_count": 1},                      # metrics computed for revision 0 → stale
    ])
    def test_blocked(self, patch):
        assert approval_block_reason(self.ok_state(**patch))
