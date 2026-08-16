import pytest

from spotter.app_server_endpoint import (
    AppServerEndpointError,
    display_app_server_endpoint,
    normalize_app_server_endpoint,
    redact_app_server_error,
)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:4500",
        "ws://",
        "ws://user:password@127.0.0.1:4500",
        "ws://127.0.0.1:4500/#fragment",
        "ws://127.0.0.1:99999",
    ],
)
def test_normalize_rejects_unsupported_or_unsafe_endpoints(endpoint: str) -> None:
    with pytest.raises(AppServerEndpointError):
        normalize_app_server_endpoint(endpoint)


def test_endpoint_display_and_errors_redact_query_values() -> None:
    endpoint = normalize_app_server_endpoint(
        " WSS://example.test:4500/socket?access_token=setup-secret "
    )

    assert endpoint == "wss://example.test:4500/socket?access_token=setup-secret"
    assert display_app_server_endpoint(endpoint) == "wss://example.test:4500/socket?<redacted>"
    detail = redact_app_server_error(f"connection to {endpoint} failed", endpoint)
    assert detail == "connection to wss://example.test:4500/socket?<redacted> failed"
    assert "setup-secret" not in detail
