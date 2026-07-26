"use client";

import type { Strategy } from "@/types";

// Colour badge per algorithm so the trader can instantly identify strategy type
const APPROACH_COLORS: Record<Strategy["approach"], string> = {
  VWAP:    "badge-info",
  TWAP:    "badge-primary",
  Sweep:   "badge-warning",
  Iceberg: "badge-secondary",
};

interface Props {
  strategy:      Strategy;
  revisionCount: number;
}

export default function StrategyCard({ strategy, revisionCount }: Props) {
  return (
    <div className="bg-[#1c1d1e] rounded border border-[#2a2b2c] p-4">
      {/* Card header */}
      <div className="flex items-center gap-2 mb-3">
        <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
          LLM Strategy
        </span>
        {revisionCount > 0 && (
          <span className="badge badge-xs badge-warning">
            Revision {revisionCount}
          </span>
        )}
      </div>

      {/* Algorithm + slicing */}
      <div className="flex items-center gap-3 mb-3">
        <span className={`badge badge-sm ${APPROACH_COLORS[strategy.approach]}`}>
          {strategy.approach}
        </span>
        <span className="text-sm text-gray-300 font-mono">
          {strategy.num_slices} slices × {strategy.shares_per_slice.toLocaleString()} shares
        </span>
      </div>

      {/* LLM reasoning — shown verbatim so trader can evaluate quality */}
      <div className="text-xs text-gray-400 leading-relaxed border-t border-[#2a2b2c] pt-3">
        <span className="text-gray-500 block mb-1">Reasoning</span>
        {strategy.reasoning}
      </div>
    </div>
  );
}
