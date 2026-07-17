# Incident 2026-07-16: hung gate-status held the run lock all day

**Status:** closed (fixed 2026-07-17).
**Impact:** zero trading Thursday 2026-07-16 (all 4 windows skipped), 12
healthchecks skipped, manager skipped → no daily email, no alert of any kind.
No financial loss (book was flat); cost = one missed trading day + silent
monitoring blackout.

## What happened

First trading day after the Hetzner migration. The IB Gateway's nightly
internal restart (03:59 UTC) left the API half-alive: TCP port accepting,
session mute. The 10:45 UTC healthcheck's `gate-status` connected, then
blocked forever inside ib_async awaiting an account-summary response that
never came (15h43m in epoll_wait). It held /tmp/autoswing-brain.lock the
whole time. Every later process — 12 healthchecks, 4 brain windows, the
manager — correctly declined to run while the lock was held. The manager
being blocked meant the one component that emails the owner was silenced
by the same fault it should have reported.

## Root causes

1. No wall-clock bound anywhere in the CLI path; some ib_async request
   awaits have no timeout of their own.
2. The lock design assumed lock-holders always terminate; one zombie
   serialized the whole system.
3. Alerting had a single path (manager run) that shared the same lock —
   the dead-man switch was wired to the thing that died.

## Fixes (shipped 2026-07-17)

- **CLI watchdog**: every `autoswing` invocation arms SIGALRM
  (AUTOSWING_CMD_TIMEOUT, default 180s) → journals `cli.watchdog_timeout`,
  emits clean JSON error, exits 2. A hang can now hold the lock ≤3 minutes.
- **Wrapper caps**: healthcheck commands under `timeout 300`; brain runs
  under `timeout 2400`; manager under `timeout 3600` (all with --kill-after).
- **Lock-blocked manager now emails anyway**: on flock timeout it writes a
  BLOCKED report and sends "system may be stalled" directly (send_report
  needs no lock/broker). The dead-man switch no longer shares the trigger's
  failure domain.

## Lesson

Every unattended wait needs a deadline, and the alerting path must not
depend on the machinery it monitors. (Both were latent on Azure too —
the migration merely changed the gateway-restart timing that exposed them.)
