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

Usage:
    python scripts/new_vulnerabilities.py --base base/*.json --head head/*.json

A lockfile the base does not have simply contributes no base report. Exit code
0 when nothing new is found, 1 otherwise. Prints one GitHub error annotation
per new finding and, when GITHUB_STEP_SUMMARY is set, appends a table to it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SEVERITIES = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")


def load_findings(reports: list[Path], min_severity: str) -> dict[tuple[str, str, str], dict]:
    """Index every finding at or above ``min_severity`` by (ecosystem, package, advisory)."""
    threshold = SEVERITIES.index(min_severity)
    findings: dict[tuple[str, str, str], dict] = {}
    for report in reports:
        data = json.loads(report.read_text())
        for result in data.get("Results") or []:
            # A single-file scan names the result by basename only; the artifact
            # keeps the path as given (ui/package-lock.json), which is what an
            # annotation needs to attach to the right file.
            lockfile = data.get("ArtifactName") or result.get("Target", "")
            for vuln in result.get("Vulnerabilities") or []:
                if SEVERITIES.index(vuln.get("Severity", "UNKNOWN")) < threshold:
                    continue
                key = (result.get("Type", ""), vuln["PkgName"], vuln["VulnerabilityID"])
                findings.setdefault(key, {**vuln, "Lockfile": lockfile})
    return findings


def _escape(text: str) -> str:
    # Workflow command data must not break out of the annotation line.
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    # Property values are additionally delimited by `,` and `:`.
    return _escape(text).replace(":", "%3A").replace(",", "%2C")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", nargs="*", type=Path, default=[], help="Trivy JSON reports of the base revision")
    parser.add_argument("--head", nargs="+", type=Path, required=True, help="Trivy JSON reports of the head revision")
    parser.add_argument("--min-severity", choices=SEVERITIES, default="HIGH")
    args = parser.parse_args(argv)

    base = load_findings(args.base, args.min_severity)
    head = load_findings(args.head, args.min_severity)
    new = sorted(
        (head[key] for key in head.keys() - base.keys()),
        key=lambda v: (-SEVERITIES.index(v["Severity"]), v["PkgName"], v["VulnerabilityID"]),
    )

    if not new:
        print(f"No new {args.min_severity}-or-above dependency vulnerabilities introduced by this change.")
        return 0

    rows = []
    for vuln in new:
        fixed = vuln.get("FixedVersion") or "no fixed version"
        message = f"{vuln['PkgName']} {vuln['InstalledVersion']}: {vuln['VulnerabilityID']} ({fixed})"
        title = f"New {vuln['Severity']} dependency vulnerability"
        print(f"::error file={_escape_property(vuln['Lockfile'])},title={_escape_property(title)}::{_escape(message)}")
        rows.append(
            f"| {vuln['Severity']} | `{vuln['PkgName']}` | {vuln['InstalledVersion']} "
            f"| [{vuln['VulnerabilityID']}]({vuln.get('PrimaryURL', '')}) | {fixed} | `{vuln['Lockfile']}` |"
        )

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(f"### {len(new)} new {args.min_severity}-or-above dependency vulnerabilities\n\n")
            fh.write("| Severity | Package | Version | Advisory | Fixed in | Lockfile |\n")
            fh.write("| --- | --- | --- | --- | --- | --- |\n")
            fh.write("\n".join(rows) + "\n")

    print(f"{len(new)} new {args.min_severity}-or-above dependency vulnerabilities; see the annotations above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
