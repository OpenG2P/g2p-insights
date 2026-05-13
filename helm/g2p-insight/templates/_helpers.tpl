{{/*
Shared helpers for the g2p-insight umbrella chart.
*/}}

{{- define "g2p-insight.fullname" -}}
{{- printf "%s" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "g2p-insight.labels" -}}
app.kubernetes.io/name: g2p-insight
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
