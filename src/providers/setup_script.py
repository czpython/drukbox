import re
import shlex

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _prepend_lines(script: str, lines: list[str]) -> str:
    if script.startswith("#!"):
        shebang, separator, body = script.partition("\n")
        if separator:
            return "\n".join([shebang, *lines, body])
        return "\n".join([shebang, *lines])
    return "\n".join([*lines, script])


def inject_env_exports(script: str, env: dict[str, str] | None) -> str:
    """Return ``script`` with ``env`` prepended as shell ``export`` lines.

    When the script starts with a shebang the exports go between the
    shebang and the body so the interpreter line stays first.

    Raises ``ValueError`` for env keys that aren't valid shell identifiers.
    """
    if not env:
        return script
    exports: list[str] = []
    for key, value in env.items():
        if not _ENV_NAME_RE.fullmatch(key):
            raise ValueError(f"invalid VM environment variable name: {key}")
        exports.append(f"export {key}={shlex.quote(value)}")
    return _prepend_lines(script, exports)


def inject_authorized_keys(
    script: str,
    *,
    username: str,
    authorized_keys: tuple[str, ...],
) -> str:
    if authorized_keys:
        script = script or "#!/bin/sh"
        quoted_username = shlex.quote(username)
        quoted_keys = " ".join(shlex.quote(key) for key in authorized_keys)
        lines = [
            f"drukbox_ssh_user={quoted_username}",
            'drukbox_ssh_home="$(getent passwd "$drukbox_ssh_user" | cut -d: -f6)"',
            'if [ -z "$drukbox_ssh_home" ]; then exit 1; fi',
            'drukbox_ssh_uid="$(id -u "$drukbox_ssh_user")" || exit 1',
            'drukbox_ssh_gid="$(id -g "$drukbox_ssh_user")" || exit 1',
            'install -d -m 700 -o "$drukbox_ssh_uid" -g "$drukbox_ssh_gid" '
            '"$drukbox_ssh_home/.ssh" || exit 1',
            'touch "$drukbox_ssh_home/.ssh/authorized_keys" || exit 1',
            f"printf '%s\\n' {quoted_keys} >> \"$drukbox_ssh_home/.ssh/authorized_keys\" || exit 1",
            'chown "$drukbox_ssh_uid:$drukbox_ssh_gid" '
            '"$drukbox_ssh_home/.ssh/authorized_keys" || exit 1',
            'chmod 600 "$drukbox_ssh_home/.ssh/authorized_keys" || exit 1',
        ]
        return _prepend_lines(script, lines)
    return script
