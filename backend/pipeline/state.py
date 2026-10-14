"""
LangGraph shared state TypedDict for the execution agent.
"""

import operator
from typing import Annotated, Literal, Optional
from typing_extensions import TypedDict


class TradeRequest(TypedDict):
    prompt: str
    instrument: str       # validated against the instrument registry at the API (ZN only today)
    side: Literal["buy", "sell"]  # order direction — a liquidation is a sell
    total_shares: int
    deadline: str


class Strategy(TypedDict):
    approach: Literal["VWAP", "TWAP", "Sweep", "Iceberg"]
    num_slices: int
    shares_per_slice: int  # computed deterministically in Python — never by the LLM
    reasoning: str         # LLM natural language — the human-readable rationale
    # Who chose the slice parameters now in force:
    #   "llm"      — LLM picked the count, Python computed the size
    #   "fallback" — LLM unavailable; deterministic default (approval blocked until a trader confirms)
    #   "trader"   — a trader override (Modify) replaced the slice parameters
    origin: Literal["llm", "fallback", "trader"]


class SlippageMetrics(TypedDict):
    avg_fill_price: float         # in the book's price units (see book_loader.ZN_METADATA)
    slippage_bps: float           # adverse move vs arrival price (positive = worse)
    market_impact_bps: float
    total_cost_usd: float         # PER SLICE — converted from price units via instrument tick metadata
    total_filled: int             # contracts actually filled by the simulated sweep
    fill_ratio: float             # total_filled / order_size — < 1.0 means partial fill
    simulation_latency_ns: int    # wall-clock time of the C++ sweep, nanoseconds
    for_revision: int             # revision_count these numbers were computed for (staleness guard)


class HumanFeedback(TypedDict):
    action: Literal["approve", "revise", "abort"]
    override_params: Optional[dict]   # e.g. {"num_slices": 3} — validated by backend.pipeline.plan


class TradeState(TypedDict):
    trade_request: TradeRequest
    strategy: Optional[Strategy]
    slippage_metrics: Optional[SlippageMetrics]
    simulation_error: Optional[str]       # set when the C++ run for the CURRENT plan failed
    simulation_warnings: list[str]        # current-plan warnings (partial fill, fallback book)
    human_feedback: Optional[HumanFeedback]
    revision_count: int
    errors: list[str]                     # cumulative log of node errors (diagnostic)
    # Append-only: one entry per trader decision, in order. The reducer
    # (operator.add) means nodes can only append, never rewrite history.
    decision_log: Annotated[list[dict], operator.add]
    thread_id: str
