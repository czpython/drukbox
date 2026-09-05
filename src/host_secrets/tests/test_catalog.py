from host_secrets.catalog import CATALOG

SERVICE_KEYS = {"host", "credential_header", "credential_prefix", "credential_var", "endpoint_var"}


def test_catalog_starts_with_the_three_built_in_services() -> None:
    assert set(CATALOG) == {"anthropic", "github", "openai"}


def test_every_entry_describes_a_service_the_way_put_secret_reads_it() -> None:
    for service in CATALOG.values():
        assert set(service) == SERVICE_KEYS
