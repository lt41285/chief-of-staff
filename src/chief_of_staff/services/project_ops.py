"""Project list / rename / merge / alias — deterministic after intent parse."""

from dataclasses import dataclass
from enum import StrEnum

from loguru import logger

from chief_of_staff.infrastructure.database.repository import SqlAlchemyTaskRepository
from chief_of_staff.models.project_command import ProjectIntent, ProjectIntentKind
from chief_of_staff.services.project_format import (
    ALREADY_ACTIVE,
    ALREADY_ARCHIVED,
    ASK_CREATE_NAME,
    BTN_ARCHIVE,
    BTN_CREATE,
    BTN_RESTORE,
    CANCELLED,
    DELETE_FOREVER,
    MERGED,
    NAME_TAKEN,
    NO_PENDING,
    NOT_FOUND,
    REFUSE_PERMANENT_DELETE,
    RENAMED,
    format_archive_prompt,
    format_archived,
    format_archived_intake,
    format_archived_project_list,
    format_create_prompt,
    format_delete_forever_prompt,
    format_merge_prompt,
    format_project_created,
    format_project_exists,
    format_project_exists_as_alias,
    format_project_list,
    format_project_tasks,
    format_rename_prompt,
    format_restore_prompt,
    format_restored,
)
from chief_of_staff.services.project_intent import (
    ProjectIntentParser,
    looks_like_project_command,
    parse_project_intent_deterministic,
)
from chief_of_staff.services.project_session import (
    InMemoryProjectOpStore,
    PendingProjectOp,
    ProjectPendingKind,
)


class ProjectKind(StrEnum):
    LIST = "list"
    TASKS = "tasks"
    ASK_CONFIRM = "ask_confirm"
    DONE = "done"
    CANCELLED = "cancelled"
    INFO = "info"


@dataclass(frozen=True)
class ProjectResult:
    kind: ProjectKind
    text: str
    show_confirm_buttons: bool = False
    confirm_label: str = "✅ Confirm"


class ProjectManagementService:
    def __init__(
        self,
        repository: SqlAlchemyTaskRepository,
        store: InMemoryProjectOpStore,
        parser: ProjectIntentParser | None = None,
    ) -> None:
        self._repository = repository
        self._store = store
        self._parser = parser

    def is_awaiting_input(self, user_id: int, chat_id: int) -> bool:
        return self._store.get(user_id, chat_id) is not None

    def describe_pending(self, user_id: int, chat_id: int) -> dict | None:
        pending = self._store.get(user_id, chat_id)
        if pending is None:
            return None
        awaiting = {
            ProjectPendingKind.ASK_NAME: "project_name",
            ProjectPendingKind.CREATE: "confirmation",
            ProjectPendingKind.RENAME: "confirmation",
            ProjectPendingKind.MERGE: "confirmation",
            ProjectPendingKind.ARCHIVE: "confirmation",
            ProjectPendingKind.RESTORE: "confirmation",
            ProjectPendingKind.DELETE: "strong_confirmation",
        }[pending.kind]
        action = {
            ProjectPendingKind.ASK_NAME: "create_project",
            ProjectPendingKind.CREATE: "create_project",
            ProjectPendingKind.RENAME: "rename_project",
            ProjectPendingKind.MERGE: "merge_projects",
            ProjectPendingKind.ARCHIVE: "archive_project",
            ProjectPendingKind.RESTORE: "restore_project",
            ProjectPendingKind.DELETE: "delete_project_permanently",
        }[pending.kind]
        return {
            "pending_action": action,
            "awaiting": awaiting,
            "draft": {
                "new_name": pending.new_name,
                "old_name": pending.old_name,
                "keep_name": pending.keep_name,
                "source_names": list(pending.source_names),
            },
        }

    async def classify(self, text: str) -> ProjectIntent:
        parsed = parse_project_intent_deterministic(text)
        if parsed is not None:
            return parsed
        if self._parser is None or not looks_like_project_command(text):
            return ProjectIntent(kind=ProjectIntentKind.CREATE_TASK)
        try:
            return await self._parser.parse(text)
        except Exception:
            logger.exception("Project intent parse failed")
            return ProjectIntent(kind=ProjectIntentKind.CREATE_TASK)

    async def handle_user_text(self, user_id: int, chat_id: int, text: str) -> ProjectResult:
        pending = self._store.get(user_id, chat_id)
        if pending is not None:
            compact = " ".join(text.split()).casefold()
            if pending.kind == ProjectPendingKind.DELETE:
                if compact.replace(" ", "") == DELETE_FOREVER.casefold().replace(" ", ""):
                    return await self.confirm(user_id, chat_id)
                if compact in {"ні", "нет", "no", "скасувати", "cancel"}:
                    return self.cancel(user_id, chat_id)
                return ProjectResult(
                    kind=ProjectKind.ASK_CONFIRM,
                    text=format_delete_forever_prompt(pending.new_name),
                    show_confirm_buttons=True,
                    confirm_label=DELETE_FOREVER,
                )
            if pending.kind == ProjectPendingKind.ASK_NAME:
                if compact in {"ні", "нет", "no", "скасувати", "cancel"}:
                    return self.cancel(user_id, chat_id)
                return await self._begin_create(user_id, chat_id, text)
            if compact in {"так", "yes", "y", "підтверджую", "confirm"}:
                return await self.confirm(user_id, chat_id)
            if compact in {"ні", "нет", "no", "скасувати", "cancel"}:
                return self.cancel(user_id, chat_id)
            return ProjectResult(
                kind=ProjectKind.ASK_CONFIRM,
                text="Підтверди або скасуй кнопками, або напиши «так» / «скасувати».",
                show_confirm_buttons=True,
                confirm_label=self._confirm_label(pending),
            )
        intent = await self.classify(text)
        return await self.handle_intent(user_id, chat_id, intent)

    async def handle_intent(
        self, user_id: int, chat_id: int, intent: ProjectIntent
    ) -> ProjectResult:
        if intent.kind == ProjectIntentKind.LIST_PROJECTS:
            rows = await self._repository.list_projects_for_user(user_id)
            return ProjectResult(kind=ProjectKind.LIST, text=format_project_list(rows))
        if intent.kind == ProjectIntentKind.LIST_ARCHIVED_PROJECTS:
            rows = await self._repository.list_projects_for_user(user_id, status="archived")
            return ProjectResult(kind=ProjectKind.LIST, text=format_archived_project_list(rows))
        if intent.kind == ProjectIntentKind.CREATE_PROJECT:
            return await self._begin_create(user_id, chat_id, intent.new_name or "")
        if intent.kind == ProjectIntentKind.PROJECT_TASKS:
            return await self._project_tasks(user_id, intent.project_query or "")
        if intent.kind == ProjectIntentKind.ARCHIVE_PROJECT:
            return await self._begin_archive(user_id, chat_id, intent.project_query or "")
        if intent.kind == ProjectIntentKind.RESTORE_PROJECT:
            return await self._begin_restore(user_id, chat_id, intent.project_query or "")
        if intent.kind == ProjectIntentKind.DELETE_PROJECT_PERMANENTLY:
            return await self._begin_delete(user_id, chat_id, intent.project_query or "")
        if intent.kind == ProjectIntentKind.RENAME_PROJECT:
            return await self._begin_rename(user_id, chat_id, intent.old_name or "", intent.new_name or "")
        if intent.kind == ProjectIntentKind.MERGE_PROJECTS:
            return await self._begin_merge(
                user_id, chat_id, intent.keep_name or "", tuple(intent.merge_names)
            )
        if intent.kind == ProjectIntentKind.LINK_ALIAS:
            return await self._link_or_merge(
                user_id, chat_id, intent.keep_name or "", intent.alias_name or ""
            )
        return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)

    async def confirm(self, user_id: int, chat_id: int) -> ProjectResult:
        pending = self._store.get(user_id, chat_id)
        if pending is None:
            return ProjectResult(kind=ProjectKind.INFO, text=NO_PENDING)
        try:
            if pending.kind == ProjectPendingKind.RENAME:
                renamed = await self._repository.rename_project(
                    user_id, pending.old_name, pending.new_name
                )
                self._store.clear(user_id, chat_id)
                return ProjectResult(
                    kind=ProjectKind.DONE,
                    text=f"{RENAMED}\n📁 {renamed.name}",
                )
            if pending.kind == ProjectPendingKind.CREATE:
                try:
                    created = await self._repository.create_user_project(
                        user_id,
                        pending.new_name,
                        telegram_chat_id=chat_id,
                    )
                except ValueError as exc:
                    self._store.clear(user_id, chat_id)
                    return self._create_error(str(exc))
                self._store.clear(user_id, chat_id)
                return ProjectResult(
                    kind=ProjectKind.DONE,
                    text=format_project_created(created.name),
                )
            if pending.kind == ProjectPendingKind.ARCHIVE:
                updated = await self._repository.set_user_project_status(
                    user_id, pending.new_name, "archived"
                )
                self._store.clear(user_id, chat_id)
                return ProjectResult(kind=ProjectKind.DONE, text=format_archived(updated.name))
            if pending.kind == ProjectPendingKind.RESTORE:
                updated = await self._repository.set_user_project_status(
                    user_id, pending.new_name, "active"
                )
                self._store.clear(user_id, chat_id)
                return ProjectResult(kind=ProjectKind.DONE, text=format_restored(updated.name))
            if pending.kind == ProjectPendingKind.DELETE:
                outcome = await self._repository.delete_empty_user_project(
                    user_id, pending.new_name
                )
                self._store.clear(user_id, chat_id)
                if outcome == "has_history":
                    return ProjectResult(kind=ProjectKind.INFO, text=REFUSE_PERMANENT_DELETE)
                return ProjectResult(
                    kind=ProjectKind.DONE,
                    text=f"Проєкт «{pending.new_name}» видалено назавжди.",
                )
            kept, moved = await self._repository.merge_projects(
                user_id, pending.keep_name, pending.source_names
            )
            self._store.clear(user_id, chat_id)
            return ProjectResult(
                kind=ProjectKind.DONE,
                text=f"{MERGED}\n📁 {kept.name}\nПеренесено задач: {moved}",
            )
        except ValueError as exc:
            logger.info("Project op failed: {}", exc)
            self._store.clear(user_id, chat_id)
            if str(exc) == "name_taken":
                return ProjectResult(kind=ProjectKind.INFO, text=NAME_TAKEN)
            return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)

    def _create_error(self, message: str) -> ProjectResult:
        if message.startswith("exists_alias:"):
            return ProjectResult(
                kind=ProjectKind.INFO,
                text=format_project_exists_as_alias(message.split(":", 1)[1]),
            )
        if message.startswith("exists_canonical:"):
            return ProjectResult(
                kind=ProjectKind.INFO,
                text=format_project_exists(message.split(":", 1)[1]),
            )
        return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)

    def cancel(self, user_id: int, chat_id: int) -> ProjectResult:
        if self._store.get(user_id, chat_id) is None:
            return ProjectResult(kind=ProjectKind.INFO, text=NO_PENDING)
        self._store.clear(user_id, chat_id)
        return ProjectResult(kind=ProjectKind.CANCELLED, text=CANCELLED)

    def _confirm_label(self, pending: PendingProjectOp) -> str:
        if pending.kind == ProjectPendingKind.CREATE:
            return BTN_CREATE
        if pending.kind == ProjectPendingKind.ARCHIVE:
            return BTN_ARCHIVE
        if pending.kind == ProjectPendingKind.RESTORE:
            return BTN_RESTORE
        if pending.kind == ProjectPendingKind.DELETE:
            return DELETE_FOREVER
        return "✅ Confirm"

    async def _project_tasks(self, user_id: int, query: str) -> ProjectResult:
        found = await self._repository.list_open_tasks_for_project_name(user_id, query)
        if found is None:
            archived = await self._repository.resolve_user_project(
                user_id, query, include_archived=True
            )
            if archived is not None and archived.status == "archived":
                return ProjectResult(
                    kind=ProjectKind.INFO,
                    text=format_archived_intake(archived.name),
                )
            return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)
        name, tasks = found
        resolved = await self._repository.resolve_user_project(
            user_id, name, include_archived=True
        )
        text = format_project_tasks(name, tasks)
        if resolved is not None and resolved.status == "archived":
            text = f"📦 Проєкт «{name}» в архіві.\n\n{text}"
        return ProjectResult(kind=ProjectKind.TASKS, text=text)

    async def _begin_create(self, user_id: int, chat_id: int, name: str) -> ProjectResult:
        cleaned = " ".join(name.split()).strip(" .")
        if not cleaned:
            self._store.put(
                user_id,
                chat_id,
                PendingProjectOp(kind=ProjectPendingKind.ASK_NAME),
            )
            return ProjectResult(kind=ProjectKind.INFO, text=ASK_CREATE_NAME)
        resolved = await self._repository.resolve_user_project(user_id, cleaned)
        if resolved is not None:
            hit = await self._exists_how(user_id, cleaned, resolved.name)
            if hit == "alias":
                return ProjectResult(
                    kind=ProjectKind.INFO, text=format_project_exists_as_alias(resolved.name)
                )
            return ProjectResult(kind=ProjectKind.INFO, text=format_project_exists(resolved.name))
        archived = await self._repository.resolve_user_project(
            user_id, cleaned, include_archived=True
        )
        if archived is not None and archived.status == "archived":
            return ProjectResult(kind=ProjectKind.INFO, text=format_archived_intake(archived.name))
        self._store.put(
            user_id,
            chat_id,
            PendingProjectOp(kind=ProjectPendingKind.CREATE, new_name=cleaned),
        )
        return ProjectResult(
            kind=ProjectKind.ASK_CONFIRM,
            text=format_create_prompt(cleaned),
            show_confirm_buttons=True,
            confirm_label=BTN_CREATE,
        )

    async def _exists_how(self, user_id: int, requested: str, canonical: str) -> str:
        from chief_of_staff.services.names import normalize_project_name

        if normalize_project_name(requested) == normalize_project_name(canonical):
            return "canonical"
        return "alias"

    async def _begin_rename(
        self, user_id: int, chat_id: int, old_name: str, new_name: str
    ) -> ProjectResult:
        if not old_name or not new_name:
            return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)
        resolved = await self._repository.resolve_user_project(user_id, old_name)
        if resolved is None:
            return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)
        self._store.put(
            user_id,
            chat_id,
            PendingProjectOp(
                kind=ProjectPendingKind.RENAME,
                old_name=old_name,
                new_name=new_name,
            ),
        )
        return ProjectResult(
            kind=ProjectKind.ASK_CONFIRM,
            text=format_rename_prompt(resolved.name, new_name),
            show_confirm_buttons=True,
        )

    async def _begin_merge(
        self,
        user_id: int,
        chat_id: int,
        keep_name: str,
        sources: tuple[str, ...],
    ) -> ProjectResult:
        keep = await self._repository.resolve_user_project(user_id, keep_name)
        if keep is None:
            return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)
        resolved_sources: list[str] = []
        total = 0
        for name in sources:
            found = await self._repository.resolve_user_project(user_id, name)
            if found is None:
                return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)
            if found.id == keep.id:
                continue
            total += await self._repository.count_all_tasks_on_project(user_id, found.id)
            resolved_sources.append(found.name)
        if not resolved_sources:
            return ProjectResult(kind=ProjectKind.INFO, text="Це вже один і той самий проєкт.")
        self._store.put(
            user_id,
            chat_id,
            PendingProjectOp(
                kind=ProjectPendingKind.MERGE,
                keep_name=keep.name,
                source_names=tuple(resolved_sources),
                task_count=total,
            ),
        )
        return ProjectResult(
            kind=ProjectKind.ASK_CONFIRM,
            text=format_merge_prompt(keep.name, tuple(resolved_sources), total),
            show_confirm_buttons=True,
        )

    async def _link_or_merge(
        self, user_id: int, chat_id: int, keep_name: str, alias_name: str
    ) -> ProjectResult:
        keep = await self._repository.resolve_user_project(user_id, keep_name)
        other = await self._repository.resolve_user_project(user_id, alias_name)
        if keep is None:
            return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)
        if other is None:
            added = await self._repository.add_alias(user_id, keep.name, alias_name)
            if added is None:
                return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)
            return ProjectResult(
                kind=ProjectKind.DONE,
                text=f"Запам'ятав: «{alias_name}» — це 📁 {added.name}.",
            )
        if other.id == keep.id:
            await self._repository.add_alias(user_id, keep.name, alias_name)
            return ProjectResult(
                kind=ProjectKind.DONE,
                text=f"«{alias_name}» вже вказує на 📁 {keep.name}.",
            )
        return await self._begin_merge(user_id, chat_id, keep.name, (other.name,))

    async def _begin_archive(self, user_id: int, chat_id: int, name: str) -> ProjectResult:
        cleaned = " ".join(name.split()).strip(" .")
        if not cleaned:
            return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)
        resolved = await self._repository.resolve_user_project(
            user_id, cleaned, include_archived=True
        )
        if resolved is None:
            return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)
        if resolved.status == "archived":
            return ProjectResult(kind=ProjectKind.INFO, text=ALREADY_ARCHIVED)
        self._store.put(
            user_id,
            chat_id,
            PendingProjectOp(kind=ProjectPendingKind.ARCHIVE, new_name=resolved.name),
        )
        return ProjectResult(
            kind=ProjectKind.ASK_CONFIRM,
            text=format_archive_prompt(resolved.name),
            show_confirm_buttons=True,
            confirm_label=BTN_ARCHIVE,
        )

    async def _begin_restore(self, user_id: int, chat_id: int, name: str) -> ProjectResult:
        cleaned = " ".join(name.split()).strip(" .")
        if not cleaned:
            return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)
        resolved = await self._repository.resolve_user_project(
            user_id, cleaned, include_archived=True
        )
        if resolved is None:
            return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)
        if resolved.status == "active":
            return ProjectResult(kind=ProjectKind.INFO, text=ALREADY_ACTIVE)
        self._store.put(
            user_id,
            chat_id,
            PendingProjectOp(kind=ProjectPendingKind.RESTORE, new_name=resolved.name),
        )
        return ProjectResult(
            kind=ProjectKind.ASK_CONFIRM,
            text=format_restore_prompt(resolved.name),
            show_confirm_buttons=True,
            confirm_label=BTN_RESTORE,
        )

    async def _begin_delete(self, user_id: int, chat_id: int, name: str) -> ProjectResult:
        cleaned = " ".join(name.split()).strip(" .")
        if not cleaned:
            return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)
        resolved = await self._repository.resolve_user_project(
            user_id, cleaned, include_archived=True
        )
        if resolved is None:
            return ProjectResult(kind=ProjectKind.INFO, text=NOT_FOUND)
        history = await self._repository.count_project_history(user_id, resolved.id)
        if history > 0:
            return ProjectResult(kind=ProjectKind.INFO, text=REFUSE_PERMANENT_DELETE)
        self._store.put(
            user_id,
            chat_id,
            PendingProjectOp(kind=ProjectPendingKind.DELETE, new_name=resolved.name),
        )
        return ProjectResult(
            kind=ProjectKind.ASK_CONFIRM,
            text=format_delete_forever_prompt(resolved.name),
            show_confirm_buttons=True,
            confirm_label=DELETE_FOREVER,
        )
