#!/usr/bin/env bash
# Adapted from the homelab's attended cold-backup procedure.
set -Eeuo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ ${1:-} == --allow-downtime ]] || { echo 'Use --allow-downtime in an attended maintenance window.'; exit 2; }
[[ $EUID == 0 ]] || { echo 'Root access is required to read application data.'; exit 2; }
: "${RESTIC_REPOSITORY:?Set an off-server Restic repository}"
: "${RESTIC_PASSWORD_FILE:?Set the path to a private password file}"
command -v restic >/dev/null
command -v docker >/dev/null
[[ -r $RESTIC_PASSWORD_FILE ]] || { echo 'Backup password file is unreadable.'; exit 1; }
paths=()
while IFS= read -r path || [[ -n $path ]]; do
  [[ -z $path || $path == \#* ]] && continue
  [[ $path == /* && -e $path ]] || { echo 'A backup path is missing or not absolute.'; exit 1; }
  paths+=("$path")
done < "$ROOT/config/backup-paths.local.txt"
((${#paths[@]})) || { echo 'The reviewed backup path list is empty.'; exit 1; }
restic snapshots >/dev/null
# Capture errors explicitly: a failed Docker query must not look like an empty host.
running_output="$(docker ps -q)"
running=()
if [[ -n $running_output ]]; then mapfile -t running <<< "$running_output"; fi
restart() {
  local rc=$?
  trap - EXIT INT TERM
  if ((${#running[@]})); then docker start "${running[@]}" || rc=1; fi
  exit "$rc"
}
trap restart EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if ((${#running[@]})); then docker stop --time 120 "${running[@]}"; fi
restic backup --tag homelab-config --exclude '**/node_modules/**' --exclude '**/.git/**' "${paths[@]}"
restic check
# The EXIT trap attempts to restart the originally running containers.
