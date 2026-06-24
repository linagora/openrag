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


def test_postgres_migration_job_sets_uv_cache_dir() -> None:
    template = MIGRATION_JOB_TEMPLATE.read_text(encoding="utf-8")

    assert "name: UV_CACHE_DIR" in template


def test_postgres_migration_job_omits_flags_the_runner_ignores() -> None:
    """The migration runner never reads these, so they must not be set here.

    ``services.persistence.migrations.run`` always runs migrations and never
    opens the app pool, so ``POSTGRES_AUTO_CREATE_DB`` / ``POSTGRES_RUN_MIGRATIONS``
    have no effect on it. Setting them on the Job only misleads readers.
    """
    template = MIGRATION_JOB_TEMPLATE.read_text(encoding="utf-8")

    assert "name: POSTGRES_AUTO_CREATE_DB" not in template
    assert "name: POSTGRES_RUN_MIGRATIONS" not in template
