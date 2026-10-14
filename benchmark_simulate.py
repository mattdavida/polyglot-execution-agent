# Latency benchmark for ExecutionSimulator.simulate(), measured FROM PYTHON.
#
# Runs N calls against the ZN LOB and reports the per-call distribution:
# min, p50, p95, p99, max — in microseconds.
#
# WHAT THIS MEASURES: the full Python → pybind11 → C++ → Python round trip,
# including argument conversion, GIL release/re-acquire and result wrapping.
# The C++ sweep itself is a small fraction of it (~70 ns native; it is also
# reported per call in SimulationResult.simulation_latency_ns, printed below).
# Percentiles across 100k iterations, not a single reading.
#
# Run from repo root (venv active):
#   python benchmark_simulate.py
#
# Expected results on a modern desktop (Windows, MSVC -O2):
#   p50  < 1 µs
#   p99  < 5 µs

import sys
import pathlib
import time
import statistics

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from backend.tools.book_loader import load_zn_book
from backend.tools.cpp_bridge import get_simulator

N_WARMUP    = 1_000     # discarded — lets branch predictor and cache warm up
N_ITERS     = 100_000   # measurement iterations
ORDER_SIZE  = 25        # ZN contracts — one slice of the default 200-lot demo trade
                        # (200 in one sweep would exhaust the book: a partial fill)

print("=" * 60)
print(f"  simulate() latency benchmark — {N_ITERS:,} iterations")
print("=" * 60)

# Load book once — this is lru_cached, so the benchmark measures pure C++ time.
print("\nLoading ZN book from tick CSV...")
asks, bids = load_zn_book()   # load_zn_book returns (ask_list, bid_list)
print(f"  Book: {len(bids)} bid levels, {len(asks)} ask levels")

sim = get_simulator()
sim.load_book(asks=asks, bids=bids)

import execution_engine as _ee  # noqa: E402  (module is importable once cpp_bridge has run)
SIDE = _ee.Side.SELL            # the headline scenario is a liquidation

check = sim.simulate(ORDER_SIZE, SIDE)
print(f"  Sanity: SELL {ORDER_SIZE} → filled {check.total_filled} "
      f"({check.total_filled / ORDER_SIZE:.0%}), slippage {check.slippage_bps:.3f} bps")

# Warm-up pass — not measured.
print(f"\nWarm-up: {N_WARMUP:,} iterations (discarded)...")
for _ in range(N_WARMUP):
    sim.simulate(ORDER_SIZE, SIDE)

# Measured pass — use time.perf_counter_ns for nanosecond resolution.
print(f"Measuring: {N_ITERS:,} iterations...")
latencies_us: list[float] = []
native_ns: list[int] = []

for _ in range(N_ITERS):
    t0 = time.perf_counter_ns()
    r = sim.simulate(ORDER_SIZE, SIDE)
    t1 = time.perf_counter_ns()
    latencies_us.append((t1 - t0) / 1_000.0)
    native_ns.append(r.simulation_latency_ns)

latencies_us.sort()

def pct(data: list[float], p: float) -> float:
    idx = int(len(data) * p / 100)
    return data[min(idx, len(data) - 1)]

mean_us  = statistics.mean(latencies_us)
stdev_us = statistics.stdev(latencies_us)
min_us   = latencies_us[0]
p50_us   = pct(latencies_us, 50)
p95_us   = pct(latencies_us, 95)
p99_us   = pct(latencies_us, 99)
max_us   = latencies_us[-1]

print()
print("=" * 60)
print(f"  Per-call from Python — SELL {ORDER_SIZE} contracts, N={N_ITERS:,}")
print("=" * 60)
print(f"  min    {min_us:>8.3f} µs")
print(f"  p50    {p50_us:>8.3f} µs")
print(f"  p95    {p95_us:>8.3f} µs")
print(f"  p99    {p99_us:>8.3f} µs")
print(f"  max    {max_us:>8.3f} µs")
print(f"  mean   {mean_us:>8.3f} µs  (stdev {stdev_us:.3f} µs)")
native_ns.sort()
print(f"  C++ sweep only (steady_clock, includes ~20-40 ns of clock reads):")
print(f"  p50    {pct(native_ns, 50):>8} ns   p99 {pct(native_ns, 99):>6} ns")
print("=" * 60)

# Soft assertions — warn rather than hard-fail (host machine varies).
WARN_P50_US = 5.0
WARN_P99_US = 20.0

if p50_us > WARN_P50_US:
    print(f"\n  [WARN] p50 {p50_us:.3f} µs exceeds {WARN_P50_US} µs — check build flags or GIL hold")
else:
    print(f"\n  [PASS] p50 {p50_us:.3f} µs")

if p99_us > WARN_P99_US:
    print(f"  [WARN] p99 {p99_us:.3f} µs exceeds {WARN_P99_US} µs — likely OS scheduling jitter")
else:
    print(f"  [PASS] p99 {p99_us:.3f} µs")

print()
print("  The per-call figure is dominated by the pybind11 crossing, not the sweep.")
print("  Quote both: the sweep in ns, the per-call cost in µs.")
print()
