"""Thin Telegram adapter for voice notes."""

from loguru import logger
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from chief_of_staff.bot.handlers.task import _chat_id, reply_markup_for
from chief_of_staff.services.voice import TRANSCRIBE_FAILED, VoiceMessageService


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.voice or not update.effective_user:
        return
    chat_id = _chat_id(update)
    if chat_id is None:
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    voice = update.message.voice
    try:
        telegram_file = await context.bot.get_file(voice.file_id)
        audio = bytes(await telegram_file.download_as_bytearray())
    except Exception:
        logger.exception(
            "Voice download failed user_id={user_id} chat_id={chat_id}",
            user_id=update.effective_user.id,
            chat_id=chat_id,
        )
        await update.message.reply_text(TRANSCRIBE_FAILED)
        return

    service: VoiceMessageService = context.bot_data["voice"]
    result = await service.handle_voice(
        update.effective_user.id,
        chat_id,
        audio,
        filename="voice.ogg",
        duration_seconds=voice.duration,
    )
    if result.error or result.intake is None or result.heard_text is None:
        await update.message.reply_text(result.error or TRANSCRIBE_FAILED)
        return
    await update.message.reply_text(result.heard_text)
    await update.message.reply_text(
        result.intake.text,
        reply_markup=reply_markup_for(result.intake),
    )
