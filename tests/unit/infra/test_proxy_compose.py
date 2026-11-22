from pathlib import Path

import yaml

COMPOSE_DIR = Path(__file__).resolve().parents[3] / "infra/compose"


class _OverrideLoader(yaml.SafeLoader):
    """SafeLoader that accepts Compose's `!override` tag (and records it)."""


class _Override(list):
    pass


_OverrideLoader.add_constructor("!override", lambda loader, node: _Override(loader.construct_sequence(node)))


def _load(name: str) -> dict:
    return yaml.load((COMPOSE_DIR / name).read_text(encoding="utf-8"), Loader=_OverrideLoader)


def test_base_compose_has_no_proxy_service():
    # The front door is opt-in; the base stack must not bind 80/443.
    assert "nginx" not in _load("docker-compose.yaml")["services"]


def test_proxy_service_has_no_profile():
    # The API declares `profiles: [""]`; any explicit --profile would deselect
    # it and leave the proxy with no backend.
    nginx = _load("proxy.docker-compose.yaml")["services"]["nginx"]

    assert "profiles" not in nginx
    # An explicit version: neither `:latest` nor an untagged (implicit latest) image.
    _, _, tag = nginx["image"].rpartition("/")[2].partition(":")
    assert tag and tag != "latest", nginx["image"]
    assert "127.0.0.1:81:81" in nginx["ports"]


def test_proxy_overlay_rebinds_every_app_port_to_loopback():
    base = _load("docker-compose.yaml")["services"]
    overlay = _load("proxy.docker-compose.yaml")["services"]

    published = [name for name, service in base.items() if service.get("ports")]
    assert published, "the base file publishes no ports"

    for name in published:
        assert name in overlay, f"{name} publishes ports but the overlay doesn't rebind them"
        ports = overlay[name]["ports"]
        # A plain list would be merged with the base one, publishing both bindings.
        assert isinstance(ports, _Override), name
        assert all(p.startswith("127.0.0.1:") for p in ports), name
        # Same published ports as the base, only the host address differs.
        assert [p.removeprefix("127.0.0.1:") for p in ports] == [
            p.removeprefix("127.0.0.1:") for p in base[name]["ports"]
        ], name


def test_proxy_overlay_sets_tls_sessions_at_http_level():
    overlay = _load("proxy.docker-compose.yaml")
    nginx = overlay["services"]["nginx"]

    # NPM includes /etc/nginx/conf.d/*.conf inside `http {}`.
    (entry,) = [c for c in nginx["configs"] if c["target"].startswith("/etc/nginx/conf.d/")]
    assert entry["target"].endswith(".conf")
    # Not a bind mount: NPM's startup chown aborts on a read-only one.
    assert not any(entry["target"] in volume for volume in nginx["volumes"])

    directives = overlay["configs"][entry["source"]]["content"].splitlines()
    assert "ssl_session_tickets off;" in directives
    # Without an http-level cache, resumption stops once tickets are off: under
    # SNI nginx resumes from the default server, which NPM gives no cache.
    # NPM's conf.d/include/ssl-cache.conf declares the same zone, and nginx
    # refuses to start when one zone has two sizes. Bumping NPM fails here on
    # purpose: read that file in the new image, then update both pins.
    assert nginx["image"] == "jc21/nginx-proxy-manager:2.16.0"
    assert "ssl_session_cache shared:SSL:50m;" in directives
