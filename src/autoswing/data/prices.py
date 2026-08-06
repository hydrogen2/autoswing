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


def _download_batch(symbols: list[str], period: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    # Yahoo spells share classes with dashes (BRK.B -> BRK-B); translate for
    # the fetch, key results by the caller's original symbol.
    yahoo = {sym: sym.replace(".", "-") for sym in symbols}
    data = yf.download(
        list(yahoo.values()), period=period, group_by="ticker",
        auto_adjust=True, threads=True, progress=False,
    )
    out = {}
    for sym in symbols:
        try:
            df = (data[yahoo[sym]]
                  if isinstance(data.columns, pd.MultiIndex) else data)
        except KeyError:
            continue
        df = df.dropna(subset=["Close"])
        if len(df):
            out[sym] = df
    return out


# A partial miss is the yfinance flake; a wholesale miss is a real outage and
# retrying it just burns the scan's watchdog budget.
_RETRY_MAX_MISSING_FRACTION = 0.25


def fetch_history(symbols: list[str], period: str = "3mo") -> dict[str, pd.DataFrame]:
    """Batch-download OHLCV per symbol. Missing/empty symbols are dropped.

    The batch download drops a varying handful of symbols per call, so a
    partial miss is retried once — otherwise a qualifying candidate vanishes
    from the scan by luck of the draw (observed 08-05: six identical scans
    returned 81/54/54/54/34/54 passing).
    """
    if not symbols:
        return {}
    out = _download_batch(symbols, period)
    missing = [s for s in symbols if s not in out]
    if missing and len(missing) <= _RETRY_MAX_MISSING_FRACTION * len(symbols):
        out.update(_download_batch(missing, period))
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
        if not on:
            return None
        i_on = on[0]
        if dates[i_on] > report_date:
            # Report is dated on a non-trading day, so D never traded: the
            # first session on or after it reacts to the news either way.
            candidates = [i_on]
        else:
            after = [i for i, d in enumerate(dates) if d > report_date]
            if not after:
                # D traded but D+1 hasn't yet. With timing unknown the report
                # may have landed after D's close, which would make D's move a
                # pre-news run-up rather than confirmation — so we cannot judge
                # the reaction yet. Wait for the next session.
                return None
            if i_on == 0:
                return None
            i_after = after[0]
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
