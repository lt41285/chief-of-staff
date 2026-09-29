import pytest

from chief_of_staff.services.intent_router import default_chat_reply
from chief_of_staff.services.write_claim_guard import (
    claims_unperformed_write,
    guard_untrusted_reply,
)


@pytest.mark.parametrize(
    "text",
    [
        # The three production incidents.
        "Прийнято, дякую за оновлення!",
        "Вітаю з новим проєктом! Якщо потрібно додати задачі — дайте знати.",
        "Зрозуміло, враховую.",
        # Same class, other verbs.
        "Записав задачу на завтра.",
        "Готово.",
        "Задачу позначено виконаною.",
        "Створено проєкт Фундація.",
        "Переніс дедлайн на п'ятницю.",
        "Нагадаю завтра зранку.",
        "Проєкт заархівовано.",
        "Done, I've added it to your list.",
        "Task created.",
        "Питань немає. Задачу збережено.",
    ],
)
def test_blocks_write_claims(text: str) -> None:
    assert claims_unperformed_write(text)
    assert guard_untrusted_reply(text, source="test") is None


@pytest.mark.parametrize(
    "text",
    [
        "Привіт.",
        "Будь ласка.",
        "Добре, далі говоритиму на ти.",
        "Дякую за уточнення!",
        "Позначити виконаною?",
        "Готово створити цю задачу?",
        "Створити новий проєкт Фундація?",
        "Я нічого не записав — уточни, що саме зробити.",
        "Задачу не створено.",
        "Всі задачі по Андрію Боровцю закриті. Відкритих задач немає.",
        "Готовий допомогти з задачами.",
        "",
        None,
    ],
)
def test_passes_non_claims(text: str | None) -> None:
    assert not claims_unperformed_write(text)
    assert guard_untrusted_reply(text, source="test") == text


def test_default_chat_reply_replaces_claim_with_neutral_text() -> None:
    assert default_chat_reply("щось", "Прийнято, дякую за оновлення!") == "Так, слухаю."
    assert default_chat_reply("дякую", "Записав!") == "Будь ласка."


def test_default_chat_reply_keeps_safe_suggestion() -> None:
    assert default_chat_reply("привіт", "Привіт! Чим допомогти?") == "Привіт! Чим допомогти?"
