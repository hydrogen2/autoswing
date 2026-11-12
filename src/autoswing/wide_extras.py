"""Wide ledger, second registered question: do the EXTRAS earn their keep?

The first wide verdict (shadow.wide_verdict, registered 2026-10-14) landed
`no_case` on 2026-11-11. It compared the wide and live versions of the SAME
trades, which share ticker, stop and target, so it could only ever measure
simulated fills against real ones. The owner's reframing (2026-11-12): the
question a wider book actually poses is whether the names the live book had
no room for do as well as the names it took. Both groups are read inside the
wide ledger -- same fixed notional, same simulated fills -- so fill drift
cancels out and what is left is the selection.

Registered 2026-11-12, BEFORE any position that can enter the sample was
opened. Definitions are fixed here so none can be chosen after the fact:

  - Sample: wide positions OPENED on or after 2026-11-12, in ledger (close)
    order. The first 40 closes and the nine positions open at registration
    are excluded for good: their outcomes or running marks had been seen.
  - Every sample row is classed from the JOURNAL, never from the row's own
    strategy tag or rationale (both are free text the brain writes):
        taken        a live pead-v1 entry was approved AND filled on the day
                     the wide position opened;
        extra        no live entry was approved for the symbol at any point
                     in the wide hold (capacity kept it out);
        unfilled     a live entry was approved but never filled -- EXCLUDED.
                     A limit that never fills selects for runaway winners:
                     in the first 40 the two such rows (DELL, CBRL) averaged
                     +14.8% alpha and had been read as "extras";
        taken_later  the live book entered (approved and filled) on a later
                     day of the hold (HTHT, 08-17/08-18) -- EXCLUDED, it is
                     neither group;
        unclassified the journal holds no wide shadow.proposal for the row's
                     opening day -- EXCLUDED and a defect. Absence of
                     evidence must never read as "extra".
  - The verdict lands when the 20th extra closes. The taken comparator is
    every taken close up to that ledger row, minimum 20. Nothing after that
    row ever counts.
  - Measure: average alpha per trade (return minus the benchmark over the
    same hold), because the two groups trade on different days.
  - wider_book_discussion: extras' total P&L > 0, avg alpha > 0 and avg
    alpha >= taken's, and all three still hold with the extras' best trade
    removed (best P&L for the dollar test, best alpha for the alpha tests).
  - extras_lag: taken's avg alpha beats extras' by >= 2.0pp, and still does
    with taken's best trade removed.
  - anything else: no_case.

A verdict is a DISCUSSION trigger for the owner and nothing more. Book
breadth lives in config/config.yaml (max entries per day, open positions,
gross exposure), which is human-only; nothing changes automatically.

Limitations, stated up front: an extra is a name the brain SAYS it would
have entered -- no order disciplined that call. And at the ledger's ~7.5pp
per-trade alpha spread, 20 extras resolve the gap to roughly +/-2pp, so
only a large difference can show.

RATIFIED by the owner 2026-11-12, the day it was registered, with the
sample at 0 extras / 0 taken and no sample position yet opened. The
ratification covers the bar above and the entry-price mechanic shipped the
same morning (c51039b: wide virtual entries fill at a fresh price, falling
back to the delayed broker quote and saying so). CLOSED to revision;
changing any of this is a dated, deliberate edit.

Nothing here is shared with shadow.py's verdicts on purpose: each
registration is its own frozen artifact.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

WIDE_EXTRAS_REGISTERED = "2026-11-12"
WIDE_EXTRAS_SAMPLE_FROM = "2026-11-12"
WIDE_EXTRAS_N = 20
WIDE_EXTRAS_MIN_TAKEN = 20
WIDE_EXTRAS_MARGIN_PP = 2.0
WIDE_EXTRAS_MIN_ALPHA_COVERAGE = 0.8
WIDE_EXTRAS_RULE = (
    "second wide period: wide positions opened on or after 2026-11-12, in "
    "ledger order, each classed from the journal as taken (live entry "
    "approved and filled the day it opened), extra (no live entry approved "
    "during the hold), or excluded (unfilled, taken_later, unclassified); "
    "lands at the 20th extra close against every taken close up to that "
    "row (min 20); wider_book_discussion only if extras' total P&L > 0, "
    "avg alpha > 0 and avg alpha >= taken's, all surviving the removal of "
    "the extras' best trade; extras_lag if taken's avg alpha leads by >= "
    "2.0pp with taken's best trade removed; otherwise no_case")
WIDE_EXTRAS_LIMITATION = (
    "an extra is a name the brain says it would have entered; no order "
    "disciplined that call. 20 extras resolve the gap to about +/-2pp")

_LIVE_STRATEGIES = (None, "", "pead-v1")
_NOT_ENTRIES = ("healthcheck", "sizing sanity check")
_KEYS = ('"gate.decision"', '"broker.place_bracket_order"',
         '"broker.recent_fills"', '"shadow.proposal"')


def wide_entry_evidence(journal_dir: Path,
                        since: str = WIDE_EXTRAS_SAMPLE_FROM) -> dict:
    """What the journal proves about each (SYMBOL, day), from `since` on:

    approved  a live, non-dry-run pead-v1 gate.decision was approved
    filled    a live buy filled (entry order Filled at placement, or a BOT
              row in broker.recent_fills dated that day)
    logged    a wide shadow.proposal opened a virtual position

    A malformed line raises: a silently skipped approval would turn a taken
    trade into an "extra"."""
    approved, filled, logged = set(), set(), set()
    for f in sorted(Path(journal_dir).glob("*.jsonl")):
        if f.stem < since:
            continue
        for line in f.read_text().splitlines():
            if not any(k in line for k in _KEYS):
                continue
            e = json.loads(line)
            ev = e.get("event")
            day = str(e.get("ts", ""))[:10]
            if ev == "gate.decision":
                if e.get("dry_run") or not (e.get("decision") or {}).get("approved"):
                    continue
                p = e.get("proposal") or {}
                if p.get("strategy") not in _LIVE_STRATEGIES:
                    continue
                if p.get("rationale") in _NOT_ENTRIES or not p.get("symbol"):
                    continue
                approved.add((p["symbol"].upper(), day))
            elif ev == "broker.place_bracket_order":
                r = e.get("result") or {}
                if r.get("symbol") and any(
                        o.get("role") == "entry" and o.get("status") == "Filled"
                        for o in r.get("orders", [])):
                    filled.add((str(r["symbol"]).upper(), day))
            elif ev == "broker.recent_fills":
                for fill in e.get("result") or []:
                    if fill.get("side") == "BOT" and fill.get("symbol"):
                        filled.add((fill["symbol"].upper(),
                                    str(fill.get("time", ""))[:10]))
            elif ev == "shadow.proposal":
                r = e.get("result") or {}
                p = e.get("proposal") or {}
                if r.get("wide") and r.get("opened_virtual") and p.get("symbol"):
                    logged.add((p["symbol"].upper(), day))
    return {"approved": approved, "filled": filled, "logged": logged}


def classify_wide_row(row: dict, evidence: dict) -> str:
    """taken | extra | unfilled | taken_later | unclassified (see module doc).

    A fill with no approved pead-v1 entry in the hold (a wheel assignment in
    the same name) is not an entry and does not make a row "taken"."""
    sym = str(row.get("symbol", "")).upper()
    d0 = row.get("opened") or ""
    d1 = row.get("closed") or d0
    if (sym, d0) not in evidence["logged"]:
        return "unclassified"
    approved = {d for s, d in evidence["approved"] if s == sym and d0 <= d <= d1}
    filled = {d for s, d in evidence["filled"] if s == sym}
    if not approved:
        return "extra"
    # Entry orders are DAY orders (since 2026-09-29): an approval and a fill
    # belong to the same entry only when they share a day.
    entered = approved & filled
    if d0 in entered:
        return "taken"
    if entered:
        return "taken_later"
    return "unfilled"


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _total(rows: list[dict]) -> float:
    return round(sum(r["pnl"] for r in rows), 2)


def _avg_alpha(rows: list[dict]) -> float | None:
    a = [r["alpha_pct"] for r in rows if _is_num(r.get("alpha_pct"))]
    return round(sum(a) / len(a), 3) if a else None


def _drop_best(rows: list[dict], key) -> list[dict]:
    if not rows:
        return rows
    best = max(range(len(rows)), key=lambda i: key(rows[i]))
    return rows[:best] + rows[best + 1:]


def _group(rows: list[dict]) -> dict:
    rets = [100 * (r["exit_price"] / r["entry_price"] - 1) for r in rows
            if _is_num(r.get("entry_price")) and r["entry_price"]
            and _is_num(r.get("exit_price"))]
    return {
        "n": len(rows),
        "wins": sum(1 for r in rows if r["pnl"] > 0),
        "total_pnl": _total(rows),
        "avg_return_pct": round(sum(rets) / len(rets), 3) if rets else None,
        "avg_alpha_pct": _avg_alpha(rows),
        "alpha_n": sum(1 for r in rows if _is_num(r.get("alpha_pct"))),
    }


def _listed(rows: list[dict]) -> dict:
    return {"n": len(rows), "total_pnl": _total(rows),
            "rows": [{"symbol": r.get("symbol"), "opened": r.get("opened"),
                      "closed": r.get("closed"), "pnl": r["pnl"],
                      "alpha_pct": r.get("alpha_pct")} for r in rows]}


def wide_extras_verdict(wide_rows: list[dict], evidence: dict) -> dict:
    """The pre-registered extras-vs-taken verdict. Below 20 extras it reports
    progress only; the numbers are shown so the trajectory is visible, but
    they are labelled as not-a-result."""
    period = [r for r in wide_rows
              if (r.get("opened") or "") >= WIDE_EXTRAS_SAMPLE_FROM]
    classed = [(r, classify_wide_row(r, evidence)) for r in period]

    cut, seen = None, 0
    for i, (_, c) in enumerate(classed):
        if c == "extra":
            seen += 1
            if seen == WIDE_EXTRAS_N:
                cut = i
                break
    sample = classed if cut is None else classed[:cut + 1]
    by = {k: [r for r, c in sample if c == k]
          for k in ("taken", "extra", "unfilled", "taken_later", "unclassified")}
    extras, taken = by["extra"], by["taken"]
    ge, gt = _group(extras), _group(taken)
    ge["symbols"] = [r.get("symbol") for r in extras]

    out = {
        "rule": WIDE_EXTRAS_RULE,
        "registered": WIDE_EXTRAS_REGISTERED,
        "sample_from": WIDE_EXTRAS_SAMPLE_FROM,
        "required_extras": WIDE_EXTRAS_N,
        "min_taken": WIDE_EXTRAS_MIN_TAKEN,
        "margin_pp": WIDE_EXTRAS_MARGIN_PP,
        "closed_total": len(wide_rows),
        "period_closed": len(period),
        "extras": ge,
        "taken": gt,
        "excluded": {
            "opened_before_registration": len(wide_rows) - len(period),
            "unfilled": _listed(by["unfilled"]),
            "taken_later": _listed(by["taken_later"]),
            "unclassified": _listed(by["unclassified"]),
            "after_landing": len(classed) - len(sample),
        },
        "limitation": WIDE_EXTRAS_LIMITATION,
    }
    if by["unclassified"]:
        out["defect"] = ("unclassified rows: the journal has no wide "
                         "shadow.proposal for their opening day -- triage, "
                         "never count them as extras")

    if cut is None:
        out.update(verdict="pending", remaining=WIDE_EXTRAS_N - ge["n"],
                   note="progress, not a result: no verdict before the "
                        f"{WIDE_EXTRAS_N}th extra closes")
        return out

    if gt["n"] < WIDE_EXTRAS_MIN_TAKEN:
        out.update(verdict="undeterminable",
                   reasons=[f"{gt['n']} taken closes < {WIDE_EXTRAS_MIN_TAKEN}"])
        return out
    thin = [name for name, g in (("extras", ge), ("taken", gt))
            if g["alpha_n"] < math.ceil(WIDE_EXTRAS_MIN_ALPHA_COVERAGE * g["n"])]
    if thin:
        out.update(verdict="undeterminable",
                   reasons=[f"alpha missing on more than a fifth of {name}"
                            for name in thin])
        return out

    e_alpha, t_alpha = ge["avg_alpha_pct"], gt["avg_alpha_pct"]
    e_alpha_rows = [r for r in extras if _is_num(r.get("alpha_pct"))]
    t_alpha_rows = [r for r in taken if _is_num(r.get("alpha_pct"))]
    best_pnl = max(extras, key=lambda r: r["pnl"])
    best_alpha = max(e_alpha_rows, key=lambda r: r["alpha_pct"])
    t_best = max(t_alpha_rows, key=lambda r: r["alpha_pct"])
    e_pnl_ex = _total(_drop_best(extras, lambda r: r["pnl"]))
    e_alpha_ex = _avg_alpha(_drop_best(e_alpha_rows, lambda r: r["alpha_pct"]))
    t_alpha_ex = _avg_alpha(_drop_best(t_alpha_rows, lambda r: r["alpha_pct"]))
    lead = round(t_alpha - e_alpha, 3)          # > 0: taken ahead
    lead_ex = round(t_alpha_ex - e_alpha, 3)
    out.update(
        taken_lead_pp=lead,
        taken_lead_pp_ex_taken_best=lead_ex,
        extras_total_pnl_ex_best=e_pnl_ex,
        extras_avg_alpha_pct_ex_best=e_alpha_ex,
        dropped_best={"extras_pnl": best_pnl.get("symbol"),
                      "extras_alpha": best_alpha.get("symbol"),
                      "taken_alpha": t_best.get("symbol")},
    )

    short = []
    if ge["total_pnl"] <= 0:
        short.append(f"extras total P&L {ge['total_pnl']} <= 0")
    if e_alpha <= 0:
        short.append(f"extras avg alpha {e_alpha}% <= 0")
    if e_alpha < t_alpha:
        short.append(f"extras avg alpha {e_alpha}% below taken {t_alpha}%")

    if not short:
        fragile = []
        if e_pnl_ex <= 0:
            fragile.append(f"extras total P&L without {best_pnl.get('symbol')} "
                           f"{e_pnl_ex} <= 0")
        if e_alpha_ex is None or e_alpha_ex <= 0:
            fragile.append(f"extras avg alpha without {best_alpha.get('symbol')} "
                           f"{e_alpha_ex}% <= 0")
        elif e_alpha_ex < t_alpha:
            fragile.append(f"extras avg alpha without {best_alpha.get('symbol')} "
                           f"{e_alpha_ex}% below taken {t_alpha}%")
        if fragile:
            out.update(verdict="no_case", reasons=fragile)
        else:
            out.update(verdict="wider_book_discussion",
                       reasons=["extras are profitable, alpha-positive and at "
                                "least match taken, with their best trade "
                                "removed"])
        return out

    if lead >= WIDE_EXTRAS_MARGIN_PP:
        if lead_ex >= WIDE_EXTRAS_MARGIN_PP:
            out.update(verdict="extras_lag",
                       reasons=[f"taken leads extras by {lead}pp of avg alpha "
                                f"({lead_ex}pp without taken's best trade)"])
        else:
            out.update(verdict="no_case",
                       reasons=short + [f"taken's {lead}pp lead rests on "
                                        f"{t_best.get('symbol')}: {lead_ex}pp "
                                        "without it"])
        return out

    out.update(verdict="no_case", reasons=short)
    return out


def wide_extras_verdict_safe(wide_rows: list[dict], journal_dir: Path) -> dict:
    """shadow-status must keep serving the other registered blocks (and the
    owner alert that reads them) whatever happens here; a failure shows up
    as an explicit error state, never as a quiet 'pending'."""
    try:
        return wide_extras_verdict(wide_rows, wide_entry_evidence(journal_dir))
    except Exception as exc:
        return {"rule": WIDE_EXTRAS_RULE,
                "registered": WIDE_EXTRAS_REGISTERED,
                "verdict": "error",
                "status": "ERROR: extras verdict could not be computed",
                "error": f"{type(exc).__name__}: {exc}"}
