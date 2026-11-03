import os, re, sqlite3, hashlib, secrets, smtplib, ssl
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from pydantic import BaseModel, Field, field_validator
from pwdlib import PasswordHash

load_dotenv()

APP_NAME, APP_VERSION, DATABASE_NAME = "RumCHUO", "1.10.0", "rumchuo.db"
SECRET_KEY = os.getenv("RUMCHUO_SECRET_KEY", "")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES, VERIFICATION_TOKEN_MINUTES = 30, 30
PASSWORD_RESET_TOKEN_MINUTES = 30
SMTP_EMAIL = os.getenv("SMTP_EMAIL", "").strip()
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "").replace(" ", "").strip()
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
PUBLIC_BASE_URL = os.getenv("RUMCHUO_PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

if not SECRET_KEY:
    raise RuntimeError("RUMCHUO_SECRET_KEY is missing from your .env file.")

app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="Student accommodation and roommate matching platform."
)


@app.get("/student/dashboard")
def student_dashboard():
    dashboard_file = Path(__file__).parent / "student_dashboard.html"

    if not dashboard_file.exists():
        raise HTTPException(
            status_code=404,
            detail="Student dashboard file was not found."
        )

    return FileResponse(dashboard_file)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:3001",
        "http://localhost:3001",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:5501",
        "http://localhost:5501"
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"]
)

security = HTTPBearer(auto_error=False)
password_hash = PasswordHash.recommended()

PROFILE_FIELDS = {
    "full_name",
    "university",
    "campus",
    "faculty",
    "course",
    "year_of_study",
    "gender",
    "phone",
    "accommodation_status"
}

BASE_DIR = Path(__file__).resolve().parent

PRIVATE_PHOTO_DIR = BASE_DIR / "private_ownership_photos"
AGENT_PHOTO_DIR = BASE_DIR / "private_agent_property_photos"

# Budgets are no longer part of active student preferences.
PREFERENCE_FIELDS = (
    "preferred_location",
    "preferred_campus",
    "room_type"
)

ProximityTier = Literal[
    "under_5_min_walk",
    "5_to_15_min_walk",
    "requires_transport"
]


@contextmanager
def get_connection():
    connection = sqlite3.connect(DATABASE_NAME)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_columns(connection, table_name):
    return {
        row["name"]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def initialize_database():
    with get_connection() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                location TEXT NOT NULL,
                price REAL NOT NULL,
                available INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        room_additions = {
            "location": "TEXT",
            "price": "REAL",
            "available": "INTEGER NOT NULL DEFAULT 1",
            "created_at": "TEXT",
            "room_type": "TEXT NOT NULL DEFAULT 'single'",
            "owner_id": "INTEGER REFERENCES students(id)",
            "ownership_status": "TEXT NOT NULL DEFAULT 'unassigned'",
            "nearest_campus": "TEXT",
            "proximity_tier": "TEXT"
        }

        student_additions = {
            "university": "TEXT",
            "faculty": "TEXT",
            "course": "TEXT",
            "year_of_study": "INTEGER",
            "gender": "TEXT",
            # Retained for compatibility with existing databases.
            # These columns are no longer used by preferences or matching.
            "budget_min": "REAL",
            "budget_max": "REAL",
            "preferred_location": "TEXT",
            "room_type": "TEXT",
            "email_verified": "INTEGER NOT NULL DEFAULT 0",
            "email_verification_token_hash": "TEXT",
            "email_verification_expires_at": "TEXT",
            "role": "TEXT NOT NULL DEFAULT 'student'",
            "account_status": "TEXT NOT NULL DEFAULT 'active'",
            "phone": "TEXT",
            "campus": "TEXT",
            "accommodation_status": "TEXT",
            "preferred_campus": "TEXT",
            "password_reset_token_hash": "TEXT",
            "password_reset_expires_at": "TEXT"
        }

        for table, additions in (
            ("rooms", room_additions),
            ("students", student_additions)
        ):
            existing = get_columns(c, table)
            for col, definition in additions.items():
                if col not in existing:
                    c.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} {definition}"
                    )

        c.execute("""
            UPDATE rooms
            SET ownership_status='unassigned'
            WHERE owner_id IS NULL
            AND (ownership_status IS NULL OR ownership_status='')
        """)

        c.execute("CREATE INDEX IF NOT EXISTS idx_rooms_location ON rooms(location)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_rooms_price ON rooms(price)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_rooms_type ON rooms(room_type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_rooms_owner ON rooms(owner_id)")
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_rooms_campus
            ON rooms(nearest_campus, proximity_tier)
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_students_email ON students(email)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_students_role ON students(role)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_room_authorizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                agent_id INTEGER NOT NULL REFERENCES students(id),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','approved','rejected','revoked')),
                advertising_enabled INTEGER NOT NULL DEFAULT 1,
                requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT,
                reviewed_by INTEGER REFERENCES students(id),
                review_note TEXT NOT NULL DEFAULT '',
                UNIQUE(room_id, agent_id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS room_rental_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                agent_id INTEGER NOT NULL REFERENCES students(id),
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT,
                reviewed_by INTEGER REFERENCES students(id)
            )
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_room_status
            ON agent_room_authorizations(status, room_id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_rental_reports_status
            ON room_rental_reports(status)
        """)

        if c.execute("SELECT COUNT(*) AS n FROM rooms").fetchone()["n"] == 0:
            c.executemany("""
                INSERT INTO rooms(
                    location, price, available, room_type, ownership_status
                )
                VALUES(?,?,?,?, 'unassigned')
            """, [
                ("Near UDSM", 150000, 1, "single"),
                ("Mlimani City area", 180000, 1, "self-contained"),
                ("Mwenge", 120000, 1, "shared")
            ])


initialize_database()


def utc_now():
    return datetime.now(timezone.utc)


def normalize_email(email):
    return email.strip().lower()


def validate_email_format(email):
    email = normalize_email(email)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Enter a valid email address.")
    return email


def hash_verification_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def student_to_dict(s):
    return {
        "id": s["id"],
        "full_name": s["full_name"],
        "email": s["email"],
        "email_verified": bool(s["email_verified"]),
        "role": s["role"],
        "university": s["university"],
        "campus": s["campus"],
        "faculty": s["faculty"],
        "course": s["course"],
        "year_of_study": s["year_of_study"],
        "gender": s["gender"],
        "phone": s["phone"],
        "accommodation_status": s["accommodation_status"],
        "preferred_location": s["preferred_location"],
        "preferred_campus": s["preferred_campus"],
        "room_type": s["room_type"],
        "created_at": s["created_at"]
    }


def room_to_dict(room):
    keys = room.keys()
    return {
        "id": room["id"],
        "location": room["location"],
        "price": room["price"],
        "available": bool(room["available"]),
        "room_type": room["room_type"] or "single",
        "created_at": room["created_at"],
        "ownership_status": (
            room["ownership_status"]
            if "ownership_status" in keys
            else "unassigned"
        ),
        "nearest_campus": (
            room["nearest_campus"]
            if "nearest_campus" in keys
            else None
        ),
        "proximity_tier": (
            room["proximity_tier"]
            if "proximity_tier" in keys
            else None
        )
    }


def create_access_token(student_id):
    now = utc_now()
    return jwt.encode(
        {
            "sub": str(student_id),
            "iat": now,
            "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        },
        SECRET_KEY,
        algorithm=ALGORITHM
    )


def send_verification_email(email, full_name, token):
    if not SMTP_EMAIL or not SMTP_APP_PASSWORD:
        raise RuntimeError(
            "SMTP_EMAIL or SMTP_APP_PASSWORD is not configured."
        )

    url = f"{PUBLIC_BASE_URL}/students/verify-email?token={quote(token, safe='')}"

    msg = EmailMessage()
    msg["Subject"] = "Verify your RumCHUO email"
    msg["From"] = SMTP_EMAIL
    msg["To"] = email

    msg.set_content(
        f"Hello {full_name},\n\n"
        "Thank you for registering with RumCHUO.\n\n"
        "Verify your email using this link:\n\n"
        f"{url}\n\n"
        f"This link expires in {VERIFICATION_TOKEN_MINUTES} minutes.\n\n"
        "If you did not create this account, ignore this email.\n\n"
        "RumCHUO"
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(SMTP_EMAIL, SMTP_APP_PASSWORD)
        server.send_message(msg)


def send_password_reset_email(email, full_name, token):
    if not SMTP_EMAIL or not SMTP_APP_PASSWORD:
        raise RuntimeError(
            "SMTP_EMAIL or SMTP_APP_PASSWORD is not configured."
        )

    url = f"{PUBLIC_BASE_URL}/reset-password?token={quote(token, safe='')}"

    msg = EmailMessage()
    msg["Subject"] = "Reset your RumCHUO password"
    msg["From"] = SMTP_EMAIL
    msg["To"] = email

    msg.set_content(
        f"Hello {full_name},\n\n"
        "We received a request to reset your RumCHUO password.\n\n"
        "Choose a new password using this link:\n\n"
        f"{url}\n\n"
        f"This link expires in {PASSWORD_RESET_TOKEN_MINUTES} minutes.\n\n"
        "If you did not request this, ignore this email. "
        "Your password will not change.\n\n"
        "RumCHUO"
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(SMTP_EMAIL, SMTP_APP_PASSWORD)
        server.send_message(msg)


def issue_verification_email(student_id, email, full_name):
    token = secrets.token_urlsafe(32)
    token_hash = hash_verification_token(token)
    expiry = (
        utc_now() + timedelta(minutes=VERIFICATION_TOKEN_MINUTES)
    ).isoformat()

    with get_connection() as c:
        c.execute("""
            UPDATE students
            SET email_verification_token_hash=?,
                email_verification_expires_at=?
            WHERE id=?
        """, (token_hash, expiry, student_id))

    send_verification_email(email, full_name, token)


# MODELS

class StudentRegister(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    email: str
    password: str = Field(min_length=8, max_length=128)

    @field_validator("full_name")
    @classmethod
    def clean_name(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Enter your full name.")
        return v

    @field_validator("email")
    @classmethod
    def clean_email(cls, v):
        return validate_email_format(v)


class StudentLogin(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def clean_email(cls, v):
        return validate_email_format(v)


class ResendVerification(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def clean_email(cls, v):
        return validate_email_format(v)


class ForgotPassword(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def clean_email(cls, v):
        return validate_email_format(v)


class ResetPassword(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=8, max_length=128)


class ChangePassword(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class StudentProfileUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=100)
    university: str | None = Field(default=None, max_length=150)
    campus: str | None = Field(default=None, max_length=100)
    faculty: str | None = Field(default=None, max_length=150)
    course: str | None = Field(default=None, max_length=150)
    year_of_study: int | None = Field(default=None, ge=1, le=10)
    gender: Literal["male", "female", "other"] | None = None
    phone: str | None = Field(default=None, max_length=25)
    accommodation_status: Literal[
        "looking_for_room",
        "has_room_needs_roommate",
        "not_looking"
    ] | None = None

    @field_validator(
        "full_name", "university", "campus", "faculty",
        "course", "phone",
        mode="before"
    )
    @classmethod
    def clean_optional_text(cls, v):
        return v.strip() or None if isinstance(v, str) else v

    @field_validator("phone")
    @classmethod
    def check_phone(cls, v):
        if v is not None:
            if not re.fullmatch(r"[+0-9()\-\s]{5,25}", v):
                raise ValueError("Enter a valid phone number.")
            if not 5 <= len(re.sub(r"\D", "", v)) <= 20:
                raise ValueError("Enter a valid phone number.")
        return v


class StudentPreferencesUpdate(BaseModel):
    preferred_location: str | None = Field(default=None, max_length=150)
    preferred_campus: str | None = Field(default=None, max_length=100)
    room_type: Literal["single", "shared", "any"] | None = None

    @field_validator("preferred_location", "preferred_campus")
    @classmethod
    def clean_location(cls, v):
        return v.strip() or None if isinstance(v, str) else v


class RoomCreate(BaseModel):
    location: str = Field(min_length=2, max_length=150)
    price: float = Field(gt=0)
    available: bool = True
    room_type: Literal["single", "shared", "self-contained"] = "single"

    @field_validator("location")
    @classmethod
    def clean_location(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Enter a valid room location.")
        return v


class RoomUpdate(BaseModel):
    location: str | None = Field(default=None, min_length=2, max_length=150)
    price: float | None = Field(default=None, gt=0)
    available: bool | None = None
    room_type: Literal["single", "shared", "self-contained"] | None = None

    @field_validator("location")
    @classmethod
    def clean_location(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if len(v) < 2:
                raise ValueError("Enter a valid room location.")
        return v


class RoomProximity(BaseModel):
    nearest_campus: str = Field(min_length=2, max_length=50)
    proximity_tier: ProximityTier

    @field_validator("nearest_campus")
    @classmethod
    def clean_campus(cls, v):
        v = v.strip().upper()
        if len(v) < 2:
            raise ValueError("Enter a valid campus code.")
        return v


class AgentRoomRequest(BaseModel):
    room_id: int = Field(gt=0)
    note: str = Field(default="", max_length=500)


class AuthorizationDecision(BaseModel):
    decision: Literal["approve", "reject", "revoke"]
    note: str = Field(default="", max_length=500)


class RentalReport(BaseModel):
    note: str = Field(default="", max_length=500)


class OwnerAssignment(BaseModel):
    owner_id: int = Field(gt=0)
    confirmation_note: str = Field(min_length=5, max_length=500)


# AUTHENTICATION AND ROLE CHECKS

def get_current_student(
    credentials: HTTPAuthorizationCredentials | None = Depends(security)
):
    if credentials is None:
        raise HTTPException(401, "Please log in to continue.")

    try:
        payload = jwt.decode(
            credentials.credentials,
            SECRET_KEY,
            algorithms=[ALGORITHM]
        )
        sid = payload.get("sub")

        if not sid:
            raise InvalidTokenError("Missing subject")

        sid = int(sid)

    except (InvalidTokenError, ValueError, TypeError):
        raise HTTPException(
            401,
            "Your session is invalid or expired. Please log in again."
        )

    with get_connection() as c:
        s = c.execute(
            "SELECT * FROM students WHERE id=?",
            (sid,)
        ).fetchone()

    if s is None:
        raise HTTPException(401, "Student account not found.")

    if s["account_status"] == "suspended":
        raise HTTPException(
            403,
            "This account is suspended. Contact RumCHUO support."
        )

    if not s["email_verified"]:
        raise HTTPException(
            403,
            "Please verify your email before using this feature."
        )

    return s


def require_room_manager(student=Depends(get_current_student)):
    if student["role"] not in {"landlord", "admin"}:
        raise HTTPException(
            403,
            "Only an Owner or administrator can manage original room records."
        )
    return student


def require_admin(student=Depends(get_current_student)):
    if student["role"] != "admin":
        raise HTTPException(
            403,
            "Only an administrator can create original room records. "
            "Owners must submit properties for verification."
        )
    return student


def require_landlord(student=Depends(get_current_student)):
    if student["role"] != "landlord":
        raise HTTPException(
            403,
            "Only an Owner can perform this action."
        )
    return student


def require_agent(student=Depends(get_current_student)):
    if student["role"] != "agent":
        raise HTTPException(
            403,
            "Only an approved Agent can perform this action."
        )
    return student


# GENERAL ROUTES

@app.get("/")
def home():
    file_path = Path(__file__).resolve().parent / "index.html"

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail="RumCHUO homepage (index.html) was not found."
        )

    return FileResponse(
        file_path,
        media_type="text/html"
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "app": APP_NAME,
        "version": APP_VERSION
    }


# STUDENT REGISTRATION AND AUTHENTICATION

@app.post("/students/register", status_code=201)
def register_student(data: StudentRegister):
    email = normalize_email(data.email)
    hashed = password_hash.hash(data.password)

    try:
        with get_connection() as c:
            cur = c.execute("""
                INSERT INTO students(
                    full_name, email, password_hash, email_verified
                )
                VALUES(?,?,?,0)
            """, (data.full_name, email, hashed))
            sid = cur.lastrowid

    except sqlite3.IntegrityError:
        raise HTTPException(
            409,
            "An account with this email already exists."
        )

    try:
        issue_verification_email(sid, email, data.full_name)
    except Exception:
        raise HTTPException(
            503,
            "Your account was created but the verification email could not "
            "be sent. Check server configuration, then request another email."
        )

    return {
        "message": "Registration successful. Check your email and verify before logging in.",
        "email": email,
        "email_verified": False,
        "role": "student"
    }


@app.post("/students/resend-verification")
def resend_verification(data: ResendVerification):
    with get_connection() as c:
        s = c.execute(
            "SELECT * FROM students WHERE email=?",
            (normalize_email(data.email),)
        ).fetchone()

    generic = {
        "message": "If an unverified account exists for that email, a verification email will be sent."
    }

    if s is None or s["email_verified"]:
        return generic

    try:
        issue_verification_email(s["id"], s["email"], s["full_name"])
    except Exception:
        raise HTTPException(
            503,
            "The verification email could not be sent. Please try again later."
        )

    return generic


@app.get("/students/verify-email")
def verify_email(token: str = Query(min_length=20)):
    token_hash = hash_verification_token(token)

    with get_connection() as c:
        s = c.execute("""
            SELECT id, email_verified, email_verification_expires_at
            FROM students
            WHERE email_verification_token_hash=?
        """, (token_hash,)).fetchone()

    if s is None:
        raise HTTPException(
            400,
            "This verification link is invalid or has already been used."
        )

    if s["email_verified"]:
        return {
            "message": "Your email address is already verified."
        }

    try:
        expiry = datetime.fromisoformat(
            s["email_verification_expires_at"]
        )
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        expiry = None

    if expiry is None or expiry <= utc_now():
        with get_connection() as c:
            c.execute("""
                UPDATE students
                SET email_verification_token_hash=NULL,
                    email_verification_expires_at=NULL
                WHERE id=?
            """, (s["id"],))

        raise HTTPException(
            400,
            "This verification link has expired. Request a new link."
        )

    with get_connection() as c:
        c.execute("""
            UPDATE students
            SET email_verified=1,
                email_verification_token_hash=NULL,
                email_verification_expires_at=NULL
            WHERE id=?
            AND email_verification_token_hash=?
        """, (s["id"], token_hash))

    return {
        "message": "Email verified successfully. You can now log in.",
        "email_verified": True
    }


@app.post("/students/login")
def login_student(data: StudentLogin):
    with get_connection() as c:
        s = c.execute(
            "SELECT * FROM students WHERE email=?",
            (normalize_email(data.email),)
        ).fetchone()

    if s is None or not password_hash.verify(
        data.password,
        s["password_hash"]
    ):
        raise HTTPException(401, "Invalid email or password.")

    if s["account_status"] == "suspended":
        raise HTTPException(
            403,
            "This account is suspended. Contact RumCHUO support."
        )

    if not s["email_verified"]:
        raise HTTPException(
            403,
            "Please verify your email before logging in."
        )

    return {
        "access_token": create_access_token(s["id"]),
        "token_type": "bearer",
        "expires_in_minutes": ACCESS_TOKEN_EXPIRE_MINUTES,
        "student": student_to_dict(s)
    }


# PASSWORD RESET AND CHANGE

@app.get("/reset-password", include_in_schema=False)
def reset_password_page():
    file_path = Path(__file__).resolve().parent / "reset_password.html"

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Password reset page (reset_password.html) was not found."
        )

    return FileResponse(file_path, media_type="text/html")


@app.post("/students/forgot-password")
def forgot_password(data: ForgotPassword):
    generic = {
        "message": "If an account exists for that email, a password reset link will be sent."
    }

    with get_connection() as c:
        s = c.execute("""
            SELECT id, full_name, email
            FROM students
            WHERE email=?
            AND email_verified=1
            AND account_status='active'
        """, (normalize_email(data.email),)).fetchone()

        if s is None:
            return generic

        token = secrets.token_urlsafe(32)
        expiry = (
            utc_now() + timedelta(minutes=PASSWORD_RESET_TOKEN_MINUTES)
        ).isoformat()

        c.execute("""
            UPDATE students
            SET password_reset_token_hash=?,
                password_reset_expires_at=?
            WHERE id=?
        """, (hash_verification_token(token), expiry, s["id"]))

    try:
        send_password_reset_email(s["email"], s["full_name"], token)
    except Exception:
        pass

    return generic


@app.post("/students/reset-password")
def reset_password(data: ResetPassword):
    token_hash = hash_verification_token(data.token)

    with get_connection() as c:
        s = c.execute("""
            SELECT id, password_reset_expires_at
            FROM students
            WHERE password_reset_token_hash=?
        """, (token_hash,)).fetchone()

        expiry = None

        if s is not None:
            try:
                expiry = datetime.fromisoformat(
                    s["password_reset_expires_at"]
                )
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                expiry = None

        if s is None or expiry is None or expiry <= utc_now():
            raise HTTPException(
                400,
                "This password reset link is invalid or has expired."
            )

        c.execute("""
            UPDATE students
            SET password_hash=?,
                password_reset_token_hash=NULL,
                password_reset_expires_at=NULL
            WHERE id=?
        """, (password_hash.hash(data.new_password), s["id"]))

    return {
        "message": "Password updated successfully. You can now log in."
    }


@app.post("/students/me/change-password")
def change_password(
    data: ChangePassword,
    student=Depends(get_current_student)
):
    if not password_hash.verify(
        data.current_password,
        student["password_hash"]
    ):
        raise HTTPException(400, "Current password is incorrect.")

    if data.current_password == data.new_password:
        raise HTTPException(
            400,
            "The new password must be different from the current password."
        )

    with get_connection() as c:
        c.execute(
            "UPDATE students SET password_hash=? WHERE id=?",
            (password_hash.hash(data.new_password), student["id"])
        )

    return {"message": "Password changed successfully."}


# PROFILE AND PREFERENCES

@app.get("/students/me")
def get_my_profile(student=Depends(get_current_student)):
    return student_to_dict(student)


@app.put("/students/me/profile")
def update_my_profile(
    data: StudentProfileUpdate,
    student=Depends(get_current_student)
):
    updates = {
        k: v
        for k, v in data.model_dump(exclude_unset=True).items()
        if k in PROFILE_FIELDS
    }

    if updates:
        with get_connection() as c:
            c.execute(
                "UPDATE students SET "
                + ", ".join(f"{k}=?" for k in updates)
                + " WHERE id=?",
                list(updates.values()) + [student["id"]]
            )

    with get_connection() as c:
        s = c.execute(
            "SELECT * FROM students WHERE id=?",
            (student["id"],)
        ).fetchone()

    return student_to_dict(s)


@app.get("/students/me/preferences")
def get_my_preferences(student=Depends(get_current_student)):
    return {k: student[k] for k in PREFERENCE_FIELDS}


@app.put("/students/me/preferences")
def update_my_preferences(
    data: StudentPreferencesUpdate,
    student=Depends(get_current_student)
):
    updates = data.model_dump(exclude_unset=True)

    updates = {
        k: v
        for k, v in updates.items()
        if k in PREFERENCE_FIELDS
    }

    if updates:
        with get_connection() as c:
            c.execute(
                "UPDATE students SET "
                + ", ".join(f"{k}=?" for k in updates)
                + " WHERE id=?",
                list(updates.values()) + [student["id"]]
            )

    with get_connection() as c:
        s = c.execute(
            "SELECT * FROM students WHERE id=?",
            (student["id"],)
        ).fetchone()

    return {k: s[k] for k in PREFERENCE_FIELDS}


# APPROVED AGENT PROPERTY HELPERS

def get_approved_agent_application(c, room_id):
    """
    Find the approved direct Agent property application for a room.

    Direct Agent properties are stored in agent_property_applications.
    The Admin approval workflow connects the application to the room.
    """

    try:
        return c.execute("""
            SELECT
                a.id AS application_id,
                a.agent_id,
                a.description,
                a.phone_number,
                a.location,
                a.status,
                s.full_name AS agent_name,
                s.university,
                s.phone AS account_phone
            FROM agent_property_applications a
            JOIN students s ON s.id=a.agent_id
            WHERE a.room_id=?
            AND a.status='approved'
            AND s.role='agent'
            AND s.account_status='active'
            AND s.email_verified=1
            ORDER BY a.id DESC
            LIMIT 1
        """, (room_id,)).fetchone()

    except sqlite3.OperationalError:
        return None


def get_agent_property_photos(c, room_id):
    """Get photographs belonging to an approved direct Agent property."""

    try:
        rows = c.execute("""
            SELECT
                p.id,
                p.original_name,
                p.content_type
            FROM agent_property_photos p
            JOIN agent_property_applications a
                ON a.id=p.application_id
            WHERE a.room_id=?
            AND a.status='approved'
            ORDER BY p.id
        """, (room_id,)).fetchall()

        return [
            {
                "id": row["id"],
                "original_name": row["original_name"],
                "content_type": row["content_type"]
            }
            for row in rows
        ]

    except sqlite3.OperationalError:
        return []


# PUBLIC ROOM RESPONSES

def add_public_agents(c, room_id):
    """
    Return Agent names for direct Agent properties or
    Owner properties advertised by authorized Agents.
    """

    direct_application = get_approved_agent_application(c, room_id)

    if direct_application is not None:
        return [{
            "agent_id": direct_application["agent_id"],
            "agent_name": direct_application["agent_name"]
        }]

    rows = c.execute("""
        SELECT s.id, s.full_name
        FROM agent_room_authorizations a
        JOIN students s ON s.id=a.agent_id
        WHERE a.room_id=?
        AND a.status='approved'
        AND a.advertising_enabled=1
        AND s.account_status='active'
        AND s.email_verified=1
        ORDER BY s.full_name
    """, (room_id,)).fetchall()

    return [
        {
            "agent_id": r["id"],
            "agent_name": r["full_name"]
        }
        for r in rows
    ]


def get_room_photos(c, room_id):
    """
    Retrieve photographs from either:
    - An approved direct Agent property.
    - The latest approved Owner verification application.
    """

    direct_application = get_approved_agent_application(c, room_id)

    if direct_application is not None:
        return get_agent_property_photos(c, room_id)

    try:
        rows = c.execute("""
            SELECT p.id, p.original_name, p.content_type
            FROM rooms r
            JOIN room_ownership_applications a
                ON a.room_id=r.id
                AND a.landlord_id=r.owner_id
                AND a.status='approved'
            JOIN room_ownership_photos p
                ON p.application_id=a.id
            WHERE r.id=?
            AND a.id=(
                SELECT MAX(a2.id)
                FROM room_ownership_applications a2
                WHERE a2.room_id=r.id
                AND a2.landlord_id=r.owner_id
                AND a2.status='approved'
            )
            ORDER BY p.id
        """, (room_id,)).fetchall()

    except sqlite3.OperationalError:
        return []

    return [
        {
            "id": r["id"],
            "original_name": r["original_name"],
            "content_type": r["content_type"]
        }
        for r in rows
    ]


def get_agents_with_contact(c, room_id):
    """
    Return verified Agent contact information to logged-in students.

    For direct Agent properties, the phone number submitted with
    the property takes priority over the Agent account phone number.
    """

    direct_application = get_approved_agent_application(c, room_id)

    if direct_application is not None:
        return [{
            "agent_id": direct_application["agent_id"],
            "agent_name": direct_application["agent_name"],
            "phone": (
                direct_application["phone_number"]
                or direct_application["account_phone"]
            ),
            "university": direct_application["university"]
        }]

    rows = c.execute("""
        SELECT s.id, s.full_name, s.phone, s.university
        FROM agent_room_authorizations a
        JOIN students s ON s.id=a.agent_id
        WHERE a.room_id=?
        AND a.status='approved'
        AND a.advertising_enabled=1
        AND s.role='agent'
        AND s.account_status='active'
        AND s.email_verified=1
        ORDER BY s.full_name
    """, (room_id,)).fetchall()

    return [
        {
            "agent_id": r["id"],
            "agent_name": r["full_name"],
            "phone": r["phone"],
            "university": r["university"]
        }
        for r in rows
    ]


@app.get("/rooms")
def list_rooms(
    location: str | None = None,
    campus: str | None = None,
    proximity: ProximityTier | None = None,
    max_price: float | None = Query(default=None, gt=0),
    room_type: Literal["single", "shared", "self-contained"] | None = None,
    available_only: bool = True,
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0)
):
    conditions = []
    params = []

    if location and location.strip():
        conditions.append("LOWER(location) LIKE ?")
        params.append(f"%{location.strip().lower()}%")

    if campus and campus.strip():
        conditions.append("UPPER(nearest_campus)=?")
        params.append(campus.strip().upper())

    if proximity:
        conditions.append("proximity_tier=?")
        params.append(proximity)

    if max_price is not None:
        conditions.append("price<=?")
        params.append(max_price)

    if room_type:
        conditions.append("room_type=?")
        params.append(room_type)

    if available_only:
        conditions.append("available=1")

    conditions.append("ownership_status='assigned'")
    conditions.append("owner_id IS NOT NULL")

    where = "WHERE " + " AND ".join(conditions)

    with get_connection() as c:
        total = c.execute(
            f"SELECT COUNT(*) n FROM rooms {where}",
            params
        ).fetchone()["n"]

        rows = c.execute(
            f"""
            SELECT *
            FROM rooms {where}
            ORDER BY price, id
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset]
        ).fetchall()

        rooms = []

        for r in rows:
            item = room_to_dict(r)

            application = get_approved_agent_application(c, r["id"])

            item["agents"] = add_public_agents(c, r["id"])

            item["description"] = (
                application["description"]
                if application is not None
                else None
            )

            photos = get_room_photos(c, r["id"])

            item["photo_ids"] = [ph["id"] for ph in photos]
            item["photo_count"] = len(photos)

            rooms.append(item)

    return {
        "total_rooms": total,
        "limit": limit,
        "offset": offset,
        "returned_rooms": len(rooms),
        "has_more": offset + len(rooms) < total,
        "rooms": rooms
    }


@app.get("/rooms/{room_id}")
def get_room(room_id: int):
    with get_connection() as c:
        r = c.execute(
            "SELECT * FROM rooms WHERE id=?",
            (room_id,)
        ).fetchone()

        if r is None:
            raise HTTPException(404, "Room not found.")

        if r["ownership_status"] != "assigned" or r["owner_id"] is None:
            raise HTTPException(404, "Verified room not found.")

        result = room_to_dict(r)

        application = get_approved_agent_application(c, room_id)

        result["agents"] = add_public_agents(c, room_id)

        result["description"] = (
            application["description"]
            if application is not None
            else None
        )

    return result


@app.get("/rooms/{room_id}/details")
def get_room_details(
    room_id: int,
    student=Depends(get_current_student)
):
    """Full room details for logged-in students."""

    with get_connection() as c:
        r = c.execute(
            "SELECT * FROM rooms WHERE id=?",
            (room_id,)
        ).fetchone()

        if (
            r is None
            or r["ownership_status"] != "assigned"
            or r["owner_id"] is None
        ):
            raise HTTPException(404, "Verified room not found.")

        result = room_to_dict(r)

        application = get_approved_agent_application(c, room_id)

        result["description"] = (
            application["description"]
            if application is not None
            else None
        )

        result["photos"] = get_room_photos(c, room_id)
        result["agents"] = get_agents_with_contact(c, room_id)

    return result


@app.get("/rooms/{room_id}/photos/{photo_id}")
def get_room_photo(
    room_id: int,
    photo_id: int,
    student=Depends(get_current_student)
):
    """Serve verified Owner or Agent photographs to logged-in students."""

    with get_connection() as c:
        r = c.execute(
            "SELECT ownership_status, owner_id FROM rooms WHERE id=?",
            (room_id,)
        ).fetchone()

        if (
            r is None
            or r["ownership_status"] != "assigned"
            or r["owner_id"] is None
        ):
            raise HTTPException(404, "Verified room not found.")

        direct_application = get_approved_agent_application(c, room_id)

        if direct_application is not None:
            photo = c.execute("""
                SELECT stored_name, content_type
                FROM agent_property_photos
                WHERE id=?
                AND application_id=?
            """, (
                photo_id,
                direct_application["application_id"]
            )).fetchone()

            photo_directory = AGENT_PHOTO_DIR

        else:
            allowed = {
                p["id"]
                for p in get_room_photos(c, room_id)
            }

            photo = None

            if photo_id in allowed:
                photo = c.execute("""
                    SELECT stored_name, content_type
                    FROM room_ownership_photos
                    WHERE id=?
                """, (photo_id,)).fetchone()

            photo_directory = PRIVATE_PHOTO_DIR

    if photo is None:
        raise HTTPException(404, "Property photograph not found.")

    path = photo_directory / Path(photo["stored_name"]).name

    if not path.is_file():
        raise HTTPException(
            404,
            "The stored photograph could not be found."
        )

    return FileResponse(
        path,
        media_type=photo["content_type"],
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff"
        }
    )


# IMPORTANT SECURITY:
# Only administrators can create original room records here.
# Owners must use the property verification workflow.

@app.post("/rooms", status_code=201)
def create_room(
    data: RoomCreate,
    student=Depends(require_admin)
):
    with get_connection() as c:
        cur = c.execute("""
            INSERT INTO rooms(
                location,
                price,
                available,
                room_type,
                owner_id,
                ownership_status
            )
            VALUES(?,?,?,?,NULL,'unassigned')
        """, (
            data.location,
            data.price,
            int(data.available),
            data.room_type
        ))

        r = c.execute(
            "SELECT * FROM rooms WHERE id=?",
            (cur.lastrowid,)
        ).fetchone()

    return room_to_dict(r)


@app.put("/rooms/{room_id}")
def update_room(
    room_id: int,
    data: RoomUpdate,
    student=Depends(require_room_manager)
):
    updates = data.model_dump(exclude_unset=True)

    if not updates:
        raise HTTPException(
            400,
            "Provide at least one field to update."
        )

    with get_connection() as c:
        r = c.execute(
            "SELECT * FROM rooms WHERE id=?",
            (room_id,)
        ).fetchone()

        if r is None:
            raise HTTPException(404, "Room not found.")

        if (
            student["role"] != "admin"
            and (
                r["owner_id"] != student["id"]
                or r["ownership_status"] != "assigned"
            )
        ):
            raise HTTPException(
                403,
                "You can only edit verified rooms assigned to your Owner account."
            )

        values = {
            k: (
                int(v)
                if k == "available" and v is not None
                else v
            )
            for k, v in updates.items()
        }

        c.execute(
            "UPDATE rooms SET "
            + ", ".join(f"{k}=?" for k in values)
            + " WHERE id=?",
            list(values.values()) + [room_id]
        )

        updated = c.execute(
            "SELECT * FROM rooms WHERE id=?",
            (room_id,)
        ).fetchone()

    return room_to_dict(updated)


@app.put("/admin/rooms/{room_id}/proximity")
def set_room_proximity(
    room_id: int,
    data: RoomProximity,
    admin=Depends(require_admin)
):
    with get_connection() as c:
        if c.execute(
            "SELECT 1 FROM rooms WHERE id=?",
            (room_id,)
        ).fetchone() is None:
            raise HTTPException(404, "Room not found.")

        c.execute("""
            UPDATE rooms
            SET nearest_campus=?, proximity_tier=?
            WHERE id=?
        """, (data.nearest_campus, data.proximity_tier, room_id))

        c.execute("""
            INSERT INTO admin_audit_logs(
                admin_id, action, target_type, target_id, details
            )
            VALUES(?,?,?,?,?)
        """, (
            admin["id"],
            "room_proximity_set",
            "room",
            room_id,
            f"{data.nearest_campus}/{data.proximity_tier}"
        ))

    return {
        "room_id": room_id,
        "nearest_campus": data.nearest_campus,
        "proximity_tier": data.proximity_tier,
        "message": "Room proximity updated."
    }


@app.delete("/rooms/{room_id}")
def delete_room(
    room_id: int,
    student=Depends(require_room_manager)
):
    with get_connection() as c:
        r = c.execute(
            "SELECT id, owner_id, ownership_status FROM rooms WHERE id=?",
            (room_id,)
        ).fetchone()

        if r is None:
            raise HTTPException(404, "Room not found.")

        if (
            student["role"] != "admin"
            and (
                r["owner_id"] != student["id"]
                or r["ownership_status"] != "assigned"
            )
        ):
            raise HTTPException(
                403,
                "You can only delete verified rooms assigned to your Owner account."
            )

        c.execute(
            "DELETE FROM rooms WHERE id=?",
            (room_id,)
        )

    return {
        "message": "Room deleted successfully.",
        "room_id": room_id
    }


# AGENT-TO-ROOM PERMISSION WORKFLOW

@app.post("/agents/me/room-requests", status_code=201)
def request_room_advertising(
    data: AgentRoomRequest,
    agent=Depends(require_agent)
):
    with get_connection() as c:
        room = c.execute("""
            SELECT id, available, ownership_status, owner_id
            FROM rooms
            WHERE id=?
        """, (data.room_id,)).fetchone()

        if room is None:
            raise HTTPException(404, "Room not found.")

        if (
            room["ownership_status"] != "assigned"
            or room["owner_id"] is None
        ):
            raise HTTPException(
                409,
                "This room has no confirmed owner yet. "
                "RumCHUO must verify ownership before an Agent can request permission."
            )

        if room["owner_id"] == agent["id"]:
            raise HTTPException(
                400,
                "You cannot request advertising permission for your own room."
            )

        existing = c.execute("""
            SELECT id, status
            FROM agent_room_authorizations
            WHERE room_id=? AND agent_id=?
        """, (data.room_id, agent["id"])).fetchone()

        if existing and existing["status"] in {"pending", "approved"}:
            raise HTTPException(
                409,
                f"A room permission request already exists with status '{existing['status']}'."
            )

        if existing:
            c.execute("""
                UPDATE agent_room_authorizations
                SET status='pending',
                    advertising_enabled=1,
                    requested_at=CURRENT_TIMESTAMP,
                    reviewed_at=NULL,
                    reviewed_by=NULL,
                    review_note=?
                WHERE id=?
            """, (data.note.strip(), existing["id"]))
        else:
            c.execute("""
                INSERT INTO agent_room_authorizations(
                    room_id, agent_id, review_note
                )
                VALUES(?,?,?)
            """, (
                data.room_id,
                agent["id"],
                data.note.strip()
            ))

    return {
        "message": "Request sent to the room owner.",
        "room_id": data.room_id,
        "status": "pending"
    }


@app.get("/agents/me/rooms")
def my_agent_rooms(agent=Depends(require_agent)):
    with get_connection() as c:
        rows = c.execute("""
            SELECT
                r.id AS room_id,
                r.location,
                r.price,
                r.available,
                r.room_type,
                r.created_at,
                a.status AS authorization_status,
                a.advertising_enabled
            FROM agent_room_authorizations a
            JOIN rooms r ON r.id=a.room_id
            WHERE a.agent_id=?
            ORDER BY a.requested_at DESC
        """, (agent["id"],)).fetchall()

    return {
        "rooms": [dict(r) for r in rows]
    }


@app.post("/agents/me/rooms/{room_id}/remove")
def remove_room_from_agent_page(
    room_id: int,
    agent=Depends(require_agent)
):
    with get_connection() as c:
        cur = c.execute("""
            UPDATE agent_room_authorizations
            SET advertising_enabled=0
            WHERE room_id=?
            AND agent_id=?
            AND status='approved'
        """, (room_id, agent["id"]))

        if cur.rowcount == 0:
            raise HTTPException(
                404,
                "No approved advertising permission was found for this room."
            )

    return {
        "message": "Room removed from your Agent page. The original room record is unchanged."
    }


@app.post("/agents/me/rooms/{room_id}/rental-report", status_code=201)
def report_room_rented(
    room_id: int,
    data: RentalReport,
    agent=Depends(require_agent)
):
    with get_connection() as c:
        auth = c.execute("""
            SELECT id
            FROM agent_room_authorizations
            WHERE room_id=?
            AND agent_id=?
            AND status='approved'
        """, (room_id, agent["id"])).fetchone()

        if auth is None:
            raise HTTPException(
                403,
                "You need approved advertising permission to report this room."
            )

        c.execute("""
            INSERT INTO room_rental_reports(room_id, agent_id, note)
            VALUES(?,?,?)
        """, (
            room_id,
            agent["id"],
            data.note.strip()
        ))

    return {
        "message": "Rental report sent to the Owner for confirmation.",
        "status": "pending"
    }


# OWNER ROUTES

@app.get("/landlord/me/rooms")
def landlord_rooms(owner=Depends(require_landlord)):
    with get_connection() as c:
        rows = c.execute("""
            SELECT *
            FROM rooms
            WHERE owner_id=?
            ORDER BY id DESC
        """, (owner["id"],)).fetchall()

    return {
        "rooms": [room_to_dict(r) for r in rows]
    }


@app.get("/landlord/me/authorization-requests")
def landlord_authorization_requests(owner=Depends(require_landlord)):
    with get_connection() as c:
        rows = c.execute("""
            SELECT
                a.id,
                a.room_id,
                a.agent_id,
                a.status,
                a.requested_at,
                a.review_note,
                r.location,
                r.price,
                s.full_name AS agent_name,
                s.email AS agent_email
            FROM agent_room_authorizations a
            JOIN rooms r ON r.id=a.room_id
            JOIN students s ON s.id=a.agent_id
            WHERE r.owner_id=?
            ORDER BY
                CASE WHEN a.status='pending' THEN 0 ELSE 1 END,
                a.requested_at DESC
        """, (owner["id"],)).fetchall()

    return {
        "requests": [dict(r) for r in rows]
    }


@app.post("/landlord/me/authorization-requests/{authorization_id}/decision")
def decide_authorization(
    authorization_id: int,
    data: AuthorizationDecision,
    owner=Depends(require_landlord)
):
    with get_connection() as c:
        a = c.execute("""
            SELECT a.*, r.owner_id
            FROM agent_room_authorizations a
            JOIN rooms r ON r.id=a.room_id
            WHERE a.id=?
        """, (authorization_id,)).fetchone()

        if a is None:
            raise HTTPException(
                404,
                "Authorization request not found."
            )

        if a["owner_id"] != owner["id"]:
            raise HTTPException(
                403,
                "You can only decide requests for rooms you own."
            )

        if (
            data.decision in {"approve", "reject"}
            and a["status"] != "pending"
        ):
            raise HTTPException(
                409,
                "Only pending requests can be approved or rejected."
            )

        if (
            data.decision == "revoke"
            and a["status"] != "approved"
        ):
            raise HTTPException(
                409,
                "Only approved permissions can be revoked."
            )

        status = {
            "approve": "approved",
            "reject": "rejected",
            "revoke": "revoked"
        }[data.decision]

        c.execute("""
            UPDATE agent_room_authorizations
            SET status=?,
                advertising_enabled=?,
                reviewed_at=CURRENT_TIMESTAMP,
                reviewed_by=?,
                review_note=?
            WHERE id=?
        """, (
            status,
            1 if status == "approved" else 0,
            owner["id"],
            data.note.strip(),
            authorization_id
        ))

    return {
        "authorization_id": authorization_id,
        "status": status,
        "message": "Authorization updated."
    }


@app.get("/landlord/me/rental-reports")
def landlord_rental_reports(owner=Depends(require_landlord)):
    with get_connection() as c:
        rows = c.execute("""
            SELECT
                rr.id,
                rr.room_id,
                rr.agent_id,
                rr.note,
                rr.status,
                rr.created_at,
                r.location,
                s.full_name AS agent_name
            FROM room_rental_reports rr
            JOIN rooms r ON r.id=rr.room_id
            JOIN students s ON s.id=rr.agent_id
            WHERE r.owner_id=?
            ORDER BY rr.id DESC
        """, (owner["id"],)).fetchall()

    return {
        "reports": [dict(r) for r in rows]
    }


@app.post("/landlord/me/rental-reports/{report_id}/confirm")
def confirm_rented(
    report_id: int,
    owner=Depends(require_landlord)
):
    with get_connection() as c:
        report = c.execute("""
            SELECT rr.*, r.owner_id
            FROM room_rental_reports rr
            JOIN rooms r ON r.id=rr.room_id
            WHERE rr.id=?
        """, (report_id,)).fetchone()

        if report is None:
            raise HTTPException(
                404,
                "Rental report not found."
            )

        if report["owner_id"] != owner["id"]:
            raise HTTPException(
                403,
                "You can only confirm rental reports for your rooms."
            )

        if report["status"] != "pending":
            raise HTTPException(
                409,
                "This rental report has already been reviewed."
            )

        c.execute(
            "UPDATE rooms SET available=0 WHERE id=?",
            (report["room_id"],)
        )

        c.execute("""
            UPDATE room_rental_reports
            SET status='confirmed',
                reviewed_at=CURRENT_TIMESTAMP,
                reviewed_by=?
            WHERE id=?
        """, (owner["id"], report_id))

        c.execute("""
            UPDATE agent_room_authorizations
            SET advertising_enabled=0
            WHERE room_id=?
        """, (report["room_id"],))

    return {
        "message": "Rental confirmed. The room is now marked unavailable and hidden from Agent pages."
    }


# ROOMMATE MATCHING

def normalize_location_words(value):
    if not value:
        return set()

    ignored = {"near", "area", "the", "at", "in", "of"}

    return {
        w
        for w in re.findall(r"[a-z0-9]+", value.lower())
        if w not in ignored
    }


def normalize_match_text(value):
    """Normalize text for case-insensitive matching."""
    if not value:
        return ""

    return " ".join(value.strip().casefold().split())


@app.get("/students/matches")
def find_roommate_matches(
    same_year: bool = False,
    student=Depends(get_current_student)
):
    # Both students must be looking for shared accommodation.
    if student["room_type"] != "shared":
        raise HTTPException(
            400,
            "Set your room_type preference to 'shared' to find roommate matches."
        )

    # University is a mandatory matching requirement.
    if not student["university"] or not student["university"].strip():
        raise HTTPException(
            400,
            "Add your university to your profile before searching for roommates."
        )

    # Gender is a mandatory matching requirement.
    if not student["gender"]:
        raise HTTPException(
            400,
            "Set your gender in your profile before searching for roommates."
        )

    # Same-year filtering is optional.
    if same_year and student["year_of_study"] is None:
        raise HTTPException(
            400,
            "Add your year of study to your profile before using the same-year filter."
        )

    university = normalize_match_text(student["university"])
    gender = normalize_match_text(student["gender"])

    target_location = normalize_location_words(
        student["preferred_location"]
    )

    target_faculty = normalize_match_text(student["faculty"])
    target_course = normalize_match_text(student["course"])
    target_year = student["year_of_study"]

    with get_connection() as c:
        candidates = c.execute("""
            SELECT *
            FROM students
            WHERE id != ?
            AND email_verified = 1
            AND account_status = 'active'
            AND role = 'student'
            AND room_type = 'shared'
            AND LOWER(TRIM(university)) = LOWER(TRIM(?))
            AND LOWER(TRIM(gender)) = LOWER(TRIM(?))
        """, (
            student["id"],
            student["university"].strip(),
            student["gender"].strip()
        )).fetchall()

    matches = []

    for candidate in candidates:

        # Apply the optional same-year filter only when requested.
        if same_year and candidate["year_of_study"] != target_year:
            continue

        score = 1
        matching_reasons = []

        # Location increases the score but does not exclude candidates.
        candidate_location = normalize_location_words(
            candidate["preferred_location"]
        )

        if (
            target_location
            and candidate_location
            and target_location.intersection(candidate_location)
        ):
            score += 3
            matching_reasons.append("Similar preferred location")

        # Same faculty increases the score.
        candidate_faculty = normalize_match_text(
            candidate["faculty"]
        )

        if (
            target_faculty
            and candidate_faculty
            and target_faculty == candidate_faculty
        ):
            score += 2
            matching_reasons.append("Same faculty")

        # Same course increases the score.
        candidate_course = normalize_match_text(
            candidate["course"]
        )

        if (
            target_course
            and candidate_course
            and target_course == candidate_course
        ):
            score += 2
            matching_reasons.append("Same course")

        # Same year increases the score, whether or not the filter is active.
        if (
            target_year is not None
            and candidate["year_of_study"] is not None
            and candidate["year_of_study"] == target_year
        ):
            score += 1
            matching_reasons.append("Same year of study")

        matches.append({
            "student_id": candidate["id"],
            "full_name": candidate["full_name"],
            "university": candidate["university"],
            "faculty": candidate["faculty"],
            "course": candidate["course"],
            "year_of_study": candidate["year_of_study"],
            "preferred_location": candidate["preferred_location"],
            "match_score": score,
            "matching_reasons": matching_reasons
        })

    # Highest score first; names alphabetically when scores are equal.
    matches.sort(
        key=lambda x: (
            -x["match_score"],
            x["full_name"].casefold()
        )
    )

    return {
        "count": len(matches),
        "matches": matches,
        "matching_rules": {
            "mandatory": [
                "Same university",
                "Same gender",
                "Both students must prefer shared accommodation"
            ],
            "score_factors": {
                "similar_location": 3,
                "same_faculty": 2,
                "same_course": 2,
                "same_year": 1
            },
            "same_year_filter_enabled": same_year
        },
        "note": (
            "University and gender are mandatory matching requirements. "
            "Location, faculty, course and year increase the match score "
            "but do not exclude otherwise eligible students. "
            "Location matching compares words, not geographic distance. "
            "Scores are indicators, not guarantees of compatibility."
        )
    }


# ADMIN ROUTES

from admin_routes import register_admin_routes

register_admin_routes(
    app,
    get_connection,
    get_current_student
)

from agent_property_routes import register_agent_property_routes

register_agent_property_routes(
    app,
    get_connection,
    get_current_student
)


# AGENT DASHBOARD

@app.get("/agent/dashboard")
def agent_dashboard():
    file_path = Path(__file__).resolve().parent / "agent_dashboard.html"

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Agent dashboard file not found"
        )

    return FileResponse(file_path)


# AGENT LOGIN PAGE

@app.get("/agent/login")
def agent_login():
    file_path = Path(__file__).resolve().parent / "agent_login.html"

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Agent login page file not found. Create agent_login.html first."
        )

    return FileResponse(
        file_path,
        media_type="text/html"
    )