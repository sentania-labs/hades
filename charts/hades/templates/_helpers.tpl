{{- define "hades.serviceImage" -}}
{{- printf "%s:%s" .Values.serviceImage.repository .Values.serviceImage.tag -}}{{- if .Values.serviceImage.digest -}}@{{ .Values.serviceImage.digest }}{{- end -}}
{{- end -}}
{{- define "hades.workerImage" -}}
{{- printf "%s:%s" .Values.workerImage.repository .Values.workerImage.tag -}}{{- if .Values.workerImage.digest -}}@{{ .Values.workerImage.digest }}{{- end -}}
{{- end -}}

{{/*
The prefix of every object name: the product name, `hades` (hades #609 step 2). The
`app.kubernetes.io/name` label stays `crucible` whatever the prefix, so a deployment
that keeps its earlier names through the overrides still matches its Deployments'
immutable selectors.
*/}}
{{- define "hades.name" -}}
{{- .Values.nameOverride | default "hades" -}}
{{- end -}}
{{- define "hades.namespace" -}}
{{- .Values.namespaceOverride | default (include "hades.name" .) -}}
{{- end -}}
{{- define "hades.workersNamespace" -}}
{{- .Values.workersNamespaceOverride | default (printf "%s-workers" (include "hades.namespace" .)) -}}
{{- end -}}

{{/*
A claim's storage class: its own, else storage.storageClass. Empty means no
storageClassName at all, so the cluster's default class answers.
*/}}
{{- define "hades.artifactsStorageClass" -}}
{{- .Values.storage.artifactsStorageClass | default .Values.storage.storageClass -}}
{{- end -}}
{{- define "hades.referenceCacheStorageClass" -}}
{{- .Values.storage.referenceCacheStorageClass | default .Values.storage.storageClass -}}
{{- end -}}
{{- define "hades.buildkitCacheStorageClass" -}}
{{- .Values.storage.buildkitCacheStorageClass | default .Values.storage.storageClass -}}
{{- end -}}
