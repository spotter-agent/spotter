from routing import route


def dispatch(method: str, path: str) -> str | None:
    return route(method, path)
