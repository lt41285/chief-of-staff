"""Task capture use-case: interpret, validate, persist on confirm."""

from dataclasses import dataclass
from enum import StrEnum
import re

from loguru import logger

from chief_of_staff.infrastructure.database.repository import TaskRepository
from chief_of_staff.models.task import TaskDraft
from chief_of_staff.services.clock import Clock
from chief_of_staff.services.task_card import (
    CANCELLED,
    EDIT_PROMPT,
    NO_PENDING_TASK,
    SAVE_FAILED,
    format_confirmation_card,
    format_created_message,
)
from chief_of_staff.services.task_interpreter import TaskInterpreter
from chief_of_staff.services.project_format import format_unknown_intake_project
from chief_of_staff.services.draft_patch import (
    apply_draft_patch,
    apply_title_corrections,
    clean_task_title,
    extract_utterance_patch,
    is_compound_intake_reply,
    looks_like_confirm_candidate,
    looks_like_project_only_reply,
    sanitize_ai_patch,
)
from chief_of_staff.services.intake_project import (
    parse_project_ordinal,
    resolve_intake_project,
    strip_project_framing,
)
from chief_of_staff.services.task_questions import follow_up_questions, format_follow_up
from chief_of_staff.services.task_session import DraftPhase, InMemoryTaskSessionStore, TaskSession
from chief_of_staff.services.task_validation import (
    fill_missing_from_user_reply,
    is_ready,
    missing_required_fields,
)

_YES = frozenset({"yes", "y", "так"})
_EDIT = frozenset({"edit", "редагувати"})
_CANCEL = frozenset({"cancel", "скасувати"})


class IntakeKind(StrEnum):
    FOLLOW_UP = "follow_up"
    CONFIRMATION = "confirmation"
    CREATED = "created"
    SAVE_FAILED = "save_failed"
    EDIT_PROMPT = "edit_prompt"
    CANCELLED = "cancelled"
    INFO = "info"
    ASK_PROJECT = "ask_project"
    DISAMBIGUATE = "disambiguate"


@dataclass(frozen=True)
class IntakeResult:
    kind: IntakeKind
    text: str
    show_confirm_buttons: bool = False
    show_disambiguate_buttons: bool = False


class TaskIntakeService:
    def __init__(
        self,
        interpreter: TaskInterpreter,
        store: InMemoryTaskSessionStore,
        repository: TaskRepository,
        clock: Clock | None = None,
    ) -> None:
        self._interpreter = interpreter
        self._store = store
        self._repository = repository
        self._clock = clock or Clock()

    def is_awaiting_input(self, user_id: int, chat_id: int) -> bool:
        return self._store.get(user_id, chat_id) is not None

    def describe_pending(self, user_id: int, chat_id: int) -> dict | None:
        session = self._store.get(user_id, chat_id)
        if session is None:
            return None
        missing = missing_required_fields(session.draft)
        awaiting = "confirmation" if session.phase == DraftPhase.CONFIRMING else (
            ",".join(field.value for field in missing) if missing else session.phase.value
        )
        draft = session.draft
        return {
            "pending_action": "create_task",
            "awaiting": awaiting,
            "presented_projects": list(session.presented_project_names),
            "similar_project": session.similar_project_name,
            "draft": {
                "task_title": draft.task_title,
                "project": draft.project,
                "people": list(draft.people),
                "deadline": draft.deadline.isoformat() if draft.deadline else None,
                "estimated_minutes": draft.estimated_minutes,
                "desired_outcome": draft.desired_outcome,
                "phase": session.phase.value,
            },
        }

    async def handle_user_text(
        self,
        user_id: int,
        text: str,
        *,
        chat_id: int,
        extracted: TaskDraft | None = None,
    ) -> IntakeResult:
        command = _normalize_command(text)
        session = self._store.get(user_id, chat_id)

        if session and session.phase == DraftPhase.CONFIRMING:
            if command == "yes":
                return await self.confirm(user_id, chat_id)
            if command == "edit":
                return self.begin_edit(user_id, chat_id)
            if command == "cancel":
                return self.cancel(user_id, chat_id)
        if session and session.phase in {
            DraftPhase.AWAITING_PROJECT,
            DraftPhase.DISAMBIGUATING,
        }:
            if command == "cancel":
                return self.cancel(user_id, chat_id)
            if command == "yes" and session.similar_project_name:
                merged = session.draft.model_copy(
                    update={"project": session.similar_project_name}
                )
                return await self._after_draft_update(
                    user_id, chat_id, merged, last_user_text=text
                )
            if looks_like_confirm_candidate(text) and session.similar_project_name:
                merged = session.draft.model_copy(
                    update={"project": session.similar_project_name}
                )
                return await self._after_draft_update(
                    user_id, chat_id, merged, last_user_text=text
                )
            ordinal = parse_project_ordinal(text, session.presented_project_names)
            if ordinal:
                merged = session.draft.model_copy(update={"project": ordinal})
                return await self._after_draft_update(
                    user_id, chat_id, merged, last_user_text=text
                )
            if looks_like_project_only_reply(text) and not is_compound_intake_reply(text):
                merged = session.draft.model_copy(
                    update={"project": strip_project_framing(text.strip())}
                )
                return await self._after_draft_update(
                    user_id, chat_id, merged, last_user_text=text
                )
            return await self._patch_draft(
                user_id, chat_id, text, session=session, extracted=extracted
            )

        return await self._patch_draft(
            user_id, chat_id, text, session=session, extracted=extracted
        )

    async def _patch_draft(
        self,
        user_id: int,
        chat_id: int,
        text: str,
        *,
        session: TaskSession | None,
        extracted: TaskDraft | None,
    ) -> IntakeResult:
        today = self._clock.now().date()
        base = session.draft if session else TaskDraft()
        missing_before = missing_required_fields(base)
        incoming = extracted
        if incoming is None:
            incoming = await self._interpreter.extract(
                text,
                current_draft=base if session else None,
                missing=missing_before if session else (),
            )
        incoming = sanitize_ai_patch(base, incoming, text)
        merged = apply_draft_patch(base, incoming)
        cues = extract_utterance_patch(text, today)
        merged = apply_draft_patch(merged, cues)
        presented = session.presented_project_names if session else ()
        if not presented:
            list_projects = getattr(self._repository, "list_projects_for_user", None)
            if list_projects is not None:
                presented = tuple(name for name, _count in await list_projects(user_id))
        ordinal = parse_project_ordinal(text, presented)
        if ordinal:
            merged = merged.model_copy(update={"project": ordinal})
        if session is not None:
            merged = fill_missing_from_user_reply(
                merged, text, missing_before, today=today
            )
        title = clean_task_title(merged.task_title, project=merged.project)
        title = apply_title_corrections(title, text)
        if cues.project:
            title = apply_title_corrections(title, cues.project)
        merged = merged.model_copy(update={"task_title": title})
        return await self._after_draft_update(user_id, chat_id, merged, last_user_text=text)

    async def confirm(self, user_id: int, chat_id: int) -> IntakeResult:
        session = self._store.get(user_id, chat_id)
        if session is None or session.phase != DraftPhase.CONFIRMING or not is_ready(session.draft):
            return IntakeResult(kind=IntakeKind.INFO, text=NO_PENDING_TASK)
        draft = session.draft
        project_id = None
        bound = await self._bind_canonical_project(user_id, chat_id, draft)
        if isinstance(bound, IntakeResult):
            return bound
        draft, project_id = bound
        try:
            await self._repository.save_validated_task(
                draft,
                telegram_user_id=user_id,
                telegram_chat_id=chat_id,
                project_id=project_id,
            )
        except ValueError:
            logger.exception("Task persist rejected (missing project)")
            return await self._unknown_project(user_id, chat_id, draft)
        except Exception:
            logger.exception("Failed to persist validated task")
            return IntakeResult(
                kind=IntakeKind.SAVE_FAILED,
                text=SAVE_FAILED,
                show_confirm_buttons=True,
            )
        self._store.clear(user_id, chat_id)
        return IntakeResult(kind=IntakeKind.CREATED, text=format_created_message(draft))

    def begin_edit(self, user_id: int, chat_id: int) -> IntakeResult:
        session = self._store.get(user_id, chat_id)
        if session is None:
            return IntakeResult(kind=IntakeKind.INFO, text=NO_PENDING_TASK)
        self._store.put(
            user_id,
            chat_id,
            TaskSession(draft=session.draft, phase=DraftPhase.COLLECTING),
        )
        return IntakeResult(kind=IntakeKind.EDIT_PROMPT, text=EDIT_PROMPT)

    def cancel(self, user_id: int, chat_id: int) -> IntakeResult:
        if self._store.get(user_id, chat_id) is None:
            return IntakeResult(kind=IntakeKind.INFO, text=NO_PENDING_TASK)
        self._store.clear(user_id, chat_id)
        return IntakeResult(kind=IntakeKind.CANCELLED, text=CANCELLED)

    async def choose_existing_project(self, user_id: int, chat_id: int) -> IntakeResult:
        session = self._store.get(user_id, chat_id)
        if session is None or session.similar_project_name is None:
            return IntakeResult(kind=IntakeKind.INFO, text=NO_PENDING_TASK)
        merged = session.draft.model_copy(update={"project": session.similar_project_name})
        return await self._after_draft_update(
            user_id, chat_id, merged, last_user_text=session.similar_project_name
        )

    async def choose_new_project(self, user_id: int, chat_id: int) -> IntakeResult:
        session = self._store.get(user_id, chat_id)
        if session is None:
            return IntakeResult(kind=IntakeKind.INFO, text=NO_PENDING_TASK)
        requested = session.requested_project or session.draft.project or "проєкт"
        return await self._unknown_project(user_id, chat_id, session.draft, requested=requested)

    async def _after_draft_update(
        self,
        user_id: int,
        chat_id: int,
        draft: TaskDraft,
        *,
        last_user_text: str,
    ) -> IntakeResult:
        missing = missing_required_fields(draft)
        previous = self._store.get(user_id, chat_id)
        presented = previous.presented_project_names if previous else ()
        if missing:
            self._store.put(
                user_id,
                chat_id,
                TaskSession(
                    draft=draft,
                    phase=DraftPhase.COLLECTING,
                    presented_project_names=presented,
                    similar_project_name=previous.similar_project_name if previous else None,
                ),
            )
            questions = follow_up_questions(missing, last_user_text=last_user_text)
            return IntakeResult(
                kind=IntakeKind.FOLLOW_UP,
                text=format_follow_up(questions),
            )
        bound = await self._bind_canonical_project(user_id, chat_id, draft)
        if isinstance(bound, IntakeResult):
            return bound
        draft, _project_id = bound
        title = clean_task_title(draft.task_title, project=draft.project)
        title = apply_title_corrections(title, draft.project or "")
        draft = draft.model_copy(update={"task_title": title})
        self._store.put(
            user_id,
            chat_id,
            TaskSession(
                draft=draft,
                phase=DraftPhase.CONFIRMING,
                presented_project_names=presented,
            ),
        )
        return IntakeResult(
            kind=IntakeKind.CONFIRMATION,
            text=format_confirmation_card(draft),
            show_confirm_buttons=True,
        )

    async def _bind_canonical_project(
        self,
        user_id: int,
        chat_id: int,
        draft: TaskDraft,
    ) -> IntakeResult | tuple[TaskDraft, object]:
        resolve = getattr(self._repository, "resolve_user_project", None)
        if resolve is None or not draft.project:
            return draft, None
        match = await resolve_intake_project(
            self._repository,
            user_id,
            draft.project,
            presented=self._presented_names(user_id, chat_id),
        )
        if match.status == "confirm" and match.name:
            return await self._unknown_project(
                user_id,
                chat_id,
                draft,
                requested=match.asked or draft.project,
                similar_name=match.name,
            )
        name = match.name if match.status == "resolved" and match.name else draft.project
        if match.status == "resolved" and match.name:
            title = apply_title_corrections(draft.task_title, match.name)
            title = clean_task_title(title, project=match.name)
            draft = draft.model_copy(update={"project": match.name, "task_title": title})
            name = match.name
        existing = await self._resolve_active_project(user_id, name)
        if existing == "archived":
            from chief_of_staff.services.project_format import format_archived_intake

            archived = await resolve(user_id, name, include_archived=True)
            return IntakeResult(
                kind=IntakeKind.INFO,
                text=format_archived_intake(archived.name if archived else name),
            )
        if existing is None:
            return await self._unknown_project(user_id, chat_id, draft, requested=name)
        return draft.model_copy(update={"project": existing.name}), existing.id

    def _presented_names(self, user_id: int, chat_id: int) -> tuple[str, ...]:
        session = self._store.get(user_id, chat_id)
        if session is None:
            return ()
        return session.presented_project_names

    async def _unknown_project(
        self,
        user_id: int,
        chat_id: int,
        draft: TaskDraft,
        *,
        requested: str | None = None,
        similar_name: str | None = None,
    ) -> IntakeResult:
        wanted = requested or draft.project or ""
        if similar_name is None:
            suggest = getattr(self._repository, "suggest_similar_project", None)
            if suggest is not None and wanted:
                similar = await suggest(user_id, wanted)
                if similar is not None:
                    similar_name = similar.name
        names: list[str] = []
        list_projects = getattr(self._repository, "list_projects_for_user", None)
        if list_projects is not None:
            names = [name for name, _count in await list_projects(user_id)]
        self._store.put(
            user_id,
            chat_id,
            TaskSession(
                draft=draft,
                phase=DraftPhase.AWAITING_PROJECT,
                similar_project_name=similar_name,
                requested_project=wanted,
                presented_project_names=tuple(names),
            ),
        )
        return IntakeResult(
            kind=IntakeKind.ASK_PROJECT,
            text=format_unknown_intake_project(wanted, names, similar=similar_name),
            show_disambiguate_buttons=similar_name is not None,
        )

    async def _resolve_active_project(self, user_id: int, name: str):
        resolve = getattr(self._repository, "resolve_user_project", None)
        if resolve is None:
            return None
        existing = await resolve(user_id, name)
        if existing is not None:
            return existing
        try:
            archived = await resolve(user_id, name, include_archived=True)
        except TypeError:
            return None
        if archived is not None and getattr(archived, "status", "active") == "archived":
            return "archived"
        return None


def _normalize_command(text: str) -> str | None:
    compact = re.sub(r"\s+", " ", text).strip().casefold()
    if compact in _YES:
        return "yes"
    if compact in _EDIT:
        return "edit"
    if compact in _CANCEL:
        return "cancel"
    return None
