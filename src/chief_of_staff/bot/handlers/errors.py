"""Application-wide Telegram error handler. Users must not get silence."""

from loguru import logger
from telegram import Update
from telegram.error import Forbidden
from telegram.ext import ContextTypes

TECHNICAL_ERROR = (
    "Сталася технічна помилка, спробуй ще раз або сформулюй інакше"
)


async def on_application_error(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger.opt(exception=context.error).error("Unhandled telegram update error")
    if isinstance(context.error, Forbidden):
        return
    chat = update.effective_chat if isinstance(update, Update) else None
    if chat is None:
        return
    try:
        await context.bot.send_message(chat_id=chat.id, text=TECHNICAL_ERROR)
    except Exception:
        logger.exception("Failed to send technical error notice")
