"""Builds the python-telegram-bot Application."""

import asyncio
from typing import Any, Final

from loguru import logger
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from chief_of_staff.bot.handlers.projects import on_newproject, on_project_callback, on_projects
from chief_of_staff.bot.handlers.start import on_start
from chief_of_staff.bot.handlers.task import on_lifecycle_callback, on_task_callback, on_text
from chief_of_staff.bot.handlers.today import on_plan_callback, on_today
from chief_of_staff.bot.handlers.voice import on_voice
from chief_of_staff.bot.notifier import BotNotifier
from chief_of_staff.config.settings import Settings
from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.infrastructure.database.session import get_session_factory
from chief_of_staff.infrastructure.openai.client import OpenAIResponsesClient
from chief_of_staff.services.clock import Clock
from chief_of_staff.services.daily_planning import DailyPlanningService
from chief_of_staff.services.health import HealthService
from chief_of_staff.services.planning_session import InMemoryPlanningSessionStore
from chief_of_staff.services.conversation_context import InMemoryConversationStore
from chief_of_staff.services.intent_router import IntentRouter
from chief_of_staff.services.lifecycle_session import InMemoryLifecycleStore
from chief_of_staff.services.project_intent import OpenAIProjectIntentParser
from chief_of_staff.services.project_ops import ProjectManagementService
from chief_of_staff.services.project_session import InMemoryProjectOpStore
from chief_of_staff.services.reminders import ReminderService
from chief_of_staff.services.replan_interpreter import ReplanInterpreter
from chief_of_staff.services.task_command_intent import OpenAITaskIntentParser
from chief_of_staff.services.task_intake import TaskIntakeService
from chief_of_staff.services.task_interpreter import TaskInterpreter
from chief_of_staff.services.task_lifecycle import TaskLifecycleService
from chief_of_staff.services.task_query import TaskQueryService
from chief_of_staff.services.task_session import InMemoryTaskSessionStore
from chief_of_staff.services.voice import VoiceMessageService

REMINDER_TASK_KEY: Final = "reminder_task"
REMINDER_TASK_NAME: Final = "deadline-reminders"


async def _reminder_loop(application: Application) -> None:
    interval = int(application.bot_data["reminder_poll_seconds"])
    await asyncio.sleep(min(15, interval))
    while True:
        service: ReminderService | None = application.bot_data.get("reminders")
        if service is not None:
            try:
                sent = await service.dispatch_due_reminders()
                if sent:
                    logger.info("Reminders sent count={count}", count=sent)
            except Exception:
                logger.exception("Reminder dispatch failed")
        await asyncio.sleep(interval)


def start_reminder_scheduler(application: Application) -> asyncio.Task[Any]:
    """Start the reminder loop once the Application is running.

    Must not use Application.create_task before Application.start(): PTB 21.x only
    tracks those tasks while running=True, and an infinite loop would also block
    Application.stop() which awaits tracked tasks without cancelling them.
    """
    if not application.running:
        raise RuntimeError("Reminder scheduler must start after Application.start()")
    existing = application.bot_data.get(REMINDER_TASK_KEY)
    if isinstance(existing, asyncio.Task) and not existing.done():
        return existing
    task = asyncio.create_task(_reminder_loop(application), name=REMINDER_TASK_NAME)
    application.bot_data[REMINDER_TASK_KEY] = task
    logger.info("Reminder scheduler started")
    return task


async def stop_reminder_scheduler(application: Application) -> None:
    task = application.bot_data.pop(REMINDER_TASK_KEY, None)
    if not isinstance(task, asyncio.Task):
        return
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    logger.info("Reminder scheduler stopped")


async def _on_post_init(application: Application) -> None:
    application.bot_data["reminders"] = ReminderService(
        get_session_factory,
        Clock(),
        BotNotifier(application.bot),
    )


class ChiefOfStaffApplication(Application):
    """Start/stop the reminder loop with the Application.

    PTB Application uses __slots__, so instance assignment of ``start``/``stop``
    raises AttributeError. Subclassing is the supported extension point.
    """

    async def start(self) -> None:
        await super().start()
        start_reminder_scheduler(self)

    async def stop(self) -> None:
        await stop_reminder_scheduler(self)
        await super().stop()


def build_application(settings: Settings) -> Application:
    openai = OpenAIResponsesClient(settings)
    repository = SqlAlchemyTaskRepository(get_session_factory)
    clock = Clock()
    intake = TaskIntakeService(
        interpreter=TaskInterpreter(openai, clock),
        store=InMemoryTaskSessionStore(),
        repository=repository,
    )
    planning = DailyPlanningService(
        repository,
        InMemoryPlanningSessionStore(),
        clock,
        ReplanInterpreter(openai),
    )
    project_ops = ProjectManagementService(
        repository,
        InMemoryProjectOpStore(),
        OpenAIProjectIntentParser(openai),
    )
    lifecycle = TaskLifecycleService(
        repository,
        InMemoryLifecycleStore(),
        clock,
        OpenAITaskIntentParser(openai),
    )
    queries = TaskQueryService(repository, OpenAITaskIntentParser(openai))
    router = IntentRouter(openai)
    conversation = InMemoryConversationStore(clock)
    application = (
        Application.builder()
        .application_class(ChiefOfStaffApplication)
        .token(settings.telegram_bot_token)
        .post_init(_on_post_init)
        .build()
    )
    application.bot_data["health"] = HealthService()
    application.bot_data["task_intake"] = intake
    application.bot_data["daily_planning"] = planning
    application.bot_data["project_ops"] = project_ops
    application.bot_data["task_lifecycle"] = lifecycle
    application.bot_data["task_queries"] = queries
    application.bot_data["intent_router"] = router
    application.bot_data["conversation"] = conversation
    application.bot_data["voice"] = VoiceMessageService(
        openai, intake, planning, project_ops, lifecycle, queries, router, conversation
    )
    application.bot_data["reminder_poll_seconds"] = settings.reminder_poll_seconds
    application.add_handler(CommandHandler("start", on_start))
    application.add_handler(CommandHandler("today", on_today))
    application.add_handler(CommandHandler("projects", on_projects))
    application.add_handler(CommandHandler("newproject", on_newproject))
    application.add_handler(CallbackQueryHandler(on_task_callback, pattern=r"^task:"))
    application.add_handler(CallbackQueryHandler(on_lifecycle_callback, pattern=r"^life:"))
    application.add_handler(CallbackQueryHandler(on_plan_callback, pattern=r"^plan:"))
    application.add_handler(CallbackQueryHandler(on_project_callback, pattern=r"^proj:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_handler(MessageHandler(filters.VOICE, on_voice))
    return application
