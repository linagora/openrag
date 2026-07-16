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
