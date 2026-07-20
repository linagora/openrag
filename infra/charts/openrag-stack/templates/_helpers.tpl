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
Usage: {{ include "openrag-stack.mergeSecurityContext" (dict "component" .Values.ray.podSecurityContext "default" .Values.security.podSecurityContext) }}
*/}}
{{- define "openrag-stack.mergeSecurityContext" -}}
{{- merge (deepCopy .component) .default | toYaml }}
{{- end }}
