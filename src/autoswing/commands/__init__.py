"""CLI command handlers, split by family.

- trading: broker-backed commands (orders, gate, positions, benchmark)
- shadow: virtual books (v2 news-catalyst + wide-PEAD measurement)
- research: data scans, forecast experiment, skip ledger, backtest

cli.py owns the argparse surface and routes here. Handlers keep their
historical leading-underscore names — they are internal to the CLI, and
identical names keep the 2026-08-11 split visibly mechanical in the diff.
"""
