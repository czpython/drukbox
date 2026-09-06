import base64

from host_secrets.catalog import CATALOG, Service, Upstream, service


def test_catalog_starts_with_the_three_built_in_services() -> None:
    assert set(CATALOG) == {"anthropic", "github", "openai"}


def test_every_entry_names_the_variable_a_client_reads_and_at_least_one_host() -> None:
    for entry in CATALOG.values():
        assert entry.auth_variable
        assert entry.upstreams


def test_github_is_one_service_with_the_api_uploads_and_git_hosts() -> None:
    api, uploads, git = CATALOG["github"].upstreams

    assert (api.host, api.credential("ghs_x")) == ("api.github.com", "Bearer ghs_x")
    assert (uploads.host, uploads.credential("ghs_x")) == ("uploads.github.com", "Bearer ghs_x")
    assert git.host == "github.com"
    assert git.credential("ghs_x") == "Basic " + base64.b64encode(b"x-access-token:ghs_x").decode()
    assert api.auth_header == uploads.auth_header == git.auth_header == "Authorization"


def test_a_custom_entry_names_one_host_with_its_own_header_shape() -> None:
    entry = {
        "host": "api.acme.test",
        "auth_header": "x-api-key",
        "auth_prefix": "",
        "auth_variable": "ACME_TOKEN",
        "value": "ak_live",
    }

    assert service("acme", entry) == Service(
        "ACME_TOKEN", (Upstream("api.acme.test", "x-api-key", ""),)
    )
    assert service("acme", entry).upstreams[0].credential("ak_live") == "ak_live"


def test_a_built_in_entry_resolves_through_the_catalog() -> None:
    assert service("github", {"value": "ghs_x"}) is CATALOG["github"]
