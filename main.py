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
from pydantic import BaseModel, Field, field_validator, model_validator
from pwdlib import PasswordHash

load_dotenv()

APP_NAME, APP_VERSION, DATABASE_NAME = "RumCHUO", "1.8.0", "rumchuo.db"
SECRET_KEY = os.getenv("RUMCHUO_SECRET_KEY", "")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES, VERIFICATION_TOKEN_MINUTES = 30, 30
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
            "ownership_status": "TEXT NOT NULL DEFAULT 'unassigned'"
        }

        student_additions = {
            "university": "TEXT",
            "faculty": "TEXT",
            "course": "TEXT",
            "year_of_study": "INTEGER",
            "gender": "TEXT",
            "budget_min": "REAL",
            "budget_max": "REAL",
            "preferred_location": "TEXT",
            "room_type": "TEXT",
            "email_verified": "INTEGER NOT NULL DEFAULT 0",
            "email_verification_token_hash": "TEXT",
            "email_verification_expires_at": "TEXT",
            "role": "TEXT NOT NULL DEFAULT 'student'",
            "account_status": "TEXT NOT NULL DEFAULT 'active'"
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

        # Preserve the original sample-room behavior only for a genuinely empty database.
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
        "course": s["course"],
        "year_of_study": s["year_of_study"],
        "gender": s["gender"],
        "budget_min": s["budget_min"],
        "budget_max": s["budget_max"],
        "preferred_location": s["preferred_location"],
        "room_type": s["room_type"],
        "created_at": s["created_at"]
    }


def room_to_dict(room):
    return {
        "id": room["id"],
        "location": room["location"],
        "price": room["price"],
        "available": bool(room["available"]),
        "room_type": room["room_type"] or "single",
        "created_at": room["created_at"],
        "ownership_status": (
            room["ownership_status"]
            if "ownership_status" in room.keys()
            else "unassigned"
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


class StudentProfileUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=100)
    university: str | None = Field(default=None, max_length=150)
    course: str | None = Field(default=None, max_length=150)
    year_of_study: int | None = Field(default=None, ge=1, le=10)
    gender: Literal["male", "female", "other"] | None = None

    @field_validator("full_name", "university", "course", mode="before")
    @classmethod
    def clean_optional_text(cls, v):
        return v.strip() or None if isinstance(v, str) else v


class StudentPreferencesUpdate(BaseModel):
    budget_min: float | None = Field(default=None, ge=0)
    budget_max: float | None = Field(default=None, ge=0)
    preferred_location: str | None = Field(default=None, max_length=150)
    room_type: Literal["single", "shared", "any"] | None = None

    @field_validator("preferred_location")
    @classmethod
    def clean_location(cls, v):
        return v.strip() or None if isinstance(v, str) else v

    @model_validator(mode="after")
    def check_budget_range(self):
        if (
            self.budget_min is not None
            and self.budget_max is not None
            and self.budget_min > self.budget_max
        ):
            raise ValueError("Minimum budget cannot exceed maximum budget.")
        return self


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
            "Only a landlord or administrator can manage original room records."
        )
    return student


def require_admin(student=Depends(get_current_student)):
    if student["role"] != "admin":
        raise HTTPException(
            403,
            "Only an administrator can create original room records. "
            "Landlords must submit properties for verification."
        )
    return student


def require_landlord(student=Depends(get_current_student)):
    if student["role"] != "landlord":
        raise HTTPException(
            403,
            "Only a landlord can perform this action."
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
        if k in {
            "full_name",
            "university",
            "course",
            "year_of_study",
            "gender"
        }
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
    return {
        k: student[k]
        for k in (
            "budget_min",
            "budget_max",
            "preferred_location",
            "room_type"
        )
    }


@app.put("/students/me/preferences")
def update_my_preferences(
    data: StudentPreferencesUpdate,
    student=Depends(get_current_student)
):
    updates = data.model_dump(exclude_unset=True)

    low = updates.get("budget_min", student["budget_min"])
    high = updates.get("budget_max", student["budget_max"])

    if low is not None and high is not None and low > high:
        raise HTTPException(
            400,
            "Minimum budget cannot exceed maximum budget."
        )

    updates = {
        k: v
        for k, v in updates.items()
        if k in {
            "budget_min",
            "budget_max",
            "preferred_location",
            "room_type"
        }
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

    return {
        k: s[k]
        for k in (
            "budget_min",
            "budget_max",
            "preferred_location",
            "room_type"
        )
    }


# PUBLIC ROOM RESPONSES DELIBERATELY OMIT LANDLORD IDENTITY AND CONTACT DETAILS.

def add_public_agents(c, room_id):
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


@app.get("/rooms")
def list_rooms(
    location: str | None = None,
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

    if max_price is not None:
        conditions.append("price<=?")
        params.append(max_price)

    if room_type:
        conditions.append("room_type=?")
        params.append(room_type)

    if available_only:
        conditions.append("available=1")

    # Only administrator-verified rooms are publicly listed.
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
            item["agents"] = add_public_agents(c, r["id"])
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

        # Do not expose pending or unverified properties publicly.
        if r["ownership_status"] != "assigned" or r["owner_id"] is None:
            raise HTTPException(404, "Verified room not found.")

        result = room_to_dict(r)
        result["agents"] = add_public_agents(c, room_id)

    return result


# IMPORTANT SECURITY CHANGE:
# Only administrators can create original room records here.
# Landlords must use the photo-based property verification workflow.
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
                "You can only edit verified rooms assigned to your landlord account."
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
                "You can only delete verified rooms assigned to your landlord account."
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
        "message": "Rental report sent to the landlord for confirmation.",
        "status": "pending"
    }


# LANDLORD ROUTES

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


# ROOMMATE MATCHING RETAINED

def normalize_location_words(value):
    if not value:
        return set()

    ignored = {"near", "area", "the", "at", "in", "of"}

    return {
        w
        for w in re.findall(r"[a-z0-9]+", value.lower())
        if w not in ignored
    }


@app.get("/students/matches")
def find_roommate_matches(
    same_year: bool = False,
    student=Depends(get_current_student)
):
    if student["room_type"] != "shared":
        raise HTTPException(
            400,
            "Set your room_type preference to 'shared' to find roommate matches."
        )

    if not student["preferred_location"]:
        raise HTTPException(
            400,
            "Set your preferred location before searching for roommates."
        )

    target = normalize_location_words(
        student["preferred_location"]
    )

    with get_connection() as c:
        candidates = c.execute("""
            SELECT *
            FROM students
            WHERE id!=?
            AND email_verified=1
            AND account_status='active'
            AND role='student'
            AND room_type='shared'
            AND gender IS ?
        """, (
            student["id"],
            student["gender"]
        )).fetchall()

    matches = []

    for candidate in candidates:
        if (
            same_year
            and candidate["year_of_study"] != student["year_of_study"]
        ):
            continue

        loc = normalize_location_words(
            candidate["preferred_location"]
        )

        if not target or not loc or not target.intersection(loc):
            continue

        score = 1

        if (
            student["course"]
            and candidate["course"]
            and student["course"].strip().lower()
            == candidate["course"].strip().lower()
        ):
            score += 2

        if (
            student["year_of_study"] is not None
            and candidate["year_of_study"] == student["year_of_study"]
        ):
            score += 1

        matches.append({
            "student_id": candidate["id"],
            "full_name": candidate["full_name"],
            "university": candidate["university"],
            "course": candidate["course"],
            "year_of_study": candidate["year_of_study"],
            "preferred_location": candidate["preferred_location"],
            "match_score": score
        })

    matches.sort(
        key=lambda x: (
            x["match_score"],
            x["full_name"].lower()
        ),
        reverse=True
    )

    return {
        "count": len(matches),
        "matches": matches,
        "note": "Location matching uses shared words in location names, not geographic distance. Match scores are indicators, not guarantees of compatibility."
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

    return FileResponse(file_path, media_type="text/html")

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