"""Block untrusted LLM text that claims a write the bot did not perform.

Python formatters speak only after a successful repository call, so they never
pass through here. This guard covers free LLM text (``chat_reply`` and the
``phrase`` step): if it sounds like a completed write («записав», «створено»,
«прийнято», «вітаю з …») and is not a question, the caller must fall back to
neutral or Python-built text.
"""

import re

from loguru import logger

_UK_CLAIMS = (
    r"створено|створив|створила|"
    r"записано|записав|записала|"
    r"позначено|позначив|позначила|"
    r"додано|додав|додала|"
    r"збережено|зберіг|зберегла|"
    r"оновлено|оновив|оновила|"
    r"перенесено|переніс|перенесла|"
    r"видалено|видалив|видалила|"
    r"заархівовано|архівовано|заархівував|заархівувала|"
    r"закрито|закрив|закрила|"
    r"змінено|змінив|змінила|"
    r"внесено|вніс|внесла|"
    r"зафіксовано|зафіксував|зафіксувала|"
    r"враховано|врахував|врахувала|враховую|"
    r"прийнято|готово|"
    r"нагадаю|запланував|запланувала|заплановано"
)
_EN_CLAIMS = (
    r"created|saved|added|recorded|updated|postponed|rescheduled|"
    r"deleted|archived|scheduled|marked|noted|logged|done"
)
_CLAIM = re.compile(
    rf"(?<![\w-])(?P<neg>не\s+|not\s+|n't\s+)?(?:{_UK_CLAIMS}|{_EN_CLAIMS})(?![\w-])",
    re.IGNORECASE,
)
_CONGRATS = re.compile(r"(?<![\w-])(?:вітаю\s+з|congratulations)", re.IGNORECASE)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")


def claims_unperformed_write(text: str | None) -> bool:
    """True if ``text`` states, outside a question, that a write was done."""
    if not text:
        return False
    for sentence in _SENTENCE.findall(text):
        if sentence.rstrip().endswith("?"):
            continue
        if _CONGRATS.search(sentence):
            return True
        for match in _CLAIM.finditer(sentence):
            if not match.group("neg"):
                return True
    return False


def guard_untrusted_reply(text: str | None, *, source: str) -> str | None:
    """Return ``text`` unless it claims an unperformed write; then None."""
    if claims_unperformed_write(text):
        logger.warning(
            "Blocked untrusted write claim source={source} text={text!r}",
            source=source,
            text=text,
        )
        return None
    return text
