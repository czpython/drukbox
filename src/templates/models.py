import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from uuid6 import uuid7

from core.database import Base
from hosts.models import UTCDateTime


class TemplateStatus(StrEnum):
    BUILDING = "building"
    AVAILABLE = "available"
    FAILED = "failed"


class Template(Base):
    __tablename__ = "templates"
    __table_args__ = (
        Index(
            "ix_templates_provider_base_image_setup_script_hash",
            "provider",
            "base_image",
            "setup_script_hash",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)
    provider: Mapped[str] = mapped_column(String(20))
    base_image: Mapped[str] = mapped_column(Text)
    setup_script_hash: Mapped[str] = mapped_column(String(64))
    setup_script: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text, default="")
    image: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default=TemplateStatus.BUILDING.value)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
        default=None,
    )
