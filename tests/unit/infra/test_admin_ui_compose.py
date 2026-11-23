import ipaddress
import json
import re
import shutil
import subprocess
from os import environ
from pathlib import Path

import pytest
import yaml

COMPOSE_DIR = Path(__file__).resolve().parents[3] / "infra/compose"


@pytest.mark.parametrize(
    "scenario",
    [
        "default",
        "custom-subnet",
        "explicit-allowlist",
        "shared-override",
        "empty-allowlist",
        "tls-overlay",
    ],
)
def test_compose_proxy_trust(tmp_path, scenario):
    if (
        shutil.which("docker") is None
        or subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode
    ):
        pytest.skip("Docker Compose is required to render the configuration")

    project_values = (
        "POSTGRES_PASSWORD=render-test-only\nMINIO_SECRET_KEY=render-test-only\nMINIO_ACCESS_KEY=render-test-only\n"
    )
    override = "127.0.0.1,192.0.2.10" if scenario == "explicit-allowlist" else None
    shared_override = "10.8.8.8" if scenario == "shared-override" else override
    if override is not None:
        project_values += f"UVICORN_FORWARDED_ALLOW_IPS={override}\n"
    elif scenario == "empty-allowlist":
        project_values += "UVICORN_FORWARDED_ALLOW_IPS=\n"
    shared_values = project_values
    if scenario == "shared-override":
        project_values += "UVICORN_FORWARDED_ALLOW_IPS=10.9.9.9\n"
        shared_values += f"UVICORN_FORWARDED_ALLOW_IPS={shared_override}\n"
    project_env = tmp_path / ".env"
    project_env.write_text(project_values, encoding="utf-8")
    shared_env = tmp_path / "shared.env"
    shared_env.write_text(shared_values, encoding="utf-8")
    render_env = {"SHARED_ENV": str(shared_env)}
    if scenario == "custom-subnet":
        render_env.update(
            OPENRAG_COMPOSE_SUBNET="172.31.252.0/24",
            OPENRAG_COMPOSE_DYNAMIC_RANGE="172.31.252.0/25",
            OPENRAG_ADMIN_UI_PROXY_IP="172.31.252.200",
        )

    # Avoid inheriting an operator override for any of the inputs under test.
    env = {key: value for key, value in environ.items() if key in {"HOME", "PATH"}}
    env.update(render_env)
    compose_files = ["-f", "docker-compose.yaml"]
    if scenario == "tls-overlay":
        compose_files.extend(("-f", "proxy.docker-compose.yaml"))
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(project_env),
            *compose_files,
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ],
        cwd=COMPOSE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(result.stdout)
    proxy_ip = config["services"]["admin-ui"]["networks"]["default"]["ipv4_address"]
    ipam = config["networks"]["default"]["ipam"]["config"][0]

    assert ipaddress.ip_address(proxy_ip) in ipaddress.ip_network(ipam["subnet"])
    assert ipaddress.ip_address(proxy_ip) not in ipaddress.ip_network(ipam["ip_range"])
    if scenario == "tls-overlay":
        npm_proxy_ip = config["services"]["nginx"]["networks"]["default"]["ipv4_address"]
        assert npm_proxy_ip != proxy_ip
        assert ipaddress.ip_address(npm_proxy_ip) in ipaddress.ip_network(ipam["subnet"])
        assert ipaddress.ip_address(npm_proxy_ip) not in ipaddress.ip_network(ipam["ip_range"])
    if scenario == "shared-override":
        expected_allowlist = shared_override
    elif scenario == "empty-allowlist":
        expected_allowlist = ""
    else:
        expected_allowlist = override or f"127.0.0.1,{proxy_ip}"
    for api in ("openrag", "openrag-cpu"):
        assert config["services"][api]["environment"]["UVICORN_FORWARDED_ALLOW_IPS"] == expected_allowlist


def test_admin_ui_compose_service_uses_project_scoped_container_name():
    compose_path = COMPOSE_DIR / "docker-compose.yaml"

    with compose_path.open(encoding="utf-8") as handle:
        compose = yaml.safe_load(handle)

    admin_ui = compose["services"]["admin-ui"]

    assert "container_name" not in admin_ui
    # nginx-unprivileged serves on the unprivileged :8080 inside the container.
    assert admin_ui["ports"] == ["${ADMIN_UI_PORT:-8081}:8080"]


def test_admin_ui_nginx_upload_limit_matches_api_default():
    nginx_conf = Path(__file__).resolve().parents[3] / "infra/compose/nginx/openrag-admin.conf"

    assert "client_max_body_size 1024M;" in nginx_conf.read_text(encoding="utf-8")


def test_admin_ui_nginx_preserves_public_host_header_for_api_redirects():
    nginx_conf = Path(__file__).resolve().parents[3] / "infra/compose/nginx/openrag-admin.conf"
    config = nginx_conf.read_text(encoding="utf-8")

    assert re.search(r"proxy_set_header\s+Host\s+\$http_host;", config)
    assert re.search(r"proxy_set_header\s+X-Forwarded-Host\s+\$http_host;", config)


def test_admin_ui_nginx_preserves_websocket_upgrades_for_chainlit():
    nginx_conf = Path(__file__).resolve().parents[3] / "infra/compose/nginx/openrag-admin.conf"
    config = nginx_conf.read_text(encoding="utf-8")

    assert re.search(r"map\s+\$http_upgrade\s+\$connection_upgrade\s+\{", config)
    assert re.search(r"proxy_http_version\s+1\.1;", config)
    assert re.search(r"proxy_set_header\s+Upgrade\s+\$http_upgrade;", config)
    assert re.search(r"proxy_set_header\s+Connection\s+\$connection_upgrade;", config)


def test_admin_ui_image_runs_as_uid_owning_its_writable_paths():
    """The runtime paths nginx writes must be group 0, and the image must run as
    a numeric UID that has access to them — the arbitrary-UID pattern
    api.Dockerfile uses.

    `USER nginx` is uid 101 with only gid 101, i.e. neither owner nor group on
    paths chowned to 10001. Nothing writes under /var/cache/nginx today (the
    base image points every *_temp_path at /tmp and openrag-admin.conf sets
    `proxy_cache off`), so this stays invisible until something does — and it
    then fails at request time, which no smoke test covers.
    """
    dockerfile = Path(__file__).resolve().parents[3] / "infra/docker/ui.Dockerfile"
    content = dockerfile.read_text(encoding="utf-8")

    assert re.search(r"^USER 10001:0$", content, re.MULTILINE)
    assert not re.search(r"^USER nginx$", content, re.MULTILINE)
    assert re.search(r"chown -R 10001:0 /var/cache/nginx /etc/nginx/conf\.d /var/run", content)
    assert re.search(r"chmod -R g\+w /var/cache/nginx /etc/nginx/conf\.d /var/run", content)
    # A private group would undo the chown above for any UID but 10001.
    assert "10001:10001" not in content


def test_admin_ui_chart_security_context_matches_image_ownership():
    """adminUi.podSecurityContext must keep runAsGroup 0 to match the image's
    chown -R 10001:0 — same reason openrag.podSecurityContext does.
    """
    values_path = Path(__file__).resolve().parents[3] / "infra/charts/openrag-stack/values.yaml"

    with values_path.open(encoding="utf-8") as handle:
        values = yaml.safe_load(handle)

    admin_ui_ctx = values["adminUi"]["podSecurityContext"]

    assert admin_ui_ctx["runAsUser"] == 10001
    assert admin_ui_ctx["runAsGroup"] == 0
    assert values["openrag"]["podSecurityContext"]["runAsGroup"] == 0
