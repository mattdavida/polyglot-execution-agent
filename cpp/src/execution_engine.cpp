#include "execution_engine.hpp"

#include <algorithm>  // std::sort, std::min
#include <chrono>
#include <span>
#include <string>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>  // required for auto-conversion of std::vector<std::pair<>>

// ═════════════════════════════════════════════════════════════════════════════
// execution_engine.cpp  —  LOB sweep implementation + pybind11 module
// ═════════════════════════════════════════════════════════════════════════════

namespace py    = pybind11;
using namespace exa;
using Clock     = std::chrono::high_resolution_clock;
using Micros    = std::chrono::microseconds;

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
    // ── Reset existing state ──────────────────────────────────────────────────
    book_ = OrderBook{};   // value-initialize: zeroes all fields, num_asks/bids = 0

    // ── Populate ask side ─────────────────────────────────────────────────────
    // Clamp to MAX_LEVELS — silently drop any excess levels.
    book_.num_asks = static_cast<int>(
        std::min(asks.size(), static_cast<std::size_t>(MAX_LEVELS)));

    // Copy into the pre-allocated array (setup time — allocation is fine here).
    for (int i = 0; i < book_.num_asks; ++i) {
        book_.asks[i].price     = asks[i].first;
        book_.asks[i].available = asks[i].second;
        book_.asks[i].next_idx  = SENTINEL;   // will be wired below
    }

    // Sort asks ascending: best ask (lowest price) first.
    // std::sort on a fixed-size slice — fine for setup, never called on hot path.
    std::sort(book_.asks.begin(),
              book_.asks.begin() + book_.num_asks,
              [](const PriceLevel& a, const PriceLevel& b) {
                  return a.price < b.price;
              });

    // Wire the intrusive linked list for the ask side.
    // asks[0].next_idx = 1, asks[1].next_idx = 2, ..., asks[n-1].next_idx = SENTINEL
    for (int i = 0; i < book_.num_asks - 1; ++i) {
        book_.asks[i].next_idx = i + 1;
    }
    // Last node already has next_idx = SENTINEL from the reset above.
    book_.ask_head = (book_.num_asks > 0) ? 0 : SENTINEL;

    // ── Populate bid side ─────────────────────────────────────────────────────
    book_.num_bids = static_cast<int>(
        std::min(bids.size(), static_cast<std::size_t>(MAX_LEVELS)));

    for (int i = 0; i < book_.num_bids; ++i) {
        book_.bids[i].price     = bids[i].first;
        book_.bids[i].available = bids[i].second;
        book_.bids[i].next_idx  = SENTINEL;
    }

    // Sort bids descending: best bid (highest price) first.
    std::sort(book_.bids.begin(),
              book_.bids.begin() + book_.num_bids,
              [](const PriceLevel& a, const PriceLevel& b) {
                  return a.price > b.price;
              });

    for (int i = 0; i < book_.num_bids - 1; ++i) {
        book_.bids[i].next_idx = i + 1;
    }
    book_.bid_head = (book_.num_bids > 0) ? 0 : SENTINEL;
}

// ─────────────────────────────────────────────────────────────────────────────
// ExecutionSimulator::simulate
// ─────────────────────────────────────────────────────────────────────────────
// THE HOT PATH. Every design decision here is intentional:
//
// ALGORITHM — Market Buy Order Sweep
// ────────────────────────────────────
// A market buy order "sweeps" up the ask side of the book, consuming the
// cheapest available liquidity first. For each ask level visited:
//
//   fill_qty = min(shares_remaining, level.available)
//   total_cost += fill_qty * level.price
//   shares_remaining -= fill_qty
//   advance to next level via intrusive next_idx
//
// When shares_remaining == 0, the order is fully filled.
// If the book is exhausted (idx == SENTINEL) before that, we report a
// partial fill — important for large orders vs. thin books.
//
// SLIPPAGE CALCULATION
// ─────────────────────
// arrival_price  = asks[ask_head].price  (the best ask when the order arrives)
// avg_fill_price = total_cost / total_filled (VWAP of all fills)
// slippage_bps   = (avg_fill_price - arrival_price) / arrival_price * 10,000
//
// Slippage is positive when we pay more than the arrival price — i.e., we
// walked up the book and consumed worse levels. For small orders vs. deep
// books, slippage is near zero. For large orders vs. thin books, it's high.
//
// MARKET IMPACT MODEL (simplified for POC)
// ─────────────────────────────────────────
// True market impact models (Almgren-Chriss, Kyle lambda) require real-time
// volume and volatility data. For the POC we use a simple proxy:
//   market_impact_bps = slippage_bps * 0.5
// This assumes roughly half the slippage is permanent price impact and half
// is temporary. Phase 3+ can replace this with a proper model.
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
ExecutionSimulator::simulate(int order_size) const noexcept {
    // Start the clock — we measure only the sweep logic, not pybind11 overhead.
    const auto t0 = Clock::now();

    SimulationResult result;

    // Guard: if the book has no ask levels, return zeroed result immediately.
    if (book_.num_asks == 0 || book_.ask_head == SENTINEL) {
        result.simulation_latency_us =
            std::chrono::duration_cast<Micros>(Clock::now() - t0).count();
        return result;
    }

    // ── Non-owning view over the populated ask levels ─────────────────────────
    // std::span borrows the data from the pre-allocated array — zero cost.
    // `ask_view` is not a copy; it's a (pointer, length) pair pointing into
    // book_.asks. Accessing ask_view[i] is identical to book_.asks[i].
    const std::span<const PriceLevel> ask_view{
        book_.asks.data(),
        static_cast<std::size_t>(book_.num_asks)
    };

    // ── Capture arrival price (best ask at time of order) ─────────────────────
    // This is the price the trader sees before the sweep begins.
    // Slippage measures how much worse the VWAP fill is vs. this reference.
    const double arrival_price = ask_view[static_cast<std::size_t>(book_.ask_head)].price;

    // ── Walk the book — intrusive index traversal ─────────────────────────────
    int    shares_remaining = order_size;
    double total_cost       = 0.0;
    int    total_filled     = 0;

    // idx starts at ask_head (the best ask level).
    // Each iteration: fill what we can at this level, advance to next_idx.
    // Loop terminates when filled (shares_remaining == 0) or book exhausted (SENTINEL).
    //
    // KEY INVARIANT: we never modify book_.asks here — this is a read-only sweep.
    // The "consumption" is purely simulated: we compute fill quantities but do
    // not decrement available shares. This lets simulate() be called multiple
    // times for the same book state (e.g., comparing different order sizes).
    int idx = book_.ask_head;
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
        const PriceLevel& level = ask_view[static_cast<std::size_t>(idx)];

        // Fill as much as available at this level, limited by what remains.
        const int fill = std::min(shares_remaining, level.available);

        total_cost       += static_cast<double>(fill) * level.price;
        total_filled     += fill;
        shares_remaining -= fill;

        // Advance to next price level via the intrusive index.
        // No heap traversal — next_idx is embedded in the PriceLevel struct itself.
        idx = level.next_idx;
    }

    // ── Compute output metrics ────────────────────────────────────────────────
    if (total_filled > 0) {
        result.avg_fill_price  = total_cost / static_cast<double>(total_filled);

        // Slippage: how many basis points above arrival_price did we pay?
        // 1 bps = 0.01% = 1/10,000.  Multiply by 10,000 to get bps.
        result.slippage_bps = (result.avg_fill_price - arrival_price)
                              / arrival_price * 10'000.0;

        // Simplified permanent impact proxy: half the measured slippage.
        result.market_impact_bps = result.slippage_bps * 0.5;

        // Total dollar cost of slippage (excludes the base notional).
        // This is the number a risk manager cares about: "how much did we
        // overpay vs. the price we saw when the order was submitted?"
        result.total_cost_usd = (result.avg_fill_price - arrival_price)
                                * static_cast<double>(total_filled);
    }

    // Stop the clock — capture only the sweep arithmetic, not result packaging.
    result.simulation_latency_us =
        std::chrono::duration_cast<Micros>(Clock::now() - t0).count();

    return result;
}

// ─────────────────────────────────────────────────────────────────────────────
// ExecutionSimulator::calculate_impact  (Phase 1 — retained for compat)
// ─────────────────────────────────────────────────────────────────────────────
// Flat-rate dummy model. Does not use the LOB.
// Phase 3 simulation_node will call simulate() instead; this will be removed.
[[nodiscard]] SimulationResult
ExecutionSimulator::calculate_impact(int shares, double price) const noexcept {
    const auto t0 = Clock::now();

    SimulationResult result;
    result.slippage_bps        = 1.0;
    result.market_impact_bps   = 0.5;
    result.avg_fill_price      = price * (1.0 + result.slippage_bps / 10'000.0);
    result.total_cost_usd      = static_cast<double>(shares) * price
                                 * (result.slippage_bps + result.market_impact_bps) / 10'000.0;

    result.simulation_latency_us =
        std::chrono::duration_cast<Micros>(Clock::now() - t0).count();
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
              "Phase 2: pre-allocated LOB with intrusive index list sweep.";

    // ── SimulationResult ──────────────────────────────────────────────────────
    // Exposed as a Python class with named attributes.
    // def_readwrite makes each field a readable/writable Python attribute.
    // Field names match SlippageMetrics TypedDict in backend/pipeline/state.py.
    py::class_<SimulationResult>(m, "SimulationResult")
        .def(py::init<>())
        .def_readwrite("avg_fill_price",        &SimulationResult::avg_fill_price)
        .def_readwrite("slippage_bps",          &SimulationResult::slippage_bps)
        .def_readwrite("market_impact_bps",     &SimulationResult::market_impact_bps)
        .def_readwrite("total_cost_usd",        &SimulationResult::total_cost_usd)
        .def_readwrite("simulation_latency_us", &SimulationResult::simulation_latency_us)
        .def("__repr__", [](const SimulationResult& r) {
            return "SimulationResult(avg_fill=" + std::to_string(r.avg_fill_price)
                + ", slippage=" + std::to_string(r.slippage_bps) + "bps"
                + ", impact=" + std::to_string(r.market_impact_bps) + "bps"
                + ", cost=$" + std::to_string(r.total_cost_usd)
                + ", latency=" + std::to_string(r.simulation_latency_us) + "us)";
        });

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
             "Populate the order book. asks/bids are lists of (price, shares) tuples.")

        // simulate: the LOB sweep hot path.
        // Python usage:
        //   result = sim.simulate(order_size=10000)
        //
        // py::gil_scoped_release: releases Python's Global Interpreter Lock for
        // the duration of this call. simulate() operates entirely on native C++
        // memory (doubles, ints, array indices) — it never touches a Python
        // object or calls back into the interpreter. Holding the GIL during pure
        // C++ computation is wasteful and prevents concurrent simulations.
        //
        // Practical impact: if FastAPI handles two simultaneous POST /api/trade
        // requests, both simulate() calls can run in parallel native threads
        // while their Python orchestration (LangGraph, LLM) blocks on I/O.
        // Without this, they would serialize at the GIL.
        .def("simulate",
             &ExecutionSimulator::simulate,
             py::arg("order_size"),
             py::call_guard<py::gil_scoped_release>(),
             "Sweep the ask side of the LOB for order_size shares. Returns SimulationResult.")

        // calculate_impact: Phase 1 dummy — retained for test_phase1.py compat.
        .def("calculate_impact",
             &ExecutionSimulator::calculate_impact,
             py::arg("shares"),
             py::arg("price"),
             "[Phase 1 legacy] Flat-rate dummy. Use simulate() for LOB-based results.");
}
