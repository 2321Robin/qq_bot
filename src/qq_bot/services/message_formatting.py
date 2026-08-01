from nonebot.adapters.onebot.v11 import Message, MessageSegment


def replace_named_mentions(message: str, replacements: dict[str, str] | None = None) -> Message:
    """Replace named mentions like ``@小呱呱`` with real @at segments.

    The mapping comes from configuration (``NAMED_MENTION_REPLACEMENTS``),
    never from source, so deployers keep their real QQ numbers out of the
    public repository.
    """
    replacements = replacements or {}
    formatted = Message()
    remaining = message

    while remaining:
        match_name = ""
        match_index = -1
        for name in replacements:
            index = remaining.find(name)
            if index != -1 and (match_index == -1 or index < match_index):
                match_name = name
                match_index = index

        if match_index == -1:
            formatted += MessageSegment.text(remaining)
            break

        if match_index > 0:
            formatted += MessageSegment.text(remaining[:match_index])

        formatted += MessageSegment.at(replacements[match_name])
        remaining = remaining[match_index + len(match_name) :]

    return formatted
