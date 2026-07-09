"""The deterministic risk gate. Every trade proposal passes through here.

No LLM in this file, ever. The gate is pure rules + persisted state
(high-water mark, kill switch, day anchors). The agent proposes; this
code disposes. All limits come from config/config.yaml, which is
human-only.

Because the paper account balance may not equal the intended base
capital (IBKR paper defaults to $1M), the gate tracks *virtual equity*:
    virtual_equity = equity_baseline + (net_liquidation - anchor)
where the anchor is the account's net liquidation recorded on first run.
All sizing, drawdown, and halt decisions use virtual equity.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .broker import BracketProposal, _validate_bracket

ET = ZoneInfo("America/New_York")


# -- inputs -------------------------------------------------------------------

@dataclass
class TradeProposal:
    """What the brain must submit. Missing context is grounds for rejection."""
    symbol: str
    action: str                       # BUY, or SELL for a short entry
    quantity: int
    entry_limit: float
    stop_loss: float
    take_profit: float
    rationale: str = ""
    # "YYYY-MM-DD", "none" (verified no upcoming report), or "unknown"
    next_earnings_date: str = "unknown"
    avg_dollar_volume: float | None = None

    def to_bracket(self) -> BracketProposal:
        return BracketProposal(
            symbol=self.symbol, action=self.action, quantity=self.quantity,
            entry_limit=self.entry_limit, stop_loss=self.stop_loss,
            take_profit=self.take_profit,
        )


@dataclass
class PositionInfo:
    symbol: str
    quantity: float
    notional: float


@dataclass
class OpenOrderInfo:
    symbol: str
    is_entry: bool
    notional: float


@dataclass
class AccountState:
    net_liquidation: float
    positions: list[PositionInfo]
    open_orders: list[OpenOrderInfo]


# -- outputs ------------------------------------------------------------------

@dataclass
class RuleResult:
    rule: str
    passed: bool
    detail: str


@dataclass
class Decision:
    approved: bool
    rules: list[RuleResult]
    warnings: list[str]
    virtual_equity: float
    drawdown_pct: float

    def to_dict(self) -> dict:
        return asdict(self)


# -- persisted state -----------------------------------------------------------

@dataclass
class GateState:
    anchor_net_liq: float | None = None
    hwm_virtual_equity: float | None = None
    kill_tripped: bool = False
    kill_reason: str = ""
    kill_date: str = ""
    day_anchor_date: str = ""
    day_anchor_equity: float | None = None
    day_trade_dates: list[str] = field(default_factory=list)  # fed by fills, Phase 3

    @classmethod
    def load(cls, path: Path) -> "GateState":
        if path.exists():
            return cls(**json.loads(path.read_text()))
        return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(path)


# -- the gate -------------------------------------------------------------------

class RiskGate:
    def __init__(self, risk_config: dict, state_path: Path):
        self.cfg = risk_config
        self.state_path = state_path
        self.state = GateState.load(state_path)

    # State/equity bookkeeping shared by evaluate() and status().
    def _update_equity_state(self, account: AccountState, now: datetime):
        baseline = float(self.cfg["equity_baseline"])
        if self.state.anchor_net_liq is None:
            self.state.anchor_net_liq = account.net_liquidation
        virtual = baseline + (account.net_liquidation - self.state.anchor_net_liq)

        if self.state.hwm_virtual_equity is None or virtual > self.state.hwm_virtual_equity:
            self.state.hwm_virtual_equity = virtual

        today = now.astimezone(ET).date().isoformat()
        if self.state.day_anchor_date != today:
            self.state.day_anchor_date = today
            self.state.day_anchor_equity = virtual

        drawdown_pct = 100.0 * (1 - virtual / self.state.hwm_virtual_equity)

        # Trip the kill switch the moment the drawdown limit is breached,
        # even if no trade is being proposed.
        if not self.state.kill_tripped and drawdown_pct >= float(self.cfg["max_drawdown_kill_pct"]):
            self.state.kill_tripped = True
            self.state.kill_reason = (
                f"drawdown {drawdown_pct:.1f}% >= {self.cfg['max_drawdown_kill_pct']}% "
                f"(virtual equity {virtual:.2f}, HWM {self.state.hwm_virtual_equity:.2f})"
            )
            self.state.kill_date = today

        self.state.save(self.state_path)
        return virtual, drawdown_pct

    def status(self, account: AccountState, now: datetime | None = None) -> dict:
        now = now or datetime.now(ET)
        virtual, dd = self._update_equity_state(account, now)
        return {
            "virtual_equity": round(virtual, 2),
            "hwm": round(self.state.hwm_virtual_equity, 2),
            "drawdown_pct": round(dd, 2),
            "day_anchor_equity": self.state.day_anchor_equity,
            "kill_tripped": self.state.kill_tripped,
            "kill_reason": self.state.kill_reason,
            "anchor_net_liq": self.state.anchor_net_liq,
        }

    def reset_kill(self) -> None:
        """Human-only, via CLI with explicit acknowledgement."""
        self.state.kill_tripped = False
        self.state.kill_reason = ""
        self.state.kill_date = ""
        # Re-anchor HWM to current reality on reset so the same breach
        # doesn't instantly re-trip.
        self.state.hwm_virtual_equity = None
        self.state.anchor_net_liq = None
        self.state.save(self.state_path)

    def evaluate(
        self,
        proposal: TradeProposal,
        account: AccountState,
        now: datetime | None = None,
    ) -> Decision:
        now = now or datetime.now(ET)
        virtual, drawdown_pct = self._update_equity_state(account, now)

        rules: list[RuleResult] = []
        warnings: list[str] = []

        def check(rule: str, passed: bool, detail: str):
            rules.append(RuleResult(rule=rule, passed=passed, detail=detail))

        cfg = self.cfg

        # 0. Kill switch and daily halt come first: no other rule matters.
        check("kill_switch", not self.state.kill_tripped,
              self.state.kill_reason or "not tripped")

        day_loss_pct = 0.0
        if self.state.day_anchor_equity:
            day_loss_pct = 100.0 * (1 - virtual / self.state.day_anchor_equity)
        check("daily_loss_halt", day_loss_pct < float(cfg["daily_loss_halt_pct"]),
              f"day P&L {-day_loss_pct:.2f}% vs halt at -{cfg['daily_loss_halt_pct']}%")

        # 1. Structural bracket sanity (same validation the broker applies).
        try:
            _validate_bracket(proposal.to_bracket())
            check("bracket_structure", True, "entry/stop/target ordering valid")
        except ValueError as e:
            check("bracket_structure", False, str(e))

        # 2. Long-only unless shorts are explicitly enabled.
        is_short = proposal.action.upper() == "SELL"
        check("short_selling", (not is_short) or bool(cfg.get("allow_short", False)),
              "short entry" if is_short else "long entry")

        # 3. Market hours (regular session only; no holiday calendar yet).
        et_now = now.astimezone(ET)
        in_rth = (
            et_now.weekday() < 5
            and (et_now.hour, et_now.minute) >= (9, 30)
            and et_now.hour < 16
        )
        check("market_hours", in_rth or bool(cfg.get("allow_outside_rth", False)),
              f"now={et_now:%Y-%m-%d %H:%M %Z}")

        # 4. Risk per trade: distance to stop * quantity vs budget.
        risk_budget = virtual * float(cfg["risk_per_trade_pct"]) / 100.0
        trade_risk = abs(proposal.entry_limit - proposal.stop_loss) * proposal.quantity
        check("risk_per_trade", trade_risk <= risk_budget + 1e-9,
              f"risk ${trade_risk:.2f} vs budget ${risk_budget:.2f}")

        # 5. Max position notional.
        max_notional = virtual * float(cfg["max_position_pct"]) / 100.0
        notional = proposal.entry_limit * proposal.quantity
        check("max_position_size", notional <= max_notional + 1e-9,
              f"notional ${notional:.2f} vs cap ${max_notional:.2f}")

        # 6. Max open positions (held + pending entries count).
        held = {p.symbol for p in account.positions if p.quantity != 0}
        pending = {o.symbol for o in account.open_orders if o.is_entry}
        open_count = len(held | pending)
        check("max_open_positions", open_count < int(cfg["max_open_positions"]),
              f"{open_count} open/pending vs max {cfg['max_open_positions']}")

        # 7. Gross exposure including this order.
        exposure = (
            sum(abs(p.notional) for p in account.positions)
            + sum(o.notional for o in account.open_orders if o.is_entry)
            + notional
        )
        max_exposure = virtual * float(cfg["max_gross_exposure_pct"]) / 100.0
        check("max_gross_exposure", exposure <= max_exposure + 1e-9,
              f"exposure ${exposure:.2f} vs cap ${max_exposure:.2f}")

        # 8. Duplicate suppression: one position/working entry per symbol.
        sym = proposal.symbol.upper()
        check("duplicate_position", sym not in held and sym not in pending,
              f"{sym} already held or pending" if sym in held | pending else "no overlap")

        # 9. Earnings blackout. "unknown" is a rejection: the brain must check.
        ned = proposal.next_earnings_date
        blackout = int(cfg["earnings_blackout_days"])
        if ned == "unknown" or not ned:
            check("earnings_blackout", False,
                  "next_earnings_date not provided — proposals must verify the calendar")
        elif ned == "none":
            check("earnings_blackout", True, "verified: no upcoming report")
        else:
            try:
                edate = date.fromisoformat(ned)
                days_until = (edate - et_now.date()).days
                ok = not (0 <= days_until <= blackout)
                check("earnings_blackout", ok,
                      f"earnings {ned} is {days_until}d away (blackout {blackout}d)")
            except ValueError:
                check("earnings_blackout", False, f"unparseable date {ned!r}")

        # 10. Liquidity floor and minimum price.
        adv = proposal.avg_dollar_volume
        min_adv = float(cfg["min_avg_dollar_volume"])
        if adv is None:
            check("liquidity", False, "avg_dollar_volume not provided")
        else:
            check("liquidity", adv >= min_adv,
                  f"ADV ${adv:,.0f} vs floor ${min_adv:,.0f}")
        check("min_price", proposal.entry_limit >= float(cfg.get("min_price", 5.0)),
              f"entry ${proposal.entry_limit} vs min ${cfg.get('min_price', 5.0)}")

        # 11. Core-holding overlap: allowed but capped and flagged.
        core = {c.upper() for c in cfg.get("core_holdings", [])}
        overlap_open = len((held | pending) & core)
        if sym in core:
            cap = int(cfg["max_core_overlap_positions"])
            check("core_overlap", overlap_open < cap,
                  f"{sym} is a core holding; {overlap_open} core overlap(s) open, cap {cap}")
            warnings.append(
                f"{sym} overlaps the owner's core portfolio — you now hold it in both books"
            )
        else:
            check("core_overlap", True, "not a core holding")

        # 12. PDT guard: dormant above the equity threshold.
        if virtual >= float(cfg.get("pdt_min_equity", 25000)):
            check("pdt_guard", True, "dormant (equity above PDT threshold)")
        else:
            cutoff = et_now.date() - timedelta(days=7)
            recent = [d for d in self.state.day_trade_dates
                      if date.fromisoformat(d) >= cutoff]
            check("pdt_guard", len(recent) < 3,
                  f"{len(recent)} day-trades in window (max 3 below ${cfg.get('pdt_min_equity', 25000)})")

        approved = all(r.passed for r in rules)
        return Decision(
            approved=approved, rules=rules, warnings=warnings,
            virtual_equity=round(virtual, 2), drawdown_pct=round(drawdown_pct, 2),
        )
