"""Instructions for replan preferences. Selection stays in Python."""

REPLAN_INSTRUCTIONS = """
The user is adjusting today's work plan. Extract constraints only.

Do not choose or rank tasks. Do not invent projects or people that were not
implied. Ukrainian and English are both valid.

exclude_projects: project or workstream names to omit (e.g. "не став нічого по
Unity Center" → ["Unity Center"]).
exclude_people: people to avoid today (e.g. "Тараса сьогодні не чіпаємо" → ["Тарас"]).
Use the dictionary form of the name when obvious (Тараса → Тарас).
extra_reserve_minutes: extra free minutes to keep unused ("залиши 1 годину резерву"
→ 60). Null if not stated.
prefer_short_tasks: true for requests like "додай більше коротких задач".

If the message is unrelated or empty of constraints, return empty lists, null
reserve, and prefer_short_tasks false.
""".strip()
