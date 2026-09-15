"""Ukrainian copy for project management."""

from chief_of_staff.models.plan import PlanCandidate
from chief_of_staff.services.task_query_format import format_project_task_list

NOT_FOUND = "Не знайшов такий проєкт серед твоїх."
RENAMED = "Проєкт перейменовано."
MERGED = "Проєкти об'єднано."
CANCELLED = "Скасовано. Нічого не змінено."
NO_PENDING = "Немає операції з проєктами, яка чекає підтвердження."
EMPTY_PROJECTS = "У тебе ще немає проєктів."
EMPTY_ARCHIVED = "Немає проєктів в архіві."
NAME_TAKEN = "Така назва вже є в іншого твого проєкту. Спочатку об'єднай їх, якщо це один проєкт."
ALREADY_ARCHIVED = "Цей проєкт уже в архіві."
ALREADY_ACTIVE = "Цей проєкт уже активний."
ARCHIVED_NOTICE = "Проєкт «{name}» в архіві. Він не з'являється у звичайному списку."
REFUSE_PERMANENT_DELETE = (
    "У цьому проєкті є задачі або історія, тому я не буду видаляти його назавжди. "
    "Можу архівувати."
)
DELETE_FOREVER = "ВИДАЛИТИ НАЗАВЖДИ"
BTN_CREATE = "✅ Створити"
BTN_ARCHIVE = "📦 Archive"
BTN_RESTORE = "✅ Відновити"


def format_project_list(rows: list[tuple[str, int]]) -> str:
    if not rows:
        return EMPTY_PROJECTS
    lines = ["📁 Проєкти", ""]
    for index, (name, count) in enumerate(rows, start=1):
        lines.append(f"{index}. {name} — {count} відкритих задач")
    return "\n".join(lines)


def format_archived_project_list(rows: list[tuple[str, int]]) -> str:
    if not rows:
        return EMPTY_ARCHIVED
    lines = ["📦 Архівні проєкти", ""]
    for index, (name, count) in enumerate(rows, start=1):
        lines.append(f"{index}. {name} — {count} відкритих задач")
    return "\n".join(lines)


def format_archive_prompt(name: str) -> str:
    return (
        f"Архівувати проєкт «{name}»?\n\n"
        "Він зникне зі звичайного списку, але задачі та історія збережуться."
    )


def format_restore_prompt(name: str) -> str:
    return f"Відновити проєкт «{name}» зі архіву?"


def format_delete_forever_prompt(name: str) -> str:
    return (
        f"ВИДАЛИТИ НАЗАВЖДИ проєкт «{name}»?\n\n"
        "Це незворотна дія. Задач і історії в ньому немає. "
        f"Щоб підтвердити, натисни {DELETE_FOREVER} або напиши саме ці слова."
    )


def format_archived(name: str) -> str:
    return f"📦 Проєкт «{name}» архівовано."


def format_restored(name: str) -> str:
    return f"✅ Проєкт «{name}» відновлено."


def format_archived_intake(name: str) -> str:
    return (
        f"Проєкт «{name}» в архіві, тому я не пропоную його як активний. "
        f"Віднови його командою «віднови {name}», якщо хочеш знову додавати задачі."
    )


def format_project_tasks(canonical: str, tasks: list[PlanCandidate]) -> str:
    return format_project_task_list(canonical, tasks)


def format_merge_prompt(keep: str, sources: tuple[str, ...], task_count: int) -> str:
    others = "\n".join(f"📁 {name}" for name in sources)
    return (
        "🔀 Об'єднати проєкти?\n\n"
        f"Залишити:\n📁 {keep}\n\n"
        f"Об'єднати з:\n{others}\n\n"
        f"Задач буде перенесено: {task_count}"
    )


def format_rename_prompt(old_name: str, new_name: str) -> str:
    return (
        "✏️ Перейменувати проєкт?\n\n"
        f"📁 {old_name}\n"
        f"→ {new_name}"
    )


def format_similar_project_question(existing: str, requested: str) -> str:
    return f"Ти маєш на увазі «{existing}»?"


ASK_CREATE_NAME = "Як назвати новий проєкт?"
CREATED_PROJECT = "✅ Проєкт створено"
CREATED_PROJECT_HINT = "Тепер можеш додавати до нього задачі."


def format_create_prompt(name: str) -> str:
    return f"➕ Створити проєкт?\n\n📁 {name}"


def format_project_created(name: str) -> str:
    return f"{CREATED_PROJECT}: {name}\n{CREATED_PROJECT_HINT}"


def format_project_exists(canonical: str) -> str:
    return f"Проєкт «{canonical}» вже існує."


def format_project_exists_as_alias(canonical: str) -> str:
    return f"Це вже існуючий проєкт «{canonical}»."


def format_unknown_intake_project(
    requested: str,
    existing: list[str],
    *,
    similar: str | None = None,
) -> str:
    lines = [f"Не знайшов проєкт «{requested}».", ""]
    if similar:
        lines.append(f"Ти маєш на увазі «{similar}»?")
        lines.append("")
    if existing:
        lines.append("📁 Існуючі проєкти:")
        for index, name in enumerate(existing, start=1):
            lines.append(f"{index}. {name}")
        lines.append("")
    lines.append("Напиши назву існуючого проєкту або створи новий командою:")
    lines.append(f"«Створи проєкт {requested}».")
    return "\n".join(lines)
