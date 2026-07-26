"use client";

import { useState, useCallback } from "react";
import TradeForm from "./components/TradeForm";
import HitlPanel from "./components/HitlPanel";
import type {
  TradeRequestBody,
  TradeState,
  ResumePayload,
  DashboardStatus,
} from "@/types";

const API = ""; // rewrites /api/* → localhost:3001 via next.config.ts

export default function DashboardPage() {
  const [status,     setStatus]     = useState<DashboardStatus>("idle");
  const [threadId,   setThreadId]   = useState<string | null>(null);
  const [tradeState, setTradeState] = useState<TradeState | null>(null);
  const [errorMsg,   setErrorMsg]   = useState<string | null>(null);

  // ── Helper: fetch current paused state from backend ───────────────────────
  const fetchTradeState = useCallback(async (id: string): Promise<TradeState | null> => {
    const r = await fetch(`${API}/api/trade/${id}`);
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail ?? `HTTP ${r.status}`);
    }
    return r.json();
  }, []);

  // ── Submit a new trade ────────────────────────────────────────────────────
  const handleSubmit = useCallback(async (body: TradeRequestBody) => {
    setStatus("submitting");
    setErrorMsg(null);
    setTradeState(null);
    setThreadId(null);

    try {
      // Step 1: POST /api/trade — starts the LangGraph, returns thread_id
      //         when the graph pauses at hitl_node (after LLM + C++ are done).
      const r = await fetch(`${API}/api/trade`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
      });

      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail ?? `HTTP ${r.status}`);
      }

      const { thread_id } = await r.json();
      setThreadId(thread_id);

      // Step 2: GET /api/trade/{thread_id} — retrieve paused state with
      //         strategy + C++ metrics now that both nodes have completed.
      const state = await fetchTradeState(thread_id);
      setTradeState(state);
      setStatus("awaiting_approval");
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : "Unknown error");
      setStatus("error");
    }
  }, [fetchTradeState]);

  // ── Resume with trader decision ───────────────────────────────────────────
  const handleResume = useCallback(async (payload: ResumePayload) => {
    if (!threadId) return;

    // "revising" spinner shows while C++ re-runs with override params
    setStatus(payload.action === "revise" ? "revising" : "submitting");
    setErrorMsg(null);

    try {
      const r = await fetch(`${API}/api/resume/${threadId}`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(payload),
      });

      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail ?? `HTTP ${r.status}`);
      }

      const result = await r.json();

      if (result.status === "awaiting_approval") {
        // Revise path: graph paused again after re-simulation.
        // Fetch updated state so the HITL panel shows new metrics.
        const updated = await fetchTradeState(threadId);
        setTradeState(updated);
        setStatus("awaiting_approval");
      } else {
        // Approve or abort: graph ran to END.
        setStatus(payload.action === "approve" ? "completed" : "aborted");
      }
    } catch (e) {
      setErrorMsg(e instanceof Error ? e.message : "Unknown error");
      setStatus("error");
    }
  }, [threadId, fetchTradeState]);

  // ── Reset for a new trade ─────────────────────────────────────────────────
  const reset = () => {
    setStatus("idle");
    setThreadId(null);
    setTradeState(null);
    setErrorMsg(null);
  };

  const isFormDisabled =
    status === "submitting" || status === "revising" || status === "awaiting_approval";

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* ── Status bar ──────────────────────────────────────────────────────── */}
      <div className="px-3 py-0.5 text-xs text-gray-500 bg-[#1a1b1c] border-b border-[#2a2b2c] flex-shrink-0 flex items-center gap-2">
        <StatusDot status={status} />
        <span>{statusLabel(status)}</span>
        {threadId && (
          <span className="ml-auto font-mono text-gray-600">
            thread: {threadId.slice(0, 8)}…
          </span>
        )}
      </div>

      {/* ── Main layout: form left, HITL panel right ─────────────────────────── */}
      <div className="flex flex-1 gap-3 p-3 min-h-0 overflow-hidden">

        {/* ── Left column: form + terminal states ─────────────────────────── */}
        <div className="flex flex-col gap-3 w-72 flex-shrink-0">
          <TradeForm onSubmit={handleSubmit} disabled={isFormDisabled} />

          {/* Error display */}
          {status === "error" && errorMsg && (
            <div className="alert alert-error text-xs">
              <span>{errorMsg}</span>
            </div>
          )}

          {/* Completed state */}
          {status === "completed" && (
            <div className="bg-[#1c1d1e] rounded border border-green-800/40 p-4 flex flex-col gap-3">
              <div className="flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-green-400" />
                <span className="text-xs font-semibold text-green-400 uppercase tracking-wider">
                  Trade Dispatched
                </span>
              </div>
              <p className="text-xs text-gray-400">
                The approved order has been sent to the execution node.
                A JSON record was written to{" "}
                <span className="font-mono text-gray-300">output/</span>.
              </p>
              <button className="btn btn-xs btn-ghost" onClick={reset}>
                New Trade
              </button>
            </div>
          )}

          {/* Aborted state */}
          {status === "aborted" && (
            <div className="bg-[#1c1d1e] rounded border border-red-800/40 p-4 flex flex-col gap-3">
              <div className="flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-red-400" />
                <span className="text-xs font-semibold text-red-400 uppercase tracking-wider">
                  Trade Aborted
                </span>
              </div>
              <p className="text-xs text-gray-400">
                The trade was cancelled. No order was dispatched.
              </p>
              <button className="btn btn-xs btn-ghost" onClick={reset}>
                New Trade
              </button>
            </div>
          )}

          {/* Submitting spinner */}
          {status === "submitting" && (
            <div className="bg-[#1c1d1e] rounded border border-[#2a2b2c] p-4 flex flex-col items-center gap-3">
              <span className="loading loading-spinner loading-md text-primary" />
              <p className="text-xs text-gray-400 text-center">
                LLM strategy node running…
                <br />
                C++ simulation queued
              </p>
            </div>
          )}
        </div>

        {/* ── Right column: HITL review panel (shown while awaiting or revising) */}
        <div className="flex-1 min-h-0 min-w-0">
          {(status === "awaiting_approval" || status === "revising") && tradeState ? (
            <HitlPanel
              tradeState={tradeState}
              onResume={handleResume}
              isRevising={status === "revising"}
            />
          ) : (
            /* Empty-state placeholder — shown while idle, submitting, or done */
            <div className="h-full border border-dashed border-[#2a2b2c] rounded flex flex-col items-center justify-center text-center p-8 gap-4">
              <div className="text-4xl opacity-20">⚡</div>
              <div className="text-xs text-gray-600 max-w-xs">
                Submit a trade on the left. The LLM will determine strategy and the
                C++ LOB engine will compute exact slippage. You will review both
                before any order is dispatched.
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function statusLabel(s: DashboardStatus): string {
  switch (s) {
    case "idle":              return "Ready";
    case "submitting":        return "Running strategy + simulation…";
    case "awaiting_approval": return "Awaiting trader decision";
    case "revising":          return "Re-running C++ simulation…";
    case "completed":         return "Trade dispatched";
    case "aborted":           return "Trade aborted";
    case "error":             return "Error";
  }
}

function StatusDot({ status }: { status: DashboardStatus }) {
  const colors: Record<DashboardStatus, string> = {
    idle:              "bg-gray-600",
    submitting:        "bg-blue-400 pulse-dot",
    awaiting_approval: "bg-amber-400 pulse-dot",
    revising:          "bg-blue-400 pulse-dot",
    completed:         "bg-green-400",
    aborted:           "bg-red-400",
    error:             "bg-red-600",
  };
  return <span className={`w-1.5 h-1.5 rounded-full ${colors[status]}`} />;
}
