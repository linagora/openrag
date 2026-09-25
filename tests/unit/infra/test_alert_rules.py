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
import subprocess
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
        "monitoring.prometheusRule.thresholds.inferenceVolumeFloor=0",
    )
    assert 'max by (state) (openrag_ingest_tasks{state="QUEUED"}) > 0\n' in rules["OpenRagBacklogGrowing"]["expr"]
    assert rules["OpenRagIngestFailureRate"]["expr"].rstrip().endswith(">= 0")
    assert rules["OpenRagInferenceProviderDown"]["expr"].rstrip().endswith(">= 0")


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


# ---------------------------------------------------------------------------
# The override guard refuses bad values, not just unknown keys (#976 review)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ("monitoring.prometheusRule.thresholds.ingestIdleSeconds=twelve_minutes", "must be a whole number"),
        ("monitoring.prometheusRule.for.OpenRagIngestStalled=soon", "must be a Prometheus duration"),
        ("monitoring.prometheusRule.for.NoSuchAlert=5m", "is not an alert"),
    ],
)
def test_a_bad_override_is_refused_not_rendered(override: str, message: str) -> None:
    """A threshold is substituted into a PromQL comparison, so a non-number is
    not an error: `> twelve_minutes` compares against a metric that does not
    exist, promtool reports SUCCESS, and the alert never fires again with
    nothing to say so. Only the unknown-*key* case was covered; these are the
    three paths that were not.
    """
    with pytest.raises(SystemExit, match=message):
        _render(override)


_THRESHOLD = "monitoring.prometheusRule.thresholds."
_FOR = "monitoring.prometheusRule.for."


@pytest.mark.parametrize(
    ("override", "message"),
    [
        # A ratio compared against a duration (`> 5m` is 300) or above 1 can
        # never be reached.
        (f"{_THRESHOLD}ingestFailureRatio=5m", r"thresholds\.ingestFailureRatio must be a plain number from 0 to 1"),
        (f"{_THRESHOLD}ingestFailureRatio=1.5", r"thresholds\.ingestFailureRatio must be a plain number from 0 to 1"),
        (f"{_THRESHOLD}inferenceErrorRatio=2", r"thresholds\.inferenceErrorRatio must be a plain number from 0 to 1"),
        (f"{_THRESHOLD}inferenceErrorRatio=10s", r"thresholds\.inferenceErrorRatio must be a plain number from 0 to 1"),
        # Floors and depth count things, not time.
        (f"{_THRESHOLD}ingestVolumeFloor=5m", r"thresholds\.ingestVolumeFloor must be a plain non-negative number"),
        (f"{_THRESHOLD}inferenceVolumeFloor=1h", r"thresholds\.inferenceVolumeFloor must be a plain non-negative"),
        (f"{_THRESHOLD}backlogDepth=50s", r"thresholds\.backlogDepth must be a plain non-negative number"),
        # The idle threshold is also a range selector: `[0s]` does not parse,
        # and the rule group containing it does not load at all.
        (f"{_THRESHOLD}ingestIdleSeconds=0", r"thresholds\.ingestIdleSeconds must be a whole number of seconds"),
        (f"{_THRESHOLD}ingestIdleSeconds=0s", r"thresholds\.ingestIdleSeconds must be a whole number of seconds"),
        (f"{_THRESHOLD}ingestIdleSeconds=0m", r"thresholds\.ingestIdleSeconds must be a whole number of seconds"),
        (f"{_THRESHOLD}ingestIdleSeconds=0.5", r"thresholds\.ingestIdleSeconds must be a whole number of seconds"),
        (f"{_THRESHOLD}ingestIdleSeconds=30m1h", r"thresholds\.ingestIdleSeconds must be a whole number of seconds"),
        # `for: 0s` fires on one bad evaluation; `30m1h` does not parse.
        (f"{_FOR}OpenRagIngestStalled=0s", r"for\.OpenRagIngestStalled must be a Prometheus duration greater than 0"),
        (f"{_FOR}OpenRagTargetDown=0m", r"for\.OpenRagTargetDown must be a Prometheus duration greater than 0"),
        (f"{_FOR}OpenRagBacklogGrowing=30m1h", r"for\.OpenRagBacklogGrowing must be a Prometheus duration greater"),
    ],
)
def test_each_threshold_accepts_only_the_form_its_expression_can_use(override: str, message: str) -> None:
    """One pattern for every key accepted `ingestFailureRatio=5m` — valid
    PromQL, a ratio compared against 300, an alert that can never fire — and
    `ingestIdleSeconds=0`, which renders `[0s]` and fails the whole group."""
    with pytest.raises(SystemExit, match=message):
        _render(override)


@pytest.mark.parametrize(
    ("override", "alert", "rendered"),
    [
        (f"{_THRESHOLD}ingestFailureRatio=0", "OpenRagIngestFailureRate", ") > 0\n"),
        (f"{_THRESHOLD}ingestFailureRatio=1", "OpenRagIngestFailureRate", ") > 1\n"),
        (f"{_THRESHOLD}inferenceErrorRatio=0.05", "OpenRagInferenceProviderDown", ") > 0.05\n"),
        (f"{_THRESHOLD}ingestVolumeFloor=12.5", "OpenRagIngestFailureRate", ">= 12.5"),
        (f"{_THRESHOLD}backlogDepth=200", "OpenRagBacklogGrowing", "> 200\n"),
        (f"{_THRESHOLD}ingestIdleSeconds=1800", "OpenRagIngestStalled", "[1800s]"),
        (f"{_THRESHOLD}ingestIdleSeconds=20m", "OpenRagIngestStalled", "[20m]"),
        (f"{_THRESHOLD}ingestIdleSeconds=1h30m", "OpenRagIngestStalled", "> 1h30m)"),
    ],
)
def test_a_threshold_in_its_accepted_form_is_rendered(override: str, alert: str, rendered: str) -> None:
    assert rendered in _render(override)[alert]["expr"]


@pytest.mark.parametrize("duration", ["30s", "1h30m", "0m5s"])
def test_a_positive_for_duration_is_rendered(duration: str) -> None:
    assert _render(f"{_FOR}OpenRagTargetDown={duration}")["OpenRagTargetDown"]["for"] == duration


def test_an_unknown_breaker_state_does_not_fire() -> None:
    """`_STATE_VALUES` maps an unrecognised aiobreaker state to -1, so -1 is
    reachable whenever that library adds or renames one. The rule uses `>= 1`
    precisely so unknown stays out; nothing pinned that, and `!= 0` passes the
    whole suite while paging for a healthy provider.
    """
    expr = next(r["expr"] for r in _all_rules() if r["alert"] == "OpenRagCircuitBreakerOpen")

    assert ">= 1" in expr, f"breaker alert no longer excludes the unknown (-1) state: {expr}"
    assert "!= 0" not in expr


# ---------------------------------------------------------------------------
# The bundled stack (monitoring.bundled)
# ---------------------------------------------------------------------------


def test_bundled_turns_the_rules_on() -> None:
    """Like the dashboards and the API monitor: a standalone stack with no alert
    rules looks exactly like a healthy one."""
    rules = _render("monitoring.prometheusRule.enabled=false", "monitoring.bundled=true")

    assert "OpenRagTargetDown" in rules


def _paged(expr: str, job: str) -> bool:
    """Whether OpenRagTargetDown's selector matches ``job``. PromQL anchors its
    regexes, which `re.fullmatch` reproduces."""
    include = re.search(r'job=~"([^"]+)"', expr).group(1)
    exclude = re.search(r'job!~"([^"]+)"', expr)
    return bool(re.fullmatch(include, job)) and not (exclude and re.fullmatch(exclude.group(1), job))


def test_target_down_leaves_the_bundled_stacks_own_jobs_out() -> None:
    """The bundled Grafana, Alertmanager, operator and Prometheus all have
    `openrag` in their job, and their being down does not make an OpenRAG alert
    inert, which is what this alert's runbook tells the reader."""
    expr = _render("monitoring.bundled=true")["OpenRagTargetDown"]["expr"]

    for job in ("openrag-openrag", "ns/openrag-raycluster"):
        assert _paged(expr, job), f"{job} is OpenRAG's and must still page: {expr}"
    for job in (
        "openrag-grafana",
        "openrag-monitoring-alertmanager",
        "openrag-monitoring-operator",
        "openrag-monitoring-prometheus",
    ):
        assert not _paged(expr, job), f"{job} is the bundled stack's, not OpenRAG's: {expr}"


def test_target_down_excludes_only_the_datastores_without_the_bundled_stack() -> None:
    """The Compose copy is generated without `bundled`: nothing of the stack's
    is excluded there, only the datastore exporters, which are never OpenRAG's."""
    expr = _render()["OpenRagTargetDown"]["expr"]

    assert "monitoring" not in expr and "grafana" not in expr
    assert _paged(expr, "openrag")


@pytest.mark.parametrize("bundled", [False, True])
@pytest.mark.parametrize(
    ("job", "pages"),
    [
        # OpenRAG's own targets: the API, and wherever the Ray-side series
        # (ingest outcomes, parse completions) are scraped from.
        ("openrag", True),
        ("ray", True),
        ("openrag-openrag", True),
        ("rag/openrag-raycluster", True),
        # #979's datastore exporters carry the release name but are not OpenRAG.
        ("openrag-postgresql-metrics", False),
        ("openrag-milvus", False),
        ("openrag-milvus-querynode", False),
        ("node-exporter", False),
    ],
)
def test_target_down_pages_for_openrags_targets_only(job: str, pages: bool, bundled: bool) -> None:
    """`.*openrag.*` alone missed the Compose `ray` job — the one carrying the
    ingest metrics — and caught the datastore exporters, paging "every OpenRag
    alert is inert" when none was."""
    overrides = ("monitoring.bundled=true",) if bundled else ()
    expr = _render(*overrides)["OpenRagTargetDown"]["expr"]

    assert _paged(expr, job) is pages, f"{job}: expected pages={pages} under {expr}"


# ---------------------------------------------------------------------------
# What the chart itself renders (#976 second audit)
# ---------------------------------------------------------------------------

#: Enough to render every object the runbooks point at: the rules, the API, and
#: the RayCluster with its PodMonitor.
_CHART_SETS = (
    "env.secrets.AUTH_TOKEN=or-unit-test-token-0123",
    "postgresql.auth.password=unit-test-password-0123",
    "monitoring.prometheusRule.enabled=true",
    "ray.enabled=true",
    "ray.metrics.podMonitor.enabled=true",
)


def _render_chart(tmp_path: Path, *overrides: str) -> list[dict]:
    """Every object ``helm template`` renders, from a copy of the chart without
    its subcharts: they are fetched archives, and nothing here comes from them."""
    if shutil.which("helm") is None:
        pytest.skip("needs helm to render the chart")
    chart = tmp_path / "openrag-stack"
    for directory in ("templates", "rules", "dashboards"):
        shutil.copytree(CHART_DIR / directory, chart / directory)
    shutil.copy(CHART_DIR / "values.yaml", chart / "values.yaml")
    meta = yaml.safe_load((CHART_DIR / "Chart.yaml").read_text(encoding="utf-8"))
    meta.pop("dependencies", None)
    (chart / "Chart.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")

    sets = (arg for value in (*_CHART_SETS, *overrides) for arg in ("--set", value))
    result = subprocess.run(
        [shutil.which("helm"), "template", "openrag", str(chart), "--namespace", "rag", *sets],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _one(objects: list[dict], kind: str) -> dict:
    found = [obj for obj in objects if obj["kind"] == kind]
    assert len(found) == 1, f"expected one {kind}, rendered {[obj['metadata']['name'] for obj in found]}"
    return found[0]


#: Alerts allowed to render without a `for`, i.e. to fire on one evaluation.
#: Empty: every shipped alert must wait.
_FIRES_INSTANTLY: frozenset[str] = frozenset()


@pytest.mark.parametrize(
    "overrides",
    [(), ("monitoring.prometheusRule.commonLabels.team=platform",)],
    ids=["rules-as-text", "rules-with-common-labels"],
)
def test_every_rule_the_chart_renders_waits_before_firing(tmp_path: Path, overrides: tuple[str, ...]) -> None:
    """An alert whose key is missing from the template's `$for` dict renders a
    bare `for:` — null, which Prometheus reads as "fire on one evaluation".
    Checked on the PrometheusRule itself, under both of its rendering branches,
    not only on the Compose copy, which is regenerated after the fact."""
    rule_object = _one(_render_chart(tmp_path, *overrides), "PrometheusRule")

    for group in rule_object["spec"]["groups"]:
        for rule in group["rules"]:
            if rule["alert"] in _FIRES_INSTANTLY:
                continue
            assert rule.get("for"), f"{rule['alert']} renders `for: {rule.get('for')}` and fires on one evaluation"


def test_values_lists_every_alerts_default_for(tmp_path: Path) -> None:
    """values.yaml leaves `for` empty and lists the defaults in a comment, which
    is the only place an operator retuning one reads them."""
    listed = dict(
        re.findall(
            r"(OpenRag\w+): (\S+)",
            (CHART_DIR / "values.yaml").read_text(encoding="utf-8").split("    for: {}")[0].rsplit("thresholds:", 1)[1],
        )
    )
    rule_object = _one(_render_chart(tmp_path), "PrometheusRule")
    rendered = {rule["alert"]: rule["for"] for group in rule_object["spec"]["groups"] for rule in group["rules"]}

    assert listed == rendered, "values.yaml's `for` list and the template's `$for` defaults disagree"
