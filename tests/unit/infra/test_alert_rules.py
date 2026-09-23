"""Guards on the shipped Prometheus alert rules.

The rules are data, not code, so nothing else fails when they are wrong: a typo'd
metric name is valid PromQL that matches nothing, and an alert that matches nothing
is indistinguishable from a healthy system. These tests are the only thing standing
between a silent typo and an alert that never fires.

Structure mirrors the other infra tests: parse the shipped artifacts from disk, assert
properties of them. No cluster, no Prometheus, no network.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
RULES_FILE = ROOT / "infra" / "compose" / "prometheus" / "rules" / "openrag-alerts.yaml"
RULES_TEMPLATE = ROOT / "infra" / "charts" / "openrag-stack" / "rules" / "openrag-alerts.yaml.tpl"
RUNBOOK_DIR = ROOT / "docs" / "deployment" / "runbooks"
CHART_DIR = ROOT / "infra" / "charts" / "openrag-stack"
COMPOSE_DIR = ROOT / "infra" / "compose"

#: The published base the rules' ``runbook_url`` annotations are built on, and the
#: template's default for ``monitoring.prometheusRule.runbookBaseUrl``.
RUNBOOK_BASE = "https://github.com/linagora/openrag/blob/main/docs/deployment/runbooks"

#: Metric names an expression is allowed to reference.
#:
#: TODO(S3-2): replace this literal set with ``metric_specs.ALL_SPECS`` once #929 lands
#: on develop, so a renamed metric breaks the rules at build time instead of silently
#: un-firing them. Until then this is a hand-maintained mirror of that contract, and the
#: duplication is the point: it fails when the two drift.
KNOWN_METRICS: frozenset[str] = frozenset(
    {
        # Declared by core/observability/metric_specs.py (S3-2, #929).
        "openrag_ingest_documents_total",
        "openrag_ingest_stage_duration_seconds",
        "openrag_ingest_queue_wait_seconds",
        "openrag_ingest_last_parse_completion_timestamp_seconds",
        "openrag_ingest_clock_skew_events_total",
        "openrag_ingest_tasks",
        "openrag_inference_requests_total",
        "openrag_inference_duration_seconds",
        "openrag_circuit_breaker_state",
        "openrag_llm_tokens_total",
        # Already on develop, in core/observability/monitoring.py.
        "openrag_retrieval_orphan_chunks_dropped_total",
        "openrag_http_requests_total",
        "openrag_http_request_failures_total",
        "openrag_http_request_duration_seconds",
        # Prometheus' own synthetic sample.
        "up",
    }
)

#: Labels no expression may group by or match on. Caller-controlled and therefore
#: unbounded: a rule that reintroduces one undoes the cardinality ceiling the metric
#: design exists to hold. The query-side analogue of S3-2's build-time label guard.
FORBIDDEN_LABELS: frozenset[str] = frozenset({"partition", "user_id", "file_id", "task_id", "request_id", "filename"})

REQUIRED_ANNOTATIONS = ("summary", "description", "runbook_url")

#: Identifiers that appear in expressions but are PromQL, not metrics.
_PROMQL_KEYWORDS = frozenset(
    {
        "and",
        "or",
        "unless",
        "by",
        "without",
        "on",
        "ignoring",
        "group_left",
        "group_right",
        "offset",
        "bool",
        "rate",
        "irate",
        "increase",
        "sum",
        "max",
        "min",
        "avg",
        "count",
        "deriv",
        "predict_linear",
        "time",
        "absent",
        "absent_over_time",
        "label_replace",
        "label_join",
        "histogram_quantile",
        "min_over_time",
        "max_over_time",
        "avg_over_time",
        "sum_over_time",
        "count_over_time",
        "last_over_time",
        "clamp_min",
        "clamp_max",
        "round",
        "humanizePercentage",
        "humanizeDuration",
        "humanize",
    }
)

# A metric name at the head of a selector: an identifier not preceded by a `.` or `$`
# and not immediately followed by `(` (which would make it a function call).
_METRIC_RE = re.compile(r"(?<![\w.$])([a-zA-Z_][a-zA-Z0-9_]*)\s*(?![\w(])")


def _rules_document() -> dict:
    return yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))


def _all_rules() -> list[dict]:
    return [rule for group in _rules_document()["groups"] for rule in group["rules"]]


#: Clauses whose parenthesised argument is a list of *label* names, not metrics.
_LABEL_LIST_RE = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")


def _metric_names(expr: str) -> set[str]:
    """Identifiers in ``expr`` that are metric selectors.

    Three things in a PromQL expression look like a bare identifier but are not a
    metric, and each has bitten this parser: label matcher values inside ``{}``,
    string arguments to functions such as ``label_replace``, and the label lists in
    ``by (...)`` / ``on (...)`` clauses. All three are removed before matching, so a
    genuine typo in a metric name is what is left.
    """
    cleaned = re.sub(r"\"[^\"]*\"|'[^']*'", "", expr)  # string literals
    cleaned = re.sub(r"\{[^}]*\}", "", cleaned)  # label matchers
    cleaned = _LABEL_LIST_RE.sub("", cleaned)  # by/on/without label lists
    return {name for name in _METRIC_RE.findall(cleaned) if name not in _PROMQL_KEYWORDS and not name.isdigit()}


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_rules_file_parses_and_declares_groups() -> None:
    doc = _rules_document()
    assert doc["groups"], "no rule groups declared"
    for group in doc["groups"]:
        assert group["name"], "a rule group has no name"
        assert group["rules"], f"group {group['name']} declares no rules"


def test_alert_names_are_unique() -> None:
    names = [rule["alert"] for rule in _all_rules()]
    assert len(names) == len(set(names)), f"duplicate alert names: {names}"


@pytest.mark.parametrize("rule", _all_rules(), ids=lambda r: r["alert"])
def test_every_alert_declares_a_severity(rule: dict) -> None:
    assert rule["labels"]["severity"] in {"critical", "warning", "info"}


@pytest.mark.parametrize("rule", _all_rules(), ids=lambda r: r["alert"])
def test_every_alert_declares_the_required_annotations(rule: dict) -> None:
    for annotation in REQUIRED_ANNOTATIONS:
        assert rule["annotations"].get(annotation), f"{rule['alert']} is missing {annotation}"


@pytest.mark.parametrize("rule", _all_rules(), ids=lambda r: r["alert"])
def test_every_alert_waits_before_firing(rule: dict) -> None:
    """``for:`` is what separates an alert from a transient scrape artefact."""
    assert rule.get("for"), f"{rule['alert']} has no `for:` and will fire on one bad scrape"


# ---------------------------------------------------------------------------
# The runbook promise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule", _all_rules(), ids=lambda r: r["alert"])
def test_runbook_url_points_at_a_page_that_exists(rule: dict) -> None:
    """A runbook_url that 404s is worse than no annotation: it promises help at 3am."""
    url = rule["annotations"]["runbook_url"]
    assert url.startswith(RUNBOOK_BASE), f"{rule['alert']} uses an unexpected runbook base: {url}"
    page = RUNBOOK_DIR / url.rsplit("/", 1)[-1]
    assert page.is_file(), f"{rule['alert']} points at {page.name}, which does not exist"


@pytest.mark.parametrize("rule", _all_rules(), ids=lambda r: r["alert"])
def test_runbook_page_is_named_after_its_alert(rule: dict) -> None:
    assert (RUNBOOK_DIR / f"{rule['alert']}.md").is_file()


@pytest.mark.parametrize("rule", _all_rules(), ids=lambda r: r["alert"])
def test_runbook_says_its_numbers_are_defaults(rule: dict) -> None:
    """Every threshold and `for` duration is a chart value, so a runbook that
    states one as fact is wrong for any deployment that retuned it — and it is
    wrong at 3am, to someone who trusts it. Each page must say so and show how to
    read the rule that is actually loaded."""
    page = (RUNBOOK_DIR / f"{rule['alert']}.md").read_text(encoding="utf-8")
    assert "are defaults; your deployment may differ" in page, (
        f"{rule['alert']}.md states numbers without saying they are defaults"
    )
    assert "get prometheusrule" in page, f"{rule['alert']}.md does not show how to read the rule actually loaded"
    assert f"for.{rule['alert']}" in page, f"{rule['alert']}.md does not name the value that changes its `for` duration"


def test_runbook_index_lists_every_alert() -> None:
    index = (RUNBOOK_DIR / "README.md").read_text(encoding="utf-8")
    for rule in _all_rules():
        assert f"({rule['alert']}.md)" in index, f"{rule['alert']} is missing from the index"


def test_no_orphan_runbook_pages() -> None:
    """A page with no rule behind it is a runbook for an alert that cannot fire."""
    named = {f"{rule['alert']}.md" for rule in _all_rules()} | {"README.md"}
    on_disk = {path.name for path in RUNBOOK_DIR.glob("*.md")}
    assert on_disk == named, f"unexpected runbook pages: {sorted(on_disk - named)}"


# ---------------------------------------------------------------------------
# Expression contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule", _all_rules(), ids=lambda r: r["alert"])
def test_expressions_reference_only_known_metrics(rule: dict) -> None:
    """A typo'd metric name is valid PromQL that matches nothing, forever."""
    unknown = _metric_names(rule["expr"]) - KNOWN_METRICS
    assert not unknown, f"{rule['alert']} references unknown metric(s): {sorted(unknown)}"


@pytest.mark.parametrize("rule", _all_rules(), ids=lambda r: r["alert"])
def test_expressions_use_no_caller_controlled_label(rule: dict) -> None:
    """The query-side half of S3-2's cardinality guard."""
    found = {label for label in FORBIDDEN_LABELS if re.search(rf"\b{label}\b", rule["expr"])}
    assert not found, f"{rule['alert']} groups on unbounded label(s): {sorted(found)}"


# ---------------------------------------------------------------------------
# Delivery — one rule set, two consumers
# ---------------------------------------------------------------------------


def test_generated_rules_are_not_hand_edited() -> None:
    """The Compose copy is output, not source. Saying so in the file is the only
    thing standing between a reader and a fix that the next regeneration eats."""
    generated = RULES_FILE.read_text(encoding="utf-8")
    assert "GENERATED FILE — DO NOT EDIT" in generated
    assert "scripts/gen_alert_rules.py" in generated


def test_job_matcher_and_runbook_base_are_values_not_needles() -> None:
    """Both were textual replacements once; a needle that stopped matching made
    the override silently do nothing. They are ordinary interpolation now."""
    template = RULES_TEMPLATE.read_text(encoding="utf-8")
    assert "jobMatcher" in template
    assert "runbookBaseUrl" in template
    chart_template = (CHART_DIR / "templates" / "prometheusrule.yaml").read_text(encoding="utf-8")
    assert "replace" not in chart_template, "textual substitution is back; use template values"
    assert 'tpl (.Files.Get "rules/openrag-alerts.yaml.tpl")' in chart_template


def test_compose_prometheus_loads_the_same_rules() -> None:
    config = yaml.safe_load((COMPOSE_DIR / "prometheus" / "prometheus.yml").read_text(encoding="utf-8"))
    assert config["rule_files"] == ["/etc/prometheus/rules/*.yaml"]

    overlay = yaml.safe_load((COMPOSE_DIR / "monitoring.docker-compose.yaml").read_text(encoding="utf-8"))
    mounts = overlay["services"]["prometheus"]["volumes"]
    assert "./prometheus/rules:/etc/prometheus/rules:ro" in mounts, (
        "the Compose overlay must mount the generated rules directory; the copy is "
        "produced from the chart template so both deployments load one definition"
    )


# ---------------------------------------------------------------------------
# Overrides — what a retuned deployment actually renders
# ---------------------------------------------------------------------------


def _render(*overrides: str) -> dict[str, dict]:
    """Render the chart's rules with ``--set`` overrides, keyed by alert name."""
    if shutil.which("helm") is None:
        pytest.skip("needs helm to render the chart's rule template")
    spec = importlib.util.spec_from_file_location("gen_alert_rules", ROOT / "scripts" / "gen_alert_rules.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    doc = yaml.safe_load(module.render(overrides))
    return {rule["alert"]: rule for group in doc["groups"] for rule in group["rules"]}


def test_a_zero_threshold_is_honoured_not_replaced_by_the_default() -> None:
    """``default`` treats 0 as empty, so ``backlogDepth: 0`` used to render as 50."""
    rules = _render(
        "monitoring.prometheusRule.thresholds.backlogDepth=0",
        "monitoring.prometheusRule.thresholds.ingestVolumeFloor=0",
    )
    assert 'openrag_ingest_tasks{state="QUEUED"} > 0\n' in rules["OpenRagBacklogGrowing"]["expr"]
    assert rules["OpenRagIngestFailureRate"]["expr"].rstrip().endswith(">= 0")


def test_an_unknown_threshold_key_is_refused() -> None:
    """A misspelt key renders the default with nothing to say the override was ignored."""
    with pytest.raises(SystemExit, match="thresholds.backlogdepth"):
        _render("monitoring.prometheusRule.thresholds.backlogdepth=10")


def test_annotations_follow_overridden_values() -> None:
    """The text a human reads must state the numbers the rule actually uses."""
    rules = _render(
        "monitoring.prometheusRule.thresholds.ingestIdleSeconds=1800",
        "monitoring.prometheusRule.thresholds.ingestFailureRatio=0.1",
        "monitoring.prometheusRule.thresholds.inferenceErrorRatio=0.2",
        "monitoring.prometheusRule.for.OpenRagBacklogGrowing=40m",
        "monitoring.prometheusRule.for.OpenRagTargetDown=10m",
    )
    assert "for over 30 minutes" in rules["OpenRagIngestStalled"]["annotations"]["description"]
    assert "More than 10%" in rules["OpenRagIngestFailureRate"]["annotations"]["summary"]
    assert "More than 20%" in rules["OpenRagInferenceProviderDown"]["annotations"]["description"]
    assert "for 40 minutes" in rules["OpenRagBacklogGrowing"]["annotations"]["description"]
    assert "for 10 minutes" in rules["OpenRagTargetDown"]["annotations"]["description"]
