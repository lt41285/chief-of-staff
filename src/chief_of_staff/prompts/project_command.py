"""Instructions for classifying project-management vs new-task messages."""

PROJECT_COMMAND_INSTRUCTIONS = """
Classify a Telegram message (Ukrainian or English) about the user's work.

intents:
- create_project: explicitly create a new empty project ("створи проєкт BG",
  "додай проєкт Fundraising", "новий проєкт — X", "create project Angel",
  "/newproject"). new_name is the display name as written. Never create a task.
- list_projects: show active projects ("покажи проєкти", "мої проєкти")
- list_archived_projects: archived only ("покажи архівні проєкти", "архів проєктів")
- archive_project: hide from the active list ("видали X зі списку", "архівуй X",
  "прибери X", "сховай X", "remove X from my projects"). This is NOT a DB delete.
  project_query is the project name.
- restore_project: unarchive ("віднови X", "поверни X з архіву")
- delete_project_permanently: only "видали назавжди" / "delete permanently"
- rename_project: rename one project (old_name, new_name)
- merge_projects: combine two existing projects; keep_name is the surviving
  canonical name; merge_names are the others to fold in
- link_alias: user says two names are the same project; keep_name is canonical,
  alias_name is the other wording (may or may not already be a separate project)
If the user asks to show/list tasks, task names, or завдання/таски, that is
NOT list_projects — leave it as create_task so another classifier can treat
it as a task query.
- project_tasks: list open tasks in one named project (project_query)
- create_task: capturing a task, including "створи задачу" / "додай задачу",
  OR showing/listing tasks (default when unsure). Not create_project.

Do not invent names. Copy project names as the user wrote them.
Messages about task titles ("щоб було видно їхні назви") are not list_projects.
""".strip()
