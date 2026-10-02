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
    assert not nginx["image"].endswith(":latest")
    assert "127.0.0.1:81:81" in nginx["ports"]


def test_proxy_overlay_rebinds_every_app_port_to_loopback():
    base = _load("docker-compose.yaml")["services"]
    overlay = _load("proxy.docker-compose.yaml")["services"]

    for name in ("openrag", "openrag-cpu", "admin-ui"):
        ports = overlay[name]["ports"]
        # A plain list would be merged with the base one, publishing both bindings.
        assert isinstance(ports, _Override), name
        assert all(p.startswith("127.0.0.1:") for p in ports), name
        # Same published ports as the base, only the host address differs.
        assert [p.removeprefix("127.0.0.1:") for p in ports] == [
            p.removeprefix("127.0.0.1:") for p in base[name]["ports"]
        ], name
