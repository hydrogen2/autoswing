"""EDGAR ingestion for the external-signal ledger.

Feeds signals automatically so nobody has to hand-log anything. Two filing
types, chosen because their disclosure lag differs by a factor of four and
that difference is itself the experiment:

  SC 13D / 13G  — a >5% stake, due within ~10 days. The TIMELY way to follow
                  a famous investor. The SGML header names the subject
                  company and its CIK outright, so ticker mapping is exact.

  13F-HR        — the full quarterly book, filed up to 45 days after period
                  end (verified live: Berkshire's Q2 book, period 06-30,
                  filed 08-14). Deliberately included as a CONTROL. If the
                  ledger reports no alpha here, that is the instrument
                  working; if it reports large alpha, distrust the
                  instrument before celebrating.

Signals are dated at the FILING date, never the period-end or trade date —
that is the first moment the information was public, and anything earlier
would credit us with knowledge no follower had.

Politeness: SEC asks for a descriptive User-Agent and <=10 req/s. We stay
far below that and cache aggressively; the data is immutable once filed.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

UA = "autoswing-research weizhiwei@gmail.com"
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
PACING_S = 0.3           # ~3 req/s, well under SEC's limit

# Filers worth following. CIKs verified against EDGAR 2026-09-08.
WATCHLIST = {
    1067983: "berkshire",        # Berkshire Hathaway (Buffett)
    1649339: "scion",            # Scion Asset Management (Burry)
    1336528: "pershing_square",  # Pershing Square (Ackman)
    1536411: "duquesne",         # Duquesne Family Office (Druckenmiller)
}

# EDGAR labels these "SCHEDULE 13D" in recent filings but "SC 13D" in older
# ones, so match by PATTERN not by exact string. Using exact names returned
# zero filings for Berkshire on 2026-09-08 while its list plainly showed
# SCHEDULE 13G/A — an empty result that reads as "they filed nothing"
# instead of "the filter never matched". Same failure family as the rest of
# this codebase's staleness bugs, so recent_filings() now also reports which
# forms it DID see when nothing matched.
STAKE_FORMS = re.compile(r"^(SC|SCHEDULE)\s*13[DG](/A)?$", re.I)
HOLDINGS_FORMS = re.compile(r"^13F-HR(/A)?$", re.I)


@dataclass
class Filing:
    cik: int
    filer: str
    form: str
    filing_date: str
    accession: str

    @property
    def acc_nodash(self) -> str:
        return self.accession.replace("-", "")


def _get(url: str, byte_range: str | None = None) -> str:
    h = dict(HEADERS)
    if byte_range:
        h["Range"] = f"bytes={byte_range}"
    r = requests.get(url, headers=h, timeout=30)
    r.raise_for_status()
    time.sleep(PACING_S)
    return r.text


def ticker_map(cache: Path | None = None) -> dict[int, str]:
    """CIK -> ticker for operating companies. Cached; the file is ~10k rows."""
    if cache and cache.exists():
        return {int(k): v for k, v in json.loads(cache.read_text()).items()}
    raw = json.loads(_get("https://www.sec.gov/files/company_tickers.json"))
    out = {int(r["cik_str"]): r["ticker"] for r in raw.values()}
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({str(k): v for k, v in out.items()}))
    return out


def recent_filings(cik: int, forms, since: date,
                   seen: set | None = None) -> list[Filing]:
    """Filings matching `forms` (a compiled pattern) since a date.

    `seen` collects every form label encountered in the window. A caller that
    gets [] can then say whether the filer was quiet or the pattern is wrong
    — the distinction that cost this module its first live run.
    """
    d = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))
    name = d.get("name", str(cik))
    r = d["filings"]["recent"]
    out = []
    for form, fdate, acc in zip(r["form"], r["filingDate"], r["accessionNumber"]):
        if date.fromisoformat(fdate) < since:
            continue
        if seen is not None:
            seen.add(form)
        if forms.match(form.strip()):
            out.append(Filing(cik, name, form, fdate, acc))
    return out


def subject_company(f: Filing) -> tuple[str, int] | None:
    """(name, cik) of the company a 13D/G is ABOUT, from the SGML header.

    A range request keeps this to a few KB rather than pulling whole filings.
    Returns None rather than guessing when the header is not in the expected
    shape — an unparsed filing must be counted, never silently skipped.
    """
    url = (f"https://www.sec.gov/Archives/edgar/data/{f.cik}/"
           f"{f.acc_nodash}/{f.accession}.txt")
    try:
        head = _get(url, byte_range="0-4000")
    except Exception:
        return None
    m = re.search(
        r"SUBJECT COMPANY:.*?COMPANY CONFORMED NAME:\s*(.+?)\s*\n"
        r".*?CENTRAL INDEX KEY:\s*(\d+)", head, re.S)
    return (m.group(1).strip(), int(m.group(2))) if m else None


@dataclass
class Holding:
    issuer: str
    cusip: str
    value_usd: int
    shares: int


def holdings_13f(f: Filing) -> list[Holding]:
    """Positions from a 13F information table. Returns [] when the table
    cannot be located — the caller reports that, it is never a silent zero."""
    base = f"https://www.sec.gov/Archives/edgar/data/{f.cik}/{f.acc_nodash}/"
    try:
        listing = _get(base)
    except Exception:
        return []
    xmls = [x for x in set(re.findall(r'[\w-]+\.xml', listing))
            if "primary_doc" not in x]
    for name in xmls:
        try:
            body = _get(base + name)
        except Exception:
            continue
        rows = re.findall(
            r"<nameOfIssuer>(.*?)</nameOfIssuer>.*?<cusip>(.*?)</cusip>"
            r".*?<value>(.*?)</value>.*?<sshPrnamt>(.*?)</sshPrnamt>",
            body, re.S)
        if rows:
            merged: dict[str, Holding] = {}
            for issuer, cusip, value, shares in rows:
                # A manager may report one issuer across several rows (voting
                # classes, multiple sub-advisers). Summing avoids reading a
                # split position as a smaller one.
                key = cusip.strip()
                h = merged.get(key)
                if h:
                    h.value_usd += int(value)
                    h.shares += int(float(shares))
                else:
                    merged[key] = Holding(issuer.strip(), key, int(value),
                                          int(float(shares)))
            return list(merged.values())
    return []


def new_or_increased(prev: list[Holding], curr: list[Holding],
                     min_increase_pct: float = 25.0) -> list[dict]:
    """Positions opened, or grown by at least min_increase_pct in shares.

    Trims and exits are ignored on purpose: this ledger measures whether an
    ENTRY predicted drift. Reading a trim as a bearish signal would conflate
    two very different claims (see signals.py).
    """
    before = {h.cusip: h for h in prev}
    out = []
    for h in curr:
        p = before.get(h.cusip)
        if p is None:
            out.append({"issuer": h.issuer, "cusip": h.cusip, "kind": "new",
                        "shares": h.shares, "value_usd": h.value_usd})
        elif p.shares > 0 and h.shares >= p.shares * (1 + min_increase_pct / 100):
            out.append({"issuer": h.issuer, "cusip": h.cusip, "kind": "increased",
                        "shares": h.shares, "prev_shares": p.shares,
                        "value_usd": h.value_usd,
                        "increase_pct": round(100 * (h.shares / p.shares - 1), 1)})
    return out


def match_ticker(issuer: str, tmap: dict[int, str],
                 names: dict[str, str] | None = None) -> str | None:
    """Best-effort issuer-name -> ticker. Returns None when unsure.

    13F reports CUSIPs, and there is no free CUSIP->ticker map, so this falls
    back to name matching and DELIBERATELY refuses ambiguous cases. Callers
    must report the unmatched count: silently dropping them would understate
    a filer's activity and read as 'they bought nothing'.
    """
    if not names:
        return None
    key = re.sub(r"[^A-Z ]", "", issuer.upper())
    key = re.sub(r"\b(INC|CORP|CO|CORPORATION|COMPANY|LTD|PLC|CLASS [A-C]|"
                 r"COM|NEW|HLDGS|HOLDINGS|GROUP|THE)\b", "", key).strip()
    key = re.sub(r"\s+", " ", key)
    hits = [t for n, t in names.items() if n == key]
    return hits[0] if len(hits) == 1 else None


# --- Form 4: insider open-market buying (research instrument #8) ----------
#
# Measurement-only enrichment for PEAD candidates: does cluster buying by
# insiders predict drift? Nothing here influences a decision until ~100
# candidates carry the field and the regression has spoken.
#
# Only form "4" exactly. Amendments (4/A) RESTATE a filing that was already
# counted; matching both would double a buy. Skipping them can miss a buy
# first disclosed in an amendment — accepted: undercounting is recoverable
# at analysis time, double-counting silently inflates the signal.
FORM4_FORMS = re.compile(r"^4$")

# Only transaction code P (open-market purchase) with acquired code A.
# Everything else on a Form 4 — option exercises (M), awards (A), sales (S),
# gifts (G) — is compensation plumbing or disposal, not conviction.
PURCHASE_CODE = "P"


@dataclass
class InsiderBuy:
    owner: str
    roles: list[str]           # director / officer / 10%-owner
    transaction_date: str
    shares: float
    price_per_share: float | None
    filing_date: str


def _xml_value(tag: str, block: str) -> str | None:
    # Bound the <value> search inside the tag's OWN element: a tag holding
    # only a footnoteId must read as None, not as the next element's value.
    m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
    if not m:
        return None
    v = re.search(r"<value>\s*([^<]*?)\s*</value>", m.group(1))
    return (v.group(1) or None) if v else None


def parse_form4(xml: str, filing_date: str) -> tuple[list[InsiderBuy], bool]:
    """Open-market purchases from one Form 4 document.

    Returns (buys, is_10b5_1_plan). Joint filings list several reporting
    owners over the SAME transactions (spouses, trusts); only the first
    owner is credited so a joint filing never counts as a cluster by itself.
    The 10b5-1 checkbox matters because a scheduled plan buy carries far
    less information than a discretionary one — the flag rides along so the
    regression can split them.
    """
    m = re.search(r"<rptOwnerName>\s*(.*?)\s*</rptOwnerName>", xml)
    owner = m.group(1) if m else "unknown"
    roles = []
    for tag, role in (("isDirector", "director"), ("isOfficer", "officer"),
                      ("isTenPercentOwner", "10%-owner")):
        if re.search(rf"<{tag}>\s*(1|true)\s*</{tag}>", xml):
            roles.append(role)
    plan = bool(re.search(r"<aff10b5One>\s*(1|true)\s*</aff10b5One>", xml))

    buys = []
    for block in re.findall(
            r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>",
            xml, re.S):
        code = re.search(r"<transactionCode>\s*(\w)\s*</transactionCode>", block)
        if not code or code.group(1) != PURCHASE_CODE:
            continue
        if _xml_value("transactionAcquiredDisposedCode", block) != "A":
            continue
        shares = _xml_value("transactionShares", block)
        if shares is None:
            continue
        price = _xml_value("transactionPricePerShare", block)
        buys.append(InsiderBuy(
            owner=owner, roles=roles,
            transaction_date=_xml_value("transactionDate", block) or filing_date,
            shares=float(shares),
            price_per_share=float(price) if price else None,
            filing_date=filing_date,
        ))
    return buys, plan


def form4_purchases(cik: int, since: date,
                    max_filings: int = 10) -> tuple[list[InsiderBuy], dict]:
    """All open-market buys filed on Form 4 since a date, newest first.

    The meta dict counts what happened — filings seen vs fetched vs
    unparsed, and whether the list was truncated — so a caller can tell
    "no insider bought" apart from "we didn't look hard enough". A silent
    partial read here would be the renders-as-benign family again.
    """
    filings = recent_filings(cik, FORM4_FORMS, since)
    meta = {"filings_seen": len(filings), "filings_fetched": 0,
            "filings_unreadable": 0,
            "truncated": len(filings) > max_filings, "any_10b5_1": False}
    buys: list[InsiderBuy] = []
    for f in filings[:max_filings]:
        url = (f"https://www.sec.gov/Archives/edgar/data/{f.cik}/"
               f"{f.acc_nodash}/{f.accession}.txt")
        try:
            body = _get(url)
        except Exception:
            meta["filings_unreadable"] += 1
            continue
        meta["filings_fetched"] += 1
        b, plan = parse_form4(body, f.filing_date)
        meta["any_10b5_1"] = meta["any_10b5_1"] or plan
        buys.extend(b)
    return buys, meta


def insider_summary(buys: list[InsiderBuy], meta: dict) -> dict:
    """One JSON-able record per candidate. `cluster` is the hypothesis
    being tested: >=2 DISTINCT insiders buying in the window."""
    owners = sorted({b.owner for b in buys})
    return {
        "buys": len(buys),
        "distinct_insiders": len(owners),
        "cluster": len(owners) >= 2,
        "total_shares": round(sum(b.shares for b in buys), 2),
        "est_notional_usd": round(sum(
            b.shares * b.price_per_share for b in buys if b.price_per_share)),
        "latest_transaction": max(
            (b.transaction_date for b in buys), default=None),
        "roles": sorted({r for b in buys for r in b.roles}),
        **meta,
    }


def cik_for_ticker(ticker: str, cache: Path | None = None) -> int | None:
    """Exact ticker -> CIK, or None. Refuses ambiguity like match_ticker."""
    hits = [cik for cik, t in ticker_map(cache).items()
            if t.upper() == ticker.upper()]
    return hits[0] if len(hits) == 1 else None
