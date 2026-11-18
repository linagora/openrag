def str_to_bool(value: str) -> bool:
    value = value.strip().lower()
    if value in ("true", "1", "t", "y", "yes", "on"):
        return True
    elif value in ("false", "0", "f", "n", "no", "off"):
        return False
    else:
        raise ValueError(f"Not a boolean : {value}")


def json_object_slice(text: str) -> str:
    """Best-effort extract the first JSON object from an LLM response.

    From the first ``{`` to the last ``}``: a model that wraps its JSON in
    prose or a code fence still parses. Returns *text* unchanged when it holds
    no such span.
    """
    start = text.find("{")
    end = text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text
