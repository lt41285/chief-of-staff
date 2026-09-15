from enum import StrEnum


class ReminderType(StrEnum):
    DAY_BEFORE = "day_before"
    DUE_TODAY = "due_today"
    OVERDUE = "overdue"
