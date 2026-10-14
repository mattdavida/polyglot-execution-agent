"""
Deterministic-core correctness tests.

The entire value proposition of this architecture is that the C++ engine's
numbers are exact and verifiable. These tests verify them by hand against a
known book (see the `simulator` fixture in conftest.py):

    Asks: 100.0 x 10, 101.0 x 10, 102.0 x 5
    Bids:  99.0 x 10,  98.0 x 10,  97.0 x 5
"""

import pytest


# ── Buy sweep (asks) ───────────────────────────────────────────────────────────

class TestBuySweep:
    def test_fill_at_top_of_book_has_zero_slippage(self, engine, simulator):
        r = simulator.simulate(order_size=10, side=engine.Side.BUY)
        assert r.total_filled == 10
        assert r.avg_fill_price == pytest.approx(100.0)
        assert r.slippage_bps == pytest.approx(0.0)
        assert r.total_cost == pytest.approx(0.0)

    def test_multi_level_sweep_math(self, engine, simulator):
        # 15 lots: 10 @ 100 + 5 @ 101 → VWAP 100.3333, arrival 100.
        r = simulator.simulate(order_size=15, side=engine.Side.BUY)
        assert r.total_filled == 15
        assert r.avg_fill_price == pytest.approx((10 * 100.0 + 5 * 101.0) / 15)
        # slippage = (100.3333 - 100) / 100 * 10000 = 33.3333 bps
        assert r.slippage_bps == pytest.approx(33.3333, abs=1e-3)
        # adverse cost = 0.3333 price units * 15 filled = 5.0
        assert r.total_cost == pytest.approx(5.0)

    def test_impact_is_half_of_slippage(self, engine, simulator):
        r = simulator.simulate(order_size=15, side=engine.Side.BUY)
        assert r.market_impact_bps == pytest.approx(r.slippage_bps * 0.5)


# ── Sell sweep (bids) — the liquidation path ───────────────────────────────────

class TestSellSweep:
    def test_sell_sweeps_bids_not_asks(self, engine, simulator):
        # If a sell wrongly swept asks, avg_fill would be >= 100.
        # Sweeping bids, it must be <= 99 (the best bid).
        r = simulator.simulate(order_size=15, side=engine.Side.SELL)
        assert r.avg_fill_price <= 99.0

    def test_multi_level_sweep_math(self, engine, simulator):
        # 15 lots: 10 @ 99 + 5 @ 98 → VWAP 98.6667, arrival 99.
        r = simulator.simulate(order_size=15, side=engine.Side.SELL)
        assert r.total_filled == 15
        assert r.avg_fill_price == pytest.approx((10 * 99.0 + 5 * 98.0) / 15)
        # Adverse for a sell = received LESS than arrival → positive slippage.
        # (99 - 98.6667) / 99 * 10000 = 33.67 bps
        assert r.slippage_bps == pytest.approx(33.67, abs=1e-2)
        assert r.total_cost == pytest.approx(5.0)

    def test_slippage_positive_means_adverse_for_both_sides(self, engine, simulator):
        buy = simulator.simulate(order_size=15, side=engine.Side.BUY)
        sell = simulator.simulate(order_size=15, side=engine.Side.SELL)
        assert buy.slippage_bps > 0
        assert sell.slippage_bps > 0


# ── Partial fills ──────────────────────────────────────────────────────────────

class TestPartialFill:
    def test_book_exhaustion_reports_partial_fill(self, engine, simulator):
        # Book holds 25 lots per side — a 100-lot order cannot fully fill.
        r = simulator.simulate(order_size=100, side=engine.Side.SELL)
        assert r.total_filled == 25
        # VWAP of the entire bid side: (10*99 + 10*98 + 5*97) / 25 = 98.2
        assert r.avg_fill_price == pytest.approx(98.2)

    def test_exact_full_fill_boundary(self, engine, simulator):
        r = simulator.simulate(order_size=25, side=engine.Side.BUY)
        assert r.total_filled == 25


# ── Edge cases ─────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_book_returns_zeroed_result(self, engine):
        sim = engine.ExecutionSimulator()
        sim.load_book(asks=[], bids=[])
        r = sim.simulate(order_size=10, side=engine.Side.BUY)
        assert r.total_filled == 0
        assert r.avg_fill_price == 0.0
        assert r.slippage_bps == 0.0

    def test_one_sided_book(self, engine):
        # Asks present, no bids: a sell fills nothing, a buy fills normally.
        sim = engine.ExecutionSimulator()
        sim.load_book(asks=[(100.0, 10)], bids=[])
        assert sim.simulate(order_size=5, side=engine.Side.SELL).total_filled == 0
        assert sim.simulate(order_size=5, side=engine.Side.BUY).total_filled == 5

    def test_unsorted_input_is_sorted_by_load_book(self, engine):
        sim = engine.ExecutionSimulator()
        # Deliberately shuffled input — load_book must sort both sides.
        sim.load_book(
            asks=[(102.0, 5), (100.0, 10), (101.0, 10)],
            bids=[(97.0, 5), (99.0, 10), (98.0, 10)],
        )
        buy = sim.simulate(order_size=10, side=engine.Side.BUY)
        sell = sim.simulate(order_size=10, side=engine.Side.SELL)
        assert buy.avg_fill_price == pytest.approx(100.0)   # best ask first
        assert sell.avg_fill_price == pytest.approx(99.0)   # best bid first

    def test_read_only_sweep_is_repeatable(self, engine, simulator):
        first = simulator.simulate(order_size=15, side=engine.Side.SELL)
        second = simulator.simulate(order_size=15, side=engine.Side.SELL)
        assert first.avg_fill_price == second.avg_fill_price
        assert first.total_filled == second.total_filled


# ── load_book input contract (B6) ──────────────────────────────────────────────

class TestLoadBookContract:
    def test_more_than_max_levels_unsorted_keeps_the_best(self, engine):
        # 12 ask levels supplied worst-first. Truncating before sorting would
        # drop 100.0 and 101.0; the best ask must still be 100.0.
        sim = engine.ExecutionSimulator()
        sim.load_book(asks=[(100.0 + i, 1) for i in range(12)][::-1], bids=[])
        r = sim.simulate(order_size=1, side=engine.Side.BUY)
        assert r.avg_fill_price == pytest.approx(100.0)
        # Only MAX_LEVELS (10) are kept: 100..109 → 10 lots fill, not 12.
        assert sim.simulate(order_size=12, side=engine.Side.BUY).total_filled == 10

    def test_more_than_max_levels_unsorted_bids_keep_the_best(self, engine):
        sim = engine.ExecutionSimulator()
        sim.load_book(asks=[], bids=[(90.0 + i, 1) for i in range(12)])
        assert sim.simulate(order_size=1, side=engine.Side.SELL).avg_fill_price == pytest.approx(101.0)

    @pytest.mark.parametrize("asks,bids", [
        ([(100.0, -5)], []),
        ([(100.0, 0)], []),
        ([], [(99.0, -1)]),
        ([(0.0, 10)], []),
        ([(-1.0, 10)], []),
        ([(float("nan"), 10)], []),
        ([(float("inf"), 10)], []),
        ([(99.0, 10)], [(100.0, 10)]),          # crossed: best bid above best ask
    ])
    def test_invalid_books_raise_value_error(self, engine, asks, bids):
        sim = engine.ExecutionSimulator()
        with pytest.raises(ValueError):
            sim.load_book(asks=asks, bids=bids)

    def test_failed_load_keeps_previous_book(self, engine, simulator):
        with pytest.raises(ValueError):
            simulator.load_book(asks=[(100.0, -1)], bids=[])
        # The hand-verifiable book from the fixture is still in place.
        assert simulator.simulate(order_size=15, side=engine.Side.BUY).total_cost == pytest.approx(5.0)

    def test_locked_book_is_accepted(self, engine):
        sim = engine.ExecutionSimulator()
        sim.load_book(asks=[(100.0, 5)], bids=[(100.0, 5)])
        assert sim.simulate(order_size=5, side=engine.Side.SELL).total_filled == 5


class TestSimulateContract:
    @pytest.mark.parametrize("size", [0, -5])
    def test_non_positive_order_returns_zeroed_result(self, engine, simulator, size):
        r = simulator.simulate(order_size=size, side=engine.Side.BUY)
        assert r.total_filled == 0 and r.avg_fill_price == 0.0

    def test_side_is_required(self, engine, simulator):
        with pytest.raises(TypeError):
            simulator.simulate(order_size=5)

    def test_latency_is_reported_in_nanoseconds(self, engine, simulator):
        r = simulator.simulate(order_size=15, side=engine.Side.BUY)
        assert r.simulation_latency_ns >= 0
        assert not hasattr(r, "simulation_latency_us")

    def test_total_filled_is_64_bit(self, engine):
        sim = engine.ExecutionSimulator()
        sim.load_book(asks=[(100.0, 2_000_000_000), (101.0, 2_000_000_000)], bids=[])
        r = sim.simulate(order_size=2_147_483_647, side=engine.Side.BUY)
        assert r.total_filled == 2_147_483_647

    def test_legacy_flat_rate_model_is_gone(self, engine):
        assert not hasattr(engine.ExecutionSimulator(), "calculate_impact")
