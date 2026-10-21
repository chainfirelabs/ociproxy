{{- define "containerproxy.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "containerproxy.fullname" -}}
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

{{- define "containerproxy.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "containerproxy.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "containerproxy.selectorLabels" -}}
app.kubernetes.io/name: {{ include "containerproxy.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Selector labels of one component: primary | replica | ui. */}}
{{- define "containerproxy.componentSelector" -}}
{{ include "containerproxy.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "containerproxy.secretName" -}}
{{- .Values.auth.existingSecret | default (printf "%s-auth" (include "containerproxy.fullname" .)) -}}
{{- end -}}

{{- define "containerproxy.configSecretName" -}}
{{- printf "%s-config" (include "containerproxy.fullname" .) -}}
{{- end -}}

{{- define "containerproxy.headless" -}}
{{- printf "%s-headless" (include "containerproxy.fullname" .) -}}
{{- end -}}

{{- define "containerproxy.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "containerproxy.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "containerproxy.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}

{{/* In-cluster URL of the primary: an explicit URL wins, else this release's Service. */}}
{{- define "containerproxy.primaryServiceUrl" -}}
{{- printf "http://%s-primary.%s.svc.%s:%v" (include "containerproxy.fullname" .) .Release.Namespace .Values.clusterDomain .Values.primary.service.port -}}
{{- end -}}

{{- define "containerproxy.replicaPrimaryUrl" -}}
{{- if .Values.replica.primaryUrl -}}
{{- .Values.replica.primaryUrl -}}
{{- else if .Values.primary.enabled -}}
{{- include "containerproxy.primaryServiceUrl" . -}}
{{- else -}}
{{- fail "replica.primaryUrl is required when primary.enabled=false" -}}
{{- end -}}
{{- end -}}

{{/*
Value of a key in the chart-managed auth Secret: the explicit value, else
what an earlier install stored (so upgrades keep it), else random.
*/}}
{{- define "containerproxy.persistedValue" -}}
{{- $existing := lookup "v1" "Secret" .root.Release.Namespace (include "containerproxy.secretName" .root) -}}
{{- $old := "" -}}
{{- if and $existing $existing.data -}}
{{- $old = get $existing.data .key | b64dec -}}
{{- end -}}
{{- .value | default $old | default (randAlphaNum 48) -}}
{{- end -}}

{{/* Environment shared by every proxy node. */}}
{{- define "containerproxy.nodeEnv" -}}
- name: CONTAINERPROXY_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "containerproxy.secretName" . }}
      key: secret
- name: CONTAINERPROXY_PUSH_USERNAME
  value: {{ .Values.auth.pushUsername | quote }}
- name: CONTAINERPROXY_PUSH_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "containerproxy.secretName" . }}
      key: push-password
- name: CONTAINERPROXY_REPLICATION_PEERS
  valueFrom:
    secretKeyRef:
      name: {{ include "containerproxy.secretName" . }}
      key: replication-peers
{{- if .Values.replica.enabled }}
- name: CONTAINERPROXY_REPLICATION_ID
  value: {{ .Values.replication.id | default .Release.Name | quote }}
- name: CONTAINERPROXY_REPLICATION_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "containerproxy.secretName" . }}
      key: replication-token
{{- end }}
- name: CONTAINERPROXY_DEFAULT_VISIBILITY
  value: {{ .Values.config.defaultVisibility | quote }}
- name: CONTAINERPROXY_TAG_TTL
  value: {{ .Values.config.tagTTL | quote }}
- name: CONTAINERPROXY_GC_INTERVAL
  value: {{ .Values.config.gcInterval | quote }}
- name: CONTAINERPROXY_GC_MANIFEST_MAX_IDLE_DAYS
  value: {{ .Values.config.gcManifestMaxIdleDays | quote }}
- name: CONTAINERPROXY_LOG_FORMAT
  value: {{ .Values.config.logFormat | quote }}
- name: CONTAINERPROXY_CONFIG
  value: /app/containerproxy.toml
- name: CONTAINERPROXY_MANAGED_SOURCES
  value: {{ .Values.config.managedSources | quote }}
{{- with .Values.config.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* Probes against /v2/, which is public whatever the visibility. */}}
{{- define "containerproxy.probes" -}}
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
{{- define "containerproxy.nodeVolumeMounts" -}}
- name: data
  mountPath: /var/lib/containerproxy
- name: config
  mountPath: /app/containerproxy.toml
  subPath: containerproxy.toml
  readOnly: true
- name: tmp
  mountPath: /tmp
{{- end -}}

{{- define "containerproxy.podChecksums" -}}
checksum/config: {{ include (print .Template.BasePath "/config-secret.yaml") . | sha256sum }}
{{- if not .Values.auth.existingSecret }}
checksum/auth: {{ .Values.auth | toJson | sha256sum }}
{{- end }}
{{- end -}}
