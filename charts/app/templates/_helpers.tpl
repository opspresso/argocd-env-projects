{{/* Namespace app-<project>-<environment>; the Broker tenant secrets policy matches this name and its labels. */}}
{{- define "app.namespace" -}}
{{- $project := required "project is required" .Values.project -}}
{{- $environment := required "environment is required" .Values.environment -}}
{{- if not (regexMatch "^[a-z][a-z0-9-]{1,30}[a-z0-9]$" $project) -}}
{{- fail (printf "project %q is not a Broker Project slug" $project) -}}
{{- end -}}
{{- if not (has $environment (list "dev" "prod")) -}}
{{- fail (printf "environment %q must be dev or prod" $environment) -}}
{{- end -}}
{{- if ne .Release.Namespace (printf "app-%s-%s" $project $environment) -}}
{{- fail (printf "release namespace %q must be app-%s-%s" .Release.Namespace $project $environment) -}}
{{- end -}}
{{- .Release.Namespace -}}
{{- end -}}

{{/* The public host. The chart derives it so that a values file cannot claim another service's host. */}}
{{- define "app.host" -}}
{{- $domain := required "domain is required" .Values.domain -}}
{{- if eq .Values.environment "prod" -}}
{{- printf "%s.%s" .Values.project $domain -}}
{{- else -}}
{{- printf "%s-%s.%s" .Values.project .Values.environment $domain -}}
{{- end -}}
{{- end -}}

{{- define "app.labels" -}}
app.kubernetes.io/name: app
app.kubernetes.io/instance: {{ include "app.namespace" . }}
app.kubernetes.io/part-of: {{ .Values.project }}
opspresso.com/broker-project: {{ .Values.project | quote }}
opspresso.com/broker-env: {{ .Values.environment | quote }}
{{- end -}}

{{- define "app.selector" -}}
app.kubernetes.io/name: app
app.kubernetes.io/instance: {{ include "app.namespace" . }}
{{- end -}}

{{/* The image pinned by digest. Tags are immutable in ECR, but the digest is what was tested and built. */}}
{{- define "app.image" -}}
{{- $repository := required "image.repository is required" .Values.image.repository -}}
{{- $digest := required "image.digest is required" .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $digest) -}}
{{- fail (printf "image.digest %q is not a sha256 digest" $digest) -}}
{{- end -}}
{{- printf "%s@%s" $repository $digest -}}
{{- end -}}

{{- define "app.replicas" -}}
{{- .Values.application.environment.replicas | default 1 | int -}}
{{- end -}}

{{- define "app.maxReplicas" -}}
{{- .Values.application.environment.maxReplicas | default (include "app.replicas" .) | int -}}
{{- end -}}
