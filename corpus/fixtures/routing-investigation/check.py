from app import dispatch

assert dispatch("get", "/health/") == "healthy"
assert dispatch("post", "/items/") == "created"
assert dispatch("get", "/") == "root"
assert dispatch("get", "/health//") is None
assert dispatch("delete", "/items/") is None
