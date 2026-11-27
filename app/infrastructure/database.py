"""Database setup and credentials. Schema changes run via the explicit migrate command."""

import hashlib
import secrets
from uuid import uuid4

from sqlalchemy import BigInteger, cast, create_engine, event, func, insert, select
from sqlalchemy.engine import Connection, make_url

from migrations.v0001 import metadata, schema_revisions, users
from migrations.v0002 import applied, metadata as study_metadata

SCHEMA_VERSION = 1


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1,
                         maxmem=64 * 1024**2, dklen=32)
    return f"{salt.hex()}:{key.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    return secrets.compare_digest(password_hash(password, bytes.fromhex(encoded.split(":")[0])), encoded)


class Database:
    def __init__(self, url: str):
        parsed = make_url(url)
        if parsed.drivername not in {"sqlite", "mysql+pymysql"}:
            raise ValueError("Use mysql+pymysql or the local SQLite test backend")
        self.sqlite = parsed.drivername == "sqlite"
        options = {"connect_args": {"timeout": 10}} if self.sqlite else {}
        self.engine = create_engine(parsed, pool_pre_ping=True, **options)
        if self.sqlite:
            @event.listens_for(self.engine, "connect")
            def foreign_keys(conn, _):
                conn.execute("PRAGMA foreign_keys=ON")

    def initialize(self) -> None:
        # Initialization is run once as a deployment step, not by API/worker startup.
        from sqlalchemy import inspect

        with self.engine.begin() as conn:
            if self.sqlite:
                conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            if inspect(conn).has_table(schema_revisions.name):
                versions = conn.scalars(select(schema_revisions.c.version)).all()
                if versions and versions != [SCHEMA_VERSION]:
                    raise RuntimeError("Unsupported service schema version")
            metadata.create_all(conn)
            versions = conn.scalars(select(schema_revisions.c.version)).all()
            if not versions:
                conn.execute(insert(schema_revisions).values(version=SCHEMA_VERSION))
            elif versions != [SCHEMA_VERSION]:
                raise RuntimeError("Unsupported service schema version")

            study_metadata.create_all(conn)
            if conn.scalar(select(applied.c.version).where(applied.c.version == 2)) is None:
                conn.execute(insert(applied).values(version=2))

    def ready(self) -> bool:
        with self.engine.connect() as conn:
            return (
                conn.scalars(select(schema_revisions.c.version)).all() == [SCHEMA_VERSION]
                and conn.scalar(select(applied.c.version).where(applied.c.version == 2)) == 2
            )

    def clock(self):
        return cast(func.strftime("%s", "now"), BigInteger) if self.sqlite else func.unix_timestamp()

    def now(self, conn: Connection) -> int:
        return int(conn.scalar(select(self.clock())))

    def create_user(self, username: str, password: str) -> str:
        user_id = uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(insert(users).values(
                id=user_id, username=username.lower(), password_hash=password_hash(password),
            ))
        return user_id

    def close(self) -> None:
        self.engine.dispose()
