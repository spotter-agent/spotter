"""Validation and secret-safe display for Codex App Server endpoints."""

from urllib.parse import urlsplit, urlunsplit


class AppServerEndpointError(ValueError):
    """The configured endpoint cannot be used as a WebSocket URL."""


def normalize_app_server_endpoint(raw: str) -> str:
    """Return a validated WebSocket endpoint without discarding query credentials."""
    endpoint = raw.strip()
    if not endpoint:
        raise AppServerEndpointError("App Server endpoint must not be empty")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as error:
        raise AppServerEndpointError(f"invalid App Server endpoint: {error}") from error
    scheme = parsed.scheme.lower()
    if scheme not in {"ws", "wss"}:
        raise AppServerEndpointError("App Server endpoint must use ws:// or wss://")
    if parsed.hostname is None:
        raise AppServerEndpointError("App Server endpoint must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise AppServerEndpointError(
            "App Server endpoint must not embed credentials in the authority; "
            "use transport configuration instead"
        )
    if parsed.fragment:
        raise AppServerEndpointError("App Server endpoint must not include a fragment")
    if any(character.isspace() for character in endpoint):
        raise AppServerEndpointError("App Server endpoint must not contain whitespace")
    # Accessing parsed.port above validates its range and spelling. Preserve the
    # original authority/path/query so transport-specific tokens are not changed.
    _ = port
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))


def display_app_server_endpoint(endpoint: str) -> str:
    """Render an endpoint for diagnostics without exposing query values or userinfo."""
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<redacted App Server endpoint>"
    if hostname is None:
        return "<redacted App Server endpoint>"
    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = f"{host}:{port}" if port is not None else host
    query = "<redacted>" if parsed.query else ""
    return urlunsplit((parsed.scheme.lower(), authority, parsed.path, query, ""))


def redact_app_server_error(error: object, endpoint: str) -> str:
    """Remove the exact configured endpoint from an exception or status detail."""
    return str(error).replace(endpoint, display_app_server_endpoint(endpoint))
