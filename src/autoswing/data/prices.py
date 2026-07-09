"""Price-reaction metrics from yfinance history.

Everything here is derived from a plain OHLCV DataFrame so the math is
unit-testable with synthetic data; only fetch_history() touches the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass
class Reaction:
    symbol: str
    reaction_date: str
    prior_close: float
    gap_pct: float            # reaction-day open vs prior close
    move_pct: float           # reaction-day close vs prior close
    drift_since_pct: float    # latest close vs reaction-day close
    volume_ratio: float       # reaction-day volume vs 20d average
    adv_dollar_20d: float     # avg daily dollar volume, 20 sessions pre-report
    last_close: float
    days_since_reaction: int  # trading days


def fetch_history(symbols: list[str], period: str = "3mo") -> dict[str, pd.DataFrame]:
    """Batch-download OHLCV per symbol. Missing/empty symbols are dropped."""
    import yfinance as yf

    if not symbols:
        return {}
    data = yf.download(
        symbols, period=period, group_by="ticker", auto_adjust=True,
        threads=True, progress=False,
    )
    out = {}
    for sym in symbols:
        try:
            df = data[sym] if len(symbols) > 1 else data
        except KeyError:
            continue
        df = df.dropna(subset=["Close"])
        if len(df):
            out[sym] = df
    return out


def reaction_metrics(
    symbol: str, df: pd.DataFrame, report_date: date, timing: str
) -> Reaction | None:
    """Compute the post-report reaction. Returns None when the reaction
    day isn't in the data yet (e.g. after-close report, market not open)."""
    dates = [d.date() for d in df.index]

    if timing == "bmo":
        candidates = [i for i, d in enumerate(dates) if d >= report_date]
    elif timing == "amc":
        candidates = [i for i, d in enumerate(dates) if d > report_date]
    else:
        # Timing unknown: reaction is whichever of D / D+1 moved more.
        on = [i for i, d in enumerate(dates) if d >= report_date]
        after = [i for i, d in enumerate(dates) if d > report_date]
        if not on:
            return None
        if not after or after[0] == on[0]:
            candidates = on
        else:
            i_on, i_after = on[0], after[0]
            if i_on == 0:
                return None
            move = lambda i: abs(
                df["Close"].iloc[i] / df["Close"].iloc[i - 1] - 1
            )
            candidates = [i_on if move(i_on) >= move(i_after) else i_after]

    if not candidates:
        return None
    idx = candidates[0]
    if idx == 0:
        return None  # no prior close to react against

    prior_close = float(df["Close"].iloc[idx - 1])
    r_open = float(df["Open"].iloc[idx])
    r_close = float(df["Close"].iloc[idx])
    r_volume = float(df["Volume"].iloc[idx])

    pre = df.iloc[max(0, idx - 20):idx]
    avg_volume = float(pre["Volume"].mean()) if len(pre) else 0.0
    adv_dollar = float((pre["Close"] * pre["Volume"]).mean()) if len(pre) else 0.0

    last_close = float(df["Close"].iloc[-1])
    return Reaction(
        symbol=symbol,
        reaction_date=dates[idx].isoformat(),
        prior_close=round(prior_close, 4),
        gap_pct=round(100 * (r_open / prior_close - 1), 2),
        move_pct=round(100 * (r_close / prior_close - 1), 2),
        drift_since_pct=round(100 * (last_close / r_close - 1), 2),
        volume_ratio=round(r_volume / avg_volume, 2) if avg_volume else 0.0,
        adv_dollar_20d=round(adv_dollar, 0),
        last_close=round(last_close, 4),
        days_since_reaction=len(df) - 1 - idx,
    )
