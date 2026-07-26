# Execution Agent

> Agentic HITL trading desk: LLM strategy + C++20 LOB simulation + human approval before any order is dispatched.  
> A POC demonstrating the architectural boundary between unstructured LLM reasoning and deterministic high-performance computation.

---

## What It Does

- **Accepts** a natural-language trade request ("Liquidate 50,000 ZN contracts by EOD — factory delay news")
- **Formulates** an execution strategy via LLM (VWAP / TWAP / Sweep / Iceberg, number of slices, shares per slice) — the LLM decides *how*, never *what the numbers are*
- **Computes** exact slippage, market impact, average fill price, and total cost by sweeping a real Level 2 order book in a C++20 engine via pybind11 — microsecond latency, zero allocation on the hot path
- **Pauses** the LangGraph execution and surfaces both the LLM strategy and the C++ metrics to the trader for review (the HITL panel)
- **Resumes** with one of three trader decisions: **Approve** (dispatch the order), **Modify** (override slice parameters and re-run C++ simulation), or **Abort** (cancel cleanly)
- **Persists** every approved trade as a JSON record in `output/` and checkpoints the full graph state to SQLite for auditability

---

## Stack

| Layer | Choice |
|---|---|
| LLM / Orchestration | LangGraph `StateGraph` + Azure OpenAI `gpt-4o` |
| HITL mechanism | `interrupt()` + `Command(resume=...)` + SQLite `MemorySaver` |
| C++ compute core | C++20, pybind11 3.x, pre-allocated LOB, intrusive index list, GIL release |
| Build toolchain | CMake 3.22+ + Ninja + MSVC x64 (Windows) |
| API | FastAPI + Uvicorn |
| Frontend | Next.js 16 + DaisyUI + Tailwind v4 |
| Market data | Bloomberg ZN tick CSV (10-Year Treasury Note Futures, 2016-12-23) |
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

# 4. Start the API (from project root, venv active)
uvicorn backend.main:app --reload --port 3001

# 5. Start the frontend (new terminal)
cd frontend
npm install
npm run dev              # → http://localhost:3000

# 6. Submit a trade in the browser
# Fill in Instrument, Total Shares, Deadline, and optionally a rationale.
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
│   └── 2016_12_23.csv        # Bloomberg ZN tick data (58,632 rows)
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
│   │   └── book_loader.py    # ZN L2 book reconstruction from Bloomberg CSV (lru_cached)
│   └── pipeline/
│       ├── graph.py          # LangGraph StateGraph + SQLite checkpointer + interrupt
│       ├── state.py          # TradeState TypedDict
│       └── nodes/
│           ├── strategy_node.py    # LLM call — decides algorithm and slicing
│           ├── simulation_node.py  # Calls C++ engine — computes slippage
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
└── test_phase*.py            # Per-phase exit criterion tests
```

---

## Pipeline Detail

```
POST /api/trade
  ↓ strategy_node   LLM decides: VWAP / TWAP / Sweep / Iceberg, num_slices, shares_per_slice
  ↓                 structured output via Pydantic — no JSON parsing
  ↓ simulation_node load ZN L2 book from Bloomberg CSV (lru_cached, parsed once)
  ↓                 call C++ ExecutionSimulator.simulate(order_size)
  ↓                 returns: avg_fill_price, slippage_bps, market_impact_bps, total_cost_usd, latency_us
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

The C++ engine (`cpp/src/execution_engine.cpp`) is the architectural centrepiece of the POC. Every design constraint is deliberate — the goal is to demonstrate what a production-grade HFT-style LOB engine looks like at the boundary with a Python orchestration layer.

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

1. Receives `order_size` (number of contracts/shares to fill)
2. Walks the ask side of the pre-loaded LOB via the intrusive index list (`ask_head → next_idx → ... → SENTINEL`)
3. At each level: fills `min(shares_remaining, level.available)` — never modifies the book (read-only sweep)
4. Computes weighted average fill price, slippage in bps vs arrival price, market impact (half-spread model), total cost
5. Records `simulation_latency_us` via `std::chrono::high_resolution_clock`

---

## Market Data

The LOB is populated from a real Bloomberg tick file for **ZN (10-Year Treasury Note Futures, CME Globex)** dated 2016-12-23. The file contains 58,632 rows of `T` (trade), `B` (bid update), and `A` (ask update) ticks.

`backend/tools/book_loader.py` reconstructs the L2 order book by:
1. Reading only ticks within a configurable rolling window (default: 10 minutes ending at 09:30)
2. Applying each B/A row as a level update (last-write-wins, qty=0 cancels the level)
3. Anchoring to the last trade price to eliminate stale levels that accumulate as the market moves
4. Returning the top 10 levels per side, sorted correctly for the C++ engine

The result is a realistic book with irregular depth (e.g. 29 contracts at one level, 5 at the best ask) that produces non-trivial, meaningful slippage numbers — unlike a symmetric dummy book.

---

## API Routes

| Method | Route | Description |
|---|---|---|
| GET | `/api/health` | Backend + graph status |
| POST | `/api/trade` | Submit trade request, returns `thread_id` when graph pauses at HITL |
| GET | `/api/trade/{thread_id}` | Retrieve paused state (strategy + C++ metrics) for HITL panel |
| POST | `/api/resume/{thread_id}` | Resume with `{"action": "approve"\|"revise"\|"abort", "override_params": {...}}` |

---

## Build Status

| Phase | Focus | Status |
|---|---|---|
| 0 | Project scaffold, C++ pybind11 bridge smoke test, CMake + Ninja + MSVC build toolchain | ✅ Complete |
| 1 | `ExecutionSimulator::calculate_impact` — dummy flat-rate, pybind11 binding, Python latency benchmark | ✅ Complete |
| 2 | Full LOB — pre-allocated intrusive list, `load_book`, `simulate`, slippage math, Phase 2 test suite | ✅ Complete |
| 3 | LangGraph pipeline — strategy_node, simulation_node, hitl_node, execution_node, SQLite checkpointer, FastAPI routes | ✅ Complete |
| 4 | Next.js trader dashboard — TradeForm, HitlPanel, StrategyCard, MetricsCard, full state machine | ✅ Complete |
| 4.1 | Real ZN market data — Bloomberg tick CSV, L2 book reconstruction, lru_cached loader | ✅ Complete |
| 4.2 | GIL release on `simulate()` hot path | ✅ Complete |
| 5 | Azure Bicep IaC — OpenAI, Key Vault, App Service, `deploy.ps1` | ✅ Complete |
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

See [DEMO.md](DEMO.md) for a full walkthrough with screenshots.  
See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design rationale, decision log, and phase definitions.
