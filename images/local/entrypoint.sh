#!/usr/bin/env bash
# First-boot entrypoint for the drukbox local sandbox container. Seeds the
# per-host public key, persists caller env for SSH sessions, then runs sshd.
set -euo pipefail

: "${DRUKBOX_AUTHORIZED_KEY:?DRUKBOX_AUTHORIZED_KEY is required}"

install -d -m 700 /root/.ssh
printf '%s\n' "$DRUKBOX_AUTHORIZED_KEY" > /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

# Persist the caller-supplied env vars so interactive SSH sessions see them:
# pam_env reads /etc/environment. Each name is also a real container env var.
for name in ${DRUKBOX_ENV_KEYS:-}; do
  printf '%s=%s\n' "$name" "${!name-}" >> /etc/environment
done

# A sandbox with secrets sends its HTTPS through the secrets proxy and gets
# the proxy's CA in SECRETS_PROXY_CA. Install it before any client starts.
if [ -n "${SECRETS_PROXY_CA:-}" ]; then
  printf '%s' "$SECRETS_PROXY_CA" | base64 -d > /usr/local/share/ca-certificates/drukbox.crt
  update-ca-certificates >/dev/null
fi

# Generate host keys if the image doesn't ship any.
ssh-keygen -A

exec /usr/sbin/sshd -D -e
