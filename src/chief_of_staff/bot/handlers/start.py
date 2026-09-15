"""Minimal handlers: confirm the process is up."""

from telegram import Update
from telegram.ext import ContextTypes

from chief_of_staff.services.health import HealthService


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    health: HealthService = context.bot_data["health"]
    await update.message.reply_text(health.running_message())
