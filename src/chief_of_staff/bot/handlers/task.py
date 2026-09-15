"""Thin Telegram adapters for task intake (text)."""

from loguru import logger
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from chief_of_staff.bot.keyboards import (
    CANCEL,
    EDIT,
    LIFE_CANCEL,
    LIFE_DONE,
    LIFE_NEW_TASK,
    LIFE_PICK_PREFIX,
    YES,
    complete_choice_keyboard,
    complete_confirm_keyboard,
    confirmation_keyboard,
    plan_keyboard,
    project_confirm_keyboard,
    project_disambiguate_keyboard,
    statement_choice_keyboard,
    statement_confirm_keyboard,
)
from chief_of_staff.bot.utterance import process_user_utterance
from chief_of_staff.services.daily_planning import DailyPlanningService, PlanResult
from chief_of_staff.services.project_ops import ProjectManagementService, ProjectResult
from chief_of_staff.services.task_intake import IntakeResult, TaskIntakeService
from chief_of_staff.services.task_lifecycle import (
    LifecycleKind,
    LifecycleResult,
    TaskLifecycleService,
)
from chief_of_staff.services.task_query import QueryResult, TaskQueryService


def _intake(context: ContextTypes.DEFAULT_TYPE) -> TaskIntakeService:
    return context.bot_data["task_intake"]


def _chat_id(update: Update) -> int | None:
    if update.effective_chat:
        return update.effective_chat.id
    return None


def _planning(context: ContextTypes.DEFAULT_TYPE) -> DailyPlanningService:
    return context.bot_data["daily_planning"]


def _projects(context: ContextTypes.DEFAULT_TYPE) -> ProjectManagementService:
    return context.bot_data["project_ops"]


def _lifecycle(context: ContextTypes.DEFAULT_TYPE) -> TaskLifecycleService:
    return context.bot_data["task_lifecycle"]


def _queries(context: ContextTypes.DEFAULT_TYPE) -> TaskQueryService:
    return context.bot_data["task_queries"]


def reply_markup_for(
    result: IntakeResult | PlanResult | ProjectResult | LifecycleResult | QueryResult,
):
    if isinstance(result, QueryResult):
        return None
    if isinstance(result, PlanResult):
        return plan_keyboard() if result.show_plan_buttons else None
    if isinstance(result, ProjectResult):
        return (
            project_confirm_keyboard(result.confirm_label)
            if result.show_confirm_buttons
            else None
        )
    if isinstance(result, LifecycleResult):
        if result.show_choice_buttons:
            if result.show_statement_buttons:
                return statement_choice_keyboard(result.choice_count)
            return complete_choice_keyboard(result.choice_count)
        if result.show_statement_buttons:
            return statement_confirm_keyboard()
        return (
            complete_confirm_keyboard(result.confirm_label)
            if result.show_confirm_buttons
            else None
        )
    if result.show_disambiguate_buttons:
        return project_disambiguate_keyboard()
    return confirmation_keyboard() if result.show_confirm_buttons else None


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text or not update.effective_user:
        return
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        result = await process_user_utterance(
            _intake(context),
            update.effective_user.id,
            chat_id,
            update.message.text,
            planning=_planning(context),
            projects=_projects(context),
            lifecycle=_lifecycle(context),
            queries=_queries(context),
            router=context.bot_data.get("intent_router"),
            context=context.bot_data.get("conversation"),
        )
    except Exception:
        logger.exception("Task interpretation failed")
        await update.message.reply_text(
            "I could not interpret that. Please try a shorter message."
        )
        return
    await update.message.reply_text(result.text, reply_markup=reply_markup_for(result))


async def on_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    await query.answer()
    intake = _intake(context)
    user_id = update.effective_user.id
    if query.data == YES:
        result = await intake.confirm(user_id, chat_id)
    elif query.data == EDIT:
        result = intake.begin_edit(user_id, chat_id)
    elif query.data == CANCEL:
        result = intake.cancel(user_id, chat_id)
    else:
        return
    if query.message:
        await query.message.reply_text(result.text, reply_markup=reply_markup_for(result))


async def on_lifecycle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    await query.answer()
    lifecycle = _lifecycle(context)
    intake = _intake(context)
    user_id = update.effective_user.id
    try:
        if query.data == LIFE_DONE:
            result = await lifecycle.confirm(user_id, chat_id)
        elif query.data == LIFE_CANCEL:
            result = lifecycle.cancel(user_id, chat_id)
        elif query.data == LIFE_NEW_TASK:
            deferred = lifecycle.defer_to_new_task(user_id, chat_id)
            if deferred.kind == LifecycleKind.DEFER_TO_INTAKE:
                result = await process_user_utterance(
                    intake,
                    user_id,
                    chat_id,
                    deferred.defer_intake_text or "",
                    planning=_planning(context),
                    projects=_projects(context),
                    lifecycle=lifecycle,
                    queries=_queries(context),
                    router=context.bot_data.get("intent_router"),
                    context=context.bot_data.get("conversation"),
                    force_intake=True,
                )
            else:
                result = deferred
        elif query.data.startswith(LIFE_PICK_PREFIX):
            index = int(query.data.removeprefix(LIFE_PICK_PREFIX))
            result = await lifecycle.choose(user_id, chat_id, index)
        else:
            return
    except Exception:
        logger.exception("Task completion callback failed")
        if query.message:
            await query.message.reply_text("Не вдалося оновити задачу. Спробуй ще раз.")
        return
    if query.message:
        await query.message.reply_text(result.text, reply_markup=reply_markup_for(result))
