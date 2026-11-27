"""Add study tables while retaining the version-1 compatibility marker."""

from sqlalchemy import BigInteger, Column, ForeignKey, Index, Integer, JSON, MetaData, String, Table

from migrations.v0001 import courses, jobs, users

metadata = MetaData()
applied = Table("service_migrations", metadata, Column("version", Integer, primary_key=True))
knowledge_bases = Table(
    "knowledge_bases", metadata,
    Column("id", String(32), primary_key=True),
    Column("course_id", ForeignKey(courses.c.id), nullable=False),
    Column("owner_id", ForeignKey(users.c.id), nullable=False),
    Column("object_key", String(96), nullable=False, unique=True),
    Column("sources", JSON, nullable=False),
    Column("counts", JSON, nullable=False),
    Column("created_at", BigInteger, nullable=False),
)
Index("ix_kb_owner_course", knowledge_bases.c.owner_id, knowledge_bases.c.course_id)
answers = Table(
    "study_answers", metadata,
    Column("id", String(32), primary_key=True),
    Column("kb_id", ForeignKey(knowledge_bases.c.id), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("record", JSON, nullable=False),
)
Index("ix_answer_kb_created", answers.c.kb_id, answers.c.created_at, answers.c.id)
generation_details = Table(
    "generation_details", metadata,
    Column("job_id", ForeignKey(jobs.c.id), primary_key=True),
    Column("detail", JSON, nullable=False),
)
