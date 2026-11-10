"""Hedged-book shadow line: the live book's daily returns with a fixed index
short held against it. Measurement only -- no orders, no gate change.

Owner question 2026-11-10: the main book is concentrated in one sector and
autoswing is meant to diversify it, but over its first four months the bot
still carried about half the market's moves (beta to VOO ~0.5, and on the
worst index days much of the loss came through). Should live money carry an
index hedge? This line answers by measuring forward, at no cost and with no
capital at risk, what the book would have looked like with one.

REGISTERED 2026-11-10, before the first forward session:
  - HEDGE_RATIO = 0.5 of equity short the benchmark (VOO), every day. It was
    read from the 2026-07-24..2026-11-09 record and is NEVER re-fit: a ratio
    tuned to the window it is scored on proves nothing.
  - hedged day return = bot day return - 0.5 x benchmark day return, both
    taken from consecutive rows of state/benchmark.jsonl (equity and
    benchmark sampled in the same run), compounded. That is a short
    rebalanced to half of equity at every mark.
  - in_sample is the window the ratio came from: shown for reference, NOT
    evidence. forward starts from the 2026-11-09 mark and is the only part
    that can inform anything.
  - No pass bar on purpose. A hedge is insurance, not alpha: on the
    in-sample record it gave up return for lower volatility. The decision it
    informs -- whether live money carries an index hedge -- is the owner's,
    read at the 100-trade verdict alongside the rest of the record.
Not modelled, and all three cost the hedge a little: dividends owed on the
short (about half the fund's yield a year at this ratio), borrow/financing,
and the trading cost of rebalancing.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

HEDGE_REGISTERED = "2026-11-10"
HEDGE_RATIO = 0.5
HEDGE_INSAMPLE_BASE = "2026-07-24"   # returns AFTER this mark ...
HEDGE_FORWARD_BASE = "2026-11-09"    # ... through this one are in-sample
STRESS_DAY_PCT = -1.0                # "bad day for the index"
MIN_DAYS_FOR_BETA = 20

DEFINITION = ("each day: bot return - 0.5 x VOO return, compounded (a short "
              "rebalanced to half of equity at every mark)")
NOT_MODELLED = [
    "dividends owed on the short (about half the fund's yield a year)",
    "borrow and financing on the short",
    "trading cost of rebalancing the hedge",
]


def load_marks(path: Path) -> list[dict]:
    """Benchmark rows usable for returns: one per date (last wins), sorted,
    with a positive equity and benchmark close."""
    if not path.exists():
        return []
    by_date: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        eq, bench = row.get("virtual_equity"), row.get("benchmark_close")
        if (isinstance(eq, (int, float)) and isinstance(bench, (int, float))
                and eq > 0 and bench > 0 and row.get("date")):
            by_date[row["date"]] = row
    return [by_date[d] for d in sorted(by_date)]


def daily_returns(marks: list[dict]) -> list[tuple[str, float, float]]:
    """(date, bot return, benchmark return) between consecutive marks, as
    fractions. A missing day folds into the next mark for BOTH series, so
    the two stay aligned."""
    out = []
    for prev, cur in zip(marks, marks[1:]):
        out.append((cur["date"],
                    cur["virtual_equity"] / prev["virtual_equity"] - 1,
                    cur["benchmark_close"] / prev["benchmark_close"] - 1))
    return out


def _series_stats(rs: list[float]) -> dict:
    equity = peak = 1.0
    max_dd = 0.0
    for r in rs:
        equity *= 1 + r
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
    vol = None
    if len(rs) >= 2:
        m = sum(rs) / len(rs)
        vol = math.sqrt(sum((r - m) ** 2 for r in rs) / (len(rs) - 1) * 252)
    return {
        "return_pct": round(100 * (equity - 1), 2),
        "ann_vol_pct": round(100 * vol, 1) if vol is not None else None,
        "max_drawdown_pct": round(100 * max_dd, 2),
        "worst_day_pct": round(100 * min(rs), 2),
    }


def _beta_corr(bot: list[float], bench: list[float]):
    n = len(bot)
    if n < MIN_DAYS_FOR_BETA:
        return None, None
    mb, mx = sum(bot) / n, sum(bench) / n
    cov = sum((b - mb) * (x - mx) for b, x in zip(bot, bench))
    var_x = sum((x - mx) ** 2 for x in bench)
    var_b = sum((b - mb) ** 2 for b in bot)
    if not var_x or not var_b:
        return None, None
    return round(cov / var_x, 2), round(cov / math.sqrt(var_x * var_b), 2)


def window_stats(returns: list[tuple[str, float, float]], ratio: float) -> dict:
    """Unhedged vs hedged vs benchmark over one window of daily returns."""
    if not returns:
        return {"n_days": 0}
    bot = [b for _, b, _ in returns]
    bench = [x for _, _, x in returns]
    hedged = [b - ratio * x for b, x in zip(bot, bench)]
    beta, corr = _beta_corr(bot, bench)
    stress = [i for i, x in enumerate(bench) if 100 * x <= STRESS_DAY_PCT]

    def avg(rs):
        return round(100 * sum(rs[i] for i in stress) / len(stress), 2)

    return {
        "n_days": len(returns),
        "through": returns[-1][0],
        "unhedged": _series_stats(bot),
        "hedged": _series_stats(hedged),
        "benchmark": _series_stats(bench),
        "realized_beta": beta,           # of the UNHEDGED book; descriptive,
        "realized_corr": corr,           # never used to move the ratio
        "benchmark_down_days": {
            "threshold_pct": STRESS_DAY_PCT, "n": len(stress),
            **({"unhedged_avg_pct": avg(bot), "hedged_avg_pct": avg(hedged),
                "benchmark_avg_pct": avg(bench)} if stress else {}),
        },
    }


def hedged_book(benchmark_path: Path) -> dict:
    returns = daily_returns(load_marks(benchmark_path))
    in_sample = [r for r in returns
                 if HEDGE_INSAMPLE_BASE < r[0] <= HEDGE_FORWARD_BASE]
    forward = [r for r in returns if r[0] > HEDGE_FORWARD_BASE]
    fwd = {"from_mark": HEDGE_FORWARD_BASE,
           **window_stats(forward, HEDGE_RATIO)}
    if not forward:
        fwd["note"] = "no forward session marked yet"
    return {
        "registered": HEDGE_REGISTERED,
        "ratio": HEDGE_RATIO,
        "definition": DEFINITION,
        "status": "measurement only: no orders, no gate change, no pass "
                  "bar, nothing for the brain to act on. The ratio is never "
                  "re-fit. Informs the owner's hedge decision at the "
                  "100-trade verdict.",
        "not_modelled": NOT_MODELLED,
        "forward": fwd,
        "in_sample": {
            "from_mark": HEDGE_INSAMPLE_BASE,
            "note": "the ratio was read from this window: reference only, "
                    "NOT evidence",
            **window_stats(in_sample, HEDGE_RATIO),
        },
    }


def hedged_book_safe(benchmark_path: Path) -> dict:
    """For shadow-status: a broken benchmark file must not take the v2 and
    wide verdicts down with it, and must not look like a quiet day either."""
    try:
        return hedged_book(benchmark_path)
    except Exception as exc:  # noqa: BLE001 -- reported, never swallowed
        return {"registered": HEDGE_REGISTERED, "ratio": HEDGE_RATIO,
                "status": "ERROR: hedged line could not be computed",
                "error": f"{type(exc).__name__}: {exc}"}
