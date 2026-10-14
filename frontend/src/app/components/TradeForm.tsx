"use client";

import { useState } from "react";
import type { TradeRequestBody } from "@/types";

interface Props {
  onSubmit: (trade: TradeRequestBody) => void;
  disabled: boolean;
}

// Only instruments with a book source and instrument metadata in the backend
// registry (backend/tools/book_loader.INSTRUMENTS). The backend rejects
// anything else with 422 — this list is a convenience, not the control.
const INSTRUMENTS = ["ZN"] as const;

export default function TradeForm({ onSubmit, disabled }: Props) {
  const [instrument,    setInstrument]    = useState<string>(INSTRUMENTS[0]);
  const [side,          setSide]          = useState<"buy" | "sell">("sell");
  const [totalShares,   setTotalShares]   = useState("200");
  const [deadline,      setDeadline]      = useState("end of day");
  const [prompt,        setPrompt]        = useState("");

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const shares = Number(totalShares.replace(/,/g, ""));
    if (!Number.isInteger(shares) || shares <= 0) return;

    // Build the natural language prompt from fields if trader left it blank.
    // Pre-fills give the LLM enough context even with minimal input.
    const fullPrompt =
      prompt.trim() ||
      `${side === "sell" ? "Sell" : "Buy"} ${shares.toLocaleString()} ${instrument} by ${deadline}.`;

    onSubmit({ prompt: fullPrompt, instrument, side, total_shares: shares, deadline });
  }

  return (
    <form onSubmit={handleSubmit} className="bg-[#1c1d1e] rounded border border-[#2a2b2c] p-4 flex flex-col gap-4">
      <div className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-1">
        New Trade Request
      </div>

      {/* Instrument */}
      <div className="flex flex-col gap-1">
        <span className="text-xs text-gray-500">Instrument</span>
        <select
          className="select select-sm bg-[#141516] border-[#2a2b2c] text-gray-200 w-full"
          value={instrument}
          onChange={(e) => setInstrument(e.target.value)}
          disabled={disabled}
          required
        >
          {INSTRUMENTS.map((ticker) => (
            <option key={ticker} value={ticker}>{ticker} — 10Y T-Note futures</option>
          ))}
        </select>
        <span className="text-[11px] text-gray-600">
          One instrument in this POC: the engine only has a ZN book.
        </span>
      </div>

      {/* Side — explicit direction. The engine sweeps bids for a sell
          (liquidation) and asks for a buy; never inferred from free text. */}
      <div className="flex flex-col gap-1">
        <span className="text-xs text-gray-500">Side</span>
        <div className="join w-full">
          <button
            type="button"
            className={`join-item btn btn-sm flex-1 ${side === "sell" ? "btn-error" : "btn-ghost border-[#2a2b2c]"}`}
            onClick={() => setSide("sell")}
            disabled={disabled}
          >
            Sell
          </button>
          <button
            type="button"
            className={`join-item btn btn-sm flex-1 ${side === "buy" ? "btn-success" : "btn-ghost border-[#2a2b2c]"}`}
            onClick={() => setSide("buy")}
            disabled={disabled}
          >
            Buy
          </button>
        </div>
      </div>

      {/* Total shares */}
      <label className="flex flex-col gap-1">
        <span className="text-xs text-gray-500">Total Contracts</span>
        <input
          type="text"
          className="input input-sm bg-[#141516] border-[#2a2b2c] text-gray-200 w-full"
          value={totalShares}
          onChange={(e) => setTotalShares(e.target.value)}
          placeholder="200"
          disabled={disabled}
          required
        />
      </label>

      {/* Deadline */}
      <label className="flex flex-col gap-1">
        <span className="text-xs text-gray-500">Deadline</span>
        <select
          className="select select-sm bg-[#141516] border-[#2a2b2c] text-gray-200 w-full"
          value={deadline}
          onChange={(e) => setDeadline(e.target.value)}
          disabled={disabled}
        >
          <option value="end of day">End of Day</option>
          <option value="1 hour">Within 1 hour</option>
          <option value="30 minutes">Within 30 minutes</option>
          <option value="immediately">Immediately (urgent)</option>
        </select>
      </label>

      {/* Optional rationale — becomes the LLM prompt */}
      <label className="flex flex-col gap-1">
        <span className="text-xs text-gray-500">
          Rationale / Context{" "}
          <span className="text-gray-600">(optional — feeds the LLM)</span>
        </span>
        <textarea
          className="textarea textarea-sm bg-[#141516] border-[#2a2b2c] text-gray-200 w-full text-sm resize-none"
          rows={3}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          maxLength={2000}
          placeholder="e.g. Reduce duration ahead of CPI. Urgency is moderate."
          disabled={disabled}
        />
      </label>

      <button
        type="submit"
        className="btn btn-sm btn-primary w-full"
        disabled={disabled}
      >
        {disabled ? (
          <span className="loading loading-spinner loading-xs" />
        ) : (
          "Submit Trade"
        )}
      </button>
    </form>
  );
}
