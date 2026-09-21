"""Application entrypoint: logging, config, Telegram long-polling."""

from loguru import logger
from telegram import Update

from chief_of_staff.bot.app import build_application
from chief_of_staff.config.log import configure_logging
from chief_of_staff.config.settings import (
    get_settings,
    railway_replica_region,
    supabase_pooler_region,
)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("Chief of Staff starting")
    logger.info(
        "runtime railway_replica_region={railway} supabase_pooler_region={db}",
        railway=railway_replica_region(),
        db=supabase_pooler_region(settings.database_url),
    )

    application = build_application(settings)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
