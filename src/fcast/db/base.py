"""Declarative base and shared column types."""

from datetime import UTC, datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Dialect, Enum, MetaData
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

# Stable constraint names are required for SQLite batch migrations in Alembic.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware datetimes as naive UTC and returns them as aware UTC datetimes.

    SQLite has no timezone support, so the conversion happens at the ORM boundary.
    Naive datetimes are rejected to avoid silently mixing local time and UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed, use timezone-aware UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def str_enum[E: PyEnum](enum_cls: type[E], length: int = 16) -> Enum:
    """Enum column stored as its string value (not the member name), without a native DB enum."""
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )
