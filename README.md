# hermes-plugin-coder

Third-party [Coder](https://coder.com/) terminal backend for Hermes Agent's experimental pluggable environment runtime.

## Requirements

- A Coder deployment reachable over HTTPS and an existing workspace.
- Hermes Agent with the terminal-backend plugin contract.
- Python dependencies declared in `pyproject.toml`.

## Configuration

Set these values in the active Hermes profile's `.env` (normally `~/.hermes/.env`):

```dotenv
EXP_BACKEND=1
TERMINAL_ENV=coder
CODER_URL=https://coder.example.com
CODER_API_KEY=...
CODER_WORKSPACE=my-workspace
```

Optional values:

```dotenv
TERMINAL_CWD=~
TERMINAL_CODER_FORWARD_ENV=["GITHUB_TOKEN"]
TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT=180
```

`CODER_API_KEY` is used only for Coder REST and PTY WebSocket authentication. It is not included in backend diagnostic metadata.

## Development installation

Hermes source plugins are directories under `$HERMES_HOME/plugins` containing `plugin.yaml` and a root `__init__.py`. A symlink is sufficient:

```bash
ln -s /path/to/hermes-plugin-coder ~/.hermes/plugins/coder-dev
hermes plugins enable coder
```

The symlink makes the source plugin discoverable; `plugins enable` opts it into the active profile. Start Hermes with `EXP_BACKEND=1` and `TERMINAL_ENV=coder`. The plugin registers a `BackendDefinition`; Hermes owns environment creation and task lifecycle.

## Tests

From a Hermes Agent checkout with its virtual environment available:

```bash
PYTHONPATH=/path/to/hermes-plugin-coder:/path/to/hermes-agent \
  /path/to/hermes-agent/.venv/bin/python -m pytest tests -q
```
