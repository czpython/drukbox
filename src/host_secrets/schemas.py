from typing import Annotated, Self, TypedDict, cast

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

ENVIRONMENT_VARIABLE_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
HOST_PATTERN = (
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
SECRET_NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
REFRESH_PATTERN = r"^[1-9][0-9]*[smhd]$"

SecretValue = Annotated[SecretStr, Field(min_length=1)]
HeaderName = Annotated[
    str,
    StringConstraints(pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$"),
]


class SecretSourceStorage(TypedDict):
    url: str
    headers: dict[str, str]
    refresh: str


class SecretEntry(TypedDict, total=False):
    host: str
    auth_var: str
    placeholder: str
    value: str
    source: SecretSourceStorage


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

    def to_storage(self) -> SecretSourceStorage:
        return {
            "url": str(self.url),
            "headers": {name: value.get_secret_value() for name, value in self.headers.items()},
            "refresh": self.refresh,
        }


class SecretRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str | None = Field(default=None, max_length=253, pattern=HOST_PATTERN)
    auth_var: str | None = Field(default=None, pattern=ENVIRONMENT_VARIABLE_PATTERN)
    placeholder: str | None = Field(default=None, min_length=1, max_length=255)
    value: SecretValue | None = None
    source: SecretSource | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if bool(self.value) == bool(self.source):
            raise ValueError("provide exactly one of value or source")

        custom_fields = (self.host, self.auth_var, self.placeholder)
        if any(custom_fields) and not all(custom_fields):
            raise ValueError("custom secrets require host, auth_var, and placeholder")
        return self

    def to_storage(self) -> SecretEntry:
        entry = SecretEntry()
        if self.host:
            entry.update(
                {
                    "host": self.host,
                    "auth_var": cast(str, self.auth_var),
                    "placeholder": cast(str, self.placeholder),
                }
            )

        if self.value:
            entry["value"] = self.value.get_secret_value()
        elif self.source:
            entry["source"] = self.source.to_storage()
        return entry
