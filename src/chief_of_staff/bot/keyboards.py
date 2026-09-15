"""Telegram keyboards for task confirmation."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

YES = "task:yes"
EDIT = "task:edit"
CANCEL = "task:cancel"
PLAN_ACCEPT = "plan:accept"
PLAN_REPLAN = "plan:replan"
PLAN_CANCEL = "plan:cancel"
PROJ_CONFIRM = "proj:confirm"
PROJ_CANCEL = "proj:cancel"
PROJ_EXISTING = "proj:existing"
PROJ_CREATE_NEW = "proj:new"
LIFE_DONE = "life:done"
LIFE_CANCEL = "life:cancel"
LIFE_NEW_TASK = "life:new"
LIFE_PICK_PREFIX = "life:pick:"


def confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes", callback_data=YES),
                InlineKeyboardButton("Edit", callback_data=EDIT),
                InlineKeyboardButton("Cancel", callback_data=CANCEL),
            ]
        ]
    )


def plan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Accept plan", callback_data=PLAN_ACCEPT),
                InlineKeyboardButton("🔄 Replan", callback_data=PLAN_REPLAN),
                InlineKeyboardButton("❌ Cancel", callback_data=PLAN_CANCEL),
            ]
        ]
    )


def project_confirm_keyboard(label: str = "✅ Confirm") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=PROJ_CONFIRM),
                InlineKeyboardButton("❌ Скасувати", callback_data=PROJ_CANCEL),
            ]
        ]
    )


def project_disambiguate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Так, цей проєкт", callback_data=PROJ_EXISTING),
            ]
        ]
    )


def complete_confirm_keyboard(label: str = "✅ Виконано") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=LIFE_DONE),
                InlineKeyboardButton("❌ Скасувати", callback_data=LIFE_CANCEL),
            ]
        ]
    )


def statement_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Так, виконано", callback_data=LIFE_DONE)],
            [InlineKeyboardButton("➕ Це нова задача", callback_data=LIFE_NEW_TASK)],
            [InlineKeyboardButton("❌ Скасувати", callback_data=LIFE_CANCEL)],
        ]
    )


def complete_choice_keyboard(count: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(str(index), callback_data=f"{LIFE_PICK_PREFIX}{index - 1}")
        for index in range(1, count + 1)
    ]
    return InlineKeyboardMarkup([buttons])


def statement_choice_keyboard(count: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(str(index), callback_data=f"{LIFE_PICK_PREFIX}{index - 1}")
        for index in range(1, count + 1)
    ]
    return InlineKeyboardMarkup(
        [
            buttons,
            [InlineKeyboardButton("➕ Це нова задача", callback_data=LIFE_NEW_TASK)],
            [InlineKeyboardButton("❌ Скасувати", callback_data=LIFE_CANCEL)],
        ]
    )
