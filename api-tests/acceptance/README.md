# Acceptance from inside the box

Black-box checks of the secrets path, run by hand against a deployment, the
way the API tests run. One run covers one provider. It creates a host with an
issuer-backed `anthropic` secret and an issuer-backed `github` secret, waits
for the host, and then works from inside the box over SSH. It ends with a
delete of the host.

Not in CI. A run needs a subscription token, an installation token for a
private repository, and a box image with `claude`, `git`, and `gh`.

## What passes

- `POST /hosts` answers 201 and echoes no secret. The host becomes active.
- The issuer is asked once for the github secret, and not before it is used.
  On docker-sbx the API asks at provisioning, and the exchange asks again when
  it first sees the host. The anthropic secret lives 70 seconds in this run,
  so the exchange asks for it again and again, which the outage step needs.
- A plain session sees both placeholders.
- `claude -p` answers `ok`.
- `gh api` and `gh pr list` work on the private repository.
- `git clone`, a push of a throwaway branch, and its delete work.
- The placeholder sent straight to Anthropic, with the proxy bypassed, is
  refused with 401.
- A wrong placeholder is refused: with 403 by the exchange, or with 401 by
  Anthropic on docker-sbx, where nothing swaps it.
- While the issuer fails, the held value keeps the box working, and the
  exchange kept asking the issuer in the meantime.
- A restart of the exchange costs one fetch of the github secret, and
  `claude -p` and `gh` answer again.
- On docker, a restarted box comes back with the same git setup.
- On docker-sbx, every secret is scoped to the sandbox, the value files are
  0600 beside the workspaces, and teardown leaves no secret in
  `sbx secret ls`, no value file, and no workspace.
- `DELETE /hosts` answers 204.

## The issuer

`issuer.py` stands in for the service that mints tokens. It answers
`GET /mint/<service>` with the JSON in `<service>.json` next to it, behind the
bearer in `ISSUER_BEARER`. The bearer comes from the environment, never from
the command line, where every process could read it. Write the token files
yourself, with mode 0600, and delete them when the run ends. They are ignored
by git.

```bash
umask 077
printf '{"value": "%s"}' "$SUBSCRIPTION_TOKEN" > api-tests/acceptance/anthropic.json
printf '{"value": "%s"}' "$INSTALLATION_TOKEN" > api-tests/acceptance/github.json
ISSUER_BEARER=<bearer> python3 api-tests/acceptance/issuer.py 8791
```

An `expires_at` in a file is optional. Without it the secret's refresh
interval sets the lifetime. The API takes issuer URLs over HTTPS only, so put
the stub behind a TLS front that the exchange trusts. Caddy with `tls
internal` and `reverse_proxy 127.0.0.1:8791` does it on one host.

The check drives the stub through two control paths behind the same bearer:
`GET /fetches` counts the mint requests, in all and per service, and
`POST /fail` with `{"failing": true}` makes every mint answer 500 until
`{"failing": false}`.

## Run

```bash
SERVICE_URL=http://localhost:8780 \
SERVICE_TOKEN=<service-token> \
PROVIDER=docker \
ISSUER_URL=https://localhost:8443 \
ISSUER_BEARER=<bearer> \
ISSUER_CONTROL_URL=http://127.0.0.1:8791 \
GITHUB_REPO=<owner>/<private-repository> \
RESTART_EXCHANGE='docker compose restart exchange' \
python3 api-tests/acceptance/check.py
```

- `PROVIDER` is `docker`, `docker-sbx`, or `exe`.
- `ISSUER_URL` is the issuer as the exchange reaches it. The check appends
  `/mint/anthropic` and `/mint/github`.
- `ISSUER_CONTROL_URL` is the issuer as the check reaches it, for the two
  control paths. It defaults to `ISSUER_URL`.
- `RESTART_EXCHANGE` is a shell command that restarts the exchange process.
- `HOST_IMAGE` names the box image. `images/local` has `git` and `gh`. Add
  Claude Code on top of it for this run.
- `SSH_KEY` is the account key for a provider whose boxes take it, such as
  exe.
- `SBX_WORKSPACE_ROOT` turns on the docker-sbx checks. Run the check on the
  sbx host then, where `sbx` and the workspace root are.
- `HOST_ACTIVE_TIMEOUT` is the wait for an active host, in seconds. The
  default is 600. `POST /hosts` provisions before it answers, so the request
  waits that long too.

A box image for docker with Claude Code:

```Dockerfile
FROM drukbox/sandbox:local
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*
```

The check prints one line per step and a summary. It exits 1 when a step
fails. It deletes its host in every case, the host carries a one hour lease
in case the run dies, and the throwaway branch is named after the host.
