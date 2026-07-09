# autoswing

Autonomous swing-trading agent. See [PLAN.md](PLAN.md) for the full design.

**Current status: Phase 2 — data layer complete.** Paper trading only.

```bash
uv run autoswing scan-candidates --days-back 3   # PEAD candidates from recent reporters
uv run autoswing next-earnings NVDA             # gate-compatible next-report lookup
```
Data sources (free tier): Nasdaq public earnings calendar (dates, EPS
estimate/actual, surprise), yfinance (prices, reactions, ADV, next report
dates). Data honesty rule: unverifiable facts are reported as "unknown",
which the risk gate rejects — the pipeline never guesses.

## One-time setup

1. **Enable paper trading** in IBKR Client Portal (Settings → Account Settings →
   Paper Trading Account). Note the generated paper username.
2. **Reset the paper balance to $50,000** (paper account settings) — the default
   $1M would invalidate the test.
3. **Credentials**: `cp docker/.env.example docker/.env` and fill in the paper
   username/password. `.env` is gitignored; never commit it.
4. **Start the gateway**:
   ```bash
   cd docker && docker compose up -d
   ```
   First login can take a couple of minutes. `docker compose logs -f` to watch.
5. **Install and verify**:
   ```bash
   uv sync
   uv run pytest            # safety tests must pass
   uv run autoswing smoke-test
   ```

## CLI

Every command prints JSON and appends to the journal (`journal/YYYY-MM-DD.jsonl`).

```bash
uv run autoswing get-account
uv run autoswing get-positions
uv run autoswing get-quote NVDA
uv run autoswing place-bracket-order AAPL BUY 10 --entry 150 --stop 142.5 --target 165
uv run autoswing cancel-order 42
uv run autoswing flatten-all --i-am-sure   # emergency: close everything

# The agent's ONLY entry path — proposal JSON through the risk gate:
echo '{"symbol":"XOM","action":"BUY","quantity":60,"entry_limit":100.0,
  "stop_loss":97.0,"take_profit":112.0,"rationale":"...",
  "next_earnings_date":"2026-10-30","avg_dollar_volume":900000000}' \
  | uv run autoswing propose-trade - [--dry-run]

uv run autoswing gate-status               # virtual equity, HWM, drawdown, kill switch
uv run autoswing gate-reset --i-am-sure    # HUMAN ONLY: clear tripped kill switch
```

## Risk gate

Deterministic rules in `src/autoswing/risk_gate.py`; limits in
`config/config.yaml` (human-only). Rules: kill switch (−15% drawdown, sticky
until human reset), daily loss halt (−3%), 1% risk/trade, 15% max position,
10 max positions, 100% gross exposure, duplicate suppression, earnings
blackout (unknown date = rejection), liquidity floor, min price, long-only,
market hours, core-overlap cap (1, flagged), PDT guard (dormant ≥ $25k).

Sizing uses **virtual equity** = `equity_baseline + (net_liq − anchor)`, so
the unreset $1M paper balance cannot inflate positions. State persists in
`state/gate_state.json`.

## Safety model (Phase 0)

- **Paper by default, live by triple opt-in**: connecting to the live port
  requires `port: 4001` + `live_trading: true` in config + `AUTOSWING_LIVE=1`
  in the environment. Anything less is refused at startup.
- **Bracket-only entries**: the only order-placement path takes entry,
  stop-loss, and take-profit as one atomic unit. A position without a stop
  cannot be created through this code.
- **Append-only journal**: every broker call and result is recorded.
- The API port is bound to localhost only.
- `config/config.yaml` risk values are human-only; the agent never edits them.
