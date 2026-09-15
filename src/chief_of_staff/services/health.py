"""Health / liveness replies used by the Telegram skeleton."""


class HealthService:
    """Keeps the bot copy out of handler modules so it can be reused later."""

    RUNNING_MESSAGE = "Chief of Staff is running"

    def running_message(self) -> str:
        return self.RUNNING_MESSAGE
