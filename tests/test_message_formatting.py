from nonebot.adapters.onebot.v11 import Message

from qq_bot.services.message_formatting import replace_named_mentions

REPLACEMENTS = {"@小呱呱": "2880000001"}


def test_replace_named_mentions_replaces_all_known_mentions() -> None:
    message = "@小呱呱 /远行商人，再提醒 @小呱呱"

    formatted = replace_named_mentions(message, REPLACEMENTS)

    assert isinstance(formatted, Message)
    assert formatted[0].type == "at"
    assert formatted[0].data["qq"] == "2880000001"
    assert formatted[1].type == "text"
    assert formatted[1].data["text"] == " /远行商人，再提醒 "
    assert formatted[2].type == "at"
    assert formatted[2].data["qq"] == "2880000001"


def test_replace_named_mentions_keeps_other_text_unchanged() -> None:
    formatted = replace_named_mentions("普通消息", REPLACEMENTS)

    assert isinstance(formatted, Message)
    assert formatted.extract_plain_text() == "普通消息"


def test_replace_named_mentions_without_replacements_keeps_text() -> None:
    formatted = replace_named_mentions("@小呱呱 早上好", None)

    assert isinstance(formatted, Message)
    assert formatted.extract_plain_text() == "@小呱呱 早上好"
