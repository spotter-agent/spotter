def normalize_settings(raw: dict[str, object]) -> dict[str, object]:
    return {
        "endpoint": raw["endpoint"],
        "retries": int(raw.get("retries", 3)),
    }
