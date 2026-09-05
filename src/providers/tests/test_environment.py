import shlex

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
