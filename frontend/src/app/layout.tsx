import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Execution Agent — Trader Dashboard",
  description: "Agentic HITL trading desk: LangGraph strategy + C++20 LOB simulation",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark" className="h-full">
      <body className="flex flex-col h-screen min-h-0 bg-[#141516] text-gray-300">
        {/* ── Header ──────────────────────────────────────────────────────────── */}
        <header className="flex-shrink-0 bg-[#042341] px-4 py-2 flex items-center gap-3 border-b border-[#0a3a6a]">
          {/* Live indicator */}
          <span className="w-2 h-2 rounded-full bg-green-400 pulse-dot" />
          <span className="text-sm font-semibold tracking-wide text-gray-100">
            EXECUTION AGENT
          </span>
          <span className="text-xs text-gray-400 ml-1">
            Agentic HITL Trading Desk — POC
          </span>
          <div className="ml-auto text-xs text-gray-500 font-mono">
            LangGraph · C++20 LOB · Azure OpenAI
          </div>
        </header>

        <main className="flex-1 flex flex-col min-h-0 overflow-hidden">
          {children}
        </main>

        <footer className="flex-shrink-0 bg-[#042341] px-3 py-1 text-xs text-gray-500 text-center border-t border-[#0a3a6a]">
          Execution Agent — internal use only
        </footer>
      </body>
    </html>
  );
}
