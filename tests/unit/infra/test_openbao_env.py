"""Tests for ``infra/scripts/openbao_env.py`` — the deploy-time helper that
renders a compose ``.env`` from an OpenBao / Vault KV v2 secret.

The HTTP side is exercised against a tiny in-process server so the URL
layout (``/v1/<mount>/data/<path>``, ``/v1/auth/<mount>/login``) and the
headers (``X-Vault-Token``, ``X-Vault-Namespace``) are checked for real, not
mocked away.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra" / "scripts" / "openbao_env.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("openbao_env", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def openbao_env():
    return _load_module()


# ---------------------------------------------------------------------------
# Pure rendering helpers
# ---------------------------------------------------------------------------


def test_render_env_without_base_emits_sorted_assignments(openbao_env) -> None:
    text = openbao_env.render_env({"B_KEY": "2", "A_KEY": "1"}, None, source="bao secret/openrag")

    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert lines == ["A_KEY=1", "B_KEY=2"]
    assert text.endswith("\n")


def test_render_env_replaces_existing_assignments_in_place(openbao_env) -> None:
    base = "\n".join(
        [
            "# LLM",
            "BASE_URL=http://llm:8000/v1",
            "API_KEY=old-key",
            "export AUTH_TOKEN = old-token",
            "# POSTGRES_USER=root",
            "POSTGRES_PASSWORD=postgres",
            "",
        ]
    )
    secrets = {"API_KEY": "new-key", "AUTH_TOKEN": "or-abc", "POSTGRES_PASSWORD": "pg-new", "NEW_ONE": "x"}

    text = openbao_env.render_env(secrets, base, source="bao secret/openrag")
    lines = text.splitlines()

    # Non-secret lines and comments survive untouched, in order.
    assert lines[0] == "# LLM"
    assert lines[1] == "BASE_URL=http://llm:8000/v1"
    # Assignments are rewritten in place, including `export`/spaced forms.
    assert lines[2] == "API_KEY=new-key"
    assert lines[3] == "AUTH_TOKEN=or-abc"
    # A commented-out assignment is not an assignment: left alone.
    assert lines[4] == "# POSTGRES_USER=root"
    assert lines[5] == "POSTGRES_PASSWORD=pg-new"
    # Keys absent from the base file are appended under a marker header.
    assert any(line.startswith("# --- ") and "openbao_env.py" in line for line in lines)
    assert lines[-1] == "NEW_ONE=x"
    # Each secret is written exactly once.
    assert sum(line.startswith("API_KEY=") for line in lines) == 1


def test_format_value_quotes_only_when_needed(openbao_env) -> None:
    assert openbao_env.format_value("K", "plain-token_1.0:x/y@z+=,") == "plain-token_1.0:x/y@z+=,"
    assert openbao_env.format_value("K", "") == "''"
    assert openbao_env.format_value("K", "has space") == "'has space'"
    assert openbao_env.format_value("K", "hash#tag") == "'hash#tag'"
    # `$` must not be interpolated by compose/uv: single quotes keep it literal.
    assert openbao_env.format_value("K", "pa$$word") == "'pa$$word'"


def test_format_value_refuses_values_no_dotenv_parser_agrees_on(openbao_env) -> None:
    with pytest.raises(openbao_env.OpenBaoError, match="K"):
        openbao_env.format_value("K", "it's")
    with pytest.raises(openbao_env.OpenBaoError, match="K"):
        openbao_env.format_value("K", "line1\nline2")


def test_validate_key_rejects_names_that_are_not_env_vars(openbao_env) -> None:
    openbao_env.validate_key("AUTH_TOKEN")
    openbao_env.validate_key("_x1")
    for bad in ("api-key", "1KEY", "with space", ""):
        with pytest.raises(openbao_env.OpenBaoError):
            openbao_env.validate_key(bad)


def test_split_kv_path_separates_mount_from_secret_path(openbao_env) -> None:
    assert openbao_env.split_kv_path("secret/openrag/staging") == ("secret", "openrag/staging")
    assert openbao_env.split_kv_path("/kv/app/") == ("kv", "app")
    with pytest.raises(openbao_env.OpenBaoError):
        openbao_env.split_kv_path("secret")


# ---------------------------------------------------------------------------
# HTTP client against an in-process fake OpenBao
# ---------------------------------------------------------------------------


class _FakeBao(BaseHTTPRequestHandler):
    """Minimal KV v2 + AppRole endpoints. Records what it saw for assertions."""

    calls: list[dict] = []
    secret_data = {"AUTH_TOKEN": "or-123", "API_KEY": "sk-abc", "IGNORED": "z"}

    def log_message(self, *_args) -> None:  # keep pytest output clean
        pass

    def _record(self, body: dict | None) -> None:
        type(self).calls.append(
            {
                "method": self.command,
                "path": self.path,
                "token": self.headers.get("X-Vault-Token"),
                "namespace": self.headers.get("X-Vault-Namespace"),
                "body": body,
            }
        )

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self._record(body)
        if self.path == "/v1/auth/approle/login" and body.get("role_id") == "rid" and body.get("secret_id") == "sid":
            self._send(200, {"auth": {"client_token": "hvs.approle-token"}})
        else:
            self._send(400, {"errors": ["invalid role or secret ID"]})

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._record(None)
        if self.headers.get("X-Vault-Token") not in ("hvs.approle-token", "hvs.static"):
            self._send(403, {"errors": ["permission denied"]})
        elif self.path == "/v1/secret/data/openrag/staging":
            self._send(200, {"data": {"data": dict(self.secret_data), "metadata": {"version": 3}}})
        else:
            self._send(404, {"errors": []})


@pytest.fixture
def fake_bao():
    _FakeBao.calls = []
    server = HTTPServer(("127.0.0.1", 0), _FakeBao)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_login_approle_posts_credentials_and_returns_client_token(openbao_env, fake_bao) -> None:
    token = openbao_env.login_approle(fake_bao, "rid", "sid", namespace="team-openrag")

    assert token == "hvs.approle-token"
    (call,) = _FakeBao.calls
    assert call["method"] == "POST"
    assert call["path"] == "/v1/auth/approle/login"
    assert call["namespace"] == "team-openrag"
    assert call["body"] == {"role_id": "rid", "secret_id": "sid"}


def test_login_approle_reports_http_errors_without_the_secret_id(openbao_env, fake_bao) -> None:
    with pytest.raises(openbao_env.OpenBaoError) as excinfo:
        openbao_env.login_approle(fake_bao, "rid", "wrong-secret-id")

    message = str(excinfo.value)
    assert "400" in message
    assert "invalid role or secret ID" in message
    assert "wrong-secret-id" not in message


def test_read_kv2_hits_the_data_endpoint_with_token_and_namespace(openbao_env, fake_bao) -> None:
    data = openbao_env.read_kv2(fake_bao, "hvs.static", "secret/openrag/staging", namespace="team-openrag")

    assert data == _FakeBao.secret_data
    (call,) = _FakeBao.calls
    assert call["path"] == "/v1/secret/data/openrag/staging"
    assert call["token"] == "hvs.static"
    assert call["namespace"] == "team-openrag"


def test_read_kv2_surfaces_permission_denied(openbao_env, fake_bao) -> None:
    with pytest.raises(openbao_env.OpenBaoError, match="403"):
        openbao_env.read_kv2(fake_bao, "hvs.bad", "secret/openrag/staging")


# ---------------------------------------------------------------------------
# CLI end to end
# ---------------------------------------------------------------------------


def test_main_logs_in_with_approle_and_writes_a_private_env_file(
    openbao_env, fake_bao, tmp_path, monkeypatch, capsys
) -> None:
    for var in ("BAO_TOKEN", "VAULT_TOKEN", "VAULT_ADDR", "VAULT_NAMESPACE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BAO_ADDR", fake_bao)
    monkeypatch.setenv("BAO_NAMESPACE", "team-openrag")
    monkeypatch.setenv("BAO_ROLE_ID", "rid")
    monkeypatch.setenv("BAO_SECRET_ID", "sid")
    base = tmp_path / ".env.example"
    base.write_text("BASE_URL=http://llm\nAUTH_TOKEN=or-openrag-1234\n", encoding="utf-8")
    out = tmp_path / ".env"

    rc = openbao_env.main(
        [
            "--path",
            "secret/openrag/staging",
            "--base",
            str(base),
            "--out",
            str(out),
            "--only",
            "AUTH_TOKEN,API_KEY",
        ]
    )

    assert rc == 0
    assert out.read_text(encoding="utf-8").splitlines()[:2] == ["BASE_URL=http://llm", "AUTH_TOKEN=or-123"]
    assert "API_KEY=sk-abc" in out.read_text(encoding="utf-8")
    assert "IGNORED" not in out.read_text(encoding="utf-8")
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    # Login happened first, then the read used the AppRole token.
    assert [c["path"] for c in _FakeBao.calls] == ["/v1/auth/approle/login", "/v1/secret/data/openrag/staging"]
    assert _FakeBao.calls[1]["token"] == "hvs.approle-token"
    # Nothing secret reaches the terminal: key names only.
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "AUTH_TOKEN" in captured.err
    assert "or-123" not in captured.err
    assert "sk-abc" not in captured.err


def test_main_fails_when_a_requested_key_is_missing_from_the_secret(openbao_env, fake_bao, monkeypatch, capsys) -> None:
    monkeypatch.setenv("BAO_ADDR", fake_bao)
    monkeypatch.setenv("BAO_TOKEN", "hvs.static")

    rc = openbao_env.main(["--path", "secret/openrag/staging", "--only", "AUTH_TOKEN,MISSING_KEY"])

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "MISSING_KEY" in captured.err


def test_main_requires_an_address_and_some_credentials(openbao_env, monkeypatch, capsys) -> None:
    for var in ("BAO_ADDR", "VAULT_ADDR", "BAO_TOKEN", "VAULT_TOKEN", "BAO_ROLE_ID", "BAO_SECRET_ID"):
        monkeypatch.delenv(var, raising=False)

    assert openbao_env.main(["--path", "secret/openrag/staging"]) == 1
    assert "BAO_ADDR" in capsys.readouterr().err

    monkeypatch.setenv("BAO_ADDR", "http://127.0.0.1:1")
    assert openbao_env.main(["--path", "secret/openrag/staging"]) == 1
    assert "BAO_ROLE_ID" in capsys.readouterr().err


def test_main_prints_to_stdout_when_no_output_file_is_given(openbao_env, fake_bao, monkeypatch, capsys) -> None:
    monkeypatch.setenv("BAO_ADDR", fake_bao)
    monkeypatch.setenv("BAO_TOKEN", "hvs.static")

    rc = openbao_env.main(["--path", "secret/openrag/staging"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "API_KEY=sk-abc" in out
    assert "AUTH_TOKEN=or-123" in out
