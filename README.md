# poe-craft-mcp

A local [MCP](https://modelcontextprotocol.io) server that gives Claude live Path of Exile 1 economy
data (poe.ninja + the official trade site) and a curated, versioned list of crafts, so it can rank
crafts by expected profit for the current league.

**Status:** under construction. See [docs/design.md](docs/design.md) for the design.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```sh
uv sync                 # create .venv with runtime and dev dependencies
uv run pytest           # tests (never call live APIs)
uv run ruff check .     # lint
uv run ruff format .    # format
```

Copy `config.example.toml` to `config.toml` to change settings. Register the server with Claude Code:

```sh
claude mcp add poe-craft -- uv --directory <repo path> run poe-craft-mcp
```

## Third-party notice

This product isn't affiliated with or endorsed by Grinding Gear Games in any way.
Price data comes from [poe.ninja](https://poe.ninja) and the Path of Exile trade site.

## License

[MIT](LICENSE)
