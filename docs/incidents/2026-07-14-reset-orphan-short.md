# Incident 2026-07-14: account reset orphaned a stop order → accidental naked short

**Status:** closed (short flattened, books re-anchored — see resolution below).
**Root cause:** IBKR paper-account reset eliminated the open position but did
NOT cancel its working bracket orders. The orphaned stop later executed into a
flat book, creating an unintended short. Compounded by us re-anchoring equity
mid-reset, believing the reset complete when only the cash phase had applied.

## Timeline (UTC)

| When | Event |
|---|---|
| Jul 10 14:04 | Bot buys 96 PENG @ $76.21, bracket: stop $71.00 / target $86.60 |
| Jul 12–13 | Owner requests paper reset to $50,096 ("takes one day to process") |
| Jul 13 02:00 | Cash phase applies: cash = $50,096 exactly; position still present. Operator (Claude) re-anchors gate believing reset complete — **error**: reset was half-applied |
| Jul 13 20:45 | Last normal snapshot: 96 PENG held, both bracket legs working |
| Jul 13→14 night | Reset's position phase runs in IBKR nightly batch: **position record deleted — not sold** (no fill, cash unchanged). **IBKR bug: both bracket legs left working** |
| Jul 14 ~04:00 | PENG announces $650M convertible offering (unrelated news) |
| Jul 14 12:01 | Premarket brain correctly diagnoses empty-locker state, predicts spurious kill-switch trip, stands down |
| Jul 14 14:16 | Stock trades through $71; orphaned stop executes: SLD 96 @ $70.91 into a flat book → **naked short −96 PENG, no protective orders** |
| Jul 14 14:45 | Phantom equity gap (~$7.2k of reset-deleted shares) pushes measured drawdown past 15% → kill switch trips (correct behavior on incorrect books) |
| Jul 14 21:15 | Manager run flags UNEXPECTED_SHORT, ships detection fix (4623cf4), escalates to owner |

## Accounting

- **Strategy P&L (economic, as if no reset):** WDFC stop −$500 (Jul 10);
  PENG long $76.21 → $70.91 = −$509 (the stop price the strategy would have
  realized regardless); accidental short drift ≈ −$330 as of Jul 14 close.
  True strategy performance since 2026-07-09 inception ≈ **−1.4%** vs VOO +0.85%.
- **Non-P&L bookkeeping event:** the reset itself removed 96 PENG (~$7.3k)
  without sale proceeds — that is the reset defining the account as $50,096
  cash, not a trading loss. The −15.36% shown on Jul 14 marks is this
  bookkeeping hole, not strategy losses. Benchmark rows for 2026-07-14 are
  distorted accordingly; post-repair rows are re-anchored to economic reality.
- The kill-switch trip on Jul 14 was spurious (phantom drawdown).

## Lessons / follow-ups

1. **Never reset/modify the account with positions open.** Account-level
   admin belongs before inception or on a flat book. (Owner + operator rule.)
2. **Brokers can leave orphaned orders.** Detection shipped (4623cf4):
   manage-positions reports UNEXPECTED_SHORT / position_mismatch and never
   manages what it didn't open. Corroborated auto-cancel of orphaned legs is
   deliberately NOT implemented on paper (complexity in the safety path);
   it is a **required item on the go-live checklist**.
3. Kill switch + brain playbook behaved correctly under corrupted books:
   no trades on misread equity, human escalation, no self-reset.
