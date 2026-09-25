#!/usr/bin/env python3
"""Generate the Compose copy of the alert rules from the chart template.

``infra/charts/openrag-stack/rules/openrag-alerts.yaml.tpl`` is the single source
of truth, and it is a Helm template: thresholds and ``for`` durations are values
because they are deployment config, not constants. Compose cannot render Helm
templates, so it loads a generated copy rendered with default values.

Generated, not duplicated. Two hand-maintained rule sets is how Compose and
Kubernetes alerting drift apart without anyone noticing; a generated copy with a
CI drift check cannot.

    python scripts/gen_alert_rules.py            # write the copy
    python scripts/gen_alert_rules.py --check    # fail if it is out of date

Rendering goes through Helm rather than reimplementing the substitutions here,
so the generated file is what the chart would actually produce. The chart's
subcharts are stripped into a temporary copy first: they are irrelevant to this
template and ``helm template`` refuses to render anything without them present.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "infra" / "charts" / "openrag-stack"
TEMPLATE = CHART / "rules" / "openrag-alerts.yaml.tpl"
GENERATED = ROOT / "infra" / "compose" / "prometheus" / "rules" / "openrag-alerts.yaml"

HEADER = """# GENERATED FILE — DO NOT EDIT.
#
# Rendered from infra/charts/openrag-stack/rules/openrag-alerts.yaml.tpl with
# default values by scripts/gen_alert_rules.py. Edit that template instead; CI
# regenerates this file and fails on any difference.
#
# Compose loads it through `rule_files` in prometheus.yml. Kubernetes renders the
# same template itself, with this deployment's threshold values applied.
"""


#: The chart links runbooks at the release it deploys (v<appVersion>). A Compose
#: install runs from a git checkout, so its copy links main, and a version bump
#: does not rewrite this file.
COMPOSE_OVERRIDES = ("monitoring.prometheusRule.runbookBaseUrl=https://github.com/linagora/openrag/blob/main/docs/deployment/runbooks",)


def render(overrides: Sequence[str] = ()) -> str:
    """Return the rule groups as Helm renders them.

    ``overrides`` are extra ``--set`` expressions. The generated file uses none;
    the tests pass some to check that a retuned deployment renders what it asked for.
    """
    helm = shutil.which("helm")
    if helm is None:
        raise SystemExit("helm is required to generate the alert rules")

    with tempfile.TemporaryDirectory() as tmp:
        stripped = Path(tmp) / "chart"
        (stripped / "templates").mkdir(parents=True)
        (stripped / "rules").mkdir()

        chart_meta = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))
        chart_meta.pop("dependencies", None)
        (stripped / "Chart.yaml").write_text(yaml.safe_dump(chart_meta), encoding="utf-8")
        shutil.copy(CHART / "values.yaml", stripped / "values.yaml")
        shutil.copy(CHART / "templates" / "_helpers.tpl", stripped / "templates")
        shutil.copy(CHART / "templates" / "prometheusrule.yaml", stripped / "templates")
        shutil.copy(TEMPLATE, stripped / "rules")

        result = subprocess.run(
            [
                helm,
                "template",
                "gen",
                str(stripped),
                "-s",
                "templates/prometheusrule.yaml",
                "--set",
                "monitoring.prometheusRule.enabled=true",
                *(arg for override in overrides for arg in ("--set", override)),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SystemExit(f"helm template failed:\n{result.stdout}\n{result.stderr}")

    # The no-commonLabels branch emits the rendered template as text, so the
    # rules' comments survive into the generated file. Take everything after
    # `spec:` and remove the two spaces of manifest indentation.
    lines = result.stdout.splitlines()
    spec = lines.index("spec:")
    body = [line[2:] if line.startswith("  ") else line for line in lines[spec + 1 :]]
    return HEADER + "\n".join(body).strip("\n") + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the generated file is out of date")
    args = parser.parse_args()

    rendered = render(COMPOSE_OVERRIDES)
    # Parse both sides: a difference that yaml.safe_load cannot see is a
    # difference in comments or formatting, which still matters for a file
    # people read, but should be reported as such rather than as a rule change.
    yaml.safe_load(rendered)

    if args.check:
        current = GENERATED.read_text(encoding="utf-8") if GENERATED.exists() else ""
        if current != rendered:
            print(f"{GENERATED.relative_to(ROOT)} is out of date.", file=sys.stderr)
            print("Run: python scripts/gen_alert_rules.py", file=sys.stderr)
            return 1
        print(f"{GENERATED.relative_to(ROOT)} is up to date.")
        return 0

    GENERATED.parent.mkdir(parents=True, exist_ok=True)
    GENERATED.write_text(rendered, encoding="utf-8")
    print(f"wrote {GENERATED.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
