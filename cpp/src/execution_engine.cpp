#include "execution_engine.hpp"

#include <algorithm>  // std::sort, std::min
#include <chrono>
#include <cmath>      // std::isfinite
#include <span>
#include <stdexcept>  // std::invalid_argument → Python ValueError via pybind11
#include <string>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>  // required for auto-conversion of std::vector<std::pair<>>

// ═════════════════════════════════════════════════════════════════════════════
// execution_engine.cpp  —  LOB sweep implementation + pybind11 module
// ═════════════════════════════════════════════════════════════════════════════

namespace py    = pybind11;
using namespace exa;
// steady_clock: monotonic, never adjusted. high_resolution_clock may alias
// the wall clock on some standard libraries.
using Clock     = std::chrono::steady_clock;
using Nanos     = std::chrono::nanoseconds;

namespace {

using Levels = std::vector<std::pair<double, int>>;

// Reject anything the sweep cannot price honestly. Runs at setup time only.
void validate_levels(const Levels& levels, const char* side_name) {
    for (const auto& [price, qty] : levels) {
        if (!std::isfinite(price) || price <= 0.0) {
            throw std::invalid_argument(
                std::string(side_name) + ": every price must be finite and > 0");
        }
        if (qty <= 0) {
            throw std::invalid_argument(
                std::string(side_name) + ": every quantity must be > 0");
        }
    }
}

// Copy one sorted side into the pre-allocated array (best MAX_LEVELS only)
// and wire the intrusive list. Returns the number of populated levels.
int fill_side(std::array<PriceLevel, MAX_LEVELS>& dst, const Levels& sorted) {
    const int n = static_cast<int>(
        std::min(sorted.size(), static_cast<std::size_t>(MAX_LEVELS)));
    for (int i = 0; i < n; ++i) {
        const auto idx = static_cast<std::size_t>(i);
        dst[idx].price     = sorted[idx].first;
        dst[idx].available = sorted[idx].second;
        dst[idx].next_idx  = (i + 1 < n) ? i + 1 : SENTINEL;
    }
    return n;
}

} // namespace

// ─────────────────────────────────────────────────────────────────────────────
// ExecutionSimulator::load_book
// ─────────────────────────────────────────────────────────────────────────────
// Populates the pre-allocated OrderBook from Python-provided price/shares pairs.
//
// This function runs once at setup time — it is NOT on the hot path, so we
// allow normal C++ operations here (sort, bounds checks, range-for).
//
// INTRUSIVE LIST CONSTRUCTION
// ───────────────────────────
// After sorting the input, we walk the sorted slice and wire the next_idx
// fields to form the intrusive linked list:
//
//   asks (sorted ascending by price):
//   index:   0         1         2
//   price:   200.01    200.05    200.10
//   next:    1         2         -1 (SENTINEL)
//
//   ask_head = 0  → walk: 0 → 1 → 2 → SENTINEL → done
//
// The first index in sorted order becomes head. Each node points to the next.
// The last node's next_idx = SENTINEL, ending the walk.
void ExecutionSimulator::load_book(
    const std::vector<std::pair<double, int>>& asks,
    const std::vector<std::pair<double, int>>& bids)
{
    // ── Validate first: a throw leaves the existing book untouched ───────────
    validate_levels(asks, "asks");
    validate_levels(bids, "bids");

    // ── Sort the FULL input, then keep the best MAX_LEVELS ───────────────────
    // Sorting before truncating matters: truncating an unsorted list first
    // would silently drop the best prices whenever more than MAX_LEVELS
    // levels arrive. Setup time — copying and sorting here is fine.
    Levels sorted_asks(asks);
    Levels sorted_bids(bids);
    std::sort(sorted_asks.begin(), sorted_asks.end(),
              [](const auto& a, const auto& b) { return a.first < b.first; });   // best ask = lowest
    std::sort(sorted_bids.begin(), sorted_bids.end(),
              [](const auto& a, const auto& b) { return a.first > b.first; });   // best bid = highest

    // ── Crossed book check (locked, bid == ask, is accepted) ─────────────────
    if (!sorted_asks.empty() && !sorted_bids.empty()
        && sorted_bids.front().first > sorted_asks.front().first) {
        throw std::invalid_argument("crossed book: best bid is above best ask");
    }

    // ── Build into a fresh book, then commit (strong exception guarantee) ────
    OrderBook book{};
    book.num_asks = fill_side(book.asks, sorted_asks);
    book.num_bids = fill_side(book.bids, sorted_bids);
    book.ask_head = (book.num_asks > 0) ? 0 : SENTINEL;
    book.bid_head = (book.num_bids > 0) ? 0 : SENTINEL;
    book_ = book;
}

// ─────────────────────────────────────────────────────────────────────────────
// ExecutionSimulator::simulate
// ─────────────────────────────────────────────────────────────────────────────
// THE HOT PATH. Every design decision here is intentional:
//
// ALGORITHM — Market Order Sweep (both directions)
// ─────────────────────────────────────────────────
// A market BUY sweeps up the ask side, consuming the cheapest offered
// liquidity first. A market SELL (liquidation) sweeps down the bid side,
// hitting the highest resting bids first. For each level visited:
//
//   fill_qty = min(shares_remaining, level.available)
//   notional += fill_qty * level.price
//   shares_remaining -= fill_qty
//   advance to next level via intrusive next_idx
//
// When shares_remaining == 0, the order is fully filled.
// If the book is exhausted (idx == SENTINEL) before that, total_filled <
// order_size — a partial fill, surfaced explicitly in the result so callers
// can flag it. Important for large orders vs. thin books.
//
// SLIPPAGE CALCULATION
// ─────────────────────
// arrival_price  = best price on the swept side when the order arrives
//                  (best ask for a buy, best bid for a sell)
// avg_fill_price = notional / total_filled (VWAP of all fills)
// slippage_bps   = adverse move vs arrival, in basis points:
//                  buy:  (avg_fill - arrival) / arrival * 10,000
//                  sell: (arrival - avg_fill) / arrival * 10,000
//
// Slippage is positive when the fill is worse than the arrival price —
// paying up on a buy, or receiving less on a sell. For small orders vs.
// deep books, slippage is near zero. For large orders vs. thin books, high.
//
// MARKET IMPACT MODEL (intentionally simple — POC scope only)
// ─────────────────────────────────────────────────────────────
// Production-grade models (Almgren-Chriss, Kyle lambda) require real-time
// volume and volatility data and are non-trivial to implement correctly.
// For this POC we use a straightforward proxy:
//   market_impact_bps = slippage_bps * 0.5
// This assumes roughly half the slippage is permanent price impact and half
// is temporary — a reasonable first-order approximation for a demo.
//
// In a real deployment this module would be replaced by (or wrap) the firm's
// existing execution analytics library. Most shops already have a calibrated
// impact model in production — the integration point is here, not a rewrite.
//
// WHY noexcept?
// ─────────────
// noexcept tells the compiler no exception can escape this function. This:
//   1. Eliminates stack-unwinding table overhead in the generated code.
//   2. Allows the compiler to make stronger inlining/optimization decisions.
//   3. Documents intent: if anything here would throw, it's a programming error.
//
// WHY std::span?
// ─────────────
// std::span<const PriceLevel> is a non-owning view over the ask array slice
// [book_.asks.data(), num_asks). It carries a pointer + length without copying
// the data. This is C++20's preferred alternative to raw pointer + size pairs.
// We use it to make the "I borrow, I do not own" relationship explicit.
[[nodiscard]] SimulationResult
ExecutionSimulator::simulate(int order_size, Side side) const noexcept {
    // Start the clock — we measure only the sweep logic, not pybind11 overhead.
    const auto t0 = Clock::now();

    SimulationResult result;

    // Select the side of the book to sweep:
    //   Buy  → asks (sorted ascending:  best = cheapest offer first)
    //   Sell → bids (sorted descending: best = highest bid first)
    const bool is_buy    = (side == Side::Buy);
    const int  num_levels = is_buy ? book_.num_asks : book_.num_bids;
    const int  head       = is_buy ? book_.ask_head : book_.bid_head;

    // Guard: nothing to do for a non-positive order or an empty side.
    if (order_size <= 0 || num_levels == 0 || head == SENTINEL) {
        result.simulation_latency_ns =
            std::chrono::duration_cast<Nanos>(Clock::now() - t0).count();
        return result;
    }

    // ── Non-owning view over the populated levels ─────────────────────────────
    // std::span borrows the data from the pre-allocated array — zero cost.
    // It's not a copy; it's a (pointer, length) pair pointing into book_.
    const std::span<const PriceLevel> view{
        is_buy ? book_.asks.data() : book_.bids.data(),
        static_cast<std::size_t>(num_levels)
    };

    // ── Capture arrival price (best price on the swept side) ──────────────────
    // This is the price the trader sees before the sweep begins.
    // Slippage measures how much worse the VWAP fill is vs. this reference.
    const double arrival_price = view[static_cast<std::size_t>(head)].price;

    // ── Walk the book — intrusive index traversal ─────────────────────────────
    int     shares_remaining = order_size;
    double  notional         = 0.0;   // sum of fill_qty * price across levels
    int64_t total_filled     = 0;

    // idx starts at head (the best level on the swept side).
    // Each iteration: fill what we can at this level, advance to next_idx.
    // Loop terminates when filled (shares_remaining == 0) or book exhausted (SENTINEL).
    //
    // KEY INVARIANT: we never modify the book here — this is a read-only sweep.
    // The "consumption" is purely simulated: we compute fill quantities but do
    // not decrement available shares. This lets simulate() be called multiple
    // times for the same book state (e.g., comparing different order sizes).
    int idx = head;
    while (idx != SENTINEL && shares_remaining > 0) {
        // idx is signed int with SENTINEL=-1. The while-condition guarantees
        // idx >= 0 here, so the cast to size_t is always safe.
        //
        // DESIGN NOTE — why int + SENTINEL=-1 rather than size_t + max():
        //   Signed int with a negative sentinel is the standard HFT intrusive-
        //   list idiom (LMAX Disruptor, many FIX ring buffers). It makes the
        //   end-of-list condition unambiguous in a debugger and self-documenting
        //   in code. size_t::max() as sentinel trades one ambiguity for another.
        //   The compiler elides this cast at -O2 anyway.
        const PriceLevel& level = view[static_cast<std::size_t>(idx)];

        // Fill as much as available at this level, limited by what remains.
        const int fill = std::min(shares_remaining, level.available);

        notional         += static_cast<double>(fill) * level.price;
        total_filled     += fill;
        shares_remaining -= fill;

        // Advance to next price level via the intrusive index.
        // No heap traversal — next_idx is embedded in the PriceLevel struct itself.
        idx = level.next_idx;
    }

    // ── Compute output metrics ────────────────────────────────────────────────
    // total_filled is always reported — callers compare it against order_size
    // to detect partial fills (book exhausted before the order completed).
    result.total_filled = total_filled;

    if (total_filled > 0) {
        result.avg_fill_price = notional / static_cast<double>(total_filled);

        // Adverse price move vs arrival, signed so worse-than-arrival is
        // positive for BOTH directions:
        //   buy:  paid above the best ask   → avg_fill - arrival
        //   sell: received below the best bid → arrival - avg_fill
        const double adverse = is_buy
            ? (result.avg_fill_price - arrival_price)
            : (arrival_price - result.avg_fill_price);

        // 1 bps = 0.01% = 1/10,000.  Multiply by 10,000 to get bps.
        result.slippage_bps = adverse / arrival_price * 10'000.0;

        // Simplified permanent impact proxy: half the measured slippage.
        result.market_impact_bps = result.slippage_bps * 0.5;

        // Total adverse cost of slippage IN PRICE UNITS (excludes base notional).
        // The Python boundary converts this to currency using the instrument's
        // tick size / tick value — the C++ core stays instrument-agnostic.
        result.total_cost = adverse * static_cast<double>(total_filled);
    }

    // Stop the clock — capture only the sweep arithmetic, not result packaging.
    result.simulation_latency_ns =
        std::chrono::duration_cast<Nanos>(Clock::now() - t0).count();

    return result;
}

// ─────────────────────────────────────────────────────────────────────────────
// pybind11 module definition
// ─────────────────────────────────────────────────────────────────────────────
// This block is the bridge between C++ types and Python objects.
// It runs once at `import execution_engine` time — no runtime overhead after.
//
// pybind11 STL integration (#include <pybind11/stl.h> above) lets Python pass
// a plain list of tuples [(200.01, 3000), ...] and pybind11 automatically
// converts it to std::vector<std::pair<double, int>> before calling load_book.
// No manual conversion loop needed.
PYBIND11_MODULE(execution_engine, m) {
    m.doc() = "Execution Agent C++20 compute core — pybind11 bridge.\n"
              "Pre-allocated LOB with intrusive index list sweep.";

    // ── SimulationResult ──────────────────────────────────────────────────────
    // Exposed as a Python class with named attributes.
    // def_readwrite makes each field a readable/writable Python attribute.
    // Field names match SlippageMetrics TypedDict in backend/pipeline/state.py.
    py::class_<SimulationResult>(m, "SimulationResult")
        .def(py::init<>())
        .def_readwrite("avg_fill_price",        &SimulationResult::avg_fill_price)
        .def_readwrite("slippage_bps",          &SimulationResult::slippage_bps)
        .def_readwrite("market_impact_bps",     &SimulationResult::market_impact_bps)
        .def_readwrite("total_cost",            &SimulationResult::total_cost)
        .def_readwrite("total_filled",          &SimulationResult::total_filled)
        .def_readwrite("simulation_latency_ns", &SimulationResult::simulation_latency_ns)
        .def("__repr__", [](const SimulationResult& r) {
            return "SimulationResult(avg_fill=" + std::to_string(r.avg_fill_price)
                + ", slippage=" + std::to_string(r.slippage_bps) + "bps"
                + ", impact=" + std::to_string(r.market_impact_bps) + "bps"
                + ", cost=" + std::to_string(r.total_cost) + " (price units)"
                + ", filled=" + std::to_string(r.total_filled)
                + ", latency=" + std::to_string(r.simulation_latency_ns) + "ns)";
        });

    // ── Side ──────────────────────────────────────────────────────────────────
    // Direction of the simulated market order. A liquidation is a SELL —
    // it sweeps the bid side, not the asks.
    py::enum_<Side>(m, "Side")
        .value("BUY",  Side::Buy)
        .value("SELL", Side::Sell);

    // ── ExecutionSimulator ────────────────────────────────────────────────────
    py::class_<ExecutionSimulator>(m, "ExecutionSimulator")
        .def(py::init<>())

        // load_book: populate the pre-allocated LOB from Python data.
        // Python usage:
        //   sim.load_book(
        //       asks=[(200.01, 3000), (200.05, 3000), (200.10, 4000)],
        //       bids=[(199.99, 2000), (199.95, 5000)]
        //   )
        // pybind11/stl.h handles list-of-tuples -> vector<pair<double,int>> conversion.
        .def("load_book",
             &ExecutionSimulator::load_book,
             py::arg("asks"),
             py::arg("bids"),
             "Populate the order book. asks/bids are lists of (price, shares) tuples.\n"
             "Raises ValueError for non-finite/non-positive prices, non-positive\n"
             "quantities or a crossed book; the previous book is kept on error.")

        // simulate: the LOB sweep hot path.
        // Python usage:
        //   result = sim.simulate(order_size=10000, side=execution_engine.Side.SELL)
        //
        // py::gil_scoped_release: releases Python's Global Interpreter Lock for
        // the duration of this call. simulate() operates entirely on native C++
        // memory (doubles, ints, array indices) — it never touches a Python
        // object or calls back into the interpreter. Holding the GIL during pure
        // C++ computation is wasteful and prevents concurrent simulations.
        //
        // Honest cost note: the sweep itself takes ~70 ns, and releasing and
        // re-acquiring the GIL costs about as much. The benefit is that a
        // sweep never blocks other Python threads (API polling, other
        // requests' orchestration) — not that a single call gets faster.
        // Measured per-call cost from Python, including this crossing, is
        // ~0.65 µs (benchmark_simulate.py); the native sweep alone is ~70 ns.
        .def("simulate",
             &ExecutionSimulator::simulate,
             py::arg("order_size"),
             py::arg("side"),   // required: direction is never defaulted
             py::call_guard<py::gil_scoped_release>(),
             "Sweep the LOB for order_size shares (Side.BUY sweeps asks, "
             "Side.SELL sweeps bids). Returns SimulationResult.");
}
