"""Ukrainian reminder copy."""

from datetime import date

from chief_of_staff.models.reminder import ReminderType

_MONTHS = {
    1: "січня",
    2: "лютого",
    3: "березня",
    4: "квітня",
    5: "травня",
    6: "червня",
    7: "липня",
    8: "серпня",
    9: "вересня",
    10: "жовтня",
    11: "листопада",
    12: "грудня",
}


def format_uk_date(day: date) -> str:
    return f"{day.day} {_MONTHS[day.month]}"


def format_reminder_message(
    *,
    reminder_type: ReminderType,
    title: str,
    project: str,
    deadline: date,
    estimated_minutes: int,
) -> str:
    body = (
        f"📋 {title}\n"
        f"📁 {project}\n"
    )
    if reminder_type is ReminderType.OVERDUE:
        header = "🚨 Прострочено"
        body += f"📅 Дедлайн був: {format_uk_date(deadline)}\n"
    elif reminder_type is ReminderType.DUE_TODAY:
        header = "⚠️ Дедлайн сьогодні"
    else:
        header = "⏰ Нагадування"
        body += f"📅 Дедлайн: {format_uk_date(deadline)}\n"
    body += f"⏱ {estimated_minutes} хв"
    return f"{header}\n\n{body}"
