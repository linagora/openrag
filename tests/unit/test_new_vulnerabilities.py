"""The dependency scan gates only on vulnerabilities a change introduces."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "new_vulnerabilities", Path(__file__).resolve().parents[2] / "scripts/new_vulnerabilities.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def vuln(pkg="aiohttp", version="3.12.14", advisory="CVE-2025-0001", severity="HIGH", fixed="3.13.3"):
    return {
        "PkgName": pkg,
        "InstalledVersion": version,
        "VulnerabilityID": advisory,
        "Severity": severity,
        "FixedVersion": fixed,
        "PrimaryURL": f"https://avd.aquasec.com/nvd/{advisory.lower()}",
    }


def report(tmp_path, name, *vulns, artifact="uv.lock", kind="uv"):
    """Write a Trivy JSON report shaped like `trivy fs --format json <lockfile>`."""
    path = tmp_path / f"{name}.json"
    target = Path(artifact).name  # Trivy names a single-file result by basename
    path.write_text(
        json.dumps(
            {"ArtifactName": artifact, "Results": [{"Target": target, "Type": kind, "Vulnerabilities": list(vulns)}]}
        )
    )
    return path


def test_finding_already_on_base_does_not_fail(tmp_path, capsys):
    base = report(tmp_path, "base", vuln())
    head = report(tmp_path, "head", vuln())

    assert gate.main(["--base", str(base), "--head", str(head)]) == 0
    assert "::error" not in capsys.readouterr().out


def test_new_advisory_fails_with_an_annotation_on_the_lockfile(tmp_path, capsys):
    base = report(tmp_path, "base", vuln(), artifact="ui/package-lock.json", kind="npm")
    head = report(
        tmp_path,
        "head",
        vuln(),
        vuln(pkg="h11", version="0.14.0", advisory="CVE-2025-43859", severity="CRITICAL", fixed="0.16.0"),
        artifact="ui/package-lock.json",
        kind="npm",
    )

    assert gate.main(["--base", str(base), "--head", str(head)]) == 1
    out = capsys.readouterr().out
    assert "::error file=ui/package-lock.json,title=New CRITICAL dependency vulnerability::h11 0.14.0" in out
    assert "aiohttp" not in out


def test_moving_between_versions_affected_by_the_same_advisory_is_not_new(tmp_path):
    base = report(tmp_path, "base", vuln(version="3.12.14"))
    head = report(tmp_path, "head", vuln(version="3.12.15"))

    assert gate.main(["--base", str(base), "--head", str(head)]) == 0


def test_same_advisory_in_another_ecosystem_is_new(tmp_path):
    base = report(tmp_path, "base", vuln(pkg="shared"), kind="uv")
    head = report(tmp_path, "head", vuln(pkg="shared"), kind="npm", artifact="ui/package-lock.json")

    assert gate.main(["--base", str(base), "--head", str(head)]) == 1


@pytest.mark.parametrize(
    ("severity", "min_severity", "fails"),
    [
        ("MEDIUM", "HIGH", False),
        ("HIGH", "HIGH", True),
        ("HIGH", "CRITICAL", False),
        ("UNKNOWN", "LOW", False),
    ],
)
def test_severity_threshold(tmp_path, severity, min_severity, fails):
    head = report(tmp_path, "head", vuln(severity=severity))

    assert gate.main(["--head", str(head), "--min-severity", min_severity]) == int(fails)


def test_lockfile_absent_on_base_makes_every_head_finding_new(tmp_path):
    head = report(tmp_path, "head", vuln())

    assert gate.main(["--base", "--head", str(head)]) == 1


def test_empty_scan_results(tmp_path):
    path = tmp_path / "clean.json"
    path.write_text(json.dumps({"ArtifactName": "uv.lock", "Results": [{"Target": "uv.lock", "Type": "uv"}]}))

    assert gate.main(["--head", str(path)]) == 0


def test_step_summary_lists_new_findings(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    head = report(tmp_path, "head", vuln(fixed=""))

    gate.main(["--head", str(head)])

    text = summary.read_text()
    assert "### 1 new HIGH-or-above dependency vulnerabilities" in text
    assert (
        "| HIGH | `aiohttp` | 3.12.14 | [CVE-2025-0001](https://avd.aquasec.com/nvd/cve-2025-0001) | no fixed version | `uv.lock` |"
        in text
    )


def test_annotation_properties_cannot_break_out_of_the_command(tmp_path, capsys):
    head = report(tmp_path, "head", vuln(advisory="CVE-1\n::warning::x"), artifact="odd,name:lock")

    gate.main(["--head", str(head)])

    line = capsys.readouterr().out.splitlines()[0]
    assert line.startswith("::error file=odd%2Cname%3Alock,title=")
    assert "%0A::warning::x" in line
