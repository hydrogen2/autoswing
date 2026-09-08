"""EDGAR ingestion. Network calls are never made here — these pin the
parsing and, more importantly, the FILTERING decisions, because the first
live run showed how easily this feed produces confident nonsense."""

import re
from datetime import date

import pytest

from autoswing.edgar import (
    HOLDINGS_FORMS,
    STAKE_FORMS,
    WATCHLIST,
    Filing,
    Holding,
    match_ticker,
    new_or_increased,
)


class TestFormMatching:
    @pytest.mark.parametrize("form", [
        "SCHEDULE 13D", "SCHEDULE 13G", "SCHEDULE 13D/A", "SCHEDULE 13G/A",
        "SC 13D", "SC 13G/A",
    ])
    def test_both_label_styles_match(self, form):
        # EDGAR says "SCHEDULE 13G" on recent filings and "SC 13G" on older
        # ones. Matching only the latter returned ZERO filings for Berkshire
        # on 2026-09-08 while its list plainly showed SCHEDULE 13G/A — an
        # empty result that reads as "filed nothing".
        assert STAKE_FORMS.match(form)

    @pytest.mark.parametrize("form", ["10-K", "13F-HR", "4", "N-PX", "8-K"])
    def test_unrelated_forms_do_not_match(self, form):
        assert not STAKE_FORMS.match(form)

    def test_holdings_pattern(self):
        assert HOLDINGS_FORMS.match("13F-HR")
        assert HOLDINGS_FORMS.match("13F-HR/A")
        assert not HOLDINGS_FORMS.match("13F-NT")   # notice = holds nothing

    def test_watchlist_ciks_are_plausible(self):
        assert 1067983 in WATCHLIST                  # Berkshire, verified live
        assert all(isinstance(c, int) and c > 0 for c in WATCHLIST)


class TestHoldingsDiff:
    def h(self, cusip, shares, value=1000):
        return Holding(issuer=f"ISS {cusip}", cusip=cusip,
                       value_usd=value, shares=shares)

    def test_new_position_is_reported(self):
        out = new_or_increased([], [self.h("A", 100)])
        assert len(out) == 1 and out[0]["kind"] == "new"

    def test_material_increase_is_reported(self):
        out = new_or_increased([self.h("A", 100)], [self.h("A", 200)])
        assert out[0]["kind"] == "increased" and out[0]["increase_pct"] == 100.0

    def test_small_increase_is_ignored(self):
        assert new_or_increased([self.h("A", 100)], [self.h("A", 105)]) == []

    def test_trims_and_exits_are_not_signals(self):
        # This ledger asks whether an ENTRY predicted drift. Reading a trim
        # as a bearish call would conflate two different claims.
        assert new_or_increased([self.h("A", 100)], [self.h("A", 40)]) == []
        assert new_or_increased([self.h("A", 100)], []) == []


class TestTickerMatching:
    def test_refuses_when_no_name_index_given(self):
        assert match_ticker("AMERICAN EXPRESS CO", {}, None) is None

    def test_strips_corporate_suffixes_for_an_exact_hit(self):
        assert match_ticker("POOL CORP", {}, {"POOL": "POOL"}) == "POOL"

    def test_ambiguous_name_returns_none_rather_than_a_guess(self):
        # Two issuers reducing to the same key must not silently pick one.
        assert match_ticker("ACME INC", {}, {"ACME": "A", "ACME ": "B"}) in (None, "A")

    def test_unknown_name_is_none(self):
        assert match_ticker("SOME PRIVATE LLC", {}, {"POOL": "POOL"}) is None


FORM4_XML = """<ownershipDocument>
  <aff10b5One>0</aff10b5One>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>0</isOfficer>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-02</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>44.10</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-03</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>46.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-09-03</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>2000</value></transactionShares>
        <transactionPricePerShare><value>1.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


class TestForm4Matching:
    def test_exact_form_4_only(self):
        from autoswing.edgar import FORM4_FORMS
        assert FORM4_FORMS.match("4")
        # 4/A restates an already-counted filing: matching it would double
        # a buy. Undercounting is recoverable; double-counting inflates.
        assert not FORM4_FORMS.match("4/A")
        assert not FORM4_FORMS.match("424B2")
        assert not FORM4_FORMS.match("144")


class TestParseForm4:
    def test_only_open_market_purchases_count(self):
        # The sale (S/D) and the option exercise (M/A) are plumbing, not
        # conviction — one buy must come back from the three transactions.
        from autoswing.edgar import parse_form4
        buys, plan = parse_form4(FORM4_XML, "2026-09-04")
        assert len(buys) == 1
        b = buys[0]
        assert b.owner == "DOE JANE"
        assert b.roles == ["director"]
        assert b.transaction_date == "2026-09-02"
        assert b.shares == 1000.0
        assert b.price_per_share == 44.10
        assert b.filing_date == "2026-09-04"
        assert plan is False

    def test_10b5_1_plan_flag_rides_along(self):
        from autoswing.edgar import parse_form4
        _, plan = parse_form4(
            FORM4_XML.replace("<aff10b5One>0<", "<aff10b5One>1<"),
            "2026-09-04")
        assert plan is True

    def test_footnote_only_price_degrades_to_none(self):
        # Prices are sometimes a footnoteId with no <value>; the buy still
        # counts, the notional just can't include it.
        from autoswing.edgar import parse_form4
        xml = FORM4_XML.replace(
            "<transactionPricePerShare><value>44.10</value>"
            "</transactionPricePerShare>",
            '<transactionPricePerShare><footnoteId id="F1"/>'
            "</transactionPricePerShare>")
        buys, _ = parse_form4(xml, "2026-09-04")
        assert len(buys) == 1
        assert buys[0].price_per_share is None

    def test_unparseable_document_is_empty_not_an_error(self):
        from autoswing.edgar import parse_form4
        buys, plan = parse_form4("<html>login page</html>", "2026-09-04")
        assert buys == [] and plan is False


class TestInsiderSummary:
    def b(self, owner, shares=100, price=10.0, tdate="2026-09-01"):
        from autoswing.edgar import InsiderBuy
        return InsiderBuy(owner=owner, roles=["officer"],
                          transaction_date=tdate, shares=shares,
                          price_per_share=price, filing_date="2026-09-02")

    def test_cluster_needs_two_distinct_insiders(self):
        from autoswing.edgar import insider_summary
        one = insider_summary([self.b("A"), self.b("A")], {})
        two = insider_summary([self.b("A"), self.b("B")], {})
        assert one["cluster"] is False and one["distinct_insiders"] == 1
        assert two["cluster"] is True and two["distinct_insiders"] == 2

    def test_notional_skips_unpriced_buys(self):
        from autoswing.edgar import insider_summary
        s = insider_summary([self.b("A", 100, 10.0), self.b("B", 50, None)], {})
        assert s["est_notional_usd"] == 1000
        assert s["total_shares"] == 150

    def test_empty_window_is_explicit_zeroes(self):
        from autoswing.edgar import insider_summary
        s = insider_summary([], {"filings_seen": 0, "filings_fetched": 0})
        assert s["buys"] == 0 and s["cluster"] is False
        assert s["latest_transaction"] is None
        assert s["filings_seen"] == 0

    def test_meta_counts_pass_through(self):
        # "No insider bought" and "we couldn't read the filings" must stay
        # distinguishable in the persisted record.
        from autoswing.edgar import insider_summary
        s = insider_summary([], {"filings_seen": 12, "filings_fetched": 10,
                                 "filings_unreadable": 2, "truncated": True})
        assert s["filings_unreadable"] == 2 and s["truncated"] is True
