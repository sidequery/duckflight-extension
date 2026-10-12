{{- define "duckflight.name" -}}
{{- printf "%s-duckflight" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "duckflight.selectorLabels" -}}
app.kubernetes.io/name: duckflight
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "duckflight.labels" -}}
{{ include "duckflight.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
{{- end -}}

{{- define "duckflight.serviceAccount" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "duckflight.name" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- required "serviceAccount.name is required when create=false" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "duckflight.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ required "image.tag or image.digest is required" .Values.image.tag }}
{{- end -}}
{{- end -}}
