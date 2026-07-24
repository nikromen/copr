{{/*
Common labels applied to every resource, matching the Kustomize
`labels: [{pairs: {app: copr}, includeSelectors: false}]` transformer that
used to run over manifests/base.
*/}}
{{- define "copr.labels" -}}
app: copr
{{- end }}

{{/*
Namespace stamped onto every resource, matching the Kustomize
top-level `namespace: copr` transformer.
*/}}
{{- define "copr.namespace" -}}
{{ .Values.global.namespace }}
{{- end }}

{{/*
Fully-qualified image reference for a copr-* component, e.g.
"localhost/copr-frontend:latest" on openshift-local, "copr-frontend:latest"
for dev/dev-local.
Usage: {{ include "copr.image" (dict "root" $ "component" "frontend") }}
*/}}
{{- define "copr.image" -}}
{{ .root.Values.images.registry }}copr-{{ .component }}:{{ .root.Values.images.tag }}
{{- end }}
