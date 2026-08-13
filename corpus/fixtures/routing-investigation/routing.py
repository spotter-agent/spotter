HANDLERS = {
    ("GET", "/"): "root",
    ("GET", "/health"): "healthy",
    ("POST", "/items"): "created",
}


def route(method: str, path: str) -> str | None:
    return HANDLERS.get((method, path))
