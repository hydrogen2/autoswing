"""gate-status must state the current ET weekday — the brain derived it
itself and got it wrong two days running (09-24 Thursday-as-Wednesday ran
the wheel book off-schedule; 09-25 Friday-as-Thursday)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from autoswing.commands.trading import _now_et

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


class TestNowEt:
    def test_weekday_and_date_spelled_out(self):
        assert (
            _now_et(datetime(2026, 9, 24, 14, 6, tzinfo=ET))
            == "Thursday 2026-09-24 14:06 ET"
        )
        assert (
            _now_et(datetime(2026, 9, 25, 9, 45, tzinfo=ET))
            == "Friday 2026-09-25 09:45 ET"
        )

    def test_utc_input_is_converted_to_et(self):
        # 2026-09-26 01:30 UTC is still Friday 21:30 in New York — the
        # weekday must come from the ET clock, not the wall clock.
        assert (
            _now_et(datetime(2026, 9, 26, 1, 30, tzinfo=UTC))
            == "Friday 2026-09-25 21:30 ET"
        )

    def test_default_now_returns_well_formed_line(self):
        line = _now_et()
        assert line.endswith(" ET")
        assert line.split()[0] in {
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday",
        }
