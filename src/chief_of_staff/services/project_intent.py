"""Parse project-management commands. Prefer deterministic rules; AI is fallback."""

import re
from typing import Protocol

from chief_of_staff.models.project_command import ProjectIntent, ProjectIntentKind
from chief_of_staff.prompts.project_command import PROJECT_COMMAND_INSTRUCTIONS

_APOS = str.maketrans({"’": "'", "ʼ": "'", "`": "'"})

_CREATE = re.compile(
    r"^\s*(?:"
    r"/newproject(?:\s+(?P<slash>.+))?"
    r"|(?:створи(?:ти)?|додай(?:ти)?|create|add)\s+"
    r"(?:новий\s+|new\s+)?(?:проєкт|проект|project)\s*[—–-]?\s*(?P<named>.*)"
    r"|(?:новий|new)\s+(?:проєкт|проект|project)\s*[—–-]?\s*(?P<bare>.+)"
    r")\s*$",
    re.IGNORECASE,
)
_TASK_CREATE = re.compile(
    r"(?:створи(?:ти)?|додай(?:ти)?|create|add)\s+"
    r"(?:нову\s+|new\s+)?(?:задач|завдан|task)",
    re.IGNORECASE,
)

_LIST = re.compile(
    r"^\s*(?:"
    r"/projects\b|"
    r"(?:мої\s+)?(?:проєкти|проекти|projects)\s*\.?|"
    r"(?:покажи|покажіть|виведи)\s+"
    r"(?:тепер\s+|мені\s+)*"
    r"(?:всі|усі)?\s*"
    r"(?:мої\s+)?"
    r"(?:проєкти|проекти|projects)\s*\.?|"
    r"(?:покажи|покажіть|виведи)\s+"
    r"(?:тепер\s+|мені\s+)*"
    r"(?:загальний\s+)?список\s+(?:проєктів|проектів|проектов|projects)\s*\.?|"
    r"(?:список|загальний\s+список)\s+(?:проєктів|проектів|проектов|projects)\s*\.?|"
    r"які\s+(?:в\s+мене\s+)?(?:проєкти|проекти|projects)\s*\??"
    r")\s*$",
    re.IGNORECASE,
)
_LIST_HINT = re.compile(
    r"(?i)(?:"
    r"список\s+(?:загальний\s+)?(?:проєктів|проектів|проектов|projects)|"
    r"загальний\s+список\s+(?:проєктів|проектів|проектов|projects)|"
    r"(?:покажи|покажіть|виведи)\s+(?:тепер\s+|мені\s+)*(?:всі\s+|усі\s+|мої\s+)*(?:проєкти|проекти|projects)|"
    r"(?:покажи|покажіть|виведи)\s+(?:тепер\s+|мені\s+)*(?:загальний\s+)?список|"
    r"які\s+(?:в\s+мене\s+)?(?:проєкти|проекти)|"
    r"просив[^\n]{0,80}список\s+(?:проєктів|проектів)"
    r")"
)
_EXPLICIT_CREATE = re.compile(
    r"(?i)^\s*(?:створи(?:ти)?|додай(?:ти)?|хочу\s+створити|create|add)\s+"
    r"(?:новий\s+|new\s+)?(?:проєкт|проект|project)"
    r"|^\s*(?:новий|new)\s+(?:проєкт|проект|project)\b"
)
_RENAME = re.compile(
    r"^\s*перейменуй\s+(?:проєкт|проект|project)?\s*(?P<old>.+?)\s+на\s+(?P<new>.+?)\s*\.?\s*$",
    re.IGNORECASE,
)
_MERGE = re.compile(
    r"^\s*об['']?єднай\s+(?P<a>.+?)\s+(?:і|та|and|&)\s+(?P<b>.+?)\s*[.!]?\s*"
    r"(?:залиш(?:и)?(?:\s+назву)?\s+(?P<keep>.+?))?\s*\.?\s*$",
    re.IGNORECASE,
)
_SAME = re.compile(
    r"^\s*(?P<alias>.+?)\s*[—–-]\s*це\s+той\s+самий\s+(?:проєкт|проект)\s*,?\s*що\s+(?P<keep>.+?)\s*\.?\s*$",
    re.IGNORECASE,
)
_TASKS = re.compile(
    r"^\s*(?:покажи(?:ть)?(?:\s+мені)?|виведи|список|які|який|show(?:\s+me)?|list)\s+"
    r"(?:всіх|всі|усі|all|моїх|мої|my)?\s*"
    r"(?:задач[іауей]*|завдан[няь]+|таск[иа]?|tasks?)\s*"
    r"(?:є\s+|are\s+)?"
    r"(?:в|у|по|для|for|in)\s+"
    r"(?:(?:архівн(?:ого|ому|ий|і)\s+)?(?:проєкті|проекті|проєкти|проекти|проєкту|проекту|проєкт|проект|project)\s+)?"
    r"(?P<q>.+?)\s*\??\s*$",
    re.IGNORECASE,
)
_LIST_ARCHIVED = re.compile(
    r"(?i)(?:"
    r"архівн[іих]+\s+проєкт|"
    r"архів\s+проєкт|"
    r"проєкт[иів]+\s+в\s+архів|"
    r"що\s+(?:в\s+мене\s+)?в\s+архів|"
    r"archived\s+projects|"
    r"show\s+archiv"
    r")"
)
_PERMANENT = re.compile(
    r"(?i)(?:"
    r"видал\w*.{0,80}назавжди|"
    r"назавжди.{0,40}видал|"
    r"повністю\s+видал|"
    r"delete.{0,40}permanently|"
    r"permanently.{0,40}delete"
    r")"
)
_ARCHIVE = re.compile(
    r"(?i)^\s*(?:видал(?:и|ити)|прибер(?:и|іть|ити)|схов(?:ай|ати)|"
    r"архівуй(?:те)?|заархівуй|archive|remove|hide)\s+"
    r"(?:проєкт|проект|project)?\s*"
    r"(?P<name>.+?)\s*"
    r"(?:зі\s+списку|з\s+моїх\s+проєктів|з\s+проєктів|"
    r"from\s+(?:the\s+)?(?:list|my\s+projects?))?\s*[.!]?\s*$"
)
_ARCHIVE_CALLED = re.compile(
    r"(?i)називається\s+(?P<name>.+?)(?:[.!]|$)"
)
_ARCHIVE_HINT = re.compile(
    r"(?i)(?:видал\w*\s+зі\s+списку|прибер\w*\s+зі\s+списку|"
    r"більше\s+не\s+потрібен|архівуй|заархів|схов(?:ай|ати)\s+|"
    r"remove\s+.+\s+from\s+my\s+projects)"
)
_RESTORE = re.compile(
    r"(?i)^\s*(?:"
    r"віднови(?:ти)?\s+(?:проєкт|проект|project)?\s*(?P<a>.+?)"
    r"|поверни(?:ти)?\s+(?:проєкт|проект|project)?\s*(?P<b>.+?)\s+з\s+архіву"
    r"|зроби\s+(?:проєкт|проект|project)?\s*(?P<c>.+?)\s+активн\w*"
    r"|restore\s+(?:project\s+)?(?P<d>.+?)"
    r"|unarchive\s+(?:project\s+)?(?P<e>.+?)"
    r")\s*[.!]?\s*$"
)
_ARCHIVED_PREFIX = re.compile(
    r"(?i)^(архівн(?:ого|ому|ий|ої|і)\s+|archived\s+)"
)


class ProjectIntentParser(Protocol):
    async def parse(self, text: str) -> ProjectIntent: ...


def parse_project_intent_deterministic(text: str) -> ProjectIntent | None:
    raw = " ".join(text.translate(_APOS).split()).strip()
    if not raw:
        return None
    created = _parse_create(raw)
    if created is not None:
        return created
    archived_list = _parse_list_archived(raw)
    if archived_list is not None:
        return archived_list
    if _LIST.match(raw) or raw.casefold() in {"/projects", "projects"}:
        return ProjectIntent(kind=ProjectIntentKind.LIST_PROJECTS)
    permanent = _parse_permanent_delete(raw)
    if permanent is not None:
        return permanent
    archive = _parse_archive(raw)
    if archive is not None:
        return archive
    restore = _parse_restore(raw)
    if restore is not None:
        return restore
    rename = _RENAME.match(raw)
    if rename:
        return ProjectIntent(
            kind=ProjectIntentKind.RENAME_PROJECT,
            old_name=_clean_name(rename.group("old")),
            new_name=_clean_name(rename.group("new")),
        )
    merge = _MERGE.match(raw)
    if merge:
        a = _clean_name(merge.group("a"))
        b = _clean_name(merge.group("b"))
        keep = _clean_name(merge.group("keep") or a)
        drop = b if _norm(keep) == _norm(a) else a
        if _norm(keep) == _norm(b):
            drop = a
        return ProjectIntent(
            kind=ProjectIntentKind.MERGE_PROJECTS,
            keep_name=keep,
            merge_names=[drop],
        )
    same = _SAME.match(raw)
    if same:
        return ProjectIntent(
            kind=ProjectIntentKind.LINK_ALIAS,
            keep_name=_clean_name(same.group("keep")),
            alias_name=_clean_name(same.group("alias")),
        )
    tasks = _TASKS.match(raw)
    if tasks:
        query = _strip_archived_prefix(_clean_name(tasks.group("q")))
        if query and query.casefold() not in {
            "проектах",
            "проєктах",
            "проекти",
            "проєкти",
            "projects",
        }:
            return ProjectIntent(kind=ProjectIntentKind.PROJECT_TASKS, project_query=query)
    return None


def _parse_create(raw: str) -> ProjectIntent | None:
    if _TASK_CREATE.search(raw):
        return None
    match = _CREATE.match(raw)
    if match is None:
        return None
    name = _clean_name(
        match.group("slash") or match.group("named") or match.group("bare") or ""
    )
    return ProjectIntent(kind=ProjectIntentKind.CREATE_PROJECT, new_name=name or None)


def _parse_list_archived(raw: str) -> ProjectIntent | None:
    if _LIST_ARCHIVED.search(raw):
        return ProjectIntent(kind=ProjectIntentKind.LIST_ARCHIVED_PROJECTS)
    return None


def _parse_permanent_delete(raw: str) -> ProjectIntent | None:
    if not _PERMANENT.search(raw):
        return None
    name = _name_from_delete_phrase(raw)
    return ProjectIntent(kind=ProjectIntentKind.DELETE_PROJECT_PERMANENTLY, project_query=name)


def _parse_archive(raw: str) -> ProjectIntent | None:
    if _PERMANENT.search(raw):
        return None
    match = _ARCHIVE.match(raw)
    if match:
        name = _clean_name(match.group("name"))
        name = re.sub(r"(?i)\s+(?:зі\s+списку|from\s+my\s+projects?)$", "", name).strip()
        if name:
            return ProjectIntent(kind=ProjectIntentKind.ARCHIVE_PROJECT, project_query=name)
    called = _ARCHIVE_CALLED.search(raw)
    if called and _ARCHIVE_HINT.search(raw):
        return ProjectIntent(
            kind=ProjectIntentKind.ARCHIVE_PROJECT,
            project_query=_clean_name(called.group("name")),
        )
    if _ARCHIVE_HINT.search(raw) and ("проєкт" in raw.casefold() or "project" in raw.casefold()):
        return ProjectIntent(kind=ProjectIntentKind.ARCHIVE_PROJECT, project_query=None)
    return None


def _parse_restore(raw: str) -> ProjectIntent | None:
    match = _RESTORE.match(raw)
    if match is None:
        return None
    name = _clean_name(
        match.group("a") or match.group("b") or match.group("c") or match.group("d") or match.group("e") or ""
    )
    if not name:
        return None
    return ProjectIntent(kind=ProjectIntentKind.RESTORE_PROJECT, project_query=name)


def _name_from_delete_phrase(raw: str) -> str | None:
    match = re.search(
        r"(?i)(?:проєкт|проект|project)\s+(?P<n>.+?)(?:\s+(?:назавжди|повністю|permanently)|$)",
        raw,
    )
    if match:
        name = _clean_name(match.group("n"))
        name = re.sub(r"(?i)\s*(?:назавжди|permanently|повністю)$", "", name).strip()
        if name:
            return name
    match = re.search(
        r"(?i)(?:видал(?:и|ити)|delete)\s+(?:назавжди\s+|permanently\s+)?(?P<n>.+?)\s*$",
        raw,
    )
    if match:
        name = _clean_name(match.group("n"))
        name = re.sub(
            r"(?i)\s*(?:назавжди|permanently|повністю|проєкт|проект|project)$",
            "",
            name,
        ).strip()
        name = re.sub(r"(?i)^(проєкт|проект|project)\s+", "", name).strip()
        return name or None
    called = _ARCHIVE_CALLED.search(raw)
    if called:
        return _clean_name(called.group("name"))
    return None


def _strip_archived_prefix(value: str) -> str:
    return _ARCHIVED_PREFIX.sub("", value).strip()


def looks_like_list_projects(text: str) -> bool:
    if looks_like_list_archived_projects(text):
        return False
    parsed = parse_project_intent_deterministic(text)
    if parsed is not None and parsed.kind == ProjectIntentKind.LIST_PROJECTS:
        return True
    folded = " ".join(text.translate(_APOS).split()).casefold()
    if looks_like_explicit_project_create(text):
        return False
    if looks_like_archive_or_restore(text):
        return False
    if "список проєктів" in folded or "список проектів" in folded or "список проектов" in folded:
        return True
    return bool(_LIST_HINT.search(text))


def looks_like_list_archived_projects(text: str) -> bool:
    parsed = parse_project_intent_deterministic(text)
    if parsed is not None and parsed.kind == ProjectIntentKind.LIST_ARCHIVED_PROJECTS:
        return True
    return bool(_LIST_ARCHIVED.search(text))


def looks_like_archive_or_restore(text: str) -> bool:
    parsed = parse_project_intent_deterministic(text)
    if parsed is not None and parsed.kind in {
        ProjectIntentKind.ARCHIVE_PROJECT,
        ProjectIntentKind.RESTORE_PROJECT,
        ProjectIntentKind.DELETE_PROJECT_PERMANENTLY,
    }:
        return True
    return bool(_ARCHIVE_HINT.search(text) or _RESTORE.match(" ".join(text.split())) or _PERMANENT.search(text))


def looks_like_explicit_project_create(text: str) -> bool:
    parsed = parse_project_intent_deterministic(text)
    if parsed is not None and parsed.kind == ProjectIntentKind.CREATE_PROJECT:
        return True
    return bool(_EXPLICIT_CREATE.search(text))


def looks_like_project_command(text: str) -> bool:
    folded = text.translate(_APOS).casefold()
    if parse_project_intent_deterministic(text) is not None:
        return True
    markers = (
        "проєкт",
        "проект",
        "project",
        "об'єднай",
        "обеднай",
        "перейменуй",
        "/projects",
        "/newproject",
        "створи проєкт",
        "створи проект",
        "додай проєкт",
        "create project",
        "архів",
        "віднови",
        "назавжди",
    )
    verbs = (
        "покажи",
        "перейменуй",
        "об'єднай",
        "обєднай",
        "задач",
        "той самий",
        "створи",
        "додай",
        "новий",
        "create",
        "add",
        "new",
        "видал",
        "прибер",
        "архів",
        "схов",
        "віднови",
        "поверни",
        "remove",
        "archive",
        "restore",
    )
    return any(m in folded for m in markers) and any(w in folded for w in verbs)


def _clean_name(value: str) -> str:
    text = " ".join(value.split()).strip(" .")
    text = re.sub(r"(?i)^(проєкт|проект|project)\s+", "", text).strip()
    return text


def _norm(value: str) -> str:
    return " ".join(value.split()).casefold()


class OpenAIProjectIntentParser:
    def __init__(self, client: object) -> None:
        self._client = client

    async def parse(self, text: str) -> ProjectIntent:
        parsed = parse_project_intent_deterministic(text)
        if parsed is not None:
            return parsed
        schema = await self._client.parse_structured(  # type: ignore[attr-defined]
            user_input=text,
            instructions=PROJECT_COMMAND_INSTRUCTIONS,
            text_format=ProjectIntent,
        )
        return schema
