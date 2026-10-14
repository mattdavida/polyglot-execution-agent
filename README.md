# Polyglot Execution Agent

> Modern AI systems should reason, not calculate. This proof of concept demonstrates a production-inspired **integration pattern**: probabilistic LLM reasoning safely composed with deterministic native computation through hard architectural boundaries — not prompt engineering.
>
> The C++ engine here is a deliberately simplified stand-in for the execution analytics a trading firm already runs in production. The demonstrated skill is not the engine — it is the **boundary**: a LangGraph orchestration layer that constrains the LLM to qualitative decisions, delegates every number to native code, owns the units contract at the seam, and gates every action behind a human checkpoint. Swap the stand-in for the firm's real engine and the architecture is unchanged.

![HITL review panel — strategy and C++ metrics side by side](docs/screenshots/04-hitl-panel.png)

> **Scope.** This is a reference architecture / proof of concept — not a trading system. It has one instrument, a static historical book snapshot, no OMS connection, and no user identity. [docs/known-gaps.md](docs/known-gaps.md) lists every simplification and assumption, alongside the contracts that *are* enforced and the tests that prove them.

---

## Architecture at a Glance

```
Trader submits trade request    explicit side: buy or sell; validated (strict ints, ZN only)
         ↓
   LangGraph strategy_node      LLM decides: VWAP / TWAP / Sweep / Iceberg + slice count
         ↓                      seconds. Slice SIZE is computed in Python — never by the LLM
   LangGraph simulation_node    C++ LOB engine sweeps one slice against the ZN book
         ↓                      sell sweeps bids, buy sweeps asks — tens of nanoseconds
   hitl_node  interrupt()       graph checkpointed to SQLite, API returns
         ↓
   Trader reviews               strategy + C++ metrics (per slice, incl. fill ratio) side by side
         ↓
   Approve / Modify / Abort     POST /api/resume/{thread_id}
         ↓                      approval gate: current metrics, no sim error, no unconfirmed fallback
   execution_node               log + write output JSON (with decision log) → END
```

| Step | Component | Latency |
|---|---|---|
| Strategy formulation | Azure OpenAI LLM | seconds |
| LOB sweep (native) | C++20 engine | tens of ns (≈ 35–75 ns on the author's host) |
| LOB sweep, one call from Python | pybind11 crossing + sweep | ≈ 0.4–0.7 µs p50 |
| Graph checkpoint | SQLite `SqliteSaver` | < 5 ms |
| HITL resume | FastAPI → LangGraph | < 30 ms |

## Key Capabilities

- **Accepts** a trade request with an explicit direction ("Sell 200 ZN by EOD — factory delay news"). Side is a form field, never inferred from free text — a liquidation is a sell and sweeps the bid side.
- **Formulates** an execution strategy via LLM — algorithm selection (VWAP / TWAP / Sweep / Iceberg) and slice count only. The slice *size* is deterministic ceil division in Python ([`backend/pipeline/plan.py`](backend/pipeline/plan.py)), and the slice-count bounds are enforced by Pydantic validation, not the prompt. The LLM reasons about *approach*; it never sizes, prices or costs anything.
- **Bounds the human too.** A trader's Modify goes through the same plan rules as the LLM's output: strict integers, 1–20 slices, no other keys, and the plan must cover the order. The algorithm label and the LLM's rationale cannot be edited through the override.
- **Computes** slippage, market impact, average fill price, filled quantity, and adverse cost for one slice in a C++20 LOB engine via pybind11. The sweep takes tens of nanoseconds with zero heap allocation; a call from Python costs ≈ 0.4–0.7 µs, almost all of it the language crossing. Sell orders sweep bids; buy orders sweep asks.
- **Owns the units contract at the boundary** — the C++ core is instrument-agnostic and returns costs in the book's raw price units; the Python layer converts to USD using the instrument's tick size and tick value. In production this is exactly where the firm's instrument reference data plugs in.
- **Surfaces partial fills** — if the book exhausts before a slice completes, the fill ratio is reported in the metrics and flagged next to the Approve button.
- **Gates approval** — Approve is accepted only when C++ metrics exist for the *current* revision of the plan, the last simulation did not fail, and the strategy is not an unconfirmed LLM-outage fallback. The API (409), the graph router and `execution_node` each check this independently.
- **Pauses** mid-graph via LangGraph `interrupt()` — the full graph state checkpoints to SQLite and the API returns. The trader sees both the LLM strategy and the C++ metrics side by side with no time pressure.
- **Resumes** with one of three trader decisions: **Approve** (dispatch), **Modify** (change the slice count and re-run the C++ simulation), or **Abort** (cancel cleanly with no order sent). Resuming an unknown trade returns 404; resuming a finished or aborted one returns 409.
- **Records** every approved trade as `output/trade_<thread_id>.json`, including an append-only decision log of every revise and approval. This is a local convenience record, not a compliance-grade audit log (no user identity) — see the known gaps.

---

## Stack

| Layer | Choice |
|---|---|
| LLM / Orchestration | LangGraph `StateGraph` + Azure OpenAI `gpt-4o` |
| HITL mechanism | `interrupt()` + `Command(resume=...)` + `SqliteSaver` |
| C++ compute core | C++20, pybind11 3.x, pre-allocated LOB, intrusive index list, GIL release |
| Build toolchain | CMake 3.22+ + Ninja; MSVC x64 via `build.ps1` on Windows, g++/clang on Linux (`-Werror`) |
| API | FastAPI + Uvicorn |
| Frontend | Next.js 16 + DaisyUI + Tailwind v4 |
| Market data | Real-world ZN tick dataset (source under verification), L2 book reconstruction |
| Testing | pytest, offline (fake LLM) — hand-verified engine math, `load_book` input contract, plan rules, and API/graph boundary probes |
| Infrastructure | Azure Bicep for the OpenAI + Key Vault resources (`.\infra\deploy.ps1`); the App Service module is a placeholder — see known gaps |

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

# 4. Run the test suite — offline, no Azure calls (the LLM is faked)
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

> **Linux / macOS build** (the CMake project is portable; only `build.ps1` is Windows-specific):
> ```bash
> pip install -r requirements.txt cmake ninja
> cmake -S cpp -B cpp/build -G Ninja -DCMAKE_BUILD_TYPE=Release -Dpybind11_DIR="$(python -m pybind11 --cmakedir)"
> cmake --build cpp/build
> python -m pytest tests/ -v
> ```

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
│       ├── graph.py          # LangGraph StateGraph + SQLite checkpointer + interrupt + approval routing
│       ├── plan.py           # Slice arithmetic, override rules, approval gate (shared by API + graph)
│       ├── state.py          # TradeState TypedDict
│       └── nodes/
│           ├── strategy_node.py    # LLM call — algorithm + slice count; size computed in code; fallback marked
│           ├── simulation_node.py  # Calls C++ engine (side-aware) — USD conversion, partial-fill warnings
│           ├── hitl_node.py        # interrupt() — pauses for trader review, appends to the decision log
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
├── docs/
│   ├── known-gaps.md         # POC scope, assumptions, and what is enforced + tested
│   └── screenshots/
│
├── tests/                    # pytest suite — runs offline (fake LLM, temp checkpoint DB)
│   ├── conftest.py           # fixtures: compiled engine, hand-verifiable book, fake LLM, API client
│   ├── test_execution_engine.py   # engine math + load_book/simulate input contract
│   ├── test_python_boundary.py    # plan rules, approval gate, units, side mapping
│   └── test_graph_boundary.py     # API + graph probes: overrides, gate, 404/409, fallback, stale metrics
│
└── test_book_loader.py       # Smoke test — real ZN CSV → L2 book → C++ sweep
```

---

## Pipeline Detail

```
POST /api/trade               side required ("buy" | "sell"); instrument must be registered (ZN); total_shares strict int > 0
  ↓ strategy_node   LLM decides: VWAP / TWAP / Sweep / Iceberg + num_slices (Pydantic-bounded 1-20)
  ↓                 structured output via Pydantic — no JSON parsing
  ↓                 shares_per_slice = ceil(total / num_slices); count normalised so no slice is empty
  ↓                 LLM failure → deterministic fallback, marked origin="fallback" (approval blocked until Modify)
  ↓ simulation_node load ZN L2 book from tick CSV (lru_cached, parsed once)
  ↓                 call C++ ExecutionSimulator.simulate(order_size, side) — sell sweeps bids, buy sweeps asks
  ↓                 returns: avg_fill_price, slippage_bps, market_impact_bps, total_cost (price units), total_filled, latency_ns
  ↓                 Python converts cost to USD via tick metadata; flags partial fills (fill_ratio < 1)
  ↓                 on failure: metrics cleared + simulation_error set (stale numbers never survive)
  ↓ hitl_node       interrupt() — graph checkpointed to SQLite, invoke() returns to FastAPI
  ↓
GET /api/trade/{thread_id}    strategy + metrics + warnings + can_approve / approval_blocked_reason
  ↓
POST /api/resume/{thread_id}  trader acts (404 unknown thread, 409 not awaiting / in flight / gate refused, 422 bad override)
  ├── approve  → [gate] → execution_node → write output/trade_{thread_id}.json → END
  ├── revise   → simulation_node (validated override applied) → hitl_node (pause again)
  └── abort    → END
```

---

## The C++ Compute Core

The C++ engine (`cpp/src/execution_engine.cpp`) is a deliberately simplified **stand-in for the execution analytics a firm already runs in production** — most trading shops have a calibrated impact model and LOB tooling in native code, and the realistic engagement is to integrate it, not rewrite it. The engine here exists to make the boundary real: it follows genuine low-latency conventions (pre-allocation, intrusive traversal, GIL release) so the integration pattern is demonstrated against production-shaped constraints, and its math is small enough to hand-verify — which the pytest suite does.

### Design constraints followed

| Constraint | Implementation |
|---|---|
| Zero allocation in the sweep | Pre-allocated `std::array<PriceLevel, MAX_LEVELS>` — no `new`, no `malloc` inside `simulate()` (verified over 1,000,000 calls with an allocation-counting harness). The Python wrapper object for each result is still allocated by pybind11 |
| Validated input | `load_book()` sorts all levels before keeping the best 10, and rejects non-finite / non-positive prices, non-positive quantities and crossed books (`ValueError`, previous book kept) |
| Intrusive data structure | `PriceLevel.next_idx` (int index, not pointer) — CPU cache-friendly traversal |
| `noexcept` hot path | `simulate()` is `[[nodiscard]] noexcept` — no exception overhead in the sweep loop |
| GIL release | `py::call_guard<py::gil_scoped_release>()` on `simulate()` — a sweep never blocks other Python threads. At tens of ns per sweep, this is about not blocking, not about speed |
| `std::span` for views | Array access via `std::span<const PriceLevel>` — bounds-safe, zero overhead |
| Signed sentinel | `SENTINEL = -1` (int) — the standard HFT intrusive-list idiom; unambiguous in a debugger |

### How `simulate()` works

1. Receives `order_size` and `side` — `Side.SELL` (a liquidation) sweeps the bid side; `Side.BUY` sweeps the asks
2. Walks the chosen side of the pre-loaded LOB via the intrusive index list (`head → next_idx → ... → SENTINEL`)
3. At each level: fills `min(shares_remaining, level.available)` — never modifies the book (read-only sweep)
4. Computes VWAP fill price, adverse slippage in bps vs arrival price (positive = worse for both directions), market impact (half-slippage proxy), and total adverse cost **in the book's price units** — the Python boundary converts to USD via instrument tick metadata
5. Reports `total_filled` so callers can detect partial fills when the book exhausts before the order completes
6. Records `simulation_latency_ns` via `std::chrono::steady_clock`

---

## Market Data

The LOB is populated from a real-world ZN (10-Year Treasury Note Futures) tick dataset. The file uses rows of `T` (trade), `B` (bid update), and `A` (ask update) ticks in a standard CME incremental refresh format.

`backend/tools/book_loader.py` reconstructs the L2 order book by:
1. Reading only ticks within a configurable rolling window (default: 10 minutes ending at 09:30)
2. Applying each B/A row as a level update (last-write-wins, qty=0 cancels the level)
3. Anchoring to the last trade price to eliminate stale levels that accumulate as the market moves
4. Returning the top 10 levels per side, sorted correctly for the C++ engine

The result is a book with irregular depth (5 ask levels / 83 contracts, 10 bid levels / 141 contracts at the default snapshot). A 25-contract sell slice fills fully at ≈ 0.6 bps; a 50-contract slice at ≈ 1.4–1.6 bps. Larger single sweeps exhaust the book — a 200-contract buy fills only 83 (41.5%), which the engine reports as a partial fill rather than hiding. The default snapshot is **locked** (best bid == best ask), an artefact of the reconstruction — see the known gaps.

Run `python benchmark_simulate.py` for the latency distribution. It reports two numbers, and both are host-dependent: the per-call cost from Python (≈ 0.4–0.7 µs p50, dominated by the pybind11 crossing) and the native sweep time (tens of nanoseconds).

**Units.** Book prices are raw integer price units with 1 tick = 0.5 units; USD cost = (price units ÷ 0.5) × $15.625 per contract. This mapping is an assumption about a dataset of unverified provenance — stated in [`book_loader.py`](backend/tools/book_loader.py) and the known gaps.

**Note on market data:** `data/2016_12_23.csv` is a real-world tick dataset included for demonstration purposes. Its provenance is unverified and it is not committed to the repository — see `.gitignore`. To run with real depth, obtain a compatible ZN L2 tick file and place it at `data/2016_12_23.csv`. The pipeline falls back to a hardcoded dummy book if the file is absent.

---

## API Routes

| Method | Route | Description |
|---|---|---|
| GET | `/api/health` | Backend + graph status |
| POST | `/api/trade` | Submit trade request (`side` required, `instrument` must be registered, `total_shares` strict int 1–100,000); returns `thread_id` and graph-derived `status` |
| GET | `/api/trade/{thread_id}` | Trade state — `status` derived from the graph (`awaiting_approval` / `completed` / `aborted` / `failed`), plus `can_approve`, `approval_blocked_reason`, warnings and the decision log. 404 if unknown |
| POST | `/api/resume/{thread_id}` | `{"action": "approve" \| "abort"}` or `{"action": "revise", "override_params": {"num_slices": n}}` (or `shares_per_slice`). 404 unknown, 409 not awaiting / in flight / gate refused, 422 invalid |

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
| 5.2 | Boundary hardening — validated trader overrides, approval gate (API + router + node), stale-metric clearing, 404/409 resume semantics, instrument registry, `load_book` validation, ns latency, fallback fails closed, decision log, API boundary tests | ✅ Complete |
| 6 | Hardening — SSE streaming, Docker + C++ module packaging for Linux, multi-instrument support | 🔲 Pending |

### Roadmap (post-demo)

| Item | Description |
|---|---|
| SSE streaming | Stream LLM reasoning tokens to the HITL panel in real time as strategy_node runs |
| Live market data | Replace the CSV snapshot with a streaming feed (e.g. a Kafka consumer) rebuilding the LOB in real time |
| Multi-instrument | Extend the instrument registry (`book_loader.INSTRUMENTS`, ZN only today) with tick size, contract multiplier and a book source per instrument |
| Order book depth display | Show the L2 ladder in the HITL panel so the trader sees what the C++ swept |
| Docker + Linux | Package the C++ module for Linux App Service (it already builds and tests on Linux via CMake) |
| Concurrent requests | Load-test N simultaneous trades; move the per-trade resume lock to a shared store for multi-worker deployments |

---

---

## Engineering Takeaways

- **LLMs are most valuable when constrained.** Probabilistic reasoning is where LLMs excel — algorithm selection, qualitative judgment, natural language context. Deterministic computation belongs in native code. Mixing the two produces systems that are neither correct nor fast.
- **Architectural boundaries are safer than prompt engineering.** The LLM in this system *cannot* compute slippage, slice sizes, or costs — not because it is told not to, but because every number is produced in code: slice sizing by deterministic Python arithmetic with Pydantic-enforced bounds, execution metrics by a C++ engine on a different runtime with no shared state. The constraint is structural.
- **Bound the human input as tightly as the model output.** "The LLM can't compute numbers" invites "but can the user?". Trader overrides go through the same plan rules, and the approval gate is enforced in three places so a UI bug cannot dispatch on stale numbers.
- **The boundary's units contract is part of the architecture.** The native engine returns costs in the instrument's raw price units; the orchestration layer owns the conversion to currency via tick metadata. Getting the semantics right at the seam — direction, units, partial fills — is the actual integration skill, and it is covered by tests.
- **Human-in-the-loop should be an architectural checkpoint, not a UI afterthought.** LangGraph's `interrupt()` + `SqliteSaver` checkpointer makes the HITL pause a first-class graph primitive — the full execution state serializes to disk and resumes from the checkpoint. Concurrent decisions on one trade are serialized by a per-thread lock (single-process POC).

### Regulatory framing

For a model-risk reader (e.g. Fed SR 11-7): the **model** in this system is the LLM's strategy recommendation — a qualitative label and a slice count, reviewed by a human before any action. The C++ engine, the slice arithmetic and the approval gate are deterministic tools whose behaviour is pinned by the test suite. Model validation, ongoing monitoring, entitlements and a compliance-grade audit store are out of scope for this POC.

---

See [DEMO.md](DEMO.md) for a full walkthrough with screenshots.  
See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design rationale, decision log, and phase definitions.  
See [docs/known-gaps.md](docs/known-gaps.md) for POC scope and assumptions.
