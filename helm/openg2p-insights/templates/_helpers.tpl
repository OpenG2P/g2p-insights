{{/*
Shared helpers for the openg2p-insights chart.
*/}}

{{- define "openg2p-insights.fullname" -}}
{{- printf "%s" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "openg2p-insights.labels" -}}
app.kubernetes.io/name: openg2p-insights
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Env for a component: literal envVars (each tpl-rendered) followed by
envVarsFrom (secretKeyRef / configMapKeyRef). Both are maps keyed by variable
name, so overriding one entry does not mean restating the rest.

Values are quoted after rendering — an unquoted `true` or a numeric port is a
bool/int to YAML, and Kubernetes requires env values to be strings.

Call as:  {{- include "openg2p-insights.env" (dict "component" .Values.x "ctx" $) | nindent 12 }}
*/}}
{{- define "openg2p-insights.env" -}}
{{- $ctx := .ctx -}}
{{- range $k, $v := .component.envVars }}
- name: {{ $k }}
  value: {{ tpl (toString $v) $ctx | quote }}
{{- end }}
{{- range $k, $ref := .component.envVarsFrom }}
- name: {{ $k }}
{{- range $kind, $spec := $ref }}
  valueFrom:
    {{ $kind }}:
      name: {{ tpl (toString $spec.name) $ctx | quote }}
      key: {{ tpl (toString $spec.key) $ctx | quote }}
      {{- /* A missing Secret otherwise wedges the pod in
             CreateContainerConfigError before it runs a line of code. Optional
             for values the app can legitimately do without — the LLM key when
             AI is switched off — so the deployment is not held hostage to a
             Secret nobody needs. */ -}}
      {{- if $spec.optional }}
      optional: true
      {{- end }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
An Istio VirtualService for one component. Every surface here is plain HTTP
behind the shared gateway, so the only things that vary are the host, the path
prefix and the backing service.
*/}}
{{- define "openg2p-insights.virtualservice" -}}
{{- $c := .component -}}
{{- $ctx := .ctx -}}
{{- if and $c.istio.enabled $c.istio.virtualservice.enabled }}
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: {{ $ctx.Release.Name }}-{{ $c.nameOverride }}
  labels: {{ include "openg2p-insights.labels" $ctx | nindent 4 }}
spec:
  hosts:
    - {{ tpl (toString $c.istio.virtualservice.host) $ctx | quote }}
  gateways:
    - {{ $c.istio.virtualservice.gateway | quote }}
  http:
    {{- range $c.istio.virtualservice.extraPrefixes }}
    {{/* Evidence emits ABSOLUTE paths for its data and its static assets —
         /data/<query>/....parquet, /geo/level1.geojson — and does NOT prefix
         them with deployment.basePath. Only SvelteKit's own /_app/ assets
         honour it. Served under /maps, those requests therefore land on the
         FRONTEND, which answers every unknown path with the SPA's index.html:
         200 OK, text/html, where the browser expected parquet. DuckDB gets
         nothing it can read and every chart sits on "Loading..." for ever,
         with no failed request to notice.

         So route them to the maps service and put the prefix back on. */}}
    - match:
        - uri:
            prefix: {{ .path | quote }}
      rewrite:
        uri: {{ .rewriteTo | quote }}
      route:
        - destination:
            host: {{ $c.istio.virtualservice.destination | default (printf "%s-%s" $ctx.Release.Name $c.nameOverride) | quote }}
            port:
              number: {{ tpl (toString $c.istio.virtualservice.destinationPort) $ctx }}
    {{- end }}
    - match:
        - uri:
            prefix: {{ $c.istio.virtualservice.prefix | quote }}
      {{- with $c.istio.virtualservice.rewriteUri }}
      {{/* Strip the match prefix before forwarding. The backend serves its routes
           at the ROOT (/health, /dashboards, /ask) — openapi_common_api_prefix is
           OpenAPI metadata, not a mount point — so without this the gateway hands
           it /v1/insight/dashboards, which it does not serve, and every API call
           404s while the pods look perfectly healthy.

           The value existed in values.yaml from the start; this template simply
           never read it, so setting it did nothing. */}}
      rewrite:
        uri: {{ tpl (toString .) $ctx | quote }}
      {{- end }}
      route:
        - destination:
            host: {{ $c.istio.virtualservice.destination | default (printf "%s-%s" $ctx.Release.Name $c.nameOverride) | quote }}
            port:
              number: {{ tpl (toString $c.istio.virtualservice.destinationPort) $ctx }}
{{- end }}
{{- end -}}

{{/*
Gate the maps rebuild on the registry's reporting views being populated.

Not "is the registry release deployed" — that is both unavailable across releases
and the wrong question, since seeding happens in post-install hooks afterwards.
This asks the database directly, so it is correct regardless of install order and
regardless of who seeded.
*/}}
{{- define "openg2p-insights.mapsBuildWait" -}}
{{- $c := .Values.insightMaps }}
- name: wait-for-reporting-views
  image: {{ $c.build.waitImage }}
  env:
    - name: PGHOST
      value: {{ tpl $c.build.source.host $ | quote }}
    - name: PGPORT
      {{/* tpl, like every other value here: source.port is the template string
           '{{ .Values.global.registryDBPort }}'. Without it psql got the literal
           braces, every connection failed, and the gate's `|| echo 0` turned
           that into "the view is empty" — so the build skipped silently and the
           maps stayed at whatever the image shipped. */}}
      value: {{ tpl ($c.build.source.port | toString) $ | quote }}
    - name: PGDATABASE
      value: {{ tpl $c.build.source.database $ | quote }}
    - name: PGUSER
      value: {{ tpl $c.build.source.user $ | quote }}
    - name: PGPASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ tpl $c.build.source.passwordSecret.name $ | quote }}
          key: {{ tpl $c.build.source.passwordSecret.key $ | quote }}
  volumeMounts:
    - name: gate
      mountPath: /gate
  command: ["sh", "-c"]
  args:
    - |
      # Never fails. This runs as a post-install hook, and Helm WAITS for hooks
      # — so exiting non-zero here would fail the whole release, and blocking
      # would hold the install open, merely because the registry has not been
      # seeded yet. Insights is installable before the registry exists; that is
      # a supported order, not an error.
      #
      # Instead: build if the data is there, otherwise leave the marker unset
      # and let the CronJob build on its next tick. The maps serve the image's
      # own build until then.
      #
      # Existing but EMPTY is the trap: the reporting views are created by their
      # own hook before any data is generated, so checking existence alone would
      # build an empty map that looks like a working one.
      rm -f /gate/ready
      for i in $(seq 1 {{ $c.build.waitAttempts }}); do
        n=$(psql -tAc "select count(*) from {{ $c.build.readyView }}" 2>/dev/null || echo 0)
        if [ "${n:-0}" -gt 0 ]; then
          echo "[maps-build] {{ $c.build.readyView }} has $n rows"
          {{- if $c.build.refreshViews }}
          # The reporting views are MATERIALIZED: they hold a snapshot taken when
          # the registry last created them, and nothing else refreshes them. Left
          # alone, every scheduled rebuild re-extracts the same frozen numbers and
          # publishes them as current — no error, no warning, just a map that
          # stopped tracking the registry.
          #
          # CONCURRENTLY so reads are not blocked for the minutes this takes; it
          # needs the unique indexes the registry's reporting_views.sql creates,
          # and a first non-concurrent refresh, which CREATE ... AS already did.
          {{- /* A list in values; a comma-separated string when it comes from
                 the install form, which cannot express a list. */ -}}
          {{- $rv := list }}
          {{- if kindIs "string" $c.build.refreshViews }}
            {{- range splitList "," $c.build.refreshViews }}
              {{- if trim . }}{{ $rv = append $rv (trim .) }}{{ end }}
            {{- end }}
          {{- else }}
            {{- $rv = $c.build.refreshViews }}
          {{- end }}
          for v in {{ join " " $rv }}; do
            echo "[maps-build] refreshing $v"
            if ! psql -v ON_ERROR_STOP=1 -c "REFRESH MATERIALIZED VIEW CONCURRENTLY $v" 2>/tmp/err; then
              # Not fatal: a stale map beats no map, and a plain view or a
              # missing unique index is a legitimate deployment.
              echo "[maps-build] could not refresh $v — building from the existing snapshot"
              sed 's/^/[maps-build]   /' /tmp/err
            fi
          done
          {{- end }}
          touch /gate/ready
          exit 0
        fi
        echo "[maps-build] waiting for {{ $c.build.readyView }} to be populated ($i)"
        sleep {{ $c.build.waitSeconds }}
      done
      echo "[maps-build] {{ $c.build.readyView }} is still empty — skipping this build."
      echo "[maps-build] The scheduled build will pick it up once the registry is seeded."
      exit 0
{{- end }}

{{- define "openg2p-insights.mapsBuildStep" -}}
{{- $c := .Values.insightMaps }}
- name: build
  image: "{{ $c.build.image.repository }}:{{ $c.build.image.tag }}"
  imagePullPolicy: {{ $c.build.image.pullPolicy }}
  env:
    - name: EVIDENCE_BUILD_OUT
      value: /out
    - name: EVIDENCE_SOURCE__registry__host
      value: {{ tpl $c.build.source.host $ | quote }}
    - name: EVIDENCE_SOURCE__registry__port
      {{/* tpl, like every other source.* value: port is the template string
           '{{ .Values.global.registryDBPort }}'. Unrendered, Evidence received
           the literal braces, parsed them as NaN and refused the connection —
           "Port should be >= 0 and < 65536" — AFTER the gate had passed and the
           boundaries had synced, so everything upstream looked healthy. The wait
           container had the same bug and was fixed; this one was missed because
           the two build their env separately. */}}
      value: {{ tpl ($c.build.source.port | toString) $ | quote }}
    - name: EVIDENCE_SOURCE__registry__database
      value: {{ tpl $c.build.source.database $ | quote }}
    - name: EVIDENCE_SOURCE__registry__user
      value: {{ tpl $c.build.source.user $ | quote }}
    {{/* Master Data, for the RUN-time boundary sync. The image bakes a DEFAULT
         country pack, because the build cannot know which country a deployment
         will carry; Master Data does, and records a boundary URL on every geo
         row. build-into re-syncs from here before each extract so the map
         follows the environment. A separate database owned by a separate role,
         hence its own credentials — reading the geo tables as the registry user
         fails with "permission denied for table g2p_geo_levels". */}}
    - name: MDS_PGHOST
      value: {{ tpl .Values.global.masterDataDBHost $ | quote }}
    - name: MDS_PGPORT
      value: {{ .Values.global.masterDataDBPort | default 5432 | quote }}
    - name: MDS_DB
      value: {{ tpl .Values.global.masterDataDB $ | quote }}
    - name: MDS_PGUSER
      value: {{ tpl .Values.global.masterDataDBUser $ | quote }}
    - name: MDS_PGPASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ tpl .Values.global.masterDataDBSecret $ | quote }}
          key: {{ tpl .Values.global.masterDataDBUserPasswordKey $ | quote }}
    - name: MAPS_SYNC_FROM_MDS
      value: {{ $c.build.syncBoundariesFromMasterData | quote }}
    {{- with $c.build.objectStore }}
    {{- if .existingSecret }}
    {{/* The bucket Master Data uploaded the boundaries to is not public — an
         anonymous GET returns 403 — so reading them back needs the same
         credentials that wrote them. Skip this only where the bucket is fronted
         by a public read policy. */}}
    - name: MAPS_S3_ACCESS_KEY
      valueFrom:
        secretKeyRef:
          name: {{ tpl .existingSecret $ | quote }}
          key: {{ .accessKeyKey | quote }}
    - name: MAPS_S3_SECRET_KEY
      valueFrom:
        secretKeyRef:
          name: {{ tpl .existingSecret $ | quote }}
          key: {{ .secretKeyKey | quote }}
    {{- end }}
    {{- end }}
    - name: EVIDENCE_SOURCE__registry__password
      valueFrom:
        secretKeyRef:
          name: {{ tpl $c.build.source.passwordSecret.name $ | quote }}
          key: {{ tpl $c.build.source.passwordSecret.key $ | quote }}
  volumeMounts:
    - name: site
      mountPath: /out
    {{- if $c.build.content.configMap }}
    - name: content
      mountPath: /content
      readOnly: true
    {{- end }}
    - name: gate
      mountPath: /gate
  command: ["sh", "-c"]
  args:
    - |
      # The gate is set by the init container only when the reporting views hold
      # rows. Without it there is nothing worth extracting, and building anyway
      # would replace a good site with an empty one.
      if [ ! -f /gate/ready ]; then
        echo "[maps-build] no data to build from — leaving the current site in place."
        exit 0
      fi
      {{- if not $c.build.content.configMap }}
      # No registry content configured — do NOT build.
      #
      # The image carries REFERENCE content, whose queries name the reference
      # registry's reporting views. Built against a different registry those
      # queries all fail, and Evidence publishes the site anyway: a complete,
      # correctly themed map with every panel empty. That is worse than no
      # build, because it looks like a data problem in the registry rather than
      # a missing setting here — it is exactly how a Farmer Registry install
      # came to be running NSR's level2_quintiles query.
      #
      # Exit 0, not 1: an install must not fail over an unset optional value.
      # The serving pod keeps whatever it has and says what is missing.
      echo "[maps-build] no map content configured — nothing registry-specific to build."
      echo "[maps-build] Set insightMaps.build.content.configMap to the registry's"
      echo "[maps-build] content ConfigMap (e.g. <registry-release>-maps-content)."
      exit 0
      {{- end }}
      {{- if $c.build.content.configMap }}
      # Overlay the registry's own content onto the toolchain.
      #
      # Replace rather than merge: a leftover reference query would run against
      # this registry's database and either fail the build or, worse, succeed and
      # put a meaningless panel on the page.
      #
      # connection.yaml is NOT touched — it belongs to the image and describes
      # "the registry's database", which is the same shape for every registry.
      echo "[maps-build] applying registry map content"
      rm -f /app/sources/registry/*.sql /app/pages/*.md
      for f in /content/*.sql; do [ -e "$f" ] && cp "$f" /app/sources/registry/; done
      for f in /content/*.md;  do [ -e "$f" ] && cp "$f" /app/pages/; done
      echo "[maps-build] $(ls /app/sources/registry/*.sql 2>/dev/null | wc -l) source(s), $(ls /app/pages/*.md 2>/dev/null | wc -l) page(s)"
      {{- end }}
      exec /usr/local/bin/build-into
  {{- with $c.build.resources }}
  resources: {{ toYaml . | nindent 4 }}
  {{- end }}
{{- end }}

{{/*
The catalog generator container, shared by the install hook and the CronJob.
*/}}
{{- define "openg2p-insights.catalogBuildStep" -}}
{{- $c := .Values.insightBackend }}
- name: catalog
  image: "{{ $c.image.repository }}:{{ $c.image.tag }}"
  imagePullPolicy: {{ $c.image.pullPolicy }}
  command: ["sh", "-c"]
  args:
    - |
      set -e
      python /app/catalog/generate_catalog.py \
        --out /tmp/schema.yaml \
        --k-anonymity {{ $c.appCatalog.generate.kAnonymity }}

      {{/* Patch the key rather than replace the ConfigMap: the same object also
           carries pii_recognizers.yaml and viz_templates.yaml, which are
           hand-authored and must survive a regeneration. */}}
      python - <<'PY'
      import json, os, ssl, urllib.request
      ns   = open("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read()
      tok  = open("/var/run/secrets/kubernetes.io/serviceaccount/token").read()
      body = json.dumps({"data": {"schema.yaml": open("/tmp/schema.yaml").read()}})
      url  = (f"https://kubernetes.default.svc/api/v1/namespaces/{ns}"
              f"/configmaps/{os.environ['CATALOG_CONFIGMAP']}")
      ctx  = ssl.create_default_context(
          cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
      req  = urllib.request.Request(url, data=body.encode(), method="PATCH",
          headers={"Authorization": f"Bearer {tok}",
                   "Content-Type": "application/merge-patch+json"})
      with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
          print(f"[catalog] wrote schema.yaml into {os.environ['CATALOG_CONFIGMAP']} ({r.status})")
      PY

      {{/* The backend loads the catalog once, at startup — so writing the
           ConfigMap changes nothing until it is told. It exposes
           /catalog/reload for exactly this, which is far cheaper than a rollout
           and needs no RBAC on deployments.

           The sleep is the kubelet's ConfigMap sync: a mounted key can take
           up to a minute to appear on disk, and reloading before then just
           re-reads the old file and reports success. */}}
      echo "[catalog] waiting {{ $c.appCatalog.generate.propagationSeconds }}s for the mount to update"
      sleep {{ $c.appCatalog.generate.propagationSeconds }}

      python - <<'PY'
      import json, os, urllib.request
      url = os.environ["BACKEND_URL"].rstrip("/") + "/catalog/reload"
      try:
          req = urllib.request.Request(url, data=b"", method="POST")
          with urllib.request.urlopen(req, timeout=30) as r:
              print(f"[catalog] backend reloaded: {json.load(r)}")
      except Exception as exc:
          # Not fatal: the catalog is written and the next backend restart picks
          # it up. Failing here would fail an install over a warm-up detail.
          print(f"[catalog] could not reload the backend ({exc}).")
          print("[catalog] It will load the new catalog on its next restart.")
      PY
  env:
    - name: BACKEND_URL
      value: "http://{{ .Release.Name }}-{{ $c.nameOverride }}:{{ $c.service.port }}"
    - name: CATALOG_CONFIGMAP
      value: {{ .Release.Name }}-insight-catalog
    - name: PGHOST
      value: {{ tpl .Values.global.registryDBHost $ | quote }}
    - name: PGPORT
      value: {{ .Values.global.registryDBPort | default 5432 | quote }}
    - name: PGDATABASE
      value: {{ tpl .Values.global.registryDBName $ | quote }}
    - name: PGUSER
      value: {{ tpl .Values.global.registryDBUser $ | quote }}
    - name: PGPASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ tpl .Values.global.registryDBSecret $ | quote }}
          key: {{ tpl .Values.global.registryDBUserPasswordKey $ | quote }}
    {{/* Master Data supplies the code lists that become enum values. A separate
         database owned by a separate role. */}}
    - name: MDS_PGHOST
      value: {{ tpl .Values.global.masterDataDBHost $ | quote }}
    - name: MDS_PGPORT
      value: {{ .Values.global.masterDataDBPort | default 5432 | quote }}
    - name: MDS_DB
      value: {{ tpl .Values.global.masterDataDB $ | quote }}
    - name: MDS_PGUSER
      value: {{ tpl .Values.global.masterDataDBUser $ | quote }}
    - name: MDS_PGPASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ tpl .Values.global.masterDataDBSecret $ | quote }}
          key: {{ tpl .Values.global.masterDataDBUserPasswordKey $ | quote }}
  resources: {{- toYaml $c.appCatalog.generate.resources | nindent 4 }}
{{- end }}

{{/*
Connection settings for the migration Job. migrations/env.py reads DB_* rather
than the app's INSIGHT_DB_* — it runs outside the app's Settings.
*/}}
{{- define "openg2p-insights.migrateEnv" -}}
- name: DB_HOST
  value: {{ tpl .Values.global.postgresqlHost $ | quote }}
- name: DB_PORT
  value: {{ .Values.global.insightDBPort | quote }}
- name: DB_NAME
  value: {{ tpl .Values.global.insightDB $ | quote }}
- name: DB_USER
  value: {{ tpl .Values.global.insightDBUser $ | quote }}
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ tpl .Values.global.insightDBSecret $ | quote }}
      key: {{ tpl .Values.global.insightDBUserPasswordKey $ | quote }}
{{- end }}
