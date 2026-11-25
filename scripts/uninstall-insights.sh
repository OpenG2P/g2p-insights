#!/usr/bin/env bash
#
# uninstall-insights.sh
# ---------------------
# Cleanly uninstall an OpenG2P Insights Helm release and everything it leaves
# behind. `helm uninstall` alone is NOT enough here — several things survive it
# on purpose, and a reinstall on top of them fails in ways that are hard to read.
#
# What survives a plain `helm uninstall`, and why:
#
#   * The insight database and role. Created by the postgres-init subchart with
#     its own lifecycle, so Helm never owned them. Left behind, a reinstall
#     regenerates the DB password Secret while the role keeps the OLD password,
#     and the API pod then hangs on authentication.
#
#   * The maps PVC, and the PV behind it. Annotated
#     helm.sh/resource-policy: keep so a redeploy does not blank the maps while
#     waiting for the next build. On a Retain storage class — nfs-csi, the
#     default here — deleting the PVC still leaves the PV `Released`, holding
#     the space.
#
#   * The Superset service-account Secret. Also resource-policy: keep, and
#     SHARED: whichever of Insights and the registry installed first owns it,
#     and the registry's dashboard-import Job converges the Superset account
#     onto whatever it holds. Deleting it while a registry still uses it breaks
#     that registry's dashboards, so this script leaves it alone unless asked.
#
#   * The OpenRouter API key Secret. Created by an operator, never by Helm.
#     Treated as the operator's property and kept by default.
#
#   * Hook Jobs and their pods. `hook-delete-policy: before-hook-creation`
#     deliberately keeps the last run around so its logs can be read.
#
# What it does, in order:
#   1. helm uninstall <release>
#   2. Delete leftover Jobs/CronJobs + pods (label app.kubernetes.io/instance)
#   3. Sweep leftover Secrets/ConfigMaps by the same label
#   4. Drop the insight database + role in commons-postgresql
#   5. Delete the maps PVC, then any PV left `Released` behind it
#   6. Optionally delete the shared/operator Secrets (opt-in flags)
#
# Requires: kubectl (cluster admin), helm, bash 4+.
#
# USAGE:
#   ./uninstall-insights.sh \
#       --namespace <ns> \
#       [--release <name>]            (default: insights)
#       [--postgres-release <name>]   (default: commons-postgresql)
#       [--postgres-namespace <ns>]   (default: same as --namespace)
#       [--delete-superset-secret]    (also delete the SHARED Superset service
#                                      account Secret — only when no registry
#                                      in this namespace still needs it)
#       [--delete-llm-secret]         (also delete the OpenRouter API key)
#       [--keep-pvs]                  (delete PVCs but leave the PVs)
#       [--keep-db]                   (leave the insight database and role)
#       [--dry-run]                   (print actions, change nothing)
#       [--yes]                       (skip the confirmation prompt)
#
# EXAMPLES:
#   # See what would go, without touching anything:
#   ./uninstall-insights.sh --namespace trial --dry-run
#
#   # Normal cleanup, with a prompt:
#   ./uninstall-insights.sh --namespace trial
#
#   # Everything, including shared secrets — when tearing the namespace down:
#   ./uninstall-insights.sh --namespace trial --yes \
#       --delete-superset-secret --delete-llm-secret

set -euo pipefail

# ---------- defaults ----------
RELEASE="insights"
NAMESPACE=""
POSTGRES_RELEASE="commons-postgresql"
POSTGRES_NAMESPACE=""
DELETE_SUPERSET_SECRET=false
DELETE_LLM_SECRET=false
KEEP_PVS=false
KEEP_DB=false
DRY_RUN=false
ASSUME_YES=false

# ---------- cli ----------
usage() { sed -n '2,63p' "$0"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace)               NAMESPACE="${2:-}"; shift 2 ;;
    --release)                 RELEASE="${2:-}"; shift 2 ;;
    --postgres-release)        POSTGRES_RELEASE="${2:-}"; shift 2 ;;
    --postgres-namespace)      POSTGRES_NAMESPACE="${2:-}"; shift 2 ;;
    --delete-superset-secret)  DELETE_SUPERSET_SECRET=true; shift ;;
    --delete-llm-secret)       DELETE_LLM_SECRET=true; shift ;;
    --keep-pvs)                KEEP_PVS=true; shift ;;
    --keep-db)                 KEEP_DB=true; shift ;;
    --dry-run)                 DRY_RUN=true; shift ;;
    --yes|-y)                  ASSUME_YES=true; shift ;;
    -h|--help)                 usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

[[ -n "$NAMESPACE" ]] || { echo "--namespace is required" >&2; usage; }
POSTGRES_NAMESPACE="${POSTGRES_NAMESPACE:-$NAMESPACE}"

# ---------- derived names (templated exactly like values.yaml) ----------
# values.yaml:
#   insightDB:     '{{ .Release.Name | replace "-" "_" }}'
#   insightDBUser: '{{ printf "%s_user" .Release.Name | replace "-" "_" }}'
RELEASE_UNDERSCORED="${RELEASE//-/_}"
INSIGHT_DB="${RELEASE_UNDERSCORED}"
INSIGHT_USER="${RELEASE_UNDERSCORED}_user"

# Fixed names, not release-derived — both are shared with the registry chart,
# which is exactly why they are not prefixed.
SUPERSET_SVC_SECRET="openg2p-insights-superset-svc"
LLM_SECRET="openg2p-insights-llm"

MAPS_PVC="${RELEASE}-maps-site"

# ---------- helpers ----------
_red()   { printf "\033[31m%s\033[0m\n" "$*"; }
_green() { printf "\033[32m%s\033[0m\n" "$*"; }
_yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }
_blue()  { printf "\033[34m%s\033[0m\n" "$*"; }

run() {
  # Print + execute, or just print under --dry-run. Never aborts: cleanup has to
  # be idempotent, and an already-deleted resource is a success, not an error.
  echo "  \$ $*"
  if [[ "$DRY_RUN" == false ]]; then
    eval "$@" || _yellow "  (command returned non-zero — continuing)"
  fi
}

kexec_psql() {
  # SQL as the postgres superuser, inside the postgres pod. Uses PGPASSWORD from
  # the pod's own env, so nothing has to read a Secret on the operator's machine.
  local sql="$1"
  echo "  \$ psql -U postgres -c \"$sql\""
  if [[ "$DRY_RUN" == false ]]; then
    kubectl exec -n "$POSTGRES_NAMESPACE" "$PG_POD" -c postgresql -- \
      bash -c "PGPASSWORD=\"\$POSTGRES_PASSWORD\" psql -U postgres -v ON_ERROR_STOP=0 -c \"$sql\"" \
      || _yellow "  (psql returned non-zero — continuing)"
  fi
}

# ---------- pre-flight ----------
command -v kubectl >/dev/null || { _red "kubectl not found"; exit 1; }
command -v helm    >/dev/null || { _red "helm not found"; exit 1; }

NAMESPACE_EXISTS=true
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || {
  NAMESPACE_EXISTS=false
  _yellow "namespace '$NAMESPACE' does not exist — nothing in-cluster to clean"
}

PG_POD=""
if [[ "$KEEP_DB" == false ]]; then
  PG_POD="$(kubectl get pods -n "$POSTGRES_NAMESPACE" \
              -l "app.kubernetes.io/name=postgresql" \
              -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [[ -n "$PG_POD" ]] || PG_POD="$(kubectl get pod -n "$POSTGRES_NAMESPACE" \
              "${POSTGRES_RELEASE}-0" -o name 2>/dev/null | cut -d/ -f2 || true)"
fi

# ---------- blast radius ----------
_blue "==> Resources to be deleted"
echo
echo "Helm release:        $RELEASE (namespace: $NAMESPACE)"
if [[ "$KEEP_DB" == true ]]; then
  echo "Postgres database:   (skipped — --keep-db)"
else
  echo "Postgres database:   $INSIGHT_DB"
  echo "Postgres role:       $INSIGHT_USER"
  echo "Postgres pod:        ${PG_POD:-<not found — DB drop will be skipped>} ($POSTGRES_NAMESPACE)"
fi
echo "Maps PVC:            $MAPS_PVC$( [[ "$KEEP_PVS" == true ]] && echo "  (PV kept — --keep-pvs)" )"
echo
echo "Shared, kept unless asked for:"
echo "  $SUPERSET_SVC_SECRET  -> $( [[ "$DELETE_SUPERSET_SECRET" == true ]] && echo "DELETE" || echo "keep (a registry may still need it)" )"
echo "  $LLM_SECRET           -> $( [[ "$DELETE_LLM_SECRET" == true ]] && echo "DELETE" || echo "keep (operator-provided credential)" )"
echo

if [[ "$NAMESPACE_EXISTS" == true ]]; then
  for kind in job cronjob secret configmap pvc; do
    echo "${kind}s (label app.kubernetes.io/instance=$RELEASE):"
    kubectl -n "$NAMESPACE" get "$kind" -l "app.kubernetes.io/instance=$RELEASE" \
      --no-headers 2>/dev/null | awk '{print "  - " $1}' || true
  done
  echo
fi

# ---------- confirmation ----------
if [[ "$DRY_RUN" == false && "$ASSUME_YES" == false ]]; then
  _yellow "This is destructive. The insight database and its data will be dropped."
  read -r -p "Type the release name ('$RELEASE') to continue: " reply
  [[ "$reply" == "$RELEASE" ]] || { echo "aborted"; exit 1; }
fi

# ---------- 1. helm uninstall ----------
_blue "==> 1/6  helm uninstall"
if helm status "$RELEASE" -n "$NAMESPACE" >/dev/null 2>&1; then
  run "helm uninstall '$RELEASE' -n '$NAMESPACE' --wait --timeout 5m"
else
  echo "  (no release '$RELEASE' in '$NAMESPACE' — skipping)"
fi

if [[ "$NAMESPACE_EXISTS" == true ]]; then
  # ---------- 2. hook Jobs and their pods ----------
  _blue "==> 2/6  leftover Jobs, CronJobs and pods"
  # Hook Jobs keep themselves (hook-delete-policy: before-hook-creation) so the
  # last run's logs survive for inspection — which also means uninstall leaves
  # them, and a reinstall would find an immutable Job of the same name.
  for j in "${RELEASE}-maps-build" "${RELEASE}-validate-llm-key"; do
    run "kubectl -n '$NAMESPACE' delete job '$j' --ignore-not-found"
  done
  run "kubectl -n '$NAMESPACE' delete job,cronjob -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found"
  run "kubectl -n '$NAMESPACE' delete pod -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found"

  # ---------- 3. Secrets / ConfigMaps ----------
  _blue "==> 3/6  leftover Secrets and ConfigMaps"
  run "kubectl -n '$NAMESPACE' delete configmap -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found"
  # Label-scoped, so this cannot touch the two shared Secrets below: neither
  # carries this release's instance label once another release owns it.
  run "kubectl -n '$NAMESPACE' delete secret -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found"
fi

# ---------- 4. database + role ----------
_blue "==> 4/6  insight database and role"
if [[ "$KEEP_DB" == true ]]; then
  echo "  (skipped — --keep-db)"
elif [[ -z "$PG_POD" ]]; then
  _yellow "  postgres pod not found in '$POSTGRES_NAMESPACE' — skipping DB drop"
  _yellow "  drop manually:  DROP DATABASE $INSIGHT_DB;  DROP ROLE $INSIGHT_USER;"
else
  # Terminate sessions first: DROP DATABASE fails outright while anything is
  # still connected, and a lingering API pod counts.
  kexec_psql "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${INSIGHT_DB}' AND pid <> pg_backend_pid();"
  kexec_psql "DROP DATABASE IF EXISTS \\\"${INSIGHT_DB}\\\";"
  # The role owns nothing once its database is gone, so this needs no REASSIGN.
  kexec_psql "DROP ROLE IF EXISTS \\\"${INSIGHT_USER}\\\";"
fi

# ---------- 5. PVCs and PVs ----------
_blue "==> 5/6  maps volume"
if [[ "$NAMESPACE_EXISTS" == true ]]; then
  PV_NAME="$(kubectl -n "$NAMESPACE" get pvc "$MAPS_PVC" \
               -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)"

  # Clear anything still MOUNTING the PVC first.
  #
  # `kubectl delete pvc` blocks — indefinitely and silently — while any pod still
  # references it: the kubernetes.io/pvc-protection finalizer holds the object in
  # Terminating and the command just sits there with no message saying why. A
  # COMPLETED or FAILED pod counts; it does not have to be running.
  #
  # Step 2 already deletes pods by label, which covers everything this chart
  # creates. It does NOT cover a Job someone ran by hand from the maps-build
  # spec — a debugging habit this volume invites, since re-running that Job is
  # how you rebuild the site. Those carry their own labels and pin the PVC
  # forever. So match on what actually matters: who mounts it.
  # Emits "pod/<name>" and, where the pod belongs to a Job, "job/<name>" too:
  # deleting only the pod lets the Job controller recreate it and the PVC stays
  # pinned. The owner has to go with it.
  MOUNTERS=$(kubectl -n "$NAMESPACE" get pods -o json 2>/dev/null \
    | PVC="$MAPS_PVC" python3 -c "
import json, os, sys
want = os.environ['PVC']
try:
    pods = json.load(sys.stdin)['items']
except Exception:
    sys.exit(0)
out = []
for p in pods:
    for v in p['spec'].get('volumes') or []:
        if (v.get('persistentVolumeClaim') or {}).get('claimName') == want:
            out.append('pod/' + p['metadata']['name'])
            for o in p['metadata'].get('ownerReferences') or []:
                if o.get('kind') == 'Job':
                    out.append('job/' + o['name'])
            break
for x in dict.fromkeys(out):
    print(x)
" 2>/dev/null || true)
  if [[ -n "$MOUNTERS" ]]; then
    echo "  still mounting '$MAPS_PVC' (would block the delete):"
    echo "$MOUNTERS" | sed 's/^/    /'
    run "kubectl -n '$NAMESPACE' delete $(echo $MOUNTERS | tr '\n' ' ') --ignore-not-found --wait=true --timeout=2m"
  fi

  # --timeout so a finalizer we did not anticipate reports instead of hanging.
  run "kubectl -n '$NAMESPACE' delete pvc '$MAPS_PVC' --ignore-not-found --timeout=2m"
  run "kubectl -n '$NAMESPACE' delete pvc -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found"

  if [[ "$KEEP_PVS" == true ]]; then
    echo "  (PV kept — --keep-pvs)"
  elif [[ -n "$PV_NAME" ]]; then
    # A Retain reclaim policy — nfs-csi, the default on this cluster — leaves the
    # PV `Released` and still holding the space after its PVC is gone.
    run "kubectl delete pv '$PV_NAME' --ignore-not-found"
  else
    echo "  (no bound PV found)"
  fi
fi

# ---------- 6. shared secrets, opt-in ----------
_blue "==> 6/6  shared Secrets"
if [[ "$NAMESPACE_EXISTS" == true ]]; then
  if [[ "$DELETE_SUPERSET_SECRET" == true ]]; then
    _yellow "  deleting the SHARED Superset service-account Secret."
    _yellow "  Any registry in this namespace that imports dashboards uses it too;"
    _yellow "  its next install will mint a new password and re-converge the account."
    run "kubectl -n '$NAMESPACE' delete secret '$SUPERSET_SVC_SECRET' --ignore-not-found"
  else
    echo "  keeping $SUPERSET_SVC_SECRET (pass --delete-superset-secret to remove)"
  fi

  if [[ "$DELETE_LLM_SECRET" == true ]]; then
    run "kubectl -n '$NAMESPACE' delete secret '$LLM_SECRET' --ignore-not-found"
  else
    echo "  keeping $LLM_SECRET (pass --delete-llm-secret to remove)"
  fi
fi

echo
if [[ "$DRY_RUN" == true ]]; then
  _yellow "DRY RUN — nothing was changed."
else
  _green "Done. '$RELEASE' removed from '$NAMESPACE'."
fi
