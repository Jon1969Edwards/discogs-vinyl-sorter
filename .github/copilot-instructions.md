## Brief

This repository is a single-file Python CLI tool that fetches your Discogs collection
and produces a printable shelf order for 33⅓ LP vinyl. The canonical entrypoint is
`discogs_app.py` (see `README.md` for examples). Keep changes small and behavior-preserving
unless the user asks for a feature change.

## Contract (what edits should preserve)
- Inputs: CLI args or env `DISCOGS_TOKEN` (or `--token`).
- Outputs: `vinyl_shelf_order.txt`, `vinyl_shelf_order.csv` (and optional JSON).
- Exit behavior: non-zero on errors, prints helpful messages; preserve message shape.

## Key files
- `discogs_app.py` — single main module. All core logic lives here: API calls, filtering,
  sorting, and writers.
- `README.md` — authoritative run/install examples.
- `requirements.txt` — runtime deps: `requests`, optional `python-dotenv`.
- `gui_app.py` — optional Tkinter GUI wrapper (no extra deps) that calls existing functions; keep it thin and avoid logic drift from CLI.

## Big-picture architecture to know
- Single-process CLI: no services or background workers.
- Integration point: Discogs HTTP API. Identity is fetched via `/oauth/identity` and
  the collection is iterated via `/users/{username}/collection/folders/0/releases`.
- Pagination is handled in `iterate_collection`; rate-limit/backoff logic is in `api_get`.
  Respect `Retry-After` header and existing retry/backoff behavior when editing network code.

## Project-specific conventions & important behaviors
- LP detection: `is_lp_33(...)` implements permissive (default) vs strict (`--lp-strict`) modes.
  Changes to filtering must preserve existing flags and the documented behavior in `README.md`.
- Sorting: artist/article stripping and numeric-suffix removal are applied via
  `make_sort_keys`, `strip_discogs_numeric_suffix`, and `strip_articles` logic. The
  `--articles-extra` flag supplies additional leading articles.
- Last-name-first: heuristic implemented in `_last_name_first_key` and enabled with
  `--last-name-first` (with optional `--lnf-allow-3` and `--lnf-exclude`). Keep heuristics
  conservative unless user asks for a new heuristic.
- Band-safe LNF: `--lnf-safe-bands` prevents flipping obvious two-word band names (plural nouns
  and ensemble terms like Orchestra/Trio), while still flipping clear personal names (e.g. "Miles Davis").
- Various artists: `--various-policy last` pushes 'Various' to the end; preserve this option.

## Developer workflows (how to run & test locally)
1. Create and activate a venv (Python 3.9+):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Run with token via env or CLI. Examples from `README.md`:

```bash
# env
DISCOGS_TOKEN=xxx python discogs_app.py --user-agent "VinylSorter/1.0 (you@example.com)"
# cli
python discogs_app.py --token xxx --dividers --json
```

3. Debugging: use `--debug-stats` to get filtering counts. Use `--max-pages` to limit
   API pages for fast iteration during development.

## Editing guidance for AI agents
- Small, focused PRs: change one behavior at a time (filtering, sorting, output format).
- Preserve CLI flags and defaults unless the user requests breaking changes.
- When modifying network logic, keep `api_get`'s retry/backoff and `Retry-After` handling.
- Prefer adding unit tests if you add non-trivial parsing/sorting helpers, but do not add
  a heavy test infra without explicit instruction from the repo owner.
- GUI changes: do not duplicate business logic—always invoke helpers in `discogs_app.py`. If adding new options, wire flag → GUI checkbox consistently and update README.

## Examples of patterns to follow
- Normalization helpers are in-file (e.g., `normalize_apostrophes`, `strip_discogs_numeric_suffix`).
  Mirror their style (small helper functions near usage).
- I/O: writers `write_txt`, `write_csv`, and `write_json` write directly to `Path` objects and
  use UTF-8; respect that encoding when changing output code.

## Quick checks before submitting changes
- Run the script locally with a small page cap: `--max-pages 2`.
- If changing sorting or article-stripping, confirm outputs still match human expectations
  for examples in the README (artist/title ordering, sample discogs suffixes like `(2)`).

## Where to look next when confused
- Start with `discogs_app.py` top-to-bottom; the CLI, pagination, filtering, and writers are
  implemented sequentially which makes it easy to trace behavior.
- `README.md` contains the most up-to-date run and option examples.

If anything in these notes is unclear or you want the instructions expanded (examples of
expected output diffs, unit test skeletons, or release/versioning rules), tell me which
section to expand and I'll update the file.
