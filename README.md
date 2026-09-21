# hermes-plugin-coder

Third-party [Coder](https://coder.com/) terminal environment provider for Hermes Agent.

## Requirements

- A Coder deployment reachable over HTTPS and an existing workspace.
- Hermes Agent with the terminal-backend plugin contract.
- Python dependencies declared in `pyproject.toml`.

## Configuration

Upstream Hermes currently passes only the standard provider factory arguments to terminal plugins, so this compatibility branch reads Coder settings from the active profile environment:

```dotenv
CODER_URL=https://coder.example.com
CODER_API_KEY=...
CODER_WORKSPACE=my-workspace
TERMINAL_CODER_FORWARD_ENV=["GITHUB_TOKEN"]
TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT=180
```

The first three variables are required. `TERMINAL_CODER_FORWARD_ENV` defaults to `[]`, and `TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT` defaults to `180`. Invalid or explicitly empty values fail closed.

Profile YAML under `terminal.backends.coder`, Dashboard/Desktop configuration fields, and config-aware probing are temporarily unavailable until upstream Hermes gains the provider-owned configuration contract. The API key remains stripped from model-authored subprocesses and is never included in probe details.

## Development installation

Hermes source plugins are directories under `$HERMES_HOME/plugins` containing `plugin.yaml` and a root `__init__.py`. A symlink is sufficient:

```bash
ln -s /path/to/hermes-plugin-coder "${HERMES_HOME:-$HOME/.hermes}/plugins/coder-dev"
hermes plugins enable coder
```

The symlink makes the source plugin discoverable; `plugins enable` opts it into the active profile. Select it with `hermes config set terminal.backend coder`. The plugin registers a `TerminalEnvironmentProvider`; upstream Hermes owns environment creation and task lifecycle, while this compatibility branch resolves Coder settings from the active profile environment.

Coder workspaces are durable user infrastructure, so the provider keeps dangerous-command guards enabled even though it uses isolated remote filesystem semantics. Only names explicitly listed in `forward_env` are copied into the remote session snapshot. Their values are delivered after the authenticated PTY WebSocket opens and are never placed in its request URL.

## Tests

From a Hermes Agent checkout with its virtual environment available:

```bash
PYTHONPATH=/path/to/hermes-plugin-coder:/path/to/hermes-agent \
  /path/to/hermes-agent/.venv/bin/python -m pytest tests -q
```
