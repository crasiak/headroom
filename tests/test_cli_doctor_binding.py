"""Doctor must stay within the process-local Ledger binding."""

import json

import pytest
from click.testing import CliRunner

import headroom.cli.doctor as doctor_mod
from headroom.cli.main import main


def binding(harness="codex", mode="isolated", endpoint="http://127.0.0.1:43219"):
    return json.dumps({"schema": 1, "harness": harness, "mode": mode, "endpoint": endpoint})


@pytest.fixture
def probes(monkeypatch):
    urls = []

    def probe(url, **kwargs):
        urls.append(url)
        return {"service": "headroom-proxy", "alive": True, "version": "0.26.0"}

    monkeypatch.setattr(doctor_mod, "probe_json", probe)
    monkeypatch.setattr(doctor_mod, "get_version", lambda: "0.26.0")

    def forbidden(*args, **kwargs):
        raise AssertionError("persistent/inactive configuration read")

    for name in [
        "claude_settings_path",
        "codex_config_path",
        "savings_path",
        "list_manifests",
        "claude_desktop_config_dir",
    ]:
        monkeypatch.setattr(doctor_mod, name, forbidden)
    return urls


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["isolated", "legacy-shared"])
@pytest.mark.parametrize("port", [43219, 49187])
def test_binding_scope_and_precedence(probes, harness, mode, port):
    endpoint = f"http://127.0.0.1:{port}"
    result = CliRunner().invoke(
        main,
        ["doctor", "--json"],
        env={
            "LEDGER_HEADROOM_BINDING": binding(harness, mode, endpoint),
            "HEADROOM_PORT": "invalid",
        },
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["port"] == port
    assert data["endpoint_source"] == "ledger-binding"
    assert data["binding"]["harness"] == harness
    assert probes == [endpoint + "/livez"]
    assert not any(c["status"] == "fail" for c in data["checks"])


@pytest.mark.parametrize("flag", ["--port", "-p"])
def test_explicit_override_keeps_active_binding(probes, flag):
    result = CliRunner().invoke(
        main,
        ["doctor", flag, "41234", "--json"],
        env={"LEDGER_HEADROOM_BINDING": binding(), "HEADROOM_PORT": "9020"},
    )
    data = json.loads(result.output)
    assert data["port"] == 41234
    assert data["endpoint_source"] == "explicit-port"
    assert data["binding"]["endpoint"].endswith(":43219")
    assert result.exit_code == 1
    assert probes == ["http://127.0.0.1:41234/livez"]


@pytest.mark.parametrize(
    "raw",
    [
        "secret-not-json",
        "",
        "[]",
        binding(endpoint="http://evil.example:1234"),
        binding(endpoint="http://user:secret@127.0.0.1:43219"),
        binding(endpoint="http://127.0.0.1:43219/path"),
        binding(endpoint="http://127.0.0.1:43219?secret=1"),
        binding(endpoint="http://127.0.0.1:43219#x"),
        binding(endpoint="http://localhost:43219"),
        binding(harness="unknown"),
        binding(mode="unknown"),
        binding().replace('"schema": 1', '"schema": 2'),
        "x" * 4097,
    ],
)
def test_bad_metadata_never_falls_back_or_leaks(probes, raw):
    result = CliRunner().invoke(main, ["doctor", "--json"], env={"LEDGER_HEADROOM_BINDING": raw})
    assert result.exit_code == 2
    data = json.loads(result.output)
    assert data["binding"] is None
    assert probes == []
    assert "secret" not in result.output


def test_explicit_probe_with_invalid_binding(probes):
    result = CliRunner().invoke(
        main, ["doctor", "-p", "41234", "--json"], env={"LEDGER_HEADROOM_BINDING": "secret-invalid"}
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["binding"] is None
    assert probes == ["http://127.0.0.1:41234/livez"]


def test_unavailable_binding(monkeypatch, probes):
    monkeypatch.setattr(doctor_mod, "probe_json", lambda *a, **kw: None)
    result = CliRunner().invoke(
        main, ["doctor", "--json"], env={"LEDGER_HEADROOM_BINDING": binding()}
    )
    assert result.exit_code == 2
    assert "wrap" not in result.output


def test_binding_probe_does_not_follow_redirects(monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    from headroom.install.health import probe_json

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/provider-must-not-be-called")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert (
            probe_json(f"http://127.0.0.1:{server.server_port}/livez", allow_redirects=False)
            is None
        )
        assert requests == ["/livez"]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_active_claude_route_mismatch_is_value_free(probes):
    result = CliRunner().invoke(
        main,
        ["doctor", "--json"],
        env={
            "LEDGER_HEADROOM_BINDING": binding("claude"),
            "ANTHROPIC_BASE_URL": "https://secret-elsewhere.example",
        },
    )
    assert result.exit_code == 2
    assert "secret-elsewhere" not in result.output


def test_legacy_proxy_does_not_receive_child_binding(monkeypatch, tmp_path):
    from headroom.cli import wrap
    from tests.test_cli_proxy_env import _FakeProxyProcess

    captured = {}
    monkeypatch.setenv("LEDGER_HEADROOM_BINDING", binding(mode="legacy-shared"))
    monkeypatch.setattr(wrap, "_get_log_path", lambda port=None: tmp_path / "proxy.log")
    monkeypatch.setattr(wrap, "_get_proxy_stdio_log_path", lambda port=None: tmp_path / "stdio.log")
    monkeypatch.setattr(wrap, "_check_proxy", lambda port: True)
    monkeypatch.setattr(wrap.time, "sleep", lambda seconds: None)

    def popen(*args, **kwargs):
        captured.update(kwargs["env"])
        return _FakeProxyProcess()

    monkeypatch.setattr(wrap.subprocess, "Popen", popen)
    wrap._start_proxy(43219, agent_type="codex")
    assert "LEDGER_HEADROOM_BINDING" not in captured


def test_legacy_port_fallback_updates_diagnostic_binding(monkeypatch):
    from types import SimpleNamespace

    from headroom.cli import wrap

    captured = {}
    monkeypatch.setattr(wrap, "_make_cleanup", lambda *a: lambda: None)
    monkeypatch.setattr(wrap.signal, "signal", lambda *a: None)
    monkeypatch.setattr(wrap, "_ensure_proxy", lambda *a, **kw: (None, 49187))
    for name in [
        "_register_proxy_client",
        "_unregister_proxy_client",
        "_push_runtime_env",
        "_print_telemetry_notice",
    ]:
        monkeypatch.setattr(wrap, name, lambda *a: None)
    monkeypatch.setattr(wrap, "_configure_quiet_cli_env", lambda env: [])

    def run(*a, **kw):
        captured.update(kw["env"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(wrap.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        wrap._launch_tool(
            "fake-native",
            (),
            {"LEDGER_HEADROOM_BINDING": binding(mode="legacy-shared")},
            43219,
            False,
            "CODEX",
            [],
        )
    assert exc.value.code == 0
    assert json.loads(captured["LEDGER_HEADROOM_BINDING"])["endpoint"] == "http://127.0.0.1:49187"
