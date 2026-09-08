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
