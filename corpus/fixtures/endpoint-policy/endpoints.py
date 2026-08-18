from collections.abc import Iterable


class EndpointError(ValueError):
    pass


def canonicalize_endpoints(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value.strip().lower().rstrip("/") for value in values))
