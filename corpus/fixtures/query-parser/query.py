def parse(text: str) -> tuple[str, str]:
    key, value = text.split("=")
    return key, value
