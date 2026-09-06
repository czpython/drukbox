"""The sandbox environment: what a provider must do with the caller's env."""

import re
import shlex

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


def export(env: dict[str, str]) -> list[str]:
    """Shell ``export`` lines, one per variable, for the running script."""
    exports: list[str] = []
    for key, value in env.items():
        if not _NAME_RE.fullmatch(key):
            raise ValueError(f"invalid VM environment variable name: {key}")
        exports.append(f"export {key}={shlex.quote(value)}")
    return exports


def persist(env: dict[str, str]) -> list[str]:
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


def bashrc(env: dict[str, str]) -> str:
    """Shell that puts the exports at the top of ~/.bashrc. Every bash session reads them."""
    return "\n".join(
        [
            "cat > ~/.bashrc.new <<'DRUKBOX_ENV'",
            *export(env),
            "DRUKBOX_ENV",
            "cat ~/.bashrc >> ~/.bashrc.new && mv ~/.bashrc.new ~/.bashrc",
        ]
    )


def cloud_init(setup_script: str, env: dict[str, str] | None) -> str:
    """The user-data for a cloud VM: a shebang, ``env`` for the setup script
    and for every later session, then the setup script."""
    script = setup_script if setup_script.startswith("#!") else f"#!/bin/sh\n{setup_script}"
    shebang, _, body = script.partition("\n")
    lines = [shebang, *export(env or {}), *persist(env or {}), body]
    return "\n".join(line for line in lines if line)
