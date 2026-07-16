def str_to_bool(value: str) -> bool:
    value = value.strip().lower()
    if value in ("true", "1", "t", "y", "yes", "on"):
        return True
    elif value in ("false", "0", "f", "n", "no", "off"):
        return False
    else:
        raise ValueError(f"Not a boolean : {value}")
