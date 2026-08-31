"""Rules-based trim framework: managing a concentrated position in a
high-volatility name WITHOUT predicting anything.

Origin (2026-08-31): the owner wanted to 做波段 a held winner (CRDO) to drive
its cost basis down. Testing the premise first showed the premise was wrong —
CRDO's daily returns have no measurable mean reversion (lag-1 autocorrelation
-0.011 against a +/-0.089 noise band), forward returns after up-moves are
POSITIVE, and the one significant-looking cell failed Bonferroni, failed
split-sample, and reversed sign on peers. So this module deliberately does
NOT try to time swings.

What it does instead: treat "take risk off a winner" as a position-sizing
problem with pre-committed rules, and measure every rule against the only
benchmark that matters — buy-and-hold of the same stock.

Honest framing, because the two are easily confused: lowering cost basis and
maximising return are DIFFERENT objectives, and on a rising stock they
conflict. Selling is the mechanism that lowers cost basis, so a rule can
reach "negative cost basis" while badly trailing buy-and-hold. Every result
here is reported as vs_hold_pct for that reason.

No look-ahead: every rule sees only bars strictly before the decision bar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class TrimResult:
    rule: str
    symbol: str
    hold_value: float          # buy & hold terminal value of the same shares
    final_value: float         # strategy terminal value (shares + cash)
    vs_hold_pct: float         # the number that decides anything
    max_drawdown_pct: float    # on the strategy equity curve
    hold_max_drawdown_pct: float
    trades: int
    ending_shares: int
    start_shares: int
    # TWO different "cost basis" numbers, because conflating them is the
    # misconception this whole book exists to test:
    #   avg_cost_basis     — accounting/tax sense: average cost of the shares
    #                        still held. Selling at average cost does NOT
    #                        move it, so it is largely flat no matter how
    #                        much you trim.
    #   net_cost_basis     — the 做波段 sense: (total paid - total received)
    #                        / remaining shares. THIS is what goes to zero or
    #                        negative, and it is just a restatement of "I have
    #                        taken more cash out than I put in". It says
    #                        nothing about whether you have more money than
    #                        someone who simply held — vs_hold_pct says that.
    avg_cost_basis: float | None
    net_cost_basis: float | None
    equity_curve: list = field(default_factory=list, repr=False)


def _max_dd(curve: list[float]) -> float:
    peak, dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        dd = max(dd, 1 - v / peak) if peak else dd
    return round(100 * dd, 2)


def _realized_vol(closes: list[float], lookback: int) -> float | None:
    """Annualised stdev of daily returns over the trailing window. None until
    there is enough history — never guess a vol to size against."""
    if len(closes) < lookback + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - lookback, len(closes))]
    n = len(rets)
    m = sum(rets) / n
    var = sum((r - m) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(252)


# -- rules ---------------------------------------------------------------------
# Each rule maps (history so far, state) -> TARGET share count for tomorrow.
# History excludes the bar being decided, so no rule can see its own outcome.

def rule_hold(hist, st, p):
    return st["start_shares"]


def rule_vol_target(hist, st, p):
    """Hold a constant RISK contribution rather than a constant share count.
    As realised vol rises the position shrinks automatically; as it falls it
    rebuilds. Requires no forecast — the trim is a consequence of measured
    volatility. (Moreira & Muir 2017 find volatility-managed positions raise
    risk-adjusted returns; this is the single-name version.)"""
    vol = _realized_vol(hist, p.get("lookback", 20))
    if vol is None or vol <= 0:
        return st["shares"]
    w = min(p.get("max_weight", 1.0), p.get("target_vol", 0.60) / vol)
    return int(round(st["start_shares"] * w))


def rule_scale_out(hist, st, p):
    """Pre-committed ladder: sell a fixed slice of the ORIGINAL position each
    time price first closes above a gain level. Never buys back. This is the
    disciplined version of what most people do by feel."""
    gain = hist[-1] / st["entry"] - 1
    sold = 0.0
    for level, frac in p.get("levels", [(0.25, 0.2), (0.50, 0.2), (1.00, 0.2)]):
        if gain >= level:
            sold += frac
    return int(round(st["start_shares"] * max(0.0, 1 - sold)))


def rule_trail_stop(hist, st, p):
    """Ride the trend, exit on a defined break from the peak, re-enter only
    after price reclaims the peak. Participates in upside; the cost is
    whipsaw in choppy tape."""
    peak = st["peak"]
    px = hist[-1]
    if st["shares"] > 0 and px <= peak * (1 - p.get("trail", 0.25)):
        return 0
    if st["shares"] == 0 and px >= peak:
        return st["start_shares"]
    return st["shares"]


def rule_band(hist, st, p):
    """Classic 做波段 for comparison: trim a slice after a rise, buy it back
    after a fall. Included because it is the intuition being tested, not
    because the evidence supports it."""
    px = hist[-1]
    ref = st["ref"]
    if px >= ref * (1 + p.get("up", 0.25)) and st["shares"] > 0:
        st["ref"] = px
        return max(0, st["shares"] - int(round(st["start_shares"] * p.get("slice", 0.25))))
    if px <= ref * (1 - p.get("down", 0.15)) and st["shares"] < st["start_shares"]:
        st["ref"] = px
        return min(st["start_shares"],
                   st["shares"] + int(round(st["start_shares"] * p.get("slice", 0.25))))
    return st["shares"]


RULES = {
    "hold": (rule_hold, {}),
    "vol_target_60": (rule_vol_target, {"target_vol": 0.60, "lookback": 20}),
    "vol_target_80": (rule_vol_target, {"target_vol": 0.80, "lookback": 20}),
    "scale_out_ladder": (rule_scale_out, {"levels": [(0.25, 0.2), (0.50, 0.2), (1.00, 0.2)]}),
    "trail_25pct": (rule_trail_stop, {"trail": 0.25}),
    "trail_35pct": (rule_trail_stop, {"trail": 0.35}),
    "band_25_15": (rule_band, {"up": 0.25, "down": 0.15, "slice": 0.25}),
}


def simulate(symbol: str, closes: list[float], rule: str,
             start_shares: int = 100, cost_per_trade: float = 0.0) -> TrimResult:
    """Walk the series once, applying the rule with no look-ahead."""
    fn, params = RULES[rule]
    st = {"shares": start_shares, "start_shares": start_shares,
          "entry": closes[0], "ref": closes[0], "peak": closes[0],
          "cash": 0.0, "basis": closes[0] * start_shares,
          "paid": closes[0] * start_shares, "received": 0.0}
    trades = 0
    curve = []
    for i in range(1, len(closes)):
        hist = closes[:i]              # strictly before today's bar
        st["peak"] = max(st["peak"], hist[-1])
        target = fn(hist, st, params)
        target = max(0, min(st["start_shares"], int(target)))
        if target != st["shares"]:
            px = closes[i]             # act at today's price, decided on prior data
            delta = target - st["shares"]
            st["cash"] -= delta * px + abs(delta) * cost_per_trade
            if delta > 0:
                st["basis"] += delta * px
                st["paid"] += delta * px
            else:
                st["basis"] += delta * (st["basis"] / st["shares"] if st["shares"] else px)
                st["received"] += -delta * px
            st["shares"] = target
            trades += 1
        curve.append(st["shares"] * closes[i] + st["cash"])

    hold_curve = [start_shares * p for p in closes[1:]]
    final = curve[-1]
    hold = hold_curve[-1]
    return TrimResult(
        rule=rule, symbol=symbol,
        hold_value=round(hold, 2), final_value=round(final, 2),
        vs_hold_pct=round(100 * (final / hold - 1), 2) if hold else 0.0,
        max_drawdown_pct=_max_dd(curve), hold_max_drawdown_pct=_max_dd(hold_curve),
        trades=trades, ending_shares=st["shares"], start_shares=start_shares,
        avg_cost_basis=round(st["basis"] / st["shares"], 2) if st["shares"] else None,
        net_cost_basis=round((st["paid"] - st["received"]) / st["shares"], 2)
        if st["shares"] else None,
        equity_curve=curve,
    )
