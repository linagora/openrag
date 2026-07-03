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


def test_ollama_compose_admin_ui_honors_port_override():
    compose_path = Path(__file__).resolve().parents[3] / "docs/assets/compose_ollama_cpu.yaml"

    with compose_path.open(encoding="utf-8") as handle:
        compose = yaml.safe_load(handle)

    admin_ui = compose["services"]["admin-ui"]

    assert admin_ui["ports"] == ["${ADMIN_UI_PORT:-8081}:8080"]


def test_admin_ui_nginx_upload_limit_matches_api_default():
    nginx_conf = Path(__file__).resolve().parents[3] / "infra/compose/nginx/openrag-admin.conf"

    assert "client_max_body_size 1024M;" in nginx_conf.read_text(encoding="utf-8")
