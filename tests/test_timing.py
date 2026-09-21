from loguru import logger

from chief_of_staff.services.timing import log_stage


def test_log_stage_emits_duration_ms() -> None:
    records: list[object] = []
    handler_id = logger.add(lambda message: records.append(message.record), format="{message}")
    try:
        with log_stage("safe_interpret"):
            pass
    finally:
        logger.remove(handler_id)
    extras = [record["extra"] for record in records]
    assert any(
        extra.get("stage") == "safe_interpret" and "duration_ms" in extra for extra in extras
    )
