from query import parse

assert parse("token=a=b") == ("token", "a=b")
