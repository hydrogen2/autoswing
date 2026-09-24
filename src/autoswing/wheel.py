"""Wheel book: cash-secured put -> assignment -> covered call, measured.

Origin (2026-09-24): the owner read the Rich Dad framing — sell a put on a
good company that is down, take assignment if it falls, then sell calls until
it bounces — described as "profit on the premium rather than the stock".

That framing is the thing this module exists to test, because it is wrong in
a specific and expensive way. A cash-secured put has almost exactly the
payoff of a covered call at the same strike: maximum gain is the premium,
maximum loss is the strike down to zero. The premium is not income sitting
alongside the stock risk; it IS the compensation for that risk, priced by a
market that does this full time. So the wheel cannot be scored on premium
collected. It is scored against holding the same stock with the same money.

What IS real is the variance risk premium: implied vol tends to print above
subsequent realized vol. But three findings from the first screen (2026-09-24,
53 sub-$49 names, 24 with a tradeable put inside the position cap) set the
rules below:

1. THE BASELINE DECIDES THE ANSWER. Scoring IV against 30-day realized vol
   made 8 names look rich. Against max(RV120, RV252) — which does not assume
   a recent calm patch persists — 21 of those 24 had NEGATIVE premium. A
   short realized-vol window is a cherry-picked yardstick; we never use it.

2. FEAR IS USUALLY PRICED CORRECTLY. The most frightened names on the board
   paid the WORST: the gold miners and the distressed balance sheets all sold
   vol below what they were actually delivering. High IV is a forecast, not a
   mispricing. Screening for "scary" selects toward negative edge.

3. THE SPREAD IS A REAL COST. Bid/ask on these contracts ran 11-30% of mid.
   A vol gap that survives the model can still die on execution, so edge is
   always reported NET of half the spread, in dollars per contract.

Measurement only. Nothing here places an order or touches the gate.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

# --- pre-committed screen thresholds -----------------------------------------
# Written down before any cycle is logged, so a disappointing book cannot be
# rescued later by widening them. Changing one is a deliberate, dated edit.
MIN_OPEN_INTEREST = 500       # contracts; thinner and the quote is theatre
MAX_SPREAD_PCT = 0.22         # (ask-bid)/mid
MIN_VRP = 0.02               # vol points of gap; below this is noise
MAX_FRONT_SLOPE = 0.10        # front IV over back IV => unexplained near event
WARN_FRONT_SLOPE = 0.05
TARGET_OTM = 0.08             # strike ~8% below spot: a real discount to own at
RISK_FREE = 0.04
MIN_N_FOR_VERDICT = 40        # completed cycles before the book says anything

STATUSES = ("csp_open", "csp_expired", "assigned", "cc_open",
            "cc_expired", "called_away")
TERMINAL = ("csp_expired", "called_away")


# --- option maths -------------------------------------------------------------

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put(spot: float, strike: float, dte_days: int, vol: float,
           rate: float = RISK_FREE) -> float:
    """Black-Scholes put value. Used ONLY as a differencing tool: the edge we
    care about is bs_put(iv) - bs_put(realized), i.e. what the market charges
    minus what the stock has actually been delivering. Both legs share every
    other assumption, so the model's flaws largely cancel."""
    if dte_days <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, strike - spot)
    t = dte_days / 365.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    return strike * math.exp(-rate * t) * _ncdf(-d2) - spot * _ncdf(-d1)


def realized_vol(closes: list[float], window: int) -> float | None:
    """Annualised stdev of daily returns. None until there is enough history —
    never guess a vol to sell against."""
    if len(closes) < window + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))][-window:]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def vrp_baseline(rv120: float | None, rv252: float | None) -> float | None:
    """The conservative realized-vol yardstick: the HIGHER of the medium and
    long window. Vol mean-reverts upward out of calm patches, and finding #1
    above is what happens when you forget that."""
    vals = [v for v in (rv120, rv252) if v is not None]
    return max(vals) if vals else None


# --- screening ----------------------------------------------------------------

@dataclass
class PutQuote:
    symbol: str
    spot: float
    strike: float
    bid: float
    ask: float
    expiry: str
    dte: int
    iv: float
    open_interest: int
    rv120: float | None = None
    rv252: float | None = None
    front_slope: float | None = None   # front ATM IV minus ~85d ATM IV
    earnings_date: str | None = None
    note: str = ""


def evaluate_put(q: PutQuote, collateral_cap: float) -> dict:
    """Pre-committed screen. Returns every reason for rejection, not the first
    one, so a near-miss is visible as a near-miss."""
    reasons: list[str] = []
    flags: list[str] = []

    collateral = q.strike * 100
    mid = (q.bid + q.ask) / 2 if (q.bid + q.ask) > 0 else 0.0
    spread_pct = (q.ask - q.bid) / mid if mid > 0 else None
    base = vrp_baseline(q.rv120, q.rv252)
    vrp = round(q.iv - base, 4) if base is not None else None

    if collateral > collateral_cap:
        reasons.append("collateral_over_cap")
    if q.open_interest < MIN_OPEN_INTEREST:
        reasons.append("illiquid_open_interest")
    if spread_pct is None or spread_pct > MAX_SPREAD_PCT:
        reasons.append("spread_too_wide")
    if base is None:
        reasons.append("no_realized_vol_baseline")
    elif vrp is not None and vrp < MIN_VRP:
        reasons.append("negative_or_noise_vrp")
    # Earnings inside the contract is a jump we are not paid separately for,
    # and it is the one risk this system can actually see coming.
    if q.earnings_date and q.expiry and q.earnings_date <= q.expiry:
        reasons.append("earnings_before_expiry")
    if q.front_slope is not None:
        if q.front_slope > MAX_FRONT_SLOPE:
            reasons.append("unexplained_front_event")
        elif q.front_slope > WARN_FRONT_SLOPE:
            flags.append("mild_front_richness")

    # Dollar edge, net of crossing half the spread. This is the number that
    # decides whether a vol gap survives contact with execution.
    edge = None
    if base is not None:
        gross = (bs_put(q.spot, q.strike, q.dte, q.iv)
                 - bs_put(q.spot, q.strike, q.dte, base)) * 100
        slip = (mid - q.bid) * 100 if mid > 0 else 0.0
        edge = round(gross - slip, 2)
        if edge is not None and edge <= 0:
            reasons.append("no_edge_after_spread")

    static_yield = round((q.bid / q.strike) * (365 / q.dte), 4) if q.dte else None
    return {
        "symbol": q.symbol, "expiry": q.expiry, "dte": q.dte,
        "spot": q.spot, "strike": q.strike, "bid": q.bid, "ask": q.ask,
        "collateral": round(collateral, 2),
        "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
        "iv": q.iv, "rv120": q.rv120, "rv252": q.rv252,
        "vrp_baseline": round(base, 4) if base is not None else None,
        "vrp": vrp, "front_slope": q.front_slope,
        "edge_net_usd": edge,
        # Static yield is what sales literature quotes. It assumes the put
        # expires worthless every time, so it is an upper bound and never a
        # ranking key here -- edge_net_usd is.
        "static_annualized_yield": static_yield,
        "net_cost_basis_if_assigned": round(q.strike - q.bid, 4),
        "discount_if_assigned_pct": round(100 * ((q.strike - q.bid) / q.spot - 1), 2),
        "earnings_date": q.earnings_date,
        "passes": not reasons, "reasons": reasons, "flags": flags,
    }


def screen(quotes: list[PutQuote], collateral_cap: float) -> dict:
    results = [evaluate_put(q, collateral_cap) for q in quotes]
    passing = [r for r in results if r["passes"]]
    passing.sort(key=lambda r: r["edge_net_usd"] or 0, reverse=True)
    tally: dict[str, int] = {}
    for r in results:
        for reason in r["reasons"]:
            tally[reason] = tally.get(reason, 0) + 1
    return {"evaluated": len(results), "passing": len(passing),
            "candidates": passing, "rejection_tally": dict(sorted(tally.items())),
            "all": results}


# --- the book -----------------------------------------------------------------

@dataclass
class Cycle:
    """One wheel cycle: the put, then whatever it turns into."""
    id: str
    symbol: str
    status: str
    opened: str                    # date the put was sold
    strike: float
    expiry: str
    premium_received: float = 0.0  # per share, cumulative across legs
    spot_at_open: float = 0.0      # the buy-and-hold benchmark entry
    collateral: float = 0.0
    contracts: int = 1
    shares: int = 0
    call_strike: float | None = None
    call_expiry: str | None = None
    closed: str | None = None
    exit_price: float | None = None
    legs: list = field(default_factory=list)
    note: str = ""


def cycle_id(symbol: str, opened: str, strike: float) -> str:
    return f"{symbol.upper()}-{opened}-{strike:g}p"


def validate_cycle(payload: dict) -> list[str]:
    errs = []
    if not str(payload.get("symbol", "")).strip():
        errs.append("symbol required")
    for f in ("opened", "expiry"):
        try:
            date.fromisoformat(str(payload.get(f, "")))
        except ValueError:
            errs.append(f"{f} must be YYYY-MM-DD")
    for f in ("strike", "premium_received", "spot_at_open"):
        try:
            if float(payload.get(f, -1)) <= 0:
                errs.append(f"{f} must be > 0")
        except (TypeError, ValueError):
            errs.append(f"{f} must be numeric")
    # The benchmark entry is not optional: without it the cycle can only ever
    # report premium collected, which is exactly the misleading number.
    if not payload.get("spot_at_open"):
        errs.append("spot_at_open required — the buy-and-hold benchmark entry")
    if not str(payload.get("note", "")).strip():
        errs.append("note required — why this name is worth owning at the strike")
    return errs


def advance(cycle: dict, event: str, on: str, price: float | None = None,
            premium: float | None = None, strike: float | None = None,
            expiry: str | None = None) -> dict:
    """Move a cycle through the state machine. Every transition is explicit;
    nothing is inferred from a price series, because inferring assignment from
    a daily low is exactly the shadow-fill bug this project has already paid
    for once."""
    st = cycle["status"]
    c = dict(cycle)
    leg = {"event": event, "on": on, "price": price, "premium": premium}

    if event == "expire_worthless" and st == "csp_open":
        c["status"] = "csp_expired"
        c["closed"] = on
        c["exit_price"] = price
    elif event == "assign" and st == "csp_open":
        c["status"] = "assigned"
        c["shares"] = 100 * c.get("contracts", 1)
    elif event == "sell_call" and st in ("assigned", "cc_expired"):
        if premium is None or strike is None or expiry is None:
            raise ValueError("sell_call needs premium, strike and expiry")
        c["status"] = "cc_open"
        c["premium_received"] = round(c.get("premium_received", 0.0) + premium, 4)
        c["call_strike"] = strike
        c["call_expiry"] = expiry
    elif event == "expire_worthless" and st == "cc_open":
        c["status"] = "cc_expired"
    elif event == "call_away" and st == "cc_open":
        c["status"] = "called_away"
        c["closed"] = on
        c["exit_price"] = c.get("call_strike")
    else:
        raise ValueError(f"illegal transition {event!r} from status {st!r}")

    c["legs"] = list(c.get("legs", [])) + [leg]
    return c


def cycle_pnl(cycle: dict, mark: float | None = None) -> dict:
    """P&L for a cycle, and the only comparison that matters beside it:
    the same collateral put into the stock at spot_at_open.

    mark is required for cycles still holding shares; a terminal cycle
    ignores it."""
    contracts = cycle.get("contracts", 1)
    shares = 100 * contracts
    prem = cycle.get("premium_received", 0.0) * shares
    strike = cycle["strike"]
    spot0 = cycle["spot_at_open"]
    collateral = strike * shares

    st = cycle["status"]
    if st == "csp_expired":
        stock_pnl = 0.0
        exit_ref = cycle.get("exit_price") or spot0
    elif st == "called_away":
        stock_pnl = (cycle["call_strike"] - strike) * shares
        exit_ref = cycle["call_strike"]
    else:
        if mark is None:
            return {"pending": True, "reason": "mark required for open cycle"}
        stock_pnl = (mark - strike) * shares if cycle.get("shares") else 0.0
        exit_ref = mark

    total = round(prem + stock_pnl, 2)
    # Benchmark: same money, same start, just own the stock.
    hold_shares = collateral / spot0 if spot0 else 0.0
    hold_pnl = round(hold_shares * (exit_ref - spot0), 2)
    # And the risk-free alternative, since the collateral is parked either way.
    days = 0
    try:
        if cycle.get("closed"):
            days = (date.fromisoformat(cycle["closed"])
                    - date.fromisoformat(cycle["opened"])).days
    except ValueError:
        days = 0
    cash_pnl = round(collateral * RISK_FREE * days / 365.0, 2)

    return {
        "pending": False, "id": cycle["id"], "symbol": cycle["symbol"],
        "status": st, "premium_usd": round(prem, 2), "stock_pnl_usd": round(stock_pnl, 2),
        "total_pnl_usd": total, "collateral_usd": round(collateral, 2),
        "return_on_collateral_pct": round(100 * total / collateral, 3) if collateral else None,
        "hold_pnl_usd": hold_pnl,
        "vs_hold_usd": round(total - hold_pnl, 2),
        "cash_pnl_usd": cash_pnl,
        "vs_cash_usd": round(total - cash_pnl, 2),
        "days_held": days,
    }


def score_book(cycles: list[dict], marks: dict | None = None) -> dict:
    """Scoreboard. Reports vs_hold first and premium last, deliberately."""
    marks = marks or {}
    scored, pending = [], 0
    for c in cycles:
        r = cycle_pnl(c, marks.get(c["symbol"]))
        if r.get("pending"):
            pending += 1
        else:
            scored.append(r)

    closed = [r for r in scored if r["status"] in TERMINAL]
    n = len(closed)
    out = {"n_cycles": len(cycles), "n_closed": n, "n_pending_mark": pending,
           "min_n_for_verdict": MIN_N_FOR_VERDICT, "rows": scored}
    if not n:
        out["verdict"] = "no closed cycles yet"
        return out

    tot = sum(r["total_pnl_usd"] for r in closed)
    vs_hold = sum(r["vs_hold_usd"] for r in closed)
    vs_cash = sum(r["vs_cash_usd"] for r in closed)
    out.update({
        "total_pnl_usd": round(tot, 2),
        "vs_hold_usd": round(vs_hold, 2),
        "vs_cash_usd": round(vs_cash, 2),
        "beat_hold": sum(1 for r in closed if r["vs_hold_usd"] > 0),
        "beat_hold_rate": round(sum(1 for r in closed if r["vs_hold_usd"] > 0) / n, 3),
        "assigned_rate": round(
            sum(1 for r in closed if r["status"] == "called_away") / n, 3),
        "premium_usd": round(sum(r["premium_usd"] for r in closed), 2),
        "worst_cycle_usd": min(r["total_pnl_usd"] for r in closed),
    })
    # The payout shape is the whole risk here: many small wins then one large
    # loss. A hit rate above 50% means nothing on its own, so refuse a verdict
    # until there are enough cycles to have seen a bad one.
    out["verdict"] = (
        f"n={n} < {MIN_N_FOR_VERDICT}: too few cycles. Short wheel samples "
        "flatter the strategy because the loss arrives rarely and all at once."
        if n < MIN_N_FOR_VERDICT else
        f"vs buy-and-hold: {vs_hold:+.2f} USD over {n} cycles")
    return out


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))


def append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")
