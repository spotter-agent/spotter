"""Validate local links and required metadata in the built static site."""

from argparse import ArgumentParser
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ALLOWED_EXTERNAL_SCHEMES = {"https", "mailto"}
REQUIRED_META = {
    "description",
    "og:description",
    "og:image",
    "og:title",
    "og:type",
    "og:url",
    "twitter:card",
}


class SiteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.references: list[str] = []
        self.meta: set[str] = set()
        self.has_title = False
        self.has_canonical = False
        self.has_favicon = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)

        if tag in {"a", "link"} and attributes.get("href"):
            self.references.append(attributes["href"])
        if tag in {"img", "script"} and attributes.get("src"):
            self.references.append(attributes["src"])

        if tag == "meta":
            key = attributes.get("name") or attributes.get("property")
            if key:
                self.meta.add(key)
        if tag == "link" and attributes.get("rel") == "canonical":
            self.has_canonical = True
        if tag == "link" and attributes.get("rel") == "icon":
            self.has_favicon = True
        if tag == "title":
            self.has_title = True


def validate_page(page: Path, site_root: Path) -> list[str]:
    parser = SiteParser()
    parser.feed(page.read_text(encoding="utf-8"))
    errors: list[str] = []

    missing_meta = REQUIRED_META - parser.meta
    if missing_meta:
        errors.append(f"{page}: missing metadata: {', '.join(sorted(missing_meta))}")
    if not parser.has_title:
        errors.append(f"{page}: missing title")
    if not parser.has_canonical:
        errors.append(f"{page}: missing canonical link")
    if not parser.has_favicon:
        errors.append(f"{page}: missing favicon")

    for reference in parser.references:
        parsed = urlsplit(reference)
        if parsed.scheme:
            if parsed.scheme not in ALLOWED_EXTERNAL_SCHEMES:
                errors.append(f"{page}: unsupported URL scheme in {reference!r}")
            continue
        if parsed.netloc:
            errors.append(f"{page}: protocol-relative URL is not allowed: {reference!r}")
            continue
        if parsed.path:
            target = site_root / unquote(parsed.path.lstrip("/"))
            if not target.exists():
                errors.append(f"{page}: missing local target {reference!r}")
        if parsed.fragment and not parsed.path and parsed.fragment not in parser.ids:
            errors.append(f"{page}: missing fragment target #{parsed.fragment}")

    return errors


def main() -> None:
    argument_parser = ArgumentParser()
    argument_parser.add_argument("site_root", type=Path)
    site_root = argument_parser.parse_args().site_root.resolve()

    errors = validate_page(site_root / "index.html", site_root)
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"validated {site_root}")


if __name__ == "__main__":
    main()
