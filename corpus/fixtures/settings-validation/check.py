from settings import normalize_settings

assert normalize_settings({"endpoint": " https://api.test ", "retries": "2"}) == {
    "endpoint": "https://api.test",
    "retries": 2,
}
assert normalize_settings({"endpoint": "https://api.test"})["retries"] == 3

for invalid in (0, -1, "many", None):
    try:
        normalize_settings({"endpoint": "https://api.test", "retries": invalid})
    except ValueError:
        pass
    else:
        raise AssertionError(f"retries={invalid!r} should be rejected")
