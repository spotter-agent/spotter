def cache_key(namespace: str, item_key: str) -> str:
    return f"{namespace.strip().lower()}:{item_key.lower()}"
