"""The Ray dashboard is published by its own opt-in Compose overlay only.

It has no authentication and its job-submission API runs arbitrary code, so the
base file and the other overlays must not publish it, and the overlay that does
binds it to the host's loopback.
"""

from pathlib import Path

import yaml

COMPOSE_DIR = Path(__file__).resolve().parents[3] / "infra/compose"
DASHBOARD_OVERLAY = "ray-dashboard.docker-compose.yaml"
DASHBOARD_PORT = "8265"
API_SERVICES = ("openrag", "openrag-cpu")


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that accepts Compose's `!override` and `!reset` tags."""


_ComposeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))
_ComposeLoader.add_constructor("!reset", lambda loader, node: None)


def _services(name: str) -> dict:
    return yaml.load((COMPOSE_DIR / name).read_text(encoding="utf-8"), Loader=_ComposeLoader).get("services") or {}


def _dashboard_ports(services: dict) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for name, service in services.items():
        ports = [str(port) for port in (service or {}).get("ports") or [] if str(port).endswith(f":{DASHBOARD_PORT}")]
        if ports:
            found[name] = ports
    return found


def test_no_other_compose_file_publishes_the_dashboard():
    files = ["docker-compose.yaml", *sorted(p.name for p in COMPOSE_DIR.glob("*.docker-compose.yaml"))]
    files.remove(DASHBOARD_OVERLAY)
    for name in files:
        assert _dashboard_ports(_services(name)) == {}, name


def test_the_dashboard_overlay_publishes_it_on_loopback_for_both_api_services():
    base = _services("docker-compose.yaml")
    overlay = _services(DASHBOARD_OVERLAY)

    assert set(overlay) == set(API_SERVICES)
    assert all(service in base for service in API_SERVICES)
    for service in API_SERVICES:
        assert overlay[service]["ports"] == [f"127.0.0.1:${{RAY_DASHBOARD_PORT:-{DASHBOARD_PORT}}}:{DASHBOARD_PORT}"]
        # Bound to the container's loopback, the dashboard is out of reach of a
        # published port.
        assert overlay[service]["environment"] == {"RAY_DASHBOARD_HOST": "0.0.0.0"}
