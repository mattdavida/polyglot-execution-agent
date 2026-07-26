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
//      If the caller ignores the result of simulate() or calculate_impact(),
//      the compiler emits a warning. Silently discarding a SimulationResult
//      is almost always a bug.
//
// PHASE ROADMAP
// ─────────────
//   Phase 1: calculate_impact()  — dummy arithmetic, proves the pybind11
//            bridge compiles and measures latency correctly. Still present
//            for backwards compatibility with test_phase1.py.
//
//   Phase 2: simulate()          — real LOB sweep using the pre-allocated
//            OrderBook and intrusive index list. Mathematically verifiable
//            against known book depths.
//
//   Phase 3: simulate() is called by LangGraph's simulation_node. Python
//            passes order_size; C++ returns SimulationResult dict.

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
// simulate() only reads from the ask side for now (market buy order sweep).
// Phase 3+ can add bid-side sweeps for sell orders.
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
// Returned by value — 40 bytes, cheap struct copy.
// Exposed to Python via pybind11 def_readwrite.
//
// Field names intentionally match SlippageMetrics TypedDict in
// backend/pipeline/state.py so the pybind11 bridge can hand the result
// directly to the LangGraph state without name translation.
struct SimulationResult {
    double  avg_fill_price        = 0.0;  // VWAP of fills across all consumed levels
    double  slippage_bps          = 0.0;  // (avg_fill - arrival_price) / arrival_price * 10000
    double  market_impact_bps     = 0.0;  // estimated permanent price impact (simple model)
    double  total_cost_usd        = 0.0;  // dollar cost of slippage: (avg_fill - arrival) * shares
    int64_t simulation_latency_us = 0;    // wall-clock time of the C++ sweep, in microseconds
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
    // asks: list of (price, shares) tuples, any order — sorted ascending inside.
    // bids: list of (price, shares) tuples, any order — sorted descending inside.
    //
    // Precondition: asks.size() <= MAX_LEVELS, bids.size() <= MAX_LEVELS.
    void load_book(const std::vector<std::pair<double, int>>& asks,
                   const std::vector<std::pair<double, int>>& bids);

    // ── simulate ──────────────────────────────────────────────────────────────
    // Phase 2: Walk the ask side of the pre-allocated LOB, consuming liquidity
    // level by level until the order is fully filled (or the book is exhausted).
    // Returns a SimulationResult with VWAP fill price, slippage in bps, and the
    // wall-clock time the sweep took in microseconds.
    //
    // HOT PATH CONSTRAINTS (enforced here, critical for Phase 2+):
    //   - noexcept: no exception overhead, no stack unwinding tables on the path
    //   - no heap allocation: all data is in book_ (pre-allocated member)
    //   - std::span view: borrows the ask array slice without copying
    //   - intrusive index walk: next_idx traversal, no pointer chasing to heap
    [[nodiscard]] SimulationResult simulate(int order_size) const noexcept;

    // ── calculate_impact ──────────────────────────────────────────────────────
    // Phase 1 dummy — retained for backwards compatibility with test_phase1.py.
    // Does not use the LOB; uses a flat-rate slippage model.
    // Will be removed once Phase 3's simulation_node is wired to simulate().
    [[nodiscard]] SimulationResult calculate_impact(int shares, double price) const noexcept;

private:
    OrderBook book_; // Owned by this instance. No shared state, no mutex needed.
};

} // namespace exa
