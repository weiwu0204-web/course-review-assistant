"""Submit a synthetic fixture to an isolated preview and verify it survives recreation."""

import argparse
from pathlib import Path
from tempfile import SpooledTemporaryFile
import time
from uuid import uuid4

import httpx


def response(res, expected):
    if res.status_code != expected:
        raise SystemExit(f"{res.request.method} {res.request.url.path}: {res.status_code}, expected {expected}")
    return res


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", type=int)
    parser.add_argument("phase", choices=("submit", "check"))
    parser.add_argument("user")
    parser.add_argument("other_user")
    parser.add_argument("password_file", type=Path)
    parser.add_argument("batch_file", type=Path)
    args = parser.parse_args()
    password = args.password_file.read_text(encoding="utf-8").strip()
    with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=5, trust_env=False) as client:
        response(client.get("/health/ready"), 200)

        def auth(name):
            data = response(client.post("/api/v1/sessions", json={"username": name, "password": password}), 201).json()
            return {"Authorization": f"Bearer {data['token']}"}

        owner, other = auth(args.user), auth(args.other_user)
        if args.phase == "submit":
            if args.batch_file.exists():
                raise SystemExit("Use a new marker path for submit")
            course = response(client.post("/api/v1/courses", headers=owner,
                                          json={"title": "Preview acceptance"}), 201).json()
            base = f"/api/v1/courses/{course['id']}"
            # Build a fixture so the script does not depend on private courseware.
            from pptx import Presentation
            ppt = Presentation()
            ppt.slides.add_slide(ppt.slide_layouts[0]).shapes.title.text = "Synthetic example"
            with SpooledTemporaryFile() as file:
                ppt.save(file)
                file.seek(0)
                material = response(client.post(base + "/materials", headers=owner,
                                                files={"file": ("acceptance.pptx", file)}), 201).json()
            batch = response(client.post(base + "/batches", headers={**owner, "Idempotency-Key": uuid4().hex},
                                         json={"materials": [material["id"]]}), 202).json()["id"]
        else:
            batch = args.batch_file.read_text(encoding="ascii").strip()
        url = f"/api/v1/batches/{batch}"
        end = time.monotonic() + 25
        while time.monotonic() < end:
            state = response(client.get(url, headers=owner), 200).json()["status"]
            if state == "done":
                break
            if state in {"failed", "cancelled", "partial"}:
                raise SystemExit(f"Unexpected batch status: {state}")
            time.sleep(0.25)
        else:
            raise SystemExit("Worker did not complete the task")
        doc = response(client.get(url + "/download", headers=owner), 200).text
        assert "acceptance.pptx" in doc and "部署演示" in doc
        response(client.get(url + "/download", headers=other), 404)
        if args.phase == "submit":
            args.batch_file.write_text(batch, encoding="ascii")
        print("Preview passed: API, worker, private storage, download and owner isolation.")


if __name__ == "__main__":
    main()
