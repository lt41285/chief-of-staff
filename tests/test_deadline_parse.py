from datetime import date

from chief_of_staff.services.deadline_parse import parse_natural_deadline, strip_deadline_phrase
from chief_of_staff.services.relative_deadline import resolve_deadline_expression

TODAY = date(2026, 9, 2)  # Wednesday


def test_relative_and_weekday_dates() -> None:
    assert parse_natural_deadline("завтра", TODAY) == date(2026, 9, 3)
    assert parse_natural_deadline("tomorrow", TODAY) == date(2026, 9, 3)
    assert parse_natural_deadline("на п'ятницю", TODAY) == date(2026, 9, 4)
    assert parse_natural_deadline("до наступного понеділка", TODAY) == date(2026, 9, 7)
    assert parse_natural_deadline("на 5 вересня", TODAY) == date(2026, 9, 5)
    assert parse_natural_deadline("until September 6", TODAY) == date(2026, 9, 6)


def test_ordinal_day_month() -> None:
    assert parse_natural_deadline("тридцяте вересня", TODAY) == date(2026, 9, 30)
    assert parse_natural_deadline("тридцятого вересня", TODAY) == date(2026, 9, 30)
    assert parse_natural_deadline("першого жовтня", TODAY) == date(2026, 10, 1)
    assert parse_natural_deadline("двадцять перше вересня", TODAY) == date(2026, 9, 21)
    assert parse_natural_deadline("на тридцяте вересня 2026", TODAY) == date(2026, 9, 30)
    assert resolve_deadline_expression("тридцяте вересня", TODAY) == date(2026, 9, 30)


def test_dotted_numeric_dates() -> None:
    assert parse_natural_deadline("30.09.26", TODAY) == date(2026, 9, 30)
    assert parse_natural_deadline("30.09.2026", TODAY) == date(2026, 9, 30)
    assert parse_natural_deadline("30/09/26", TODAY) == date(2026, 9, 30)
    assert parse_natural_deadline("05.10.26", TODAY) == date(2026, 10, 5)
    assert parse_natural_deadline("01.09.26", TODAY) == date(2027, 9, 1)
    assert resolve_deadline_expression("30.09.26", TODAY) == date(2026, 9, 30)


def test_dotted_invalid_date_returns_none() -> None:
    assert parse_natural_deadline("31.02.26", TODAY) is None
    assert resolve_deadline_expression("31.02.26", TODAY) is None


def test_period_deadline_phrases() -> None:
    assert parse_natural_deadline("до кінця місяця", TODAY) == date(2026, 9, 30)
    assert parse_natural_deadline("до кінця тижня", TODAY) == date(2026, 9, 6)
    assert parse_natural_deadline("на початку наступного тижня", TODAY) == date(2026, 9, 7)
    assert parse_natural_deadline("до кінця наступного місяця", TODAY) == date(2026, 10, 31)
    assert resolve_deadline_expression("на початку наступного тижня", TODAY) == date(2026, 9, 7)


def test_strip_leaves_task_reference() -> None:
    deadline, rest = strip_deadline_phrase("Перенеси задачу про бюджет на п'ятницю", TODAY)
    assert deadline == date(2026, 9, 4)
    assert "бюджет" in rest.casefold()
    assert "п'ятниц" not in rest.casefold()
