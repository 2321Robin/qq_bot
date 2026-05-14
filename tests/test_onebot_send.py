import pytest
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
from nonebot.exception import FinishedException

from qq_bot.services.onebot_send import finish_with_send_errors_logged, is_send_timeout_error


class FakeMatcher:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.messages: list[object] = []

    async def finish(self, message: object) -> None:
        self.messages.append(message)
        if self.error is not None:
            raise self.error
        raise FinishedException


def test_is_send_timeout_error_matches_onebot_network_send_timeout() -> None:
    error = NetworkError("WebSocket call api send_msg timeout")

    assert is_send_timeout_error(error)


def test_is_send_timeout_error_matches_napcat_send_action_timeout() -> None:
    error = ActionFailed(
        status="failed",
        retcode=1,
        message="Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg",
    )

    assert is_send_timeout_error(error)


def test_is_send_timeout_error_ignores_non_send_timeout() -> None:
    assert not is_send_timeout_error(NetworkError("WebSocket call api get_login_info timeout"))
    assert not is_send_timeout_error(RuntimeError("send failed"))


@pytest.mark.asyncio
async def test_finish_with_send_errors_logged_reraises_send_timeout() -> None:
    matcher = FakeMatcher(NetworkError("WebSocket call api send_msg timeout"))

    with pytest.raises(NetworkError):
        await finish_with_send_errors_logged(matcher, "hello")

    assert matcher.messages == ["hello"]


@pytest.mark.asyncio
async def test_finish_with_send_errors_logged_reraises_other_errors() -> None:
    matcher = FakeMatcher(RuntimeError("send failed"))

    with pytest.raises(RuntimeError, match="send failed"):
        await finish_with_send_errors_logged(matcher, "hello")
