"""Build the dependency-free static project site."""

import shutil
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SITE_DIR.parent
OUTPUT_DIR = SITE_DIR / "_build"

SITE_FILES = ("index.html", "styles.css")
BRAND_ASSETS = {
    REPOSITORY_ROOT / "docs/assets/main.png": "favicon.png",
    REPOSITORY_ROOT / "docs/assets/social-preview.png": "social-preview.png",
}


def main() -> None:
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)

    assets_dir = OUTPUT_DIR / "assets"
    assets_dir.mkdir(parents=True)

    for filename in SITE_FILES:
        shutil.copy2(SITE_DIR / filename, OUTPUT_DIR / filename)
    for source, filename in BRAND_ASSETS.items():
        shutil.copy2(source, assets_dir / filename)


if __name__ == "__main__":
    main()
