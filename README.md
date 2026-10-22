# garmin-archive

A local archive of your Garmin Connect activities, with read-only SQL for analysis.

- **Sync** every activity through Garmin's official Personal API: summary, workouts and the original FIT file, for every sport. Responses are stored byte-for-byte in a single SQLite file.
- **Process** them offline into analysis tables and views: sessions, laps, samples, sets, swim lengths, plus running, swimming, cycling and cardio metrics.
- **Query** with `schema`, `describe`, `sample` and read-only `sql`, either from the command line or through an AI agent. The agent setup is in [`SKILL.md`](SKILL.md).

Designed for a single user on modest hardware such as a Raspberry Pi. There are no services and no system packages. Nothing is uploaded anywhere.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- A Garmin Connect **Personal API key**

## Setup

```bash
git clone https://github.com/oscarrenalias/garmin-connect-data-retriever.git
cd garmin-connect-data-retriever
uv sync
cp .env.example .env        # then put your key in GARMIN_API_KEY
uv run garmin-archive init
uv run garmin-archive auth check
```

The key is read from the environment, or from `./.env` in the current directory (`--env-file PATH` to override). It is never stored anywhere else. A fresh access token is fetched on each run and held only in memory.

The database defaults to your platform's data directory. Override it with `GARMIN_ARCHIVE_DB` or `--db PATH`.

## Usage

```bash
uv run garmin-archive update            # incremental sync and processing; the first run takes a few minutes
uv run garmin-archive update --full     # re-download everything
uv run garmin-archive reprocess         # rebuild analysis tables offline
uv run garmin-archive import-form FILE  # import FORM goggles swims (or --url LINK); no credentials needed

uv run garmin-archive schema            # tables and views, with grain, columns and notes
uv run garmin-archive describe v_running
uv run garmin-archive sample v_swimming --limit 3
uv run garmin-archive sql --query "SELECT substr(local_date,1,7) AS month, ROUND(SUM(distance_m)/1000,1) AS km FROM v_running GROUP BY month"
```

Every command prints one JSON document on stdout, with progress on stderr. If an `update` is interrupted, run it again and it continues where it left off.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | ok |
| 2 | bad arguments or query |
| 3 | auth |
| 4 | network or API |
| 5 | storage |
| 6 | partial sync |
| 7 | processing failures |
| 8 | query timeout |
| 130 | interrupted |

## Documentation

- [`SKILL.md`](SKILL.md): how an agent should operate the CLI.
- [`docs/reference.md`](docs/reference.md): detailed command, storage and processing reference.
- [`docs/SPEC.md`](docs/SPEC.md): master specification, including the v1 decisions.
- [`docs/sport-metrics-and-views.md`](docs/sport-metrics-and-views.md): analysis tables and views.
- [`docs/discovery.md`](docs/discovery.md): developer tool that records the Garmin API contract.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

Tests never touch the network. Never commit databases, FIT/ZIP files, `.env`, or anything under `.local/`. Fixtures in `tests/fixtures/` are sanitized.

Development workflow and project rules are in [`CLAUDE.md`](CLAUDE.md). Work is planned and run with takt from the specs in `specs/`.
