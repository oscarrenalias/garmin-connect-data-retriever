# CLAUDE.md

Guidance for Claude Code (interactive sessions and takt worker agents) in this repository.

## Project

`garmin_archive` is a local-first Python library plus a thin CLI (`garmin-archive`). It archives every Garmin Connect activity through the official Garmin **Personal API** into a single SQLite database and exposes a read-only SQL interface that an agent skill can use for analysis.

- **Master specification:** `docs/SPEC.md`. It is the source of truth for behavior. Feature specs in `specs/` implement slices of it and cite its sections (for example, "SPEC §6.1"). When a feature spec and `docs/SPEC.md` disagree, stop and flag the conflict. Do not pick one silently.
- **Upstream contract:** Only the documented Personal API endpoints listed in SPEC §2.2 may be used. Never call private Garmin web endpoints. Never invent API response fields. If a field or behavior isn't confirmed by the OpenAPI snapshot or a recorded contract fixture, treat it as unknown.

## Repository layout

```text
src/garmin_archive/    # library package (src layout); CLI lives in cli.py only
tests/                 # pytest suite; tests/fixtures/ holds SANITIZED fixtures only
docs/SPEC.md           # master spec
docs/upstream/         # OpenAPI snapshot + manifest (URL, retrieval date, SHA-256)
specs/                 # takt feature specs: drafts/ → planned/ → done/
.takt/                 # takt state and config (beads are not committed)
templates/             # takt agent/skill templates (takt-managed; don't edit casually)
```

## Development commands

All tooling runs through `uv`:

```bash
uv sync                          # install deps (incl. dev group)
uv run pytest                    # full test suite (takt merge gate)
uv run pytest tests/path -q      # focused tests
uv run ruff check .              # lint
uv run ruff format .             # format
uv run pyright                   # type check (strict for src/)
uv run garmin-archive --help     # CLI
```

Before finishing any code change, run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright` and the relevant tests. All of them must be clean.

## Architectural rules (non-negotiable)

- **Library first.** All behavior lives in the library. `cli.py` only parses arguments, calls library functions and serializes results. It never contains auth, SQL authorization, ingestion or sport-mapping logic.
- **No side effects in the library.** Library code never calls `print`, `sys.exit` or `input`, and never prompts. HTTP client, credential provider, clock and progress reporter are injected dependencies, so tests use fakes instead of monkeypatching globals.
- **Local methods never touch the network** and never require credentials.
- **Raw data is sacred.** Store response bodies exactly as received: TEXT for decodable text plus a BLOB holding the authoritative bytes. Never re-serialize JSON into the raw store. Never delete archived payloads because an upstream resource disappeared.
- **Garmin IDs are TEXT** in storage and JSON output.
- **Null means absent.** Never coerce a missing metric to 0.
- **Timestamps:** Store UTC as RFC 3339 strings ending in `Z`. Never interpret offset-free Garmin timestamps using the host timezone.
- **Python 3.11+** compatibility. Must run on Linux/ARM64, macOS and Windows. Use `pathlib`, never hard-coded paths.
- **Dependencies:** Use `httpx` for HTTP, `garmin-fit-sdk` for FIT decoding, Typer for the CLI and the stdlib `sqlite3` module for storage. Add any other runtime dependency only if the spec calls for it.

## Security and privacy rules (non-negotiable)

- Never write API keys, access tokens, token endpoint responses or `Authorization` headers to the database, logs, fixtures, test output, CLI output or exception messages.
- The API key is never accepted as a command-line argument.
- **No real personal data in git.** Never commit real activity files (FIT/ZIP/GPX/TCX), databases (`*.db`, `*.sqlite`), unredacted API responses or anything under `.local/`. Fixtures in `tests/fixtures/` must be synthetic or sanitized (identities, names, GPS coordinates, device serials and free text redacted).
- Tests must never hit the real Garmin API. Live tests are opt-in only (pytest marker `live`, skipped unless `GARMIN_LIVE_TESTS=1`) and must not mutate the account.

## Testing conventions

- pytest with fixtures and fakes. Use `httpx.MockTransport` for HTTP tests.
- Every behavior in a spec's acceptance criteria gets at least one test.
- Tests must be deterministic: no wall-clock dependence (inject a clock), no network and no ordering assumptions.

## takt workflow

This project uses **takt** for multi-agent orchestration and **spec-management** (`.claude/skills/skill-spec-management/spec.py`) for spec lifecycle. Read `.claude/skills/takt/SKILL.md` before doing any takt operation.

- Always use `uv run takt ...`. Never run plain `takt`.
- Specs: create and transition them with `python3 .claude/skills/skill-spec-management/spec.py`. Never `mv` spec files or hand-edit frontmatter.
- Flow: spec draft → `spec-reviewer` agent review → `uv run takt plan --write <spec>` → `spec.py set status planned` → `uv run takt --runner claude run` → `uv run takt merge <feature-root>` → `spec.py set status done`.
- Never `git merge` feature branches manually and never resolve takt merge-conflict beads by hand. Let the scheduler handle them.
- Keep each spec small enough for about 3–5 developer beads.
