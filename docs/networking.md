# Networking and reachability

This page explains how a caller reaches a sandbox and why each path is
shaped the way it is. For turning these modes on, read
[Deploy](deploy.md).

## Two modes

`TAILSCALE_ENABLED` selects between two networking models. A provider
whose hosts cannot join a tailnet (docker — local containers,
docker-sbx — local microVMs) always takes the external path,
whatever the mode. The API response carries
both addresses; which is populated depends on the mode and the
provider:

- `external_ssh_host` / `external_ssh_port` — the provider-given
  public path. Always present (empty for an AWS host with Tailscale
  on, which has no public path at all).
- `internal_ssh_host` — the tailnet MagicDNS name, populated only with
  Tailscale on. The internal path is always port 22 by Tailscale
  convention, so there is no internal port field.

Callers pick whichever path they can reach and dial it themselves.

## Reverse tunnel to the injecting proxy

Docker, AWS, Hetzner, and Exoscale hosts cannot dial the injecting proxy
directly. Drukbox opens one reverse SSH forward for each such host. The forward
listens on `127.0.0.1:8781` inside the host and sends traffic to the proxy bind
port. The proxy and the remote listener both stay on loopback by default.

The forward has its own supervised SSH connection. It is not attached to a
caller connection or any other SSH work. Closing an unrelated SSH connection
does not affect it. Drukbox closes the forward before an API-owned teardown and
closes all forwards during graceful shutdown. The SSH transport also closes when
an out-of-process janitor removes the VM.

The connection uses the host's stored `known_hosts` material. On a public path,
the host trusts a persistent Drukbox service key installed during provisioning.
On a tailnet path, Tailscale SSH authenticates the connection and AsyncSSH sends
no client key. The public service key is not installed on that path. For each
forwarded TCP connection, the tunnel writes a versioned host-ID preamble before
the box's bytes. The proxy accepts that identity from the tunnel transport, not
from box-controlled HTTP headers.

The API restores missing tunnels for active hosts after startup and after a pool
process creates a host. A pool claim also waits for the tunnel before it returns
the host. If an established tunnel drops, Drukbox marks the host `error`, expires
it, and tells the caller to create a replacement. It does not leave an active host
with a dead proxy route.

## Tailscale on: the overlay is the security model

Per sandbox, drukbox mints an ephemeral, tag-scoped Tailscale auth key
and injects it at boot. The sandbox joins the tailnet, drukbox
discovers the device by hostname (one shared poller serves all
concurrent provisions), stores the device ID, and ssh-keyscans over
the tailnet path.

On AWS this mode has no public surface at all: no keypair is imported,
no security group is managed, and no public IP is attached —
authentication and reachability are tailnet ACLs plus tailscaled-SSH,
and `external_ssh_host` comes back empty.

Hetzner can't drop the public IP that cheaply (a server needs one to
reach the tailnet), so a tailscale-on Hetzner box still has a public
IPv4 — but it is key-locked with the per-VM key drukbox always mints,
and `external_ssh_host` carries that public path alongside the tailnet
one. The tailnet remains the intended path; the public port just isn't
left open to passwords.

Teardown releases the Tailscale device by its stored ID — never by
hostname — and treats a 404 as success, because ephemeral devices
self-delete and operator cleanup can race the API.

## Tailscale off: public path, key-only auth

With Tailscale off the provider's public path is the only path.

On exe.dev, SSH terminates at exe's edge (`ssh_dest`), and exe owns
authentication; drukbox returns no key material.

On AWS, drukbox generates a per-VM ed25519 keypair, imports the public
half, and returns the private half exactly once in the create
response — it is never persisted, and a later `GET /hosts/{id}`
returns `private_key: null`. Ingress comes from the managed security
group (`drukbox-managed`): SSH from the service's detected public
egress IP as a `/32` by default, `AWS_SSH_CIDRS` authoritative when
set, and an operator-supplied `AWS_SECURITY_GROUP_ID` is never
touched. If egress detection fails, ingress falls back to `0.0.0.0/0`
with a warning log — the per-VM key remains the auth boundary, never
the source address.

On Hetzner the per-VM keypair works the same way, but a fresh server
has no firewall — port 22 is open and the key is the only boundary.
There is no ingress configuration to manage.

## The IP-literal invariant

The detected-`/32` ingress rule is only sound under one invariant:
*the address callers dial must route over the same path the detector
saw*. That is why `external_ssh_host` on AWS is the sandbox's public
IP literal and never its public DNS name.

EC2 public DNS is split-horizon: outside the VPC it resolves to the
public IP, but inside the VPC it resolves to the sandbox's private IP.
A same-VPC caller dialing the DNS name therefore arrives from its
private address — which a public-`/32` rule silently drops, surfacing
as SSH connect timeouts that depend on which resolver answered. Dials
to the IP literal hairpin through the IGW and arrive from the detected
egress IP no matter where the caller sits.

The same trap applies to operators who set `AWS_SSH_CIDRS`: if callers
share the sandboxes' VPC and you restrict ingress, include the VPC
CIDR, not just a public `/32`.

## known_hosts

The response's `known_hosts` field carries `ssh-keyscan`-style host key
material. Drukbox scans every available SSH path because Tailscale SSH and the
provider's SSH service present different host keys. Each entry is keyed to the
address and port that were scanned.

With Tailscale off, the first-time keyscan runs over the public
network and carries the same TCP-session MITM window any first-time
keyscan has. Operators who need stronger guarantees should enable
Tailscale and let the scan run over the authenticated overlay.
