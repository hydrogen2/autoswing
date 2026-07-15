# Migration runbook: Azure VM → Hetzner CPX21

Target: Hetzner CPX21 (2 vCPU x86, 4 GB), Ubuntu 24.04, any region.
Total time ~1 evening. The golden rules:

- **Flat book**: migrate on a weekend or with no open positions/orders
  (account admin on a flat book only — incident 2026-07-14 rule).
- **Never two live instances**: the same IBKR paper user in two gateways
  kicks each other's sessions; two crons would double-trade. OLD box goes
  quiet before the NEW box goes live.

## Phase 1 — on the OLD (Azure) machine

1. Verify flat: `uv run autoswing get-positions` → no positions, no orders.
2. Freeze the bot: `sudo mv /etc/cron.d/autoswing /etc/cron.d/autoswing.disabled`
3. Stop the gateway: `cd ~/autoswing/docker && sudo docker compose down`
4. Pack the valuables (fresh — state changes daily):
   `scripts/migrate-pack.sh` → writes `~/autoswing-migrate-<date>.tar.gz.enc`
   (choose a passphrase; it is not stored anywhere)
5. Copy to the new box: `scp ~/autoswing-migrate-*.tar.gz.enc <new-box>:`

## Phase 2 — on the NEW (Hetzner) machine

6. **Create the same user** so every path in scripts/cron matches verbatim:
   ```bash
   adduser supper-user && usermod -aG sudo supper-user
   # passwordless sudo (scripts assume it):
   echo "supper-user ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/supper-user
   su - supper-user
   ```
7. Base tools:
   ```bash
   sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 gh
   sudo usermod -aG docker supper-user
   curl -LsSf https://astral.sh/uv/install.sh | sh
   curl -fsSL https://claude.ai/install.sh | bash   # Claude Code CLI
   timedatectl | grep -q UTC || sudo timedatectl set-timezone UTC
   ```
8. Auth (both interactive, one-time):
   `gh auth login` (github.com, HTTPS, web) and `claude` (log in with the
   Claude subscription).
9. Clone + restore:
   ```bash
   git clone https://github.com/hydrogen2/autoswing.git ~/autoswing
   cd ~/autoswing
   scripts/migrate-unpack.sh ~/autoswing-migrate-<date>.tar.gz.enc
   ```
10. Build + verify offline pieces:
    `uv sync && uv run pytest`  → all tests green.
11. Gateway up (old one MUST already be down — phase 1 step 3):
    `cd docker && docker compose up -d && docker compose logs -f`
    → wait for "Login has completed".
12. End-to-end proof:
    ```bash
    uv run autoswing gate-status      # sane virtual equity, kill switch state
    uv run autoswing smoke-test       # full Phase-0 exit test
    ./scripts/healthcheck.sh && echo HEALTHY
    ```
13. Re-arm the schedule:
    `sudo cp scripts/cron.d-autoswing.example /etc/cron.d/autoswing`
14. Open Claude Code in ~/autoswing and ask it to review migration state —
    its memory was restored in step 9; it should know the whole project.

## Phase 3 — burn-in and decommission

15. Watch one full trading day: four brain logs in `state/brain/logs/`,
    hourly healthchecks green, manager email arrives on schedule.
16. Keep the Azure VM stopped-but-existing for ~1 week as fallback
    (a stopped VM ≈ storage-only cost), then delete it.
17. Rotate the IBKR paper password and Gmail app password afterwards if you
    want the old box's copies dead (update docker/.env and .secrets.env).

## What moves how

| Asset | Path | Carried by |
|---|---|---|
| Code, config, prompts, scripts, plan | repo | git clone |
| IBKR credentials | docker/.env | encrypted pack |
| Gmail app password | .secrets.env | encrypted pack |
| Gate anchor/HWM/kill state | state/gate_state.json | encrypted pack |
| Position intent metadata | state/positions.json | encrypted pack |
| Reconciler shadow record | state/reconcile_state.json | encrypted pack |
| Benchmark history (the track record) | state/benchmark.jsonl | encrypted pack |
| Full audit journal | journal/*.jsonl | encrypted pack |
| Brain/manager/health logs | state/brain/logs/ | encrypted pack |
| Claude Code project memory | ~/.claude/projects/.../memory | encrypted pack (auto-relocated by unpack script) |
| Cron schedule | /etc/cron.d/autoswing | recreated from repo example (step 13) |
| Claude + GitHub auth | — | re-login on new box (step 8) |
