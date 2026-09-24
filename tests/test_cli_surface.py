"""CLI surface smoke tests.

A malformed add_parser() call once left every command unrunnable while the
whole suite stayed green, because nothing built the parser. These tests are
cheap and catch that class of breakage immediately.
"""

from autoswing.cli import DATA_COMMANDS, _build_parser


def test_parser_builds():
    assert _build_parser() is not None


def test_every_data_command_has_a_subparser():
    parser = _build_parser()
    action = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    missing = [c for c in DATA_COMMANDS if c not in action.choices]
    assert not missing, f"declared in DATA_COMMANDS but not registered: {missing}"


def test_no_duplicate_command_registrations():
    parser = _build_parser()
    action = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    names = list(action.choices)
    assert len(names) == len(set(names))


def test_wheel_screen_symbols_are_optional():
    """The weekly run invokes it bare, falling back to the universe file."""
    args = _build_parser().parse_args(["wheel-screen"])
    assert args.symbols is None
