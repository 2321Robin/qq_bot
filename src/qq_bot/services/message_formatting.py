from nonebot.adapters.onebot.v11 import Message, MessageSegment


NAMED_MENTION_REPLACEMENTS = {
    "@小呱呱": "2854203313",
}


def replace_named_mentions(message: str) -> Message:
    formatted = Message()
    remaining = message

    while remaining:
        match_name = ""
        match_index = -1
        for name in NAMED_MENTION_REPLACEMENTS:
            index = remaining.find(name)
            if index != -1 and (match_index == -1 or index < match_index):
                match_name = name
                match_index = index

        if match_index == -1:
            formatted += MessageSegment.text(remaining)
            break

        if match_index > 0:
            formatted += MessageSegment.text(remaining[:match_index])

        formatted += MessageSegment.at(NAMED_MENTION_REPLACEMENTS[match_name])
        remaining = remaining[match_index + len(match_name) :]

    return formatted
