"""Application entrypoint: logging, config, Telegram long-polling."""

from loguru import logger
from telegram import Update

from chief_of_staff.bot.app import build_application
from chief_of_staff.config.log import configure_logging
from chief_of_staff.config.settings import get_settings


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("Chief of Staff starting")

    application = build_application(settings)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
