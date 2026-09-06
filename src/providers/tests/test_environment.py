import os
import shlex
import shutil
import subprocess

import pytest

from providers import environment


def test_export_quotes_each_value_for_the_shell():
    assert environment.export({"FOO": "bar", "QUOTE": "a b c", "EMPTY": ""}) == [
        "export FOO=bar",
        "export QUOTE='a b c'",
        "export EMPTY=''",
    ]


def test_export_rejects_invalid_env_names():
    with pytest.raises(ValueError, match="invalid VM environment variable name"):
        environment.export({"BAD-NAME": "v"})


@pytest.mark.parametrize(
    "value",
    [
        "",
        "bar",
        "a b  c",
        "x=y=z",
        "http://host:8080/api.anthropic.com/v1",
        "!$%&()*+,-./:;<=>?@[]^_`{|}~",
    ],
)
def test_persist_accepts_values_pam_reads_back_unchanged(value):
    assert environment.persist({"KEY": value}) == [
        f"printf '%s\\n' {shlex.quote(f'KEY={value}')} >> /etc/environment"
    ]


@pytest.mark.parametrize(
    "value", ["ab#cd", '"abc', "it's", "a\\", " lead", "trail ", "a\nb", "a\tb", "café"]
)
def test_persist_rejects_values_pam_would_change(value):
    with pytest.raises(ValueError, match=r"must be printable ASCII .*: KEY"):
        environment.persist({"KEY": value})


def test_persist_accepts_a_line_of_8191_bytes_and_rejects_8192():
    longest = "a" * (8191 - len("KEY=\n"))
    assert environment.persist({"KEY": longest}) == [
        f"printf '%s\\n' KEY={longest} >> /etc/environment"
    ]
    with pytest.raises(ValueError, match="longer than 8191 bytes: KEY"):
        environment.persist({"KEY": longest + "a"})


def test_cloud_init_adds_a_shebang_exports_and_persists_the_env():
    out = environment.cloud_init("", env={"FOO": "bar", "URL": "http://x/y"})
    assert out.splitlines() == [
        "#!/bin/sh",
        "export FOO=bar",
        "export URL=http://x/y",
        "printf '%s\\n' FOO=bar >> /etc/environment",
        "printf '%s\\n' URL=http://x/y >> /etc/environment",
    ]


def test_cloud_init_keeps_the_setup_script_shebang_first():
    out = environment.cloud_init("#!/usr/bin/env bash\nset -e\necho ready\n", env={"FOO": "bar"})
    assert out.startswith("#!/usr/bin/env bash\nexport FOO=bar\nprintf")
    assert out.endswith("/etc/environment\nset -e\necho ready\n")


def test_cloud_init_without_env_is_the_setup_script():
    assert environment.cloud_init("#!/bin/sh\necho hi\n", env=None) == "#!/bin/sh\necho hi\n"


def test_trust_installs_the_proxy_ca_from_the_exported_variable_and_stops_on_failure():
    assert environment.trust({"SECRETS_PROXY_CA": "Q0VSVA==", "FOO": "bar"}) == [
        "printf '%s' \"$SECRETS_PROXY_CA\" | base64 -d | tee "
        "/usr/local/share/ca-certificates/drukbox.crt >/dev/null || exit 1",
        "update-ca-certificates >/dev/null || exit 1",
    ]


def test_trust_uses_sudo_for_a_script_that_runs_as_a_user():
    assert environment.trust({"SECRETS_PROXY_CA": "Q0VSVA=="}, sudo=True) == [
        "printf '%s' \"$SECRETS_PROXY_CA\" | base64 -d | sudo -n tee "
        "/usr/local/share/ca-certificates/drukbox.crt >/dev/null || exit 1",
        "sudo -n update-ca-certificates >/dev/null || exit 1",
    ]


def test_a_box_without_secrets_installs_nothing():
    assert environment.trust({"FOO": "bar"}) == []
    assert "ca-certificates" not in environment.cloud_init("echo hi", env={"FOO": "bar"})


GIT_LINES = [
    "git config --system --replace-all credential.https://github.com.helper '' || exit 1",
    "git config --system --add credential.https://github.com.helper '!gh auth git-credential' "
    "|| exit 1",
    "git config --system --replace-all url.https://github.com/.insteadOf git@github.com: || exit 1",
    "git config --system --add url.https://github.com/.insteadOf ssh://git@github.com/ || exit 1",
]


def test_github_points_git_at_gh_and_sends_ssh_remotes_over_https():
    assert environment.github({"GH_TOKEN": "drk.a.github.b"}) == GIT_LINES


def test_github_uses_sudo_for_a_script_that_runs_as_a_user():
    assert environment.github({"GH_TOKEN": "drk.a.github.b"}, sudo=True) == [
        f"sudo -n {line}" for line in GIT_LINES
    ]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_git_setup_runs_again_on_the_same_box(tmp_path):
    # A container restart runs the entrypoint again. git refuses to replace a
    # key with several values, so the lines must replace and then add.
    system = tmp_path / "gitconfig"
    script = "\n".join(environment.github({"GH_TOKEN": "drk.a.github.b"}))
    for _ in range(2):
        subprocess.run(
            ["sh", "-c", script],
            env={**os.environ, "GIT_CONFIG_SYSTEM": str(system)},
            check=True,
            timeout=60,
        )
    helpers = subprocess.run(
        ["git", "config", "--system", "--get-all", "credential.https://github.com.helper"],
        env={**os.environ, "GIT_CONFIG_SYSTEM": str(system)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert helpers == "\n!gh auth git-credential\n"


def test_a_box_without_a_github_secret_gets_no_git_setup():
    assert environment.github({"ANTHROPIC_AUTH_TOKEN": "drk.a.anthropic.b"}) == []
    assert "git config" not in environment.cloud_init("echo hi", env={"FOO": "bar"})


def test_cloud_init_sets_git_up_after_the_ca_and_before_the_script():
    out = environment.cloud_init("echo hi", env={"GH_TOKEN": "drk.a.github.b"})
    assert out.splitlines()[-5:] == [*GIT_LINES, "echo hi"]


def test_cloud_init_installs_the_ca_after_the_env_and_before_the_script():
    out = environment.cloud_init("echo hi", env={"SECRETS_PROXY_CA": "Q0VSVA=="})
    assert out.splitlines() == [
        "#!/bin/sh",
        "export SECRETS_PROXY_CA=Q0VSVA==",
        "printf '%s\\n' SECRETS_PROXY_CA=Q0VSVA== >> /etc/environment",
        "printf '%s' \"$SECRETS_PROXY_CA\" | base64 -d | tee "
        "/usr/local/share/ca-certificates/drukbox.crt >/dev/null || exit 1",
        "update-ca-certificates >/dev/null || exit 1",
        "echo hi",
    ]
