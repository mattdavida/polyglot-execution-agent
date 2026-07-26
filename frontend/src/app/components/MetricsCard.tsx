"use client";

import type { SlippageMetrics } from "@/types";

// Colour-code slippage based on industry thresholds:
//   < 5 bps  → green (acceptable)
//   5-15 bps → amber (elevated)
//   > 15 bps → red   (high cost, trader should review)
function bpsClass(bps: number): string {
  if (bps < 5)  return "bps-good";
  if (bps < 15) return "bps-warn";
  return "bps-bad";
}

interface MetricRowProps {
  label:     string;
  value:     string;
  className?: string;
  mono?:     boolean;
}

function MetricRow({ label, value, className, mono }: MetricRowProps) {
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-[#2a2b2c] last:border-0">
      <span className="text-xs text-gray-500">{label}</span>
      <span className={`text-sm font-medium ${mono ? "font-mono" : ""} ${className ?? "text-gray-200"}`}>
        {value}
      </span>
    </div>
  );
}

interface Props {
  metrics: SlippageMetrics;
}

export default function MetricsCard({ metrics }: Props) {
  return (
    <div className="bg-[#1c1d1e] rounded border border-[#2a2b2c] p-4">
      {/* Card header — surface that this is deterministic C++ output */}
      <div className="flex items-center gap-2 mb-3">
        <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
          C++ LOB Simulation
        </span>
        <span className="badge badge-xs badge-success">deterministic</span>
      </div>

      <MetricRow
        label="Avg Fill Price"
        value={`$${metrics.avg_fill_price.toFixed(4)}`}
        mono
      />
      <MetricRow
        label="Slippage"
        value={`${metrics.slippage_bps.toFixed(2)} bps`}
        className={bpsClass(metrics.slippage_bps)}
        mono
      />
      <MetricRow
        label="Market Impact"
        value={`${metrics.market_impact_bps.toFixed(2)} bps`}
        className={bpsClass(metrics.market_impact_bps)}
        mono
      />
      <MetricRow
        label="Total Cost"
        value={`$${metrics.total_cost_usd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
        mono
      />
      <MetricRow
        label="Simulation Latency"
        value={
          metrics.simulation_latency_us > 0
            ? `${metrics.simulation_latency_us} µs`
            : "< 1 µs"
        }
        className="text-green-400"
        mono
      />
    </div>
  );
}
