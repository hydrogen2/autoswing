"""Research/data CLI commands: scans, forecast experiment, skip ledger,
backtest. No broker connection — these run on public data and local state."""

import json
import sys

from ..journal import Journal


def _dispatch_data(config, journal: Journal, args):
    if args.command == "scan-candidates":
        from ..data.candidates import scan

        result = scan(config.risk, days_back=args.days_back, min_move_pct=args.min_move)
        journal.record(
            "data.scan_candidates",
            scanned=result["scanned"], passing=result["passing"],
            price_data_missing=result["price_data_missing"],
            price_data_missing_symbols=result["price_data_missing_symbols"],
            symbols=[c["symbol"] for c in result["candidates"]],
        )
        return result
    if args.command == "next-earnings":
        from ..data.earnings import next_earnings_date

        return {"symbol": args.symbol.upper(),
                "next_earnings_date": next_earnings_date(args.symbol)}
    if args.command == "scan-movers":
        from ..config import PROJECT_ROOT
        from ..data.movers import scan_movers

        result = scan_movers(config.risk, min_move_pct=args.min_move,
                             state_dir=PROJECT_ROOT / "state" / "shadow")
        journal.record("shadow.scan_movers", scanned=result["scanned"],
                       passing=result["passing"],
                       symbols=[c["symbol"] for c in result["candidates"]],
                       prior_session_recheck=[
                           {k: r.get(k) for k in ("symbol", "session",
                                                  "volume_verdict", "status")}
                           for r in result.get("prior_session_recheck", [])],
                       pending_recheck=result.get("pending_recheck", []))
        return result
    if args.command == "shadow-mark":
        from .shadow import _shadow_mark

        return _shadow_mark(config, journal)
    if args.command == "shadow-status":
        from .shadow import _shadow_status

        return _shadow_status()
    if args.command == "scan-upcoming":
        return _scan_upcoming(args.days, journal)
    if args.command == "forecast-log":
        return _forecast_log(args, journal)
    if args.command == "forecast-score":
        return _forecast_score(journal)
    if args.command == "forecast-stats":
        from ..config import PROJECT_ROOT
        from ..forecast import compute_stats, load_jsonl

        d = PROJECT_ROOT / "state" / "forecast"
        return compute_stats(load_jsonl(d / "forecasts.jsonl"),
                             load_jsonl(d / "scores.jsonl"))
    if args.command == "exit-counterfactuals":
        from ..config import PROJECT_ROOT
        from ..data.prices import fetch_history
        from ..research import compare_exit_rules, extract_live_trades

        trades = extract_live_trades(PROJECT_ROOT / "journal")
        history = fetch_history(sorted({t.symbol for t in trades}), period="6mo")
        comparison = compare_exit_rules(trades, history)
        journal.record("research.exit_counterfactuals", summary={
            name: {k: v for k, v in stats.items() if k != "results"}
            for name, stats in comparison.items()
        })
        return comparison
    if args.command == "fill-quality":
        from ..config import PROJECT_ROOT
        from ..research import fill_quality

        result = fill_quality(PROJECT_ROOT / "journal")
        journal.record("research.fill_quality", summary={
            k: {m: v for m, v in result[k].items() if m != "fills"}
            for k in ("entries", "stops", "targets", "market_exits")
        } | {"unmatched": result["unmatched"]})
        return result
    if args.command == "log-skip":
        return _log_skip(args, journal)
    if args.command == "backtest":
        return _backtest(config, journal, args)
    if args.command == "trim-compare":
        return _trim_compare(journal, args)
    if args.command == "signal-ingest":
        return _signal_ingest(journal, args)
    if args.command == "signal-log":
        return _signal_log(args, journal)
    if args.command == "signal-score":
        return _signal_score(journal)
    if args.command == "signal-stats":
        from ..signals import aggregate, load_jsonl

        scores = load_jsonl(_signal_paths()[1])
        if args.source:
            scores = [s for s in scores if s["source"] == args.source]
        return {"scored": len(scores), "by_source": aggregate(scores),
                "caveats": [
                    "alpha is measured from the NEXT session's close after "
                    "disclosure, never the price at the moment of the post",
                    "entries only — disclosure is asymmetric, so this asks "
                    "'did the entry predict drift', not 'does copying pay'",
                    "picking whose signals to follow is itself a selection "
                    "on a noisy track record",
                ]}
    if args.command == "lesson-pending":
        return _lesson_pending(config, journal)
    if args.command == "lesson-log":
        return _lesson_log(config, args, journal)
    if args.command == "lessons":
        from ..lessons import lessons_context, load_lessons

        lessons = load_lessons(_lessons_path())
        return {"count": len(lessons),
                "context": lessons_context(lessons, args.symbol),
                "lessons": lessons[-20:]}
    if args.command == "skip-outcomes":
        from ..config import PROJECT_ROOT
        from ..data.prices import fetch_history
        from ..forecast import load_jsonl
        from ..research import score_skips

        skips = load_jsonl(PROJECT_ROOT / "state" / "research" / "skips.jsonl")
        if not skips:
            return {"scored": [], "pending": 0, "by_category": {}}
        history = fetch_history(sorted({s["symbol"] for s in skips}), period="3mo")
        result = score_skips(skips, history)
        # Stop-geometry counterfactual: raw forward return is the wrong unit
        # for a category defined by a wide stop (wide stop = fewer shares =
        # smaller R for the same % move). Replays those declines in R.
        from ..research import replay_stop_geometry_skips

        replay = replay_stop_geometry_skips(
            skips, history,
            int(config.strategy.get("max_hold_days", 15)))
        result["stop_geometry_replay"] = replay
        journal.record("research.skip_outcomes",
                       by_category=result["by_category"],
                       pending=result["pending"],
                       stop_geometry_replay={
                           k: v for k, v in replay.items() if k != "results"})
        return result
    raise ValueError(f"unknown data command {args.command!r}")


def _backtest(config, journal: Journal, args):
    from datetime import date

    from ..backtest import run_backtest
    from ..config import PROJECT_ROOT

    overrides = {}
    if args.min_move is not None:
        overrides["min_move_pct"] = args.min_move
    if args.min_volume_ratio is not None:
        overrides["min_volume_ratio"] = args.min_volume_ratio
    if args.min_surprise is not None:
        overrides["min_surprise_pct"] = args.min_surprise

    result = run_backtest(
        date.fromisoformat(args.start), date.fromisoformat(args.end),
        config.risk, PROJECT_ROOT / "state" / "backtest", overrides,
    )
    out = PROJECT_ROOT / "state" / "backtest" / (
        f"results-{args.start}-{args.end}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    journal.record("research.backtest", range=result["range"],
                   params=result["params"], funnel=result["funnel"],
                   overall=result["overall"], by_year=result["by_year"])
    # full per-trade detail lives in the results file, not stdout
    return {k: v for k, v in result.items() if k != "trades"} | {
        "results_file": str(out)}


def _trim_compare(journal: Journal, args):
    """Every trim rule vs buy-and-hold, per symbol and averaged.

    Reports drawdown alongside return because they are the two halves of the
    real question: trimming a winner almost always COSTS return, so it is
    only justified if it buys enough risk reduction to be worth the price."""
    import statistics as stx

    from ..data.prices import fetch_history
    from ..trim import RULES, simulate

    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    period = "5y" if args.days > 730 else "2y"
    hist = fetch_history(syms, period=period)
    cutoff_days = args.days

    out, missing = {}, [s for s in syms if s not in hist]
    series = {}
    for s in syms:
        if s not in hist:
            continue
        c = hist[s]["Close"]
        c = c[c.index >= c.index[-1] - __import__("pandas").Timedelta(days=cutoff_days)]
        series[s] = [float(x) for x in c]

    for rule in RULES:
        per = {}
        for s, closes in series.items():
            if len(closes) < 30:
                continue
            r = simulate(s, closes, rule, start_shares=args.shares)
            per[s] = {"vs_hold_pct": r.vs_hold_pct, "max_dd_pct": r.max_drawdown_pct,
                      "hold_max_dd_pct": r.hold_max_drawdown_pct,
                      "trades": r.trades, "ending_shares": r.ending_shares,
                      "avg_cost_basis": r.avg_cost_basis,
                      "net_cost_basis": r.net_cost_basis}
        if not per:
            continue
        rets = [v["vs_hold_pct"] for v in per.values()]
        dds = [v["max_dd_pct"] for v in per.values()]
        holds = [v["hold_max_dd_pct"] for v in per.values()]
        out[rule] = {
            "mean_vs_hold_pct": round(stx.mean(rets), 2),
            "median_vs_hold_pct": round(stx.median(rets), 2),
            "beat_hold_count": sum(1 for x in rets if x > 0),
            "n_symbols": len(rets),
            "mean_max_dd_pct": round(stx.mean(dds), 2),
            "mean_hold_max_dd_pct": round(stx.mean(holds), 2),
            "mean_dd_saved_pts": round(stx.mean(holds) - stx.mean(dds), 2),
            "per_symbol": per,
        }
    result = {
        "symbols": list(series), "missing": missing, "window_days": cutoff_days,
        "benchmark": "buy-and-hold the same symbol (NOT VOO)",
        "rules": out,
        "caveats": [
            "basket chosen today = survivorship bias; buy-and-hold is flattered",
            "high-vol growth names are highly correlated — effective sample "
            "size is far below the symbol count",
            "results flip sign across windows; treat regime dependence as the "
            "finding, not as an edge",
            "lowering cost basis and maximising return are different "
            "objectives and conflict on a rising stock",
        ],
    }
    journal.record("research.trim_compare", symbols=list(series),
                   window_days=cutoff_days,
                   summary={k: {m: v[m] for m in
                                ("mean_vs_hold_pct", "beat_hold_count",
                                 "mean_dd_saved_pts")}
                            for k, v in out.items()})
    return result


def _signal_paths():
    from ..config import PROJECT_ROOT
    d = PROJECT_ROOT / "state" / "signals"
    return d / "signals.jsonl", d / "scores.jsonl"


def _signal_ingest(journal: Journal, args):
    """Pull disclosed stakes/holdings from EDGAR into the signal ledger.

    Everything is dated at the FILING date — the first moment the position
    was public. Anything earlier would credit us with knowledge no follower
    had, which is exactly how copy-trade backtests invent returns."""
    from datetime import date, datetime, timedelta, timezone

    from ..config import PROJECT_ROOT
    from ..edgar import (
        HOLDINGS_FORMS, STAKE_FORMS, WATCHLIST, holdings_13f, new_or_increased,
        recent_filings, subject_company, ticker_map,
    )
    from ..signals import (
        append_jsonl, load_jsonl, next_tradeable_session, signal_id,
    )

    since = date.today() - timedelta(days=args.days)
    spath, _ = _signal_paths()
    existing = {s["id"] for s in load_jsonl(spath)}
    tmap = ticker_map(PROJECT_ROOT / "state" / "signals" / "ticker_map.json")

    logged, skipped, unmapped, forms_seen = [], 0, [], set()

    if args.kind in ("stakes", "both"):
        for cik, label in WATCHLIST.items():
            for f in recent_filings(cik, STAKE_FORMS, since, forms_seen):
                subj = subject_company(f)
                if not subj:
                    unmapped.append({"filer": label, "form": f.form,
                                     "date": f.filing_date,
                                     "why": "subject company not parsed"})
                    continue
                name, subj_cik = subj
                # A filer disclosing about ITSELF (buybacks, subsidiary
                # structure) is not a signal about anyone's stock picking.
                if subj_cik == cik:
                    unmapped.append({"filer": label, "subject": name,
                                     "date": f.filing_date,
                                     "why": "self-filing, not a pick"})
                    continue
                # An AMENDMENT (/A) can report an increased, reduced, or
                # exited stake — the form alone does not say which. Calling
                # them all "buy" would have made two-thirds of the first
                # ingest directionally unknown. Only INITIAL 13D/13G filings
                # are unambiguous ("crossed 5%", i.e. accumulated). Parsing
                # amendment percentages is future work, not a guess.
                if f.form.strip().endswith("/A"):
                    unmapped.append({"filer": label, "subject": name,
                                     "date": f.filing_date,
                                     "why": "amendment — direction unknown "
                                            "without parsing the percentage"})
                    continue
                ticker = tmap.get(subj_cik)
                if not ticker:
                    # Not an exchange-listed operating company we can price.
                    unmapped.append({"filer": label, "subject": name,
                                     "date": f.filing_date,
                                     "why": "no ticker for CIK"})
                    continue
                source = f"stake_{label}"
                sid = signal_id(source, ticker, f.filing_date)
                if sid in existing:
                    skipped += 1
                    continue
                entry = {
                    "id": sid, "source": source, "symbol": ticker,
                    "direction": "buy", "signal_date": f.filing_date,
                    "actionable_date": next_tradeable_session(
                        date.fromisoformat(f.filing_date)).isoformat(),
                    "benchmark": "SPY",
                    "note": f"{f.form} on {name} by {f.filer} — INITIAL "
                            f">5% stake crossing, ~10d disclosure deadline",
                    "logged_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"),
                }
                append_jsonl(spath, entry)
                existing.add(sid)
                logged.append({"source": source, "symbol": ticker,
                               "signal_date": f.filing_date, "subject": name})

    if args.kind in ("holdings", "both"):
        # 13F is the CONTROL arm: 45-day lag by construction. Expect ~zero
        # alpha; a large positive reading should discredit the instrument
        # before it flatters the strategy.
        for cik, label in WATCHLIST.items():
            fs = sorted(recent_filings(cik, HOLDINGS_FORMS, since),
                        key=lambda f: f.filing_date)
            for prev_f, curr_f in zip(fs, fs[1:]):
                prev, curr = holdings_13f(prev_f), holdings_13f(curr_f)
                if not curr:
                    unmapped.append({"filer": label, "date": curr_f.filing_date,
                                     "why": "13F table not parsed"})
                    continue
                for chg in new_or_increased(prev, curr):
                    unmapped.append({"filer": label, "issuer": chg["issuer"],
                                     "date": curr_f.filing_date,
                                     "why": "13F reports CUSIP; no free "
                                            "CUSIP->ticker map"})

    result = {
        "logged": len(logged), "already_present": skipped,
        "unmapped": len(unmapped),
        "signals": logged[:40],
        "unmapped_detail": unmapped[:20],
        "forms_seen": sorted(forms_seen)[:12],
        "note": "13D/G subject companies map exactly via CIK. 13F holdings "
                "report CUSIPs with no free ticker map, so they are counted "
                "as unmapped rather than guessed — an unmatched filing must "
                "never read as 'they bought nothing'.",
    }
    if not args.dry_run:
        journal.record("signals.ingested", logged=len(logged),
                       unmapped=len(unmapped), kind=args.kind)
    return result


def _signal_log(args, journal: Journal):
    """Record an external signal. The actionable date is DERIVED from the
    trading calendar, never taken from the payload — that number is the whole
    experiment's integrity and must not be suppliable by whoever logs it."""
    from datetime import date, datetime, timezone

    from ..signals import (
        append_jsonl, load_jsonl, next_tradeable_session, signal_id,
        validate_signal,
    )

    raw = sys.stdin.read() if args.signal == "-" else open(args.signal).read()
    payload = json.loads(raw)
    errs = validate_signal(payload)
    if errs:
        raise ValueError("invalid signal: " + "; ".join(errs))

    source = payload["source"].strip().lower()
    symbol = payload["symbol"].strip().upper()
    sid = signal_id(source, symbol, payload["signal_date"])
    spath, _ = _signal_paths()
    if any(s["id"] == sid for s in load_jsonl(spath)):
        raise ValueError(f"signal {sid} already logged — signals are "
                         "immutable, the first record stands")

    entry = {
        "id": sid, "source": source, "symbol": symbol,
        "direction": payload["direction"],
        "signal_date": payload["signal_date"],
        "actionable_date": next_tradeable_session(
            date.fromisoformat(payload["signal_date"])).isoformat(),
        "benchmark": (payload.get("benchmark") or "SPY").strip().upper(),
        "note": payload["note"].strip(),
        "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    append_jsonl(spath, entry)
    journal.record("signals.logged", **entry)
    return {"logged": sid, "actionable_date": entry["actionable_date"],
            "benchmark": entry["benchmark"]}


def _signal_score(journal: Journal):
    from ..data.prices import fetch_history
    from ..signals import append_jsonl, load_jsonl, score_signal

    spath, scpath = _signal_paths()
    signals = load_jsonl(spath)
    done = {s["signal_id"] for s in load_jsonl(scpath)}
    pending = [s for s in signals if s["id"] not in done]
    if not pending:
        return {"scored": 0, "pending": 0, "total": len(signals)}

    syms = sorted({s["symbol"] for s in pending}
                  | {s.get("benchmark", "SPY") for s in pending})
    hist = fetch_history(syms, period="1y")

    scored, still = [], 0
    for sig in pending:
        df = hist.get(sig["symbol"])
        if df is None:
            still += 1
            continue
        row = score_signal(sig, df, hist.get(sig.get("benchmark", "SPY")))
        if row is None:
            still += 1          # no horizon elapsed yet; try again later
            continue
        append_jsonl(scpath, row)
        scored.append(row)
    journal.record("signals.scored", scored=len(scored), still_pending=still)
    return {"scored": len(scored), "pending": still, "results": scored}


def _lessons_path():
    from ..config import PROJECT_ROOT
    return PROJECT_ROOT / "state" / "research" / "lessons.jsonl"


def _benchmark_closer(config):
    """Returns close(date_str) -> benchmark close on/before that date, or
    None when the series is unavailable. One fetch, reused per call."""
    from datetime import date

    from ..data.prices import fetch_history

    bench = config.strategy.get("benchmark_symbol", "VOO")
    hist = fetch_history([bench], period="6mo").get(bench)

    def bench_close(d: str):
        if hist is None:
            return None
        day = date.fromisoformat(d)
        rows = hist[[ts.date() <= day for ts in hist.index]]
        return float(rows["Close"].iloc[-1]) if len(rows) else None

    return bench_close


def _lesson_pending(config, journal: Journal):
    """Closed trades awaiting a reflection, with realized outcome numbers.
    Deterministic; the brain writes the lesson text via lesson-log."""
    from ..config import PROJECT_ROOT
    from ..lessons import extract_closed_trades, load_lessons, outcome

    done = {l["id"] for l in load_lessons(_lessons_path())}
    closed = [t for t in extract_closed_trades(PROJECT_ROOT / "journal")
              if t.id not in done]
    if not closed:
        return {"pending": []}

    bench_close = _benchmark_closer(config)
    pending = []
    for t in closed:
        pending.append({
            "id": t.id, "symbol": t.symbol, "strategy": t.strategy,
            "placed_date": t.placed_date, "closed_date": t.closed_date,
            "entry_limit": t.entry_limit, "stop_loss": t.stop_loss,
            "take_profit": t.take_profit, "thesis": t.rationale,
            **outcome(t, bench_close(t.placed_date), bench_close(t.closed_date)),
        })
    journal.record("lessons.pending", count=len(pending),
                   ids=[p["id"] for p in pending])
    return {"pending": pending}


def _lesson_log(config, args, journal: Journal):
    from datetime import datetime, timezone

    from ..config import PROJECT_ROOT
    from ..forecast import append_jsonl
    from ..lessons import (
        extract_closed_trades, load_lessons, outcome, validate_lesson,
    )

    raw = sys.stdin.read() if args.lesson == "-" else open(args.lesson).read()
    payload = json.loads(raw)
    errs = validate_lesson(payload)
    if errs:
        raise ValueError("invalid lesson: " + "; ".join(errs))

    sym = payload["symbol"].upper()
    closed = {t.id: t for t in extract_closed_trades(PROJECT_ROOT / "journal")}
    matches = [t for t in closed.values()
               if t.symbol == sym and t.closed_date == payload["closed_date"]]
    if not matches:
        raise ValueError(
            f"no closed {sym} trade on {payload['closed_date']} in the journal "
            "— lessons must anchor to a real close (see lesson-pending)")
    t = matches[0]
    if any(l["id"] == t.id for l in load_lessons(_lessons_path())):
        raise ValueError(f"lesson for {t.id} already exists — lessons are "
                         "immutable, the first call stands")

    # Outcome numbers are computed here, never copied from the brain's
    # payload — the ledger must be trustworthy independent of the prose.
    bench_close = _benchmark_closer(config)
    o = outcome(t, bench_close(t.placed_date), bench_close(t.closed_date))
    entry = {
        "id": t.id, "symbol": sym, "strategy": t.strategy,
        "placed_date": t.placed_date, "closed_date": t.closed_date,
        "exit_kind": t.exit_kind, "r_multiple": o["r_multiple"],
        "return_pct": o["return_pct"],
        "alpha_pct": o["alpha_pct"],
        "thesis_held": payload["thesis_held"],
        "lesson": payload["lesson"].strip(),
        "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    append_jsonl(_lessons_path(), entry)
    journal.record("lessons.logged", **entry)
    return {"logged": t.id, "thesis_held": entry["thesis_held"]}


def _log_skip(args, journal: Journal):
    from datetime import date

    from ..config import PROJECT_ROOT
    from ..forecast import append_jsonl
    from ..research import validate_skip

    raw = sys.stdin.read() if args.skip == "-" else open(args.skip).read()
    payload = json.loads(raw)
    errs = validate_skip(payload)
    if errs:
        raise ValueError("invalid skip: " + "; ".join(errs))
    entry = {
        "symbol": payload["symbol"].upper(),
        "date": payload.get("date", date.today().isoformat()),
        "category": payload["category"],
        "reason": payload["reason"],
    }
    append_jsonl(PROJECT_ROOT / "state" / "research" / "skips.jsonl", entry)
    journal.record("research.skip_logged", **entry)
    return {"logged": f"{entry['symbol']}-{entry['date']}",
            "category": entry["category"]}


def _forecast_paths():
    from ..config import PROJECT_ROOT
    d = PROJECT_ROOT / "state" / "forecast"
    return d / "forecasts.jsonl", d / "scores.jsonl"


def _scan_upcoming(days: int, journal: Journal):
    from datetime import date, timedelta

    from ..data.earnings import fetch_calendar_day
    from ..data.prices import fetch_history

    today = date.today()
    rows = []
    for offset in range(days + 1):
        d = today + timedelta(days=offset)
        if d.weekday() >= 5:
            continue
        for r in fetch_calendar_day(d):
            rows.append({
                "symbol": r.symbol, "company": r.company,
                "report_date": r.report_date, "timing": r.timing,
                "eps_forecast": r.eps_forecast, "num_estimates": r.num_estimates,
                "market_cap": r.market_cap,
            })
    # Enrich the biggest 30 by market cap with liquidity data.
    rows.sort(key=lambda r: r["market_cap"] or 0, reverse=True)
    top = [r["symbol"] for r in rows[:30]]
    history = fetch_history(top, period="1mo")
    for r in rows[:30]:
        df = history.get(r["symbol"])
        if df is not None and len(df) >= 5:
            r["last_close"] = round(float(df["Close"].iloc[-1]), 2)
            r["adv_dollar_20d"] = round(float((df["Close"] * df["Volume"]).mean()), 0)
    journal.record("forecast.scan_upcoming", count=len(rows),
                   enriched=[r["symbol"] for r in rows[:30]])
    return {"count": len(rows), "reporters": rows[:60]}


def _forecast_log(args, journal: Journal):
    from datetime import datetime, timezone

    from ..forecast import (
        Forecast, append_jsonl, forecast_id, load_jsonl, post_hoc_reason,
        validate_forecast,
    )
    from ..risk_gate import ET

    raw = sys.stdin.read() if args.forecast == "-" else open(args.forecast).read()
    payload = json.loads(raw)
    errs = validate_forecast(payload)
    if errs:
        raise ValueError("invalid forecast: " + "; ".join(errs))

    stale = post_hoc_reason(payload["report_date"], payload["timing"],
                            datetime.now(ET))
    if stale:
        raise ValueError(f"refusing to log a post-hoc forecast: {stale}")

    fpath, _ = _forecast_paths()
    fid = forecast_id(payload["symbol"], payload["report_date"])
    if any(f["id"] == fid for f in load_jsonl(fpath)):
        raise ValueError(
            f"forecast {fid} already exists — predictions are immutable, "
            "the first call stands"
        )
    fc = Forecast(
        id=fid, symbol=payload["symbol"].upper(),
        made_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        report_date=payload["report_date"], timing=payload["timing"],
        tier=payload["tier"], eps_call=payload["eps_call"],
        reaction_call=payload.get("reaction_call"),
        confidence=float(payload["confidence"]),
        reasoning=payload["reasoning"],
    )
    from dataclasses import asdict
    append_jsonl(fpath, asdict(fc))
    journal.record("forecast.logged", **asdict(fc))
    return {"logged": fid, "tier": fc.tier}


def _forecast_score(journal: Journal):
    from datetime import date, datetime, timezone

    from ..data.earnings import fetch_calendar_day
    from ..data.prices import fetch_history, reaction_metrics
    from ..forecast import (
        append_jsonl, awaiting_actuals, load_jsonl, score_forecast,
    )

    fpath, spath = _forecast_paths()
    forecasts = load_jsonl(fpath)
    scored_ids = {s["forecast_id"] for s in load_jsonl(spath)}
    today = date.today()
    due = [f for f in forecasts
           if f["id"] not in scored_ids
           and date.fromisoformat(f["report_date"]) <= today]
    if not due:
        return {"scored": 0, "pending_future": len(forecasts) - len(scored_ids)}

    calendar_cache: dict[str, dict] = {}
    history = fetch_history(sorted({f["symbol"] for f in due}), period="1mo")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results, still_pending = [], 0
    for f in due:
        rdate = date.fromisoformat(f["report_date"])
        key = f["report_date"]
        if key not in calendar_cache:
            calendar_cache[key] = {
                r.symbol: r for r in fetch_calendar_day(rdate)
            }
        report = calendar_cache[key].get(f["symbol"])
        surprise = report.surprise_pct if report else None

        df = history.get(f["symbol"])
        reaction = (reaction_metrics(f["symbol"], df, rdate, f["timing"])
                    if df is not None else None)
        move = reaction.move_pct if reaction else None

        grace_expired = (today - rdate).days > 5
        if awaiting_actuals(surprise, move, grace_expired):
            still_pending += 1  # a leg is still unpublished; retry next run
            continue
        entry = score_forecast(f, surprise, move, now)
        append_jsonl(spath, entry)
        results.append({k: entry[k] for k in
                        ("forecast_id", "eps_correct", "reaction_correct",
                         "eps_actual", "reaction_actual", "scorable")})
    journal.record("forecast.scored", scored=len(results),
                   awaiting_actuals=still_pending, results=results)
    return {"scored": len(results), "awaiting_actuals": still_pending,
            "results": results}
