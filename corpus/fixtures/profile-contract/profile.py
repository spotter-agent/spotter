def normalize_profile(raw: dict[str, str]) -> dict[str, str]:
    return {"name": raw["name"].strip()}
