/**
 * TypeScript types mirroring the FastAPI Pydantic models in backend/main.py.
 * Keep in sync when backend models change.
 */

// ── Submit payload ─────────────────────────────────────────────────────────────
export interface TradeRequestBody {
  prompt:       string;   // free-text context for the LLM (≤ 2000 chars)
  instrument:   string;   // must be in the backend instrument registry (ZN only today)
  side:         "buy" | "sell";  // explicit direction — a liquidation is a sell
  total_shares: number;
  deadline:     string;   // "end of day", "10:00 AM", etc.
}

// ── POST /api/trade response ───────────────────────────────────────────────────
export interface SubmitResponse {
  thread_id: string;
  status:    "awaiting_approval";
}

// ── GET /api/trade/{thread_id} response ───────────────────────────────────────
export interface Strategy {
  approach:        "VWAP" | "TWAP" | "Sweep" | "Iceberg";
  num_slices:      number;
  shares_per_slice: number;
  reasoning:       string;
  // Who chose the slice parameters in force. "fallback" cannot be approved
  // until a trader confirms the slice count via Modify.
  origin:          "llm" | "fallback" | "trader";
}

export interface SlippageMetrics {
  avg_fill_price:        number;  // in the book's raw price units (1 tick = 0.5)
  slippage_bps:          number;  // adverse move vs arrival price (positive = worse)
  market_impact_bps:     number;
  total_cost_usd:        number;  // USD, PER SLICE, via instrument tick metadata
  total_filled:          number;  // contracts actually filled by the simulated sweep
  fill_ratio:            number;  // total_filled / order_size — < 1 means partial fill
  simulation_latency_ns: number;  // C++ sweep only, nanoseconds
  for_revision:          number;  // revision these numbers were computed for
}

export interface DecisionLogEntry {
  at:              string;
  revision:        number;
  action:          string;
  override_params: Record<string, number> | null;
  outcome:         string;   // "accepted" | "refused: …"
  metrics_present: boolean;
}

export interface TradeState {
  thread_id:        string;
  status:           "awaiting_approval" | "completed" | "aborted" | "failed";
  trade_request:    TradeRequestBody | null;
  strategy:         Strategy | null;
  slippage_metrics: SlippageMetrics | null;
  simulation_error: string | null;     // C++ run for the current plan failed
  simulation_warnings: string[];       // partial fill, synthetic fallback book, …
  can_approve:      boolean;           // the backend approval gate's verdict
  approval_blocked_reason: string | null;
  revision_count:   number;
  decision_log:     DecisionLogEntry[];
  errors:           string[];          // cumulative diagnostic log
}

// ── POST /api/resume/{thread_id} ──────────────────────────────────────────────
// override_params is required for "revise" and forbidden otherwise.
// Only slice parameters can be overridden; the backend recomputes the other one.
export interface ResumePayload {
  action:           "approve" | "revise" | "abort";
  override_params?: {
    num_slices?:       number;
    shares_per_slice?: number;
  };
}

export interface ResumeResponse {
  thread_id: string;
  status:    "awaiting_approval" | "completed" | "aborted" | "failed";
  action?:   "approve" | "revise" | "abort";
}

// ── UI-only state machine ──────────────────────────────────────────────────────
// Tracks which screen the trader is looking at. Not sent to the API.
export type DashboardStatus =
  | "idle"              // blank form
  | "submitting"        // POST /api/trade in flight
  | "awaiting_approval" // HITL panel visible, waiting for trader action
  | "revising"          // POST /api/resume (revise) in flight, re-sim running
  | "completed"         // trade approved and dispatched
  | "aborted"           // trade aborted
  | "error";            // something went wrong
