{{- define "hades.serviceImage" -}}
{{- printf "%s:%s" .Values.serviceImage.repository .Values.serviceImage.tag -}}{{- if .Values.serviceImage.digest -}}@{{ .Values.serviceImage.digest }}{{- end -}}
{{- end -}}
{{- define "hades.workerImage" -}}
{{- printf "%s:%s" .Values.workerImage.repository .Values.workerImage.tag -}}{{- if .Values.workerImage.digest -}}@{{ .Values.workerImage.digest }}{{- end -}}
{{- end -}}

