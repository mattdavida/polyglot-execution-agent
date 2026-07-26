# Polyglot Execution Agent — Architecture & Design Decisions

> **Status:** POC complete. All phases 0–5 delivered. See [README.md](README.md) for build status and [DEMO.md](DEMO.md) for the full walkthrough.  
> **Design Philosophy:** Complexity is intentional. The C++ rigor is the point — this architecture is built to demonstrate a hard boundary between probabilistic AI and deterministic computation.

---

## 1. Project Context & Business Goal

**The Goal:** Build a Proof of Concept that showcases a "best of both worlds" architecture — using LLMs for unstructured reasoning and strategy formulation, while offloading strict, deterministic computation to a high-speed C++20 core.

**The Value Proposition:** LLMs hallucinate. In financial execution, an LLM must never calculate slippage or market impact. This architecture proves that AI can be safely *constrained* by deterministic, high-performance native systems, with a Human-In-The-Loop (HITL) safety net as the final gate before any action is taken.

**HITL Design Choice:** Prior analyst-in-the-loop patterns (e.g. document review pipelines) are *asynchronous* — the graph runs to completion and the analyst reviews a finished artifact. This system requires a *synchronous* HITL — the graph pauses mid-execution, its entire state serialized, and a trader must act before any state transition continues.

---

## 2. System Architecture & Stack

| Layer | Technology | Purpose |
| :--- | :--- | :--- |
| **Backend API** | Python, FastAPI, Uvicorn | HTTP + SSE server. Bridges LangGraph to the frontend. Owns the `/resume` endpoint contract. |
| **Cognitive / Orchestration** | LangGraph, Azure OpenAI | Ingests trade requests, formulates strategy (VWAP vs. Sweep vs. Iceberg), routes state, issues the synchronous HITL pause. |
| **Compute Core** | C++20 | Deterministic Limit Order Book (LOB) simulator. Pre-allocated, zero-heap-allocation matching engine. Calculates exact slippage, market impact, and fill cost in microseconds. |
| **The Bridge** | `pybind11` | Exposes the C++ core to Python as a native module (`import execution_engine`). The boundary between probabilistic AI and deterministic math. |
| **Frontend UI** | Next.js 16, Tailwind, AG Grid | Trader dashboard. Streams LLM reasoning live via SSE. Displays C++ computed metrics. Issues Approve / Modify / Reject back to the FastAPI backend. |
| **State Persistence** | SQLite (dev) → Postgres (prod) | LangGraph checkpointer. Required for synchronous HITL — the graph's mid-execution state must survive the HTTP round-trip between pause and resume. |

---

## 3. The Execution Flow (Detailed)

```
Trader submits prompt
        │
        ▼
[FastAPI] POST /api/trade
        │
        ▼
[LangGraph] strategy_node
  LLM: "Liquidate 50k TSLA by EOD"
  → decides: 5 slices × 10k shares, VWAP strategy
  → writes Strategy to state
        │
        ▼
[LangGraph] simulation_node
  Python calls C++ module (pybind11):
    execution_engine.simulate(order_size=10000, book_depth=...)
  → returns: avg_fill_price, slippage_bps, market_impact_bps, total_cost_usd
  → writes SlippageMetrics to state
        │
        ▼
[LangGraph] hitl_node  ← INTERRUPT HERE
  Graph checkpoints state to SQLite/Postgres
  FastAPI receives the paused state
  SSE stream pushes { status: "awaiting_approval", thread_id, reasoning, metrics }
  to the frontend
        │
        ▼
[Next.js Dashboard]
  Trader reviews:
    - LLM natural language reasoning (streamed live)
    - C++ computed: slippage_bps, market_impact_bps, total_cost_usd
  Trader acts:
    ┌─────────────┬────────────────┬───────────────┐
    │   APPROVE   │    MODIFY      │    REJECT     │
    │             │ (new params)   │               │
    └──────┬──────┴───────┬────────┴──────┬────────┘
           │              │               │
           ▼              ▼               ▼
   POST /api/resume  POST /api/resume  POST /api/resume
   { action: ok }   { action: revise  { action: abort }
                      slices: 3 }
        │              │
        ▼              ▼
[LangGraph resumes via thread_id + checkpointer]
    approve → execution_node (stub: log the fills)
    revise  → simulation_node (re-run C++ with new params)
    abort   → end
```

---

## 4. The HITL Contract (Critical Design Decision)

This is the most architecturally important detail in the system. It must be correct before any code is written.

### 4.1 Why an async approach does not work here

An async analyst-review pattern runs LangGraph to completion in a background thread and stores results for later review. The analyst reviews a completed artifact. There is no live paused graph.

This system needs a *live paused graph* — a graph that is suspended mid-execution, with its entire state serialized, waiting for human input to resume.

### 4.2 LangGraph Checkpointer

LangGraph supports this via a persistent `Checkpointer`. The graph is compiled with one:

```python
from langgraph.checkpoint.sqlite import SqliteSaver

checkpointer = SqliteSaver.from_conn_string("./checkpoints.db")
graph = workflow.compile(checkpointer=checkpointer, interrupt_before=["hitl_node"])
```

Every `graph.invoke(...)` call must pass a `config` with a unique `thread_id`:

```python
config = {"configurable": {"thread_id": str(uuid4())}}
graph.invoke({"trade_request": prompt}, config=config)
```

When the graph hits `hitl_node`, it serializes its state to the checkpointer DB and returns control to the caller. The `thread_id` is the key to resuming it later.

### 4.3 The Resume Endpoint

```python
# FastAPI
@app.post("/api/resume/{thread_id}")
async def resume_trade(thread_id: str, body: ResumePayload):
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke(
        Command(resume={"action": body.action, "params": body.params}),
        config=config
    )
    return result
```

### 4.4 The Three Resume Paths

| Action | Graph Behavior |
| :--- | :--- |
| `approve` | Transitions to `execution_node` (logs the fills, POC stub) |
| `revise` | Transitions back to `simulation_node` with updated slice/size params, re-runs C++ LOB |
| `abort` | Transitions to `end` node, marks trade cancelled |

The `revise` path is the most complex. It requires the `simulation_node` to accept both fresh invocations and mid-graph re-entries with modified parameters. Design the state `TypedDict` to handle a `revision_count` field and `override_params` so the strategy node is not re-run on revise (only the C++ simulation).

---

## 5. Strict Engineering Constraints

### 5.1 C++ Compute Core (Non-Negotiable)

These constraints exist because this is deliberate training for HFT systems. They will not be relaxed. Complexity is expected.

* **Zero-Allocation Hot Path:** The order book matching loop must NOT allocate on the heap. No `new`, no `std::shared_ptr`, no `std::vector::push_back` during execution. Use a pre-allocated memory pool: a flat `std::array<PriceLevel, MAX_LEVELS>` where `MAX_LEVELS` is a `constexpr`.

* **No String Lookups:** No `std::string` or `strcmp` on the hot path. All instrument identifiers must be integers, enums, or compile-time hashed IDs (`constexpr` FNV-1a hash is appropriate).

* **Modern C++20:** Use `std::span` for non-owning views over the order book array. Use `constexpr` for all compile-time constants. Use `[[nodiscard]]` on all computation functions.

* **Data Ownership over Locking:** Thread safety via strict ownership — the C++ core owns its data exclusively during a simulation run. No speculative `std::mutex`. No `std::atomic` unless a specific shared counter requires it.

* **Intrusive Data Structures:** For the price level linked list (walking the book), use an intrusive list where nodes are embedded in the pre-allocated array, not heap-allocated separately.

### 5.2 Complexity Expectation

Phase 2 (the LOB core) is the highest-risk phase. A correctly pre-allocated, intrusive LOB with a working market-order sweep is non-trivial. Budget significant time here. If the LOB implementation is blocked, the fallback is a correct-but-non-zero-allocation implementation to unblock Phases 3 and 4, with the low-latency version completed in parallel. Do not let C++ block the full system.

### 5.3 Windows Build Toolchain

| Tool | Requirement |
| :--- | :--- |
| Visual Studio 2022 (C++ workload) | MSVC x64 compiler (`cl.exe`) |
| CMake 3.22+ | Bundled with VS |
| Ninja | Bundled with VS at `Common7/IDE/CommonExtensions` |
| Python 3.11+ + venv | Backend runtime and pybind11 host |
| Azure OpenAI credentials | Populate `.env` from `.env.example` |
| clangd / Cursor | `cpp/.clangd` strips MSVC flags for IntelliSense |

**Build pattern:** `build.ps1` loads the MSVC dev shell, configures CMake with `Python3_ROOT_DIR` pointing at the active venv, builds with Ninja, and copies `compile_commands.json` for clangd. Run it with the venv active.

**Note:** The compiled output is a `.pyd` (Windows DLL with Python ABI). `pybind11_add_module()` in CMake handles this automatically — no manual configuration needed.

---

## 6. LangGraph State Design

```python
from typing import TypedDict, Optional, Literal
from dataclasses import dataclass

class TradeRequest(TypedDict):
    prompt: str
    instrument: str          # integer ID at C++ boundary; string here for LLM
    total_shares: int
    deadline: str

class Strategy(TypedDict):
    approach: Literal["VWAP", "TWAP", "Sweep", "Iceberg"]
    num_slices: int
    shares_per_slice: int
    reasoning: str           # LLM natural language output

class SlippageMetrics(TypedDict):
    avg_fill_price: float
    slippage_bps: float
    market_impact_bps: float
    total_cost_usd: float
    simulation_latency_us: int   # microseconds — surfaced in UI for the demo

class HumanFeedback(TypedDict):
    action: Literal["approve", "revise", "abort"]
    override_params: Optional[dict]   # e.g. {"num_slices": 3}

class TradeState(TypedDict):
    trade_request: TradeRequest
    strategy: Optional[Strategy]
    slippage_metrics: Optional[SlippageMetrics]
    human_feedback: Optional[HumanFeedback]
    revision_count: int
    errors: list[str]
    thread_id: str
```

---

## 7. Real-Time Streaming (SSE) — Demo Differentiator

For the demo to be maximally compelling, the trader must *watch* the LLM reason before the HITL pause. This requires Server-Sent Events (SSE) from FastAPI.

```python
from fastapi.responses import StreamingResponse

@app.post("/api/trade/stream")
async def stream_trade(request: TradeRequest):
    async def event_generator():
        async for chunk in graph.astream(request, config=config):
            yield f"data: {json.dumps(chunk)}\n\n"
        yield f"data: {json.dumps({'status': 'awaiting_approval'})}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

On the Next.js side, use the native `EventSource` API or a hook wrapping `fetch` with `ReadableStream` to consume the SSE and update the UI in real time as the LLM tokens arrive.

This feature separates the demo from a static results page. It is optional for correctness but high-value for presentation.

---

## 8. Phased Execution Roadmap (Revised)

### Phase 0: Environment & Toolchain
*Goal: Prove the pybind11 bridge compiles and imports.*

1. Create a Python `venv`: `python -m venv .venv`
2. Activate and install: `pip install pybind11`
3. Confirm: `python -m pybind11 --includes` (outputs the header paths CMake needs)
4. Write a minimal `CMakeLists.txt` using `pybind11_add_module` pointing `Python3_ROOT_DIR` at the venv
5. Write `build.ps1` to load the MSVC dev shell and run `cmake --build` (CMake both compiles and emits `compile_commands.json`)
6. Configure `cpp/.clangd` to strip MSVC-specific flags for IntelliSense

**Exit criterion:** pybind11 smoke-test module imports and returns expected output from Python. ✅ Complete.

### Phase 1: The C++ / Python Bridge Scaffold
*Goal: Prove the pybind11 bridge is functional with real timing instrumentation.*

* Scaffold the full C++ project structure: `src/`, `include/`, `CMakeLists.txt`.
* Create `ExecutionSimulator` class with method:
  ```cpp
  SimulationResult calculate_impact(int shares, double price) const noexcept;
  ```
* `SimulationResult` is a C++ `struct` mapped to a Python `dict` via pybind11 `def_readwrite`.
* The Python test script calls `calculate_impact`, captures the result, and logs:
  - Return values
  - Execution time in **microseconds** using `std::chrono::high_resolution_clock`
* **Exit criterion:** Python test prints latency ≤ 10µs for the dummy implementation.

### Phase 2: The Limit Order Book (LOB) Core (C++) — High Complexity
*Goal: A correct, pre-allocated LOB that can sweep a market order and return precise fill metrics.*

**Data structures:**
```cpp
constexpr std::size_t MAX_LEVELS = 10;

struct PriceLevel {
    double price;
    int    available_shares;
    int    next_idx;   // intrusive list: index into the flat array, -1 = end
};

struct OrderBook {
    std::array<PriceLevel, MAX_LEVELS> asks;  // pre-allocated, sorted ascending
    std::array<PriceLevel, MAX_LEVELS> bids;  // pre-allocated, sorted descending
    int ask_head_idx = 0;
    int bid_head_idx = 0;
    int num_ask_levels = 0;
    int num_bid_levels = 0;
};
```

**Matching logic (no heap allocation):**
* `sweep_market_order(OrderBook& book, int order_size)` — walks the intrusive ask list, consuming levels, accumulating filled shares and VWAP fill price.
* Returns `SimulationResult { avg_fill_price, slippage_bps, market_impact_bps, total_cost_usd }`.
* All arithmetic is `double` with `[[nodiscard]]` on the return value.
* No `std::string` in the hot path. Instrument ID is a `uint32_t` enum.

**pybind11 exposure:** Map `SimulationResult` to a Python dict with field names matching `SlippageMetrics` TypedDict.

**Exit criterion:** Python test loads a dummy book (5 price levels, known depth), sweeps 10k shares, and returns mathematically verifiable slippage numbers. Latency logged.

**Complexity note:** If the intrusive list proves to be a blocker, implement with `std::array` index walking (no pointer manipulation) first. The architecture is correct either way; the intrusive approach is the learning stretch goal.

### Phase 3: LangGraph AI Orchestration (Python)
*Goal: A working stateful graph with a synchronous HITL pause that can be resumed via HTTP.*

* **Dependencies:** `langgraph`, `langchain-openai`, `langgraph-checkpoint-sqlite`, `fastapi`, `uvicorn`, `pybind11` module from Phase 2.
* Define `TradeState` TypedDict (Section 6 above).
* Build `strategy_node`: LLM call with structured output (`Strategy` TypedDict). Prompt engineering is explicit — the LLM is told it must NOT perform any numerical calculation; it only decides approach and slicing.
* Build `simulation_node`: imports `execution_engine` (C++ module), calls `simulate()`, writes `SlippageMetrics` to state. Accepts `override_params` from `HumanFeedback` for the revise path.
* Build `hitl_node`: declared as the interrupt point. In practice it is a pass-through node — LangGraph's `interrupt_before=["hitl_node"]` does the pause.
* Build `execution_node`: POC stub — logs the approved trade to stdout and a text file.
* Wire the graph edges including the conditional resume routing (`approve` / `revise` / `abort`).
* Build FastAPI backend:
  - `POST /api/trade` — invoke graph, return `{ thread_id, status: "awaiting_approval" }`
  - `GET /api/trade/{thread_id}` — return current paused state from checkpointer
  - `POST /api/resume/{thread_id}` — resume graph with `HumanFeedback` payload
  - `GET /api/health` — heartbeat
* **Exit criterion:** `curl`-driven test: submit a trade, receive `thread_id`, fetch paused state, POST approve, confirm graph reaches `execution_node`.

### Phase 4: Trader Dashboard (Next.js)
*Goal: A compelling, real-time UI that makes the architecture legible to a non-technical client.*

**Views:**
* **Trade Submission** — free-text prompt input + instrument selector → `POST /api/trade`
* **Live Reasoning Panel** — SSE stream showing LLM tokens arriving in real time (optional but high-value)
* **HITL Review Panel** — displays:
  - LLM reasoning block (natural language strategy)
  - C++ metrics table: `avg_fill_price`, `slippage_bps`, `market_impact_bps`, `total_cost_usd`, `simulation_latency_us`
  - **[APPROVE]** / **[MODIFY]** / **[REJECT]** action buttons
* **Modify Modal** — lets trader override `num_slices` / `shares_per_slice`, re-runs simulation
* **Resolution Panel** — shows final approved/rejected/modified trade record

**Implementation notes:**
* Proxy `/api/*` → FastAPI (`localhost:3001`) in `next.config.ts`
* Use DaisyUI components for the metrics display
* `simulation_latency_us` should be displayed prominently — it is a demo talking point

**Exit criterion:** End-to-end demo runnable from a single terminal: `uvicorn` + `next dev`. Submit a trade, watch reasoning stream, review metrics, click Approve.

---

## 9. Open Questions & Reassessment Points

These are the questions to resolve before locking the final plan:

| # | Question | Status | Decision |
| :-- | :-- | :-- | :-- |
| 1 | **Azure OpenAI or OpenAI direct?** | **Resolved** | Azure OpenAI — dedicated resource per project. Credentials in `.env`. |
| 2 | **SQLite or Postgres for checkpointer?** | **Resolved** | SQLite. Checkpointer state is graph snapshots, not relational data. Zero config, single file. One-line swap to Postgres if deploying. |
| 3 | **Is `execution_node` a stub or real?** | **Resolved** | POC stub — logs approved trade to stdout and a `.json` file. No FIX protocol. |
| 4 | **Streaming (SSE) — Phase 4 or nice-to-have?** | **Resolved** | Phase 4b (demo polish). Build Phase 4 core with `graph.invoke()` (sync) first. Prove all three HITL resume paths are correct, then swap to `graph.astream()` and wire the frontend `EventSource` as an isolated, final pass. Keeps HITL debugging clean. |
| 5 | **LOB market data — static or dynamic?** | **Resolved** | Static dummy book (hardcoded depth levels). Dynamic feed is out of scope for POC. |
| 6 | **Windows `.pyd` vs WSL `.so`?** | **Resolved** | Native Windows MSVC confirmed. No WSL needed. |

---

## 10. Phase 5: Azure Infrastructure (Bicep)
*Goal: Make the project one-command reproducible for any future developer or client demo.*

Executed **after** Phase 4's local exit criterion is green. Simpler resource set than a full RAG pipeline — no Postgres, no vector store. Checkpointer is SQLite (file-based, no Azure resource).

**Azure resources required:**

| Resource | Module | Notes |
| :--- | :--- | :--- |
| Azure OpenAI | `modules/openai.bicep` | Chat model only — no embedding deployment |
| Key Vault | `modules/keyvault.bicep` | Stores API key for prod; skip for local dev |
| App Service Plan + Apps | `modules/app-service.bicep` | Gated by `deployAppService` flag (false by default) |

**Deliverables:**
* `infra/main.bicep` — orchestrates openai + keyvault + appService modules
* `infra/modules/openai.bicep` — chat-only deployment
* `infra/modules/keyvault.bicep` — API key storage
* `infra/modules/app-service.bicep` — FastAPI backend + Next.js frontend apps
* `infra/params/dev.bicepparam` — project name `exa`, dev SKUs
* `infra/params/prod.bicepparam` — prod SKUs
* `infra/deploy.ps1` — provisions resources and prints `.env` values on success
* `infra/cleanup.ps1` — removes the resource group

**One-command local dev onboarding (future dev):**
```powershell
# 1. Deploy Azure OpenAI only (deployAppService = false)
.\infra\deploy.ps1 -Environment dev

# 2. Paste outputs into .env, then:
python -m venv .venv && .venv\Scripts\Activate.ps1
pip install -r requirements.txt
.\cpp\build.ps1
uvicorn backend.main:app --reload --port 3001
cd frontend && npm install && npm run dev
```

**Exit criterion:** `az deployment group what-if` runs clean. A fresh clone + `deploy.ps1` produces a working `.env` with no manual Azure portal steps.

---

## 11. Success Criteria for the POC

The POC is complete and presentable when the following are true end-to-end:

1. A natural language trade prompt reaches the LangGraph agent.
2. The LLM produces a named strategy with slice parameters — and does **not** perform any arithmetic.
3. The C++ module returns verifiable slippage numbers in < 100µs.
4. The dashboard displays both the LLM reasoning and the C++ metrics side-by-side.
5. The trader can Approve, Modify (triggering a C++ re-simulation), or Reject.
6. The graph resumes correctly in all three paths.
7. The `simulation_latency_us` value is visible in the UI — this is the demo's proof-of-concept moment.
