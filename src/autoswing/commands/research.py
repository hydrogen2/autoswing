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
        from ..data.movers import scan_movers

        result = scan_movers(config.risk, min_move_pct=args.min_move)
        journal.record("shadow.scan_movers", scanned=result["scanned"],
                       passing=result["passing"],
                       symbols=[c["symbol"] for c in result["candidates"]])
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
    if args.command == "log-skip":
        return _log_skip(args, journal)
    if args.command == "backtest":
        return _backtest(config, journal, args)
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
        journal.record("research.skip_outcomes",
                       by_category=result["by_category"],
                       pending=result["pending"])
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
        Forecast, append_jsonl, forecast_id, load_jsonl, validate_forecast,
    )

    raw = sys.stdin.read() if args.forecast == "-" else open(args.forecast).read()
    payload = json.loads(raw)
    errs = validate_forecast(payload)
    if errs:
        raise ValueError("invalid forecast: " + "; ".join(errs))

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
        reaction_call=payload["reaction_call"],
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
