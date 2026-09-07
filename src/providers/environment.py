"""The sandbox environment: what a provider must do with the caller's env."""

import re
import shlex

from host_secrets.catalog import CATALOG

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# pam_env reads /etc/environment one entry per line, with its own parser: a
# `#` starts a comment, a quote opens a quoted value that needs no close, and
# a trailing backslash joins the next line. A value from this pattern passes
# through unchanged: printable ASCII without `#`, quotes, or backslashes, and
# without a space at either end.
VALUE_PATTERN = r"^(?:[!$-&(-\[\]-~](?:[ !$-&(-\[\]-~]*[!$-&(-\[\]-~])?)?$"
_VALUE_RE = re.compile(VALUE_PATTERN)
# pam_env reads a line into a buffer of 8192 bytes. A `KEY=VALUE\n` line that
# fills it ends the read, and every entry after it is lost too.
LINE_LIMIT = 8191
# The proxy's CA certificate, base64. The box installs it.
PROXY_CA = "SECRETS_PROXY_CA"
PROXY_CA_PATH = "/usr/local/share/ca-certificates/drukbox.crt"


def get_export(env: dict[str, str]) -> list[str]:
    """Shell ``export`` lines, one per variable, for the running script."""
    exports: list[str] = []
    for key, value in env.items():
        if not _NAME_RE.fullmatch(key):
            raise ValueError(f"invalid VM environment variable name: {key}")
        exports.append(f"export {key}={shlex.quote(value)}")
    return exports


def get_persist(env: dict[str, str]) -> list[str]:
    """Shell lines that write ``env`` to /etc/environment, so every later
    session gets it from PAM.

    Raises ``ValueError`` for a key that is not a shell identifier, a value
    outside ``VALUE_PATTERN``, or a line over ``LINE_LIMIT``.
    """
    lines: list[str] = []
    for key, value in env.items():
        if not _NAME_RE.fullmatch(key):
            raise ValueError(f"invalid VM environment variable name: {key}")
        if not _VALUE_RE.fullmatch(value):
            raise ValueError(
                "env value must be printable ASCII without #, quotes, backslashes, "
                f"or a space at either end: {key}"
            )
        if len(f"{key}={value}\n") > LINE_LIMIT:
            raise ValueError(f"env entry is longer than {LINE_LIMIT} bytes: {key}")
        lines.append(f"printf '%s\\n' {shlex.quote(f'{key}={value}')} >> /etc/environment")
    return lines


def get_bashrc(env: dict[str, str]) -> str:
    """Shell that puts the exports at the top of ~/.bashrc. Every bash session reads them."""
    return "\n".join(
        [
            "cat > ~/.bashrc.new <<'DRUKBOX_ENV'",
            *get_export(env),
            "DRUKBOX_ENV",
            "cat ~/.bashrc >> ~/.bashrc.new && mv ~/.bashrc.new ~/.bashrc",
        ]
    )


def get_install_ca(env: dict[str, str], *, sudo: bool = False) -> list[str]:
    """Lines after ``export``. A failed install ends the script: a box must not
    come up without trust in the proxy."""
    privileged = "sudo -n " if sudo else ""
    if PROXY_CA in env:
        return [
            f"printf '%s' \"${PROXY_CA}\" | base64 -d | {privileged}tee {PROXY_CA_PATH} >/dev/null"
            " || exit 1",
            f"{privileged}update-ca-certificates >/dev/null || exit 1",
        ]
    return []


def get_github(env: dict[str, str], *, sudo: bool = False) -> list[str]:
    """The lines ``gh auth setup-git`` writes, for every user, and SSH remotes
    sent over HTTPS. They can run again on the same box."""
    git = f"{'sudo -n ' if sudo else ''}git config --system"
    if CATALOG["github"].auth_variable in env:
        return [
            f"{git} --replace-all credential.https://github.com.helper '' || exit 1",
            f"{git} --add credential.https://github.com.helper '!gh auth git-credential' || exit 1",
            f"{git} --replace-all url.https://github.com/.insteadOf git@github.com: || exit 1",
            f"{git} --add url.https://github.com/.insteadOf ssh://git@github.com/ || exit 1",
        ]
    return []


def get_cloud_init(setup_script: str, env: dict[str, str] | None) -> str:
    """A shebang, ``env`` for the script and every session, the proxy's CA,
    git's setup, then the script."""
    script = setup_script if setup_script.startswith("#!") else f"#!/bin/sh\n{setup_script}"
    shebang, _, body = script.partition("\n")
    env = env or {}
    lines = [
        shebang,
        *get_export(env),
        *get_persist(env),
        *get_install_ca(env),
        *get_github(env),
        body,
    ]
    return "\n".join(line for line in lines if line)
