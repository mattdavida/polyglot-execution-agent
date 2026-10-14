"use client";

import { useState } from "react";
import type { TradeState, ResumePayload } from "@/types";
import StrategyCard from "./StrategyCard";
import MetricsCard from "./MetricsCard";

interface Props {
  tradeState: TradeState;
  onResume:   (payload: ResumePayload) => void;
  isRevising: boolean;
}

export default function HitlPanel({ tradeState, onResume, isRevising }: Props) {
  // "modify" mode shows override fields inline — no modal, no navigation
  const [mode,           setMode]           = useState<"review" | "modify">("review");
  const [overrideSlices, setOverrideSlices] = useState(
    String(tradeState.strategy?.num_slices ?? "")
  );

  const {
    strategy, slippage_metrics, trade_request, revision_count,
    simulation_error, simulation_warnings, can_approve, approval_blocked_reason,
  } = tradeState;

  function approve() {
    onResume({ action: "approve" });
  }

  function abort() {
    onResume({ action: "abort" });
  }

  // The trader chooses the slice COUNT; the backend computes the size with
  // the same deterministic rule it applies to the LLM's choice. The backend
  // validates the value too — the input bounds here are a convenience only.
  const parsedSlices = Number(overrideSlices);
  const slicesValid  = Number.isInteger(parsedSlices) && parsedSlices >= 1 && parsedSlices <= 20;

  function submitRevision() {
    if (!slicesValid) return;
    onResume({ action: "revise", override_params: { num_slices: parsedSlices } });
    setMode("review");
  }

  return (
    <div className="bg-[#1a1b1c] rounded border border-[#2a2b2c] flex flex-col h-full min-h-0 overflow-hidden">
      {/* ── Panel header ────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-2 px-4 py-2 bg-[#0d1f35] border-b border-[#0a3a6a] flex-shrink-0">
        <span className="w-2 h-2 rounded-full bg-amber-400 pulse-dot" />
        <span className="text-xs font-semibold text-amber-300 uppercase tracking-wider">
          Awaiting Trader Approval
        </span>
        {revision_count > 0 && (
          <span className="ml-auto text-xs text-gray-500">
            Revision {revision_count}
          </span>
        )}
      </div>

      {/* ── Trade summary ────────────────────────────────────────────────────── */}
      {trade_request && (
        <div className="px-4 py-2 border-b border-[#2a2b2c] flex-shrink-0">
          <div className="flex items-baseline gap-3">
            <span className="text-lg font-bold text-gray-100 font-mono">
              {trade_request.instrument}
            </span>
            <span
              className={`badge badge-sm font-semibold ${
                trade_request.side === "buy" ? "badge-success" : "badge-error"
              }`}
            >
              {trade_request.side?.toUpperCase() ?? "SELL"}
            </span>
            <span className="text-sm text-gray-300 font-mono">
              {trade_request.total_shares.toLocaleString()} contracts
            </span>
            <span className="text-xs text-gray-500">
              by {trade_request.deadline}
            </span>
          </div>
          {trade_request.prompt && (
            <p className="text-xs text-gray-500 mt-0.5 line-clamp-2">
              {trade_request.prompt}
            </p>
          )}
        </div>
      )}

      {/* ── Cards ───────────────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto p-4 flex flex-col gap-3 min-h-0">
        {strategy && (
          <StrategyCard strategy={strategy} revisionCount={revision_count} />
        )}
        {/* Fallback strategy — the LLM did not choose this plan */}
        {strategy?.origin === "fallback" && (
          <div className="alert alert-warning py-2 px-3 text-xs">
            <span>
              LLM unavailable — this is a deterministic fallback plan. Approval is
              blocked until you confirm the slice count with Modify.
            </span>
          </div>
        )}

        {slippage_metrics ? (
          <MetricsCard metrics={slippage_metrics} />
        ) : (
          <div className="bg-[#1c1d1e] rounded border border-red-800/40 p-4 text-xs text-gray-400">
            No C++ metrics for the current plan. Nothing can be approved until a
            simulation succeeds — revise the plan or abort.
          </div>
        )}

        {simulation_error && (
          <div className="alert alert-error py-2 px-3 text-xs">
            <span>C++ simulation failed for this plan: {simulation_error}</span>
          </div>
        )}

        {simulation_warnings?.map((w) => (
          <div key={w} className="alert alert-warning py-2 px-3 text-xs">
            <span>{w}</span>
          </div>
        ))}

        {/* ── Modify form (inline, shown when trader clicks Modify) ────────── */}
        {mode === "modify" && (
          <div className="bg-[#1c1d1e] rounded border border-amber-800/40 p-4 flex flex-col gap-3">
            <div className="text-xs font-semibold text-amber-400 uppercase tracking-wider">
              Override Parameters
            </div>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-gray-500">Number of Slices (1–20)</span>
              <input
                type="number"
                min={1}
                max={20}
                step={1}
                className="input input-sm bg-[#141516] border-[#2a2b2c] text-gray-200 w-full"
                value={overrideSlices}
                onChange={(e) => setOverrideSlices(e.target.value)}
              />
              <span className="text-[11px] text-gray-600">
                Slice size is recomputed deterministically by the backend
                (ceil of total ÷ slices).
              </span>
            </label>
            <div className="flex gap-2">
              <button
                className="btn btn-sm btn-warning flex-1"
                onClick={submitRevision}
                disabled={isRevising || !slicesValid}
              >
                {isRevising ? (
                  <span className="loading loading-spinner loading-xs" />
                ) : (
                  "Re-simulate"
                )}
              </button>
              <button
                className="btn btn-sm btn-ghost"
                onClick={() => setMode("review")}
                disabled={isRevising}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      {/* ── Action bar ──────────────────────────────────────────────────────── */}
      {mode === "review" && !can_approve && approval_blocked_reason && (
        <div className="px-4 py-2 text-xs text-amber-300 border-t border-[#2a2b2c] bg-[#141516]">
          Approval blocked: {approval_blocked_reason}
        </div>
      )}
      {mode === "review" && (
        <div className="flex gap-2 px-4 py-3 border-t border-[#2a2b2c] flex-shrink-0 bg-[#141516]">
          {/* APPROVE — green, primary action. Enabled only when the backend's
              approval gate passes; the backend re-checks it regardless. */}
          <button
            className="btn btn-sm btn-success flex-1"
            onClick={approve}
            disabled={isRevising || !can_approve}
            title={approval_blocked_reason ?? undefined}
          >
            Approve
          </button>

          {/* MODIFY — amber, opens the override form */}
          <button
            className="btn btn-sm btn-warning flex-1"
            onClick={() => setMode("modify")}
            disabled={isRevising}
          >
            Modify
          </button>

          {/* ABORT — muted red, destructive */}
          <button
            className="btn btn-sm btn-error btn-outline flex-1"
            onClick={abort}
            disabled={isRevising}
          >
            Abort
          </button>
        </div>
      )}
    </div>
  );
}
