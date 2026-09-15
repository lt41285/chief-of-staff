from datetime import date

from chief_of_staff.services.deadline_parse import parse_natural_deadline, strip_deadline_phrase

TODAY = date(2026, 9, 2)  # Wednesday


def test_relative_and_weekday_dates() -> None:
    assert parse_natural_deadline("завтра", TODAY) == date(2026, 9, 3)
    assert parse_natural_deadline("tomorrow", TODAY) == date(2026, 9, 3)
    assert parse_natural_deadline("на п'ятницю", TODAY) == date(2026, 9, 4)
    assert parse_natural_deadline("до наступного понеділка", TODAY) == date(2026, 9, 7)
    assert parse_natural_deadline("на 5 вересня", TODAY) == date(2026, 9, 5)
    assert parse_natural_deadline("until September 6", TODAY) == date(2026, 9, 6)


def test_strip_leaves_task_reference() -> None:
    deadline, rest = strip_deadline_phrase("Перенеси задачу про бюджет на п'ятницю", TODAY)
    assert deadline == date(2026, 9, 4)
    assert "бюджет" in rest.casefold()
    assert "п'ятниц" not in rest.casefold()
