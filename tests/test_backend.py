import json
import re
import threading
import uuid
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock

import pytest
from websockets.exceptions import WebSocketException

from hermes_plugin_coder.backend import CoderEnvironment, _BoundedPTYOutput


class _FakeWebSocket:
    def __init__(self, messages):
        self._messages = list(messages)
        self.requested = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def recv(self, timeout=None, decode=None):
        self.requested.append({"timeout": timeout, "decode": decode})
        if not self._messages:
            raise EOFError
        message = self._messages.pop(0)
        if isinstance(message, BaseException):
            raise message
        return message


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


def test_pty_output_collector_retains_bounded_head_and_tail():
    output = _BoundedPTYOutput(100)

    output.append("A" * 500)
    output.append("EXIT-MARKER")
    captured = output.value()

    assert captured.startswith("A" * 40)
    assert captured.endswith("EXIT-MARKER")
    assert "PTY characters omitted" in captured
    assert len(captured) < 160


def test_coder_environment_requires_explicit_workspace_name():
    with pytest.raises(ValueError, match="workspace_name"):
        CoderEnvironment(
            base_url="https://coder.example",
            task_id="task-coder",
            api_key="secret-token",
            workspace_name="",
            init_session=False,
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://coder.example",
        "ws://coder.example",
        "https://user:password@coder.example",
        "https://coder.example?redirect=https://attacker.example",
        "https://coder.example#fragment",
        " https://coder.example",
        "https://coder.example\n.attacker.example",
        "https://coder.example\t@attacker.example",
        "https://coder.example:99999",
        "https://coder.example:bad",
        "https://coder.example:",
        "https://coder.example:/",
        "https://[::1]:",
        "https://coder%2eexample",
        "https://.",
        "https://coder..example",
        "https://coder.example/prefix",
        "https://coder.example\\@attacker.example",
    ],
)
def test_coder_environment_rejects_insecure_or_ambiguous_base_url(base_url):
    with pytest.raises(ValueError, match="Coder base_url"):
        CoderEnvironment(
            base_url=base_url,
            task_id="task-coder",
            api_key="secret-token",
            workspace_name="shared-dev",
            init_session=False,
        )


@pytest.mark.parametrize(
    "api_key",
    ["", " token", "token\nInjected: value", "token\tvalue", "é", "Ā"],
)
def test_coder_environment_rejects_non_header_safe_api_key(api_key):
    with pytest.raises(ValueError, match="api_key"):
        CoderEnvironment(
            base_url="https://coder.example",
            task_id="task-coder",
            api_key=api_key,
            workspace_name="shared-dev",
            init_session=False,
        )


def test_coder_environment_defaults_snapshot_timeout_to_three_minutes():
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        init_session=False,
    )

    assert env._snapshot_timeout == 180


def test_coder_environment_uses_workspace_startup_timeout():
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        workspace_startup_timeout=240,
        init_session=False,
    )

    assert env._snapshot_timeout == 240


def test_coder_environment_initializes_session_snapshot_without_recursive_execute(
    monkeypatch,
):
    workspace_payload = {
        "id": "workspace-123",
        "name": "shared-dev",
        "latest_build": {
            "transition": "start",
            "resources": [
                {
                    "agents": [
                        {
                            "id": "agent-123",
                            "status": "connected",
                            "lifecycle_state": "ready",
                        }
                    ]
                }
            ],
        },
    }
    requests_get = MagicMock(
        side_effect=[
            _FakeResponse({"workspaces": [workspace_payload]}),
            _FakeResponse(workspace_payload),
            _FakeResponse({"workspaces": [workspace_payload]}),
            _FakeResponse(workspace_payload),
        ]
    )
    connect_urls = []

    def fake_connect(url, **_kwargs):
        connect_urls.append(url)
        query = parse_qs(urlparse(url).query)
        reconnect_id = query["reconnect"][0]
        exit_marker = f"__HERMES_EXIT_{reconnect_id}__"
        command = query["command"][0]
        cwd_match = re.search(r"__HERMES_CWD_[0-9a-f]{12}__", command)
        assert cwd_match is not None
        cwd_marker = cwd_match.group(0)
        return _FakeWebSocket(
            [
                f"\n{cwd_marker}/home/coder{cwd_marker}\n\n{exit_marker}0{exit_marker}\n".encode()
            ]
        )

    monkeypatch.setattr("hermes_plugin_coder.backend.requests.get", requests_get)
    monkeypatch.setattr("hermes_plugin_coder.backend.requests.post", MagicMock())
    monkeypatch.setattr("hermes_plugin_coder.backend.connect", fake_connect)
    monkeypatch.setenv("HERMES_CODER_SNAPSHOT_TEST", "forwarded-value")

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="shared-dev",
        timeout=5,
        forward_env=["HERMES_CODER_SNAPSHOT_TEST"],
    )

    assert env._snapshot_ready is True
    assert env.cwd == "/home/coder"
    assert len(connect_urls) == 1
    init_query = parse_qs(urlparse(connect_urls[0]).query)
    init_command = init_query["command"][0]
    assert init_command.startswith("bash -c ")
    assert "bash -lc" in init_command
    assert "export HERMES_CODER_SNAPSHOT_TEST=forwarded-value" in init_command
    assert "export -p" in init_command
    assert "declare -f" in init_command

    env.execute("printf $HERMES_CODER_SNAPSHOT_TEST")

    assert len(connect_urls) == 2
    followup_query = parse_qs(urlparse(connect_urls[1]).query)
    followup_command = followup_query["command"][0]
    assert f"source {env._snapshot_path}" in followup_command
    assert f"export -p > {env._snapshot_path}" in followup_command
    assert "printf $HERMES_CODER_SNAPSHOT_TEST" in followup_command


def test_coder_environment_leaves_snapshot_unready_when_init_session_fails(monkeypatch):
    workspace_payload = {
        "id": "workspace-123",
        "name": "shared-dev",
        "latest_build": {
            "transition": "start",
            "resources": [
                {
                    "agents": [
                        {
                            "id": "agent-123",
                            "status": "connected",
                            "lifecycle_state": "ready",
                        }
                    ]
                }
            ],
        },
    }
    monkeypatch.setattr(
        "hermes_plugin_coder.backend.requests.get",
        MagicMock(
            side_effect=[
                _FakeResponse({"workspaces": [workspace_payload]}),
                _FakeResponse(workspace_payload),
            ]
        ),
    )
    monkeypatch.setattr("hermes_plugin_coder.backend.requests.post", MagicMock())
    monkeypatch.setattr(CoderEnvironment, "_snapshot_timeout", 0.01)
    monkeypatch.setattr(
        "hermes_plugin_coder.backend.connect",
        MagicMock(return_value=_FakeWebSocket([b"init failed without exit marker\n"])),
    )

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="shared-dev",
        timeout=5,
    )

    assert env._snapshot_ready is False


def test_coder_environment_uses_configured_workspace_without_session_derivation(
    monkeypatch,
):
    existing_workspace = {"id": "workspace-123", "name": "shared-dev"}
    requests_get = MagicMock(
        return_value=_FakeResponse({"workspaces": [existing_workspace]})
    )
    requests_post = MagicMock()

    monkeypatch.setattr("hermes_plugin_coder.backend.requests.get", requests_get)
    monkeypatch.setattr("hermes_plugin_coder.backend.requests.post", requests_post)
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="shared-dev",
        timeout=5,
        init_session=False,
    )

    assert env.workspace == "shared-dev"
    assert env._ensure_workspace() == existing_workspace
    requests_get.assert_called_once_with(
        "https://coder.example/api/v2/workspaces",
        headers={"Coder-Session-Token": "secret-token"},
        params={"q": "owner:me name:shared-dev", "limit": 100},
        timeout=5,
        allow_redirects=False,
    )
    requests_post.assert_not_called()


def test_coder_environment_execute_existing_workspace_then_reads_pty_until_eof(
    monkeypatch,
):
    existing_workspace = {
        "id": "workspace-123",
        "name": "hermes-20260521-173045-ab12cd",
        "latest_build": {
            "transition": "start",
            "resources": [
                {
                    "agents": [
                        {
                            "id": "agent-123",
                            "status": "connected",
                            "lifecycle_state": "ready",
                        }
                    ]
                }
            ],
        },
    }
    reconnect_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    exit_marker = f"__HERMES_EXIT_{reconnect_id}__"
    fake_ws = _FakeWebSocket(
        [f"hello from coder\n\n{exit_marker}0{exit_marker}\n".encode()]
    )
    connect_mock = MagicMock(return_value=fake_ws)
    requests_get = MagicMock(
        side_effect=[
            _FakeResponse({"workspaces": [existing_workspace]}),
            _FakeResponse(existing_workspace),
        ]
    )
    requests_post = MagicMock()

    monkeypatch.setattr("hermes_plugin_coder.backend.requests.get", requests_get)
    monkeypatch.setattr("hermes_plugin_coder.backend.requests.post", requests_post)
    monkeypatch.setattr("hermes_plugin_coder.backend.connect", connect_mock)
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        cwd="/root",
        timeout=5,
        init_session=False,
    )

    result = env.execute("echo hello-from-hermes")

    assert result["returncode"] == 0
    assert result["output"] == "hello from coder\n"

    requests_post.assert_not_called()
    connect_kwargs = connect_mock.call_args.kwargs
    assert connect_kwargs["additional_headers"]["Coder-Session-Token"] == "secret-token"
    connect_url = connect_mock.call_args.args[0]
    assert "/api/v2/workspaceagents/agent-123/pty" in connect_url
    query = parse_qs(urlparse(connect_url).query)
    assert query["reconnect"] == [str(reconnect_id)]
    pty_command = query["command"][0]
    assert pty_command.startswith("bash -c ")
    assert f"{exit_marker}%s{exit_marker}" in pty_command
    assert "bash -lc" in pty_command
    assert "echo hello-from-hermes" in pty_command
    assert pty_command != "pwd"


def test_coder_environment_rejects_pty_url_that_exceeds_http_query_limit(monkeypatch):
    reconnect_id = uuid.UUID("44444444-5555-6666-7777-888888888888")
    connect_mock = MagicMock()

    monkeypatch.setattr("hermes_plugin_coder.backend.connect", connect_mock)
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )
    monkeypatch.setattr(CoderEnvironment, "_MAX_PTY_URL_LENGTH", 250, raising=False)

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=5,
        init_session=False,
    )

    result = env.execute("printf '%s' " + "x" * 500)

    assert result["returncode"] == 1
    assert "Coder PTY command is too long" in result["output"]
    assert "limit is 250 bytes" in result["output"]
    assert "put the script in a file/stdin" in result["output"]
    connect_mock.assert_not_called()


def test_coder_long_command_suggestion_uses_encoded_url_budget(monkeypatch):
    reconnect_id = uuid.UUID("44444444-5555-6666-7777-888888888888")
    connect_mock = MagicMock()
    monkeypatch.setattr("hermes_plugin_coder.backend.connect", connect_mock)
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )
    monkeypatch.setattr(CoderEnvironment, "_MAX_PTY_URL_LENGTH", 900, raising=False)
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=5,
        init_session=False,
    )
    command = "printf '%s'\n" * 200
    full_url_length = len(
        env._pty_url(
            "agent-123",
            command=env._pty_command(
                command,
                login=True,
                exit_marker=env._exit_marker(str(reconnect_id)),
            ),
            reconnect_id=str(reconnect_id),
        ).encode("utf-8")
    )
    result = env.execute(command)
    match = re.search(r"roughly (\d+) characters", result["output"])
    assert result["returncode"] == 1
    assert match is not None
    suggested = int(match.group(1))
    assert suggested > 0
    assert suggested < len(command)
    assert max(0, len(command) - (full_url_length - env._MAX_PTY_URL_LENGTH)) == 0
    connect_mock.assert_not_called()


def test_coder_environment_reconnects_same_pty_after_empty_initial_eof(monkeypatch):
    reconnect_id = uuid.UUID("22222222-3333-4444-5555-666666666666")
    exit_marker = f"__HERMES_EXIT_{reconnect_id}__"
    first_ws = _FakeWebSocket([])
    second_ws = _FakeWebSocket(
        [f"hello after reconnect\n\n{exit_marker}0{exit_marker}\n".encode()]
    )
    connect_mock = MagicMock(side_effect=[first_ws, second_ws])

    monkeypatch.setattr("hermes_plugin_coder.backend.connect", connect_mock)
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)
    monkeypatch.setattr("hermes_plugin_coder.backend._sleep", lambda _seconds: None)
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=5,
        init_session=False,
    )

    result = env._execute_via_pty("echo hello", login=False, timeout=5)

    assert result == ("hello after reconnect\n", 0)
    assert connect_mock.call_count == 2
    first_query = parse_qs(urlparse(connect_mock.call_args_list[0].args[0]).query)
    second_query = parse_qs(urlparse(connect_mock.call_args_list[1].args[0]).query)
    assert first_query["reconnect"] == [str(reconnect_id)]
    assert second_query["reconnect"] == [str(reconnect_id)]
    assert second_query["command"] == first_query["command"]


def test_coder_environment_reconnects_after_partial_output_before_marker(monkeypatch):
    reconnect_id = uuid.UUID("22222222-3333-4444-5555-777777777777")
    exit_marker = f"__HERMES_EXIT_{reconnect_id}__"
    first_ws = _FakeWebSocket([b"partial output\n"])
    second_ws = _FakeWebSocket([f"completed\n\n{exit_marker}0{exit_marker}\n".encode()])
    connect_mock = MagicMock(side_effect=[first_ws, second_ws])

    monkeypatch.setattr("hermes_plugin_coder.backend.connect", connect_mock)
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)
    monkeypatch.setattr("hermes_plugin_coder.backend._sleep", lambda _seconds: None)
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=5,
        init_session=False,
    )

    result = env._execute_via_pty("echo hello", login=False, timeout=5)

    assert result == ("partial output\ncompleted\n", 0)
    assert connect_mock.call_count == 2
    first_query = parse_qs(urlparse(connect_mock.call_args_list[0].args[0]).query)
    second_query = parse_qs(urlparse(connect_mock.call_args_list[1].args[0]).query)
    assert first_query["reconnect"] == second_query["reconnect"]


@pytest.mark.parametrize(
    "connect_error",
    [OSError("temporary connect failure"), WebSocketException("handshake failed")],
)
def test_coder_environment_retries_connect_error_after_partial_output(
    monkeypatch, connect_error
):
    reconnect_id = uuid.UUID("22222222-3333-4444-5555-888888888888")
    exit_marker = f"__HERMES_EXIT_{reconnect_id}__"
    connect_mock = MagicMock(
        side_effect=[
            _FakeWebSocket([b"partial\n"]),
            connect_error,
            _FakeWebSocket([f"done\n{exit_marker}0{exit_marker}\n".encode()]),
        ]
    )
    monkeypatch.setattr("hermes_plugin_coder.backend.connect", connect_mock)
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)
    monkeypatch.setattr("hermes_plugin_coder.backend._sleep", lambda _seconds: None)
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="shared-dev",
        timeout=5,
        init_session=False,
    )

    result = env._execute_via_pty("echo done", login=False, timeout=5)

    assert result == ("partial\ndone", 0)
    assert connect_mock.call_count == 3


def test_coder_environment_interrupts_when_cancelled_during_reconnect_error(
    monkeypatch,
):
    reconnect_id = uuid.UUID("22222222-3333-4444-5555-bbbbbbbbbbbb")
    cancel_state = {
        "lock": threading.Lock(),
        "websocket": None,
        "cancelled": False,
    }
    calls = 0

    def connect_side_effect(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _FakeWebSocket([b"partial\n"])
        with cancel_state["lock"]:
            cancel_state["cancelled"] = True
        raise OSError("cancelled reconnect")

    monkeypatch.setattr("hermes_plugin_coder.backend.connect", connect_side_effect)
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)
    monkeypatch.setattr("hermes_plugin_coder.backend._sleep", lambda _seconds: None)
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="shared-dev",
        timeout=5,
        init_session=False,
    )
    interrupt = MagicMock()
    monkeypatch.setattr(env, "_interrupt_reconnected_pty", interrupt)

    output, returncode = env._execute_via_pty(
        "echo done", login=False, timeout=5, cancel_state=cancel_state
    )

    assert output == "partial\n"
    assert returncode == 1
    interrupt.assert_called_once()


def test_coder_environment_stops_receiving_as_soon_as_marker_arrives(monkeypatch):
    reconnect_id = uuid.UUID("22222222-3333-4444-5555-999999999999")
    exit_marker = f"__HERMES_EXIT_{reconnect_id}__"
    websocket = _FakeWebSocket(
        [
            f"done\n{exit_marker}0{exit_marker}\n".encode(),
            AssertionError("recv called after exit marker"),
        ]
    )
    monkeypatch.setattr(
        "hermes_plugin_coder.backend.connect", MagicMock(return_value=websocket)
    )
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="shared-dev",
        timeout=5,
        init_session=False,
    )

    assert env.execute("echo done") == {"output": "done", "returncode": 0}
    assert len(websocket._messages) == 1


def test_coder_environment_decodes_utf8_split_across_binary_frames(monkeypatch):
    reconnect_id = uuid.UUID("22222222-3333-4444-5555-aaaaaaaaaaaa")
    exit_marker = f"__HERMES_EXIT_{reconnect_id}__"
    websocket = _FakeWebSocket(
        [b"price: \xe2", b"\x82\xac\n" + f"{exit_marker}0{exit_marker}\n".encode()]
    )
    monkeypatch.setattr(
        "hermes_plugin_coder.backend.connect", MagicMock(return_value=websocket)
    )
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="shared-dev",
        timeout=5,
        init_session=False,
    )

    assert env.execute("printf euro") == {"output": "price: €", "returncode": 0}


def test_coder_environment_reconnects_empty_eof_with_stdin_without_resending(
    monkeypatch,
):
    reconnect_id = uuid.UUID("22222222-3333-4444-5555-666666666666")
    exit_marker = f"__HERMES_EXIT_{reconnect_id}__"

    class _SendingWebSocket(_FakeWebSocket):
        def __init__(self, messages):
            super().__init__(messages)
            self.sent = []

        def send(self, message):
            self.sent.append(message)

    first_ws = _SendingWebSocket([])
    second_ws = _SendingWebSocket(
        [f"after reconnect\n\n{exit_marker}0{exit_marker}\n".encode()]
    )
    connect_mock = MagicMock(side_effect=[first_ws, second_ws])

    monkeypatch.setattr("hermes_plugin_coder.backend.connect", connect_mock)
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=5,
        init_session=False,
    )

    result = env.execute("cat > /tmp/out.txt", stdin_data="hello stdin")

    assert result["returncode"] == 0
    assert result["output"] == "after reconnect\n"
    assert connect_mock.call_count == 2
    sent_payloads = [json.loads(frame.decode("utf-8")) for frame in first_ws.sent]
    assert sent_payloads == [{"data": "hello stdin"}, {"data": "\u0004"}]
    assert second_ws.sent == []
    first_query = parse_qs(urlparse(connect_mock.call_args_list[0].args[0]).query)
    second_query = parse_qs(urlparse(connect_mock.call_args_list[1].args[0]).query)
    assert first_query["reconnect"] == [str(reconnect_id)]
    assert second_query["reconnect"] == [str(reconnect_id)]
    assert second_query["command"] == first_query["command"]


def test_coder_environment_recv_timeout_poll_does_not_fail_silent_command(monkeypatch):
    reconnect_id = uuid.UUID("33333333-4444-5555-6666-777777777777")
    exit_marker = f"__HERMES_EXIT_{reconnect_id}__"
    fake_ws = _FakeWebSocket(
        [
            TimeoutError(),
            f"eventual output\n\n{exit_marker}0{exit_marker}\n".encode(),
        ]
    )
    connect_mock = MagicMock(return_value=fake_ws)

    monkeypatch.setattr("hermes_plugin_coder.backend.connect", connect_mock)
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=5,
        init_session=False,
    )

    result = env.execute("sleep 2 && echo done", timeout=9)

    assert result["returncode"] == 0
    assert result["output"] == "eventual output\n"
    assert 0 < connect_mock.call_args.kwargs["open_timeout"] <= 9
    assert fake_ws.requested[0]["timeout"] <= 1.0
    assert fake_ws.requested[0]["decode"] is False


def test_coder_environment_deadline_interrupts_active_pty_exactly_once(monkeypatch):
    class _TimeoutWebSocket(_FakeWebSocket):
        def __init__(self):
            super().__init__([TimeoutError()])
            self.sent = []
            self.closed = False

        def send(self, message):
            self.sent.append(message)

        def close(self):
            self.closed = True

    fake_ws = _TimeoutWebSocket()
    monkeypatch.setattr(
        "hermes_plugin_coder.backend.connect", MagicMock(return_value=fake_ws)
    )
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )
    monkeypatch.setattr(
        "hermes_plugin_coder.backend._monotonic",
        MagicMock(side_effect=[0.0, 0.0, 0.0, 0.0, 6.0]),
    )
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="shared-dev",
        init_session=False,
    )
    reconnect_interrupt = MagicMock()
    monkeypatch.setattr(env, "_interrupt_reconnected_pty", reconnect_interrupt)

    output, returncode = env._execute_via_pty("sleep 999", login=False, timeout=5)

    assert output == ""
    assert returncode == 1
    assert fake_ws.sent == [CoderEnvironment._stdin_frame("\u0003")]
    reconnect_interrupt.assert_not_called()


def test_coder_environment_uses_one_deadline_across_agent_resolution_and_pty(
    monkeypatch,
):
    reconnect_id = uuid.UUID("33333333-4444-5555-6666-999999999999")
    exit_marker = f"__HERMES_EXIT_{reconnect_id}__"
    clock = [0.0]
    captured = {}

    def resolve_agent(_self, **kwargs):
        captured["deadline"] = kwargs["deadline"]
        clock[0] = 4.0
        return "agent-123"

    connect_mock = MagicMock(
        return_value=_FakeWebSocket([f"done\n{exit_marker}0{exit_marker}\n".encode()])
    )
    monkeypatch.setattr("hermes_plugin_coder.backend._monotonic", lambda: clock[0])
    monkeypatch.setattr("hermes_plugin_coder.backend.connect", connect_mock)
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)
    monkeypatch.setattr(CoderEnvironment, "_resolve_agent_id", resolve_agent)
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="shared-dev",
        init_session=False,
    )

    result = env.execute("echo done", timeout=5)

    assert result == {"output": "done", "returncode": 0}
    assert captured["deadline"] == 5.0
    assert 0 < connect_mock.call_args.kwargs["open_timeout"] <= 1.0


def test_coder_environment_stdin_data_uses_binary_json_frames_and_eof(monkeypatch):
    reconnect_id = uuid.UUID("11111111-2222-3333-4444-555555555555")
    exit_marker = f"__HERMES_EXIT_{reconnect_id}__"

    class _SendingWebSocket(_FakeWebSocket):
        def __init__(self, messages):
            super().__init__(messages)
            self.sent = []

        def send(self, message):
            self.sent.append(message)

    fake_ws = _SendingWebSocket([f"ok\n\n{exit_marker}0{exit_marker}\n".encode()])
    connect_mock = MagicMock(return_value=fake_ws)

    monkeypatch.setattr("hermes_plugin_coder.backend.connect", connect_mock)
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=5,
        init_session=False,
    )

    result = env.execute("cat > /tmp/out.txt", stdin_data="hello stdin")

    assert result["returncode"] == 0
    sent_payloads = [json.loads(frame.decode("utf-8")) for frame in fake_ws.sent]
    assert sent_payloads == [{"data": "hello stdin"}, {"data": "\u0004"}]


def test_coder_environment_returns_nonzero_exit_code_from_pty_marker(monkeypatch):
    reconnect_id = uuid.UUID("87654321-4321-6789-4321-678987654321")
    exit_marker = f"__HERMES_EXIT_{reconnect_id}__"
    fake_ws = _FakeWebSocket(
        [f"failure output\r\n{exit_marker}42{exit_marker}\r\n".encode()]
    )
    connect_mock = MagicMock(return_value=fake_ws)

    monkeypatch.setattr("hermes_plugin_coder.backend.connect", connect_mock)
    monkeypatch.setattr("hermes_plugin_coder.backend.uuid.uuid4", lambda: reconnect_id)
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=5,
        init_session=False,
    )

    result = env.execute("exit 42")

    assert result["returncode"] == 42
    assert result["output"] == "failure output"
    connect_url = connect_mock.call_args.args[0]
    query = parse_qs(urlparse(connect_url).query)
    assert query["reconnect"] == [str(reconnect_id)]
    assert exit_marker in query["command"][0]


def test_coder_environment_missing_exit_marker_returns_backend_error(monkeypatch):
    fake_ws = _FakeWebSocket([b"plain output without marker\n"])
    monkeypatch.setattr(
        "hermes_plugin_coder.backend.connect", MagicMock(return_value=fake_ws)
    )
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=5,
        init_session=False,
    )

    result = env.execute("echo no-marker", timeout=0.01)

    assert result["returncode"] == 1
    assert "plain output without marker" in result["output"]


def test_coder_resolve_agent_id_stops_rest_polling_after_cancel(monkeypatch):
    stopped_workspace = {
        "id": "workspace-123",
        "name": "hermes-20260521-173045-ab12cd",
        "latest_build": {
            "transition": "stop",
            "status": "stopped",
            "resources": [],
        },
    }
    pending_build = {"job": {"status": "running", "completed_at": None}}
    requests_get = MagicMock(
        side_effect=[
            _FakeResponse({"workspaces": [stopped_workspace]}),
            _FakeResponse(stopped_workspace),
            _FakeResponse(pending_build),
        ]
    )
    requests_post = MagicMock(
        return_value=_FakeResponse({"id": "build-123"}, status_code=201)
    )
    cancel_state = {"lock": threading.Lock(), "websocket": None, "cancelled": False}

    def cancel_during_sleep(_seconds):
        with cancel_state["lock"]:
            cancel_state["cancelled"] = True

    monkeypatch.setattr("hermes_plugin_coder.backend.requests.get", requests_get)
    monkeypatch.setattr("hermes_plugin_coder.backend.requests.post", requests_post)
    monkeypatch.setattr("hermes_plugin_coder.backend._sleep", cancel_during_sleep)

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=30,
        workspace_startup_timeout=120,
        init_session=False,
    )

    with pytest.raises(RuntimeError, match="cancelled"):
        env._resolve_agent_id(timeout=30, cancel_state=cancel_state)

    assert requests_get.call_count == 3
    assert requests_post.call_count == 1


def test_coder_workspace_startup_timeout_bounds_agent_ready_wait(monkeypatch):
    current_time = [1000.0]
    sleep_calls = []
    not_ready_workspace = {
        "id": "workspace-123",
        "name": "hermes-20260521-173045-ab12cd",
        "latest_build": {
            "transition": "start",
            "resources": [
                {
                    "agents": [
                        {
                            "id": "agent-123",
                            "status": "starting",
                            "lifecycle_state": "created",
                        }
                    ]
                }
            ],
        },
    }

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        current_time[0] += seconds

    def fake_get(url, **_kwargs):
        if url.endswith("/api/v2/workspaces"):
            return _FakeResponse({"workspaces": [not_ready_workspace]})
        return _FakeResponse(not_ready_workspace)

    monkeypatch.setattr(
        "hermes_plugin_coder.backend.requests.get", MagicMock(side_effect=fake_get)
    )
    monkeypatch.setattr("hermes_plugin_coder.backend.requests.post", MagicMock())
    monkeypatch.setattr(
        "hermes_plugin_coder.backend._monotonic", lambda: current_time[0]
    )
    monkeypatch.setattr("hermes_plugin_coder.backend._sleep", fake_sleep)

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=30,
        workspace_startup_timeout=3,
        init_session=False,
    )

    with pytest.raises(TimeoutError, match="agent startup"):
        env._resolve_agent_id(timeout=30, cancel_state=None)

    assert sum(sleep_calls) <= 3
    assert sleep_calls


def test_coder_process_kill_sends_ctrl_c_to_active_pty(monkeypatch):
    connected = threading.Event()
    closed = threading.Event()

    class _BlockingWebSocket:
        def __init__(self):
            self.sent = []
            self.closed = False

        def __enter__(self):
            connected.set()
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()
            return False

        def recv(self, timeout=None, decode=None):
            closed.wait(timeout=2)
            raise EOFError

        def send(self, message):
            self.sent.append(message)

        def close(self):
            self.closed = True
            closed.set()

    fake_ws = _BlockingWebSocket()
    monkeypatch.setattr(
        "hermes_plugin_coder.backend.connect", MagicMock(return_value=fake_ws)
    )
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=5,
        init_session=False,
    )

    handle = env._run_bash("sleep 999", timeout=5)
    assert connected.wait(timeout=2)

    handle.kill()
    handle.kill()
    handle.wait(timeout=2)

    assert fake_ws.sent == [CoderEnvironment._stdin_frame("\u0003")]
    assert fake_ws.closed is True


def test_coder_process_blocked_active_interrupt_does_not_race_reconnect(monkeypatch):
    connected = threading.Event()
    send_started = threading.Event()
    recv_release = threading.Event()
    send_release = threading.Event()

    class _SlowInterruptWebSocket:
        def __enter__(self):
            connected.set()
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def recv(self, timeout=None, decode=None):
            assert recv_release.wait(timeout=2)
            raise EOFError

        def send(self, _message):
            send_started.set()
            recv_release.set()
            assert send_release.wait(timeout=3)

        def close(self):
            recv_release.set()

    monkeypatch.setattr(
        "hermes_plugin_coder.backend.connect",
        MagicMock(return_value=_SlowInterruptWebSocket()),
    )
    monkeypatch.setattr(
        CoderEnvironment, "_resolve_agent_id", lambda self, **_kwargs: "agent-123"
    )
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=5,
        init_session=False,
    )
    reconnect_interrupt = MagicMock(return_value=True)
    monkeypatch.setattr(env, "_interrupt_reconnected_pty", reconnect_interrupt)
    handle = env._run_bash("sleep 999", timeout=5)
    assert connected.wait(timeout=2)
    kill_thread = threading.Thread(target=handle.kill)
    kill_thread.start()
    assert send_started.wait(timeout=2)

    try:
        handle.wait(timeout=2)
        reconnect_interrupt.assert_not_called()
    finally:
        send_release.set()
        kill_thread.join(timeout=2)

    assert not kill_thread.is_alive()


def test_coder_environment_missing_workspace_raises_without_creating(monkeypatch):
    requests_get = MagicMock(return_value=_FakeResponse({"workspaces": []}))
    requests_post = MagicMock()

    monkeypatch.setattr("hermes_plugin_coder.backend.requests.get", requests_get)
    monkeypatch.setattr("hermes_plugin_coder.backend.requests.post", requests_post)

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="shared-dev",
        timeout=5,
        init_session=False,
    )

    with pytest.raises(
        RuntimeError, match="Coder workspace 'shared-dev' does not exist"
    ):
        env._ensure_workspace()
    requests_post.assert_not_called()


def test_coder_environment_autostarts_existing_stopped_workspace(monkeypatch):
    existing_workspace = {
        "id": "workspace-123",
        "name": "hermes-20260521-173045-ab12cd",
        "latest_build": {
            "transition": "stop",
            "status": "stopped",
            "resources": [],
        },
    }
    started_workspace = {
        "id": "workspace-123",
        "name": "hermes-20260521-173045-ab12cd",
        "latest_build": {
            "transition": "start",
            "resources": [
                {
                    "agents": [
                        {
                            "id": "agent-123",
                            "status": "connected",
                            "lifecycle_state": "ready",
                        }
                    ]
                }
            ],
        },
    }
    requests_get = MagicMock(
        side_effect=[
            _FakeResponse({"workspaces": [existing_workspace]}),
            _FakeResponse(existing_workspace),
            _FakeResponse(
                {"job": {"status": "succeeded", "completed_at": "2026-05-19T10:10:00Z"}}
            ),
            _FakeResponse({"workspaces": [started_workspace]}),
            _FakeResponse(started_workspace),
        ]
    )
    requests_post = MagicMock(
        return_value=_FakeResponse({"id": "build-123"}, status_code=201)
    )

    monkeypatch.setattr("hermes_plugin_coder.backend.requests.get", requests_get)
    monkeypatch.setattr("hermes_plugin_coder.backend.requests.post", requests_post)
    monkeypatch.setattr("hermes_plugin_coder.backend._sleep", lambda _seconds: None)

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        init_session=False,
    )

    assert env._resolve_agent_id() == "agent-123"
    requests_post.assert_called_once_with(
        "https://coder.example/api/v2/workspaces/workspace-123/builds",
        headers={"Coder-Session-Token": "secret-token"},
        json={"transition": "start"},
        timeout=60,
        allow_redirects=False,
    )


def test_resolve_agent_id_waits_for_agent_connected_and_ready_before_returning(
    monkeypatch,
):
    base_workspace = {
        "id": "workspace-123",
        "name": "hermes-20260521-173045-ab12cd",
    }
    not_ready_workspace = {
        **base_workspace,
        "latest_build": {
            "transition": "start",
            "resources": [
                {
                    "agents": [
                        {
                            "id": "agent-123",
                            "status": "starting",
                            "lifecycle_state": "created",
                        }
                    ]
                }
            ],
        },
    }
    ready_workspace = {
        **base_workspace,
        "latest_build": {
            "transition": "start",
            "resources": [
                {
                    "agents": [
                        {
                            "id": "agent-123",
                            "status": "connected",
                            "lifecycle_state": "ready",
                        }
                    ]
                }
            ],
        },
    }

    requests_get = MagicMock(
        side_effect=[
            _FakeResponse({"workspaces": [base_workspace]}),
            _FakeResponse(not_ready_workspace),
            _FakeResponse({"workspaces": [base_workspace]}),
            _FakeResponse(ready_workspace),
        ]
    )

    monkeypatch.setattr("hermes_plugin_coder.backend.requests.get", requests_get)
    monkeypatch.setattr("hermes_plugin_coder.backend.requests.post", MagicMock())
    monkeypatch.setattr("hermes_plugin_coder.backend._sleep", lambda _seconds: None)

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="20260521_180000_ef3456",
        api_key="secret-token",
        workspace_name="hermes-20260521-173045-ab12cd",
        timeout=5,
        init_session=False,
    )

    assert env._resolve_agent_id() == "agent-123"
    assert requests_get.call_count == 4


def test_coder_cleanup_removes_session_snapshot_files(monkeypatch):
    process = MagicMock()
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="shared-dev",
        init_session=False,
    )
    env._workspace_id = "workspace-123"
    env._snapshot_ready = True
    run_bash = MagicMock(return_value=process)
    monkeypatch.setattr(env, "_run_bash", run_bash)

    process.wait.return_value = 0
    env.cleanup()
    env.cleanup()

    command = run_bash.call_args.args[0]
    assert "rm -f --" in command
    assert env._snapshot_path in command
    assert env._cwd_file in command
    assert env._snapshot_path + ".tmp." in command
    run_bash.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)
    assert env._snapshot_ready is False


def test_coder_cleanup_retries_after_failure_then_becomes_idempotent(monkeypatch):
    failed = MagicMock()
    failed.wait.return_value = 1
    succeeded = MagicMock()
    succeeded.wait.return_value = 0
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="shared-dev",
        init_session=False,
    )
    env._workspace_id = "workspace-123"
    env._snapshot_ready = True
    run_bash = MagicMock(side_effect=[failed, succeeded])
    monkeypatch.setattr(env, "_run_bash", run_bash)

    env.cleanup()
    assert env._cleanup_complete is False
    assert env._snapshot_ready is True

    env.cleanup()
    env.cleanup()

    assert run_bash.call_count == 2
    assert env._cleanup_complete is True
    assert env._snapshot_ready is False


def test_coder_cleanup_serializes_concurrent_retries(monkeypatch):
    retry_started = threading.Event()
    release_retry = threading.Event()
    second_entered = threading.Event()
    second_finished = threading.Event()

    failed = MagicMock()
    failed.wait.return_value = 1
    retry = MagicMock()

    def wait_for_release(timeout):
        assert timeout == 5
        retry_started.set()
        assert release_retry.wait(timeout=2)
        return 0

    retry.wait.side_effect = wait_for_release
    unexpected_overlap = MagicMock()
    unexpected_overlap.wait.return_value = 0

    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="shared-dev",
        init_session=False,
    )
    env._workspace_id = "workspace-123"
    env._snapshot_ready = True
    run_bash = MagicMock(side_effect=[failed, retry, unexpected_overlap])
    monkeypatch.setattr(env, "_run_bash", run_bash)

    env.cleanup()
    first_retry = threading.Thread(target=env.cleanup)

    def run_second_retry():
        second_entered.set()
        env.cleanup()
        second_finished.set()

    second_retry = threading.Thread(target=run_second_retry)
    first_retry.start()
    assert retry_started.wait(timeout=2)
    second_retry.start()
    assert second_entered.wait(timeout=2)

    try:
        assert not second_finished.wait(timeout=0.1)
    finally:
        release_retry.set()
        first_retry.join(timeout=2)
        second_retry.join(timeout=2)

    assert not first_retry.is_alive()
    assert not second_retry.is_alive()
    assert run_bash.call_count == 2
    assert env._cleanup_complete is True


@pytest.mark.parametrize(
    ("wait_failure", "expected_kills"),
    [
        (None, 1),
        (TimeoutError("cleanup timed out"), 1),
        (RuntimeError("cleanup failed"), 0),
    ],
)
def test_coder_cleanup_timeout_and_exceptions_remain_retryable(
    monkeypatch, wait_failure, expected_kills
):
    failed = MagicMock()
    if isinstance(wait_failure, BaseException):
        failed.wait.side_effect = wait_failure
    else:
        failed.wait.return_value = wait_failure
    succeeded = MagicMock()
    succeeded.wait.return_value = 0
    env = CoderEnvironment(
        base_url="https://coder.example",
        task_id="task-coder",
        api_key="secret-token",
        workspace_name="shared-dev",
        init_session=False,
    )
    env._workspace_id = "workspace-123"
    env._snapshot_ready = True
    run_bash = MagicMock(side_effect=[failed, succeeded])
    monkeypatch.setattr(env, "_run_bash", run_bash)

    env.cleanup()

    assert env._cleanup_complete is False
    assert env._snapshot_ready is True
    assert failed.kill.call_count == expected_kills

    env.cleanup()

    assert run_bash.call_count == 2
    assert env._cleanup_complete is True
    assert env._snapshot_ready is False
