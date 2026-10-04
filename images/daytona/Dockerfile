FROM tailscale/tailscale:v1.102.5 AS tailscale
FROM ubuntu:24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates sudo jq git gh openssh-server \
    && rm -rf /var/lib/apt/lists/*

COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscaled /usr/local/bin/
