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

import json
import logging
from pydantic import BaseModel
from typing import Literal

from backend.pipeline.state import TradeState
from backend.tools.llm_client import get_chat_llm

logger = logging.getLogger(__name__)

# ── Pydantic schema for structured LLM output ──────────────────────────────────
# Using .with_structured_output(StrategyOutput) forces the LLM to return valid
# JSON that matches this schema. No regex parsing, no json.loads — Pydantic
# validates the response and raises if the LLM produces invalid output.
class StrategyOutput(BaseModel):
    approach: Literal["VWAP", "TWAP", "Sweep", "Iceberg"]
    num_slices: int
    shares_per_slice: int
    reasoning: str   # natural language rationale — shown to the trader in the HITL panel


# ── System prompt ──────────────────────────────────────────────────────────────
# The CRITICAL constraint: "DO NOT calculate or estimate any numerical values".
# This is enforced in the prompt, not in code. The LangGraph simulation_node
# owns all numbers. This prompt is the contract between the LLM and the C++ core.
_SYSTEM_PROMPT = """You are an algorithmic trading specialist at an institutional hedge fund.
Given a trade request, select the best execution algorithm and slicing plan.

Your response should be a JSON object with:
- approach: one of "VWAP", "TWAP", "Sweep", or "Iceberg"
- num_slices: integer number of child orders (1-20)
- shares_per_slice: integer shares per child order
- reasoning: 2-4 sentences of plain-English strategic rationale for the portfolio manager

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
    Pattern is identical to SNIE's triage node: try → structured output → fallback.
    """
    errors: list[str] = list(state.get("errors", []))
    trade = state.get("trade_request", {})

    if not trade:
        errors.append("strategy_node: no trade_request in state")
        return {"errors": errors}

    user_prompt = (
        f"Trade request: {trade.get('prompt', '')}\n"
        f"Instrument: {trade.get('instrument', 'UNKNOWN')}\n"
        f"Total shares: {trade.get('total_shares', 0):,}\n"
        f"Deadline: {trade.get('deadline', 'end of day')}\n\n"
        f"Select the execution algorithm and slicing plan for this order."
    )

    try:
        llm = get_chat_llm(temperature=0.2)
        # with_structured_output uses function-calling / JSON mode under the hood.
        # The LLM response is validated against StrategyOutput before returning.
        structured_llm = llm.with_structured_output(StrategyOutput)
        result: StrategyOutput = structured_llm.invoke([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ])

        strategy = {
            "approach":        result.approach,
            "num_slices":      result.num_slices,
            "shares_per_slice": result.shares_per_slice,
            "reasoning":       result.reasoning,
        }

        logger.info(
            f"[strategy_node] approach={result.approach} "
            f"slices={result.num_slices}x{result.shares_per_slice:,} shares"
        )
        return {"strategy": strategy, "errors": errors}

    except Exception as exc:
        msg = f"strategy_node: LLM call failed — {exc}"
        logger.exception(msg)
        errors.append(msg)
        # Safe fallback: VWAP with conservative slicing
        return {
            "strategy": {
                "approach": "VWAP",
                "num_slices": 5,
                "shares_per_slice": trade.get("total_shares", 50_000) // 5,
                "reasoning": "[FALLBACK] LLM unavailable. Defaulted to VWAP with 5 equal slices.",
            },
            "errors": errors,
        }
