# Polyglot Execution Agent

> Modern AI systems should reason, not calculate. This proof of concept demonstrates a production-inspired **integration pattern**: probabilistic LLM reasoning safely composed with deterministic native computation through hard architectural boundaries — not prompt engineering.
>
> The C++ engine here is a deliberately simplified stand-in for the execution analytics a trading firm already runs in production. The demonstrated skill is not the engine — it is the **boundary**: a LangGraph orchestration layer that constrains the LLM to qualitative decisions, delegates every number to native code, owns the units contract at the seam, and gates every action behind a human checkpoint. Swap the stand-in for the firm's real engine and the architecture is unchanged.

![HITL review panel — strategy and C++ metrics side by side](docs/screenshots/04-hitl-panel.png)

---

## Architecture at a Glance

```
Trader submits trade request    explicit side: buy or sell
         ↓
   LangGraph strategy_node      LLM decides: VWAP / TWAP / Sweep / Iceberg + slice count
         ↓                      ~4–8 seconds. Slice SIZE is computed in Python — never by the LLM
   LangGraph simulation_node    C++ LOB engine sweeps real ZN book
         ↓                      sell sweeps bids, buy sweeps asks — p50 ≈ 0.7 µs
   hitl_node  interrupt()       graph checkpointed to SQLite, API returns
         ↓
   Trader reviews               LLM strategy + C++ metrics (incl. fill ratio) side by side
         ↓
   Approve / Modify / Abort     POST /api/resume/{thread_id}
         ↓
   execution_node               log + write output JSON → END
```

| Step | Component | Latency |
|---|---|---|
| Strategy formulation | Azure OpenAI LLM | ~4–8 s |
| LOB simulation | C++20 engine (pybind11) | p50 ≈ 0.7 µs, p99 ≈ 1 µs |
| Graph checkpoint | SQLite `SqliteSaver` | < 5 ms |
| HITL resume | FastAPI → LangGraph | < 30 ms |

## Key Capabilities

- **Accepts** a trade request with an explicit direction ("Sell 200 ZN by EOD — factory delay news"). Side is a form field, never inferred from free text — a liquidation is a sell and sweeps the bid side.
- **Formulates** an execution strategy via LLM — algorithm selection (VWAP / TWAP / Sweep / Iceberg) and slice count only. The slice *size* is deterministic ceil division in Python, and the slice-count bounds are enforced by Pydantic validation, not the prompt. The LLM reasons about *approach*; it never computes a number.
- **Computes** exact slippage, market impact, average fill price, filled quantity, and adverse cost in a C++20 LOB engine via pybind11 — p50 ≈ 0.7 µs, p99 ≈ 1 µs across 100k iterations, zero allocation on the hot path. Sell orders sweep bids; buy orders sweep asks. All arithmetic lives here, never in the LLM.
- **Owns the units contract at the boundary** — the C++ core is instrument-agnostic and returns costs in the book's raw price units; the Python layer converts to USD using the instrument's tick size and tick value. In production this is exactly where the firm's instrument reference data plugs in.
- **Surfaces partial fills** — if the book exhausts before a slice completes, the fill ratio is reported in the metrics and flagged in the HITL panel. The trader never approves on silently incomplete numbers.
- **Pauses** mid-graph via LangGraph `interrupt()` — the full graph state checkpoints to SQLite and the API returns. The trader sees both the LLM strategy and the C++ metrics side by side with no time pressure.
- **Resumes** with one of three trader decisions: **Approve** (dispatch), **Modify** (override slice parameters and re-run the C++ simulation), or **Abort** (cancel cleanly with no order sent)
- **Persists** every approved trade as a JSON record in `output/` and retains the full graph checkpoint in SQLite for auditability and replay

---

## Stack

| Layer | Choice |
|---|---|
| LLM / Orchestration | LangGraph `StateGraph` + Azure OpenAI `gpt-4o` |
| HITL mechanism | `interrupt()` + `Command(resume=...)` + `SqliteSaver` |
| C++ compute core | C++20, pybind11 3.x, pre-allocated LOB, intrusive index list, GIL release |
| Build toolchain | CMake 3.22+ + Ninja + MSVC x64 (Windows) |
| API | FastAPI + Uvicorn |
| Frontend | Next.js 16 + DaisyUI + Tailwind v4 |
| Market data | Real-world ZN tick dataset (source under verification), L2 book reconstruction |
| Testing | pytest — hand-verified engine math (buy/sell/partial fill), slicing, unit conversion |
| Infrastructure | Azure Bicep (fully repeatable — `.\infra\deploy.ps1`) |

---

## Prerequisites

| Tool | Purpose | Install |
|---|---|---|
| Python 3.11+ | Backend runtime | [python.org](https://www.python.org/downloads/) |
| Node.js 20+ | Frontend runtime | [nodejs.org](https://nodejs.org/) |
| Visual Studio 2022 | C++ compiler (MSVC x64) | [visualstudio.com](https://visualstudio.microsoft.com/) — install "Desktop development with C++" workload |
| CMake 3.22+ | C++ build system | Bundled with Visual Studio, or [cmake.org](https://cmake.org/) |
| Azure CLI | Provision infra via `deploy.ps1` | [aka.ms/installazurecli](https://aka.ms/installazurecli) |

Then authenticate with Azure:

```bash
az login
```

> **Already have a `.env` from a prior deployment?** You can skip `az login` and go straight to Quick Start. The app reads API keys directly from `.env` and does not call Azure CLI at runtime.

---

## Quick Start

```powershell
# 1. Clone
git clone <repo-url>
cd polyglot-execution-agent

# 2. Configure environment
cp .env.example .env
# Fill in all values — the app raises immediately on startup if any are missing

# 3. Build the C++ LOB engine
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
.\cpp\build.ps1          # compiles execution_engine.pyd into cpp/build-ninja/

# 4. Run the test suite (verifies the engine math end to end)
python -m pytest tests/ -v

# 5. Start the API (from project root, venv active)
uvicorn backend.main:app --reload --port 3001

# 6. Start the frontend (new terminal)
cd frontend
npm install
npm run dev              # → http://localhost:3000

# 7. Submit a trade in the browser
# Pick Instrument and Side (buy/sell), set quantity and deadline, optionally add a rationale.
# The HITL review panel appears once the LLM strategy and C++ metrics are ready.
```

> **Azure infra**: provision a dedicated Azure OpenAI resource and Key Vault with `.\infra\deploy.ps1`.  
> All resources are defined in `infra/main.bicep` — no manual portal configuration.

---

## Project Structure

```
polyglot-execution-agent/
├── .env.example              # Required env var names (no values)
├── requirements.txt          # Python dependencies
├── data/
│   └── 2016_12_23.csv        # ZN tick dataset — not committed, see .gitignore
├── output/                   # Approved trade JSON records (gitignored)
│
├── cpp/
│   ├── build.ps1             # One-command C++ build (MSVC + CMake + Ninja)
│   ├── CMakeLists.txt        # pybind11 module definition
│   ├── CMakePresets.json     # Dev preset (Ninja, x64, compile_commands.json)
│   ├── .clangd               # clangd config — strips MSVC flags for IntelliSense
│   ├── include/
│   │   └── execution_engine.hpp   # ExecutionSimulator, OrderBook, PriceLevel, SimulationResult
│   ├── src/
│   │   └── execution_engine.cpp   # LOB sweep impl + pybind11 bindings
│   └── build-ninja/               # CMake output — .pyd lives here (gitignored)
│
├── backend/
│   ├── main.py               # FastAPI entry point + all routes
│   ├── config.py             # Centralized env loading (fail-fast on missing vars)
│   ├── tools/
│   │   ├── llm_client.py     # AzureChatOpenAI factory
│   │   ├── cpp_bridge.py     # sys.path injection + ExecutionSimulator factory
│   │   └── book_loader.py    # ZN L2 book reconstruction from tick CSV (lru_cached)
│   └── pipeline/
│       ├── graph.py          # LangGraph StateGraph + SQLite checkpointer + interrupt
│       ├── state.py          # TradeState TypedDict
│       └── nodes/
│           ├── strategy_node.py    # LLM call — decides algorithm + slice count; slice size computed in code
│           ├── simulation_node.py  # Calls C++ engine (side-aware) — converts cost to USD, flags partial fills
│           ├── hitl_node.py        # interrupt() — pauses graph for trader review
│           └── execution_node.py   # Approved order dispatch (POC: logs + writes JSON)
│
├── frontend/                 # Next.js 16 trader dashboard
│   └── src/app/
│       ├── page.tsx          # Dashboard state machine (idle → submitting → review → done)
│       └── components/
│           ├── TradeForm.tsx       # Trade submission form
│           ├── HitlPanel.tsx       # Review panel with Approve / Modify / Abort
│           ├── StrategyCard.tsx    # LLM output display (algorithm, slices, reasoning)
│           └── MetricsCard.tsx     # C++ output display (fill price, slippage, latency)
│
├── infra/                    # Azure Bicep IaC
│   ├── main.bicep
│   ├── deploy.ps1
│   ├── cleanup.ps1
│   ├── modules/              # openai, keyvault, app-service
│   └── params/               # dev.bicepparam, prod.bicepparam
│
├── tests/                    # pytest suite — engine math (buy/sell/partial), slicing, unit conversion
│   ├── conftest.py           # fixtures: compiled engine + hand-verifiable book
│   ├── test_execution_engine.py
│   └── test_python_boundary.py
│
└── test_book_loader.py       # Smoke test — real ZN CSV → L2 book → C++ sweep
```

---

## Pipeline Detail

```
POST /api/trade               body includes explicit side: "buy" | "sell"
  ↓ strategy_node   LLM decides: VWAP / TWAP / Sweep / Iceberg + num_slices (Pydantic-bounded 1-20)
  ↓                 structured output via Pydantic — no JSON parsing
  ↓                 shares_per_slice = ceil(total / num_slices) — computed in Python, never by the LLM
  ↓ simulation_node load ZN L2 book from tick CSV (lru_cached, parsed once)
  ↓                 call C++ ExecutionSimulator.simulate(order_size, side) — sell sweeps bids, buy sweeps asks
  ↓                 returns: avg_fill_price, slippage_bps, market_impact_bps, total_cost (price units), total_filled, latency_us
  ↓                 Python converts cost to USD via tick metadata; flags partial fills (fill_ratio < 1)
  ↓ hitl_node       interrupt() — graph checkpointed to SQLite, invoke() returns to FastAPI
  ↓
GET /api/trade/{thread_id}    frontend polls → renders strategy + metrics in HITL panel
  ↓
POST /api/resume/{thread_id}  trader acts
  ├── approve  → execution_node → log + write output/trade_{instrument}_{ts}.json → END
  ├── modify   → simulation_node (override params applied) → hitl_node (pause again)
  └── abort    → END
```

---

## The C++ Compute Core

The C++ engine (`cpp/src/execution_engine.cpp`) is a deliberately simplified **stand-in for the execution analytics a firm already runs in production** — most trading shops have a calibrated impact model and LOB tooling in native code, and the realistic engagement is to integrate it, not rewrite it. The engine here exists to make the boundary real: it follows genuine low-latency conventions (pre-allocation, intrusive traversal, GIL release) so the integration pattern is demonstrated against production-shaped constraints, and its math is small enough to hand-verify — which the pytest suite does.

### Design constraints followed

| Constraint | Implementation |
|---|---|
| Zero allocation on hot path | Pre-allocated `std::array<PriceLevel, MAX_LEVELS>` — no `new`, no `malloc` inside `simulate()` |
| Intrusive data structure | `PriceLevel.next_idx` (int index, not pointer) — CPU cache-friendly traversal |
| `noexcept` hot path | `simulate()` is `[[nodiscard]] noexcept` — no exception overhead in the sweep loop |
| GIL release | `py::call_guard<py::gil_scoped_release>()` on `simulate()` — concurrent simulations run in parallel native threads |
| `std::span` for views | Array access via `std::span<const PriceLevel>` — bounds-safe, zero overhead |
| Signed sentinel | `SENTINEL = -1` (int) — the standard HFT intrusive-list idiom; unambiguous in a debugger |

### How `simulate()` works

1. Receives `order_size` and `side` — `Side.SELL` (a liquidation) sweeps the bid side; `Side.BUY` sweeps the asks
2. Walks the chosen side of the pre-loaded LOB via the intrusive index list (`head → next_idx → ... → SENTINEL`)
3. At each level: fills `min(shares_remaining, level.available)` — never modifies the book (read-only sweep)
4. Computes VWAP fill price, adverse slippage in bps vs arrival price (positive = worse for both directions), market impact (half-slippage proxy), and total adverse cost **in the book's price units** — the Python boundary converts to USD via instrument tick metadata
5. Reports `total_filled` so callers can detect partial fills when the book exhausts before the order completes
6. Records `simulation_latency_us` via `std::chrono::high_resolution_clock`

---

## Market Data

The LOB is populated from a real-world ZN (10-Year Treasury Note Futures) tick dataset. The file uses rows of `T` (trade), `B` (bid update), and `A` (ask update) ticks in a standard CME incremental refresh format.

`backend/tools/book_loader.py` reconstructs the L2 order book by:
1. Reading only ticks within a configurable rolling window (default: 10 minutes ending at 09:30)
2. Applying each B/A row as a level update (last-write-wins, qty=0 cancels the level)
3. Anchoring to the last trade price to eliminate stale levels that accumulate as the market moves
4. Returning the top 10 levels per side, sorted correctly for the C++ engine

The result is a realistic book with irregular depth that produces non-trivial, meaningful slippage numbers — unlike a symmetric dummy book. Slippage is non-trivial (1.4–2.0 bps for a 50–200 contract order) because it comes from sweeping through multiple depth levels at different prices. Run `python benchmark_simulate.py` to see the full latency distribution: p50 ≈ 0.7 µs, p99 ≈ 1 µs across 100,000 iterations (host-dependent; figures include ~50–200 ns of Python timer overhead).

**Note on market data:** `data/2016_12_23.csv` is a real-world tick dataset included for demonstration purposes. Its provenance is unverified and it is not committed to the repository — see `.gitignore`. To run with real depth, obtain a compatible ZN L2 tick file and place it at `data/2016_12_23.csv`. The pipeline falls back to a hardcoded dummy book if the file is absent.

---

## API Routes

| Method | Route | Description |
|---|---|---|
| GET | `/api/health` | Backend + graph status |
| POST | `/api/trade` | Submit trade request (`side: "buy"\|"sell"`, default sell), returns `thread_id` when graph pauses at HITL |
| GET | `/api/trade/{thread_id}` | Retrieve trade state — `status` derived from the graph: `awaiting_approval` / `completed` / `aborted` |
| POST | `/api/resume/{thread_id}` | Resume with `{"action": "approve"\|"revise"\|"abort", "override_params": {...}}` |

The graph-invoking routes are deliberately synchronous `def` endpoints — FastAPI runs them in its threadpool, so the event loop stays free to serve polling and health checks while an LLM call (seconds) is in flight. This is what makes the C++ engine's GIL release meaningful under concurrent requests.

---

## Build Status

| Phase | Focus | Status |
|---|---|---|
| 0 | Project scaffold, C++ pybind11 bridge smoke test, CMake + Ninja + MSVC build toolchain | ✅ Complete |
| 1 | `ExecutionSimulator::calculate_impact` — dummy flat-rate, pybind11 binding, Python latency benchmark | ✅ Complete |
| 2 | Full LOB — pre-allocated intrusive list, `load_book`, `simulate`, slippage math, Phase 2 test suite | ✅ Complete |
| 3 | LangGraph pipeline — strategy_node, simulation_node, hitl_node, execution_node, SQLite checkpointer, FastAPI routes | ✅ Complete |
| 4 | Next.js trader dashboard — TradeForm, HitlPanel, StrategyCard, MetricsCard, full state machine | ✅ Complete |
| 4.1 | Real ZN market data — L2 book reconstruction from tick dataset, lru_cached loader | ✅ Complete |
| 4.2 | GIL release on `simulate()` hot path | ✅ Complete |
| 5 | Azure Bicep IaC — OpenAI, Key Vault, App Service, `deploy.ps1` | ✅ Complete |
| 5.1 | Domain-correctness hardening — side-aware sweep (sell sweeps bids), units contract (price units → USD via tick metadata), partial-fill reporting, deterministic slice sizing, non-blocking API handlers, pytest suite | ✅ Complete |
| 6 | Hardening — SSE streaming, Docker + C++ module packaging for Linux, multi-instrument support | 🔲 Pending |

### Roadmap (post-demo)

| Item | Description |
|---|---|
| SSE streaming | Stream LLM reasoning tokens to the HITL panel in real time as strategy_node runs |
| Live market data | Replace CSV snapshot with a Kafka consumer feeding the LOB in real time (3forge-kafka-minimal pattern) |
| Multi-instrument | Instrument registry mapping tickers to correct tick size, contract multiplier, and book snapshot |
| Order book depth display | Show the L2 ladder in the HITL panel so the trader sees what the C++ swept |
| Docker + Linux | Cross-compile the C++ module for Linux App Service — currently Windows MSVC only |
| Concurrent requests | Stress test the GIL release — simulate N simultaneous trades to measure parallel speedup |

---

---

## Engineering Takeaways

- **LLMs are most valuable when constrained.** Probabilistic reasoning is where LLMs excel — algorithm selection, qualitative judgment, natural language context. Deterministic computation belongs in native code. Mixing the two produces systems that are neither correct nor fast.
- **Architectural boundaries are safer than prompt engineering.** The LLM in this system *cannot* compute slippage, slice sizes, or costs — not because it is told not to, but because every number is produced in code: slice sizing by deterministic Python arithmetic with Pydantic-enforced bounds, execution metrics by a C++ engine on a different runtime with no shared state. The constraint is structural.
- **The boundary's units contract is part of the architecture.** The native engine returns costs in the instrument's raw price units; the orchestration layer owns the conversion to currency via tick metadata. Getting the semantics right at the seam — direction, units, partial fills — is the actual integration skill, and it is covered by tests.
- **Human-in-the-loop should be an architectural checkpoint, not a UI afterthought.** LangGraph's `interrupt()` + `SqliteSaver` checkpointer makes the HITL pause a first-class graph primitive — the full execution state serializes to disk and resumes deterministically. No polling loops, no shared mutable state, no race conditions.

---

See [DEMO.md](DEMO.md) for a full walkthrough with screenshots.  
See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design rationale, decision log, and phase definitions.
