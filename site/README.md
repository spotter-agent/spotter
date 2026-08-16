# Spotter project site

This directory is the source for <https://spotter-agent.github.io/spotter/>. The site is a
dependency-free static entry point; detailed and fast-changing implementation information remains
canonical in the repository documentation.

## Preview and validate

From the repository root:

```bash
python site/build.py
python site/check.py site/_build
python -m http.server --directory site/_build 8000
```

Then open <http://localhost:8000>. The generated `site/_build/` directory is ignored by Git.

## Update

- Edit `site/index.html` or `site/styles.css`.
- Reuse repository branding from `docs/assets/`; `site/build.py` copies the selected web assets.
- Keep current/target capability statements aligned with `docs/status.md` and `docs/architecture.md`.
- Run the build, link check, and repository full checks before opening a pull request.

## Deploy

`.github/workflows/pages.yml` validates site changes on pull requests. A site change merged to
`main` builds the same static output and deploys it with GitHub's Pages artifact action. The default
repository Pages URL is retained; a future custom-domain file can be added independently.
