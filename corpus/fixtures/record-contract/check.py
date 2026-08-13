from records import normalize_record
from renderer import render_record

raw: dict[str, object] = {
    "name": " Example ",
    "tags": [" API ", "Bug", "api", "  ", "BUG"],
}
record = normalize_record(raw)
assert record == {"name": "Example", "tags": ["api", "bug"]}
assert render_record(record) == "Example [api, bug]"
assert raw == {
    "name": " Example ",
    "tags": [" API ", "Bug", "api", "  ", "BUG"],
}
assert render_record(normalize_record({"name": "Plain"})) == "Plain"
