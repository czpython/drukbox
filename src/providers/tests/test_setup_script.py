import pytest

from providers.setup_script import inject_authorized_keys, inject_env_exports


def test_returns_script_unchanged_when_no_env():
    script = "#!/usr/bin/env bash\necho hi\n"
    assert inject_env_exports(script, env=None) == script
    assert inject_env_exports(script, env={}) == script


def test_prepends_exports_after_shebang():
    script = "#!/usr/bin/env bash\nset -e\necho ready\n"
    out = inject_env_exports(script, env={"FOO": "bar", "QUOTE": "a b c"})
    # Shebang stays on the first line; exports follow before the body.
    shebang_idx = out.index("#!/usr/bin/env bash")
    export_idx = out.index("export FOO=bar")
    body_idx = out.index("set -e")
    assert shebang_idx < export_idx < body_idx
    # shell-quoted values guard against weird chars.
    assert "export QUOTE='a b c'" in out


def test_prepends_exports_to_non_shebanged_script():
    out = inject_env_exports("echo hi", env={"FOO": "bar"})
    assert out == "export FOO=bar\necho hi"


def test_rejects_invalid_env_names():
    with pytest.raises(ValueError, match="invalid VM environment variable name"):
        inject_env_exports("echo hi", env={"BAD-NAME": "v"})


def test_injects_additional_authorized_keys_after_the_shebang():
    script = inject_authorized_keys(
        "#!/usr/bin/env bash\necho ready\n",
        username="ubuntu",
        authorized_keys=("ssh-ed25519 AAAATUNNEL",),
    )

    assert script.startswith("#!/usr/bin/env bash\n")
    assert "drukbox_ssh_user=ubuntu" in script
    assert 'if [ -z "$drukbox_ssh_home" ]; then exit 1; fi' in script
    assert 'chmod 600 "$drukbox_ssh_home/.ssh/authorized_keys" || exit 1' in script
    assert "ssh-ed25519 AAAATUNNEL" in script
    assert script.index("ssh-ed25519 AAAATUNNEL") < script.index("echo ready")


def test_leaves_script_unchanged_without_additional_authorized_keys():
    script = "#!/usr/bin/env bash\necho ready\n"

    assert inject_authorized_keys(script, username="ubuntu", authorized_keys=()) == script


def test_adds_a_shell_shebang_when_the_key_is_the_only_bootstrap():
    script = inject_authorized_keys(
        "",
        username="ubuntu",
        authorized_keys=("ssh-ed25519 AAAATUNNEL",),
    )

    assert script.startswith("#!/bin/sh\n")
