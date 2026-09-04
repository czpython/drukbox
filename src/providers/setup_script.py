import base64
import re
import shlex

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
_PROXY_CA_PATH = "/usr/local/share/ca-certificates/drukbox-secret-proxy.crt"


def _privileged_lines() -> list[str]:
    return [
        "drukbox_run_privileged() {",
        '  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi',
        "}",
    ]


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
            *_privileged_lines(),
            f"drukbox_ssh_user={quoted_username}",
            'drukbox_ssh_home="$(getent passwd "$drukbox_ssh_user" | cut -d: -f6)"',
            'if [ -z "$drukbox_ssh_home" ]; then exit 1; fi',
            'drukbox_ssh_uid="$(id -u "$drukbox_ssh_user")" || exit 1',
            'drukbox_ssh_gid="$(id -g "$drukbox_ssh_user")" || exit 1',
            'drukbox_run_privileged install -d -m 700 -o "$drukbox_ssh_uid" '
            '-g "$drukbox_ssh_gid" '
            '"$drukbox_ssh_home/.ssh" || exit 1',
            'drukbox_authorized_keys="$(mktemp)" || exit 1',
            'drukbox_run_privileged cat "$drukbox_ssh_home/.ssh/authorized_keys" '
            '> "$drukbox_authorized_keys" 2>/dev/null || true',
            f"""printf '%s\\n' {quoted_keys} >> "$drukbox_authorized_keys" || exit 1""",
            'drukbox_run_privileged install -m 600 -o "$drukbox_ssh_uid" '
            '-g "$drukbox_ssh_gid" "$drukbox_authorized_keys" '
            '"$drukbox_ssh_home/.ssh/authorized_keys" || exit 1',
            'rm -f "$drukbox_authorized_keys"',
        ]
        return _prepend_lines(script, lines)
    return script


def inject_secret_proxy_trust(
    script: str,
    *,
    ca_certificate: str,
    proxy_url: str | None,
) -> str:
    """Install the public proxy CA and optional per-host HTTPS proxy route."""
    if not ca_certificate.startswith("-----BEGIN CERTIFICATE-----\n"):
        raise ValueError("secret proxy CA certificate is invalid")

    encoded_ca = base64.b64encode(ca_certificate.encode("ascii")).decode("ascii")
    environment = {
        "SSL_CERT_FILE": _SYSTEM_CA_BUNDLE,
        "REQUESTS_CA_BUNDLE": _SYSTEM_CA_BUNDLE,
        "CURL_CA_BUNDLE": _SYSTEM_CA_BUNDLE,
        "NODE_EXTRA_CA_CERTS": _PROXY_CA_PATH,
        "NO_PROXY": "127.0.0.1,localhost,::1,169.254.169.254",
        "no_proxy": "127.0.0.1,localhost,::1,169.254.169.254",
    }
    if proxy_url:
        environment.update({"HTTPS_PROXY": proxy_url, "https_proxy": proxy_url})

    lines = [
        *_privileged_lines(),
        'drukbox_ca_file="$(mktemp)" || exit 1',
        f"printf '%s' {shlex.quote(encoded_ca)} | base64 -d > \"$drukbox_ca_file\" || exit 1",
        f'drukbox_run_privileged install -m 644 "$drukbox_ca_file" {_PROXY_CA_PATH} || exit 1',
        'rm -f "$drukbox_ca_file"',
        "drukbox_run_privileged update-ca-certificates >/dev/null || exit 1",
        'drukbox_environment_file="$(mktemp)" || exit 1',
        'drukbox_run_privileged cat /etc/environment > "$drukbox_environment_file" '
        "2>/dev/null || true",
    ]
    for key, value in environment.items():
        lines.extend(
            [
                f"sed -i '/^{key}=/d' \"$drukbox_environment_file\" || exit 1",
                f"printf '%s\\n' {shlex.quote(f'{key}={value}')} "
                '>> "$drukbox_environment_file" || exit 1',
            ]
        )
    lines.extend(
        [
            'drukbox_run_privileged install -m 644 "$drukbox_environment_file" '
            "/etc/environment || exit 1",
            'rm -f "$drukbox_environment_file"',
        ]
    )
    return _prepend_lines(script or "#!/bin/sh", lines)
