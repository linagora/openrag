from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"


# ---------------------------------------------------------------------------
# One MinIO image everywhere
# ---------------------------------------------------------------------------


def test_every_stack_runs_the_same_minio_image() -> None:
    """Compose, Helm and the test stacks must agree: data written by a newer
    build can't be read by an older one, so a stack left behind on another tag
    can't share volumes or fixtures with the others."""
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    helm_image = values["milvus"]["minio"]["image"]
    expected = f"{helm_image['repository']}:{helm_image['tag']}"

    compose_files = [
        ROOT / "infra/compose/milvus/milvus.yaml",
        ROOT / "infra/compose/milvus/milvus.named-volumes.yaml",
        ROOT / "tests/integration/api/api_run/docker-compose.yaml",
        ROOT / "tests/integration/repos/docker-compose.yaml",
        ROOT / "tests/load/workspace/docker-compose.yml",
    ]
    images = {
        path.relative_to(ROOT).as_posix(): yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["minio"][
            "image"
        ]
        for path in compose_files
    }

    assert images == dict.fromkeys(images, expected)
    # A tag alone can be re-pushed upstream; the digest fixes what runs.
    assert "@sha256:" in expected
