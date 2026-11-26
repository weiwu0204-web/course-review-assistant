"""Isolated MySQL/S3 acceptance: two API processes and a worker killed after claim."""

import os
from pathlib import Path
from socket import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.infrastructure.service_config import ServiceSettings
from app.infrastructure.service_runtime import make_database
from migrations.v0001 import jobs

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def await_ready(client):
    until = time.monotonic() + 20
    while time.monotonic() < until:
        try:
            if client.get("/health/ready").status_code == 200:
                return
        except httpx.RequestError:
            pass
        time.sleep(0.2)
    raise RuntimeError("Preview API did not become ready")


def checked(res, status):
    if res.status_code != status:
        raise RuntimeError(f"{res.request.method} {res.request.url.path}: expected {status}, got {res.status_code}")
    return res.json() if res.headers.get("content-type", "").startswith("application/json") else res.text


def main():
    if os.getenv("PPT_SERVICE_SMOKE_CONFIRM") != "isolated":
        raise SystemExit("Point at isolated MySQL and object storage and set PPT_SERVICE_SMOKE_CONFIRM=isolated")
    config = ServiceSettings()
    db = make_database(config)
    if db.sqlite or config.object_backend != "s3":
        raise SystemExit("Smoke requires MySQL and S3-compatible storage")
    processes = []
    with TemporaryDirectory(prefix="ppt-service-smoke-") as work:
        with (Path(work) / "app.log").open("wb") as log:
            env = {**os.environ, "PPT_SERVICE_GENERATOR": "demo", "PPT_SERVICE_LEASE_SECONDS": "3"}

            def launch(*args):
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                proc = subprocess.Popen([sys.executable, "-B", *args], cwd=ROOT, env=env,
                                        stdout=log, stderr=log, creationflags=flags)
                processes.append(proc)
                return proc

            try:
                names = ["smoke-" + uuid4().hex for _ in range(2)]
                passwords = [uuid4().hex for _ in names]
                for name, password in zip(names, passwords, strict=True):
                    db.create_user(name, password)
                ports = [port(), port()]
                if ports[0] == ports[1]:
                    raise RuntimeError("Cannot reserve distinct service ports")
                for value in ports:
                    launch("-m", "uvicorn", "app.service_main:create_app", "--factory",
                           "--host", "127.0.0.1", "--port", str(value))
                with (httpx.Client(base_url=f"http://127.0.0.1:{ports[0]}", timeout=4, trust_env=False) as a,
                      httpx.Client(base_url=f"http://127.0.0.1:{ports[1]}", timeout=4, trust_env=False) as b):
                    await_ready(a)
                    await_ready(b)
                    auth = []
                    for name, password in zip(names, passwords, strict=True):
                        login = checked(a.post("/api/v1/sessions", json={"username": name, "password": password}), 201)
                        auth.append({"Authorization": f"Bearer {login['token']}"})
                    owner, other = auth
                    course = checked(a.post("/api/v1/courses", json={"title": "Acceptance"}, headers=owner), 201)
                    resource = f"/api/v1/courses/{course['id']}"
                    material = checked(a.post(resource + "/materials", headers=owner,
                                       files={"file": ("fixture.pptx", (ROOT / "tests/samples/sample_course.pptx").read_bytes())}), 201)
                    submitted = checked(a.post(resource + "/batches",
                                               headers={**owner, "Idempotency-Key": "smoke"},
                                               json={"materials": [material["id"]]}), 202)
                    batch = f"/api/v1/batches/{submitted['id']}"
                    job_id = submitted["jobs"][0]["id"]
                    checked(b.get(batch, headers=other), 404)
                    old = checked(b.get(batch, headers=owner), 200)
                    assert old["jobs"][0]["revision"] == 1
                    checked(a.post(resource + f"/materials/{material['id']}/revisions",
                                   headers=owner, data={"base_revision": 1},
                                   files={"file": ("next.pptx", b"replacement only")}), 201)
                    # A child process claims and stays alive long enough to be forcefully stopped.
                    claimant = launch("-c", (
                        "from app.infrastructure.service_config import ServiceSettings; "
                        "from app.infrastructure.service_runtime import make_database; "
                        "from app.generation.queue import JobQueue; import time; "
                        "assert JobQueue(make_database(ServiceSettings()),lease_seconds=3).claim(); "
                        "time.sleep(15)"
                    ))
                    until = time.monotonic() + 10
                    while time.monotonic() < until:
                        with db.engine.connect() as conn:
                            if conn.scalar(select(jobs.c.status).where(jobs.c.id == job_id)) == "running":
                                break
                        time.sleep(0.1)
                    else:
                        raise RuntimeError("Worker did not claim the submitted job")
                    claimant.kill()
                    claimant.communicate(timeout=5)
                    time.sleep(3.2)
                    recovery = launch("-m", "app.worker", "--once")
                    if recovery.wait(timeout=20) != 0:
                        raise RuntimeError("Replacement worker failed")
                    final = checked(b.get(batch, headers=owner), 200)
                    assert final["status"] == "done" and final["jobs"][0]["attempts"] == 2
                    assert final["jobs"][0]["revision"] == 1
                    doc = checked(b.get(batch + "/download", headers=owner), 200)
                    assert "fixture.pptx" in doc and "next.pptx" not in doc
                    checked(b.get(batch + "/download", headers=other), 404)
            finally:
                for proc in reversed(processes):
                    if proc.poll() is None:
                        proc.terminate()
                    proc.communicate(timeout=5)
                db.close()
    print("Passed: two API processes, account isolation, file revisions, worker recovery, download.")


if __name__ == "__main__":
    main()
