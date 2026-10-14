#pragma once

#include <array>
#include <cstdint>
#include <span>
#include <utility>   // std::pair
#include <vector>

// ═════════════════════════════════════════════════════════════════════════════
// execution_engine.hpp  —  Execution Agent C++20 compute core
// ═════════════════════════════════════════════════════════════════════════════
//
// ARCHITECTURE OVERVIEW
// ─────────────────────
// This header defines the Limit Order Book (LOB) simulator that sits at the
// heart of the execution agent. It is designed for HFT-style low-latency
// systems engineering practice. The critical design constraints are:
//
//   1. ZERO HEAP ALLOCATION ON THE HOT PATH
//      The simulate() function must never call `new`, `malloc`, or any
//      container that heap-allocates (no std::vector::push_back, no
//      std::shared_ptr). All data lives in pre-allocated stack or class-member
//      storage.  We enforce this by using std::array<PriceLevel, MAX_LEVELS>
//      as the storage for both bid and ask sides of the book.
//
//   2. INTRUSIVE LINKED LIST FOR BOOK TRAVERSAL
//      Rather than a pointer-based linked list (which would scatter nodes
//      across the heap), PriceLevel embeds a next_idx field — an index into
//      the flat std::array.  Walking the book follows these indices: no
//      pointer chasing into arbitrary heap addresses, cache-friendly.
//      Sentinel value -1 signals end-of-list.
//
//   3. NO STRINGS ON THE HOT PATH
//      Instrument identifiers are uint32_t (or enum) at the C++ boundary.
//      The human-readable ticker symbol lives in Python/LLM land only.
//
//   4. std::span FOR NON-OWNING VIEWS
//      The simulate() sweep uses std::span<const PriceLevel> to view the
//      relevant slice of the ask array without copying it. This is C++20's
//      preferred way to express "I borrow this data, I do not own it".
//
//   5. [[nodiscard]] ON ALL COMPUTATION FUNCTIONS
//      If the caller ignores the result of simulate(),
//      the compiler emits a warning. Silently discarding a SimulationResult
//      is almost always a bug.
//
// INPUT CONTRACT
// ──────────────
//   load_book() validates everything the sweep relies on and throws
//   std::invalid_argument (Python: ValueError) otherwise — the book is left
//   unchanged on failure. simulate() can therefore stay noexcept and branch-
//   light: every level it walks has a finite positive price and a positive
//   quantity, both sides are sorted best-first, and the book is not crossed.
//
// THREADING
// ─────────
//   simulate() is const and releases the GIL. load_book() mutates the book
//   and holds the GIL. Never call load_book() on an instance another thread
//   may be sweeping — use one instance per request (the backend does).

namespace exa {

// ─────────────────────────────────────────────────────────────────────────────
// Compile-time constants
// ─────────────────────────────────────────────────────────────────────────────

// Maximum number of price levels per side of the book.
// 10 levels covers typical L2 market depth for equities.
// Making this constexpr means no runtime check and no variable-length array.
constexpr int MAX_LEVELS = 10;

// Sentinel index value signalling "no next node" in the intrusive list.
// -1 is chosen because valid indices are always [0, MAX_LEVELS).
constexpr int SENTINEL = -1;

// ─────────────────────────────────────────────────────────────────────────────
// Side
// ─────────────────────────────────────────────────────────────────────────────
// Direction of the simulated market order:
//   Buy  → sweeps the ask side (paying up through offered liquidity)
//   Sell → sweeps the bid side (selling down through resting bids)
// A liquidation is a Sell — it consumes bids, not asks.
enum class Side : int {
    Buy  = 0,
    Sell = 1,
};

// ─────────────────────────────────────────────────────────────────────────────
// PriceLevel
// ─────────────────────────────────────────────────────────────────────────────
// One node in the order book — represents all resting limit orders at a single
// price point, aggregated into a single available share count.
//
// next_idx is the "pointer" in the intrusive linked list: instead of storing
// an actual memory address (which would live on the heap), we store an index
// into the flat std::array<PriceLevel, MAX_LEVELS> that owns all nodes.
//
// Example: if asks = [{200.01, 3000, 1}, {200.05, 2000, 2}, {200.10, 5000, -1}]
//   ask_head = 0
//   Walk: asks[0] -> asks[asks[0].next_idx=1] -> asks[1].next_idx=2 -> asks[2].next_idx=-1 -> done
//
// sizeof(PriceLevel) = 8 (double) + 4 (int) + 4 (int) = 16 bytes.
// 10 levels per side = 160 bytes per side, 320 bytes total for the full book.
// Fits easily in L1 cache (typically 32–64 KB).
struct PriceLevel {
    double price     = 0.0;      // absolute price, e.g. 200.05
    int    available = 0;        // shares available at this price level
    int    next_idx  = SENTINEL; // index of next level in the flat array; SENTINEL = end
};

// ─────────────────────────────────────────────────────────────────────────────
// OrderBook
// ─────────────────────────────────────────────────────────────────────────────
// The full two-sided limit order book, stored entirely in pre-allocated arrays.
// No heap allocation — the arrays are value members of this struct.
//
// Invariants maintained by load_book():
//   asks: sorted ascending  by price (best ask = lowest  price = asks[ask_head])
//   bids: sorted descending by price (best bid = highest price = bids[bid_head])
//
// simulate() sweeps the ask side for Side::Buy and the bid side for Side::Sell.
struct OrderBook {
    std::array<PriceLevel, MAX_LEVELS> asks = {};  // ask side: ascending price
    std::array<PriceLevel, MAX_LEVELS> bids = {};  // bid side: descending price
    int ask_head = 0;   // index of the best (lowest) ask level
    int bid_head = 0;   // index of the best (highest) bid level
    int num_asks = 0;   // number of populated ask levels (0..MAX_LEVELS)
    int num_bids = 0;   // number of populated bid levels (0..MAX_LEVELS)
};

// ─────────────────────────────────────────────────────────────────────────────
// SimulationResult
// ─────────────────────────────────────────────────────────────────────────────
// Plain data struct returned by the C++ compute core.
// Returned by value — 48 bytes, cheap struct copy.
// Exposed to Python via pybind11 def_readwrite.
//
// Field names intentionally match SlippageMetrics TypedDict in
// backend/pipeline/state.py so the pybind11 bridge can hand the result
// directly to the LangGraph state without name translation.
// UNITS: prices and total_cost are in the same *price units* as the book that
// was loaded (e.g. raw CME ticks for the ZN dataset). The C++ core is
// instrument-agnostic — converting price units to real currency (via tick size
// and tick value) is the Python boundary's job, where instrument metadata lives.
struct SimulationResult {
    double  avg_fill_price        = 0.0;  // VWAP of fills across all consumed levels (price units)
    double  slippage_bps          = 0.0;  // adverse move vs arrival price, in basis points (positive = worse)
    double  market_impact_bps     = 0.0;  // estimated permanent price impact (simple model)
    double  total_cost            = 0.0;  // adverse cost of slippage in price units: |avg_fill - arrival| * filled
    int64_t total_filled          = 0;    // contracts/shares actually filled (< order_size = partial fill)
    int64_t simulation_latency_ns = 0;    // wall-clock time of the C++ sweep, in nanoseconds (steady_clock)
};

// ─────────────────────────────────────────────────────────────────────────────
// ExecutionSimulator
// ─────────────────────────────────────────────────────────────────────────────
// The primary class exposed to Python.  Owns one OrderBook instance (stack /
// class-member storage, no heap).  Python calls load_book() once to populate
// the depth, then calls simulate() for each trade scenario.
//
// Thread safety: ExecutionSimulator owns its OrderBook exclusively.
// The recommended usage pattern is one instance per thread (or per request).
// No mutex is needed because there is no shared mutable state between threads
// when each thread owns its own simulator instance.
class ExecutionSimulator {
public:
    ExecutionSimulator() = default;

    // ── load_book ─────────────────────────────────────────────────────────────
    // Populate the order book from Python-provided price/shares pairs.
    // This function IS allowed to do normal work (sort, copy) — it runs once at
    // setup time, not on the hot path.
    //
    // asks: list of (price, shares) tuples, any order, any length — sorted
    //       ascending inside, then the best MAX_LEVELS are kept.
    // bids: list of (price, shares) tuples, any order, any length — sorted
    //       descending inside, then the best MAX_LEVELS are kept.
    //
    // Throws std::invalid_argument (book unchanged) if any price is not finite
    // and > 0, any quantity is <= 0, or the best bid is above the best ask
    // (crossed). A locked book (best bid == best ask) is accepted.
    void load_book(const std::vector<std::pair<double, int>>& asks,
                   const std::vector<std::pair<double, int>>& bids);

    // ── simulate ──────────────────────────────────────────────────────────────
    // Walk one side of the pre-allocated LOB, consuming liquidity level by
    // level until the order is fully filled (or the book is exhausted).
    //   Side::Buy  → sweeps asks (paying up)
    //   Side::Sell → sweeps bids (selling down) — the liquidation path
    // Returns a SimulationResult with VWAP fill price, adverse slippage in bps,
    // filled quantity (for partial-fill detection), and the wall-clock time the
    // sweep took in microseconds.
    //
    // order_size <= 0 returns a zeroed result.
    //
    // HOT PATH CONSTRAINTS:
    //   - noexcept: no exception overhead, no stack unwinding tables on the path
    //   - no heap allocation: all data is in book_ (pre-allocated member)
    //   - std::span view: borrows the array slice without copying
    //   - intrusive index walk: next_idx traversal, no pointer chasing to heap
    [[nodiscard]] SimulationResult simulate(int order_size, Side side) const noexcept;

private:
    OrderBook book_; // Owned by this instance. No shared state, no mutex needed.
};

} // namespace exa
