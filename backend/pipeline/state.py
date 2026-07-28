"""
LangGraph shared state TypedDict for the execution agent.
Wired into the StateGraph in Phase 3.
"""

from typing import Literal, Optional
from typing_extensions import TypedDict


class TradeRequest(TypedDict):
    prompt: str
    instrument: str       # human-readable for the LLM; converted to uint32 at the C++ boundary
    side: Literal["buy", "sell"]  # order direction — a liquidation is a sell
    total_shares: int
    deadline: str


class Strategy(TypedDict):
    approach: Literal["VWAP", "TWAP", "Sweep", "Iceberg"]
    num_slices: int
    shares_per_slice: int  # computed deterministically in Python — never by the LLM
    reasoning: str         # LLM natural language — the human-readable rationale


class SlippageMetrics(TypedDict):
    avg_fill_price: float         # in the book's price units (e.g. raw CME ticks for ZN)
    slippage_bps: float           # adverse move vs arrival price (positive = worse)
    market_impact_bps: float
    total_cost_usd: float         # converted from price units via instrument tick metadata
    total_filled: int             # contracts actually filled by the simulated sweep
    fill_ratio: float             # total_filled / order_size — < 1.0 means partial fill
    simulation_latency_us: int    # microseconds — surfaced in the UI as the demo proof point


class HumanFeedback(TypedDict):
    action: Literal["approve", "revise", "abort"]
    override_params: Optional[dict]   # e.g. {"num_slices": 3, "shares_per_slice": 16700}


class TradeState(TypedDict):
    trade_request: TradeRequest
    strategy: Optional[Strategy]
    slippage_metrics: Optional[SlippageMetrics]
    human_feedback: Optional[HumanFeedback]
    revision_count: int
    errors: list[str]
    thread_id: str
