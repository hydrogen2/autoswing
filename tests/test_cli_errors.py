"""Regression: CLI failures must journal a usable error message.

On 2026-07-13 the gateway was down during an account reset and three
cli.error journal entries recorded error="" because str(TimeoutError())
is empty. The journal is the only forensic record — blank errors are bugs.
"""

from autoswing.cli import _error_text


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
