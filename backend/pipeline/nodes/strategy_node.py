"""
Strategy node — LLM decides HOW to execute the trade.

This is the ONLY node that calls the LLM. It is explicitly constrained by its
system prompt to NOT perform any numerical calculation. The LLM's job is pure
strategy reasoning: which algorithm to use and how to slice the order.

All arithmetic (slippage, market impact, cost) happens downstream in the C++
simulation_node. This is the architectural boundary the whole POC demonstrates.

Input state keys:  trade_request
Output state keys: strategy (incl. origin), errors
"""

import logging
from pydantic import BaseModel, Field
from typing import Literal

from backend.pipeline.plan import MAX_SLICES, plan_from_slice_count, slice_size
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
# deterministically in backend.pipeline.plan. The num_slices bound (1-20) is
# enforced by Pydantic Field validation, not just by the prompt, and the count
# is then normalised in code so it can never exceed what the order can fill.
class StrategyOutput(BaseModel):
    approach: Literal["VWAP", "TWAP", "Sweep", "Iceberg"]
    num_slices: int = Field(ge=1, le=MAX_SLICES)
    # Natural language rationale — shown to the trader in the HITL panel,
    # labelled as narrative. Bounded so a runaway response cannot flood the UI.
    reasoning: str = Field(min_length=1, max_length=2000)


# Kept as a module-level name for backwards compatibility with existing tests.
_slice_size = slice_size


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
Do NOT state slice sizes, prices, slippage, costs or percentages in your reasoning.
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
        if not isinstance(result, StrategyOutput):
            # Some provider/version combinations return a dict — validate it
            # against the same schema rather than trusting its shape.
            result = StrategyOutput.model_validate(result)

    except Exception as exc:
        # Log the detail server-side; keep provider/stack details out of state,
        # which is returned to the browser.
        logger.exception("[strategy_node] LLM call failed")
        errors.append(f"strategy_node: LLM call failed ({type(exc).__name__}) — fallback strategy applied")
        # Fallback: deterministic VWAP default. It is marked origin="fallback",
        # and the HITL gate (backend.pipeline.plan.approval_block_reason)
        # refuses to approve it until a trader confirms the slice count via
        # Modify. Nothing the LLM did not choose and no human confirmed can be
        # dispatched.
        num_slices, shares_per_slice = plan_from_slice_count(trade["total_shares"], 5)
        return {
            "strategy": {
                "approach": "VWAP",
                "num_slices": num_slices,
                "shares_per_slice": shares_per_slice,
                "reasoning": "LLM unavailable. Deterministic fallback: VWAP. Confirm the slice count with Modify before approving.",
                "origin": "fallback",
            },
            "errors": errors,
        }

    # Slice arithmetic is OURS, not the LLM's: the requested count is normalised
    # and the size is ceil division (backend.pipeline.plan).
    num_slices, shares_per_slice = plan_from_slice_count(trade["total_shares"], result.num_slices)

    strategy = {
        "approach":         result.approach,
        "num_slices":       num_slices,
        "shares_per_slice": shares_per_slice,
        "reasoning":        result.reasoning,
        "origin":           "llm",
    }

    logger.info(
        f"[strategy_node] approach={result.approach} requested_slices={result.num_slices} "
        f"plan={num_slices}x{shares_per_slice:,}"
    )
    return {"strategy": strategy, "errors": errors}
