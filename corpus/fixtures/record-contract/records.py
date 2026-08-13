def normalize_record(raw: dict[str, object]) -> dict[str, object]:
    return {
        "name": str(raw["name"]).strip(),
        "tags": list(raw.get("tags", [])),
    }
