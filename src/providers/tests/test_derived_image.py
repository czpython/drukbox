from providers.derived_image import derived_image_context, derived_image_tag


def test_derived_image_context_contains_the_base_and_verbatim_script() -> None:
    setup_script = "printf 'first\\nsecond\\n' | tee /tmp/output"

    with derived_image_context(base_image="sandbox:base", setup_script=setup_script) as context:
        assert context.joinpath("setup.sh").read_bytes() == setup_script.encode("utf-8")
        assert context.joinpath("Dockerfile").read_text(encoding="utf-8") == (
            "FROM sandbox:base\n"
            "COPY setup.sh /drukbox-setup.sh\n"
            "RUN sh /drukbox-setup.sh && rm /drukbox-setup.sh\n"
        )


def test_derived_image_tag_is_deterministic_and_base_specific() -> None:
    first = derived_image_tag(base_image="sandbox:base", setup_script="apt-get update")
    repeated = derived_image_tag(base_image="sandbox:base", setup_script="apt-get update")
    different_base = derived_image_tag(base_image="sandbox:other", setup_script="apt-get update")

    assert first == repeated
    assert first.startswith("drukbox-template:")
    assert len(first.removeprefix("drukbox-template:")) == 12
    assert different_base != first
