import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TemplateCreate(BaseModel):
    provider: str | None = Field(
        default=None,
        description="VM provider to materialize on. Omit to use the service default.",
    )
    base_image: str | None = Field(
        default=None,
        description="Provider image to build from. Omit to use the provider default.",
    )
    setup_script: str
    label: str = ""

    @field_validator("base_image")
    @classmethod
    def reject_blank_base_image(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("base_image must not be blank")
        return value

    @field_validator("setup_script")
    @classmethod
    def reject_blank_setup_script(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("setup_script must not be blank")
        return value


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    base_image: str
    requirements_hash: str
    label: str
    handle: str
    status: str
    last_error: str
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None
