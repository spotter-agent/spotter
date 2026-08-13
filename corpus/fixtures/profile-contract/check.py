from profile import normalize_profile

from renderer import render_profile

profile = normalize_profile({"name": " Ada ", "role": "ADMIN"})
assert profile == {"name": "Ada", "role": "admin"}
assert render_profile(profile) == "Ada (admin)"
