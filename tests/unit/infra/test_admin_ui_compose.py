import re
from pathlib import Path

import yaml


def test_admin_ui_compose_service_uses_project_scoped_container_name():
    compose_path = Path(__file__).resolve().parents[3] / "infra/compose/docker-compose.yaml"

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
