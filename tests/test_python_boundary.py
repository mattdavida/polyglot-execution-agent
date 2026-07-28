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


# ── Side mapping ───────────────────────────────────────────────────────────────

class TestSideMapping:
    def test_side_strings_map_to_engine_enum(self):
        import execution_engine as ee
        from backend.tools.cpp_bridge import get_side

        assert get_side("buy") == ee.Side.BUY
        assert get_side("BUY") == ee.Side.BUY
        assert get_side("sell") == ee.Side.SELL
        assert get_side("Sell") == ee.Side.SELL
