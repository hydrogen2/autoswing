"""Position/order consistency reconciler — the orphan-order guard.

The broker keeps positions and working orders as separate records with no
transactional link (see docs/incidents/2026-07-14-reset-orphan-short.md).
This module enforces the invariant from outside, as a control loop:

    desired state  = positions.json intent (what the bot meant to hold)
    actual state   = broker snapshot (positions, orders, fills, cash)
    reconciler     = converge actual toward consistent, slowly, with
                     evidence proportional to how destructive the action is

Two invariant violations, opposite repairs:
  - ORPHAN:  working exit orders with no position -> cancel the orders
             (destructive: needs multi-witness corroboration)
  - NAKED:   long position with no working stop  -> re-place the stop from
             intent metadata (additive: needs less corroboration)

MODES (config, human-only): "shadow" journals decisions without acting;
"enforce" executes them. Shadow is the default and must accumulate a clean
record before enforce is ever enabled (go-live gate).

A single observation is never trusted: snapshots can lie (observed during
gateway restarts). Confirmation requires persistence across polls AND time,
plus fills/cash to classify the cause.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

WORKING_STATUSES = {"PreSubmitted", "Submitted", "PendingSubmit"}


@dataclass
class OrderObs:
    order_id: int
    symbol: str
    action: str        # BUY / SELL
    order_type: str    # LMT / STP / MKT
    quantity: float
    status: str


@dataclass
class Observation:
    ts: str                          # ISO
    positions: dict                  # symbol -> signed quantity
    orders: list                     # list[OrderObs]
    fills: list                      # today's executions (dicts w/ symbol, side)
    cash: float


@dataclass
class SymbolState:
    status: str = "consistent"       # consistent|suspect_orphan|confirmed_orphan|suspect_naked
    first_seen: str = ""             # when suspicion started
    polls: int = 0                   # consecutive polls confirming suspicion
    cash_at_first: float = 0.0
    est_notional: float = 0.0


def load_state(path: Path) -> dict[str, SymbolState]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {sym: SymbolState(**s) for sym, s in raw.items()}


def save_state(path: Path, state: dict[str, SymbolState]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({s: asdict(v) for s, v in state.items()}, indent=2))
    tmp.replace(path)


def _age_minutes(first_seen: str, now: datetime) -> float:
    if not first_seen:
        return 0.0
    t0 = datetime.fromisoformat(first_seen)
    return (now - t0).total_seconds() / 60.0


def evaluate(
    obs: Observation,
    prev: dict[str, SymbolState],
    intent: dict,                    # symbol -> {"stop_loss": float, "quantity": ...}
    cfg: dict,
    now: datetime | None = None,
) -> tuple[dict[str, SymbolState], list[dict], list[str]]:
    """Pure state-machine step. Returns (new_state, decisions, notes).

    decisions: [{symbol, action: cancel_orphans|replace_stop, order_ids|stop_price,
                 cause, corroboration}]
    Never touches the broker — the caller acts (or shadow-logs) on decisions.
    """
    now = now or datetime.now(timezone.utc)
    min_polls = int(cfg.get("min_polls", 2))
    min_minutes = float(cfg.get("min_suspect_minutes", 90))

    working = [o for o in obs.orders if o.status in WORKING_STATUSES]
    sells_by_sym: dict[str, list[OrderObs]] = {}
    for o in working:
        if o.action == "SELL":
            sells_by_sym.setdefault(o.symbol, []).append(o)

    state: dict[str, SymbolState] = {}
    decisions: list[dict] = []
    notes: list[str] = []
    symbols = set(obs.positions) | set(sells_by_sym) | set(prev)

    for sym in sorted(symbols):
        qty = obs.positions.get(sym, 0.0)
        sells = sells_by_sym.get(sym, [])
        st = prev.get(sym, SymbolState())

        if qty < 0:
            # Unexpected short: manage-positions owns the narrative; the
            # reconciler only refuses to touch it.
            notes.append(f"{sym}: unexpected short ({qty}) — no reconciler action")
            state[sym] = SymbolState(status="consistent")
            continue

        # --- ORPHAN direction: SELL orders working, no shares ---------------
        if sells and qty == 0:
            est_notional = sum(
                o.quantity * intent.get(sym, {}).get("stop_loss", 0.0) for o in sells
            )
            if st.status not in ("suspect_orphan", "confirmed_orphan"):
                st = SymbolState(
                    status="suspect_orphan", first_seen=now.isoformat(), polls=1,
                    cash_at_first=obs.cash, est_notional=est_notional,
                )
                notes.append(f"{sym}: SUSPECT orphan — {len(sells)} working SELL "
                             f"order(s), no position. Watching, not acting.")
            else:
                st.polls += 1
                fill_explains = any(f.get("symbol") == sym for f in obs.fills)
                cash_moved = (
                    st.est_notional > 0
                    and abs(obs.cash - st.cash_at_first) > 0.5 * st.est_notional
                )
                aged = _age_minutes(st.first_seen, now) >= min_minutes
                cause = None
                if fill_explains or cash_moved:
                    # Independent ledger agrees the position really closed:
                    # fast path, persistence alone suffices.
                    if st.polls >= min_polls:
                        cause = "sold" if fill_explains else "sold_unrecorded"
                elif st.polls >= min_polls and aged:
                    cause = "deleted"  # no sale story + persisted: side-door removal
                if cause:
                    st.status = "confirmed_orphan"
                    decisions.append({
                        "symbol": sym, "action": "cancel_orphans",
                        "order_ids": [o.order_id for o in sells],
                        "cause": cause,
                        "corroboration": {
                            "polls": st.polls,
                            "age_minutes": round(_age_minutes(st.first_seen, now), 1),
                            "fill_explains": fill_explains,
                            "cash_moved": cash_moved,
                        },
                    })
                else:
                    notes.append(f"{sym}: still suspect_orphan (polls={st.polls}, "
                                 f"age={_age_minutes(st.first_seen, now):.0f}m)")
            state[sym] = st
            continue

        # --- NAKED direction: shares held, no working stop ------------------
        has_stop = any(o.order_type == "STP" for o in sells)
        if qty > 0 and not has_stop:
            meta = intent.get(sym)
            if st.status != "suspect_naked":
                st = SymbolState(status="suspect_naked",
                                 first_seen=now.isoformat(), polls=1)
                notes.append(f"{sym}: SUSPECT naked — {qty} shares with no working "
                             "stop. One more poll to confirm.")
            else:
                st.polls += 1
                if st.polls >= min_polls:
                    if meta and meta.get("stop_loss"):
                        decisions.append({
                            "symbol": sym, "action": "replace_stop",
                            "quantity": qty, "stop_price": meta["stop_loss"],
                            "cause": "position without protective stop",
                            "corroboration": {"polls": st.polls},
                        })
                    else:
                        notes.append(f"{sym}: naked with NO intent metadata — "
                                     "cannot derive stop; ESCALATE to owner")
            state[sym] = st
            continue

        # --- consistent ------------------------------------------------------
        if st.status != "consistent":
            notes.append(f"{sym}: back to consistent (was {st.status})")
        state[sym] = SymbolState(status="consistent")

    return state, decisions, notes
