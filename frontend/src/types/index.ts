/**
 * TypeScript types mirroring the FastAPI Pydantic models in backend/main.py.
 * Keep in sync when backend models change.
 */

// ── Submit payload ─────────────────────────────────────────────────────────────
export interface TradeRequestBody {
  prompt:       string;   // "Liquidate 50,000 TSLA by EOD — factory delay news"
  instrument:   string;   // ticker or ISIN
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
}

export interface SlippageMetrics {
  avg_fill_price:        number;
  slippage_bps:          number;
  market_impact_bps:     number;
  total_cost_usd:        number;
  simulation_latency_us: number;
}

export interface TradeState {
  thread_id:        string;
  status:           "awaiting_approval" | "completed" | "aborted";
  trade_request:    TradeRequestBody | null;
  strategy:         Strategy | null;
  slippage_metrics: SlippageMetrics | null;
  revision_count:   number;
  errors:           string[];
}

// ── POST /api/resume/{thread_id} ──────────────────────────────────────────────
export interface ResumePayload {
  action:           "approve" | "revise" | "abort";
  override_params?: {
    num_slices?:       number;
    shares_per_slice?: number;
  } | null;
}

export interface ResumeResponse {
  thread_id: string;
  status:    "awaiting_approval" | "completed";
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
