from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MAX_DURABLE_PAYLOAD_BYTES = 262_144


class Base(DeclarativeBase):
    pass


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    applied_at: Mapped[str] = mapped_column(Text, nullable=False)


class InboxMessage(Base):
    __tablename__ = "inbox_messages"

    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    received_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            func.length(state).between(1, 64),
            name="ck_inbox_state_length",
        ),
        CheckConstraint(
            func.length(payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_inbox_payload_size",
        ),
        Index(
            "idx_inbox_state_received",
            state,
            received_at,
            message_id,
        ),
    )


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    message_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    stream: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    acked_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(sequence >= 0, name="ck_outbox_sequence_nonnegative"),
        CheckConstraint(
            state.in_(("pending", "acked", "retired")),
            name="ck_outbox_state",
        ),
        CheckConstraint(
            func.length(payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_outbox_payload_size",
        ),
        UniqueConstraint(
            "stream",
            "sequence",
            name="uq_outbox_stream_sequence",
        ),
        Index(
            "idx_outbox_pending_order",
            state,
            sequence,
            id,
        ),
        {"sqlite_autoincrement": True},
    )


class StreamCursor(Base):
    __tablename__ = "stream_cursors"

    stream: Mapped[str] = mapped_column(Text, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(sequence >= 0, name="ck_cursor_sequence_nonnegative"),
    )
