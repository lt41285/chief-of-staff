"""Thin Telegram adapters for /today planning."""

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from chief_of_staff.bot.keyboards import PLAN_ACCEPT, PLAN_CANCEL, PLAN_REPLAN, plan_keyboard
from chief_of_staff.services.daily_planning import DailyPlanningService, PlanResult


def _planning(context: ContextTypes.DEFAULT_TYPE) -> DailyPlanningService:
    return context.bot_data["daily_planning"]


def _chat_id(update: Update) -> int | None:
    if update.effective_chat:
        return update.effective_chat.id
    return None


def plan_markup(result: PlanResult):
    return plan_keyboard() if result.show_plan_buttons else None


async def on_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    result = _planning(context).start(update.effective_user.id, chat_id)
    await update.message.reply_text(result.text, reply_markup=plan_markup(result))


async def on_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return
    chat_id = _chat_id(update)
    if chat_id is None:
        return
    await query.answer()
    planning = _planning(context)
    user_id = update.effective_user.id
    try:
        if query.data == PLAN_ACCEPT:
            result = await planning.accept(user_id, chat_id)
        elif query.data == PLAN_REPLAN:
            result = planning.begin_replan(user_id, chat_id)
        elif query.data == PLAN_CANCEL:
            result = planning.cancel(user_id, chat_id)
        else:
            return
    except Exception:
        logger.exception("Plan callback failed")
        if query.message:
            await query.message.reply_text("Не вдалося оновити план. Спробуй ще раз.")
        return
    if query.message:
        await query.message.reply_text(result.text, reply_markup=plan_markup(result))
