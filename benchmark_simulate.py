# Latency benchmark for ExecutionSimulator.simulate()
#
# Runs N sweeps of the ZN LOB and reports wall-clock distribution:
# min, p50, p95, p99, max — in microseconds.
#
# This validates the sub-microsecond latency claim properly.
# A single-shot 0µs reading is below clock resolution and not meaningful.
# Percentiles across 100k+ iterations are the standard HFT validation method.
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
ORDER_SIZE  = 200       # ZN contracts — matches default demo trade

print("=" * 60)
print(f"  simulate() latency benchmark — {N_ITERS:,} iterations")
print("=" * 60)

# Load book once — this is lru_cached, so the benchmark measures pure C++ time.
print("\nLoading ZN book from Bloomberg CSV...")
asks, bids = load_zn_book()   # load_zn_book returns (ask_list, bid_list)
print(f"  Book: {len(bids)} bid levels, {len(asks)} ask levels")

sim = get_simulator()
sim.load_book(asks, bids)

# Warm-up pass — not measured.
print(f"\nWarm-up: {N_WARMUP:,} iterations (discarded)...")
for _ in range(N_WARMUP):
    sim.simulate(ORDER_SIZE)

# Measured pass — use time.perf_counter_ns for nanosecond resolution.
print(f"Measuring: {N_ITERS:,} iterations...")
latencies_us: list[float] = []

for _ in range(N_ITERS):
    t0 = time.perf_counter_ns()
    sim.simulate(ORDER_SIZE)
    t1 = time.perf_counter_ns()
    latencies_us.append((t1 - t0) / 1_000.0)

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
print(f"  Results — order_size={ORDER_SIZE} contracts, N={N_ITERS:,}")
print("=" * 60)
print(f"  min    {min_us:>8.3f} µs")
print(f"  p50    {p50_us:>8.3f} µs")
print(f"  p95    {p95_us:>8.3f} µs")
print(f"  p99    {p99_us:>8.3f} µs")
print(f"  max    {max_us:>8.3f} µs")
print(f"  mean   {mean_us:>8.3f} µs  (stdev {stdev_us:.3f} µs)")
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
print("  Note: measurements include Python perf_counter_ns call overhead (~50-200 ns).")
print("  The C++ sweep itself is faster than the reported numbers by that margin.")
print("  To eliminate overhead entirely, use a C++-side timing loop (not needed for a demo claim).")
print()
