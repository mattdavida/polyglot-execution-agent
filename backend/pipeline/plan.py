"""
Deterministic execution-plan rules — the single place slice arithmetic lives.

Both sources of slice parameters go through this module:
  - the LLM (strategy_node): chooses an approach and a slice COUNT only
  - the trader (Modify / revise): may change the slice count OR the slice size

Neither source is trusted to keep the plan consistent. The rules here are
pure functions with no I/O, so the API layer (to reject bad input with 422
before the graph runs) and the graph nodes (as defence in depth) apply exactly
the same checks.

Invariant enforced for every plan that can reach the HITL panel:
    1 <= num_slices <= MAX_SLICES
    num_slices == ceil(total_shares / shares_per_slice)
i.e. the slices cover the whole order and the last slice is never empty.
"""

from __future__ import annotations

import math
from typing import Any, Optional

MAX_SLICES = 20

# Keys a trader override may set. Anything else (approach, reasoning, origin…)
# is rejected: the LLM's rationale and the strategy label are not editable
# through the override path, so the audit record cannot be rewritten.
OVERRIDE_KEYS = frozenset({"num_slices", "shares_per_slice"})


def slice_size(total_shares: int, num_slices: int) -> int:
    """Deterministic slice sizing: ceil division so slices always cover the order."""
    if num_slices <= 0:
        raise ValueError("num_slices must be >= 1")
    return math.ceil(total_shares / num_slices)


def _is_int(value: Any) -> bool:
    # bool is a subclass of int in Python — reject it explicitly.
    return isinstance(value, int) and not isinstance(value, bool)


def plan_from_slice_count(total_shares: int, num_slices: int) -> tuple[int, int]:
    """
    Turn a requested slice count into a consistent (num_slices, shares_per_slice).

    The LLM's count is bounded 1..MAX_SLICES by Pydantic, but it can still ask
    for more slices than the order can fill (8 slices for 3 lots, or 6 slices of
    2 for a 10-lot order, whose 6th slice would be empty). The count is
    normalised deterministically rather than trusted:
        shares_per_slice = ceil(total / requested)
        num_slices       = ceil(total / shares_per_slice)
    """
    if not _is_int(total_shares) or total_shares <= 0:
        raise ValueError("total_shares must be a positive integer")
    requested = max(1, min(num_slices, MAX_SLICES))
    size = slice_size(total_shares, requested)
    return math.ceil(total_shares / size), size


def apply_override(strategy: dict, override: dict, total_shares: int) -> dict:
    """
    Apply a trader override to a strategy and return a NEW, consistent strategy.

    Accepted overrides:
      {"num_slices": n}              → shares_per_slice recomputed (and n
                                       normalised so no slice is empty)
      {"shares_per_slice": s}        → num_slices = ceil(total / s); the last
                                       slice carries the remainder
      {"num_slices": n, "shares_per_slice": s}
                                     → accepted only if n == ceil(total / s)

    Raises ValueError with a human-readable reason for anything else.
    """
    if not _is_int(total_shares) or total_shares <= 0:
        raise ValueError("trade has no valid total_shares")
    if not isinstance(override, dict) or not override:
        raise ValueError("override must set num_slices and/or shares_per_slice")

    unknown = set(override) - OVERRIDE_KEYS
    if unknown:
        raise ValueError(f"override may not set: {', '.join(sorted(unknown))}")

    n: Optional[int] = override.get("num_slices")
    s: Optional[int] = override.get("shares_per_slice")

    if n is not None:
        if not _is_int(n) or not 1 <= n <= MAX_SLICES:
            raise ValueError(f"num_slices must be an integer 1-{MAX_SLICES}")
        if n > total_shares:
            raise ValueError(f"num_slices ({n}) cannot exceed total_shares ({total_shares})")
    if s is not None:
        if not _is_int(s) or s < 1:
            raise ValueError("shares_per_slice must be a positive integer")
        if s > total_shares:
            raise ValueError(f"shares_per_slice ({s}) cannot exceed total_shares ({total_shares})")

    if n is not None and s is None:
        n, s = plan_from_slice_count(total_shares, n)
    elif s is not None and n is None:
        n = math.ceil(total_shares / s)
        if n > MAX_SLICES:
            raise ValueError(
                f"shares_per_slice {s} needs {n} slices for {total_shares}; max is {MAX_SLICES}"
            )
    else:
        expected = math.ceil(total_shares / s)
        if n != expected:
            raise ValueError(
                f"inconsistent override: {n} slices x {s} does not match "
                f"{total_shares} total (needs {expected} slices at that size)"
            )

    updated = dict(strategy or {})
    updated["num_slices"] = n
    updated["shares_per_slice"] = s
    updated["origin"] = "trader"
    return updated


def approval_block_reason(state: dict) -> Optional[str]:
    """
    Return why the current state may NOT be approved, or None if it may.

    This is the HITL gate. It is evaluated by the API before resuming and again
    by the graph router, so neither layer alone can dispatch on incomplete
    numbers.
    """
    strategy = state.get("strategy") or {}
    if not strategy:
        return "No strategy has been formulated for this trade."
    if strategy.get("origin") == "fallback":
        return (
            "Strategy is a fallback default (LLM unavailable). Confirm the slice "
            "count with Modify before approving."
        )
    if state.get("simulation_error"):
        return f"C++ simulation failed for the current plan: {state['simulation_error']}"
    metrics = state.get("slippage_metrics")
    if not metrics:
        return "No C++ metrics exist for the current plan. Revise or abort."
    if metrics.get("for_revision") != state.get("revision_count", 0):
        return "C++ metrics are stale (computed for an earlier revision). Revise or abort."
    return None
