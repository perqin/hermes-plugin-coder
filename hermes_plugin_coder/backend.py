"""Minimal Coder execution environment.

Resolve an existing workspace agent over the Coder REST API, open the workspace
PTY websocket, and read terminal output until EOF.

Current intentional limitations for the bootstrap step:
- no remote workspace provisioning; an existing workspace is required
- no host-side file-transfer primitive; file operations execute through the PTY
"""

from __future__ import annotations

import codecs
import contextlib
import ipaddress
import json
import logging
import os
import re
import shlex
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterable

import requests
from tools.environments import BaseEnvironment
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import ClientConnection, connect

logger = logging.getLogger(__name__)

# Patchable module-local clock hooks. Tests must patch these names rather than
# attributes on Python's shared ``time`` module, which can perturb unrelated
# worker threads and make PTY reconnect tests race nondeterministically.
_monotonic = time.monotonic
_sleep = time.sleep
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NON_FORWARDABLE_ENV = frozenset({"CODER_API_KEY"})


class _Unset:
    """Sentinel type distinguishing omitted constructor config from explicit null."""


_UNSET = _Unset()


def _normalize_forward_env_names(forward_env: Iterable[object] | None) -> list[str]:
    """Return deduplicated valid names explicitly approved for forwarding."""
    normalized: list[str] = []
    seen: set[str] = set()
    for item in () if forward_env is None else forward_env:
        if not isinstance(item, str):
            logger.warning("Ignoring non-string coder forward_env entry")
            continue
        name = item.strip()
        if not name or not _ENV_VAR_NAME_RE.fullmatch(name):
            logger.warning("Ignoring invalid coder forward_env entry")
            continue
        if name in _NON_FORWARDABLE_ENV:
            logger.warning("Ignoring non-forwardable Coder credential")
            continue
        if name not in seen:
            seen.add(name)
            normalized.append(name)
    return normalized


def _collect_forwarded_env_values(forward_env: list[str]) -> dict[str, str]:
    """Resolve only explicitly configured names from the active process env."""
    names = _normalize_forward_env_names(forward_env)
    resolved: dict[str, str] = {}
    for name in names:
        value = os.getenv(name)
        if value:
            resolved[name] = value
    return resolved


def _validate_coder_forward_env(value: object) -> list[str]:
    """Validate and normalize the provider's forward_env list."""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Coder forward_env must be a list of strings")
    return _normalize_forward_env_names(value)


def _validate_coder_startup_timeout(value: object) -> int:
    """Validate and return the positive integer workspace startup timeout."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("Coder workspace_startup_timeout must be a positive integer")
    return value


class _ThreadedProcessHandle:
    """Adapt a blocking Coder PTY call to Hermes' process-handle protocol."""

    def __init__(
        self,
        exec_fn: Callable[[], tuple[str, int]],
        cancel_fn: Callable[[], None] | None = None,
    ):
        self._cancel_fn = cancel_fn
        self._done = threading.Event()
        self._returncode: int | None = None
        read_fd, write_fd = os.pipe()
        self._stdout = os.fdopen(read_fd, "r", encoding="utf-8", errors="replace")
        self._write_fd = write_fd

        def worker() -> None:
            try:
                output, exit_code = exec_fn()
                self._returncode = exit_code
                with contextlib.suppress(OSError):
                    os.write(self._write_fd, output.encode("utf-8", errors="replace"))
            except Exception:  # noqa: BLE001 - process adapters fail as exit 1
                self._returncode = 1
            finally:
                with contextlib.suppress(OSError):
                    os.close(self._write_fd)
                self._done.set()

        threading.Thread(target=worker, daemon=True).start()

    @property
    def stdout(self):
        return self._stdout

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode if self._done.is_set() else None

    def kill(self) -> None:
        if self._cancel_fn:
            with contextlib.suppress(Exception):
                self._cancel_fn()

    def wait(self, timeout: float | None = None) -> int | None:
        self._done.wait(timeout=timeout)
        return self._returncode


class _BoundedPTYOutput:
    """Retain a fixed-size head/tail window while tracking total PTY output."""

    def __init__(self, capacity: int):
        self.capacity = max(2, capacity)
        self.head_capacity = max(1, self.capacity * 2 // 5)
        self.tail_capacity = self.capacity - self.head_capacity
        self.head = ""
        self.tail = ""
        self.total_chars = 0

    def append(self, text: str) -> None:
        if not text:
            return
        self.total_chars += len(text)
        remaining = text
        if len(self.head) < self.head_capacity:
            take = min(self.head_capacity - len(self.head), len(remaining))
            self.head += remaining[:take]
            remaining = remaining[take:]
        if remaining:
            self.tail = (self.tail + remaining)[-self.tail_capacity :]

    def value(self) -> str:
        if self.total_chars <= self.capacity:
            return self.head + self.tail
        omitted = self.total_chars - len(self.head) - len(self.tail)
        return self.head + f"\n... [{omitted} PTY characters omitted] ...\n" + self.tail


def _coder_headers(api_key: str) -> dict[str, str]:
    return {"Coder-Session-Token": api_key}


def _workspace_search_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/v2/workspaces"


def _find_workspace_by_name(
    *, base_url: str, workspace_name: str, api_key: str, timeout: int = 10
) -> dict | None:
    response = requests.get(
        _workspace_search_url(base_url),
        headers=_coder_headers(api_key),
        params={"q": f"owner:me name:{workspace_name}", "limit": 100},
        timeout=timeout,
        allow_redirects=False,
    )
    response.raise_for_status()
    payload = response.json()
    workspaces = payload.get("workspaces") if isinstance(payload, dict) else None
    if not isinstance(workspaces, list):
        raise RuntimeError(
            f"Unexpected workspace search payload while looking up {workspace_name!r}"
        )
    for workspace in workspaces:
        if isinstance(workspace, dict) and workspace.get("name") == workspace_name:
            owner = workspace.get("owner_name") or workspace.get("owner", {}).get(
                "username"
            )
            if owner in (None, "", "me") or owner == workspace.get("owner_name"):
                return workspace
    return None


def coder_workspace_exists(
    *, base_url: str, workspace_name: str, api_key: str, timeout: int = 10
) -> bool:
    """Return True when a workspace with the given name exists for the current user."""
    return (
        _find_workspace_by_name(
            base_url=base_url,
            workspace_name=workspace_name,
            api_key=api_key,
            timeout=timeout,
        )
        is not None
    )


def _normalize_coder_base_url(base_url: object) -> str:
    """Validate a Coder API origin and return its canonical form."""
    if (
        not isinstance(base_url, str)
        or not base_url
        or base_url != base_url.strip()
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in base_url
        )
    ):
        raise ValueError(
            "Coder base_url must not contain whitespace or control characters"
        )
    if "\\" in base_url:
        raise ValueError("Coder base_url must not contain backslashes")
    parsed_base_url = urllib.parse.urlsplit(base_url)
    if parsed_base_url.netloc.endswith(":"):
        raise ValueError("Coder base_url contains an empty port")
    try:
        parsed_port = parsed_base_url.port
    except ValueError as exc:
        raise ValueError("Coder base_url contains an invalid port") from exc
    hostname = parsed_base_url.hostname or ""
    if "%" in hostname:
        raise ValueError("Coder base_url contains an invalid hostname")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if (
            not re.fullmatch(r"[A-Za-z0-9.-]+", hostname)
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                for label in labels
            )
            or (hostname.replace(".", "").isdigit() and "." in hostname)
        ):
            raise ValueError("Coder base_url contains an invalid hostname") from None
    if (
        parsed_base_url.scheme != "https"
        or not parsed_base_url.hostname
        or parsed_base_url.username is not None
        or parsed_base_url.password is not None
        or parsed_base_url.query
        or parsed_base_url.fragment
        or parsed_base_url.path not in {"", "/"}
    ):
        raise ValueError(
            "Coder base_url must be an HTTPS origin without credentials, "
            "path, query, or fragment"
        )
    normalized_netloc = parsed_base_url.hostname
    if ":" in normalized_netloc and not normalized_netloc.startswith("["):
        normalized_netloc = f"[{normalized_netloc}]"
    if parsed_port is not None:
        normalized_netloc += f":{parsed_port}"
    return urllib.parse.urlunsplit(
        (parsed_base_url.scheme, normalized_netloc, "", "", "")
    )


def _validate_coder_api_key(api_key: object) -> str:
    """Validate and return a credential safe for the Coder HTTP header."""
    if (
        not isinstance(api_key, str)
        or not api_key
        or any(not 0x21 <= ord(character) <= 0x7E for character in api_key)
    ):
        raise ValueError("Coder api_key must be a non-empty header-safe string")
    return api_key


def _normalize_coder_workspace_name(workspace_name: object) -> str:
    """Validate and normalize the configured existing workspace name."""
    workspace = workspace_name.strip() if isinstance(workspace_name, str) else ""
    if not workspace:
        raise ValueError(
            "Coder environment requires explicit workspace_name (CODER_WORKSPACE)"
        )
    return workspace


class CoderEnvironment(BaseEnvironment):
    """Execute commands inside a Coder workspace via the /pty websocket."""

    _snapshot_timeout = 180
    _stdin_mode = "passthrough"
    _STDIN_CHUNK_SIZE = 32 * 1024
    _PTY_RECV_POLL_TIMEOUT = 1.0
    _PTY_EMPTY_EOF_RECONNECTS = 5
    _PTY_EMPTY_EOF_RECONNECT_WINDOW = 3.0
    _PTY_EMPTY_EOF_RECONNECT_DELAY = 0.2
    # Conservative, widely supported HTTP request-line/URL limit.  Coder's PTY
    # endpoint currently carries the command in the query string, so reject
    # locally before a proxy/server rejects an oversized URL opaquely.
    _MAX_PTY_URL_LENGTH = 8192
    _MAX_PTY_CAPTURE_CHARS = 1_000_000

    def __init__(
        self,
        *,
        base_url: str,
        task_id: str,
        api_key: str,
        workspace_name: str | None = None,
        cwd: str = "~",
        timeout: int = 60,
        forward_env: list[str] | _Unset = _UNSET,
        workspace_startup_timeout: int | _Unset = _UNSET,
        init_session: bool = True,
    ):
        super().__init__(cwd=cwd, timeout=timeout)
        # Pin the connector for this environment's lifetime. Besides keeping a
        # command internally consistent, this prevents concurrent environments
        # (and their test doubles) from changing one another's reconnect path.
        self._connect = connect
        self.base_url = _normalize_coder_base_url(base_url)
        self.task_id = task_id
        self.workspace = _normalize_coder_workspace_name(workspace_name)
        self.api_key = _validate_coder_api_key(api_key)
        if isinstance(workspace_startup_timeout, _Unset):
            self._workspace_startup_timeout = self._snapshot_timeout
        else:
            self._workspace_startup_timeout = _validate_coder_startup_timeout(
                workspace_startup_timeout
            )
        self._snapshot_timeout = self._workspace_startup_timeout
        self._workspace_id: str | None = None
        self._cleanup_complete = False
        self._session_cleanup_needed = False
        self._cleanup_lock = threading.Lock()
        if isinstance(forward_env, _Unset):
            forward_env = []
        self._forward_env = _validate_coder_forward_env(forward_env)

        # Safe to call here: init_session() uses _run_bash() directly, which
        # resolves the workspace/agent and opens a PTY without going back
        # through BaseEnvironment.execute(), so there is no recursive wrapping
        # or re-entry into init_session().
        if init_session:
            self.init_session()

    def _headers(self) -> dict[str, str]:
        return _coder_headers(self.api_key)

    def _workspace_url(self) -> str:
        if not self._workspace_id:
            raise RuntimeError(
                f"Coder workspace {self.workspace!r} has not been resolved yet"
            )
        workspace_id = urllib.parse.quote(self._workspace_id, safe="")
        return f"{self.base_url}/api/v2/workspaces/{workspace_id}"

    def _workspace_build_url(self, build_id: str) -> str:
        quoted_id = urllib.parse.quote(build_id, safe="")
        return f"{self.base_url}/api/v2/workspacebuilds/{quoted_id}"

    def _workspace_builds_url(self, workspace_id: str) -> str:
        quoted_id = urllib.parse.quote(workspace_id, safe="")
        return f"{self.base_url}/api/v2/workspaces/{quoted_id}/builds"

    @staticmethod
    def _startup_deadline(timeout: float, workspace_startup_timeout: float) -> float:
        # Startup REST polling is part of the command, so it must respect both
        # the command timeout and the Coder-specific startup bound.
        return _monotonic() + max(
            0.001, min(float(timeout), float(workspace_startup_timeout))
        )

    @staticmethod
    def _raise_if_cancelled(cancel_state: dict | None) -> None:
        if CoderEnvironment._cancel_requested(cancel_state):
            raise RuntimeError("Coder workspace startup cancelled")

    def _check_startup_deadline(self, deadline: float, what: str) -> None:
        if _monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for Coder {what} for {self.workspace!r}"
            )

    def _rest_timeout(self, deadline: float | None, what: str) -> float:
        if deadline is None:
            return float(self.timeout)
        self._check_startup_deadline(deadline, what)
        remaining = deadline - _monotonic()
        configured_timeout = float(self.timeout)
        if remaining + 0.05 >= configured_timeout:
            return self.timeout
        return max(0.001, min(configured_timeout, remaining))

    def _ensure_workspace(
        self, *, deadline: float | None = None, cancel_state: dict | None = None
    ) -> dict:
        self._raise_if_cancelled(cancel_state)
        payload = _find_workspace_by_name(
            base_url=self.base_url,
            workspace_name=self.workspace,
            api_key=self.api_key,
            timeout=self._rest_timeout(deadline, "workspace lookup"),
        )
        self._raise_if_cancelled(cancel_state)
        if payload is None:
            raise RuntimeError(f"Coder workspace {self.workspace!r} does not exist")
        workspace_id = payload.get("id") if isinstance(payload, dict) else None
        if not workspace_id:
            raise RuntimeError(
                f"Coder workspace {self.workspace!r} did not include a workspace id"
            )
        self._workspace_id = workspace_id
        return payload

    def _get_workspace_payload(
        self, *, deadline: float | None = None, cancel_state: dict | None = None
    ) -> dict:
        self._ensure_workspace(deadline=deadline, cancel_state=cancel_state)
        self._raise_if_cancelled(cancel_state)
        response = requests.get(
            self._workspace_url(),
            headers=self._headers(),
            timeout=self._rest_timeout(deadline, "workspace payload"),
            allow_redirects=False,
        )
        self._raise_if_cancelled(cancel_state)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Unexpected workspace payload for Coder workspace {self.workspace!r}"
            )
        return payload

    def _start_workspace(
        self,
        workspace_id: str,
        *,
        deadline: float | None = None,
        cancel_state: dict | None = None,
    ) -> str:
        self._raise_if_cancelled(cancel_state)
        response = requests.post(
            self._workspace_builds_url(workspace_id),
            headers=self._headers(),
            json={"transition": "start"},
            timeout=self._rest_timeout(deadline, "workspace start"),
            allow_redirects=False,
        )
        self._raise_if_cancelled(cancel_state)
        response.raise_for_status()
        payload = response.json()
        build_id = payload.get("id")
        if not build_id:
            raise RuntimeError(
                f"Coder start build for workspace {self.workspace!r} did not "
                "return a build id"
            )
        return build_id

    def _wait_for_build_completion(
        self,
        build_id: str,
        *,
        deadline: float,
        cancel_state: dict | None = None,
    ) -> None:
        while True:
            self._raise_if_cancelled(cancel_state)
            self._check_startup_deadline(
                deadline, f"workspace build {build_id} to complete"
            )
            response = requests.get(
                self._workspace_build_url(build_id),
                headers=self._headers(),
                timeout=self._rest_timeout(
                    deadline, f"workspace build {build_id} to complete"
                ),
                allow_redirects=False,
            )
            self._raise_if_cancelled(cancel_state)
            response.raise_for_status()
            payload = response.json()
            job = payload.get("job") or {}
            status = (job.get("status") or "").lower()
            if job.get("completed_at"):
                if status != "succeeded":
                    raise RuntimeError(
                        f"Coder workspace build {build_id} finished with status "
                        f"{status or 'unknown'}"
                    )
                return
            _sleep(min(2, max(0.0, deadline - _monotonic())))
            self._raise_if_cancelled(cancel_state)
            self._check_startup_deadline(
                deadline, f"workspace build {build_id} to complete"
            )

    @staticmethod
    def _agent_startup_ready(agent: dict) -> bool:
        """Return True when an agent is ready for PTY execution."""
        agent_status = str(agent.get("status") or "").strip().lower()
        if agent_status != "connected":
            return False

        lifecycle_state = str(agent.get("lifecycle_state") or "").strip().lower()
        return lifecycle_state == "ready"

    def _wait_for_agent_ready(
        self,
        payload: dict,
        *,
        deadline: float,
        cancel_state: dict | None = None,
    ) -> dict:
        while True:
            self._raise_if_cancelled(cancel_state)
            latest_build = payload.get("latest_build") or {}
            resources = latest_build.get("resources") or []
            for resource in resources:
                for agent in resource.get("agents") or []:
                    agent_id = agent.get("id")
                    if not agent_id:
                        continue
                    if self._agent_startup_ready(agent):
                        return agent

            self._check_startup_deadline(deadline, "workspace agent startup")
            _sleep(min(2, max(0.0, deadline - _monotonic())))
            self._raise_if_cancelled(cancel_state)
            self._check_startup_deadline(deadline, "workspace agent startup")
            payload = self._get_workspace_payload(
                deadline=deadline, cancel_state=cancel_state
            )

    def _resolve_agent_id(
        self,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        cancel_state: dict | None = None,
    ) -> str:
        command_timeout = self.timeout if timeout is None else timeout
        startup_deadline = self._startup_deadline(
            command_timeout, self._workspace_startup_timeout
        )
        if deadline is not None:
            startup_deadline = min(startup_deadline, deadline)
        payload = self._get_workspace_payload(
            deadline=startup_deadline, cancel_state=cancel_state
        )
        latest_build = payload.get("latest_build") or {}
        transition = (latest_build.get("transition") or "").lower()

        if transition != "start":
            if transition == "delete":
                raise RuntimeError(f"Coder workspace {self.workspace!r} is deleted")
            if (latest_build.get("status") or "").lower() != "stopped":
                raise RuntimeError(
                    f"Coder workspace {self.workspace!r} must be started before "
                    "terminal execution"
                )
            workspace_id = payload.get("id")
            if not workspace_id:
                raise RuntimeError(
                    f"Coder workspace {self.workspace!r} did not include a workspace id"
                )
            build_id = self._start_workspace(
                workspace_id, deadline=startup_deadline, cancel_state=cancel_state
            )
            self._wait_for_build_completion(
                build_id, deadline=startup_deadline, cancel_state=cancel_state
            )
            payload = self._get_workspace_payload(
                deadline=startup_deadline, cancel_state=cancel_state
            )
        else:
            job = latest_build.get("job") or {}
            if latest_build.get("id") and not job.get("completed_at"):
                self._wait_for_build_completion(
                    latest_build["id"],
                    deadline=startup_deadline,
                    cancel_state=cancel_state,
                )
                payload = self._get_workspace_payload(
                    deadline=startup_deadline, cancel_state=cancel_state
                )

        agent = self._wait_for_agent_ready(
            payload, deadline=startup_deadline, cancel_state=cancel_state
        )
        agent_id = agent.get("id")
        if agent_id:
            return agent_id
        raise RuntimeError(
            f"No workspace agent found for Coder workspace {self.workspace!r}"
        )

    @staticmethod
    def _exit_marker(reconnect_id: str) -> str:
        return f"__HERMES_EXIT_{reconnect_id}__"

    @staticmethod
    def _exit_marker_match(output: str, exit_marker: str) -> re.Match[str] | None:
        pattern = re.compile(
            rf"(?:\r?\n)?{re.escape(exit_marker)}(\d{{1,3}}){re.escape(exit_marker)}\r?\n?"
        )
        matches = list(pattern.finditer(output))
        return matches[-1] if matches else None

    @classmethod
    def _has_exit_marker(cls, output: str, exit_marker: str) -> bool:
        return cls._exit_marker_match(output, exit_marker) is not None

    @classmethod
    def _extract_exit_code(cls, output: str, exit_marker: str) -> tuple[str, int]:
        match = cls._exit_marker_match(output, exit_marker)
        if match is None:
            logger.error("[coder] PTY exit marker missing; treating command as failed")
            return output, 1

        exit_code = int(match.group(1))
        if not 0 <= exit_code <= 255:
            exit_code = 1
        cleaned = output[: match.start()] + output[match.end() :]
        return cleaned, exit_code

    def _pty_command(self, cmd_string: str, *, login: bool, exit_marker: str) -> str:
        inner_shell_flag = "-lc" if login else "-c"
        capture_script = "\n".join(
            [
                f"bash {inner_shell_flag} {shlex.quote(cmd_string)}",
                "__coder_ec=$?",
                f"printf '\\n{exit_marker}%s{exit_marker}\\n' \"$__coder_ec\"",
                'exit "$__coder_ec"',
            ]
        )
        return f"bash -c {shlex.quote(capture_script)}"

    def _build_init_env_exports(self) -> str:
        """Build shell exports that seed forwarded env vars into the snapshot."""
        env = _collect_forwarded_env_values(self._forward_env)
        if not env:
            return ""
        return "\n".join(
            f"export {key}={shlex.quote(value)}" for key, value in sorted(env.items())
        )

    def _pty_url(self, agent_id: str, *, command: str, reconnect_id: str) -> str:
        parsed = urllib.parse.urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        query = urllib.parse.urlencode(
            {
                "reconnect": reconnect_id,
                "command": command,
                "height": 80,
                "width": 80,
            }
        )
        return urllib.parse.urlunparse(
            (
                scheme,
                parsed.netloc,
                f"/api/v2/workspaceagents/{agent_id}/pty",
                "",
                query,
                "",
            )
        )

    @classmethod
    def _stdin_frame(cls, data: str) -> bytes:
        return json.dumps(
            {"data": data}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    @classmethod
    def _send_stdin_data(cls, websocket: ClientConnection, stdin_data: str) -> None:
        if not stdin_data:
            return
        chunk_size = max(1, cls._STDIN_CHUNK_SIZE)
        for start in range(0, len(stdin_data), chunk_size):
            chunk = stdin_data[start : start + chunk_size]
            frame = cls._stdin_frame(chunk)
            websocket.send(frame)

    @classmethod
    def _send_stdin_eof(cls, websocket: ClientConnection) -> None:
        # EOT / Ctrl+D signals EOF for stdin-driven commands.
        frame = cls._stdin_frame("\u0004")
        websocket.send(frame)

    @classmethod
    def _interrupt_pty(cls, websocket) -> bool:
        """Send Ctrl+C to a Coder PTY websocket, close it, and report send success.

        Coder PTY expects binary WebSocket frames that carry JSON payloads.
        Interrupt is ETX (0x03) in the "data" field.
        """
        sent = False
        try:
            frame = cls._stdin_frame("\u0003")
            websocket.send(frame)
            sent = True
        except Exception:  # noqa: BLE001 - best-effort cancellation
            logger.debug("[coder] Unable to send PTY interrupt")
        with contextlib.suppress(Exception):
            websocket.close()
        return sent

    def _interrupt_reconnected_pty(self, pty_url: str, timeout: float) -> bool:
        """Best-effort interrupt when the command deadline expires between sockets."""
        try:
            with self._connect(
                pty_url,
                additional_headers=self._headers(),
                open_timeout=max(0.1, min(1.0, timeout)),
                close_timeout=1,
            ) as websocket:
                return self._interrupt_pty(websocket)
        except Exception:  # noqa: BLE001 - transport exceptions vary by version
            logger.warning(
                "[coder] Unable to reconnect PTY for deadline interrupt: workspace=%s",
                self.workspace,
            )
            return False

    @staticmethod
    def _cancel_requested(cancel_state: dict | None) -> bool:
        if cancel_state is None:
            return False
        with cancel_state["lock"]:
            return bool(cancel_state.get("cancelled"))

    @staticmethod
    def _cancel_interrupt_sent(cancel_state: dict | None) -> bool:
        if cancel_state is None:
            return False
        with cancel_state["lock"]:
            return bool(cancel_state.get("interrupt_sent"))

    @staticmethod
    def _cancel_interrupt_in_flight(cancel_state: dict | None) -> bool:
        if cancel_state is None:
            return False
        with cancel_state["lock"]:
            return bool(cancel_state.get("interrupt_in_flight"))

    @staticmethod
    def _mark_cancel_interrupt_sent(cancel_state: dict | None) -> None:
        if cancel_state is not None:
            with cancel_state["lock"]:
                cancel_state["interrupt_sent"] = True

    @staticmethod
    def _wait_for_cancel_interrupt(cancel_state: dict | None) -> None:
        if cancel_state is None:
            return
        with cancel_state["lock"]:
            event = cancel_state.get("interrupt_event")
        if event is not None:
            event.wait(timeout=1)

    def _suggest_command_length_for_url_limit(
        self,
        *,
        agent_id: str,
        reconnect_id: str,
        cmd_string: str,
        login: bool,
    ) -> int:
        """Return the longest command prefix whose encoded PTY URL fits."""
        lo = 0
        hi = len(cmd_string)
        best = 0

        while lo <= hi:
            mid = (lo + hi) // 2
            exit_marker = self._exit_marker(reconnect_id)
            pty_command = self._pty_command(
                cmd_string[:mid], login=login, exit_marker=exit_marker
            )
            pty_url = self._pty_url(
                agent_id, command=pty_command, reconnect_id=reconnect_id
            )
            if len(pty_url.encode("utf-8")) <= self._MAX_PTY_URL_LENGTH:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        return best

    def _execute_via_pty(
        self,
        cmd_string: str,
        *,
        login: bool,
        timeout: int,
        stdin_data: str | None = None,
        cancel_state: dict | None = None,
    ) -> tuple[str, int]:
        deadline = _monotonic() + max(0.001, float(timeout))
        agent_id = self._resolve_agent_id(
            timeout=timeout, deadline=deadline, cancel_state=cancel_state
        )
        reconnect_id = str(uuid.uuid4())
        exit_marker = self._exit_marker(reconnect_id)
        pty_command = self._pty_command(
            cmd_string, login=login, exit_marker=exit_marker
        )
        pty_url = self._pty_url(
            agent_id, command=pty_command, reconnect_id=reconnect_id
        )
        encoded_url_length = len(pty_url.encode("utf-8"))
        if encoded_url_length > self._MAX_PTY_URL_LENGTH:
            suggested_command_length = self._suggest_command_length_for_url_limit(
                agent_id=agent_id,
                reconnect_id=reconnect_id,
                cmd_string=cmd_string,
                login=login,
            )
            if suggested_command_length <= 0:
                suggestion = (
                    "The fixed PTY URL overhead already exceeds the limit; "
                    "put the script in a file/stdin and execute that instead."
                )
            else:
                suggestion = (
                    f"Shorten the command to roughly {suggested_command_length} "
                    "characters "
                    "or put the script in a file/stdin and execute that instead."
                )
            return (
                "Coder PTY command is too long for the HTTP query URL: "
                f"encoded URL is {encoded_url_length} bytes, "
                f"limit is {self._MAX_PTY_URL_LENGTH} bytes. " + suggestion,
                1,
            )
        output = _BoundedPTYOutput(self._MAX_PTY_CAPTURE_CHARS)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        reconnect_attempts = 0
        stdin_send_attempted = False
        deadline_expired = False
        deadline_interrupt_sent = False
        connect_attempted = False
        marker_received = False

        while True:
            if self._cancel_requested(cancel_state):
                self._wait_for_cancel_interrupt(cancel_state)
                if (
                    connect_attempted
                    and not self._cancel_interrupt_sent(cancel_state)
                    and not self._cancel_interrupt_in_flight(cancel_state)
                    and self._interrupt_reconnected_pty(
                        pty_url, min(1.0, float(timeout))
                    )
                ):
                    self._mark_cancel_interrupt_sent(cancel_state)
                break
            remaining = deadline - _monotonic()
            if remaining <= 0:
                deadline_expired = True
                break
            attempt_started = _monotonic()
            attempt_start_chars = output.total_chars
            connect_attempted = True

            try:
                with self._connect(
                    pty_url,
                    additional_headers=self._headers(),
                    open_timeout=remaining,
                    close_timeout=1,
                ) as websocket:
                    if cancel_state is not None:
                        with cancel_state["lock"]:
                            cancel_state["websocket"] = websocket
                    try:
                        if self._cancel_requested(cancel_state):
                            self._wait_for_cancel_interrupt(cancel_state)
                            if (
                                not self._cancel_interrupt_sent(cancel_state)
                                and not self._cancel_interrupt_in_flight(cancel_state)
                                and self._interrupt_pty(websocket)
                            ):
                                self._mark_cancel_interrupt_sent(cancel_state)
                            continue

                        if stdin_data is not None and not stdin_send_attempted:
                            # The reconnect id resumes the same PTY session. Stdin
                            # must be forwarded at most once locally.
                            stdin_send_attempted = True
                            self._send_stdin_data(websocket, stdin_data)
                            self._send_stdin_eof(websocket)

                        while True:
                            try:
                                remaining = deadline - _monotonic()
                                if remaining <= 0:
                                    deadline_expired = True
                                    deadline_interrupt_sent = self._interrupt_pty(
                                        websocket
                                    )
                                    break
                                message = websocket.recv(
                                    timeout=max(
                                        0.1,
                                        min(self._PTY_RECV_POLL_TIMEOUT, remaining),
                                    ),
                                    decode=False,
                                )
                            except TimeoutError:
                                if _monotonic() >= deadline:
                                    deadline_expired = True
                                    deadline_interrupt_sent = self._interrupt_pty(
                                        websocket
                                    )
                                    break
                                continue
                            except (EOFError, ConnectionClosed):
                                break

                            if isinstance(message, bytes):
                                output.append(decoder.decode(message))
                            else:
                                output.append(message)
                            if self._has_exit_marker(output.value(), exit_marker):
                                marker_received = True
                                break
                    finally:
                        if cancel_state is not None:
                            with cancel_state["lock"]:
                                if cancel_state.get("websocket") is websocket:
                                    cancel_state["websocket"] = None
            except (OSError, TimeoutError, WebSocketException):
                logger.warning(
                    "[coder] PTY connection failed; retrying same session: "
                    "workspace=%s reconnect_id=%s",
                    self.workspace,
                    reconnect_id,
                )

            if marker_received or deadline_expired:
                break
            if self._cancel_requested(cancel_state):
                self._wait_for_cancel_interrupt(cancel_state)
                if (
                    connect_attempted
                    and not self._cancel_interrupt_sent(cancel_state)
                    and not self._cancel_interrupt_in_flight(cancel_state)
                    and self._interrupt_reconnected_pty(
                        pty_url, min(1.0, float(timeout))
                    )
                ):
                    self._mark_cancel_interrupt_sent(cancel_state)
                break

            attempt_output_chars = output.total_chars - attempt_start_chars
            attempt_elapsed = _monotonic() - attempt_started
            reconnect_attempts += 1
            logger.warning(
                "[coder] PTY closed before exit marker; reconnecting same session: "
                "workspace=%s reconnect_id=%s reconnect_attempt=%s "
                "attempt_output_chars=%s elapsed_ms=%.1f",
                self.workspace,
                reconnect_id,
                reconnect_attempts,
                attempt_output_chars,
                attempt_elapsed * 1000,
            )
            delay = min(
                self._PTY_EMPTY_EOF_RECONNECT_DELAY * reconnect_attempts,
                max(0.0, deadline - _monotonic()),
            )
            while delay > 0 and not self._cancel_requested(cancel_state):
                sleep_for = min(0.05, delay)
                _sleep(sleep_for)
                delay -= sleep_for

        if deadline_expired and connect_attempted and not deadline_interrupt_sent:
            self._interrupt_reconnected_pty(pty_url, min(1.0, float(timeout)))

        output.append(decoder.decode(b"", final=True))
        combined_output = output.value()
        cleaned_output, exit_code = self._extract_exit_code(
            combined_output, exit_marker
        )
        # Workaround: \r\n -> \n for pty
        cleaned_output = cleaned_output.replace("\r\n", "\n")
        return cleaned_output, exit_code

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ):
        if login:
            exports = self._build_init_env_exports()
            if exports:
                if stdin_data is not None:
                    raise ValueError(
                        "Coder login initialization cannot combine forwarded "
                        "environment with caller stdin_data"
                    )
                stdin_data = f"{exports}\n{cmd_string}\n"
                cmd_string = "bash -l -s"
                login = False

        cancel_state = {
            "lock": threading.Lock(),
            "websocket": None,
            "cancelled": False,
            "interrupt_sent": False,
            "interrupt_in_flight": False,
        }

        def cancel_pty() -> None:
            websocket = None
            with cancel_state["lock"]:
                cancel_state["cancelled"] = True
                websocket = cancel_state.get("websocket")
                if (
                    websocket is None
                    or cancel_state.get("interrupt_sent")
                    or cancel_state.get("interrupt_in_flight")
                ):
                    return
                cancel_state["interrupt_in_flight"] = True
                interrupt_event = cancel_state.get("interrupt_event")
                if interrupt_event is None:
                    interrupt_event = threading.Event()
                    cancel_state["interrupt_event"] = interrupt_event

            def send_active_interrupt() -> None:
                sent = self._interrupt_pty(websocket)
                try:
                    with cancel_state["lock"]:
                        cancel_state["interrupt_sent"] = bool(
                            cancel_state.get("interrupt_sent") or sent
                        )
                        cancel_state["interrupt_in_flight"] = False
                finally:
                    interrupt_event.set()

            threading.Thread(
                target=send_active_interrupt,
                name="coder-pty-interrupt",
                daemon=True,
            ).start()

        return _ThreadedProcessHandle(
            lambda: self._execute_via_pty(
                cmd_string,
                login=login,
                timeout=timeout,
                stdin_data=stdin_data,
                cancel_state=cancel_state,
            ),
            cancel_fn=cancel_pty,
        )

    def init_session(self):
        self._session_cleanup_needed = True
        return super().init_session()

    def cleanup(self):
        with self._cleanup_lock:
            self._cleanup_locked()

    def _cleanup_locked(self):
        if self._cleanup_complete:
            return
        if not self._session_cleanup_needed and not self._snapshot_ready:
            self._cleanup_complete = True
            return
        if self._workspace_id is None:
            self._cleanup_complete = True
            self._session_cleanup_needed = False
            self._snapshot_ready = False
            return
        snapshot = shlex.quote(self._snapshot_path)
        cwd_file = shlex.quote(self._cwd_file)
        snapshot_tmp_prefix = shlex.quote(self._snapshot_path + ".tmp.")
        command = f"rm -f -- {snapshot} {cwd_file} {snapshot_tmp_prefix}*"
        try:
            process = self._run_bash(command, timeout=5)
            try:
                returncode = process.wait(timeout=5)
            except TimeoutError:
                process.kill()
                logger.warning(
                    "[coder] Timed out removing remote session snapshot files: "
                    "workspace=%s",
                    self.workspace,
                )
                return
            if returncode is None:
                process.kill()
                logger.warning(
                    "[coder] Timed out removing remote session snapshot files: "
                    "workspace=%s",
                    self.workspace,
                )
            elif returncode != 0:
                logger.warning(
                    "[coder] Remote session snapshot cleanup failed: "
                    "workspace=%s returncode=%s",
                    self.workspace,
                    returncode,
                )
            else:
                self._cleanup_complete = True
                self._session_cleanup_needed = False
                self._snapshot_ready = False
        except Exception:  # noqa: BLE001 - cleanup must remain best effort
            logger.warning(
                "[coder] Failed to remove remote session snapshot files: workspace=%s",
                self.workspace,
            )
