"""The Ray dashboard is published by its own opt-in Compose overlay only.

It has no authentication and its job-submission API runs arbitrary code, so the
base file and the other Compose files must neither publish it nor set
`RAY_DASHBOARD_HOST`, and the overlay that does publish it binds it to the host's
loopback.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_DIR = REPO_ROOT / "infra/compose"
DASHBOARD_OVERLAY = "ray-dashboard.docker-compose.yaml"
DASHBOARD_PORT = 8265
API_SERVICES = ("openrag", "openrag-cpu")

# `${VAR:-default}` or `${VAR-default}`, innermost first.
_INTERPOLATION_WITH_DEFAULT = re.compile(r"\$\{\w+:?-([^${}]*)\}")


class _ComposeLoader(yaml.SafeLoader):
    """SafeLoader that accepts Compose's `!override` and `!reset` tags."""


def _construct_override(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_ComposeLoader.add_constructor("!override", _construct_override)
_ComposeLoader.add_constructor("!reset", lambda loader, node: None)


def _services(path: Path) -> dict:
    services: dict = {}
    for document in yaml.load_all(path.read_text(encoding="utf-8"), Loader=_ComposeLoader):
        if isinstance(document, dict):
            services.update(document.get("services") or {})
    return services


def _resolve_defaults(value: str) -> str:
    while True:
        resolved = _INTERPOLATION_WITH_DEFAULT.sub(r"\1", value)
        if resolved == value:
            return value
        value = resolved


def _container_ports(port: object) -> range:
    """The container ports a `ports:` entry publishes, in short or long syntax."""
    if isinstance(port, dict):
        target = _resolve_defaults(str(port["target"]))
    else:
        without_protocol = _resolve_defaults(str(port)).split("/")[0]
        target = without_protocol.rsplit(":", 1)[-1]
    if "$" in target:
        pytest.fail(f"cannot tell which container port {port!r} publishes: its variable has no default")
    first, _, last = target.partition("-")
    return range(int(first), int(last or first) + 1)


def _dashboard_ports(services: dict) -> dict[str, list[object]]:
    found: dict[str, list[object]] = {}
    for name, service in services.items():
        ports = [port for port in (service or {}).get("ports") or [] if DASHBOARD_PORT in _container_ports(port)]
        if ports:
            found[name] = ports
    return found


def _environment_keys(service: dict) -> set[str]:
    environment = service.get("environment") or {}
    if isinstance(environment, dict):
        return set(environment)
    return {entry.split("=", 1)[0] for entry in environment}


def _compose_files() -> list[Path]:
    """Every YAML file in the repo that Compose can merge in, except the dashboard overlay.

    That is the files under infra/compose (the base file, the overlays and the
    `include:` defaults such as milvus/) and the `include:` targets under extern/.
    The extern/chainlit-datalayer submodule is not checked out in CI, so it is not read.
    """
    files = [*COMPOSE_DIR.rglob("*.yaml"), *COMPOSE_DIR.rglob("*.yml")]
    files += [*(REPO_ROOT / "extern").glob("*.yaml"), *(REPO_ROOT / "extern/reranker").glob("*.yaml")]
    return sorted(path for path in files if path.name != DASHBOARD_OVERLAY)


@pytest.mark.parametrize(
    ("port", "published"),
    [
        ("127.0.0.1:8265:8265", True),
        ("[::1]:8265:8265", True),
        ("0.0.0.0:8265:8265/tcp", True),
        ("8265", True),
        (8265, True),
        ("8260-8270:8260-8270", True),
        ("127.0.0.1:${RAY_DASHBOARD_PORT:-8265}:8265", True),
        ("${HOST_PORT}:${INNER:-${OUTER:-8265}}", True),
        ({"target": 8265, "published": "8265", "host_ip": "0.0.0.0"}, True),
        ({"target": "${PORT:-8265}"}, True),
        ("8265:8080", False),
        ("${APP_PORT:-8080}:${APP_iPORT:-8080}", False),
    ],
)
def test_the_port_matcher(port, published):
    assert bool(_dashboard_ports({"svc": {"ports": [port]}})) is published


@pytest.mark.parametrize("path", _compose_files(), ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_no_other_compose_file_publishes_the_dashboard(path):
    services = _services(path)
    assert _dashboard_ports(services) == {}
    assert [
        name for name, service in services.items() if "RAY_DASHBOARD_HOST" in _environment_keys(service or {})
    ] == []


def test_the_dashboard_overlay_publishes_it_on_loopback_for_both_api_services():
    base = _services(COMPOSE_DIR / "docker-compose.yaml")
    overlay = _services(COMPOSE_DIR / DASHBOARD_OVERLAY)

    assert set(overlay) == set(API_SERVICES)
    assert all(service in base for service in API_SERVICES)
    for service in API_SERVICES:
        assert overlay[service]["ports"] == [f"127.0.0.1:${{RAY_DASHBOARD_PORT:-{DASHBOARD_PORT}}}:{DASHBOARD_PORT}"]
        # Bound to the container's loopback, the dashboard is out of reach of a
        # published port.
        assert overlay[service]["environment"] == {"RAY_DASHBOARD_HOST": "0.0.0.0"}
