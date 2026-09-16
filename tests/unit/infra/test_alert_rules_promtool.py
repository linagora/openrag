"""Run the promtool unit tests for the shipped alert rules.

``test_alert_rules.py`` checks the rules' *structure* — annotations present,
runbooks existing, metric names known. This runs their *behaviour*: promtool
replays synthetic series through the real expressions and asserts which alerts
fire, when, and with what rendered annotation text.

That last part is what structure tests cannot reach. An expression can carry
the right labels, fire at the right time, and still put a per-second slope in
the field a human reads first — which is exactly what the first run of
``alert_rules.promtool.yaml`` found in OpenRagBacklogGrowing.

Skipped rather than failed when promtool is unavailable: a local ``promtool``
binary is used if present, otherwise the Prometheus image *if it is already
pulled*. Nothing here pulls a 100 MB image into somebody's unit-test run, so CI
must install promtool (or pre-pull the image) for this to execute there.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TEST_FILE = Path(__file__).with_name("alert_rules.promtool.yaml")
#: Pinned to the version the Compose overlay runs, so the tests are checked
#: against the promtool that will actually evaluate these rules.
PROMETHEUS_IMAGE = "prom/prometheus:v2.54.1"


def _local_promtool() -> list[str] | None:
    binary = shutil.which("promtool")
    return [binary, "test", "rules", str(TEST_FILE)] if binary else None


def _dockerised_promtool() -> list[str] | None:
    docker = shutil.which("docker")
    if not docker:
        return None
    # Only if the image is already local — see the module docstring.
    present = subprocess.run(
        [docker, "image", "inspect", PROMETHEUS_IMAGE],
        capture_output=True,
        check=False,
    )
    if present.returncode != 0:
        return None
    return [
        docker, "run", "--rm",
        "-v", f"{ROOT}:/repo:ro",
        "-w", "/repo",
        "--entrypoint", "promtool",
        PROMETHEUS_IMAGE,
        "test", "rules", str(TEST_FILE.relative_to(ROOT)),
    ]


def test_alert_rules_behave_as_specified() -> None:
    command = _local_promtool() or _dockerised_promtool()
    if command is None:
        pytest.skip(f"needs a promtool binary, or the {PROMETHEUS_IMAGE} image already pulled")

    result = subprocess.run(command, capture_output=True, text=True, check=False, cwd=ROOT)

    assert result.returncode == 0, f"promtool unit tests failed:\n{result.stdout}\n{result.stderr}"
    assert "SUCCESS" in result.stdout
