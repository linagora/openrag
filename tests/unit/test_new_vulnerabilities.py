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


def report(tmp_path, name, *vulns, artifact="uv.lock", kind="uv", suppressed=()):
    """Write a Trivy JSON report shaped like `trivy fs --format json [--show-suppressed] <lockfile>`.

    ``suppressed`` holds (finding, statement) pairs an ignore file matched.
    """
    path = tmp_path / f"{name}.json"
    target = Path(artifact).name  # Trivy names a single-file result by basename
    result = {"Target": target, "Type": kind, "Vulnerabilities": list(vulns)}
    if suppressed:
        result["ExperimentalModifiedFindings"] = [
            {
                "Type": "vulnerability",
                "Status": "ignored",
                "Statement": statement,
                "Source": ".trivyignore.yaml",
                "Finding": finding,
            }
            for finding, statement in suppressed
        ]
    path.write_text(json.dumps({"ArtifactName": artifact, "Results": [result]}))
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


def test_suppression_added_by_the_change_passes_but_is_annotated_and_summarised(tmp_path, capsys, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    base = report(tmp_path, "base")
    h11 = vuln(pkg="h11", version="0.14.0", advisory="CVE-2025-43859", severity="CRITICAL", fixed="0.16.0")
    head = report(tmp_path, "head", suppressed=[(h11, "Only reached by the test client | not in production")])

    assert gate.main(["--base", str(base), "--head", str(head)]) == 0
    out = capsys.readouterr().out
    assert (
        "::warning file=uv.lock,title=CRITICAL dependency vulnerability accepted in .trivyignore.yaml"
        "::h11 0.14.0: CVE-2025-43859 — Only reached by the test client | not in production"
    ) in out
    assert "::error" not in out
    text = summary.read_text()
    assert "### 1 HIGH-or-above dependency vulnerabilities accepted in `.trivyignore.yaml`" in text
    assert "| yes | CRITICAL | `h11` | 0.14.0 |" in text
    assert "Only reached by the test client \\| not in production" in text


def test_suppression_of_an_advisory_already_on_base_is_summarised_without_annotation(tmp_path, capsys, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    base = report(tmp_path, "base", vuln())
    head = report(tmp_path, "head", suppressed=[(vuln(), "accepted in an earlier change")])

    assert gate.main(["--base", str(base), "--head", str(head)]) == 0
    assert "::warning" not in capsys.readouterr().out
    assert "| no | HIGH | `aiohttp` |" in summary.read_text()


def test_suppression_without_statement_is_called_out(tmp_path, capsys):
    head = report(tmp_path, "head", suppressed=[(vuln(), "")])

    assert gate.main(["--head", str(head)]) == 0
    assert "CVE-2025-0001 — (no statement given)" in capsys.readouterr().out


def test_suppression_below_threshold_is_not_reported(tmp_path, capsys):
    head = report(tmp_path, "head", suppressed=[(vuln(severity="MEDIUM"), "fine")])

    assert gate.main(["--head", str(head)]) == 0
    assert "accepted" not in capsys.readouterr().out


def test_suppression_does_not_hide_a_different_new_finding(tmp_path, capsys):
    head = report(
        tmp_path,
        "head",
        vuln(pkg="pillow", advisory="CVE-2025-0002"),
        suppressed=[(vuln(), "accepted")],
    )

    assert gate.main(["--base", "--head", str(head)]) == 1
    out = capsys.readouterr().out
    assert "::error file=uv.lock,title=New HIGH dependency vulnerability::pillow" in out
    assert "::warning file=uv.lock" in out
