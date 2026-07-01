from io import StringIO

from core.utils.banner import print_startup_banner


def test_startup_banner_mentions_super_admin_mode_when_enabled(monkeypatch):
    monkeypatch.setenv("OPENRAG_BANNER", "true")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("APP_PORT", "8077")
    monkeypatch.setenv("SUPER_ADMIN_MODE", "true")
    stream = StringIO()

    print_startup_banner("test-version", stream=stream)

    output = stream.getvalue()
    assert "OpenRAG vtest-version" in output
    assert "API docs: http://localhost:8077/docs" in output
    assert "Super Admin Mode: enabled" in output
    assert "Status: ready" in output


def test_startup_banner_mentions_super_admin_mode_with_color_enabled(monkeypatch):
    monkeypatch.setenv("OPENRAG_BANNER", "true")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setenv("SUPER_ADMIN_MODE", "true")
    stream = StringIO()

    print_startup_banner("test-version", stream=stream)

    output = stream.getvalue()
    assert "Super Admin Mode" in output
    assert "enabled" in output
    assert "Status" in output


def test_startup_banner_omits_super_admin_mode_when_disabled(monkeypatch):
    monkeypatch.setenv("OPENRAG_BANNER", "true")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("SUPER_ADMIN_MODE", "false")
    stream = StringIO()

    print_startup_banner("test-version", stream=stream)

    assert "Super Admin Mode" not in stream.getvalue()
