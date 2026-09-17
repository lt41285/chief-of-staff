"""Lifecycle for existing tasks. AI never writes to the database."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
import re
from uuid import UUID

from loguru import logger

from chief_of_staff.infrastructure.database.repository import (
    SqlAlchemyTaskRepository,
    _person_names_match,
)
from chief_of_staff.services.names import normalize_entity_name
from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.models.task_command import TaskIntent, TaskIntentKind
from chief_of_staff.services.actual_time import (
    is_skip_actual_time,
    is_skip_all_actual_time,
    parse_actual_minutes,
)
from chief_of_staff.services.completion_batch import (
    MIN_BATCH_ITEMS,
    BatchMatch,
    is_count_only_completion,
    looks_like_all_reference,
    match_completion_batch,
    split_completion_items,
)
from chief_of_staff.services.completion_statement import (
    CompletionPhrasing,
    classify_completion_phrasing,
    content_token_count,
    is_new_task_override,
    match_completion_statement,
)
from chief_of_staff.services.clock import KYIV, Clock
from chief_of_staff.services.deadline_parse import parse_natural_deadline
from chief_of_staff.services.lifecycle_format import (
    ALREADY_DONE,
    ASK_ACTUAL,
    ASK_DEADLINE,
    ASK_LIST_COMPLETED,
    format_actual_recorded,
    ASK_WHICH,
    ASK_WHICH_POSTPONE,
    ASK_WHICH_RESUME,
    ASK_WHICH_TIME,
    ASK_WHICH_WAITING,
    BTN_ALL_DONE,
    BTN_DONE,
    BTN_YES_DONE,
    BTN_POSTPONE,
    BTN_RESUME,
    BTN_WAITING,
    CANCELLED,
    NEED_TASK_HINT,
    NO_MATCHED_COMPLETION,
    NO_PENDING,
    NOT_FOUND,
    NOT_WAITING,
    RESUMED,
    SEPARATE_ACTIONS,
    SKIPPED_ACTUAL_ALL,
    UNSUPPORTED,
    WAITING_SET,
    format_ask_actual_for,
    format_batch_complete_preview,
    format_batch_completed,
    format_choice_list,
    format_complete_preview,
    format_statement_complete_preview,
    format_completed_message,
    format_postpone_preview,
    format_postponed_message,
    format_resume_preview,
    format_waiting_preview,
)
from chief_of_staff.services.lifecycle_session import (
    InMemoryLifecycleStore,
    LifecycleAction,
    LifecyclePhase,
    PendingLifecycle,
)
from chief_of_staff.services.task_command_intent import (
    TaskIntentParser,
    looks_like_complete_command,
    looks_like_task_command,
    parse_task_intent_deterministic,
)
from chief_of_staff.services.task_match import match_task_reference
from chief_of_staff.services.task_validation import parse_deadline

_YES = frozenset(
    {
        "так",
        "yes",
        "y",
        "підтверджую",
        "confirm",
        "виконано",
        "waiting",
        "перенести",
        "так, виконано",
        "так виконано",
    }
)
_CANCEL = frozenset({"ні", "нет", "no", "скасувати", "cancel"})
_PICK = re.compile(r"^\s*(\d{1,2})\s*[.)]?\s*$")
_MAX_REFERENCE_CHOICES = 8
_LIFECYCLE_KINDS = frozenset(
    {
        TaskIntentKind.COMPLETE_TASK,
        TaskIntentKind.POSTPONE_TASK,
        TaskIntentKind.WAITING_TASK,
        TaskIntentKind.RESUME_TASK,
        TaskIntentKind.CANCEL_TASK,
        TaskIntentKind.UPDATE_TASK,
    }
)


class LifecycleKind(StrEnum):
    ASK_CONFIRM = "ask_confirm"
    ASK_WHICH = "ask_which"
    ASK_ACTUAL = "ask_actual"
    ASK_DEADLINE = "ask_deadline"
    DONE = "done"
    CANCELLED = "cancelled"
    INFO = "info"
    DEFER_TO_INTAKE = "defer_to_intake"


@dataclass(frozen=True)
class LifecycleResult:
    kind: LifecycleKind
    text: str
    show_confirm_buttons: bool = False
    show_choice_buttons: bool = False
    show_statement_buttons: bool = False
    choice_count: int = 0
    confirm_label: str = BTN_DONE
    defer_intake_text: str | None = None


class TaskLifecycleService:
    def __init__(
        self,
        repository: SqlAlchemyTaskRepository,
        store: InMemoryLifecycleStore,
        clock: Clock,
        parser: TaskIntentParser | None = None,
    ) -> None:
        self._repository = repository
        self._store = store
        self._clock = clock
        self._parser = parser

    def is_awaiting_input(self, user_id: int, chat_id: int) -> bool:
        return self._store.get(user_id, chat_id) is not None

    def describe_pending(self, user_id: int, chat_id: int) -> dict | None:
        pending = self._store.get(user_id, chat_id)
        if pending is None:
            return None
        if pending.phase == LifecyclePhase.AWAITING_ACTUAL:
            return {
                "pending_action": "record_actual_minutes",
                "awaiting": "actual_minutes",
                "task_id": str(pending.task_id) if pending.task_id else None,
                "draft": {
                    "task_id": str(pending.task_id) if pending.task_id else None,
                    "task_title": pending.current_title,
                    "remaining_tasks": len(pending.actual_queue),
                },
            }
        if pending.phase == LifecyclePhase.AWAITING_TASK_REFERENCE:
            return {
                "pending_action": "complete_task",
                "awaiting": "task_reference",
                "task_id": None,
                "draft": {"original_text": pending.original_text},
            }
        if pending.phase == LifecyclePhase.CONFIRMING_BATCH:
            return {
                "pending_action": "complete_task",
                "awaiting": "confirmation",
                "task_id": None,
                "draft": {
                    "task_titles": [title for _task_id, title in pending.batch_items],
                },
            }
        awaiting = {
            LifecyclePhase.AWAITING_DEADLINE: "deadline",
            LifecyclePhase.CONFIRMING: "confirmation",
            LifecyclePhase.CHOOSING: "which_task",
        }[pending.phase]
        return {
            "pending_action": pending.action.value + "_task"
            if pending.action.value != "complete"
            else "complete_task",
            "awaiting": awaiting,
            "task_id": str(pending.task_id) if pending.task_id else None,
            "draft": {
                "task_id": str(pending.task_id) if pending.task_id else None,
                "new_deadline": pending.new_deadline.isoformat() if pending.new_deadline else None,
                "waiting_for": pending.waiting_for,
            },
        }

    async def classify(self, text: str) -> TaskIntent:
        parsed = parse_task_intent_deterministic(text)
        if parsed is not None:
            return parsed
        phrasing = classify_completion_phrasing(text)
        if phrasing in {CompletionPhrasing.COMPLETED, CompletionPhrasing.SHORT}:
            return TaskIntent(kind=TaskIntentKind.NORMAL_TASK_INPUT)
        if self._parser is not None and looks_like_task_command(text):
            try:
                return await self._parser.parse(text)
            except Exception:
                logger.exception("Task command intent parse failed")
        if looks_like_complete_command(text):
            return TaskIntent(kind=TaskIntentKind.COMPLETE_TASK, task_query=text)
        return TaskIntent(kind=TaskIntentKind.NORMAL_TASK_INPUT)

    async def handle_user_text(self, user_id: int, chat_id: int, text: str) -> LifecycleResult:
        pending = self._store.get(user_id, chat_id)
        if pending is not None:
            if pending.offer_new_task and is_new_task_override(text):
                return self.defer_to_new_task(user_id, chat_id)
            if (
                pending.phase
                in {LifecyclePhase.CONFIRMING, LifecyclePhase.CONFIRMING_BATCH}
                and pending.action == LifecycleAction.COMPLETE
                and classify_completion_phrasing(text) == CompletionPhrasing.SHORT
            ):
                return await self.confirm(user_id, chat_id)
            if pending.phase == LifecyclePhase.AWAITING_DEADLINE:
                dated = parse_natural_deadline(text, self._today())
                if dated is not None:
                    return await self._continue_pending(user_id, chat_id, pending, text)
            restart = parse_task_intent_deterministic(text)
            if (
                restart is not None
                and restart.kind in _LIFECYCLE_KINDS
                and pending.phase != LifecyclePhase.AWAITING_ACTUAL
            ):
                return await self.handle_intent(user_id, chat_id, restart, raw_text=text)
            return await self._continue_pending(user_id, chat_id, pending, text)
        intent = await self.classify(text)
        return await self.handle_intent(user_id, chat_id, intent, raw_text=text)

    async def handle_intent(
        self,
        user_id: int,
        chat_id: int,
        intent: TaskIntent,
        *,
        raw_text: str = "",
    ) -> LifecycleResult:
        if intent.kind == TaskIntentKind.UPDATE_TASK:
            folded = raw_text.casefold()
            if "waiting" in folded and re.search(r"перенес|відклад|postpone", folded):
                return LifecycleResult(kind=LifecycleKind.INFO, text=SEPARATE_ACTIONS)
            return LifecycleResult(kind=LifecycleKind.INFO, text=UNSUPPORTED)
        if intent.kind == TaskIntentKind.CANCEL_TASK:
            return LifecycleResult(kind=LifecycleKind.INFO, text=UNSUPPORTED)
        if intent.kind == TaskIntentKind.POSTPONE_TASK:
            return await self._start_postpone(user_id, chat_id, intent, raw_text)
        if intent.kind == TaskIntentKind.WAITING_TASK:
            return await self._start_waiting(user_id, chat_id, intent, raw_text)
        if intent.kind == TaskIntentKind.RESUME_TASK:
            return await self._start_resume(user_id, chat_id, intent, raw_text)
        if intent.kind != TaskIntentKind.COMPLETE_TASK:
            return LifecycleResult(kind=LifecycleKind.INFO, text=NOT_FOUND)
        actual = parse_actual_minutes(raw_text or intent.task_query or "")
        query = (intent.task_query or "").strip()
        if is_count_only_completion(raw_text):
            query = ""
        batch = await self._batch_from_text(user_id, chat_id, raw_text)
        if batch is not None:
            return batch
        phrasing = classify_completion_phrasing(raw_text)
        if phrasing in {CompletionPhrasing.COMPLETED, CompletionPhrasing.SHORT} or not query:
            statement = await self.consider_completed_statement(
                user_id, chat_id, raw_text or query
            )
            if statement is not None:
                return statement
            if not query:
                return self._ask_which_task(user_id, chat_id, raw_text)
        return await self._resolve_and_present(
            user_id,
            chat_id,
            query,
            LifecycleAction.COMPLETE,
            actual_minutes=actual,
        )

    async def consider_completed_statement(
        self, user_id: int, chat_id: int, text: str
    ) -> LifecycleResult | None:
        phrasing = classify_completion_phrasing(text)
        completed = phrasing in {CompletionPhrasing.COMPLETED, CompletionPhrasing.SHORT}
        if phrasing == CompletionPhrasing.PROSPECTIVE:
            return None
        if phrasing == CompletionPhrasing.UNKNOWN:
            completed = await self._llm_says_completed(text)
        if not completed:
            return None
        open_tasks = await self._repository.list_user_tasks(user_id)
        batch = self._consider_batch(user_id, chat_id, text, open_tasks)
        if batch is not None:
            return batch
        if phrasing == CompletionPhrasing.SHORT:
            if len(open_tasks) == 1:
                return self._present_statement(user_id, chat_id, open_tasks[0], text)
            matched = match_completion_statement(text, open_tasks)
            if matched.selected is not None:
                return self._present_statement(user_id, chat_id, matched.selected, text)
            if matched.candidates:
                return self._present_statement_choices(
                    user_id, chat_id, matched.candidates, text
                )
            return self._ask_which_task(user_id, chat_id, text)
        matched = match_completion_statement(text, open_tasks)
        if matched.selected is not None:
            return self._present_statement(user_id, chat_id, matched.selected, text)
        if matched.candidates:
            return self._present_statement_choices(
                user_id, chat_id, matched.candidates, text
            )
        if len(open_tasks) == 1 and content_token_count(text) <= 2:
            return self._present_statement(user_id, chat_id, open_tasks[0], text)
        if is_count_only_completion(text):
            return self._ask_which_task(user_id, chat_id, text)
        return self._ask_which_task(
            user_id, chat_id, text, hint=NO_MATCHED_COMPLETION
        )

    def defer_to_new_task(self, user_id: int, chat_id: int) -> LifecycleResult:
        pending = self._store.get(user_id, chat_id)
        if pending is None or not pending.offer_new_task:
            return LifecycleResult(kind=LifecycleKind.INFO, text=NO_PENDING)
        original = (pending.original_text or "").strip()
        self._store.clear(user_id, chat_id)
        if not original:
            return LifecycleResult(kind=LifecycleKind.INFO, text=NO_PENDING)
        return LifecycleResult(
            kind=LifecycleKind.DEFER_TO_INTAKE,
            text=original,
            defer_intake_text=original,
        )

    async def _llm_says_completed(self, text: str) -> bool:
        classifier = getattr(self._parser, "classify_as_completed_action", None)
        if classifier is None:
            return False
        try:
            return bool(await classifier(text))
        except Exception:
            logger.exception("Completion-statement classification failed")
            return False

    def _present_statement(
        self,
        user_id: int,
        chat_id: int,
        task: PlanCandidate,
        original_text: str,
    ) -> LifecycleResult:
        pending = PendingLifecycle(
            phase=LifecyclePhase.CONFIRMING,
            action=LifecycleAction.COMPLETE,
            original_text=original_text,
            offer_new_task=True,
            actual_minutes=parse_actual_minutes(original_text),
        )
        return self._present_task(user_id, chat_id, task, pending)

    def _present_statement_choices(
        self,
        user_id: int,
        chat_id: int,
        candidates: tuple[PlanCandidate, ...],
        original_text: str,
    ) -> LifecycleResult:
        pending = PendingLifecycle(
            phase=LifecyclePhase.CHOOSING,
            action=LifecycleAction.COMPLETE,
            candidate_ids=tuple(task.id for task in candidates),
            original_text=original_text,
            offer_new_task=True,
        )
        self._store.put(user_id, chat_id, pending)
        return LifecycleResult(
            kind=LifecycleKind.ASK_WHICH,
            text=format_choice_list(candidates, ASK_WHICH),
            show_choice_buttons=True,
            show_statement_buttons=True,
            choice_count=len(candidates),
        )

    async def _batch_from_text(
        self, user_id: int, chat_id: int, text: str
    ) -> LifecycleResult | None:
        """A list of several tasks is a batch whatever the phrasing around it."""
        if len(split_completion_items(text)) < MIN_BATCH_ITEMS:
            return None
        open_tasks = await self._repository.list_user_tasks(user_id)
        return self._consider_batch(user_id, chat_id, text, open_tasks)

    def _consider_batch(
        self,
        user_id: int,
        chat_id: int,
        text: str,
        open_tasks: list[PlanCandidate],
    ) -> LifecycleResult | None:
        """Engage the batch flow only when the message really names several tasks."""
        items = split_completion_items(text)
        if len(items) < MIN_BATCH_ITEMS:
            return None
        matched = match_completion_batch(items, open_tasks, today=self._today())
        if len(matched.tasks) < MIN_BATCH_ITEMS:
            return None
        return self._present_batch(user_id, chat_id, matched, text)

    def _present_batch(
        self,
        user_id: int,
        chat_id: int,
        matched: BatchMatch,
        original_text: str,
    ) -> LifecycleResult:
        self._store.put(
            user_id,
            chat_id,
            PendingLifecycle(
                phase=LifecyclePhase.CONFIRMING_BATCH,
                action=LifecycleAction.COMPLETE,
                batch_items=tuple((task.id, task.title) for task in matched.tasks),
                unmatched_items=matched.unmatched,
                original_text=original_text,
            ),
        )
        return LifecycleResult(
            kind=LifecycleKind.ASK_CONFIRM,
            text=format_batch_complete_preview(matched.tasks, matched.unmatched),
            show_confirm_buttons=True,
            confirm_label=BTN_ALL_DONE,
        )

    async def _present_batch_from_ids(
        self,
        user_id: int,
        chat_id: int,
        task_ids: tuple[UUID, ...],
        original_text: str,
    ) -> LifecycleResult:
        """«всі» after a numbered choice list — take every candidate shown."""
        tasks: list[PlanCandidate] = []
        for task_id in task_ids:
            task = await self._repository.get_user_task(user_id, task_id)
            if task is not None and task.status not in {"done", "cancelled"}:
                tasks.append(task)
        if not tasks:
            self._store.clear(user_id, chat_id)
            return LifecycleResult(kind=LifecycleKind.INFO, text=NOT_FOUND)
        if len(tasks) == 1:
            return self._present_statement(user_id, chat_id, tasks[0], original_text)
        return self._present_batch(
            user_id,
            chat_id,
            BatchMatch(tasks=tuple(tasks), unmatched=()),
            original_text,
        )

    def _ask_which_task(
        self,
        user_id: int,
        chat_id: int,
        original_text: str,
        *,
        hint: str | None = None,
    ) -> LifecycleResult:
        """Asking «which one?» must leave state behind, or the next turn repeats it."""
        self._store.put(
            user_id,
            chat_id,
            PendingLifecycle(
                phase=LifecyclePhase.AWAITING_TASK_REFERENCE,
                action=LifecycleAction.COMPLETE,
                original_text=original_text,
                offer_new_task=hint == NO_MATCHED_COMPLETION,
            ),
        )
        message = hint
        if message is None:
            message = (
                ASK_LIST_COMPLETED
                if is_count_only_completion(original_text)
                else NEED_TASK_HINT
            )
        return LifecycleResult(kind=LifecycleKind.ASK_WHICH, text=message)

    async def confirm(self, user_id: int, chat_id: int) -> LifecycleResult:
        pending = self._store.get(user_id, chat_id)
        if pending is not None and pending.phase == LifecyclePhase.CONFIRMING_BATCH:
            return await self._mark_batch_done(user_id, chat_id, pending)
        if pending is None or pending.phase != LifecyclePhase.CONFIRMING or pending.task_id is None:
            return LifecycleResult(kind=LifecycleKind.INFO, text=NO_PENDING)
        if pending.action == LifecycleAction.POSTPONE:
            return await self._apply_postpone(user_id, chat_id, pending)
        if pending.action == LifecycleAction.WAITING:
            return await self._apply_waiting(user_id, chat_id, pending)
        if pending.action == LifecycleAction.RESUME:
            return await self._apply_resume(user_id, chat_id, pending)
        return await self._mark_done(user_id, chat_id, pending.task_id, pending.actual_minutes)

    def cancel(self, user_id: int, chat_id: int) -> LifecycleResult:
        if self._store.get(user_id, chat_id) is None:
            return LifecycleResult(kind=LifecycleKind.INFO, text=NO_PENDING)
        self._store.clear(user_id, chat_id)
        return LifecycleResult(kind=LifecycleKind.CANCELLED, text=CANCELLED)

    async def choose(self, user_id: int, chat_id: int, index: int) -> LifecycleResult:
        pending = self._store.get(user_id, chat_id)
        if pending is None or pending.phase != LifecyclePhase.CHOOSING:
            return LifecycleResult(kind=LifecycleKind.INFO, text=NO_PENDING)
        if index < 0 or index >= len(pending.candidate_ids):
            return LifecycleResult(
                kind=LifecycleKind.ASK_WHICH,
                text="Обери номер зі списку.",
                show_choice_buttons=True,
                choice_count=len(pending.candidate_ids),
            )
        task_id = pending.candidate_ids[index]
        task = await self._repository.get_user_task(user_id, task_id)
        if task is None or task.status in {"done", "cancelled"}:
            self._store.clear(user_id, chat_id)
            return LifecycleResult(kind=LifecycleKind.INFO, text=NOT_FOUND)
        return self._present_task(user_id, chat_id, task, pending)

    async def _start_postpone(
        self, user_id: int, chat_id: int, intent: TaskIntent, raw_text: str
    ) -> LifecycleResult:
        today = self._today()
        deadline = parse_natural_deadline(raw_text, today) or parse_deadline(intent.new_deadline)
        return await self._resolve_and_present(
            user_id,
            chat_id,
            intent.task_query or "",
            LifecycleAction.POSTPONE,
            new_deadline=deadline,
        )

    async def _start_waiting(
        self, user_id: int, chat_id: int, intent: TaskIntent, raw_text: str
    ) -> LifecycleResult:
        return await self._resolve_and_present(
            user_id,
            chat_id,
            intent.task_query or raw_text,
            LifecycleAction.WAITING,
            waiting_for=intent.waiting_for,
        )

    async def _start_resume(
        self, user_id: int, chat_id: int, intent: TaskIntent, raw_text: str
    ) -> LifecycleResult:
        return await self._resolve_and_present(
            user_id,
            chat_id,
            intent.task_query or raw_text,
            LifecycleAction.RESUME,
        )

    async def _resolve_and_present(
        self,
        user_id: int,
        chat_id: int,
        query: str,
        action: LifecycleAction,
        *,
        actual_minutes: int | None = None,
        new_deadline: date | None = None,
        waiting_for: str | None = None,
    ) -> LifecycleResult:
        cleaned = query.strip()
        if not cleaned:
            if waiting_for:
                cleaned = waiting_for
            else:
                return LifecycleResult(kind=LifecycleKind.INFO, text=NEED_TASK_HINT)
        open_tasks = await self._repository.list_user_tasks(user_id)
        done_tasks = await self._repository.list_user_tasks(user_id, only_statuses=("done",))
        matched = match_task_reference(cleaned, open_tasks, done_tasks)
        if matched.already_done is not None:
            return LifecycleResult(kind=LifecycleKind.INFO, text=ALREADY_DONE)
        pending = PendingLifecycle(
            phase=LifecyclePhase.CHOOSING,
            action=action,
            actual_minutes=actual_minutes,
            new_deadline=new_deadline,
            waiting_for=waiting_for,
        )
        if matched.selected is not None:
            return self._present_task(user_id, chat_id, matched.selected, pending)
        if matched.candidates:
            pending.candidate_ids = tuple(task.id for task in matched.candidates)
            self._store.put(user_id, chat_id, pending)
            return LifecycleResult(
                kind=LifecycleKind.ASK_WHICH,
                text=format_choice_list(matched.candidates, _which_header(action)),
                show_choice_buttons=True,
                choice_count=len(matched.candidates),
            )
        by_person = _match_waiting_person(cleaned, waiting_for, open_tasks)
        if by_person is not None:
            return self._present_task(user_id, chat_id, by_person, pending)
        return LifecycleResult(kind=LifecycleKind.INFO, text=NOT_FOUND)

    def _present_task(
        self,
        user_id: int,
        chat_id: int,
        task: PlanCandidate,
        pending: PendingLifecycle,
    ) -> LifecycleResult:
        if pending.waiting_for:
            pending.waiting_for = _canonical_waiting_name(task, pending.waiting_for)
        if pending.action == LifecycleAction.POSTPONE and pending.new_deadline is None:
            self._store.put(
                user_id,
                chat_id,
                PendingLifecycle(
                    phase=LifecyclePhase.AWAITING_DEADLINE,
                    action=LifecycleAction.POSTPONE,
                    task_id=task.id,
                ),
            )
            return LifecycleResult(kind=LifecycleKind.ASK_DEADLINE, text=ASK_DEADLINE)
        self._store.put(
            user_id,
            chat_id,
            PendingLifecycle(
                phase=LifecyclePhase.CONFIRMING,
                action=pending.action,
                task_id=task.id,
                actual_minutes=pending.actual_minutes,
                new_deadline=pending.new_deadline,
                waiting_for=pending.waiting_for,
                original_text=pending.original_text,
                offer_new_task=pending.offer_new_task,
            ),
        )
        return self._confirm_result(task, pending)

    def _confirm_result(self, task: PlanCandidate, pending: PendingLifecycle) -> LifecycleResult:
        if pending.action == LifecycleAction.POSTPONE:
            assert pending.new_deadline is not None
            return LifecycleResult(
                kind=LifecycleKind.ASK_CONFIRM,
                text=format_postpone_preview(task, pending.new_deadline),
                show_confirm_buttons=True,
                confirm_label=BTN_POSTPONE,
            )
        if pending.action == LifecycleAction.WAITING:
            who = pending.waiting_for or task.waiting_for
            return LifecycleResult(
                kind=LifecycleKind.ASK_CONFIRM,
                text=format_waiting_preview(task, who),
                show_confirm_buttons=True,
                confirm_label=BTN_WAITING,
            )
        if pending.action == LifecycleAction.RESUME:
            return LifecycleResult(
                kind=LifecycleKind.ASK_CONFIRM,
                text=format_resume_preview(task),
                show_confirm_buttons=True,
                confirm_label=BTN_RESUME,
            )
        if pending.offer_new_task:
            return LifecycleResult(
                kind=LifecycleKind.ASK_CONFIRM,
                text=format_statement_complete_preview(task),
                show_confirm_buttons=True,
                show_statement_buttons=True,
                confirm_label=BTN_YES_DONE,
            )
        return LifecycleResult(
            kind=LifecycleKind.ASK_CONFIRM,
            text=format_complete_preview(task),
            show_confirm_buttons=True,
            confirm_label=BTN_DONE,
        )

    async def _continue_pending(
        self,
        user_id: int,
        chat_id: int,
        pending: PendingLifecycle,
        text: str,
    ) -> LifecycleResult:
        compact = " ".join(text.split()).casefold()
        if pending.phase == LifecyclePhase.AWAITING_DEADLINE:
            if compact in _CANCEL:
                return self.cancel(user_id, chat_id)
            deadline = parse_natural_deadline(text, self._today())
            if deadline is None or pending.task_id is None:
                return LifecycleResult(kind=LifecycleKind.ASK_DEADLINE, text=ASK_DEADLINE)
            task = await self._repository.get_user_task(user_id, pending.task_id)
            if task is None:
                self._store.clear(user_id, chat_id)
                return LifecycleResult(kind=LifecycleKind.INFO, text=NOT_FOUND)
            pending.new_deadline = deadline
            return self._present_task(user_id, chat_id, task, pending)
        if pending.phase == LifecyclePhase.AWAITING_TASK_REFERENCE:
            if compact in _CANCEL:
                return self.cancel(user_id, chat_id)
            return await self._resolve_task_reference(user_id, chat_id, pending, text)
        if pending.phase == LifecyclePhase.CHOOSING:
            pick = _PICK.match(text)
            if pick:
                return await self.choose(user_id, chat_id, int(pick.group(1)) - 1)
            if compact in _CANCEL:
                return self.cancel(user_id, chat_id)
            if looks_like_all_reference(text) and len(pending.candidate_ids) >= MIN_BATCH_ITEMS:
                return await self._present_batch_from_ids(
                    user_id,
                    chat_id,
                    pending.candidate_ids,
                    pending.original_text or text,
                )
            if len(split_completion_items(text)) >= MIN_BATCH_ITEMS:
                return await self._complete_from_reference(user_id, chat_id, text)
            return LifecycleResult(
                kind=LifecycleKind.ASK_WHICH,
                text="Обери номер зі списку або натисни кнопку.",
                show_choice_buttons=True,
                show_statement_buttons=pending.offer_new_task,
                choice_count=len(pending.candidate_ids),
            )
        if pending.phase in {LifecyclePhase.CONFIRMING, LifecyclePhase.CONFIRMING_BATCH}:
            if compact in _YES:
                return await self.confirm(user_id, chat_id)
            if compact in _CANCEL:
                return self.cancel(user_id, chat_id)
            batch = pending.phase == LifecyclePhase.CONFIRMING_BATCH
            return LifecycleResult(
                kind=LifecycleKind.ASK_CONFIRM,
                text="Підтверди або скасуй кнопками, або напиши «так» / «скасувати».",
                show_confirm_buttons=True,
                show_statement_buttons=pending.offer_new_task,
                confirm_label=BTN_ALL_DONE
                if batch
                else _label_for(pending.action, pending.offer_new_task),
            )
        if is_skip_all_actual_time(text):
            self._store.clear(user_id, chat_id)
            return LifecycleResult(kind=LifecycleKind.DONE, text=SKIPPED_ACTUAL_ALL)
        if is_skip_actual_time(text):
            return self._advance_actual(user_id, chat_id, pending, saved=None)
        minutes = parse_actual_minutes(text, allow_bare=True)
        if minutes is None or pending.task_id is None:
            if pending.task_id is not None and pending.retry_count == 0:
                pending.retry_count = 1
                self._store.put(user_id, chat_id, pending)
                return LifecycleResult(kind=LifecycleKind.ASK_ACTUAL, text=ASK_WHICH_TIME)
            return self._advance_actual(user_id, chat_id, pending, saved=None)
        saved = await self._repository.set_actual_minutes(user_id, pending.task_id, minutes)
        if not saved:
            self._store.clear(user_id, chat_id)
            return LifecycleResult(kind=LifecycleKind.INFO, text=NOT_FOUND)
        return self._advance_actual(user_id, chat_id, pending, saved=minutes)

    async def _resolve_task_reference(
        self,
        user_id: int,
        chat_id: int,
        pending: PendingLifecycle,
        text: str,
    ) -> LifecycleResult:
        """The reply to «яку саме?» — a list, a name, or «всі» pointing back."""
        source = text
        if looks_like_all_reference(text):
            earlier = pending.original_text or ""
            if len(split_completion_items(earlier)) < MIN_BATCH_ITEMS:
                return await self._offer_open_task_choices(user_id, chat_id, earlier or text)
            source = earlier
        return await self._complete_from_reference(user_id, chat_id, source)

    async def _offer_open_task_choices(
        self, user_id: int, chat_id: int, original_text: str
    ) -> LifecycleResult:
        """«всі» with nothing to point at: show what there is instead of re-asking."""
        open_tasks = await self._repository.list_user_tasks(user_id)
        if MIN_BATCH_ITEMS <= len(open_tasks) <= _MAX_REFERENCE_CHOICES:
            return self._present_statement_choices(
                user_id, chat_id, tuple(open_tasks), original_text
            )
        return LifecycleResult(kind=LifecycleKind.ASK_WHICH, text=ASK_LIST_COMPLETED)

    async def _complete_from_reference(
        self, user_id: int, chat_id: int, source: str
    ) -> LifecycleResult:
        """A named reference is a batch first; a single task keeps the old path."""
        open_tasks = await self._repository.list_user_tasks(user_id)
        batch = self._consider_batch(user_id, chat_id, source, open_tasks)
        if batch is not None:
            return batch
        self._store.clear(user_id, chat_id)
        return await self.handle_intent(
            user_id,
            chat_id,
            TaskIntent(kind=TaskIntentKind.COMPLETE_TASK, task_query=source),
            raw_text=source,
        )

    async def _apply_postpone(
        self, user_id: int, chat_id: int, pending: PendingLifecycle
    ) -> LifecycleResult:
        if pending.new_deadline is None or pending.task_id is None:
            return LifecycleResult(kind=LifecycleKind.INFO, text=NO_PENDING)
        outcome = await self._repository.postpone_user_task(
            user_id, pending.task_id, pending.new_deadline
        )
        self._store.clear(user_id, chat_id)
        if outcome != "updated":
            return LifecycleResult(kind=LifecycleKind.INFO, text=NOT_FOUND)
        return LifecycleResult(
            kind=LifecycleKind.DONE,
            text=format_postponed_message(pending.new_deadline),
        )

    async def _apply_waiting(
        self, user_id: int, chat_id: int, pending: PendingLifecycle
    ) -> LifecycleResult:
        if pending.task_id is None:
            return LifecycleResult(kind=LifecycleKind.INFO, text=NO_PENDING)
        outcome = await self._repository.set_user_task_waiting(
            user_id, pending.task_id, pending.waiting_for
        )
        self._store.clear(user_id, chat_id)
        if outcome != "updated":
            return LifecycleResult(kind=LifecycleKind.INFO, text=NOT_FOUND)
        return LifecycleResult(kind=LifecycleKind.DONE, text=WAITING_SET)

    async def _apply_resume(
        self, user_id: int, chat_id: int, pending: PendingLifecycle
    ) -> LifecycleResult:
        if pending.task_id is None:
            return LifecycleResult(kind=LifecycleKind.INFO, text=NO_PENDING)
        outcome = await self._repository.resume_user_task(user_id, pending.task_id)
        self._store.clear(user_id, chat_id)
        if outcome == "not_waiting":
            return LifecycleResult(kind=LifecycleKind.INFO, text=NOT_WAITING)
        if outcome != "updated":
            return LifecycleResult(kind=LifecycleKind.INFO, text=NOT_FOUND)
        return LifecycleResult(kind=LifecycleKind.DONE, text=RESUMED)

    async def _mark_batch_done(
        self,
        user_id: int,
        chat_id: int,
        pending: PendingLifecycle,
    ) -> LifecycleResult:
        completed: list[tuple[UUID, str]] = []
        moment = self._clock.now()
        for task_id, title in pending.batch_items:
            outcome = await self._repository.complete_user_task(
                user_id,
                task_id,
                completed_at=moment,
                actual_minutes=None,
            )
            if outcome == "completed":
                completed.append((task_id, title))
        if not completed:
            self._store.clear(user_id, chat_id)
            return LifecycleResult(kind=LifecycleKind.INFO, text=ALREADY_DONE)
        head_id, head_title = completed[0]
        self._store.put(
            user_id,
            chat_id,
            PendingLifecycle(
                phase=LifecyclePhase.AWAITING_ACTUAL,
                action=LifecycleAction.COMPLETE,
                task_id=head_id,
                current_title=head_title,
                actual_queue=tuple(completed[1:]),
            ),
        )
        return LifecycleResult(
            kind=LifecycleKind.ASK_ACTUAL,
            text=(
                f"{format_batch_completed(len(completed))}\n\n"
                f"{format_ask_actual_for(head_title)}"
            ),
        )

    def _advance_actual(
        self,
        user_id: int,
        chat_id: int,
        pending: PendingLifecycle,
        *,
        saved: int | None,
    ) -> LifecycleResult:
        """Move to the next completed task, or finish the chain."""
        if not pending.actual_queue:
            self._store.clear(user_id, chat_id)
            if saved is not None:
                return LifecycleResult(
                    kind=LifecycleKind.DONE, text=format_actual_recorded(saved)
                )
            return LifecycleResult(
                kind=LifecycleKind.DONE, text=format_completed_message(None)
            )
        next_id, next_title = pending.actual_queue[0]
        self._store.put(
            user_id,
            chat_id,
            PendingLifecycle(
                phase=LifecyclePhase.AWAITING_ACTUAL,
                action=LifecycleAction.COMPLETE,
                task_id=next_id,
                current_title=next_title,
                actual_queue=pending.actual_queue[1:],
            ),
        )
        question = format_ask_actual_for(next_title)
        if saved is not None:
            question = f"{format_actual_recorded(saved)}\n\n{question}"
        return LifecycleResult(kind=LifecycleKind.ASK_ACTUAL, text=question)

    async def _mark_done(
        self,
        user_id: int,
        chat_id: int,
        task_id: UUID,
        actual_minutes: int | None,
    ) -> LifecycleResult:
        outcome = await self._repository.complete_user_task(
            user_id,
            task_id,
            completed_at=self._clock.now(),
            actual_minutes=actual_minutes,
        )
        if outcome == "not_found":
            self._store.clear(user_id, chat_id)
            return LifecycleResult(kind=LifecycleKind.INFO, text=NOT_FOUND)
        if outcome == "already_done":
            self._store.clear(user_id, chat_id)
            return LifecycleResult(kind=LifecycleKind.INFO, text=ALREADY_DONE)
        if actual_minutes is not None:
            self._store.clear(user_id, chat_id)
            return LifecycleResult(
                kind=LifecycleKind.DONE,
                text=format_completed_message(actual_minutes),
            )
        self._store.put(
            user_id,
            chat_id,
            PendingLifecycle(
                phase=LifecyclePhase.AWAITING_ACTUAL,
                action=LifecycleAction.COMPLETE,
                task_id=task_id,
            ),
        )
        return LifecycleResult(
            kind=LifecycleKind.ASK_ACTUAL,
            text=f"{format_completed_message(None)}\n\n{ASK_ACTUAL}",
        )

    def _today(self) -> date:
        return self._clock.now().astimezone(KYIV).date()


def _which_header(action: LifecycleAction) -> str:
    if action == LifecycleAction.POSTPONE:
        return ASK_WHICH_POSTPONE
    if action == LifecycleAction.WAITING:
        return ASK_WHICH_WAITING
    if action == LifecycleAction.RESUME:
        return ASK_WHICH_RESUME
    return "Яку задачу позначити виконаною?"


def _label_for(action: LifecycleAction, offer_new_task: bool = False) -> str:
    if action == LifecycleAction.POSTPONE:
        return BTN_POSTPONE
    if action == LifecycleAction.WAITING:
        return BTN_WAITING
    if action == LifecycleAction.RESUME:
        return BTN_RESUME
    if offer_new_task:
        return BTN_YES_DONE
    return BTN_DONE


def _match_waiting_person(
    query: str, waiting_for: str | None, tasks: list[PlanCandidate]
) -> PlanCandidate | None:
    for raw in (waiting_for, query):
        if not raw or not raw.strip():
            continue
        key = normalize_entity_name(raw)
        hits = [
            task
            for task in tasks
            if any(_person_names_match(key, normalize_entity_name(person)) for person in task.people)
            or (task.waiting_for and _person_names_match(key, normalize_entity_name(task.waiting_for)))
        ]
        if len(hits) == 1:
            return hits[0]
    return None


def _canonical_waiting_name(task: PlanCandidate, name: str) -> str:
    key = normalize_entity_name(name)
    for person in task.people:
        if _person_names_match(key, normalize_entity_name(person)):
            return person
    if task.waiting_for and _person_names_match(key, normalize_entity_name(task.waiting_for)):
        return task.waiting_for
    return name
