from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MIGRATION_JOB_TEMPLATE = ROOT / "infra" / "charts" / "openrag-stack" / "templates" / "postgres-migration-job.yaml"


def test_postgres_migration_job_uses_secret_ref_instead_of_literal_secret_values() -> None:
    template = MIGRATION_JOB_TEMPLATE.read_text(encoding="utf-8")

    assert "envFrom:" in template
    assert "configMapRef:" in template
    assert "name: rag-env" in template
    assert "secretRef:" in template
    assert "name: rag-env-secrets" in template
    assert ".Values.env.secrets" not in template
    assert 'value: "{{ $value }}"' not in template


def test_postgres_migration_job_keeps_migration_specific_overrides() -> None:
    template = MIGRATION_JOB_TEMPLATE.read_text(encoding="utf-8")

    assert "name: POSTGRES_AUTO_CREATE_DB" in template
    assert "name: POSTGRES_RUN_MIGRATIONS" in template
    assert "name: UV_CACHE_DIR" in template
