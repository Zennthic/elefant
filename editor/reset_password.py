"""Break-glass password reset for local Elefant accounts.

Installed in the editor image as /usr/local/bin/reset-password. Run it inside
the editor container when nobody can log in to reset a password from the UI:

  reset-password --list                 # list accounts and exit
  reset-password                        # interactive: list, then prompt
  reset-password <login>                # prompt for the new password
  reset-password <login> <password>     # non-interactive (scripts)

<login> is what the user types on the login page: `alice@acme.com`, or bare
`alice` for the default organisation.

It writes user_accounts.password_hash, the same row the login page checks,
and bumps the account's session epoch so every session signed in before the
reset is logged out (SEC-04). Only local accounts have a password here;
OIDC/SAML accounts sign in at their identity provider.

Works against whichever database the editor uses: POSTGRES_EDITOR_DSN when set,
else the SQLite file at DB_PATH.
"""
from __future__ import annotations

import getpass
import sys
from datetime import datetime
from typing import Optional

MIN_PASSWORD_LENGTH = 8


def _session():
    import config_db
    config_db._init_engine()
    return config_db.SessionLocal()


def list_users(session) -> list:
    from config_db import UserAccount
    return session.query(UserAccount).order_by(UserAccount.full_username).all()


def _print_users(users) -> None:
    if not users:
        print("No user accounts found.")
        return
    print(f"\n{'Login':<40}  {'Role':<15}  {'Auth':<6}  Active")
    print("─" * 72)
    for u in users:
        print(f"{u.full_username:<40}  {u.role:<15}  {u.auth_method:<6}  "
              f"{'yes' if u.is_active else 'no'}")
    print()


def reset_password(session, login: str, password: str) -> Optional[str]:
    """Set a local account's password and log out its sessions.

    Returns None on success, otherwise the error to show. Commits on success.
    """
    from auth import bump_session_epoch, hash_password, lookup_user_by_login

    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters."
    user = lookup_user_by_login(session, login)
    if user is None:
        return f"no user '{login}'. Run with --list to see existing users."
    if user.auth_method != "local":
        return (f"'{user.full_username}' signs in with {user.auth_method.upper()}; "
                "reset the password at the identity provider.")
    user.password_hash = hash_password(password)
    user.last_password_reset_at = datetime.utcnow()
    bump_session_epoch(user)
    session.commit()
    return None


def _prompt_password(login: str) -> str:
    password = getpass.getpass(f"New password for '{login}': ")
    if getpass.getpass("Confirm password: ") != password:
        print("error: passwords do not match.", file=sys.stderr)
        sys.exit(1)
    return password


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    session = _session()
    try:
        if not args or args[0] in ("--list", "-l"):
            _print_users(list_users(session))
            if args:
                return 0
            login = input("Login: ").strip()
            if not login:
                return 0
            password = _prompt_password(login)
        else:
            login = args[0]
            password = args[1] if len(args) >= 2 else _prompt_password(login)

        error = reset_password(session, login, password)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(f"Password updated for '{login}'. Existing sessions have been logged out.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
