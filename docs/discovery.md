# Contract discovery tool

A developer tool that probes the Garmin Personal API and records what it actually returns. It
produces a contract report and sanitized fixtures that later work can rely on. It is not part of
the `garmin-archive` CLI. It lives in `src/garmin_archive/devtools/discovery/`.

## Developer setup

```bash
uv sync                          # install dependencies, including the dev group
uv run pytest                    # tests (never hit the real Garmin API)
uv run ruff check .              # lint
uv run ruff format --check .     # formatting
uv run pyright                   # type check
```

## Running it

```bash
uv run python -m garmin_archive.devtools.discovery capture [options]
```

The tool is read-only against the account. Only documented Personal API endpoints are used.

### Credentials

The API key is read from one of these variables, checked in this order:

1. `GARMIN_API_KEY`
2. `GARMIN_CONNECT_API_KEY`

Sources, in priority order: the process environment, then a dotenv file. The dotenv file is
the one passed with `--env-file`, or `./.env` in the current directory if that exists. `.env` is
gitignored.

**The API key is never a command-line argument.** No option accepts it. Error and progress
output never include the key, tokens or exception messages.

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--env-file PATH` | none | dotenv file holding the API key |
| `--token-field NAME` | auto-detect | field of the token response that holds the token |
| `--page-size N` | 99 | page size for the activity inventory |
| `--max-pages N` | 500 | maximum inventory pages to fetch |
| `--activity-id ID` | none | explicit activity to sample (repeatable) |
| `--per-category N` | 2 | samples per category |
| `--cardio-type TYPE` | built-in list | activity type counted as cardio (repeatable; replaces the defaults) |
| `--delay SECONDS` | 1.0 | pause between requests |
| `--fixtures-dir PATH` | `tests/fixtures/contract` | where sanitized fixtures and the report go |

If the token field cannot be detected, the run stops with outcome `needs_token_field`. Rerun
with `--token-field`.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Run complete and the leak check passed |
| 1 | Unexpected error, or the leak check failed for at least one file |
| 2 | Run partial, stopped, or `needs_token_field` |
| 3 | API key not found |

On completion the tool prints `outcome`, the report path, the number of fixtures written and,
when relevant, a `next:` hint. Progress goes to stderr.

## Outputs

### Raw capture: local only

Every data-call response body is saved byte for byte under
`.local/discovery/<run-id>/raw/`, with one line per call in
`.local/discovery/<run-id>/requests.jsonl`. The run id is a UTC timestamp (`YYYYMMDDTHHMMSSZ`).
Only allowlisted response headers are logged. The token endpoint response and `Authorization`
headers are never recorded. **`.local/` is gitignored. Raw captures contain real personal data
and must never be committed.**

### Sanitized fixtures and report

Written under `tests/fixtures/contract/` (or `--fixtures-dir`):

```text
summaries/<name>.json         sanitized activity summaries
workouts/<name>.json          sanitized workouts bodies
search-items/<name>.json      sanitized inventory items
files/<name>.manifest.json    manifests of the original files (not the files)
redaction-log.json            json paths and rules applied, never the original values
contract-report.json          findings per SPEC §2.3 topic, with a leak_check section
contract-report.md            Markdown rendering of the report
```

Original activity files (FIT, ZIP, GPX, TCX) are never written to the fixtures directory.

## Leak check

Each file is sanitized, then checked against the set of sensitive values seen during the run
(IDs, names, free text and similar). The check runs immediately before each file is written,
and it covers manifests, the redaction log and the report too.

- A file with a hit is not written. The tool records its file name and JSON path, never the
  matched value.
- The run continues, and the failures appear in `contract-report.json` under `leak_check`.
- The run then exits with code 1 and prints `leak check failed for N fixture(s)` to stderr.

## Manual review before committing

The leak check reduces risk. It does not replace a human. Before committing anything under
`tests/fixtures/contract/`:

1. Read the new files, in particular `summaries/`, `workouts/`, `search-items/` and the
   report. Look for names, locations, coordinates, device serials, free text or IDs.
2. Check `redaction-log.json` and confirm the redacted paths look complete.
3. Run `git status` and confirm nothing from `.local/`, no `.env` and no FIT/ZIP/GPX/TCX or
   database file is staged.
4. If anything looks real, delete the file, fix the sanitizer and capture again.

Only commit after this review. Fixtures must be synthetic or sanitized (see `CLAUDE.md`).
