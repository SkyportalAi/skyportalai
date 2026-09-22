{{/*
Chart name, truncated to the 63 char label limit.
*/}}
{{- define "skyportalai-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully qualified app name. Truncated to 63 chars for resource names.
*/}}
{{- define "skyportalai-agent.fullname" -}}
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
Chart label value (name-version).
*/}}
{{- define "skyportalai-agent.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "skyportalai-agent.labels" -}}
helm.sh/chart: {{ include "skyportalai-agent.chart" . }}
{{ include "skyportalai-agent.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels (stable across upgrades; do not add version here).
*/}}
{{- define "skyportalai-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "skyportalai-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the ServiceAccount to use.
*/}}
{{- define "skyportalai-agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "skyportalai-agent.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Selector labels for the Kubernetes monitoring workloads (#3566). A distinct name,
not a component label on the shared one: the experiment Deployment's selector
(name + instance) is immutable, and pods carrying its labels would match it too.
Call with (dict "root" $ "component" "cluster").
*/}}
{{- define "skyportalai-agent.componentSelectorLabels" -}}
app.kubernetes.io/name: {{ printf "%s-%s" (include "skyportalai-agent.name" .root) .component | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "skyportalai-agent.componentLabels" -}}
helm.sh/chart: {{ include "skyportalai-agent.chart" .root }}
{{ include "skyportalai-agent.componentSelectorLabels" . }}
{{- if .root.Chart.AppVersion }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
{{- end }}

{{/*
Image reference, shared by every workload in the chart.
*/}}
{{- define "skyportalai-agent.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end }}

{{/*
The Kubernetes roles ship in agent 0.3.0. Refuse to install them on an older image
rather than let the pods crash loop on an unknown role. A tag that is not a plain
version (a digest-style or custom tag) is trusted as-is.
*/}}
{{- define "skyportalai-agent.requireKubernetesImage" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if and (regexMatch "^v?[0-9]+\\.[0-9]+\\.[0-9]+$" $tag) (semverCompare "<0.3.0" $tag) -}}
{{- fail (printf "kubernetes.enabled needs skyportalai-agent 0.3.0 or newer; image tag %s predates it. Set image.tag." $tag) -}}
{{- end -}}
{{- end }}

{{/*
Env shared by the Kubernetes roles: the token and the API root.
*/}}
{{- define "skyportalai-agent.kubernetesEnv" -}}
- name: SKYPORTALAI_AGENT_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ required "token.existingSecret is required" .Values.token.existingSecret }}
      key: {{ .Values.token.secretKey }}
{{- with .Values.config.baseUrl }}
- name: SKYPORTALAI_BASE_URL
  value: {{ . | quote }}
{{- end }}
- name: SKYPORTALAI_AGENT_HEALTHZ_PORT
  value: {{ .Values.config.healthzPort | quote }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}
