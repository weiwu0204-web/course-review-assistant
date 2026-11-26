"""Run one leased job, storing per-attempt artifacts outside the application process."""

from dataclasses import dataclass
from io import BytesIO
import logging
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import Callable

from app.generation.queue import Claim, JobQueue
from app.infrastructure.object_store import Objects, ObjectStoreError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Generated:
    markdown: str
    pages: int
    truncated: bool = False


Generate = Callable[[Path, Path | None, dict, Path], Generated]


class Worker:
    def __init__(self, queue: JobQueue, objects: Objects, generate: Generate):
        self.queue = queue
        self.objects = objects
        self.generate = generate

    def _read(self, key: str, path: Path) -> None:
        with self.objects.open(key) as source, path.open("wb") as dest:
            shutil.copyfileobj(source, dest, length=1024 * 1024)

    def _heartbeat(self, claim: Claim, stopped: Event, lost: Event) -> None:
        while not stopped.wait(self.queue.lease_seconds / 3):
            try:
                if self.queue.renew(claim):
                    continue
            except Exception:
                logger.warning("Could not renew job %s", claim.id)
            lost.set()
            return

    def run_one(self) -> bool:
        claim = self.queue.claim()
        if claim is None:
            return False
        stopped, lost = Event(), Event()
        heartbeat = Thread(target=self._heartbeat, args=(claim, stopped, lost), daemon=True)
        heartbeat.start()
        try:
            with TemporaryDirectory(prefix="ppt-review-job-") as directory:
                workdir = Path(directory)
                source = workdir / ("course" + Path(claim.filename).suffix.lower())
                self._read(claim.source_key, source)
                exam_source = None
                if claim.exam_key:
                    exam_source = workdir / ("exam" + Path(claim.exam_name or "exam.txt").suffix.lower())
                    self._read(claim.exam_key, exam_source)
                out = self.generate(source, exam_source, claim.options, workdir)
                if not out.markdown.strip():
                    raise ValueError("Empty generated document")
            if not lost.is_set():
                result_key = f"results/{claim.id}/{claim.token}"
                body = out.markdown.encode("utf-8")
                self.objects.put(result_key, BytesIO(body), len(body))
        except Exception as exc:
            # Exceptions from parsers and providers may contain user content.
            logger.warning("Job %s failed (%s)", claim.id, type(exc).__name__)
            self.queue.fail(claim, "generation_failed")
            return True
        finally:
            stopped.set()
            heartbeat.join()

        if lost.is_set():
            logger.info("Job %s abandoned after lease loss", claim.id)
            return True
        # If commit outcome is unknown, retain the object.
        if not self.queue.finish(claim, result_key=result_key, pages=out.pages, truncated=out.truncated):
            try:
                self.objects.remove(result_key)
            except ObjectStoreError:
                logger.warning("Could not clean stale output for job %s", claim.id)
        return True
