"""
Strategy node — LLM decides HOW to execute the trade.

This is the ONLY node that calls the LLM. It is explicitly constrained by its
system prompt to NOT perform any numerical calculation. The LLM's job is pure
strategy reasoning: which algorithm to use and how to slice the order.

All arithmetic (slippage, market impact, cost) happens downstream in the C++
simulation_node. This is the architectural boundary the whole POC demonstrates.

Input state keys:  trade_request
Output state keys: strategy, errors
"""

import logging
import math
from pydantic import BaseModel, Field
from typing import Literal

from backend.pipeline.state import TradeState

logger = logging.getLogger(__name__)

# ── Pydantic schema for structured LLM output ──────────────────────────────────
# Using .with_structured_output(StrategyOutput) forces the LLM to return valid
# JSON that matches this schema. No regex parsing, no json.loads — Pydantic
# validates the response and raises if the LLM produces invalid output.
#
# DELIBERATE OMISSION: shares_per_slice is NOT in this schema. The LLM chooses
# the approach and how many slices; the division (total_shares / num_slices)
# is arithmetic, and arithmetic never comes from the LLM. It is computed
# deterministically in _slice_size() below. The num_slices bound (1-20) is
# enforced by Pydantic Field validation, not just by the prompt.
class StrategyOutput(BaseModel):
    approach: Literal["VWAP", "TWAP", "Sweep", "Iceberg"]
    num_slices: int = Field(ge=1, le=20)
    reasoning: str   # natural language rationale — shown to the trader in the HITL panel


def _slice_size(total_shares: int, num_slices: int) -> int:
    """Deterministic slice sizing: ceil division so slices always cover the order."""
    return math.ceil(total_shares / num_slices)


# ── System prompt ──────────────────────────────────────────────────────────────
# The CRITICAL constraint: the LLM decides approach and slice COUNT only.
# All arithmetic (slice size, slippage, cost) is computed in code — the slice
# size deterministically in this module, the execution metrics in the C++ core.
_SYSTEM_PROMPT = """You are an algorithmic trading specialist at an institutional hedge fund.
Given a trade request, select the best execution algorithm and slicing plan.

Your response should be a JSON object with:
- approach: one of "VWAP", "TWAP", "Sweep", or "Iceberg"
- num_slices: integer number of child orders (1-20)
- reasoning: 2-4 sentences of plain-English strategic rationale for the portfolio manager

Do NOT calculate or estimate any numerical values beyond choosing the slice count.
Slice sizes, slippage, and costs are computed by a deterministic engine downstream.

Algorithm guide:
- VWAP: volume-weighted average price — distributes order across the session in
  proportion to historical volume. Ideal for large orders where minimizing market
  impact is the priority.
- TWAP: time-weighted average price — splits order evenly across fixed time intervals.
  Predictable and straightforward. Best when execution certainty matters most.
- Sweep: immediate aggressive execution at market. Best when timing is critical,
  such as acting quickly on breaking news or an urgent position change.
- Iceberg: posts only a small visible quantity at a time, refreshing after each fill.
  Ideal for very large orders where concealing total size reduces market impact."""


def run(state: TradeState) -> dict:
    """
    Call the LLM to determine execution strategy for the incoming trade request.

    Returns a partial state dict — LangGraph merges it with the existing state.
    Pattern: try → structured output → fallback.
    """
    errors: list[str] = list(state.get("errors", []))
    trade = state.get("trade_request", {})

    if not trade:
        errors.append("strategy_node: no trade_request in state")
        return {"errors": errors}

    user_prompt = (
        f"Trade request: {trade.get('prompt', '')}\n"
        f"Instrument: {trade.get('instrument', 'UNKNOWN')}\n"
        f"Side: {trade.get('side', 'sell').upper()}\n"
        f"Total shares: {trade.get('total_shares', 0):,}\n"
        f"Deadline: {trade.get('deadline', 'end of day')}\n\n"
        f"Select the execution algorithm and slicing plan for this order."
    )

    try:
        # Imported lazily so this module (and its pure helpers like _slice_size)
        # can be imported in tests/CI without Azure credentials configured.
        from backend.tools.llm_client import get_chat_llm

        llm = get_chat_llm(temperature=0.2)
        # with_structured_output uses function-calling / JSON mode under the hood.
        # The LLM response is validated against StrategyOutput before returning.
        structured_llm = llm.with_structured_output(StrategyOutput)
        result: StrategyOutput = structured_llm.invoke([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ])

        # Slice size is OUR arithmetic, not the LLM's — deterministic ceil division.
        shares_per_slice = _slice_size(trade.get("total_shares", 0), result.num_slices)

        strategy = {
            "approach":        result.approach,
            "num_slices":      result.num_slices,
            "shares_per_slice": shares_per_slice,
            "reasoning":       result.reasoning,
        }

        logger.info(
            f"[strategy_node] approach={result.approach} "
            f"slices={result.num_slices}x{shares_per_slice:,} shares"
        )
        return {"strategy": strategy, "errors": errors}

    except Exception as exc:
        msg = f"strategy_node: LLM call failed — {exc}"
        logger.exception(msg)
        errors.append(msg)
        # Safe fallback: VWAP with conservative slicing
        total = trade.get("total_shares", 0)
        return {
            "strategy": {
                "approach": "VWAP",
                "num_slices": 5,
                "shares_per_slice": _slice_size(total, 5) if total > 0 else 0,
                "reasoning": "[FALLBACK] LLM unavailable. Defaulted to VWAP with 5 equal slices.",
            },
            "errors": errors,
        }
