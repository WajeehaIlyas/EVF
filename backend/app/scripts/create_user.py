"""Create a user, reset a password, or check a login.

    python -m app.scripts.create_user <username>            create the user / reset the password
    python -m app.scripts.create_user <username> --check    say whether a password would be accepted
    python -m app.scripts.create_user --list                list the users in the database

Every run prints which database it is talking to: the site only sees users in the database the
*running backend* uses, so compare this line with the backend's own DATABASE_URL.
"""

import getpass
import sys

from sqlalchemy import select

from app.api.v1.auth import create_or_update_user
from app.core.database import SessionLocal, engine, init_db
from app.db.models import User
from app.services.auth_core import verify_password


def main() -> int:
    args = sys.argv[1:]
    print(f"Database: {engine.url.render_as_string(hide_password=True)}")
    init_db()

    if args == ["--list"]:
        with SessionLocal() as db:
            for user in db.scalars(select(User).order_by(User.username)):
                print(f"  {user.username}  active={user.is_active}")
        return 0

    if not args or len(args) > 2 or (len(args) == 2 and args[1] != "--check"):
        print(__doc__)
        return 2
    username = args[0].strip().lower()

    if len(args) == 2:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.username == username))
        if not user:
            print(f"No user '{username}' in this database.")
            return 1
        ok = verify_password(getpass.getpass("Password: "), user.password_hash)
        print("Password accepted." if ok else "Password does NOT match.")
        return 0 if ok else 1

    password = getpass.getpass("Password: ")
    if len(password) < 8 or password != getpass.getpass("Repeat password: "):
        print("Passwords must match and be at least 8 characters.")
        return 1
    with SessionLocal() as db:
        user = create_or_update_user(db, username, password)
    print(f"User '{user.username}' is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
