# hermes-plugin-coder

Third-party [Coder](https://coder.com/) terminal environment provider for Hermes Agent.

## Requirements

- A Coder deployment reachable over HTTPS and an existing workspace.
- Hermes Agent with the terminal-backend plugin contract.
- Python dependencies declared in `pyproject.toml`.

## Configuration

Select Coder and store its non-secret settings in the active Hermes profile config:

```yaml
terminal:
  backend: coder
  backends:
    coder:
      base_url: https://coder.example.com
      workspace_name: my-workspace
      forward_env:
        - GITHUB_TOKEN
      workspace_startup_timeout: 180
```

Keep the Coder API key in the active profile's `.env` (recommended):

```dotenv
CODER_API_KEY=...
```

Every schema field can also be overridden through its declared environment variable:

```dotenv
CODER_URL=https://coder.example.com
CODER_WORKSPACE=my-workspace
TERMINAL_CODER_FORWARD_ENV=["GITHUB_TOKEN"]
TERMINAL_CODER_WORKSPACE_STARTUP_TIMEOUT=180
```

Runtime precedence is environment variable, then profile YAML, then the backend default. An explicitly set but invalid or empty environment override fails closed instead of falling back to YAML.

The Dashboard/Desktop schema also exposes `api_key` as a masked secret field under `terminal.backends.coder`. `CODER_API_KEY` takes precedence when it is present. The key is used only for Coder REST and PTY WebSocket authentication, is stripped from model-authored subprocesses, and is never included in probe details.

## Development installation

Hermes source plugins are directories under `$HERMES_HOME/plugins` containing `plugin.yaml` and a root `__init__.py`. A symlink is sufficient:

```bash
ln -s /path/to/hermes-plugin-coder "${HERMES_HOME:-$HOME/.hermes}/plugins/coder-dev"
hermes plugins enable coder
```

The symlink makes the source plugin discoverable; `plugins enable` opts it into the active profile. Select it with `hermes config set terminal.backend coder`. The plugin registers a `TerminalEnvironmentProvider`; Hermes owns profile-scoped config resolution, environment creation, and task lifecycle.

Coder workspaces are durable user infrastructure, so the provider keeps dangerous-command guards enabled even though it uses isolated remote filesystem semantics. Only names explicitly listed in `forward_env` are copied into the remote session snapshot. Their values are delivered after the authenticated PTY WebSocket opens and are never placed in its request URL.

## Tests

From a Hermes Agent checkout with its virtual environment available:

```bash
PYTHONPATH=/path/to/hermes-plugin-coder:/path/to/hermes-agent \
  /path/to/hermes-agent/.venv/bin/python -m pytest tests -q
```
