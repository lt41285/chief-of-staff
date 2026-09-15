"""Telegram adapters for /projects and merge/rename confirmation."""

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from chief_of_staff.bot.keyboards import (
    PROJ_CANCEL,
    PROJ_CONFIRM,
    PROJ_CREATE_NEW,
    PROJ_EXISTING,
    project_confirm_keyboard,
    project_disambiguate_keyboard,
)
from chief_of_staff.models.project_command import ProjectIntent, ProjectIntentKind
from chief_of_staff.services.project_ops import ProjectManagementService, ProjectResult
from chief_of_staff.services.task_intake import IntakeResult, TaskIntakeService


def _projects(context: ContextTypes.DEFAULT_TYPE) -> ProjectManagementService:
    return context.bot_data["project_ops"]


def _intake(context: ContextTypes.DEFAULT_TYPE) -> TaskIntakeService:
    return context.bot_data["task_intake"]


def _chat_id(update: Update) -> int | None:
    if update.effective_chat:
        return update.effective_chat.id
    return None


def project_markup(result: ProjectResult | IntakeResult):
    if isinstance(result, IntakeResult):
        if result.show_disambiguate_buttons:
            return project_disambiguate_keyboard()
        return None
    return project_confirm_keyboard(result.confirm_label) if result.show_confirm_buttons else None


async def on_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    result = await _projects(context).handle_intent(
        update.effective_user.id,
        chat_id,
        ProjectIntent(kind=ProjectIntentKind.LIST_PROJECTS),
    )
    await update.message.reply_text(result.text, reply_markup=project_markup(result))


async def on_newproject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    name = " ".join(context.args or []).strip()
    result = await _projects(context).handle_intent(
        update.effective_user.id,
        chat_id,
        ProjectIntent(kind=ProjectIntentKind.CREATE_PROJECT, new_name=name or None),
    )
    await update.message.reply_text(result.text, reply_markup=project_markup(result))


async def on_project_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    await query.answer()
    user_id = update.effective_user.id
    try:
        if query.data == PROJ_CONFIRM:
            result: ProjectResult | IntakeResult = await _projects(context).confirm(user_id, chat_id)
        elif query.data == PROJ_CANCEL:
            result = _projects(context).cancel(user_id, chat_id)
        elif query.data == PROJ_EXISTING:
            result = await _intake(context).choose_existing_project(user_id, chat_id)
        elif query.data == PROJ_CREATE_NEW:
            result = await _intake(context).choose_new_project(user_id, chat_id)
        else:
            return
    except Exception:
        logger.exception("Project callback failed")
        if query.message:
            await query.message.reply_text("Не вдалося змінити проєкт. Спробуй ще раз.")
        return
    if query.message:
        await query.message.reply_text(result.text, reply_markup=project_markup(result))
