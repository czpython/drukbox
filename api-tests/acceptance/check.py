"""Acceptance from inside the box: what an agent does, through one provider's secrets.

Run by hand against a deployment, one provider per run. README.md says what to
set and what passes.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def setting(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        sys.exit(f"set {name}")
    return value


SERVICE_URL = setting("SERVICE_URL").rstrip("/")
SERVICE_TOKEN = setting("SERVICE_TOKEN")
PROVIDER = setting("PROVIDER")
ISSUER_URL = setting("ISSUER_URL").rstrip("/")
ISSUER_BEARER = setting("ISSUER_BEARER")
ISSUER_CONTROL_URL = setting("ISSUER_CONTROL_URL", ISSUER_URL).rstrip("/")
GITHUB_REPO = setting("GITHUB_REPO")
RESTART_EXCHANGE = setting("RESTART_EXCHANGE")
HOST_ACTIVE_TIMEOUT = int(setting("HOST_ACTIVE_TIMEOUT", "600"))
HOST_IMAGE = os.environ.get("HOST_IMAGE")
# A provider whose boxes take the account key, such as exe, needs the key file.
SSH_KEY = setting("SSH_KEY") if PROVIDER == "exe" else ""
SBX_WORKSPACE_ROOT = os.environ.get("SBX_WORKSPACE_ROOT")
# docker-sbx holds the value in sbx's own store, so the box never dials the exchange.
HELD = PROVIDER == "docker-sbx"

# The anthropic value lives 70 seconds, so the run sees a refresh and an outage.
# The github value lives an hour and carries the counts of one fetch per secret.
LIFETIMES = {"anthropic": "70s", "github": "1h"}
GIT = "GIT_TERMINAL_PROMPT=0 git -c user.name=drukbox -c user.email=drukbox@example.invalid"
PROMPT = "Reply with exactly the word ok and nothing else."
MODELS = "https://api.anthropic.com/v1/models?limit=1"
results: list[tuple[str, bool, str]] = []


def api(
    method: str, path: str, body: dict | None = None, timeout: int = 600
) -> tuple[int, dict | None]:
    request = urllib.request.Request(
        f"{SERVICE_URL}{path}",
        method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, (json.load(response) if response.status != 204 else None)
    except urllib.error.HTTPError as error:
        print(
            f"  {method} {path} answered {error.code}: "
            f"{error.read().decode(errors='replace')[:300]}"
        )
        return error.code, None


def issuer(path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{ISSUER_CONTROL_URL}{path}",
        method="POST" if body else "GET",
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {ISSUER_BEARER}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetches(service: str) -> int:
    """Mint requests for the service since this run began. The stub counts across runs."""
    return issuer("/fetches")["services"].get(service, 0) - at_start[service]


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'ok' if ok else 'FAIL'}] {name} {detail}")


def on_host(command: str) -> str:
    done = subprocess.run(
        command, shell=True, capture_output=True, text=True, errors="replace", timeout=600
    )
    return (done.stdout + done.stderr).strip()


def in_box(host: dict, script: str) -> str:
    with tempfile.TemporaryDirectory() as directory:
        key = f"{directory}/key"
        known = f"{directory}/known_hosts"
        with open(key, "w") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(host["private_key"] or pathlib.Path(SSH_KEY).read_text())
        with open(known, "w") as file:
            file.write(host["known_hosts"])
        command = [
            "ssh",
            "-i",
            key,
            "-o",
            f"UserKnownHostsFile={known}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=60",
            "-p",
            str(host["external_ssh_port"]),
            f"{host['ssh_username']}@{host['external_ssh_host']}",
            script,
        ]
        done = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=600
        )
        return done.stdout.strip() + (
            f"\n[stderr] {done.stderr.strip()[-300:]}" if done.returncode else ""
        )


def model_call(host: dict) -> str:
    script = (
        f"export ANTHROPIC_API_KEY=; timeout 120 claude -p {json.dumps(PROMPT)}"
        " --output-format text </dev/null 2>&1 | tail -1"
    )
    return in_box(host, script)


def status_of(host: dict, curl: str) -> str:
    return in_box(
        host,
        f"curl -s -m 25 -o /dev/null -w '%{{http_code}}' {curl} -H 'anthropic-version: 2023-06-01'",
    )


def entry(service: str) -> dict:
    return {
        "issuer": {
            "url": f"{ISSUER_URL}/mint/{service}",
            "headers": {"Authorization": f"Bearer {ISSUER_BEARER}"},
            "refresh": LIFETIMES[service],
        }
    }


print(f"== {PROVIDER}")
at_start = {service: 0 for service in LIFETIMES}
at_start = {service: fetches(service) for service in LIFETIMES}
# A lease, so a run that dies leaves no permanent host.
expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
body = {
    "provider": PROVIDER,
    "expires_at": expires_at,
    "secrets": {"anthropic": entry("anthropic"), "github": entry("github")},
}
if HOST_IMAGE:
    body["image"] = HOST_IMAGE
status, host = api("POST", "/hosts", body, timeout=HOST_ACTIVE_TIMEOUT)
check(
    "POST /hosts with two issuer-backed secrets answers 201, and echoes no secret",
    status == 201 and host is not None and "secrets" not in host,
    str(status),
)
if not host:
    sys.exit(1)
name = host["name"]
branch = f"drukbox-acceptance/{name}"
try:
    deadline = time.time() + HOST_ACTIVE_TIMEOUT
    while host["status"] not in ("active", "error") and time.time() < deadline:
        time.sleep(5)
        host = {**host, **api("GET", f"/hosts/{host['id']}")[1]}
    check(
        "the host becomes active",
        host["status"] == "active",
        f"{name} {host['status']} {host.get('last_error') or ''}",
    )
    if HELD:
        time.sleep(8)
        check(
            "the issuer was asked for each secret at provisioning, and again at first sight",
            fetches("github") == 2 and fetches("anthropic") >= 2,
            f"github={fetches('github')} anthropic={fetches('anthropic')}",
        )
    else:
        check("the issuer was not asked at boot", fetches("github") + fetches("anthropic") == 0)

    env = in_box(host, 'echo "$ANTHROPIC_AUTH_TOKEN" | cut -c1-12; echo "$GH_TOKEN" | cut -c1-12')
    check(
        "a plain session sees both placeholders", env.count("drk.") == 2, env.replace("\n", " | ")
    )
    tools = in_box(host, "command -v claude git gh | wc -l")
    check("claude, git, and gh are in the box", tools == "3", tools[-40:])

    if SBX_WORKSPACE_ROOT:
        listing = on_host(f"sbx secret ls --sandbox {name}")
        check(
            "sbx holds the github service secret and a custom secret for api.anthropic.com",
            "github" in listing and "api.anthropic.com" in listing,
            listing.replace("\n", " | ")[-120:],
        )
        rows = [re.split(r"\s{2,}", line.strip()) for line in on_host("sbx secret ls").splitlines()]
        check(
            "no sbx secret is global",
            not [row for row in rows if len(row) >= 4 and row[0] == "global"],
        )
        modes = {
            service: stat.S_IMODE(os.stat(f"{SBX_WORKSPACE_ROOT}/secrets/{name}/{service}").st_mode)
            for service in ("anthropic", "github")
        }
        check(
            "the value files are 0600 beside the workspaces",
            all(mode == 0o600 for mode in modes.values()),
            " ".join(f"{service}={mode:o}" for service, mode in modes.items()),
        )

    answer = model_call(host)
    check("claude -p answers ok", answer.endswith("ok"), answer[-60:])
    check(
        "gh api reaches the private repository",
        in_box(host, f"gh api repos/{GITHUB_REPO} --jq .full_name 2>&1 | tail -1") == GITHUB_REPO,
    )
    check(
        "gh pr list works",
        in_box(host, f"gh pr list -R {GITHUB_REPO} --limit 3 >/dev/null 2>&1 && echo listed")
        == "listed",
    )
    check(
        "the issuer was asked once for the github secret",
        fetches("github") == (2 if HELD else 1),
        f"github={fetches('github')}",
    )

    clone = in_box(
        host,
        f"rm -rf /tmp/repo && {GIT} clone -q https://github.com/{GITHUB_REPO}.git /tmp/repo 2>&1"
        " && echo cloned",
    )
    check("git clone of the private repository", clone.endswith("cloned"), clone[-100:])
    push = in_box(
        host,
        f"cd /tmp/repo && {GIT} checkout -q -b {branch} && date > acceptance.txt"
        f" && {GIT} add acceptance.txt && {GIT} commit -q -m acceptance"
        f" && {GIT} push -q origin {branch} 2>&1 && echo pushed",
    )
    check("git push of a throwaway branch", push.endswith("pushed"), push[-100:])
    delete = in_box(
        host, f"cd /tmp/repo && {GIT} push -q origin --delete {branch} 2>&1 && echo deleted"
    )
    check("the throwaway branch is deleted", delete.endswith("deleted"), delete[-100:])

    direct = status_of(
        host, f"--noproxy '*' {MODELS} -H \"Authorization: Bearer $ANTHROPIC_AUTH_TOKEN\""
    )
    check("the placeholder sent straight to Anthropic is refused with 401", direct == "401", direct)
    wrong = status_of(host, f'{MODELS} -H "Authorization: Bearer ${{ANTHROPIC_AUTH_TOKEN%??}}xx"')
    check(
        "a wrong placeholder is refused" + ("" if HELD else " with 403 by the exchange"),
        wrong == ("401" if HELD else "403"),
        wrong,
    )

    # A fresh value first, then the issuer fails while that value nears its end.
    answer = model_call(host)
    asked = fetches("anthropic")
    issuer("/fail", {"failing": True})
    try:
        time.sleep(20)
        answer = model_call(host)
        time.sleep(2)
        check(
            "while the issuer fails, the held value keeps the box working",
            answer.endswith("ok") and fetches("anthropic") > asked,
            f"{answer[-40:]} failed attempts={fetches('anthropic') - asked}",
        )
    finally:
        issuer("/fail", {"failing": False})

    asked = fetches("github")
    restarted = subprocess.run(
        RESTART_EXCHANGE, shell=True, capture_output=True, text=True, timeout=300
    )
    check(
        "the exchange restarts",
        restarted.returncode == 0,
        (restarted.stdout + restarted.stderr).strip()[-100:],
    )
    time.sleep(10)
    answer = model_call(host)
    check("after the restart claude -p answers ok again", answer.endswith("ok"), answer[-60:])
    check(
        "after the restart gh still works",
        in_box(host, f"gh api repos/{GITHUB_REPO} --jq .full_name 2>&1 | tail -1") == GITHUB_REPO,
    )
    check(
        "the restart cost one fetch of the github secret",
        fetches("github") - asked == 1,
        f"github={fetches('github') - asked}",
    )

    if PROVIDER == "docker" and shutil.which("docker"):
        # The entrypoint runs again at a restart. sshd must come back, and
        # the git setup must stay single.
        restarted = subprocess.run(
            ["docker", "restart", name], capture_output=True, text=True, timeout=120
        )
        time.sleep(3)
        port = (
            subprocess.run(["docker", "port", name, "22"], capture_output=True, text=True)
            .stdout.strip()
            .rsplit(":", 1)[-1]
        )
        host = {
            **host,
            "external_ssh_port": int(port),
            "known_hosts": host["known_hosts"].replace(
                f"]:{host['external_ssh_port']} ", f"]:{port} "
            ),
        }
        helpers = in_box(
            host,
            "git config --system --get-all credential.https://github.com.helper | wc -l;"
            f" gh api repos/{GITHUB_REPO} --jq .full_name 2>&1 | tail -1",
        )
        check(
            "a restarted box comes back with the same git setup and gh still works",
            restarted.returncode == 0 and helpers.startswith("2") and GITHUB_REPO in helpers,
            helpers.replace("\n", " | ")[-80:],
        )
finally:
    status, _ = api("DELETE", f"/hosts/{host['id']}")
    check("DELETE /hosts answers 204", status == 204, str(status))
    if SBX_WORKSPACE_ROOT:
        check(
            "teardown leaves no secret in sbx secret ls",
            "No secrets found" in on_host(f"sbx secret ls --sandbox {name}"),
        )
        check(
            "teardown leaves no value file and no workspace",
            not os.path.exists(f"{SBX_WORKSPACE_ROOT}/secrets/{name}")
            and not os.path.exists(f"{SBX_WORKSPACE_ROOT}/{name}"),
        )

failed = [check_name for check_name, ok, _ in results if not ok]
print(
    f"== {PROVIDER}: {len(results) - len(failed)}/{len(results)} passed"
    + (f", failed: {', '.join(failed)}" if failed else "")
)
sys.exit(1 if failed else 0)
