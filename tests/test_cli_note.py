"""Regression: journal-note must honor the `-`=stdin convention.

On 2026-07-28 the brain piped three digests as `... | journal-note -`,
expecting the same `-`=stdin behavior propose-trade already provides.
journal-note instead recorded the literal note "-", silently dropping
each digest (recovered only because the brain noticed and re-posted).
The journal is the audit's only record of the brain's reasoning — a
dropped digest is a data-loss bug.
"""

import io

from autoswing.cli import _resolve_note


class TestResolveNote:
    def test_dash_reads_stdin(self):
        note = _resolve_note("-", stdin=io.StringIO("PRECLOSE DIGEST\nbody\n"))
        assert note == "PRECLOSE DIGEST\nbody"

    def test_dash_never_records_literal_dash(self):
        assert _resolve_note("-", stdin=io.StringIO("real digest")) != "-"

    def test_plain_note_passthrough(self):
        assert _resolve_note("MIDDAY quiet, healthy book") == (
            "MIDDAY quiet, healthy book"
        )

    def test_plain_note_ignores_stdin(self):
        note = _resolve_note("literal note", stdin=io.StringIO("should be ignored"))
        assert note == "literal note"


class TestJsonWrapperUnwrap:
    """Regression: on 2026-09-21 the brain piped the JSON request body
    {"note": "..."} instead of bare text and the journal stored the wrapper
    verbatim. The exact single-key {"note": <str>} shape is unwrapped; any
    other JSON-looking note is stored as-is."""

    def test_unwraps_json_note_wrapper(self):
        assert _resolve_note('{"note": "ENTRY WINDOW digest"}') == (
            "ENTRY WINDOW digest"
        )

    def test_unwraps_wrapper_from_stdin(self):
        note = _resolve_note("-", stdin=io.StringIO('{"note": "piped digest"}\n'))
        assert note == "piped digest"

    def test_unwraps_double_wrap(self):
        double = '{"note": "{\\"note\\": \\"inner digest\\"}"}'
        assert _resolve_note(double) == "inner digest"

    def test_other_json_object_kept_verbatim(self):
        raw = '{"note": "x", "extra": 1}'
        assert _resolve_note(raw) == raw

    def test_non_string_note_value_kept_verbatim(self):
        raw = '{"note": 42}'
        assert _resolve_note(raw) == raw

    def test_json_array_kept_verbatim(self):
        assert _resolve_note('["a", "b"]') == '["a", "b"]'

    def test_plain_text_with_braces_kept(self):
        raw = "digest mentions {\"note\": style} informally"
        assert _resolve_note(raw) == raw
