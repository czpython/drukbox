#!/usr/bin/env bash
set -euo pipefail

state_dir=/var/lib/sandbox
done_path="$state_dir/bootstrap.done"

run_privileged() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
    return
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    echo "Sandbox bootstrap requires root or passwordless sudo." >&2
    exit 1
  fi

  sudo -n "$@"
}

run_privileged install -d -m 755 -o "$(id -u)" -g "$(id -g)" "$state_dir"

if [[ -f "$done_path" ]]; then
  exit 0
fi

require_var() {
  if [[ -z "${!1:-}" ]]; then
    echo "Sandbox bootstrap requires $1 to be set." >&2
    exit 1
  fi
}

require_var TAILSCALE_AUTHKEY
require_var TAILSCALE_HOSTNAME

advertise_tags="${TAILSCALE_ADVERTISE_TAGS:-tag:sandbox}"

if [[ -d /run/systemd/system ]]; then
  run_privileged systemctl enable --now tailscaled.service
else
  run_privileged install -d -m 755 /var/run/tailscale
  run_privileged sh -c 'nohup tailscaled --tun=userspace-networking --state=mem: </dev/null >/var/log/tailscaled.log 2>&1 &'
  for attempt in {1..60}; do
    if [[ -S /var/run/tailscale/tailscaled.sock ]]; then
      break
    fi
    sleep 0.5
  done
  if [[ ! -S /var/run/tailscale/tailscaled.sock ]]; then
    echo "Tailscale did not create its control socket." >&2
    exit 1
  fi
fi

tailscale_running() {
  tailscale status --json 2>/dev/null | jq -e '.BackendState == "Running"' >/dev/null
}

if ! tailscale_running; then
  tailscale_args=(
    up
    "--authkey=$TAILSCALE_AUTHKEY"
    --accept-dns=true
    --ssh
    "--hostname=$TAILSCALE_HOSTNAME"
    --reset
  )
  if [[ -n "$advertise_tags" ]]; then
    tailscale_args+=("--advertise-tags=$advertise_tags")
  fi
  if [[ -n "${TAILSCALE_LOGIN_SERVER:-}" ]]; then
    tailscale_args+=("--login-server=$TAILSCALE_LOGIN_SERVER")
  fi
  run_privileged tailscale "${tailscale_args[@]}"
fi

touch "$done_path"
