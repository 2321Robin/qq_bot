from __future__ import annotations

from typing import Any, Protocol, NoReturn

from nonebot import logger
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.exception import FinishedException


class FinishableMatcher(Protocol):
    async def finish(self, message: object, **kwargs: Any) -> NoReturn:
        raise NotImplementedError


def is_send_timeout_error(error: Exception) -> bool:
    text = _error_text(error)
    if "timeout" not in text.lower():
        return False
    if isinstance(error, NetworkError):
        return "send_msg" in text or "send_group_msg" in text
    if isinstance(error, ActionFailed):
        return "sendmsg" in text.replace("_", "").lower()
    return False


async def finish_with_send_timeout_handled(
    matcher: FinishableMatcher,
    message: object,
    **kwargs: Any,
) -> NoReturn:
    try:
        await matcher.finish(message, **kwargs)
    except Exception as exc:
        if is_send_timeout_error(exc):
            logger.warning(f"Message send timed out after dispatch; suppressing duplicate retry: {exc!r}")
            raise FinishedException from None
        raise

    raise FinishedException


def _error_text(error: Exception) -> str:
    parts = [str(error), repr(error)]
    info = getattr(error, "info", None)
    if isinstance(info, dict):
        parts.extend(str(value) for value in info.values())
    return " ".join(parts)
