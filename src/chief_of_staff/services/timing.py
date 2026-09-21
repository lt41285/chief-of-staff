"""Stage timers for turn latency. Logs milliseconds; never logs secrets."""

from collections.abc import Iterator
from contextlib import contextmanager
import time

from loguru import logger


@contextmanager
def log_stage(stage: str, **fields: object) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "perf stage={stage} duration_ms={duration_ms:.0f}",
            stage=stage,
            duration_ms=duration_ms,
            **fields,
        )
