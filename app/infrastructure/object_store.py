"""Private object I/O. Callers publish a database reference only after a successful put."""

from contextlib import contextmanager
from pathlib import Path
import re
from shutil import copyfileobj
from typing import BinaryIO, ContextManager, Protocol

from botocore.exceptions import BotoCoreError, ClientError


class ObjectStoreError(RuntimeError):
    pass


class Objects(Protocol):
    def put(self, key: str, source: BinaryIO, size: int) -> None: ...
    def open(self, key: str) -> ContextManager[BinaryIO]: ...
    def remove(self, key: str) -> None: ...
    def ready(self) -> bool: ...


class LocalObjects:
    """Private development adapter. Use an S3-compatible bucket for separate hosts."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not re.fullmatch(r"[a-z0-9]+(?:/[a-z0-9]+)*", key):
            raise ValueError("Invalid internal object key")
        return self.root / key

    def put(self, key: str, source: BinaryIO, size: int) -> None:
        path = self._path(key)
        created = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as dst:
                created = True
                copyfileobj(source, dst, length=1024 * 1024)
                if dst.tell() != size:
                    raise OSError("Object length did not match")
        except OSError as exc:
            if created:
                self.remove(key)
            raise ObjectStoreError("could not write object") from exc

    @contextmanager
    def open(self, key: str):
        try:
            reader = self._path(key).open("rb")
        except OSError as exc:
            raise ObjectStoreError("could not open object") from exc
        with reader:
            yield reader

    def remove(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as exc:
            raise ObjectStoreError("could not delete object") from exc

    def ready(self) -> bool:
        return self.root.is_dir()


class S3Objects:
    def __init__(self, client, bucket: str):
        self.client = client
        self.bucket = bucket

    def put(self, key: str, source: BinaryIO, size: int) -> None:
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=source,
                                   ContentLength=size, IfNoneMatch="*")
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreError("could not write object") from exc

    @contextmanager
    def open(self, key: str):
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreError("could not open object") from exc
        try:
            yield body
        finally:
            body.close()

    def remove(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStoreError("could not delete object") from exc

    def ready(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (BotoCoreError, ClientError):
            return False
        return True
