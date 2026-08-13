from cache import cache_key

assert cache_key(" Users ", "AbC-123") == "users:AbC-123"
assert cache_key("USERS", "abc-123") == "users:abc-123"
assert cache_key("USERS", " abc ") == "users: abc "
