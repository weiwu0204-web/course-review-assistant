import argparse
import logging
import signal
from threading import Event

from sqlalchemy.exc import SQLAlchemyError

from app.generation.queue import JobQueue
from app.infrastructure.service_config import ServiceSettings
from app.infrastructure.service_runtime import make_database, make_objects
from app.worker.generator import make_generator
from app.worker.runner import Worker

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="PPT review worker")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = ServiceSettings()
    db = make_database(config)
    stopped = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    try:
        worker = Worker(JobQueue(db, lease_seconds=config.lease_seconds), make_objects(config),
                        make_generator(config))
        while not stopped.is_set():
            try:
                worked = worker.run_one()
            except SQLAlchemyError:
                if args.once:
                    raise
                logger.warning("Job store temporarily unavailable")
                stopped.wait(3)
                continue
            if args.once:
                return
            if not worked:
                stopped.wait(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
