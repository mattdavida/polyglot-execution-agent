"use client";

import { useState } from "react";
import type { TradeRequestBody } from "@/types";

interface Props {
  onSubmit: (trade: TradeRequestBody) => void;
  disabled: boolean;
}

const INSTRUMENT_GROUPS = [
  {
    label: "Equities",
    options: ["TSLA", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "JPM", "GS", "SPY", "QQQ"],
  },
  {
    label: "Futures — Rates",
    options: ["ZN", "ZB", "ZF", "ZT"],
  },
  {
    label: "Futures — Equity Index",
    options: ["ES", "NQ", "RTY", "YM"],
  },
  {
    label: "Futures — Commodities",
    options: ["CL", "GC", "SI", "NG"],
  },
  {
    label: "FX",
    options: ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"],
  },
];

const CUSTOM_VALUE = "__custom__";

export default function TradeForm({ onSubmit, disabled }: Props) {
  const [instrument,    setInstrument]    = useState("ZN");
  const [isCustom,      setIsCustom]      = useState(false);
  const [customTicker,  setCustomTicker]  = useState("");
  const [side,          setSide]          = useState<"buy" | "sell">("sell");
  const [totalShares,   setTotalShares]   = useState("200");
  const [deadline,      setDeadline]      = useState("end of day");
  const [prompt,        setPrompt]        = useState("");

  function handleSelectChange(value: string) {
    if (value === CUSTOM_VALUE) {
      setIsCustom(true);
      setInstrument("");
    } else {
      setIsCustom(false);
      setCustomTicker("");
      setInstrument(value);
    }
  }

  function handleCustomChange(value: string) {
    const upper = value.toUpperCase();
    setCustomTicker(upper);
    setInstrument(upper);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const shares = parseInt(totalShares.replace(/,/g, ""), 10);
    if (isNaN(shares) || shares <= 0) return;
    if (!instrument.trim()) return;

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
          value={isCustom ? CUSTOM_VALUE : instrument}
          onChange={(e) => handleSelectChange(e.target.value)}
          disabled={disabled}
          required={!isCustom}
        >
          {INSTRUMENT_GROUPS.map((group) => (
            <optgroup key={group.label} label={group.label}>
              {group.options.map((ticker) => (
                <option key={ticker} value={ticker}>{ticker}</option>
              ))}
            </optgroup>
          ))}
          <optgroup label="Other">
            <option value={CUSTOM_VALUE}>Custom ticker…</option>
          </optgroup>
        </select>
        {isCustom && (
          <input
            type="text"
            className="input input-sm bg-[#141516] border-[#2a2b2c] text-gray-200 w-full mt-1"
            value={customTicker}
            onChange={(e) => handleCustomChange(e.target.value)}
            placeholder="Enter ticker"
            disabled={disabled}
            required
            autoFocus
          />
        )}
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
        <span className="text-xs text-gray-500">Total Shares</span>
        <input
          type="text"
          className="input input-sm bg-[#141516] border-[#2a2b2c] text-gray-200 w-full"
          value={totalShares}
          onChange={(e) => setTotalShares(e.target.value)}
          placeholder="50000"
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
          placeholder="e.g. Reduce position ahead of earnings. Factory delay news warrants urgency."
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
