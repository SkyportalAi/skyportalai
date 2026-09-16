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
