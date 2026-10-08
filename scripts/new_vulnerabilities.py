"""Fail when a change adds a dependency vulnerability at or above a severity.

Compares Trivy JSON reports (``trivy fs --format json``) of the same lockfiles
taken at two revisions — a pull request's base and its head — and reports the
findings the head has and the base does not.

This is what lets the dependency scan gate pull requests while the lockfiles
still carry advisories that predate the gate: an existing finding never fails
a build, only one the change itself brings in. The existing ones are reported
in full to code scanning by the scheduled run instead.

Both sides must come from the same Trivy database, so the difference is caused
by the change alone and not by an advisory published between two scans; the
workflow runs both scans in one job for that reason.

A finding is identified by (ecosystem, package, advisory), not by version.
Moving a package between two versions still affected by the same advisory is
not new; picking up an advisory the base did not have is.

Accepted findings. The head is scanned with the pull request's own
.trivyignore.yaml, which is how a reviewed change accepts an advisory it
introduces — and also how one could hide it. So both sides are scanned with
their own ignore file and ``--show-suppressed``, and every suppression at or
above the threshold is listed in the step summary with its statement. A
suppression the base did not already have is one this change adds, whether or
not the base reported the advisory, and also becomes a warning annotation.
Accepting stays possible, but never silent.

For the gate itself an advisory the base suppressed counts as present on the
base, so removing an acceptance does not fail a pull request over an advisory
that predates it.

Usage:
    python scripts/new_vulnerabilities.py --base base/*.json --head head/*.json

A lockfile the base does not have simply contributes no base report. Exit code
0 when nothing new is found, 1 otherwise; accepted findings never fail. Prints
one GitHub annotation per finding and, when GITHUB_STEP_SUMMARY is set, appends
tables to it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SEVERITIES = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")

Key = tuple[str, str, str]


def load_findings(reports: list[Path], min_severity: str) -> tuple[dict[Key, dict], dict[Key, dict]]:
    """Index findings at or above ``min_severity`` by (ecosystem, package, advisory).

    Returns the reported findings and, separately, the ones an ignore file
    suppressed (present only when Trivy ran with ``--show-suppressed``).
    """
    threshold = SEVERITIES.index(min_severity)
    findings: dict[Key, dict] = {}
    suppressed: dict[Key, dict] = {}
    for report in reports:
        data = json.loads(report.read_text())
        for result in data.get("Results") or []:
            # A single-file scan names the result by basename only; the artifact
            # keeps the path as given (ui/package-lock.json), which is what an
            # annotation needs to attach to the right file.
            lockfile = data.get("ArtifactName") or result.get("Target", "")
            ecosystem = result.get("Type", "")
            for vuln in result.get("Vulnerabilities") or []:
                if SEVERITIES.index(vuln.get("Severity", "UNKNOWN")) >= threshold:
                    key = (ecosystem, vuln["PkgName"], vuln["VulnerabilityID"])
                    findings.setdefault(key, {**vuln, "Lockfile": lockfile})
            for modified in result.get("ExperimentalModifiedFindings") or []:
                vuln = modified.get("Finding") or {}
                if modified.get("Type") != "vulnerability" or modified.get("Status") != "ignored":
                    continue
                if SEVERITIES.index(vuln.get("Severity", "UNKNOWN")) >= threshold:
                    key = (ecosystem, vuln["PkgName"], vuln["VulnerabilityID"])
                    suppressed.setdefault(
                        key,
                        {**vuln, "Lockfile": lockfile, "Statement": modified.get("Statement") or ""},
                    )
    return findings, suppressed


def _escape(text: str) -> str:
    # Workflow command data must not break out of the annotation line.
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    # Property values are additionally delimited by `,` and `:`.
    return _escape(text).replace(":", "%3A").replace(",", "%2C")


def _cell(text: str) -> str:
    # Keep a free-text statement inside its Markdown table cell.
    return " ".join(text.split()).replace("|", "\\|")


def _by_severity(findings) -> list[dict]:
    """Most severe first, then by package and advisory for a stable order."""
    return sorted(findings, key=lambda v: (-SEVERITIES.index(v["Severity"]), v["PkgName"], v["VulnerabilityID"]))


def _advisory(vuln: dict) -> str:
    return f"[{vuln['VulnerabilityID']}]({vuln.get('PrimaryURL', '')})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", nargs="*", type=Path, default=[], help="Trivy JSON reports of the base revision")
    parser.add_argument("--head", nargs="+", type=Path, required=True, help="Trivy JSON reports of the head revision")
    parser.add_argument("--min-severity", choices=SEVERITIES, default="HIGH")
    args = parser.parse_args(argv)
    level = f"{args.min_severity}-or-above"

    base, base_suppressed = load_findings(args.base, args.min_severity)
    head, suppressed = load_findings(args.head, args.min_severity)
    # Suppressed or not, an advisory the base had is not new to this change.
    new = _by_severity(head[key] for key in head.keys() - base.keys() - base_suppressed.keys())
    # Suppressions this change adds (the base did not suppress them) first, most severe first.
    accepted = sorted(
        ((key in base_suppressed, vuln) for key, vuln in suppressed.items()),
        key=lambda item: (
            item[0],
            -SEVERITIES.index(item[1]["Severity"]),
            item[1]["PkgName"],
            item[1]["VulnerabilityID"],
        ),
    )

    summary: list[str] = []

    if accepted:
        rows = []
        for on_base, vuln in accepted:
            statement = vuln["Statement"] or "(no statement given)"
            if not on_base:
                message = f"{vuln['PkgName']} {vuln['InstalledVersion']}: {vuln['VulnerabilityID']} — {statement}"
                title = f"{vuln['Severity']} dependency vulnerability accepted in .trivyignore.yaml"
                print(
                    f"::warning file={_escape_property(vuln['Lockfile'])},title={_escape_property(title)}"
                    f"::{_escape(message)}"
                )
            rows.append(
                f"| {'yes' if not on_base else 'no'} | {vuln['Severity']} | `{vuln['PkgName']}` "
                f"| {vuln['InstalledVersion']} | {_advisory(vuln)} | {_cell(statement)} | `{vuln['Lockfile']}` |"
            )
        summary += [
            f"### {len(accepted)} {level} dependency vulnerabilities accepted in `.trivyignore.yaml`\n",
            "| Accepted by this change | Severity | Package | Version | Advisory | Statement | Lockfile |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            *rows,
            "",
        ]

    if new:
        rows = []
        for vuln in new:
            fixed = vuln.get("FixedVersion") or "no fixed version"
            message = f"{vuln['PkgName']} {vuln['InstalledVersion']}: {vuln['VulnerabilityID']} ({fixed})"
            title = f"New {vuln['Severity']} dependency vulnerability"
            print(
                f"::error file={_escape_property(vuln['Lockfile'])},title={_escape_property(title)}::{_escape(message)}"
            )
            rows.append(
                f"| {vuln['Severity']} | `{vuln['PkgName']}` | {vuln['InstalledVersion']} "
                f"| {_advisory(vuln)} | {fixed} | `{vuln['Lockfile']}` |"
            )
        summary += [
            f"### {len(new)} new {level} dependency vulnerabilities\n",
            "| Severity | Package | Version | Advisory | Fixed in | Lockfile |",
            "| --- | --- | --- | --- | --- | --- |",
            *rows,
            "",
        ]

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path and summary:
        with open(summary_path, "a") as fh:
            fh.write("\n".join(summary) + "\n")

    if accepted:
        print(
            f"{len(accepted)} {level} dependency vulnerabilities accepted in .trivyignore.yaml; see the step summary."
        )
    if not new:
        print(f"No new {level} dependency vulnerabilities introduced by this change.")
        return 0
    print(f"{len(new)} new {level} dependency vulnerabilities; see the annotations above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
