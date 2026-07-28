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
