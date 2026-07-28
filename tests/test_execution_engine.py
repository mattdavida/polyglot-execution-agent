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
