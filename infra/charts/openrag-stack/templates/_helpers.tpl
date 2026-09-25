{{/*
Expand the name of the chart.
*/}}
{{- define "openrag-stack.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "openrag-stack.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "openrag-stack.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "openrag-stack.labels" -}}
helm.sh/chart: {{ include "openrag-stack.chart" . }}
{{ include "openrag-stack.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "openrag-stack.selectorLabels" -}}
app.kubernetes.io/name: {{ include "openrag-stack.name" . }}
app.kubernetes.io/instance: {{ include "openrag-stack.fullname" . }}
{{- end }}

{{/*
Component-scoped selector labels. app.kubernetes.io/name identifies the
specific workload ("openrag", "admin-ui", "reranker", ...) instead of the
umbrella chart name, so e.g. `kubectl get pods -l app.kubernetes.io/name=admin-ui`
targets one component — every workload template should use this (and
"componentLabels" below) instead of hand-rolling its own label block.
Usage: {{ include "openrag-stack.componentSelectorLabels" (dict "component" "openrag" "context" $) }}
*/}}
{{- define "openrag-stack.componentSelectorLabels" -}}
app.kubernetes.io/name: {{ .component }}
app.kubernetes.io/instance: {{ include "openrag-stack.fullname" .context }}
{{- end }}

{{/*
Component-scoped common labels (componentSelectorLabels plus chart/version/managed-by).
Usage: {{ include "openrag-stack.componentLabels" (dict "component" "openrag" "context" $) }}
*/}}
{{- define "openrag-stack.componentLabels" -}}
helm.sh/chart: {{ include "openrag-stack.chart" .context }}
{{ include "openrag-stack.componentSelectorLabels" . }}
{{- if .context.Chart.AppVersion }}
app.kubernetes.io/version: {{ .context.Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .context.Release.Service }}
{{- end }}

{{/*
Environment secret name used by all consumers.
When env.existingSecret is set, that name is returned directly.
*/}}
{{- define "openrag-stack.secretName" -}}
{{- if .Values.env.existingSecret }}
{{- .Values.env.existingSecret }}
{{- else }}
{{- printf "%s-env-secrets" (include "openrag-stack.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Merge a component's security context override (e.g. just runAsUser/runAsGroup/
fsGroup, tuned to that component's own Dockerfile) on top of a shared default
from values.yaml's top-level `security` block — component keys win on
conflicts, everything else is inherited from the default.
Deliberately NOT Sprig's `merge` (mergo): mergo treats a zero value (false, 0,
"") as "unset" and overwrites it with the default, so an explicit
`allowPrivilegeEscalation: false` override would be silently discarded the
day the shared default becomes `true`. This does a presence-based (hasKey)
shallow merge instead, so an explicitly-set false/0/"" always wins.
Usage: {{ include "openrag-stack.mergeSecurityContext" (dict "component" .Values.ray.podSecurityContext "default" .Values.security.podSecurityContext) }}
*/}}
{{- define "openrag-stack.mergeSecurityContext" -}}
{{- $result := deepCopy .component -}}
{{- range $key, $value := .default -}}
{{- if not (hasKey $result $key) -}}
{{- $_ := set $result $key $value -}}
{{- end -}}
{{- end -}}
{{- $result | toYaml -}}
{{- end }}

{{/*
Component override for a single boolean/scalar security field (e.g.
automountServiceAccountToken): uses the component's own value only if the key
is explicitly present, otherwise falls back to the shared default. A plain
`| default` would treat an explicit `false` override as empty and silently
fall back anyway — this checks presence (hasKey) instead of truthiness.
Usage: {{ include "openrag-stack.securityFieldOverride" (dict "component" .Values.ray "key" "automountServiceAccountToken" "default" .Values.security.automountServiceAccountToken) }}
*/}}
{{- define "openrag-stack.securityFieldOverride" -}}
{{- if hasKey .component .key -}}
{{- get .component .key -}}
{{- else -}}
{{- .default -}}
{{- end -}}
{{- end }}

{{/*
Render an OpenRAG container probe. Ray Serve exposes the HTTP application from
the Ray head rather than from the OpenRAG driver pod, so Kubernetes must run
the request inside the container where cluster DNS can resolve the head
Service. Timing and threshold settings continue to come from openrag.probes.
*/}}
{{- define "openrag-stack.openragProbe" -}}
{{- $probe := deepCopy .probe -}}
{{- if .rayServe -}}
{{- $_ := unset $probe "httpGet" -}}
{{- $_ := unset $probe "tcpSocket" -}}
{{- $_ := unset $probe "grpc" -}}
{{- $host := printf "%s-raycluster-head-svc" (include "openrag-stack.fullname" .context) -}}
{{- $url := printf "http://%s:80%s" $host .path -}}
{{- $_ := set $probe "exec" (dict "command" (list "curl" "--fail" "--silent" "--show-error" $url)) -}}
{{- end -}}
{{- $probe | toYaml -}}
{{- end }}

{{/*
"true" when the API is served by Ray Serve replicas behind the RayCluster head
Service (port ray-serve) rather than by uvicorn on the openrag Deployment's
port 8080. Both switches are required: ray.enabled renders the RayCluster,
ENABLE_RAY_SERVE makes the app hand itself to serve.run. Anything that must
target "where the API actually listens" (Ingress backend, metrics discovery)
branches on this.
Usage: {{- if eq (include "openrag-stack.rayServeApi" .) "true" }}
*/}}
{{- define "openrag-stack.rayServeApi" -}}
{{- if and .Values.ray.enabled (eq (toString .Values.env.config.ENABLE_RAY_SERVE) "true") -}}true{{- else -}}false{{- end -}}
{{- end }}

{{/*
Port every Ray node exports its metrics on, head and workers alike, and the
embedded Ray inside the openrag pod when ray.enabled=false. Kept off
networkPolicy.externalPorts' 8080 on purpose: that rule matches by port number
across every pod in the namespace, and these metrics are unauthenticated.
KubeRay's own default is exactly 8080, so both halves of raycluster.yaml must
override it — see the comments there.
*/}}
{{- define "openrag-stack.rayMetricsPort" -}}
8090
{{- end }}

{{/*
Whether Ray's metrics get a PodMonitor. monitoring.bundled turns it on like the
API's ServiceMonitor: the ingestion and worker-side inference series exist on
Ray's endpoint only, so a stack that scrapes the API alone leaves the alerts
built on them unable to fire.
*/}}
{{- define "openrag-stack.rayPodMonitor" -}}
{{- if or .Values.ray.metrics.podMonitor.enabled .Values.monitoring.bundled -}}true{{- else -}}false{{- end -}}
{{- end }}
