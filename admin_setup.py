
import getpass
import re
import sqlite3
import sys
from pathlib import Path

from pwdlib import PasswordHash


DATABASE = Path(__file__).resolve().parent / "rumchuo.db"
password_hash = PasswordHash.recommended()


def main():
    print("\nRumCHUO — One-time First Administrator Setup")
    print("=" * 46)

    if not DATABASE.is_file():
        print("ERROR: rumchuo.db was not found.")
        print("Run this script from your existing RumCHUO project.")
        sys.exit(1)

    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute("BEGIN IMMEDIATE")

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(students)"
            ).fetchall()
        }

        required = {
            "id",
            "full_name",
            "email",
            "password_hash",
            "email_verified",
            "role",
        }

        if not required.issubset(columns):
            raise RuntimeError(
                "The existing students table does not have the "
                "expected columns. No administrator was created."
            )

        existing_admin = connection.execute(
            "SELECT id FROM students WHERE role = 'admin' LIMIT 1"
        ).fetchone()

        if existing_admin:
            raise RuntimeError(
                "An administrator already exists. "
                "This one-time setup will not create another."
            )

        name = input("Administrator full name: ").strip()
        if len(name) < 2 or len(name) > 100:
            raise ValueError(
                "Name must contain between 2 and 100 characters."
            )

        email = input("Administrator email: ").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("Please enter a valid email address.")

        existing_user = connection.execute(
            "SELECT id FROM students WHERE email = ?",
            (email,),
        ).fetchone()

        if existing_user:
            raise RuntimeError(
                "That email already belongs to an account. "
                "No existing account was changed."
            )

        password = getpass.getpass(
            "Create a strong administrator password "
            "(at least 14 characters): "
        )
        confirmation = getpass.getpass("Confirm password: ")

        if len(password) < 14 or len(password) > 128:
            raise ValueError(
                "Password must contain 14 to 128 characters."
            )

        if password != confirmation:
            raise ValueError("Passwords do not match.")

        hashed_password = password_hash.hash(password)

        fields = [
            "full_name",
            "email",
            "password_hash",
            "email_verified",
            "role",
        ]
        values = [name, email, hashed_password, 1, "admin"]

        if "account_status" in columns:
            fields.append("account_status")
            values.append("active")

        placeholders = ", ".join("?" for _ in fields)
        field_names = ", ".join(fields)

        connection.execute(
            f"INSERT INTO students ({field_names}) "
            f"VALUES ({placeholders})",
            values,
        )

        connection.commit()

        print("\nAdministrator created successfully.")
        print(f"Email: {email}")
        print("The password was hashed before storage.")
        print("Keep your password private.")

    except Exception as error:
        connection.rollback()
        print(f"\nSetup stopped: {error}")
        sys.exit(1)

    finally:
        connection.close()


if __name__ == "__main__":
    main()
