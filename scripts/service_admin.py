"""Initialize the service DB or provision a private preview account."""

import argparse
from getpass import getpass
import os
import re

from sqlalchemy.exc import IntegrityError

from app.infrastructure.service_config import ServiceSettings
from app.infrastructure.service_runtime import make_database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    cmds = parser.add_subparsers(dest="cmd", required=True)
    cmds.add_parser("migrate")
    user = cmds.add_parser("create-user")
    user.add_argument("username")
    args = parser.parse_args()
    db = make_database(ServiceSettings())
    try:
        if args.cmd == "migrate":
            db.initialize()
            print("Service schema ready.")
            return
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.username):
            raise SystemExit("Invalid username")
        password = os.environ.get("PPT_SERVICE_NEW_PASSWORD") or getpass("Password: ")
        if not 12 <= len(password) <= 256:
            raise SystemExit("Password length must be 12–256")
        try:
            db.create_user(args.username, password)
        except IntegrityError:
            raise SystemExit("Account already exists.") from None
        print("Account created.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
