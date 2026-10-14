# Known gaps (POC)

This is a **reference architecture and proof of concept**, not a trading system. What it demonstrates is the *boundary*: an LLM constrained to qualitative choices, every number produced by deterministic code, and a human approval gate that cannot be passed on incomplete or stale numbers. Everything below is deliberately out of scope, simplified, or assumed — stated up front so a reviewer does not have to find it.

## What is enforced (and tested)

| Contract | Where | Test |
|---|---|---|
| The LLM outputs an algorithm label, a slice count (1–20) and narrative — no sizes, prices or costs | `StrategyOutput` in `strategy_node.py` | `test_python_boundary.py` |
| Slice size is ceil division in Python; the count is normalised so no slice is empty | `backend/pipeline/plan.py` | `TestPlanFromSliceCount` |
| Trader overrides are bounded exactly like LLM output (strict ints, no other keys, plan must cover the order) | `OverrideParams` in `main.py` + `apply_override` | `test_graph_boundary.py` |
| Approve requires current C++ metrics, no simulation error, and a non-fallback strategy | `approval_block_reason` — checked by the API, the graph router and `execution_node` | `test_graph_boundary.py` |
| Failed re-simulation clears metrics instead of leaving stale ones | `simulation_node.py` | `test_engine_failure_clears_metrics_and_blocks_approval` |
| Resume refuses unknown (404), finished/aborted (409) and concurrent (409) decisions | `main.py` | `test_graph_boundary.py` |
| Only registered instruments are accepted (ZN) | `book_loader.INSTRUMENTS` | `test_bad_trade_requests_are_rejected` |
| `load_book` sorts all input before keeping 10 levels; rejects non-finite / non-positive prices, non-positive quantities and crossed books | `execution_engine.cpp` | `TestLoadBookContract` |
| No heap allocation inside `simulate()` | `execution_engine.cpp` | Verified with an allocation-counting harness (1,000,000 calls); not part of the pytest suite |

## Market model — simplified by design

| Gap | Detail |
|---|---|
| **One instrument** | ZN only. Other instruments are rejected with 422 until they have metadata and a book source in `book_loader.INSTRUMENTS`. |
| **Static snapshot** | The book is reconstructed once from a 10-minute window ending 09:30 and cached. No live feed, no replenishment. |
| **Locked book** | The default snapshot reconstructs with best bid == best ask (an artefact of anchoring both sides to the last trade price). The engine accepts locked books and rejects crossed ones. |
| **One slice simulated** | The engine sweeps a single child order of `shares_per_slice`. Metrics are **per slice**, not for the parent order. |
| **Strategy label does not move the numbers** | VWAP / TWAP / Sweep / Iceberg all simulate the same immediate sweep; only slice size affects the result. The label is the LLM's qualitative recommendation for the trader. |
| **Impact model is a placeholder** | `market_impact_bps = 0.5 × slippage_bps`. Not calibrated; a firm's own impact model would replace the engine at this seam. |
| **Partial fills are allowed with a warning** | A slice larger than the visible book fills partially; the fill ratio and a warning are shown and recorded. Approval is the trader's call. |

## Data and units — assumed, not verified

| Gap | Detail |
|---|---|
| **Data provenance unverified** | `data/2016_12_23.csv` is a real-world-looking ZN tick file of unconfirmed origin. It is gitignored and not redistributed. Without it, a synthetic fallback book is used and flagged in the UI. |
| **Price encoding assumed** | Raw prices (≈ 7000) are treated as integer price units with 1 tick = 0.5 units. USD cost uses ZN's $15.625 per tick per contract and holds under that assumption. Slippage in bps is computed on the raw index and is only meaningful if the index is proportional to the quoted price. |

## HITL, audit and security — POC level

| Gap | Detail |
|---|---|
| **No authentication or user identity** | Any caller that can reach the API can approve. The decision log records *what* was decided and *when*, not *who*. |
| **Approval record is not an audit log** | `output/trade_<thread_id>.json` is a local, mutable convenience file. The SQLite checkpoint holds full graph history but is local and gitignored. A production system would write decisions to an append-only, access-controlled store. |
| **No model provenance stamped** | Deployment name, model version and prompt version are not stored with the strategy. |
| **LLM narrative is not policed** | The prompt tells the model not to state figures, and the UI labels the rationale "narrative — not computed", but nothing strips numbers from it. |
| **Single process, local SQLite** | The per-thread resume lock is in-process. Multiple API workers would need a shared lock or a database-level revision check. |
| **CORS / docs** | CORS origins come from `ALLOWED_ORIGINS`; `/docs` is on by default (`ENABLE_API_DOCS=false` to disable). |

## Engineering

| Gap | Detail |
|---|---|
| **Build scripts** | `cpp/build.ps1` targets Windows/MSVC. The CMake project itself is portable: the module builds warning-free with `-Werror` and the full test suite passes on Linux (g++ 11, CMake + Ninja). |
| **Azure App Service module** | `infra/modules/app-service.bicep` provisions a Linux Python app, but nothing packages the C++ module for Linux. Treat it as a placeholder; local run is the supported path. |
| **No CI** | Tests run locally (`python -m pytest tests/`). |
| **Python dependencies unpinned** | `requirements.txt` uses minimum versions; there is no lock file. |
| **Concurrency benefit of GIL release is small** | `simulate()` releases the GIL, which keeps a sweep from blocking other Python threads, but the sweep is tens of nanoseconds — comparable to the cost of releasing the GIL. |
