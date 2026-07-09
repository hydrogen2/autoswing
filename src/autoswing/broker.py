"""Broker layer: thin ib_async wrapper over IB Gateway.

Exposes exactly six operations. The only entry path is a bracket order
(entry + stop-loss + take-profit as one atomic unit) so no position can
ever exist without an attached stop, even if the caller crashes mid-run.
"""

from dataclasses import dataclass

from ib_async import IB, LimitOrder, MarketOrder, Stock

from .config import Config
from .journal import Journal

# Delayed market data (paper phase runs on free delayed quotes).
# 3 = delayed, 4 = delayed-frozen fallback when the market is closed.
DELAYED = 3
DELAYED_FROZEN = 4


@dataclass
class BracketProposal:
    symbol: str
    action: str  # BUY or SELL (SELL = short entry)
    quantity: int
    entry_limit: float
    stop_loss: float
    take_profit: float
    time_in_force: str = "GTC"


class Broker:
    def __init__(self, config: Config, journal: Journal):
        self.config = config
        self.journal = journal
        self.ib = IB()

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> None:
        b = self.config.broker
        self.ib.connect(
            b.host, b.port, clientId=b.client_id, timeout=b.connect_timeout_s
        )
        # Delayed-frozen: delayed quotes in market hours, last-known
        # values when closed. The paper phase runs entirely on this.
        self.ib.reqMarketDataType(DELAYED_FROZEN)
        self.journal.record(
            "broker.connect", host=b.host, port=b.port, client_id=b.client_id,
            server_version=self.ib.client.serverVersion(),
        )

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()

    # -- the six operations ---------------------------------------------------

    def get_account(self) -> dict:
        rows = self.ib.accountSummary()
        wanted = {
            "NetLiquidation", "TotalCashValue", "GrossPositionValue",
            "AvailableFunds", "BuyingPower", "RealizedPnL", "UnrealizedPnL",
        }
        summary = {
            r.tag: {"value": r.value, "currency": r.currency}
            for r in rows if r.tag in wanted
        }
        accounts = self.ib.managedAccounts()
        result = {"accounts": accounts, "summary": summary}
        self.journal.record("broker.get_account", result=result)
        return result

    def get_positions(self) -> list[dict]:
        positions = [
            {
                "account": p.account,
                "symbol": p.contract.symbol,
                "sec_type": p.contract.secType,
                "quantity": p.position,
                "avg_cost": p.avgCost,
            }
            for p in self.ib.positions()
        ]
        open_orders = [
            {
                "order_id": t.order.orderId,
                "symbol": t.contract.symbol,
                "action": t.order.action,
                "type": t.order.orderType,
                "quantity": t.order.totalQuantity,
                "limit_price": t.order.lmtPrice,
                "stop_price": t.order.auxPrice,
                "status": t.orderStatus.status,
            }
            for t in self.ib.openTrades()
        ]
        result = {"positions": positions, "open_orders": open_orders}
        self.journal.record("broker.get_positions", result=result)
        return result

    def get_quote(self, symbol: str) -> dict:
        contract = self._qualified_stock(symbol)
        # Streaming (not snapshot): snapshot requests ignore delayed-frozen
        # mode and come back empty without a real-time subscription.
        # Delayed data ticks in within ~10s; poll then cancel.
        ticker = self.ib.reqMktData(contract, "", snapshot=False)
        for _ in range(30):
            self.ib.sleep(0.5)
            if any(_num(v) is not None for v in (ticker.last, ticker.close, ticker.bid)):
                break
        quote = {
            "symbol": symbol.upper(),
            "bid": _num(ticker.bid),
            "ask": _num(ticker.ask),
            "last": _num(ticker.last),
            "close": _num(ticker.close),
            "volume": _num(ticker.volume),
            "market_data_type": ticker.marketDataType,  # 3=delayed
        }
        self.ib.cancelMktData(contract)
        self.journal.record("broker.get_quote", result=quote)
        return quote

    def place_bracket_order(self, proposal: BracketProposal) -> dict:
        """Place entry + stop + target as one atomic bracket. The ONLY entry path."""
        p = proposal
        _validate_bracket(p)
        contract = self._qualified_stock(p.symbol)

        bracket = self.ib.bracketOrder(
            action=p.action.upper(),
            quantity=p.quantity,
            limitPrice=p.entry_limit,
            takeProfitPrice=p.take_profit,
            stopLossPrice=p.stop_loss,
        )
        for order in bracket:
            order.tif = p.time_in_force
            order.outsideRth = False

        trades = [self.ib.placeOrder(contract, o) for o in bracket]
        self.ib.sleep(1.5)  # let order statuses come back

        result = {
            "symbol": p.symbol.upper(),
            "action": p.action.upper(),
            "quantity": p.quantity,
            "entry_limit": p.entry_limit,
            "stop_loss": p.stop_loss,
            "take_profit": p.take_profit,
            "orders": [
                {
                    "order_id": t.order.orderId,
                    "role": role,
                    "status": t.orderStatus.status,
                }
                for role, t in zip(("entry", "take_profit", "stop_loss"), trades)
            ],
        }
        self.journal.record("broker.place_bracket_order", result=result)
        return result

    def cancel_order(self, order_id: int) -> dict:
        target = next(
            (t for t in self.ib.openTrades() if t.order.orderId == order_id), None
        )
        if target is None:
            result = {"order_id": order_id, "status": "not_found"}
        else:
            self.ib.cancelOrder(target.order)
            self.ib.sleep(1.0)
            result = {"order_id": order_id, "status": target.orderStatus.status}
        self.journal.record("broker.cancel_order", result=result)
        return result

    def flatten_all(self) -> dict:
        """Emergency exit: cancel every open order, close every position at market."""
        self.journal.record("broker.flatten_all.begin")
        cancelled = []
        for t in list(self.ib.openTrades()):
            self.ib.cancelOrder(t.order)
            cancelled.append(t.order.orderId)
        self.ib.sleep(1.0)

        closed = []
        for pos in self.ib.positions():
            if pos.position == 0 or pos.contract.secType != "STK":
                continue
            action = "SELL" if pos.position > 0 else "BUY"
            contract = self._qualified_stock(pos.contract.symbol)
            order = MarketOrder(action, abs(pos.position))
            trade = self.ib.placeOrder(contract, order)
            closed.append(
                {"symbol": pos.contract.symbol, "action": action,
                 "quantity": abs(pos.position), "order_id": trade.order.orderId}
            )
        self.ib.sleep(1.5)
        result = {"cancelled_order_ids": cancelled, "closing_orders": closed}
        self.journal.record("broker.flatten_all", result=result)
        return result

    def close_position(self, symbol: str) -> dict:
        """Cancel the symbol's working orders, then close any position at
        market. Used by deterministic exits (time-box, pre-earnings)."""
        sym = symbol.upper()
        cancelled = []
        for t in list(self.ib.openTrades()):
            if t.contract.symbol == sym:
                self.ib.cancelOrder(t.order)
                cancelled.append(t.order.orderId)
        self.ib.sleep(1.0)

        closing_order = None
        for pos in self.ib.positions():
            if pos.contract.symbol == sym and pos.position != 0:
                action = "SELL" if pos.position > 0 else "BUY"
                contract = self._qualified_stock(sym)
                trade = self.ib.placeOrder(contract, MarketOrder(action, abs(pos.position)))
                self.ib.sleep(1.5)
                closing_order = {
                    "order_id": trade.order.orderId, "action": action,
                    "quantity": abs(pos.position),
                    "status": trade.orderStatus.status,
                }
        result = {"symbol": sym, "cancelled_order_ids": cancelled,
                  "closing_order": closing_order}
        self.journal.record("broker.close_position", result=result)
        return result

    def account_state(self):
        """Snapshot for the risk gate: net liq, positions, working entry orders."""
        from .risk_gate import AccountState, OpenOrderInfo, PositionInfo

        net_liq = next(
            (float(r.value) for r in self.ib.accountSummary()
             if r.tag == "NetLiquidation"),
            None,
        )
        if net_liq is None:
            raise RuntimeError("could not read NetLiquidation from account summary")

        positions = [
            PositionInfo(
                symbol=p.contract.symbol,
                quantity=p.position,
                notional=abs(p.position) * p.avgCost,
            )
            for p in self.ib.positions()
            if p.contract.secType == "STK" and p.position != 0
        ]
        open_orders = [
            OpenOrderInfo(
                symbol=t.contract.symbol,
                # Bracket children carry parentId; bare parentId==0 orders
                # are entries.
                is_entry=t.order.parentId == 0,
                notional=t.order.totalQuantity * (t.order.lmtPrice or 0),
            )
            for t in self.ib.openTrades()
        ]
        return AccountState(
            net_liquidation=net_liq, positions=positions, open_orders=open_orders
        )

    # -- helpers ---------------------------------------------------------------

    def _qualified_stock(self, symbol: str) -> Stock:
        contract = Stock(symbol.upper(), "SMART", "USD")
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError(f"Unknown or non-tradable symbol: {symbol!r}")
        return qualified[0]


def _validate_bracket(p: BracketProposal) -> None:
    if p.action.upper() not in ("BUY", "SELL"):
        raise ValueError(f"action must be BUY or SELL, got {p.action!r}")
    if p.quantity <= 0 or int(p.quantity) != p.quantity:
        raise ValueError(f"quantity must be a positive integer, got {p.quantity!r}")
    if min(p.entry_limit, p.stop_loss, p.take_profit) <= 0:
        raise ValueError("all prices must be positive")
    if p.action.upper() == "BUY":
        if not (p.stop_loss < p.entry_limit < p.take_profit):
            raise ValueError(
                "BUY bracket requires stop_loss < entry_limit < take_profit, got "
                f"stop={p.stop_loss} entry={p.entry_limit} target={p.take_profit}"
            )
    else:
        if not (p.take_profit < p.entry_limit < p.stop_loss):
            raise ValueError(
                "SELL bracket requires take_profit < entry_limit < stop_loss, got "
                f"target={p.take_profit} entry={p.entry_limit} stop={p.stop_loss}"
            )


def _num(value):
    """NaN-safe float for JSON output."""
    if value is None or value != value:
        return None
    return value
