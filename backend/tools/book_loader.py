"""
ZN Level 2 Order Book Loader.

Reconstructs a realistic L2 order book from an external tick dataset
(ZN = 10-Year Treasury Note Futures, CME Globex).

CSV format (no header):
    timestamp, price (CME raw ticks), type (T/B/A), quantity (contracts)

    T = executed Trade  — used for validation only, not book building
    B = Bid level update — sets qty at that price on the bid side
    A = Ask level update — sets qty at that price on the ask side

Book reconstruction logic:
    Each B/A row REPLACES the quantity at that price level (last-write-wins).
    qty == 0 means the level is cancelled — it is removed from the book.
    This is the standard CME incremental refresh model.

Why real data matters here:
    The dummy book in simulation_node.py had a perfectly uniform 7-level
    structure that never caused meaningful slippage for reasonable order sizes.
    The ZN book has:
      - Irregular level depths (real institutional order clustering)
      - A tight but real bid-ask spread (~0.5 ticks = ~$7.81/contract)
      - Levels that disappear mid-session (realistic liquidity conditions)
    Sweeping this book produces non-trivial, defensible slippage numbers.
"""

import csv
import logging
import pathlib
from datetime import time
from functools import lru_cache

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_CSV_PATH = (
    pathlib.Path(__file__).parent.parent.parent   # repo root
    / "data"
    / "2016_12_23.csv"
)

# Only consider ticks within a rolling window ending at the snapshot time.
# Using a narrow window (10 minutes) avoids the stale-level staleness problem:
# incremental feeds don't always cancel old levels explicitly when the market
# moves, so accumulating hours of ticks creates phantom depth.
# 10 minutes is wide enough to see realistic multi-level depth build up,
# but narrow enough that stale levels from earlier in the session are excluded.
DEFAULT_SNAPSHOT_TIME  = time(9, 30)   # end of the window
DEFAULT_WINDOW_MINUTES = 10            # only look back this far

# Must match C++ MAX_LEVELS = 10
MAX_LEVELS = 10

# ZN instrument metadata — used for UI display AND for converting the C++
# engine's price-unit outputs into real currency. In production this is the
# firm's instrument reference data; here it is the single source of truth
# for the units contract at the Python/C++ boundary.
ZN_METADATA = {
    "symbol":       "ZN",
    "description":  "10-Year Treasury Note Futures (CME)",
    "tick_size":    0.5,          # minimum price increment (in the book's price units)
    "tick_value":   15.625,       # USD value per tick per contract
    "price_unit":   "CME ticks",  # raw CME integer tick format
}


def price_units_to_usd(cost_price_units: float) -> float:
    """
    Convert an adverse cost expressed in the book's raw price units
    (summed over contracts) into US dollars.

    The C++ engine returns total_cost = |avg_fill - arrival| * filled, in
    price units. Dollars = (price units / tick_size) ticks * tick_value $/tick.
    """
    return (cost_price_units / ZN_METADATA["tick_size"]) * ZN_METADATA["tick_value"]


# ── Loader ────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=4)
def load_zn_book(
    snapshot_time:  time = DEFAULT_SNAPSHOT_TIME,
    window_minutes: int  = DEFAULT_WINDOW_MINUTES,
    max_levels:     int  = MAX_LEVELS,
) -> tuple[list[tuple[float, int]], list[tuple[float, int]]]:
    """
    Parse the ZN tick CSV and return a Level 2 order book snapshot.

    Uses a rolling time window ending at snapshot_time. Only ticks within the
    window are included, avoiding the stale-level problem that arises when
    accumulating many hours of incremental updates from a feed that doesn't
    always send explicit level cancellations.

    Results are cached via lru_cache — the file is only parsed once per
    unique argument combination across the application lifetime.

    Args:
        snapshot_time:  End of the observation window.
        window_minutes: How many minutes before snapshot_time to include.
                        Default 10 — wide enough for depth, narrow for freshness.
        max_levels:     Maximum levels to return per side.

    Returns:
        (asks, bids) where each is a list of (price, qty) tuples:
          asks: sorted ascending  (best ask = lowest price first)
          bids: sorted descending (best bid = highest price first)

    Raises:
        FileNotFoundError: if 2016_12_23.csv is not in data/.
        ValueError: if no B/A ticks were found in the window.
    """
    if not _CSV_PATH.exists():
        raise FileNotFoundError(
            f"ZN CSV not found at {_CSV_PATH}.\n"
            f"Expected: execution_agent/data/2016_12_23.csv"
        )

    # Compute the start of the window
    snap_minutes_total = snapshot_time.hour * 60 + snapshot_time.minute
    start_minutes_total = snap_minutes_total - window_minutes
    window_start = time(
        start_minutes_total // 60,
        start_minutes_total % 60,
    )

    # Running book within the window: price → qty (last-write-wins)
    bids: dict[float, int] = {}
    asks: dict[float, int] = {}
    last_trade_price: float | None = None
    rows_read = 0

    with open(_CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 4:
                continue

            try:
                price     = float(row[1])
                tick_type = row[2].strip()
                qty       = int(row[3])
            except (ValueError, IndexError):
                continue

            if price == 0.0:
                continue

            # Parse time — format: 2016-12-23T01:00:00.000
            try:
                time_part = row[0].split("T")[1]
                h, m, s   = time_part.split(":")
                tick_time = time(int(h), int(m), int(s.split(".")[0]))
            except (ValueError, IndexError):
                continue

            # Skip ticks before the window opens
            if tick_time < window_start:
                continue

            # Stop once we've passed the snapshot cutoff
            if tick_time > snapshot_time:
                break

            # Track the last trade price — used as the market anchor below
            if tick_type == "T" and qty > 0:
                last_trade_price = price

            # Apply the update: qty=0 cancels a level, qty>0 sets/replaces it
            if tick_type == "B":
                if qty == 0:
                    bids.pop(price, None)
                else:
                    bids[price] = qty
            elif tick_type == "A":
                if qty == 0:
                    asks.pop(price, None)
                else:
                    asks[price] = qty

            rows_read += 1

    if not bids and not asks:
        raise ValueError(
            f"No B/A ticks found in window [{window_start}-{snapshot_time}]. "
            f"Try a larger window_minutes or different snapshot_time."
        )

    # ── Anchor to last trade price to avoid crossed-book artifact ─────────────
    # This incremental feed format doesn't send explicit level cancellations
    # when the market moves. As a result, old bid/ask levels from the start of the
    # window linger and cross the current market. We anchor by using the last
    # traded price as the boundary:
    #   valid bids  = B levels at or below the last trade price
    #   valid asks  = A levels at or above the last trade price
    # This mirrors how a real order book works: bids can't be above where the
    # last trade executed, and asks can't be below it.
    if last_trade_price is not None:
        bids = {p: q for p, q in bids.items() if p <= last_trade_price}
        asks = {p: q for p, q in asks.items() if p >= last_trade_price}
        logger.debug(f"[book_loader] anchored to last trade @ {last_trade_price}")

    # Sort and cap each side
    ask_list = sorted(asks.items(), key=lambda x: x[0])[:max_levels]
    bid_list = sorted(bids.items(), key=lambda x: x[0], reverse=True)[:max_levels]

    spread = ask_list[0][0] - bid_list[0][0] if ask_list and bid_list else 0
    logger.info(
        f"[book_loader] ZN window [{window_start}-{snapshot_time}] — "
        f"{len(bid_list)} bid levels, {len(ask_list)} ask levels — "
        f"best bid {bid_list[0][0]}@{bid_list[0][1]}  "
        f"best ask {ask_list[0][0]}@{ask_list[0][1]}  "
        f"spread {spread:.1f} ticks — "
        f"({rows_read:,} ticks processed)"
    )

    return ask_list, bid_list
