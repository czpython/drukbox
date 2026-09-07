# Sandbox image for the local `docker` provider: Ubuntu, sshd, git, and gh.
# Build once, then point the provider at it:
#
#   docker build -t drukbox/sandbox:latest images/local/
#
# The entrypoint seeds authorized_keys from $DRUKBOX_AUTHORIZED_KEY (injected by
# drukbox per host) and runs sshd in the foreground. Key-only auth; no password.
FROM ubuntu:24.04

# gh comes from GitHub's own apt repository, as its install guide says.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server ca-certificates curl git \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd

COPY entrypoint.sh /usr/local/bin/drukbox-entrypoint
RUN chmod +x /usr/local/bin/drukbox-entrypoint

EXPOSE 22
ENTRYPOINT ["/usr/local/bin/drukbox-entrypoint"]
