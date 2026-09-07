import io
import re
import tarfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.docker.images import create_build_context, derive_image_name, remove_derived_image


def test_create_build_context_contains_the_base_and_verbatim_script() -> None:
    setup_script = "printf 'first\\nsecond\\n' | tee /tmp/output"

    context_tar = create_build_context(base_image="sandbox:base", setup_script=setup_script)

    with tarfile.open(fileobj=io.BytesIO(context_tar), mode="r:gz") as archive:
        members = {member.name: archive.extractfile(member) for member in archive.getmembers()}
        assert set(members) == {"Dockerfile", "setup.sh"}
        setup_member = members["setup.sh"]
        dockerfile_member = members["Dockerfile"]
        assert setup_member and setup_member.read() == setup_script.encode("utf-8")
        assert dockerfile_member and dockerfile_member.read().decode("utf-8") == (
            "FROM sandbox:base\n"
            "COPY setup.sh /drukbox-setup.sh\n"
            "RUN sh /drukbox-setup.sh && rm /drukbox-setup.sh\n"
        )


@pytest.mark.parametrize(
    ("label", "purpose"),
    [
        ("Site_Builder / Build.sh", "site-builder-build-sh"),
        ("../TAG:@!", "tag"),
        ("", "template"),
        ("💡", "template"),
        ("x" * 200, "x" * 95),
    ],
)
def test_image_tags_are_readable_valid_and_unique(label, purpose):
    first = derive_image_name(label=label, repository="ghcr.io/acme/templates")
    repeated = derive_image_name(label=label, repository="ghcr.io/acme/templates")

    assert first != repeated
    assert first.startswith(f"ghcr.io/acme/templates:{purpose}-")
    tag = first.rpartition(":")[2]
    assert len(tag) <= 128
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", tag)


async def test_delete_pinned_build_removes_its_tag():
    docker = MagicMock(remove_image=AsyncMock())
    tag = "ghcr.io/acme/templates:site-builder-build-unique"

    await remove_derived_image(docker, tag + "@sha256:" + "a" * 64)

    docker.remove_image.assert_awaited_once_with(tag)
