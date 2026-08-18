from endpoints import EndpointError, canonicalize_endpoints


def rejects(value: str, message: str) -> None:
    try:
        canonicalize_endpoints([value])
    except EndpointError as error:
        assert message in str(error)
    else:
        raise AssertionError(f"unsafe endpoint accepted: {value}")


assert canonicalize_endpoints(
    [
        " HTTPS://Example.COM:443/API/ ",
        "https://example.com/API",
        "https://example.com:8443/API/",
        "https://[2001:DB8::1]:443/v1/",
        "https://[2001:db8::1]:8443/v1",
        "https://example.com/",
    ]
) == [
    "https://example.com/API",
    "https://example.com:8443/API",
    "https://[2001:db8::1]/v1",
    "https://[2001:db8::1]:8443/v1",
    "https://example.com/",
]

rejects("http://example.com/api", "https")
rejects("https://user:secret@example.com/api", "credentials")
rejects("https://example.com/api?token=secret", "query")
rejects("https://example.com/api#part", "fragment")
rejects("https:///api", "host")
rejects("https://example.com:99999/api", "port")
rejects("https://example.com/a/../admin", "traversal")
rejects("https://example.com/a/%2e%2e/admin", "traversal")
rejects("https://example.com/a%2fadmin", "encoded separator")
rejects("https://example.com/a\\admin", "separator")
rejects("https://exa mple.com/api", "host")
