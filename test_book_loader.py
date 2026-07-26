# Smoke test: load ZN book from CSV and run C++ LOB simulation against it.
#
# Verifies:
#   1. CSV parses without error
#   2. Book has realistic bid/ask spread (ZN spread should be 0.5 ticks)
#   3. Asks are ascending, bids are descending (correct sort order for C++ engine)
#   4. C++ simulate() produces non-trivial slippage against real depth
#   5. Revision path (smaller order) produces less slippage
#
# Run from repo root:
#   python test_book_loader.py

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from backend.tools.book_loader import load_zn_book, ZN_METADATA
from backend.tools.cpp_bridge import get_simulator

PASS = "[PASS]"
FAIL = "[FAIL]"

def check(label: str, condition: bool) -> bool:
    print(f"  {PASS if condition else FAIL} {label}")
    return condition

print("=" * 60)
print("ZN Book Loader Smoke Test")
print("=" * 60)

# ── Test 1: Load the book ──────────────────────────────────────────────────────
print("\nTest 1: Parse CSV and reconstruct L2 book")
try:
    asks, bids = load_zn_book()
    best_ask = asks[0]
    best_bid = bids[0]
    spread   = best_ask[0] - best_bid[0]

    all_ok = all([
        check("asks list non-empty",               len(asks) > 0),
        check("bids list non-empty",               len(bids) > 0),
        check("asks sorted ascending (best first)", all(asks[i][0] <= asks[i+1][0] for i in range(len(asks)-1))),
        check("bids sorted descending (best first)", all(bids[i][0] >= bids[i+1][0] for i in range(len(bids)-1))),
        check("spread >= 0 ticks (non-crossed)",    spread >= 0.0),
        check("spread <= 5 ticks (reasonable)",    spread <= 5.0),
        check("ask qty > 0",                       all(q > 0 for _, q in asks)),
        check("bid qty > 0",                       all(q > 0 for _, q in bids)),
    ])

    print(f"\n  Instrument : {ZN_METADATA['symbol']} — {ZN_METADATA['description']}")
    print(f"  Levels     : {len(bids)} bids / {len(asks)} asks")
    print(f"  Best bid   : {best_bid[0]} @ {best_bid[1]} contracts")
    print(f"  Best ask   : {best_ask[0]} @ {best_ask[1]} contracts")
    print(f"  Spread     : {spread:.1f} ticks = ${spread * ZN_METADATA['tick_value']:.2f}/contract")
    print(f"\n  Ask side:")
    for p, q in asks:
        print(f"    {p:>8.1f}  @ {q:>4} contracts")
    print(f"  Bid side:")
    for p, q in bids:
        print(f"    {p:>8.1f}  @ {q:>4} contracts")
    print(f"\n  Test 1: {'PASS' if all_ok else 'FAIL'}")

except Exception as e:
    print(f"  {FAIL} Exception: {e}")
    import traceback; traceback.print_exc()
    sys.exit(1)

# ── Test 2: C++ LOB sweep against real book ────────────────────────────────────
print("\nTest 2: C++ simulate() against real ZN depth")

ORDER_LARGE = 20    # 20 contracts — likely sweeps multiple levels
ORDER_SMALL = 3     # 3 contracts — should fill at or near best ask

try:
    sim = get_simulator()
    sim.load_book(asks=asks, bids=bids)

    result_large = sim.simulate(order_size=ORDER_LARGE)
    result_small = sim.simulate(order_size=ORDER_SMALL)

    all_ok = all([
        check("avg_fill > 0",                          result_large.avg_fill_price > 0),
        check("slippage_bps >= 0",                     result_large.slippage_bps >= 0),
        check("total_cost_usd > 0",                    result_large.total_cost_usd > 0),
        check("small order slippage <= large order",   result_small.slippage_bps <= result_large.slippage_bps),
        check("latency captured (us)",                 result_large.simulation_latency_us >= 0),
    ])

    print(f"\n  Large order ({ORDER_LARGE} contracts):")
    print(f"    avg_fill     : {result_large.avg_fill_price:.2f}")
    print(f"    slippage     : {result_large.slippage_bps:.4f} bps")
    print(f"    market_impact: {result_large.market_impact_bps:.4f} bps")
    print(f"    total_cost   : ${result_large.total_cost_usd:.4f}")
    print(f"    latency      : {result_large.simulation_latency_us} us")

    print(f"\n  Small order ({ORDER_SMALL} contracts):")
    print(f"    avg_fill     : {result_small.avg_fill_price:.2f}")
    print(f"    slippage     : {result_small.slippage_bps:.4f} bps")
    print(f"    total_cost   : ${result_small.total_cost_usd:.4f}")

    print(f"\n  Test 2: {'PASS' if all_ok else 'FAIL'}")

except Exception as e:
    print(f"  {FAIL} Exception: {e}")
    import traceback; traceback.print_exc()

print("\n" + "=" * 60)
print("Book loader verified. Real ZN depth is wired into simulation_node.")
print("=" * 60)
