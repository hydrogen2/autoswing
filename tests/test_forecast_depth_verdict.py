"""Pre-registered deep-vs-quick verdict (owner approved 2026-10-19).

The rule is closed to revision; these tests pin it. Each case flips the
verdict by changing exactly one thing, so a silently broken comparator
cannot pass.
"""
from autoswing.forecast import (
    DEPTH_PREMIUM_PP,
    DEPTH_VERDICT_N,
    compute_stats,
    depth_verdict,
)


def _score(tier, i, correct, ts=None, **kw):
    row = {"forecast_id": f"{tier.upper()}{i}-2026-01-01", "tier": tier,
           "scored_at": ts or f"2026-01-{1 + i // 10:02d}T{i % 10:02d}:00:00+00:00",
           "eps_actual": "beat", "eps_correct": correct, "scorable": True,
           "reaction_actual": "not_forecast", "confidence": 0.6}
    row.update(kw)
    return row


def _deep(n, hits):
    return [_score("deep", i, i < hits) for i in range(n)]


def _quick(n, hits, ts=None):
    return [_score("quick", i, i < hits, ts=ts) for i in range(n)]


def test_pending_below_n():
    v = depth_verdict(_deep(99, 70) + _quick(50, 35))
    assert v["verdict"] == "pending"
    assert v["remaining"] == 1
    assert v["deep_n"] == 99
    # Comparator is reported but not frozen while pending.
    assert v["quick_n"] == 50 and "frozen_at" not in v


def test_premium_at_exactly_3pp():
    # deep 73/100, quick 70/100 -> +3.0pp: premium (>= is the registered rule)
    early = "2026-01-01T00:00:00+00:00"
    v = depth_verdict(_deep(100, 73) + _quick(100, 70, ts=early))
    assert v["verdict"] == "depth_premium"
    assert v["margin_pp"] == DEPTH_PREMIUM_PP


def test_no_premium_one_hit_short():
    early = "2026-01-01T00:00:00+00:00"
    v = depth_verdict(_deep(100, 72) + _quick(100, 70, ts=early))
    assert v["verdict"] == "no_depth_premium"
    assert v["margin_pp"] == 2.0


def test_deep_below_quick_is_no_premium():
    early = "2026-01-01T00:00:00+00:00"
    v = depth_verdict(_deep(100, 66) + _quick(100, 70, ts=early))
    assert v["verdict"] == "no_depth_premium"
    assert v["margin_pp"] == -4.0


def test_frozen_at_100th_deep_score_ignores_later_rows():
    early = "2026-01-01T00:00:00+00:00"
    late = "2026-12-31T00:00:00+00:00"
    base = _deep(100, 66) + _quick(100, 70, ts=early)
    v0 = depth_verdict(base)
    # 101st+ deep scores and later quick scores must not move the verdict.
    more = [_score("deep", 200 + i, True, ts=late) for i in range(50)]
    more += [_score("quick", 300 + i, False, ts=late) for i in range(80)]
    v1 = depth_verdict(base + more)
    assert v1["verdict"] == v0["verdict"] == "no_depth_premium"
    assert (v1["deep_n"], v1["quick_n"]) == (DEPTH_VERDICT_N, 100)
    assert v1["frozen_at"] == v0["frozen_at"]


def test_unknown_and_unscorable_rows_excluded():
    rows = _deep(100, 73) + _quick(100, 70, ts="2026-01-01T00:00:00+00:00")
    rows.append(_score("deep", 500, False, eps_actual="unknown"))
    rows.append(_score("quick", 501, False, scorable=False))
    v = depth_verdict(rows)
    assert v["verdict"] == "depth_premium"


def test_no_comparator():
    v = depth_verdict(_deep(100, 73))
    assert v["verdict"] == "no_comparator"


def test_compute_stats_carries_verdict():
    st = compute_stats([], _deep(10, 7) + _quick(10, 7))
    assert st["depth_verdict"]["verdict"] == "pending"
    assert st["depth_verdict"]["registered"] == "2026-10-19"
