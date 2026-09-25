{{- /*
Every tunable is resolved once, here, so an expression and the prose describing
it cannot disagree: the annotations quote these same values, and a retuned
deployment must not page someone with the default's numbers.

Defaults live in these two dicts. values.yaml leaves each key empty to mean
"keep the default". Sprig's `default` is not used because it treats 0 as empty,
and 0 is a legitimate threshold (backlogDepth: 0 alerts on any sustained growth).

Bad overrides are refused rather than shipped. A threshold is substituted into a
PromQL comparison, so a value that is not a number is not an error: `>
twelve_minutes` parses as a comparison against a metric that does not exist,
`promtool check rules` reports SUCCESS, and the alert never fires again with
nothing to say so. Prometheus duration literals are allowed because `12m` and
`720` are genuinely equivalent there. A key that matches nothing is refused too:
it renders the default, and the operator believes the override took effect.
*/}}
{{- $cfg := .Values.monitoring.prometheusRule }}
{{- $t := dict "ingestIdleSeconds" 720 "ingestFailureRatio" 0.25 "ingestVolumeFloor" 5 "backlogDepth" 50 "inferenceErrorRatio" 0.5 "inferenceVolumeFloor" 5 }}
{{- range $name, $value := ($cfg.thresholds | default dict) }}
{{- if not (hasKey $t $name) }}
{{- fail (printf "monitoring.prometheusRule.thresholds.%s is not a threshold in this chart, so setting it would do nothing. Known thresholds: %s" $name (join ", " (keys $t | sortAlpha))) }}
{{- end }}
{{- if not (or (kindIs "invalid" $value) (eq (toString $value) "")) }}
{{- if not (regexMatch `^([0-9]+(\.[0-9]+)?|([0-9]+(ms|[smhdwy]))+)$` (toString $value)) }}
{{- fail (printf "monitoring.prometheusRule.thresholds.%s must be a number (e.g. 720, 0.25) or a Prometheus duration (e.g. 12m), got %q. Anything else becomes a metric name in the comparison, and the alert silently never fires." $name (toString $value)) }}
{{- end }}
{{- $_ := set $t $name $value }}
{{- end }}
{{- end }}
{{- $for := dict "OpenRagIngestStalled" "2m" "OpenRagIngestFailureRate" "5m" "OpenRagBacklogGrowing" "25m" "OpenRagCatalogDriftDetected" "5m" "OpenRagInferenceProviderDown" "5m" "OpenRagCircuitBreakerOpen" "5m" "OpenRagTargetDown" "5m" }}
{{- range $name, $value := ($cfg.for | default dict) }}
{{- if not (hasKey $for $name) }}
{{- fail (printf "monitoring.prometheusRule.for.%s is not an alert in this chart, so setting it would do nothing. Known alerts: %s" $name (join ", " (keys $for | sortAlpha))) }}
{{- end }}
{{- if not (or (kindIs "invalid" $value) (eq (toString $value) "")) }}
{{- if not (regexMatch `^([0-9]+(ms|[smhdwy]))+$` (toString $value)) }}
{{- fail (printf "monitoring.prometheusRule.for.%s must be a Prometheus duration such as \"5m\", got %q." $name (toString $value)) }}
{{- end }}
{{- $_ := set $for $name (toString $value) }}
{{- end }}
{{- end }}
{{- /* The same values in words, for the annotations. A bare number of idle
   seconds reads as minutes when it divides evenly; anything more exotic than a
   single-unit duration is quoted as written. */}}
{{- /* The idle threshold again, as a range for min_over_time: a bare number of
   seconds becomes "<n>s", a duration literal is used as written. */}}
{{- $idleRange := toString $t.ingestIdleSeconds }}
{{- if regexMatch `^[0-9]+(\.[0-9]+)?$` $idleRange }}
{{- $idleRange = printf "%ds" (int (float64 $idleRange)) }}
{{- end }}
{{- $idle := toString $t.ingestIdleSeconds }}
{{- if regexMatch `^[0-9]+$` $idle }}
{{- $n := atoi $idle }}
{{- $idle = ternary (printf "%dm" (div $n 60)) (printf "%ds" $n) (and (gt $n 0) (eq (mod $n 60) 0)) }}
{{- end }}
{{- $units := dict "s" "second" "m" "minute" "h" "hour" "d" "day" }}
{{- $words := dict }}
{{- range $name, $d := (merge (dict "idle" $idle) $for) }}
{{- $text := $d }}
{{- if regexMatch `^[0-9]+[smhd]$` $d }}
{{- $n := regexReplaceAll `[smhd]$` $d "" }}
{{- $text = printf "%s %s%s" $n (get $units (regexReplaceAll `^[0-9]+` $d "")) (ternary "" "s" (eq $n "1")) }}
{{- end }}
{{- $_ := set $words $name $text }}
{{- end }}
{{- $pct := dict }}
{{- range $name := list "ingestFailureRatio" "inferenceErrorRatio" }}
{{- $_ := set $pct $name (printf "%v%%" (round (mulf (float64 (get $t $name)) 100) 2)) }}
{{- end -}}
# OpenRag alert rules — a Helm template, and the single source of truth.
#
# Thresholds and `for` durations are values because they are deployment
# config, not constants: they depend on a client's SLO, the size of the
# corpus and how many people are querying it. Every default here was
# measured against synthetic traffic, so treat them as a starting point.
#
# `{{ "{{ $value }}" }}` and friends are Prometheus' templating, escaped so
# Helm emits them literally instead of failing on an undefined variable.
#
# One source of truth, two consumers:
#   * Kubernetes — templates/prometheusrule.yaml wraps these groups in a
#     monitoring.coreos.com/v1 PrometheusRule via .Files.Get.
#   * Compose    — loads infra/compose/prometheus/rules/openrag-alerts.yaml,
#     which is GENERATED from this file with default values by
#     scripts/gen_alert_rules.py. Never edit that copy; CI regenerates it
#     and fails on any difference.
#
# Metric names and label values follow core/observability/metric_specs.py.
# Nothing here may reference a caller-controlled label (partition, user_id,
# file_id, task_id, request_id, filename): those are unbounded by construction,
# and a recording rule or alert that groups by one reintroduces the cardinality
# the metric design excluded. tests/unit/infra/test_alert_rules.py enforces it.
#
# KNOWN LIMITATION — Ray Serve multi-replica. Every expression below reads
# per-process counters. With ENABLE_RAY_SERVE=true and num_replicas > 1
# (api/main.py), a scrape reaches one replica at random, so rates and gauges are
# a random 1/N sample that appears to reset between scrapes. These rules will
# both miss real conditions and fire on phantom ones under that topology. Run
# num_replicas=1, or treat the alerts as advisory, until per-replica scraping
# exists.

groups:
  # ── Ingestion ────────────────────────────────────────────────────────────
  - name: openrag-ingestion
    rules:
      - alert: OpenRagIngestStalled
        # Work is waiting and nothing has finished parsing for 12 minutes.
        # The gauge is a completion *timestamp*, not a seconds-since counter, so
        # the age is computed here at evaluation time — a seconds-since gauge
        # reads 0 when nothing updates it, which is exactly the wedged-pool
        # condition this detects.
        #
        # max() over pools, not per pool: one idle pool is normal (no audio
        # uploads today), every pool idle while work is queued is not. If no
        # pool has *ever* completed a parse the gauge is absent and this cannot
        # fire — OpenRagBacklogGrowing covers a queue that rises from zero.
        #
        # The queue must have been non-empty for the whole idle window, not just
        # now. After a quiet night the last parse is hours old, so "queued now"
        # alone paged two minutes into the next batch, before its first parse
        # could finish. Waiting the idle window keeps the meaning — work waited
        # that long and nothing finished — and a real stall still fires then.
        #
        # max by (state): every API replica exports the same count, read from
        # the jobs table, and one series per pod raised one alert per pod.
        expr: |
          max by (state) (openrag_ingest_tasks{state="QUEUED"}) > 0
          and on()
          max(min_over_time(openrag_ingest_tasks{state="QUEUED"}[{{ $idleRange }}])) > 0
          and on()
          (time() - max(openrag_ingest_last_parse_completion_timestamp_seconds) > {{ $t.ingestIdleSeconds }})
        for: {{ $for.OpenRagIngestStalled }}
        labels:
          severity: critical
        annotations:
          summary: "Ingestion is stalled — documents are queued and nothing is completing"
          description: >-
            {{ "{{ $value }}" }} task(s) are QUEUED and no parser pool has completed a parse
            for over {{ $words.idle }}. Uploads are being accepted and never indexed.
          runbook_url: "{{ $cfg.runbookBaseUrl | default "https://github.com/linagora/openrag/blob/main/docs/deployment/runbooks" }}/OpenRagIngestStalled.md"

      - alert: OpenRagIngestFailureRate
        # Ratio over terminal outcomes only. `cancelled` is excluded from both
        # sides: a user cancelling an upload is not a failure, and counting it
        # would either dilute a real spike or page on ordinary behaviour.
        #
        # The volume floor stops 1-failed-of-2 from paging on a quiet instance.
        # When no documents finish at all the denominator is 0, the ratio is NaN
        # and no series is produced, so this stays silent rather than dividing by
        # zero — "nothing is completing" is OpenRagIngestStalled's job.
        #
        # The two windows differ on purpose. The ratio reads 5m so a real failure
        # is caught in ~8 minutes rather than ~15; the floor reads 15m because it
        # is counting whether enough work happened to judge at all, and a slow
        # instance that finishes one document every few minutes never reaches
        # five inside a 5m window — measured, it is never detected at all. `for`
        # is what rejects a brief blip, so shortening the ratio window costs no
        # stability.
        expr: |
          (
            sum(rate(openrag_ingest_documents_total{status="failed"}[5m]))
            /
            sum(rate(openrag_ingest_documents_total{status=~"completed|failed"}[5m]))
          ) > {{ $t.ingestFailureRatio }}
          and
          sum(increase(openrag_ingest_documents_total{status=~"completed|failed"}[15m])) >= {{ $t.ingestVolumeFloor }}
        for: {{ $for.OpenRagIngestFailureRate }}
        labels:
          severity: warning
        annotations:
          summary: "More than {{ $pct.ingestFailureRatio }} of documents are failing to index"
          description: >-
            {{ "{{ $value | humanizePercentage }}" }} of documents reaching a terminal state
            over the last 5 minutes failed.
          runbook_url: "{{ $cfg.runbookBaseUrl | default "https://github.com/linagora/openrag/blob/main/docs/deployment/runbooks" }}/OpenRagIngestFailureRate.md"

      - alert: OpenRagBacklogGrowing
        # Both conditions required: either alone is noisy — a burst upload
        # rises steeply and drains fine, a steady small queue is healthy.
        # Depth is on the left because `and` takes its left-hand side's value,
        # which is what puts the queue depth in the annotation rather than the
        # derivative's per-second slope. Filtering is symmetric either way.
        #
        # The derivative window is deliberately SHORT. A 30m window lags: while
        # the queue is actively draining it still contains the earlier rise, so
        # the slope stays positive and the alert fires on a bulk import that is
        # clearing normally — measured, the 30m/15m pair false-alarmed on every
        # burst tested. At 5m the slope turns with the queue.
        #
        # `for` is then the only thing separating "growing" from "grew, now
        # draining", so it is long: 25m outlasts the arrival phase of the bulk
        # imports measured. A slower import whose queue climbs for longer than
        # that will still trip this, and no threshold on these two series can
        # prevent it — "2000 documents just arrived" and "we are underwater"
        # are the same shape. The signal that separates them is the age of the
        # oldest pending item, which this deployment cannot measure.
        #
        # max by (state): every API replica exports the same count, read from
        # the jobs table, so per-pod series raised one alert per pod.
        #
        # END OF LIFE: this rule reads the backlog the API counts from the
        # durable jobs table. Once ingestion sits behind a broker the backlog
        # lives there, openrag_ingest_tasks collapses to "work already pulled"
        # — bounded by prefetch, near-constant however deep the real queue is —
        # and this rule stops measuring anything. Retire it then in favour of
        # broker-native signals (queue depth, consumer lag, oldest unacked
        # message age); do not retune it.
        expr: |
          max by (state) (openrag_ingest_tasks{state="QUEUED"}) > {{ $t.backlogDepth }}
          and
          max by (state) (deriv(openrag_ingest_tasks{state="QUEUED"}[5m])) > 0
        for: {{ $for.OpenRagBacklogGrowing }}
        labels:
          severity: warning
        annotations:
          summary: "Indexing backlog is growing faster than it drains"
          description: >-
            The QUEUED task count has risen continuously for {{ $words.OpenRagBacklogGrowing }} and now stands at
            {{ "{{ $value }}" }}. Ingestion capacity is below the arrival rate.
          runbook_url: "{{ $cfg.runbookBaseUrl | default "https://github.com/linagora/openrag/blob/main/docs/deployment/runbooks" }}/OpenRagBacklogGrowing.md"

      - alert: OpenRagCatalogDriftDetected
        # openrag_retrieval_orphan_chunks_dropped_total counts retrieval hits
        # dropped because the file is absent from the catalog — vector store and
        # catalog disagree. Its own definition notes repeated retrievals can
        # count the same chunk again, so the rate is not a document count: only
        # "non-zero" is meaningful here, never a magnitude threshold.
        #
        # increase() over an hour, not rate() over the `for` duration. The
        # counter only moves when a query happens to touch an orphaned file, so
        # the signal is sparse and its frequency says nothing about how bad the
        # drift is. A `[15m]` window with `for: 15m` was self-defeating: one
        # drop holds the rate above zero for exactly 15 minutes and never quite
        # the 15 *continuous* minutes the timer wanted, so a single or sporadic
        # drop — the likeliest shape, an orphan nobody queries often — never
        # fired at all. Measured: it fired only when the orphan was retrieved
        # constantly.
        #
        # The hour is also about how this CLEARS. Drift needs manual
        # reconciliation, so an alert that resolves fifteen minutes after the
        # last query is reporting "nobody looked recently", not "it is fixed".
        # A longer window keeps it up between sporadic hits. It still cannot
        # distinguish the two — see the runbook.
        expr: sum(increase(openrag_retrieval_orphan_chunks_dropped_total[1h])) > 0
        for: {{ $for.OpenRagCatalogDriftDetected }}
        labels:
          severity: critical
        annotations:
          summary: "Retrieval is dropping chunks whose file is missing from the catalog"
          description: >-
            Retrieval dropped chunks in the last hour for files the catalog does not know
            about. Answers are quietly missing content, with no error to show for it.
          runbook_url: "{{ $cfg.runbookBaseUrl | default "https://github.com/linagora/openrag/blob/main/docs/deployment/runbooks" }}/OpenRagCatalogDriftDetected.md"

  # ── Inference ────────────────────────────────────────────────────────────
  - name: openrag-inference
    rules:
      - alert: OpenRagInferenceProviderDown
        # `circuit_open` is deliberately NOT in the numerator: it is a
        # consequence of the breaker, which OpenRagCircuitBreakerOpen detects.
        # Counting it here as well would make the ratio self-sustaining once the
        # breaker trips, so this could never clear on its own.
        #
        # `provider` is the registry entry name — admin-created, and the string
        # to look up when this fires. It is deliberately NOT unified with the
        # breaker's `name`, which is a code-defined kind; the two were once
        # projected onto one label and that made the annotation resolve to a
        # value that did not exist in the registry.
        #
        # The volume floor stops a single failed call on a quiet instance from
        # reading as 100% and paging: 1 timeout in 10 minutes is not "down".
        # A provider hard down under low traffic still fires once it has seen
        # the floor's worth of calls; the breaker needs 50 failures to open.
        #
        # The denominator and the floor count only the outcomes that judge the
        # provider: success, error, timeout. `cancelled` (the caller gave up),
        # `rejected` (a 4xx refusing that one request, which any user can
        # provoke) and `circuit_open` (never sent) say nothing about its
        # health. Counting them diluted a provider failing every call it
        # served below the ratio, and let calls it never judged make up the
        # volume floor.
        expr: |
          (
            sum by (provider) (rate(openrag_inference_requests_total{outcome=~"error|timeout"}[10m]))
            /
            sum by (provider) (rate(openrag_inference_requests_total{outcome=~"success|error|timeout"}[10m]))
          ) > {{ $t.inferenceErrorRatio }}
          and
          sum by (provider) (increase(openrag_inference_requests_total{outcome=~"success|error|timeout"}[10m])) >= {{ $t.inferenceVolumeFloor }}
        for: {{ $for.OpenRagInferenceProviderDown }}
        labels:
          severity: critical
        annotations:
          summary: "Inference provider {{ "{{ $labels.provider }}" }} is failing more than {{ $pct.inferenceErrorRatio }} of its calls"
          description: >-
            More than {{ $pct.inferenceErrorRatio }} of the calls to registry endpoint {{ "{{ $labels.provider }}" }} are
            returning errors or timing out. Chat and any indexing stage that depends on it
            will fail.
          runbook_url: "{{ $cfg.runbookBaseUrl | default "https://github.com/linagora/openrag/blob/main/docs/deployment/runbooks" }}/OpenRagInferenceProviderDown.md"

      - alert: OpenRagCircuitBreakerOpen
        # Separate from the error-rate alert on purpose. The two need different
        # first responses — a tripped breaker means OpenRag has *stopped*
        # calling, so calls return circuit_open immediately and the endpoint is
        # under no load from us — and a single unioned alert forced the runbook
        # to open with "work out which half fired".
        #
        # Being separate is also what makes an Alertmanager inhibit rule
        # possible: a breaker opening causes the error ratio to change, so the
        # routing can suppress the other alert for the same outage. You cannot
        # inhibit half a unioned alert.
        #
        # `name` is the breaker kind declared in services/inference — llm,
        # embedder, vlm, reranker. Code-defined and therefore bounded, but NOT
        # a registry entry name.
        #
        # `>= 1`, not `== 1`: half-open (2) is still tripped. A hard-down
        # provider cycles open → half-open for the length of one trial call →
        # open, and a trial that times out sits at 2 across whole scrapes.
        # Under `== 1` every such scrape reset the `for` timer, so the alert
        # could stay pending through exactly the outage it exists for.
        # Unknown (-1) stays out.
        expr: max by (name) (openrag_circuit_breaker_state) >= 1
        for: {{ $for.OpenRagCircuitBreakerOpen }}
        labels:
          severity: critical
        annotations:
          summary: "The {{ "{{ $labels.name }}" }} circuit breaker is open"
          description: >-
            OpenRag has stopped calling its {{ "{{ $labels.name }}" }} endpoint after repeated
            failures. Calls return immediately without reaching it, so chat and indexing
            that depend on it fail fast.
          runbook_url: "{{ $cfg.runbookBaseUrl | default "https://github.com/linagora/openrag/blob/main/docs/deployment/runbooks" }}/OpenRagCircuitBreakerOpen.md"

  # ── Meta ─────────────────────────────────────────────────────────────────
  - name: openrag-meta
    rules:
      - alert: OpenRagTargetDown
        # The meta-alert: without it, every rule above fails silent. A target
        # that stops answering produces no series, and "no series" is
        # indistinguishable from "healthy" to every other expression here.
        #
        # This is NOT OpenRagNotReady. `up` reports whether /metrics answered,
        # and /metrics deliberately keeps answering while the container is
        # degraded (it reads process config, not the service container) — so a
        # degraded instance is up==1 and not ready at the same time. Readiness
        # needs a gauge exported from readiness_service; see S3-1b.
        #
        # With monitoring.bundled the release also runs kube-prometheus-stack, and
        # its own jobs (openrag-grafana, openrag-monitoring-*) match the default
        # matcher. They are the stack's, not OpenRAG's, and paging for them here
        # would claim every OpenRAG alert is inert when none is: upstream's own
        # TargetDown covers them. The Compose copy renders without `bundled`, so
        # its expression is unchanged.
        #
        # What counts as OpenRAG's own target, by default:
        #   * `.*openrag.*` — the API (Compose `openrag`, chart `<release>-openrag`)
        #     and the chart's Ray PodMonitor (`<namespace>/<release>-raycluster`);
        #   * `ray` — the Compose job scraping Ray's metrics agent, which carries
        #     every Ray-side series (ingest outcomes, parse completions,
        #     worker-side inference). Missing it left IngestStalled and
        #     IngestFailureRate inert with nothing paging for it.
        # The datastore exporters (`<release>-postgresql-metrics`,
        # `<release>-milvus*`) match `.*openrag.*` by release name but are not
        # OpenRAG's: their being down makes no OpenRAG alert inert, which is what
        # this alert's description tells the reader. They are always left out.
        {{- $jobExclude := list ".*-(postgresql|milvus)(-.*)?" }}
        {{- if .Values.monitoring.bundled }}
        {{- $stack := .Values.kubePrometheusStack | toYaml | fromYaml }}
        {{- with dig "fullnameOverride" "" $stack }}{{ $jobExclude = append $jobExclude (printf "%s-.*" (regexQuoteMeta .)) }}{{ end }}
        {{- with dig "grafana" "fullnameOverride" "" $stack }}{{ $jobExclude = append $jobExclude (regexQuoteMeta .) }}{{ end }}
        {{- end }}
        expr: up{job=~"{{ $cfg.jobMatcher | default ".*openrag.*|ray" }}"{{ with $jobExclude }}, job!~"{{ join "|" . }}"{{ end }}} == 0
        for: {{ $for.OpenRagTargetDown }}
        labels:
          severity: critical
        annotations:
          summary: "Prometheus cannot scrape OpenRag ({{ "{{ $labels.instance }}" }})"
          description: >-
            The target has been unreachable for {{ $words.OpenRagTargetDown }}. Every other OpenRag alert is
            inert while this is firing, because absent series cannot breach a threshold.
          runbook_url: "{{ $cfg.runbookBaseUrl | default "https://github.com/linagora/openrag/blob/main/docs/deployment/runbooks" }}/OpenRagTargetDown.md"
