from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
MIGRATION_JOB_TEMPLATE = CHART_DIR / "templates" / "postgres-migration-job.yaml"
HELM = os.environ.get("HELM_BIN") or shutil.which("helm")
requires_helm = pytest.mark.skipif(HELM is None, reason="Helm is not installed")

# What the chart requires before it renders, plus the Job turned on as the
# managed-Postgres setup does.
_REQUIRED = (
    "--set",
    "env.secrets.AUTH_TOKEN=or-render-check-0123456789",
    "--set",
    "env.secrets.POSTGRES_PASSWORD=render-check-pg-0123",
)
_JOB_ON = (
    "--set",
    "postgresProvisioning.migrationJob.enabled=true",
    "--set",
    "postgresProvisioning.runMigrationsInApp=false",
)
_EXTERNAL_SECRET = (
    "--set",
    "env.secretsProvider.type=externalSecret",
    "--set",
    "env.secretsProvider.externalSecret.secretStore.name=store",
    "--set",
    "env.secretsProvider.externalSecret.dataFrom[0].extract.key=openrag",
)
_VAULT_SECRET = (
    "--set",
    "env.secretsProvider.type=vaultStaticSecret",
    "--set",
    "env.secretsProvider.vaultStaticSecret.path=openrag",
)


def test_postgres_migration_job_uses_secret_ref_instead_of_literal_secret_values() -> None:
    """The Job reads its env through envFrom, from the hook copies the helpers
    name, rather than inlining literal secret values."""
    template = MIGRATION_JOB_TEMPLATE.read_text(encoding="utf-8")

    assert "envFrom:" in template
    assert "configMapRef:" in template
    assert 'name: {{ include "openrag-stack.migrationConfigMapName" . }}' in template
    assert "secretRef:" in template
    assert 'name: {{ include "openrag-stack.migrationSecretName" . }}' in template
    assert ".Values.env.secrets" not in template
    assert 'value: "{{ $value }}"' not in template


def test_postgres_migration_job_sets_uv_cache_dir() -> None:
    template = MIGRATION_JOB_TEMPLATE.read_text(encoding="utf-8")

    assert "name: UV_CACHE_DIR" in template


def test_postgres_migration_job_reuses_the_openrag_service_account() -> None:
    """The Job runs the OpenRAG image under the same pinned UID as its
    Deployment, so it needs the same ServiceAccount to reach the same SCC.

    Without this the Job silently falls back to the namespace's `default` SA.
    On OpenShift that means `restricted-v2` (MustRunAsRange), which rejects the
    requested runAsUser as outside the namespace's assigned range — and since
    this is a pre-install/pre-upgrade hook, the failure aborts the release.
    """
    template = MIGRATION_JOB_TEMPLATE.read_text(encoding="utf-8")

    assert "{{- with .Values.openrag.serviceAccountName }}" in template
    # tpl(), so a value like "{{ .Release.Name }}-openrag" resolves.
    assert "serviceAccountName: {{ tpl . $ }}" in template


def test_postgres_migration_job_omits_flags_the_runner_ignores() -> None:
    """The migration runner never reads these, so they must not be set here.

    ``services.persistence.migrations.run`` always runs migrations and never
    opens the app pool, so ``POSTGRES_AUTO_CREATE_DB`` / ``POSTGRES_RUN_MIGRATIONS``
    have no effect on it. Setting them on the Job only misleads readers.
    """
    template = MIGRATION_JOB_TEMPLATE.read_text(encoding="utf-8")

    assert "name: POSTGRES_AUTO_CREATE_DB" not in template
    assert "name: POSTGRES_RUN_MIGRATIONS" not in template


def _isolated_chart(tmp_path: Path) -> Path:
    """The parent chart without its sub-charts, which these templates never read."""
    chart = tmp_path / "openrag-stack"
    shutil.copytree(CHART_DIR / "templates", chart / "templates")
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: openrag-stack\nversion: 0.0.0\n", encoding="utf-8")
    return chart


def _render(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert HELM is not None
    return subprocess.run(
        [
            HELM,
            "template",
            "test",
            str(_isolated_chart(tmp_path)),
            *_REQUIRED,
            "--set",
            "postgresql.enabled=false",
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _documents(tmp_path: Path, *args: str) -> dict[tuple[str, str], dict]:
    result = _render(tmp_path, *args)
    assert result.returncode == 0, result.stderr
    return {
        (document["kind"], document["metadata"]["name"]): document
        for document in yaml.safe_load_all(result.stdout)
        if document
    }


def _hook_weight(document: dict) -> int:
    return int(document["metadata"]["annotations"]["helm.sh/hook-weight"])


def _job_env_from(documents: dict[tuple[str, str], dict]) -> list[str]:
    [job] = [document for (kind, _), document in documents.items() if kind == "Job"]
    return [next(iter(source.values()))["name"] for source in job["spec"]["template"]["spec"]["containers"][0]["envFrom"]]


@requires_helm
def test_the_job_reads_env_copies_created_before_it(tmp_path: Path) -> None:
    """A pre-install hook runs before Helm creates the release's own resources,
    so the Job cannot read the app's ConfigMap and Secret on a fresh install
    (#1034). It reads hook copies of lower weight, which Helm creates first."""
    documents = _documents(tmp_path, *_JOB_ON)
    job = documents[("Job", "openrag-postgres-migration")]
    config_copy = documents[("ConfigMap", "openrag-migration-env")]
    secret_copy = documents[("Secret", "openrag-migration-env-secrets")]

    assert _job_env_from(documents) == ["openrag-migration-env", "openrag-migration-env-secrets"]
    for copy in (config_copy, secret_copy):
        annotations = copy["metadata"]["annotations"]
        assert annotations["helm.sh/hook"] == job["metadata"]["annotations"]["helm.sh/hook"]
        assert _hook_weight(copy) < _hook_weight(job)
        assert "hook-succeeded" in annotations["helm.sh/hook-delete-policy"]


@requires_helm
def test_a_long_fullname_keeps_the_copies_apart_from_the_originals(tmp_path: Path) -> None:
    """Cut after the suffix, a 62-character fullname named the Secret copy like
    the app's Secret, and the copy's delete policy would delete the app's."""
    documents = _documents(tmp_path, *_JOB_ON, "--set", "fullnameOverride=" + "a" * 62)
    names = {kind: [name for document_kind, name in documents if document_kind == kind] for kind in ("ConfigMap", "Secret")}

    assert len(set(names["ConfigMap"])) == 2
    assert len(set(names["Secret"])) == 2
    assert all(len(name) <= 63 for name in names["Secret"])
    assert _job_env_from(documents) == ["a" * 49 + "-migration-env", "a" * 41 + "-migration-env-secrets"]


@requires_helm
@pytest.mark.parametrize(
    ("fullname", "copy_name"),
    [
        pytest.param("a" * 49 + "-migration", "a" * 49 + "-migration-env", id="config-map"),
        pytest.param("a" * 48 + "-migration", "a" * 48 + "-migration-env", id="config-map-cut-on-dash"),
        pytest.param("a" * 41 + "-migration", "a" * 41 + "-migration-env-secrets", id="secret"),
        pytest.param("a" * 40 + "-migration", "a" * 40 + "-migration-env-secrets", id="secret-cut-on-dash"),
    ],
)
def test_a_fullname_that_names_a_copy_like_the_original_is_refused(
    tmp_path: Path, fullname: str, copy_name: str
) -> None:
    """Cut before the suffix, a fullname ending in "-migration" still names a
    copy like the original at one length, and on an upgrade the copy's delete
    policy would delete the app's object."""
    result = _render(tmp_path, *_JOB_ON, "--set", "fullnameOverride=" + fullname)

    assert result.returncode != 0
    assert f'would be named "{copy_name}"' in result.stderr


@requires_helm
@pytest.mark.parametrize("args", [pytest.param((), id="job-off"), pytest.param(_JOB_ON, id="job-on")])
def test_the_config_checksum_is_the_rendered_config_map(tmp_path: Path, args: tuple[str, ...]) -> None:
    """checksum/config hashes configmap-env.yaml as the template outputs it. A
    change that only adds whitespace around the ConfigMap leaves the ConfigMap
    alone but changes the hash, and restarts every pod on the next upgrade."""
    documents = _documents(tmp_path / "full", *args)
    result = _render(tmp_path / "config-map", *args, "--show-only", "templates/configmap-env.yaml")
    assert result.returncode == 0, result.stderr
    # Helm prints "---" and a "# Source:" line before each template.
    config_map = result.stdout.split("\n", 2)[2]
    deployment = documents[("Deployment", "openrag-openrag")]

    checksum = deployment["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    assert checksum == hashlib.sha256(config_map.encode()).hexdigest()


@requires_helm
def test_the_env_copies_hold_what_the_app_reads(tmp_path: Path) -> None:
    documents = _documents(tmp_path, *_JOB_ON)

    assert documents[("ConfigMap", "openrag-migration-env")]["data"] == documents[("ConfigMap", "openrag-env")]["data"]
    assert (
        documents[("Secret", "openrag-migration-env-secrets")]["stringData"]
        == documents[("Secret", "openrag-env-secrets")]["stringData"]
    )


@requires_helm
def test_an_existing_secret_is_read_as_it_is(tmp_path: Path) -> None:
    """It is created before the install, so the hook can read it and has no copy."""
    documents = _documents(tmp_path, *_JOB_ON, "--set", "env.existingSecret=my-secret")

    assert _job_env_from(documents) == ["openrag-migration-env", "my-secret"]
    assert ("Secret", "openrag-migration-env-secrets") not in documents
    assert ("Secret", "openrag-env-secrets") not in documents


@requires_helm
def test_an_external_secret_gets_its_own_hook_copy(tmp_path: Path) -> None:
    """The operator writes the copy's Secret; the Job's pod waits for it."""
    documents = _documents(tmp_path, *_JOB_ON, *_EXTERNAL_SECRET)
    copy = documents[("ExternalSecret", "openrag-migration-env-secrets")]

    assert copy["spec"]["target"]["name"] == "openrag-migration-env-secrets"
    assert _hook_weight(copy) < _hook_weight(documents[("Job", "openrag-postgres-migration")])
    assert copy["spec"]["dataFrom"] == documents[("ExternalSecret", "openrag-env-secrets")]["spec"]["dataFrom"]


@requires_helm
def test_a_vault_copy_does_not_restart_the_app(tmp_path: Path) -> None:
    documents = _documents(tmp_path, *_JOB_ON, *_VAULT_SECRET)
    copy = documents[("VaultStaticSecret", "openrag-migration-env-secrets")]

    assert copy["spec"]["destination"]["name"] == "openrag-migration-env-secrets"
    assert "rolloutRestartTargets" not in copy["spec"]
    assert "rolloutRestartTargets" in documents[("VaultStaticSecret", "openrag-env-secrets")]["spec"]


@requires_helm
def test_without_the_job_nothing_is_a_hook(tmp_path: Path) -> None:
    documents = _documents(tmp_path)

    assert ("ConfigMap", "openrag-env") in documents
    assert ("ConfigMap", "openrag-migration-env") not in documents
    assert not [
        key for key, document in documents.items() if "helm.sh/hook" in (document["metadata"].get("annotations") or {})
    ]


@requires_helm
@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("--set", "env.config.POSTGRES_RUN_MIGRATIONS=false"), id="env-off-app-on"),
        pytest.param(("--set-string", "env.config.POSTGRES_RUN_MIGRATIONS=no"), id="env-no-app-on"),
        pytest.param((*_JOB_ON, "--set", "env.config.POSTGRES_RUN_MIGRATIONS=true"), id="env-on-app-off"),
        # Blank is unset to the app, which then migrates.
        pytest.param((*_JOB_ON, "--set-string", "env.config.POSTGRES_RUN_MIGRATIONS="), id="env-blank-app-off"),
        # Unset in the ConfigMap, which the app also reads as true.
        pytest.param((*_JOB_ON, "--set", "env.config.POSTGRES_RUN_MIGRATIONS=null"), id="env-removed-app-off"),
    ],
)
def test_an_env_override_that_disagrees_is_refused(tmp_path: Path, args: tuple[str, ...]) -> None:
    """env.config.POSTGRES_RUN_MIGRATIONS replaces the value derived from
    runMigrationsInApp, which then has no effect (#1039)."""
    result = _render(tmp_path, *args)

    assert result.returncode != 0
    assert "env.config.POSTGRES_RUN_MIGRATIONS is" in result.stderr


@requires_helm
@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("--set-string", "env.secrets.POSTGRES_RUN_MIGRATIONS=false"), id="secret-off-app-on"),
        pytest.param((*_JOB_ON, "--set-string", "env.secrets.POSTGRES_RUN_MIGRATIONS=true"), id="secret-on-app-off"),
        pytest.param((*_JOB_ON, "--set-string", "env.secrets.POSTGRES_RUN_MIGRATIONS="), id="secret-blank-app-off"),
    ],
)
def test_a_secret_override_that_disagrees_is_refused(tmp_path: Path, args: tuple[str, ...]) -> None:
    """The pods read the env Secret after the ConfigMap, so its value wins."""
    result = _render(tmp_path, *args)

    assert result.returncode != 0
    assert "env.secrets.POSTGRES_RUN_MIGRATIONS is" in result.stderr


@requires_helm
@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("--set-string", "env.config.POSTGRES_RUN_MIGRATIONS=on"), id="env-on"),
        pytest.param(("--set-string", "env.config.POSTGRES_RUN_MIGRATIONS=true "), id="env-trailing-space"),
        pytest.param(("--set-string", "env.secrets.POSTGRES_RUN_MIGRATIONS=on"), id="secret-on"),
    ],
)
def test_an_env_value_the_app_rejects_is_refused(tmp_path: Path, args: tuple[str, ...]) -> None:
    """The app accepts true/1/yes and false/0/no in any case, untrimmed, and
    stops at startup on anything else (_coerce in core/config/loader.py)."""
    result = _render(tmp_path, *args)

    assert result.returncode != 0
    assert "which the app rejects at startup" in result.stderr


@requires_helm
def test_a_run_migrations_in_app_the_app_rejects_is_refused(tmp_path: Path) -> None:
    result = _render(tmp_path, *_JOB_ON, "--set-string", "postgresProvisioning.runMigrationsInApp=on")

    assert result.returncode != 0
    assert "set it to true or false" in result.stderr


@requires_helm
@pytest.mark.parametrize(
    "args",
    [
        pytest.param((), id="defaults"),
        pytest.param(("--set", "env.config.POSTGRES_RUN_MIGRATIONS=True"), id="env-agrees-app-on"),
        pytest.param((*_JOB_ON, "--set-string", "env.config.POSTGRES_RUN_MIGRATIONS=0"), id="env-agrees-app-off"),
        pytest.param(("--set-string", "env.config.POSTGRES_RUN_MIGRATIONS=YES"), id="env-uppercase-agrees"),
        pytest.param(("--set-string", "env.config.POSTGRES_RUN_MIGRATIONS="), id="env-blank-app-on"),
        pytest.param(("--set-string", "env.secrets.POSTGRES_RUN_MIGRATIONS=yes"), id="secret-agrees-app-on"),
        # The chart does not render env.secrets then, so they cannot override.
        pytest.param(
            ("--set", "env.existingSecret=my-secret", "--set-string", "env.secrets.POSTGRES_RUN_MIGRATIONS=false"),
            id="secrets-unused-with-existing-secret",
        ),
        pytest.param(
            (
                "--set",
                "postgresProvisioning.runMigrationsInApp=false",
                "--set",
                "postgresProvisioning.externalMigrations=true",
            ),
            id="applied-externally",
        ),
    ],
)
def test_settings_that_apply_the_migrations_render(tmp_path: Path, args: tuple[str, ...]) -> None:
    result = _render(tmp_path, *args)

    assert result.returncode == 0, result.stderr


@requires_helm
@pytest.mark.parametrize(
    "args",
    [
        pytest.param((), id="external-unset"),
        # A non-empty string, which a template condition reads as true.
        pytest.param(("--set-string", "postgresProvisioning.externalMigrations=false"), id="external-string-false"),
    ],
)
def test_nothing_applying_the_migrations_is_refused(tmp_path: Path, args: tuple[str, ...]) -> None:
    """Migrations off in the app and no Job: the app answers 503 (#1039)."""
    result = _render(tmp_path, "--set", "postgresProvisioning.runMigrationsInApp=false", *args)

    assert result.returncode != 0
    assert "nothing applies the PostgreSQL migrations" in result.stderr


_BUNDLED_POSTGRES = (
    "--set",
    "postgresql.enabled=true",
    "--set",
    "postgresql.auth.password=render-check-pg-0123",
)


@requires_helm
@pytest.mark.parametrize("args", [pytest.param((), id="install"), pytest.param(("--is-upgrade",), id="upgrade")])
def test_the_job_with_the_bundled_postgres_is_refused(tmp_path: Path, args: tuple[str, ...]) -> None:
    """The Job runs before Helm creates the bundled PostgreSQL, so on a first
    install it cannot resolve its host. Refused on upgrades too: the Job is for
    an external PostgreSQL."""
    result = _render(tmp_path, *_JOB_ON, *_BUNDLED_POSTGRES, *args)

    assert result.returncode != 0
    assert "postgresProvisioning.migrationJob is for an external PostgreSQL" in result.stderr
