from chief_of_staff.services.available_time import parse_available_time


def test_parses_one_hour_nominative() -> None:
    assert parse_available_time("1 година").minutes == 60


def test_parses_decimal_hours() -> None:
    assert parse_available_time("2.5 hours").minutes == 150
    assert parse_available_time("2,5 год").minutes == 150


def test_parses_minutes() -> None:
    assert parse_available_time("90 хвилин").minutes == 90


def test_parses_short_hours() -> None:
    assert parse_available_time("4 год").minutes == 240


def test_parses_hours_and_minutes() -> None:
    assert parse_available_time("3 год 30 хв").minutes == 210


def test_parses_word_hours_from_voice() -> None:
    assert parse_available_time("три години").minutes == 180
    assert parse_available_time("У мене три години вільного часу").minutes == 180


def test_parses_half_and_one_and_a_half() -> None:
    assert parse_available_time("пів години").minutes == 30
    assert parse_available_time("півтори години").minutes == 90


def test_ambiguous_without_unit_asks_clarification() -> None:
    parsed = parse_available_time("3")
    assert parsed.minutes is None
    assert parsed.clarify is not None


def test_two_different_durations_are_ambiguous() -> None:
    parsed = parse_available_time("2 години або 5 годин")
    assert parsed.minutes is None
    assert parsed.clarify is not None
