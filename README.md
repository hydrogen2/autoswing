# autoswing

Autonomous swing-trading agent. See [PLAN.md](PLAN.md) for the full design.

**Current status: Phase 0 — broker plumbing.** Paper trading only.

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
```

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
