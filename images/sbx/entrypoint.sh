#!/usr/bin/env bash
# Boot entrypoint for the drukbox Docker Sandboxes template. The script starts
# sshd. drukbox injects the SSH key through the exec channel after the start.
# sshd reads authorized_keys for each authentication. A restart is not
# necessary after the key injection.
#
# Do not make an existing authorized_keys file empty. The entrypoint runs
# again at each sandbox restart, after the key injection.
set -euo pipefail

install -d -m 700 /root/.ssh
[ -f /root/.ssh/authorized_keys ] || install -m 600 /dev/null /root/.ssh/authorized_keys

# /run is a fresh tmpfs at start. sshd stops immediately without its
# privilege-separation directory.
mkdir -p /run/sshd

ssh-keygen -A

exec /usr/sbin/sshd -D -e
