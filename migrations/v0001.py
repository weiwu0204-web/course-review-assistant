"""Initial service schema. Keep this definition stable for existing databases."""

from sqlalchemy import BigInteger, Column, ForeignKey, Index, Integer, JSON, MetaData, String, Table, UniqueConstraint

metadata = MetaData()
schema_revisions = Table("schema_revisions", metadata, Column("version", Integer, primary_key=True))

users = Table(
    "accounts", metadata,
    Column("id", String(32), primary_key=True),
    Column("username", String(64), nullable=False, unique=True),
    Column("password_hash", String(172), nullable=False),
)
sessions = Table(
    "sessions", metadata,
    Column("digest", String(64), primary_key=True),
    Column("owner_id", ForeignKey(users.c.id, ondelete="CASCADE"), nullable=False),
    Column("expires_at", BigInteger, nullable=False, index=True),
)
courses = Table(
    "courses", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner_id", ForeignKey(users.c.id), nullable=False, index=True),
    Column("title", String(200), nullable=False),
)
materials = Table(
    "materials", metadata,
    Column("id", String(32), primary_key=True),
    Column("course_id", ForeignKey(courses.c.id), nullable=False, index=True),
    Column("kind", String(16), nullable=False),
    Column("revision", Integer, nullable=False),
)
revisions = Table(
    "material_revisions", metadata,
    Column("id", String(32), primary_key=True),
    Column("material_id", ForeignKey(materials.c.id), nullable=False),
    Column("number", Integer, nullable=False),
    Column("name", String(255), nullable=False),
    Column("object_key", String(96), nullable=False, unique=True),
    Column("bytes", BigInteger, nullable=False),
    UniqueConstraint("material_id", "number"),
)
batches = Table(
    "batches", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner_id", ForeignKey(users.c.id), nullable=False),
    Column("course_id", ForeignKey(courses.c.id), nullable=False),
    Column("course_title", String(200), nullable=False),
    Column("request_key", String(64), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("options", JSON, nullable=False),
    Column("exam_revision_id", ForeignKey(revisions.c.id)),
    Column("created_at", BigInteger, nullable=False),
    UniqueConstraint("owner_id", "request_key"),
)
Index("ix_batches_owner_created", batches.c.owner_id, batches.c.created_at, batches.c.id)

jobs = Table(
    "jobs", metadata,
    Column("id", String(32), primary_key=True),
    Column("batch_id", ForeignKey(batches.c.id), nullable=False),
    Column("position", Integer, nullable=False),
    Column("revision_id", ForeignKey(revisions.c.id), nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("claims", Integer, nullable=False, default=0),
    Column("lease_token", String(32)),
    Column("lease_until", BigInteger),
    Column("result_key", String(96)),
    Column("pages", Integer, nullable=False, default=0),
    Column("truncated", Integer, nullable=False, default=0),
    Column("error", String(64)),
    UniqueConstraint("batch_id", "position"),
)
Index("ix_jobs_status_lease", jobs.c.status, jobs.c.lease_until)
