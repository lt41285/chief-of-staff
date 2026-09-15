"""Instructions for classifying task-management vs new-task messages."""

TASK_COMMAND_INSTRUCTIONS = """
Classify a Telegram message (Ukrainian or English) about the user's work.

intents:
- list_project_tasks: show tasks in one named project ("покажи задачі по BG",
  "список всіх завдань по проєкту Unity Center"). project_query is the project
  name as written. Never create a project or task.
- list_all_tasks: show the user's tasks grouped by project ("покажи всі задачі",
  "всі таски по проектах", "покажи всі таски по всіх проєктах",
  "у всіх / в усіх / з усіх / за всіма проєктами", "show all tasks",
  "across/from/for all projects", "всі задачі, щоб було видно їхні назви по
  проєктах", "список завдань за проєктами"). Never treat "всіх проєктах" or
  "all projects" as a project name.
- list_tasks: same as list_all_tasks when no project is named
- people_tasks_query: show open tasks related to a named person
  ("покажи, що у мене є по Тарасові Хомі", "які задачі по Тарасові Хомі",
  "що треба обговорити з Тарасом Хомою", "що я чекаю від Тараса Хоми",
  "show me everything related to Taras Khoma"). person_query is the person
  name as written. status_filter is "waiting" only for waiting-from-that-person
  questions. Never create a person or task. Infinitive new tasks
  ("поговорити з Тарасом", "треба домовитися з Тарасом") are
  normal_task_input, not this intent.
- complete_task: mark an existing task done ("виконано", "готово", "познач як
  виконану", "я вже поговорив…", "done: …"). task_query is the reference to
  the existing task, without command words or actual-time phrases.
  Past-tense reports of work already done ("я домовився з Лесею", "передав
  кошти") are complete_task only when they clearly refer to finishing work;
  the app will still ask before marking done. Infinitive / "треба" / "нагадай"
  messages are normal_task_input.
- postpone_task: change an existing task's deadline ("перенеси задачу… на п'ятницю",
  "відклади дедлайн", "postpone … until September 6"). new_deadline is YYYY-MM-DD
  in Europe/Kyiv. Never create a task.
- waiting_task: mark an existing task waiting ("чекаю від Наталі", "постав у
  waiting", "waiting for Andriy"). waiting_for is the person if stated.
- resume_task: take a waiting task back to work ("поверни в роботу", "resume",
  "більше не waiting"). Status becomes next. Not done.
- update_task: two lifecycle actions in one message (postpone AND waiting).
  Do not execute either; the app will ask to do them separately.
- cancel_task: cancel/drop an existing task (not "скасувати" on a new-task card)
- normal_task_input: capturing a new task or answering intake questions (default)

Show/list/які завдання messages are NEVER normal_task_input.
Do not treat project rename/merge as these intents.
If the message is a new task, use normal_task_input and leave other fields null.
""".strip()
