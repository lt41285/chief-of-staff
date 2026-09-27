"""Split outbound Telegram text so one reply cannot exceed the API limit."""

from collections.abc import Awaitable, Callable

TELEGRAM_MESSAGE_LIMIT = 4096

ReplyFn = Callable[..., Awaitable[object]]


def split_telegram_text(
    text: str, *, limit: int = TELEGRAM_MESSAGE_LIMIT
) -> list[str]:
    """Split on paragraph, then line, then space. Each chunk fits ``limit``."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        window = rest[:limit]
        cut = _best_cut(window, limit)
        chunk = rest[:cut].rstrip()
        rest = rest[cut:].lstrip("\n")
        if chunk:
            chunks.append(chunk)
        elif rest:
            chunks.append(rest[:limit])
            rest = rest[limit:]
    return chunks or [""]


def _best_cut(window: str, limit: int) -> int:
    floor = max(1, limit // 2)
    for separator in ("\n\n", "\n", " "):
        cut = window.rfind(separator)
        if cut >= floor:
            return cut
    return limit


async def reply_telegram_text(
    reply: ReplyFn,
    text: str,
    *,
    reply_markup: object | None = None,
    limit: int = TELEGRAM_MESSAGE_LIMIT,
    **kwargs: object,
) -> None:
    """Send ``text`` as one or more Telegram messages under the size limit."""
    chunks = split_telegram_text(text, limit=limit)
    last = len(chunks) - 1
    for index, chunk in enumerate(chunks):
        extra = dict(kwargs)
        if index == last and reply_markup is not None:
            extra["reply_markup"] = reply_markup
        await reply(chunk, **extra)
