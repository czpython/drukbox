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

# A sandbox with a github secret uses gh as git's credential helper, so git
# sends the placeholder and the proxy swaps it. An SSH remote would go around
# the proxy, so it is rewritten to HTTPS. This runs again at every restart.
if [ -n "${GH_TOKEN:-}" ]; then
  git config --system --replace-all credential.https://github.com.helper ''
  git config --system --add credential.https://github.com.helper '!gh auth git-credential'
  git config --system --replace-all url.https://github.com/.insteadOf git@github.com:
  git config --system --add url.https://github.com/.insteadOf ssh://git@github.com/
fi

# Generate host keys if the image doesn't ship any.
ssh-keygen -A

exec /usr/sbin/sshd -D -e
