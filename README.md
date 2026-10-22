# garmin-archive

Local-first Python library and CLI (`garmin-archive`) that archives Garmin Connect activities
through the official Garmin Personal API into a SQLite database, with a read-only SQL interface.
The master specification is [docs/SPEC.md](docs/SPEC.md). Contributor rules are in
[CLAUDE.md](CLAUDE.md).

## Development

```bash
uv sync                          # install dependencies
uv run pytest                    # tests
uv run ruff check .              # lint
uv run ruff format --check .     # formatting
uv run pyright                   # type check
uv run garmin-archive --help     # CLI
```

## Documentation

- [Contract discovery tool](docs/discovery.md): captures sanitized API contract fixtures. The
  API key is read from the environment or a dotenv file, never from a CLI argument. Raw
  captures stay in `.local/` and are never committed.
