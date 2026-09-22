#!/usr/bin/env bash
# First boot: the host key, the caller env, then sshd.
set -euo pipefail

: "${DRUKBOX_AUTHORIZED_KEY:?DRUKBOX_AUTHORIZED_KEY is required}"

# drukbox names the user callers SSH as. The stock image has root only; a
# derived image adds its own user and sets DOCKER_SSH_USERNAME to it.
user="${DRUKBOX_SSH_USER:-root}"
home="$(getent passwd "$user" | cut -d: -f6)" \
  || { echo "DRUKBOX_SSH_USER names no user in the image: $user" >&2; exit 1; }
install -d -m 700 "$home/.ssh"
printf '%s\n' "$DRUKBOX_AUTHORIZED_KEY" > "$home/.ssh/authorized_keys"
chmod 600 "$home/.ssh/authorized_keys"
chown -R "$user:" "$home/.ssh"

# pam_env reads /etc/environment.
for name in ${DRUKBOX_ENV_KEYS:-}; do
  printf '%s=%s\n' "$name" "${!name-}" >> /etc/environment
done

# A box with secrets gets the proxy's CA in SECRETS_PROXY_CA. Install it first.
if [ -n "${SECRETS_PROXY_CA:-}" ]; then
  printf '%s' "$SECRETS_PROXY_CA" | base64 -d > /usr/local/share/ca-certificates/drukbox.crt
  update-ca-certificates >/dev/null
fi

# git takes its credential from gh, and SSH remotes go over HTTPS.
if [ -n "${GH_TOKEN:-}" ]; then
  git config --system --replace-all credential.https://github.com.helper ''
  git config --system --add credential.https://github.com.helper '!gh auth git-credential'
  git config --system --replace-all url.https://github.com/.insteadOf git@github.com:
  git config --system --add url.https://github.com/.insteadOf ssh://git@github.com/
fi

# Generate host keys if the image doesn't ship any.
ssh-keygen -A

exec /usr/sbin/sshd -D -e
