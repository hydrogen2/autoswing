"""Regression: CLI failures must journal a usable error message.

On 2026-07-13 the gateway was down during an account reset and three
cli.error journal entries recorded error="" because str(TimeoutError())
is empty. The journal is the only forensic record — blank errors are bugs.
"""

import pytest

from autoswing.cli import _build_proposal, _error_text


class TestBuildProposal:
    """Regression: on 2026-08-06 a proposal payload missing "action" surfaced
    as TypeError("TradeProposal.__init__() missing 1 required positional
    argument: 'action'") — the error must name the field in plain terms."""

    PAYLOAD = {
        "symbol": "VOYG", "action": "BUY", "quantity": 140,
        "entry_limit": 34.9, "stop_loss": 32.3, "take_profit": 40.3,
    }

    def test_valid_payload_builds(self):
        p = _build_proposal(dict(self.PAYLOAD))
        assert p.symbol == "VOYG" and p.action == "BUY"

    def test_missing_required_field_named(self):
        payload = dict(self.PAYLOAD)
        del payload["action"]
        with pytest.raises(ValueError, match=r"missing required field\(s\): action"):
            _build_proposal(payload)

    def test_multiple_missing_fields_all_named(self):
        with pytest.raises(ValueError) as e:
            _build_proposal({"symbol": "XOM"})
        for field in ("action", "quantity", "entry_limit", "stop_loss",
                      "take_profit"):
            assert field in str(e.value)

    def test_unknown_field_named(self):
        payload = dict(self.PAYLOAD, sotp_loss=1.0)
        with pytest.raises(ValueError, match=r"unknown field\(s\): sotp_loss"):
            _build_proposal(payload)

    def test_optional_fields_still_optional(self):
        p = _build_proposal(dict(self.PAYLOAD, strategy="news-v2",
                                 next_earnings_date="none"))
        assert p.strategy == "news-v2"

    def test_error_is_valueerror_not_typeerror(self):
        payload = dict(self.PAYLOAD)
        del payload["action"]
        with pytest.raises(ValueError):
            _build_proposal(payload)


class TestErrorText:
    def test_plain_message_preserved(self):
        assert _error_text(ValueError("bad proposal")) == "bad proposal"

    def test_empty_timeout_falls_back_to_repr(self):
        assert _error_text(TimeoutError()) == "TimeoutError()"

    def test_empty_connection_error_falls_back_to_repr(self):
        assert _error_text(ConnectionError()) == "ConnectionError()"

    def test_never_empty(self):
        for exc in (TimeoutError(), OSError(), RuntimeError(), Exception()):
            assert _error_text(exc)
