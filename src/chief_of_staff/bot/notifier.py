"""Sends reminder text through the live Telegram Bot API."""

from telegram import Bot


class BotNotifier:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_message(self, chat_id: int, text: str) -> None:
        await self._bot.send_message(chat_id=chat_id, text=text)
