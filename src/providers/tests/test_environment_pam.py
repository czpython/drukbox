"""Acceptance: pam_env in a real Debian container reads back every value."""

import shutil
import subprocess

import pytest

from providers import environment

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")

ENV = {
    # The longest line pam_env reads. A longer one ends the read, and the
    # entries after it are lost.
    "LONGEST": "a" * (environment.LINE_LIMIT - len("LONGEST=\n")),
    "PLACEHOLDER": "drk.0123abcd.anthropic.x-y_z-AbC",
    "BASE_URL": "http://secrets.example:8080/api.anthropic.com/v1",
    "SPACES": "a b  c",
    "EQUALS": "x=y=z",
    "SYMBOLS": "!$%&()*+,-./:;<=>?@[]^_`{|}~",
    "EMPTY": "",
}


@pytest.mark.parametrize("user", ["root", "nobody"])
def test_a_session_sees_each_value_unchanged(user):
    read_back = " ; ".join(f"printenv {key}" for key in ENV)
    script = "\n".join([*environment.persist(ENV), f"su -m {user} -s /bin/sh -c '{read_back}'"])
    result = subprocess.run(
        ["docker", "run", "--rm", "debian:bookworm-slim", "sh", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert result.stdout.split("\n")[: len(ENV)] == list(ENV.values())
