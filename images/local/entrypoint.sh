#!/usr/bin/env bash
# First boot: the host key, the caller env, then sshd.
set -euo pipefail

: "${DRUKBOX_AUTHORIZED_KEY:?DRUKBOX_AUTHORIZED_KEY is required}"

install -d -m 700 /root/.ssh
printf '%s\n' "$DRUKBOX_AUTHORIZED_KEY" > /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

# pam_env reads /etc/environment.
for name in ${DRUKBOX_ENV_KEYS:-}; do
  printf '%s=%s\n' "$name" "${!name-}" >> /etc/environment
done

# A box with secrets gets the proxy's CA in SECRETS_PROXY_CA. Install it first.
if [ -n "${SECRETS_PROXY_CA:-}" ]; then
  printf '%s' "$SECRETS_PROXY_CA" | base64 -d > /usr/local/share/ca-certificates/drukbox.crt
  update-ca-certificates >/dev/null
fi

# Generate host keys if the image doesn't ship any.
ssh-keygen -A

exec /usr/sbin/sshd -D -e
