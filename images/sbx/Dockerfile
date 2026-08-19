# Sandbox template for the `docker-sandbox` provider: Ubuntu + sshd.
#
#   docker build -t drukbox/sbx-sandbox:latest images/sbx/
#
# This template does not need environment variables at start, because
# `sbx create` cannot send them. drukbox injects the SSH key through the exec
# channel when the sandbox runs. The images/local/ entrypoint needs boot-time
# environment variables. Thus it cannot operate as a sandbox template.
FROM ubuntu:24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server ca-certificates sudo \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd

COPY entrypoint.sh /usr/local/bin/drukbox-entrypoint
RUN chmod +x /usr/local/bin/drukbox-entrypoint

EXPOSE 22
ENTRYPOINT ["/usr/local/bin/drukbox-entrypoint"]
