{{- define "ociproxy.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ociproxy.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "ociproxy.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "ociproxy.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "ociproxy.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ociproxy.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Selector labels of one component: primary | replica | ui. */}}
{{- define "ociproxy.componentSelector" -}}
{{ include "ociproxy.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "ociproxy.secretName" -}}
{{- .Values.auth.existingSecret | default (printf "%s-auth" (include "ociproxy.fullname" .)) -}}
{{- end -}}

{{- define "ociproxy.configSecretName" -}}
{{- printf "%s-config" (include "ociproxy.fullname" .) -}}
{{- end -}}

{{- define "ociproxy.headless" -}}
{{- printf "%s-headless" (include "ociproxy.fullname" .) -}}
{{- end -}}

{{- define "ociproxy.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "ociproxy.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "ociproxy.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}

{{/* In-cluster URL of the primary: an explicit URL wins, else this release's Service. */}}
{{- define "ociproxy.primaryServiceUrl" -}}
{{- printf "http://%s-primary.%s.svc.%s:%v" (include "ociproxy.fullname" .) .Release.Namespace .Values.clusterDomain .Values.primary.service.port -}}
{{- end -}}

{{- define "ociproxy.replicaPrimaryUrl" -}}
{{- if .Values.replica.primaryUrl -}}
{{- .Values.replica.primaryUrl -}}
{{- else if .Values.primary.enabled -}}
{{- include "ociproxy.primaryServiceUrl" . -}}
{{- else -}}
{{- fail "replica.primaryUrl is required when primary.enabled=false" -}}
{{- end -}}
{{- end -}}

{{/*
Value of a key in the chart-managed auth Secret: the explicit value, else
what an earlier install stored (so upgrades keep it), else random.
*/}}
{{- define "ociproxy.persistedValue" -}}
{{- $existing := lookup "v1" "Secret" .root.Release.Namespace (include "ociproxy.secretName" .root) -}}
{{- $old := "" -}}
{{- if and $existing $existing.data -}}
{{- $old = get $existing.data .key | b64dec -}}
{{- end -}}
{{- .value | default $old | default (randAlphaNum 48) -}}
{{- end -}}

{{/* Environment shared by every proxy node. */}}
{{- define "ociproxy.nodeEnv" -}}
- name: OCIPROXY_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "ociproxy.secretName" . }}
      key: secret
- name: OCIPROXY_PUSH_USERNAME
  value: {{ .Values.auth.pushUsername | quote }}
- name: OCIPROXY_PUSH_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "ociproxy.secretName" . }}
      key: push-password
- name: OCIPROXY_REPLICATION_PEERS
  valueFrom:
    secretKeyRef:
      name: {{ include "ociproxy.secretName" . }}
      key: replication-peers
{{- if .Values.replica.enabled }}
- name: OCIPROXY_REPLICATION_ID
  value: {{ .Values.replication.id | default .Release.Name | quote }}
- name: OCIPROXY_REPLICATION_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "ociproxy.secretName" . }}
      key: replication-token
{{- end }}
- name: OCIPROXY_DEFAULT_VISIBILITY
  value: {{ .Values.config.defaultVisibility | quote }}
- name: OCIPROXY_TAG_TTL
  value: {{ .Values.config.tagTTL | quote }}
- name: OCIPROXY_GC_INTERVAL
  value: {{ .Values.config.gcInterval | quote }}
- name: OCIPROXY_GC_MANIFEST_MAX_IDLE_DAYS
  value: {{ .Values.config.gcManifestMaxIdleDays | quote }}
- name: OCIPROXY_LOG_FORMAT
  value: {{ .Values.config.logFormat | quote }}
- name: OCIPROXY_CONFIG
  value: /app/ociproxy.toml
- name: OCIPROXY_MANAGED_SOURCES
  value: {{ .Values.config.managedSources | quote }}
{{- with .Values.config.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* Probes against /v2/, which is public whatever the visibility. */}}
{{- define "ociproxy.probes" -}}
startupProbe:
  httpGet: {path: /v2/, port: http}
  periodSeconds: 2
  failureThreshold: 60
readinessProbe:
  httpGet: {path: /v2/, port: http}
  periodSeconds: 10
  timeoutSeconds: 5
livenessProbe:
  httpGet: {path: /v2/, port: http}
  periodSeconds: 20
  timeoutSeconds: 10
  failureThreshold: 6
{{- end -}}

{{/* Volumes every proxy node mounts besides its data. */}}
{{- define "ociproxy.nodeVolumeMounts" -}}
- name: data
  mountPath: /var/lib/ociproxy
- name: config
  mountPath: /app/ociproxy.toml
  subPath: ociproxy.toml
  readOnly: true
- name: tmp
  mountPath: /tmp
{{- end -}}

{{- define "ociproxy.podChecksums" -}}
checksum/config: {{ include (print .Template.BasePath "/config-secret.yaml") . | sha256sum }}
{{- if not .Values.auth.existingSecret }}
checksum/auth: {{ .Values.auth | toJson | sha256sum }}
{{- end }}
{{- end -}}
