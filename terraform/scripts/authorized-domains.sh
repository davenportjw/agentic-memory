#!/usr/bin/env bash
# Additively manage this app's hosts in the project-wide sign-in "authorized domains" list.
#
#   authorized-domains.sh add    <project_id> <host> [host ...]
#   authorized-domains.sh remove <project_id> <host> [host ...]
#   authorized-domains.sh list   <project_id>
#
# The list is a single field on the project's Identity Platform config, shared by every app in
# the project. This script only ever adds or removes the hosts passed to it: it reads the live
# list, applies the union/difference, PATCHes with updateMask=authorizedDomains, and re-reads the
# response to confirm (retrying if a concurrent writer clobbered it). Auth is Application Default
# Credentials only. The same script is vendored into each app's Terraform directory.
set -euo pipefail

usage() { echo "usage: $0 <add|remove|list> <project_id> [host ...]" >&2; exit 2; }
ACTION="${1:-}"; PROJECT_ID="${2:-}"
[[ -n "$ACTION" && -n "$PROJECT_ID" ]] || usage
shift 2

API_URL="https://identitytoolkit.googleapis.com/admin/v2/projects/${PROJECT_ID}/config"
TOKEN="$(gcloud auth application-default print-access-token)"

get_domains() {
  curl -fsS -H "Authorization: Bearer ${TOKEN}" -H "x-goog-user-project: ${PROJECT_ID}" \
    "${API_URL}" | jq -c '.authorizedDomains // []'
}

case "$ACTION" in
  list) get_domains | jq -r '.[]'; exit 0 ;;
  add | remove) ;;
  *) usage ;;
esac

# Normalise: strip scheme/trailing slash, drop empties.
HOSTS=()
for h in "$@"; do
  h="${h#https://}"; h="${h#http://}"; h="${h%/}"
  [[ -n "$h" ]] && HOSTS+=("$h")
done
[[ ${#HOSTS[@]} -gt 0 ]] || exit 0

# Serialise local writers (macOS has no flock; mkdir is atomic).
LOCK_DIR="${TMPDIR:-/tmp}/authorized-domains-${PROJECT_ID}.lock"
LOCKED=0
for _ in $(seq 1 60); do
  if mkdir "$LOCK_DIR" 2>/dev/null; then LOCKED=1; trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT; break; fi
  sleep 1
done
[[ $LOCKED -eq 1 ]] || { echo "could not acquire $LOCK_DIR (remove it if stale)" >&2; exit 1; }

for attempt in 1 2 3 4 5; do
  CURRENT="$(get_domains)"
  if [[ "$ACTION" == add ]]; then
    NEXT="$(jq -cn --argjson cur "$CURRENT" '$cur + ($ARGS.positional - $cur)' --args "${HOSTS[@]}")"
  else
    NEXT="$(jq -cn --argjson cur "$CURRENT" '$cur - $ARGS.positional' --args "${HOSTS[@]}")"
  fi

  if [[ "$NEXT" != "$CURRENT" ]]; then
    RESULT="$(curl -fsS -X PATCH -H "Authorization: Bearer ${TOKEN}" -H "x-goog-user-project: ${PROJECT_ID}" \
      -H "Content-Type: application/json" "${API_URL}?updateMask=authorizedDomains" \
      -d "$(jq -cn --argjson d "$NEXT" '{authorizedDomains: $d}')" | jq -c '.authorizedDomains // []')"
  else
    RESULT="$CURRENT"
  fi

  if [[ "$ACTION" == add ]]; then
    PENDING="$(jq -n --argjson r "$RESULT" '$ARGS.positional - $r | length' --args "${HOSTS[@]}")"
  else
    PENDING="$(jq -n --argjson r "$RESULT" '[$ARGS.positional[] | select(. as $h | $r | index($h))] | length' --args "${HOSTS[@]}")"
  fi
  if [[ "$PENDING" -eq 0 ]]; then
    echo "authorized domains: ${ACTION} ${HOSTS[*]} (ok)"
    exit 0
  fi
  sleep $((attempt + RANDOM % 2))
done

echo "authorized domains: ${ACTION} did not converge after 5 attempts" >&2
exit 1
