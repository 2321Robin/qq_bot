def extract_ai_prompt(text: str, *, prefix: str) -> str | None:
    marker = prefix.strip()
    if not marker:
        return None

    normalized_text = text.strip()
    normalized_marker = marker.casefold()
    normalized_message = normalized_text.casefold()

    if normalized_message == normalized_marker:
        return ""

    prefix_with_space = f"{normalized_marker} "
    if normalized_message.startswith(prefix_with_space):
        return normalized_text[len(marker) :].strip()

    return None
