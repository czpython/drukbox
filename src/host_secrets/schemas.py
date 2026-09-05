from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from host_secrets.catalog import BEARER_HEADER, BEARER_PREFIX

HOST_PATTERN = (
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
SECRET_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
REFRESH_PATTERN = r"^[1-9][0-9]*[smhd]$"
SERVICE_FIELDS = frozenset(
    {"host", "credential_header", "credential_prefix", "credential_var", "endpoint_var"}
)

SecretValue = Annotated[SecretStr, Field(min_length=1)]
HeaderName = Annotated[str, StringConstraints(pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")]
EnvironmentVariable = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class SecretSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    headers: dict[HeaderName, SecretValue] = Field(min_length=1)
    refresh: str = Field(pattern=REFRESH_PATTERN)

    @field_validator("url")
    @classmethod
    def require_secure_source(cls, url: HttpUrl) -> HttpUrl:
        if url.scheme != "https":
            raise ValueError("source URL must use https")
        if url.username or url.password:
            raise ValueError("source URL must not contain credentials")
        if url.fragment:
            raise ValueError("source URL must not contain a fragment")
        return url

    def to_storage(self) -> dict[str, Any]:
        return {
            "url": str(self.url),
            "headers": {name: value.get_secret_value() for name, value in self.headers.items()},
            "refresh": self.refresh,
        }


class SecretRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str | None = Field(default=None, max_length=253, pattern=HOST_PATTERN)
    credential_var: EnvironmentVariable | None = None
    credential_header: HeaderName = BEARER_HEADER
    credential_prefix: str = BEARER_PREFIX
    # Empty when the client has no base URL variable.
    endpoint_var: str = Field(default="", pattern=r"^(?:[A-Za-z_][A-Za-z0-9_]*)?$")
    value: SecretValue | None = None
    source: SecretSource | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if bool(self.value) == bool(self.source):
            raise ValueError("provide exactly one of value or source")

        if SERVICE_FIELDS & self.model_fields_set and not (self.host and self.credential_var):
            raise ValueError("a custom service needs host and credential_var")
        return self

    def to_storage(self) -> dict[str, Any]:
        entry = self.model_dump(include=set(SERVICE_FIELDS)) if self.host else {}
        if self.value:
            entry["value"] = self.value.get_secret_value()
        elif self.source:
            entry["source"] = self.source.to_storage()
        return entry
