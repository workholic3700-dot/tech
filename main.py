import os, re, sqlite3, hashlib, secrets, smtplib, ssl, json
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

APP_NAME, APP_VERSION, DATABASE_NAME = "RumCHUO", "1.13.0", "rumchuo.db"
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
        raise HTTPException(404, "Student dashboard file was not found.")
    return FileResponse(dashboard_file)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000", "http://localhost:3000",
        "http://127.0.0.1:3001", "http://localhost:3001",
        "http://127.0.0.1:5500", "http://localhost:5500",
        "http://127.0.0.1:5501", "http://localhost:5501"
    ],
    allow_origin_regex=r"^http://(?:localhost|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}):(3000|3001|5500|5501)$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"]
)

security = HTTPBearer(auto_error=False)
password_hash = PasswordHash.recommended()

PROFILE_FIELDS = {
    "full_name", "university", "campus", "faculty", "course",
    "year_of_study", "gender", "phone", "accommodation_status"
}

BASE_DIR = Path(__file__).resolve().parent
PRIVATE_PHOTO_DIR = BASE_DIR / "private_ownership_photos"
AGENT_PHOTO_DIR = BASE_DIR / "private_agent_property_photos"

PREFERENCE_FIELDS = (
    "preferred_location",
    "preferred_campus",
    "room_type",
    "contact_visibility"
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
            "contact_visibility": "INTEGER NOT NULL DEFAULT 1",
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

        # ACTIVE AGENT DISCOVERY SETTINGS
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_discovery_profiles (
                agent_id INTEGER PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
                is_available INTEGER NOT NULL DEFAULT 0,
                service_areas TEXT NOT NULL DEFAULT '[]',
                universities_covered TEXT NOT NULL DEFAULT '[]',
                share_exact_location INTEGER NOT NULL DEFAULT 0,
                latitude REAL,
                longitude REAL,
                subscription_tier TEXT NOT NULL DEFAULT 'free',
                subscription_status TEXT NOT NULL DEFAULT 'inactive',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_discovery_available
            ON agent_discovery_profiles(is_available)
        """)

        # STUDENT-TO-AGENT ENQUIRIES
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_enquiries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL REFERENCES students(id),
                agent_id INTEGER NOT NULL REFERENCES students(id),
                room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new'
                    CHECK(status IN ('new','read','replied','closed')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ENQUIRY REPLY MIGRATION
        # Adds the student's contact details (saved with each enquiry)
        # and the Agent's reply to the existing agent_enquiries table.
        enquiry_additions = {
            "contact_phone": "TEXT",
            "contact_email": "TEXT",
            "reply_message": "TEXT",
            "replied_at": "TEXT",
            "replied_by": "INTEGER REFERENCES students(id)"
        }

        existing_enquiry_columns = get_columns(c, "agent_enquiries")
        for col, definition in enquiry_additions.items():
            if col not in existing_enquiry_columns:
                c.execute(
                    f"ALTER TABLE agent_enquiries ADD COLUMN {col} {definition}"
                )

        # Backfill contact details for enquiries created before this upgrade.
        c.execute("""
            UPDATE agent_enquiries
            SET contact_phone=(
                    SELECT s.phone FROM students s
                    WHERE s.id=agent_enquiries.student_id
                ),
                contact_email=(
                    SELECT s.email FROM students s
                    WHERE s.id=agent_enquiries.student_id
                )
            WHERE contact_email IS NULL
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_enquiries_agent
            ON agent_enquiries(agent_id, status, created_at)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_enquiries_student
            ON agent_enquiries(student_id, created_at)
        """)



        # STUDENT ROOMMATE REQUESTS
        c.execute("""
            CREATE TABLE IF NOT EXISTS roommate_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL
                    REFERENCES students(id) ON DELETE CASCADE,
                recipient_id INTEGER NOT NULL
                    REFERENCES students(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'accepted', 'rejected')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK(sender_id != recipient_id)
            )
        """)

        # Prevent duplicate pending requests in either direction.
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_roommate_requests_pending_pair
            ON roommate_requests (
                MIN(sender_id, recipient_id),
                MAX(sender_id, recipient_id)
            )
            WHERE status = 'pending'
        """)

        # Prevent duplicate accepted roommate connections.
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_roommate_requests_accepted_pair
            ON roommate_requests (
                MIN(sender_id, recipient_id),
                MAX(sender_id, recipient_id)
            )
            WHERE status = 'accepted'
        """)

        # Index for received requests.
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_roommate_requests_recipient
            ON roommate_requests(recipient_id, status, created_at)
        """)

        # Index for sent requests.
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_roommate_requests_sender
            ON roommate_requests(sender_id, status, created_at)
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
        "contact_visibility": s["contact_visibility"],
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
            if "ownership_status" in keys else "unassigned"
        ),
        "nearest_campus": (
            room["nearest_campus"] if "nearest_campus" in keys else None
        ),
        "proximity_tier": (
            room["proximity_tier"] if "proximity_tier" in keys else None
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
        raise RuntimeError("SMTP_EMAIL or SMTP_APP_PASSWORD is not configured.")

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
        "If you did not create this account, ignore this email.\n\nRumCHUO"
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(SMTP_EMAIL, SMTP_APP_PASSWORD)
        server.send_message(msg)


def send_password_reset_email(email, full_name, token):
    if not SMTP_EMAIL or not SMTP_APP_PASSWORD:
        raise RuntimeError("SMTP_EMAIL or SMTP_APP_PASSWORD is not configured.")

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
        "If you did not request this, ignore this email. Your password will not change.\n\nRumCHUO"
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
    phone: str = Field(min_length=5, max_length=25)
    password: str = Field(min_length=8, max_length=128)
    joining_as: Literal["student", "agent", "owner"] = "student"
    owner_declaration_confirmed: bool = False

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

    @field_validator("phone")
    @classmethod
    def clean_phone(cls, v):
        v = v.strip()
        if not re.fullmatch(r"[+0-9()\-\s]{5,25}", v):
            raise ValueError("Enter a valid phone number.")
        if not 5 <= len(re.sub(r"\D", "", v)) <= 20:
            raise ValueError("Enter a valid phone number.")
        return v

    @model_validator(mode="after")
    def require_owner_declaration(self):
        if self.joining_as == "owner" and not self.owner_declaration_confirmed:
            raise ValueError(
                "Confirm that you are the property owner before creating an Owner account."
            )
        return self


class StudentLogin(BaseModel):
    email: str
    password: str
    login_as: Literal["student", "agent", "owner", "admin"] | None = None

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
        "looking_for_room", "has_room_needs_roommate", "not_looking"
    ] | None = None

    @field_validator(
        "full_name", "university", "campus", "faculty", "course", "phone",
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
    contact_visibility: bool | None = None

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


class RoommateRequestDecision(BaseModel):
    decision: Literal["accept", "reject"]


class RentalReport(BaseModel):
    note: str = Field(default="", max_length=500)


class OwnerAssignment(BaseModel):
    owner_id: int = Field(gt=0)
    confirmation_note: str = Field(min_length=5, max_length=500)


# ACTIVE AGENT MODELS

class AgentDiscoveryUpdate(BaseModel):
    is_available: bool = False
    service_areas: list[str] = Field(default_factory=list, max_length=20)
    universities_covered: list[str] = Field(default_factory=list, max_length=30)
    share_exact_location: bool = False
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @field_validator("service_areas", "universities_covered")
    @classmethod
    def clean_lists(cls, values):
        cleaned = []
        for value in values:
            value = str(value).strip()
            if value and value not in cleaned:
                cleaned.append(value[:150])
        return cleaned

    @model_validator(mode="after")
    def validate_location_sharing(self):
        if self.is_available and not self.service_areas:
            raise ValueError(
                "Add at least one service area before making yourself available."
            )

        if self.share_exact_location:
            if self.latitude is None or self.longitude is None:
                raise ValueError(
                    "Latitude and longitude are required when exact location sharing is enabled."
                )
        else:
            self.latitude = None
            self.longitude = None

        return self


class AgentEnquiryCreate(BaseModel):
    agent_id: int = Field(gt=0)
    room_id: int | None = Field(default=None, gt=0)
    message: str = Field(min_length=5, max_length=2000)

    @field_validator("message")
    @classmethod
    def clean_message(cls, value):
        value = value.strip()
        if len(value) < 5:
            raise ValueError("Please enter a longer enquiry message.")
        return value


class AgentEnquiryStatusUpdate(BaseModel):
    # "replied" is set automatically when the Agent sends a reply,
    # so it can no longer be chosen manually.
    status: Literal["read", "closed"]


class AgentEnquiryReply(BaseModel):
    reply: str = Field(min_length=2, max_length=2000)

    @field_validator("reply")
    @classmethod
    def clean_reply(cls, value):
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Please enter a reply message.")
        return value


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
    if student["role"] not in {"landlord", "admin", "owner"}:
        raise HTTPException(
            403,
            "Only an Owner or administrator can manage original room records."
        )
    return student


def require_admin(student=Depends(get_current_student)):
    if student["role"] != "admin":
        raise HTTPException(
            403,
            "Only an administrator can create original room records. Owners must submit properties for verification."
        )
    return student


def require_landlord(student=Depends(get_current_student)):
    if student["role"] not in {"landlord", "owner"}:
        raise HTTPException(403, "Only an Owner can perform this action.")
    return student


def require_agent(student=Depends(get_current_student)):
    if student["role"] != "agent":
        raise HTTPException(403, "Only an approved Agent can perform this action.")

    if student["account_status"] != "active":
        raise HTTPException(403, "Your Agent account is not currently active.")

    return student


# GENERAL ROUTES

@app.get("/")
def home():
    file_path = Path(__file__).resolve().parent / "index.html"
    if not file_path.exists():
        raise HTTPException(404, "RumCHUO homepage (index.html) was not found.")
    return FileResponse(file_path)


@app.get("/login", include_in_schema=False)
@app.get("/login.html", include_in_schema=False)
def login_page():
    file_path = Path(__file__).resolve().parent / "login.html"
    if not file_path.exists():
        raise HTTPException(404, "RumCHUO login page (login.html) was not found.")
    return FileResponse(file_path)


@app.get("/health")
def health():
    return {"status": "ok", "app": APP_NAME, "version": APP_VERSION}


# STUDENT REGISTRATION AND AUTHENTICATION

@app.post("/students/register", status_code=201)
def register_student(data: StudentRegister):
    email = normalize_email(data.email)
    hashed = password_hash.hash(data.password)

    try:
        with get_connection() as c:
            cur = c.execute("""
                INSERT INTO students(
                    full_name, email, phone, password_hash, email_verified
                )
                VALUES(?,?,?,?,0)
            """, (data.full_name, email, data.phone, hashed))
            sid = cur.lastrowid

            if data.joining_as == "agent":
                c.execute("""
                    INSERT INTO agent_applications(student_id, application_note)
                    VALUES(?,?)
                """, (
                    sid,
                    "Agent access requested during homepage registration."
                ))

            elif data.joining_as == "owner":
                c.execute("""
                    INSERT INTO landlord_applications(
                        student_id, relationship_to_property,
                        application_note, declaration_confirmed
                    )
                    VALUES(?,?,?,1)
                """, (
                    sid,
                    "owner",
                    "Owner access requested during homepage registration. The applicant confirmed property ownership."
                ))

    except sqlite3.IntegrityError:
        raise HTTPException(409, "An account or application with this email already exists.")

    try:
        issue_verification_email(sid, email, data.full_name)
    except Exception:
        raise HTTPException(
            503,
            "Your account was created but the verification email could not be sent. Check server configuration, then request another email."
        )

    response_message = "Registration successful. Check your email and verify before logging in."
    if data.joining_as in {"agent", "owner"}:
        response_message += (
            " Your " + data.joining_as.title()
            + " application has been submitted and requires administrator approval."
        )

    return {
        "message": response_message,
        "email": email,
        "email_verified": False,
        "role": "student",
        "joining_as": data.joining_as,
        "application_status": (
            "pending" if data.joining_as in {"agent", "owner"} else None
        )
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
        raise HTTPException(503, "The verification email could not be sent. Please try again later.")

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
        raise HTTPException(400, "This verification link is invalid or has already been used.")

    if s["email_verified"]:
        return {"message": "Your email address is already verified."}

    try:
        expiry = datetime.fromisoformat(s["email_verification_expires_at"])
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
        raise HTTPException(400, "This verification link has expired. Request a new link.")

    with get_connection() as c:
        c.execute("""
            UPDATE students
            SET email_verified=1,
                email_verification_token_hash=NULL,
                email_verification_expires_at=NULL
            WHERE id=? AND email_verification_token_hash=?
        """, (s["id"], token_hash))

    return {"message": "Email verified successfully. You can now log in.", "email_verified": True}


@app.post("/students/login")
def login_student(data: StudentLogin):
    with get_connection() as c:
        s = c.execute(
            "SELECT * FROM students WHERE email=?",
            (normalize_email(data.email),)
        ).fetchone()

    if s is None or not password_hash.verify(data.password, s["password_hash"]):
        raise HTTPException(401, "Invalid email or password.")

    if s["account_status"] == "suspended":
        raise HTTPException(403, "This account is suspended. Contact RumCHUO support.")

    if not s["email_verified"]:
        raise HTTPException(403, "Please verify your email before logging in.")

    selected_role = data.login_as
    actual_role = s["role"]

    if selected_role == "agent" and actual_role != "agent":
        with get_connection() as c:
            application = c.execute("""
                SELECT status, review_reason
                FROM agent_applications
                WHERE student_id=?
            """, (s["id"],)).fetchone()

        if application and application["status"] == "pending":
            raise HTTPException(403, "Your Agent application is awaiting administrator approval.")
        if application and application["status"] == "rejected":
            reason = application["review_reason"] or "No reason was provided."
            raise HTTPException(403, "Your Agent application was not approved. " + reason)
        raise HTTPException(403, "This account does not have approved Agent access.")

    if selected_role == "owner" and actual_role not in {"landlord", "owner"}:
        with get_connection() as c:
            application = c.execute("""
                SELECT status, review_reason
                FROM landlord_applications
                WHERE student_id=?
            """, (s["id"],)).fetchone()

        if application and application["status"] == "pending":
            raise HTTPException(403, "Your Owner application is awaiting administrator approval.")
        if application and application["status"] == "rejected":
            reason = application["review_reason"] or "No reason was provided."
            raise HTTPException(403, "Your Owner application was not approved. " + reason)
        raise HTTPException(403, "This account does not have approved Owner access.")

    expected_roles = {
        "student": {"student"},
        "agent": {"agent"},
        "owner": {"landlord", "owner"},
        "admin": {"admin"}
    }

    if selected_role and actual_role not in expected_roles[selected_role]:
        raise HTTPException(403, "This account does not have access to the selected sign-in type.")

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
        raise HTTPException(404, "Password reset page (reset_password.html) was not found.")
    return FileResponse(file_path)


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
        expiry = (utc_now() + timedelta(minutes=PASSWORD_RESET_TOKEN_MINUTES)).isoformat()

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
                expiry = datetime.fromisoformat(s["password_reset_expires_at"])
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                expiry = None

        if s is None or expiry is None or expiry <= utc_now():
            raise HTTPException(400, "This password reset link is invalid or has expired.")

        c.execute("""
            UPDATE students
            SET password_hash=?,
                password_reset_token_hash=NULL,
                password_reset_expires_at=NULL
            WHERE id=?
        """, (password_hash.hash(data.new_password), s["id"]))

    return {"message": "Password updated successfully. You can now log in."}


@app.post("/students/me/change-password")
def change_password(data: ChangePassword, student=Depends(get_current_student)):
    if not password_hash.verify(data.current_password, student["password_hash"]):
        raise HTTPException(400, "Current password is incorrect.")
    if data.current_password == data.new_password:
        raise HTTPException(400, "The new password must be different from the current password.")

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
def update_my_profile(data: StudentProfileUpdate, student=Depends(get_current_student)):
    updates = {
        k: v for k, v in data.model_dump(exclude_unset=True).items()
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
        s = c.execute("SELECT * FROM students WHERE id=?", (student["id"],)).fetchone()

    return student_to_dict(s)


@app.get("/students/me/preferences")
def get_my_preferences(student=Depends(get_current_student)):
    return {k: student[k] for k in PREFERENCE_FIELDS}


@app.put("/students/me/preferences")
def update_my_preferences(data: StudentPreferencesUpdate, student=Depends(get_current_student)):
    updates = {
        k: v for k, v in data.model_dump(exclude_unset=True).items()
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
        s = c.execute("SELECT * FROM students WHERE id=?", (student["id"],)).fetchone()

    return {k: s[k] for k in PREFERENCE_FIELDS}


# APPROVED AGENT PROPERTY HELPERS

def get_approved_agent_application(c, room_id):
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
                s.phone AS account_phone,
                s.role AS submitter_role
            FROM agent_property_applications a
            JOIN students s ON s.id=a.agent_id
            WHERE a.room_id=?
            AND a.status='approved'
            AND s.role IN ('agent','landlord','owner')
            AND s.account_status='active'
            AND s.email_verified=1
            ORDER BY a.id DESC
            LIMIT 1
        """, (room_id,)).fetchone()
    except sqlite3.OperationalError:
        return None


def get_agent_property_photos(c, room_id):
    try:
        rows = c.execute("""
            SELECT p.id, p.original_name, p.content_type
            FROM agent_property_photos p
            JOIN agent_property_applications a ON a.id=p.application_id
            WHERE a.room_id=? AND a.status='approved'
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


def add_public_agents(c, room_id):
    direct_application = get_approved_agent_application(c, room_id)

    if direct_application is not None:
        submitter_role = (direct_application["submitter_role"] or "agent").lower()
        return [{
            "agent_id": direct_application["agent_id"],
            "agent_name": direct_application["agent_name"],
            "contact_name": direct_application["agent_name"],
            "contact_role": submitter_role,
            "contact_label": "Owner" if submitter_role in {"owner", "landlord"} else "Agent"
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

    return [{"agent_id": r["id"], "agent_name": r["full_name"]} for r in rows]


def get_room_photos(c, room_id):
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
            JOIN room_ownership_photos p ON p.application_id=a.id
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
        {"id": r["id"], "original_name": r["original_name"], "content_type": r["content_type"]}
        for r in rows
    ]


def get_agents_with_contact(c, room_id):
    direct_application = get_approved_agent_application(c, room_id)

    if direct_application is not None:
        submitter_role = (direct_application["submitter_role"] or "agent").lower()
        return [{
            "agent_id": direct_application["agent_id"],
            "agent_name": direct_application["agent_name"],
            "contact_name": direct_application["agent_name"],
            "contact_role": submitter_role,
            "contact_label": "Owner" if submitter_role in {"owner", "landlord"} else "Agent",
            "phone": direct_application["phone_number"] or direct_application["account_phone"],
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


# ROOM ROUTES

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

    conditions.extend(["ownership_status='assigned'", "owner_id IS NOT NULL"])
    where = "WHERE " + " AND ".join(conditions)

    with get_connection() as c:
        total = c.execute(
            f"SELECT COUNT(*) n FROM rooms {where}", params
        ).fetchone()["n"]

        rows = c.execute(
            f"SELECT * FROM rooms {where} ORDER BY price, id LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()

        rooms = []
        for r in rows:
            item = room_to_dict(r)
            application = get_approved_agent_application(c, r["id"])
            item["agents"] = add_public_agents(c, r["id"])
            item["property_contact"] = get_agents_with_contact(c, r["id"])
            item["description"] = application["description"] if application else None
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
        r = c.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        if r is None or r["ownership_status"] != "assigned" or r["owner_id"] is None:
            raise HTTPException(404, "Verified room not found.")

        result = room_to_dict(r)
        application = get_approved_agent_application(c, room_id)
        result["agents"] = add_public_agents(c, room_id)
        result["property_contact"] = get_agents_with_contact(c, room_id)
        result["description"] = application["description"] if application else None

    return result


@app.get("/rooms/{room_id}/details")
def get_room_details(room_id: int, student=Depends(get_current_student)):
    with get_connection() as c:
        r = c.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()

        if r is None or r["ownership_status"] != "assigned" or r["owner_id"] is None:
            raise HTTPException(404, "Verified room not found.")

        result = room_to_dict(r)
        application = get_approved_agent_application(c, room_id)
        result["description"] = application["description"] if application else None
        result["photos"] = get_room_photos(c, room_id)
        result["agents"] = get_agents_with_contact(c, room_id)
        result["property_contact"] = result["agents"]

    return result


@app.get("/rooms/{room_id}/photos/{photo_id}")
def get_room_photo(room_id: int, photo_id: int):
    with get_connection() as c:
        r = c.execute(
            "SELECT ownership_status, owner_id FROM rooms WHERE id=?",
            (room_id,)
        ).fetchone()

        if r is None or r["ownership_status"] != "assigned" or r["owner_id"] is None:
            raise HTTPException(404, "Verified room not found.")

        direct_application = get_approved_agent_application(c, room_id)

        if direct_application is not None:
            photo = c.execute("""
                SELECT stored_name, content_type
                FROM agent_property_photos
                WHERE id=? AND application_id=?
            """, (photo_id, direct_application["application_id"])).fetchone()
            photo_directory = AGENT_PHOTO_DIR
        else:
            allowed = {p["id"] for p in get_room_photos(c, room_id)}
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
        raise HTTPException(404, "The stored photograph could not be found.")

    return FileResponse(
        path,
        media_type=photo["content_type"],
        headers={
            "Cache-Control": "public, max-age=300",
            "X-Content-Type-Options": "nosniff"
        }
    )


@app.post("/rooms", status_code=201)
def create_room(data: RoomCreate, student=Depends(require_admin)):
    with get_connection() as c:
        cur = c.execute("""
            INSERT INTO rooms(
                location, price, available, room_type, owner_id, ownership_status
            )
            VALUES(?,?,?,?,NULL,'unassigned')
        """, (data.location, data.price, int(data.available), data.room_type))
        r = c.execute("SELECT * FROM rooms WHERE id=?", (cur.lastrowid,)).fetchone()

    return room_to_dict(r)


@app.put("/rooms/{room_id}")
def update_room(room_id: int, data: RoomUpdate, student=Depends(require_room_manager)):
    updates = data.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "Provide at least one field to update.")

    with get_connection() as c:
        r = c.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        if r is None:
            raise HTTPException(404, "Room not found.")

        if student["role"] != "admin" and (
            r["owner_id"] != student["id"] or r["ownership_status"] != "assigned"
        ):
            raise HTTPException(403, "You can only edit verified rooms assigned to your Owner account.")

        values = {
            k: int(v) if k == "available" and v is not None else v
            for k, v in updates.items()
        }

        c.execute(
            "UPDATE rooms SET " + ", ".join(f"{k}=?" for k in values) + " WHERE id=?",
            list(values.values()) + [room_id]
        )
        updated = c.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()

    return room_to_dict(updated)


@app.put("/admin/rooms/{room_id}/proximity")
def set_room_proximity(room_id: int, data: RoomProximity, admin=Depends(require_admin)):
    with get_connection() as c:
        if c.execute("SELECT 1 FROM rooms WHERE id=?", (room_id,)).fetchone() is None:
            raise HTTPException(404, "Room not found.")

        c.execute("""
            UPDATE rooms SET nearest_campus=?, proximity_tier=?
            WHERE id=?
        """, (data.nearest_campus, data.proximity_tier, room_id))

        c.execute("""
            INSERT INTO admin_audit_logs(admin_id, action, target_type, target_id, details)
            VALUES(?,?,?,?,?)
        """, (
            admin["id"], "room_proximity_set", "room", room_id,
            f"{data.nearest_campus}/{data.proximity_tier}"
        ))

    return {
        "room_id": room_id,
        "nearest_campus": data.nearest_campus,
        "proximity_tier": data.proximity_tier,
        "message": "Room proximity updated."
    }


@app.delete("/rooms/{room_id}")
def delete_room(room_id: int, student=Depends(require_room_manager)):
    with get_connection() as c:
        r = c.execute(
            "SELECT id, owner_id, ownership_status FROM rooms WHERE id=?",
            (room_id,)
        ).fetchone()

        if r is None:
            raise HTTPException(404, "Room not found.")

        if student["role"] != "admin" and (
            r["owner_id"] != student["id"] or r["ownership_status"] != "assigned"
        ):
            raise HTTPException(403, "You can only delete verified rooms assigned to your Owner account.")

        c.execute("DELETE FROM rooms WHERE id=?", (room_id,))

    return {"message": "Room deleted successfully.", "room_id": room_id}


# AGENT-TO-ROOM PERMISSION WORKFLOW

@app.post("/agents/me/room-requests", status_code=201)
def request_room_advertising(data: AgentRoomRequest, agent=Depends(require_agent)):
    with get_connection() as c:
        room = c.execute("""
            SELECT id, available, ownership_status, owner_id
            FROM rooms WHERE id=?
        """, (data.room_id,)).fetchone()

        if room is None:
            raise HTTPException(404, "Room not found.")
        if room["ownership_status"] != "assigned" or room["owner_id"] is None:
            raise HTTPException(409, "This room has no confirmed owner yet. RumCHUO must verify ownership before an Agent can request permission.")
        if room["owner_id"] == agent["id"]:
            raise HTTPException(400, "You cannot request advertising permission for your own room.")

        existing = c.execute("""
            SELECT id, status FROM agent_room_authorizations
            WHERE room_id=? AND agent_id=?
        """, (data.room_id, agent["id"])).fetchone()

        if existing and existing["status"] in {"pending", "approved"}:
            raise HTTPException(409, f"A room permission request already exists with status '{existing['status']}'.")

        if existing:
            c.execute("""
                UPDATE agent_room_authorizations
                SET status='pending', advertising_enabled=1,
                    requested_at=CURRENT_TIMESTAMP, reviewed_at=NULL,
                    reviewed_by=NULL, review_note=?
                WHERE id=?
            """, (data.note.strip(), existing["id"]))
        else:
            c.execute("""
                INSERT INTO agent_room_authorizations(room_id, agent_id, review_note)
                VALUES(?,?,?)
            """, (data.room_id, agent["id"], data.note.strip()))

    return {"message": "Request sent to the room owner.", "room_id": data.room_id, "status": "pending"}


@app.get("/agents/me/rooms")
def my_agent_rooms(agent=Depends(require_agent)):
    with get_connection() as c:
        rows = c.execute("""
            SELECT
                r.id AS room_id, r.location, r.price, r.available,
                r.room_type, r.created_at,
                a.status AS authorization_status,
                a.advertising_enabled
            FROM agent_room_authorizations a
            JOIN rooms r ON r.id=a.room_id
            WHERE a.agent_id=?
            ORDER BY a.requested_at DESC
        """, (agent["id"],)).fetchall()

    return {"rooms": [dict(r) for r in rows]}


@app.post("/agents/me/rooms/{room_id}/remove")
def remove_room_from_agent_page(room_id: int, agent=Depends(require_agent)):
    with get_connection() as c:
        cur = c.execute("""
            UPDATE agent_room_authorizations
            SET advertising_enabled=0
            WHERE room_id=? AND agent_id=? AND status='approved'
        """, (room_id, agent["id"]))

        if cur.rowcount == 0:
            raise HTTPException(404, "No approved advertising permission was found for this room.")

    return {
        "message": "Advertising has been paused. Your Owner's approval is still valid; you can enable this property for students again at any time."
    }


@app.post("/agents/me/rooms/{room_id}/enable")
def enable_room_for_students(room_id: int, agent=Depends(require_agent)):
    with get_connection() as c:
        authorization = c.execute("""
            SELECT a.id, a.status, r.available, r.ownership_status, r.owner_id
            FROM agent_room_authorizations a
            JOIN rooms r ON r.id=a.room_id
            WHERE a.room_id=? AND a.agent_id=?
        """, (room_id, agent["id"])).fetchone()

        if authorization is None:
            raise HTTPException(404, "No advertising permission record was found for this property.")
        if authorization["status"] != "approved":
            raise HTTPException(409, "This property does not have an active Owner approval.")
        if authorization["ownership_status"] != "assigned" or authorization["owner_id"] is None:
            raise HTTPException(409, "This property no longer has verified ownership.")
        if not authorization["available"]:
            raise HTTPException(409, "This property is currently unavailable and cannot be enabled for students.")

        c.execute("""
            UPDATE agent_room_authorizations
            SET advertising_enabled=1
            WHERE id=? AND agent_id=? AND status='approved'
        """, (authorization["id"], agent["id"]))

    return {
        "message": "Property enabled for students. Your existing Owner approval remains valid.",
        "room_id": room_id,
        "status": "approved",
        "advertising_enabled": True
    }


@app.post("/agents/me/rooms/{room_id}/rental-report", status_code=201)
def report_room_rented(room_id: int, data: RentalReport, agent=Depends(require_agent)):
    with get_connection() as c:
        auth = c.execute("""
            SELECT id FROM agent_room_authorizations
            WHERE room_id=? AND agent_id=? AND status='approved'
        """, (room_id, agent["id"])).fetchone()

        if auth is None:
            raise HTTPException(403, "You need approved advertising permission to report this room.")

        c.execute("""
            INSERT INTO room_rental_reports(room_id, agent_id, note)
            VALUES(?,?,?)
        """, (room_id, agent["id"], data.note.strip()))

    return {"message": "Rental report sent to the Owner for confirmation.", "status": "pending"}


# OWNER ROUTES


@app.get("/landlord/me/rooms")
def landlord_rooms(owner=Depends(require_landlord)):
    with get_connection() as c:
        rows = c.execute(
            """
            SELECT *
            FROM rooms
            WHERE owner_id=?
            ORDER BY id DESC
            """,
            (owner["id"],)
        ).fetchall()

        properties = []

        for row in rows:
            item = room_to_dict(row)
            room_id = row["id"]

            # First, look for the property in the shared
            # Owner/Agent application workflow.
            shared_application = c.execute(
                """
                SELECT id, status
                FROM agent_property_applications
                WHERE room_id=?
                  AND agent_id=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (room_id, owner["id"])
            ).fetchone()

            if shared_application is not None:
                application_id = shared_application["id"]

                photo_rows = c.execute(
                    """
                    SELECT id, original_name, content_type
                    FROM agent_property_photos
                    WHERE application_id=?
                    ORDER BY id
                    """,
                    (application_id,)
                ).fetchall()

                item["application_id"] = application_id
                item["application_status"] = shared_application["status"]
                item["photos"] = [
                    dict(photo) for photo in photo_rows
                ]

            else:
                # Preserve support for older properties
                # that still use the legacy ownership tables.
                application = c.execute(
                    """
                    SELECT id, status
                    FROM room_ownership_applications
                    WHERE room_id=?
                      AND landlord_id=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (room_id, owner["id"])
                ).fetchone()

                if application is None:
                    item["application_id"] = None
                    item["application_status"] = None
                    item["photos"] = []

                else:
                    item["application_id"] = application["id"]
                    item["application_status"] = application["status"]

                    photo_rows = c.execute(
                        """
                        SELECT id, original_name, content_type
                        FROM room_ownership_photos
                        WHERE application_id=?
                        ORDER BY id
                        """,
                        (application["id"],)
                    ).fetchall()

                    item["photos"] = [
                        dict(photo) for photo in photo_rows
                    ]

            properties.append(item)

    return {"rooms": properties}



@app.get("/landlord/me/authorization-requests")
def landlord_authorization_requests(owner=Depends(require_landlord)):
    with get_connection() as c:
        rows = c.execute("""
            SELECT
                a.id, a.room_id, a.agent_id, a.status,
                a.requested_at, a.review_note,
                r.location, r.price,
                s.full_name AS agent_name,
                s.email AS agent_email,
                s.phone AS agent_phone
            FROM agent_room_authorizations a
            JOIN rooms r ON r.id=a.room_id
            JOIN students s ON s.id=a.agent_id
            WHERE r.owner_id=?
            ORDER BY CASE WHEN a.status='pending' THEN 0 ELSE 1 END,
                     a.requested_at DESC
        """, (owner["id"],)).fetchall()

    return {"requests": [dict(r) for r in rows]}


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
            raise HTTPException(404, "Authorization request not found.")
        if a["owner_id"] != owner["id"]:
            raise HTTPException(403, "You can only decide requests for rooms you own.")
        if data.decision in {"approve", "reject"} and a["status"] != "pending":
            raise HTTPException(409, "Only pending requests can be approved or rejected.")
        if data.decision == "revoke" and a["status"] != "approved":
            raise HTTPException(409, "Only approved permissions can be revoked.")

        status = {
            "approve": "approved",
            "reject": "rejected",
            "revoke": "revoked"
        }[data.decision]

        c.execute("""
            UPDATE agent_room_authorizations
            SET status=?, advertising_enabled=?,
                reviewed_at=CURRENT_TIMESTAMP, reviewed_by=?, review_note=?
            WHERE id=?
        """, (
            status, 1 if status == "approved" else 0,
            owner["id"], data.note.strip(), authorization_id
        ))

    return {"authorization_id": authorization_id, "status": status, "message": "Authorization updated."}


@app.get("/landlord/me/rental-reports")
def landlord_rental_reports(owner=Depends(require_landlord)):
    with get_connection() as c:
        rows = c.execute("""
            SELECT
                rr.id, rr.room_id, rr.agent_id, rr.note, rr.status,
                rr.created_at, r.location, s.full_name AS agent_name
            FROM room_rental_reports rr
            JOIN rooms r ON r.id=rr.room_id
            JOIN students s ON s.id=rr.agent_id
            WHERE r.owner_id=?
            ORDER BY rr.id DESC
        """, (owner["id"],)).fetchall()

    return {"reports": [dict(r) for r in rows]}


@app.post("/landlord/me/rental-reports/{report_id}/confirm")
def confirm_rented(report_id: int, owner=Depends(require_landlord)):
    with get_connection() as c:
        report = c.execute("""
            SELECT rr.*, r.owner_id
            FROM room_rental_reports rr
            JOIN rooms r ON r.id=rr.room_id
            WHERE rr.id=?
        """, (report_id,)).fetchone()

        if report is None:
            raise HTTPException(404, "Rental report not found.")
        if report["owner_id"] != owner["id"]:
            raise HTTPException(403, "You can only confirm rental reports for your rooms.")
        if report["status"] != "pending":
            raise HTTPException(409, "This rental report has already been reviewed.")

        c.execute("UPDATE rooms SET available=0 WHERE id=?", (report["room_id"],))
        c.execute("""
            UPDATE room_rental_reports
            SET status='confirmed', reviewed_at=CURRENT_TIMESTAMP, reviewed_by=?
            WHERE id=?
        """, (owner["id"], report_id))
        c.execute("""
            UPDATE agent_room_authorizations
            SET advertising_enabled=0 WHERE room_id=?
        """, (report["room_id"],))

    return {"message": "Rental confirmed. The room is now marked unavailable and hidden from Agent pages."}


# ACTIVE AGENTS MAP AND ENQUIRIES

def parse_json_list(value):
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def count_agent_verified_properties(c, agent_id):
    row = c.execute("""
        SELECT COUNT(DISTINCT r.id) AS total
        FROM rooms r
        WHERE r.available=1
        AND r.ownership_status='assigned'
        AND r.owner_id IS NOT NULL
        AND (
            EXISTS (
                SELECT 1
                FROM agent_property_applications ap
                WHERE ap.room_id=r.id
                AND ap.agent_id=?
                AND ap.status='approved'
            )
            OR EXISTS (
                SELECT 1
                FROM agent_room_authorizations ara
                WHERE ara.room_id=r.id
                AND ara.agent_id=?
                AND ara.status='approved'
                AND ara.advertising_enabled=1
            )
        )
    """, (agent_id, agent_id)).fetchone()

    return row["total"] if row else 0


def agent_discovery_profile(c, agent_id):
    profile = c.execute("""
        SELECT * FROM agent_discovery_profiles WHERE agent_id=?
    """, (agent_id,)).fetchone()

    if profile is None:
        return {
            "is_available": False,
            "service_areas": [],
            "universities_covered": [],
            "share_exact_location": False,
            "latitude": None,
            "longitude": None,
            "subscription_tier": "free",
            "subscription_status": "inactive"
        }

    share = bool(profile["share_exact_location"])

    return {
        "is_available": bool(profile["is_available"]),
        "service_areas": parse_json_list(profile["service_areas"]),
        "universities_covered": parse_json_list(profile["universities_covered"]),
        "share_exact_location": share,
        "latitude": profile["latitude"] if share else None,
        "longitude": profile["longitude"] if share else None,
        "subscription_tier": profile["subscription_tier"],
        "subscription_status": profile["subscription_status"]
    }


@app.get("/agents/me/discovery-profile")
def get_agent_discovery_profile(agent=Depends(require_agent)):
    with get_connection() as c:
        profile = agent_discovery_profile(c, agent["id"])

    return profile


@app.put("/agents/me/discovery-profile")
def update_agent_discovery_profile(
    data: AgentDiscoveryUpdate,
    agent=Depends(require_agent)
):
    with get_connection() as c:
        c.execute("""
            INSERT INTO agent_discovery_profiles(
                agent_id, is_available, service_areas,
                universities_covered, share_exact_location,
                latitude, longitude, subscription_tier,
                subscription_status, updated_at
            )
            VALUES(?,?,?,?,?,?,?,'free','inactive',CURRENT_TIMESTAMP)
            ON CONFLICT(agent_id) DO UPDATE SET
                is_available=excluded.is_available,
                service_areas=excluded.service_areas,
                universities_covered=excluded.universities_covered,
                share_exact_location=excluded.share_exact_location,
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                updated_at=CURRENT_TIMESTAMP
        """, (
            agent["id"],
            int(data.is_available),
            json.dumps(data.service_areas),
            json.dumps(data.universities_covered),
            int(data.share_exact_location),
            data.latitude if data.share_exact_location else None,
            data.longitude if data.share_exact_location else None
        ))

        profile = agent_discovery_profile(c, agent["id"])

    return {
        **profile,
        "message": "Your Active Agents profile has been updated."
    }


@app.get("/students/active-agents")
def get_active_agents(student=Depends(get_current_student)):
    """
    Return approved, verified, active Agents who have enabled availability.
    Exact coordinates are returned only when the Agent has opted in.
    Includes the registered agent phone number for student contact.
    """

    with get_connection() as c:
        agents = c.execute("""
            SELECT
                s.id,
                s.full_name,
                s.phone,
                s.university,
                d.service_areas,
                d.universities_covered,
                d.share_exact_location,
                d.latitude,
                d.longitude
            FROM agent_discovery_profiles d
            JOIN students s ON s.id=d.agent_id
            WHERE d.is_available=1
            AND s.role='agent'
            AND s.account_status='active'
            AND s.email_verified=1
            AND EXISTS (
                SELECT 1 FROM agent_applications aa
                WHERE aa.student_id=s.id
                AND aa.status='approved'
            )
            ORDER BY s.full_name COLLATE NOCASE
        """).fetchall()

        results = []

        for agent in agents:
            profile = agent_discovery_profile(c, agent["id"])
            property_count = count_agent_verified_properties(
                c, agent["id"]
            )

            results.append({
                "agent_id": agent["id"],
                "full_name": agent["full_name"],
                "phone": agent["phone"],
                "university": agent["university"],
                "service_areas": profile["service_areas"],
                "universities_covered": profile["universities_covered"],
                "available": True,
                "verified_property_count": property_count,
                "share_exact_location": profile["share_exact_location"],
                "latitude": profile["latitude"],
                "longitude": profile["longitude"]
            })

    return {
        "count": len(results),
        "agents": results
    }

@app.post("/students/agent-enquiries", status_code=201)
def create_agent_enquiry(
    data: AgentEnquiryCreate,
    student=Depends(get_current_student)
):
    if student["role"] != "student":
        raise HTTPException(403, "Only student accounts can send Agent enquiries.")

    with get_connection() as c:
        agent = c.execute("""
            SELECT s.id
            FROM students s
            JOIN agent_discovery_profiles d ON d.agent_id=s.id
            WHERE s.id=?
            AND s.role='agent'
            AND s.account_status='active'
            AND s.email_verified=1
            AND d.is_available=1
            AND EXISTS (
                SELECT 1 FROM agent_applications aa
                WHERE aa.student_id=s.id
                AND aa.status='approved'
            )
        """, (data.agent_id,)).fetchone()

        if agent is None:
            raise HTTPException(404, "This Agent is not currently available.")

        if data.agent_id == student["id"]:
            raise HTTPException(400, "You cannot send an enquiry to yourself.")

        if data.room_id is not None:
            room = c.execute("""
                SELECT r.id
                FROM rooms r
                WHERE r.id=?
                AND r.available=1
                AND r.ownership_status='assigned'
                AND r.owner_id IS NOT NULL
                AND (
                    EXISTS (
                        SELECT 1 FROM agent_property_applications ap
                        WHERE ap.room_id=r.id
                        AND ap.agent_id=?
                        AND ap.status='approved'
                    )
                    OR EXISTS (
                        SELECT 1 FROM agent_room_authorizations ara
                        WHERE ara.room_id=r.id
                        AND ara.agent_id=?
                        AND ara.status='approved'
                        AND ara.advertising_enabled=1
                    )
                )
            """, (data.room_id, data.agent_id)).fetchone()

            if room is None:
                raise HTTPException(
                    404,
                    "This property is not an available verified property advertised by this Agent."
                )

        # The student's contact details are saved with the enquiry so the
        # Agent can reach them. They are only ever shown to the Agent the
        # enquiry was addressed to.
        cur = c.execute("""
            INSERT INTO agent_enquiries(
                student_id, agent_id, room_id, message,
                contact_phone, contact_email
            )
            VALUES(?,?,?,?,?,?)
        """, (
            student["id"],
            data.agent_id,
            data.room_id,
            data.message,
            student["phone"],
            student["email"]
        ))

        enquiry_id = cur.lastrowid

    return {
        "message": "Your enquiry has been sent to the Agent.",
        "enquiry_id": enquiry_id,
        "status": "new"
    }


@app.get("/agents/me/enquiries")
def get_agent_enquiries(agent=Depends(require_agent)):
    # Only enquiries addressed to this Agent are returned
    # (WHERE e.agent_id = the logged-in Agent), so an Agent can never
    # see contact details of students who did not contact them.
    with get_connection() as c:
        rows = c.execute("""
            SELECT
                e.id,
                e.student_id,
                e.room_id,
                e.message,
                e.status,
                e.created_at,
                e.updated_at,
                e.reply_message,
                e.replied_at,
                s.full_name AS student_name,
                s.university AS student_university,
                s.course AS student_course,
                COALESCE(e.contact_phone, s.phone) AS student_phone,
                COALESCE(e.contact_email, s.email) AS student_email,
                r.location AS property_location
            FROM agent_enquiries e
            JOIN students s ON s.id=e.student_id
            LEFT JOIN rooms r ON r.id=e.room_id
            WHERE e.agent_id=?
            ORDER BY e.created_at DESC, e.id DESC
        """, (agent["id"],)).fetchall()

    return {
        "count": len(rows),
        "enquiries": [dict(row) for row in rows]
    }


@app.put("/agents/me/enquiries/{enquiry_id}/status")
def update_agent_enquiry_status(
    enquiry_id: int,
    data: AgentEnquiryStatusUpdate,
    agent=Depends(require_agent)
):
    with get_connection() as c:
        enquiry = c.execute("""
            SELECT id, status FROM agent_enquiries
            WHERE id=? AND agent_id=?
        """, (enquiry_id, agent["id"])).fetchone()

        if enquiry is None:
            raise HTTPException(404, "Enquiry not found.")

        if enquiry["status"] == "replied" and data.status == "read":
            raise HTTPException(
                409,
                "This enquiry has already been replied to."
            )

        c.execute("""
            UPDATE agent_enquiries
            SET status=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND agent_id=?
        """, (data.status, enquiry_id, agent["id"]))

    return {
        "message": "Enquiry status updated.",
        "enquiry_id": enquiry_id,
        "status": data.status
    }


@app.post("/agents/me/enquiries/{enquiry_id}/reply")
def reply_to_agent_enquiry(
    enquiry_id: int,
    data: AgentEnquiryReply,
    agent=Depends(require_agent)
):
    with get_connection() as c:
        enquiry = c.execute("""
            SELECT id, status, reply_message
            FROM agent_enquiries
            WHERE id=? AND agent_id=?
        """, (enquiry_id, agent["id"])).fetchone()

        if enquiry is None:
            raise HTTPException(404, "Enquiry not found.")

        if enquiry["status"] == "closed":
            raise HTTPException(409, "This enquiry is closed and can no longer be replied to.")

        if enquiry["reply_message"]:
            raise HTTPException(409, "You have already replied to this enquiry.")

        c.execute("""
            UPDATE agent_enquiries
            SET reply_message=?,
                replied_at=CURRENT_TIMESTAMP,
                replied_by=?,
                status='replied',
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND agent_id=?
        """, (data.reply, agent["id"], enquiry_id, agent["id"]))

    return {
        "message": "Your reply has been sent to the student.",
        "enquiry_id": enquiry_id,
        "status": "replied"
    }


@app.get("/students/me/agent-enquiries")
def get_student_agent_enquiries(student=Depends(get_current_student)):
    with get_connection() as c:
        rows = c.execute("""
            SELECT
                e.id,
                e.agent_id,
                e.room_id,
                e.message,
                e.status,
                e.created_at,
                e.updated_at,
                e.reply_message,
                e.replied_at,
                s.full_name AS agent_name,
                r.location AS property_location
            FROM agent_enquiries e
            JOIN students s ON s.id=e.agent_id
            LEFT JOIN rooms r ON r.id=e.room_id
            WHERE e.student_id=?
            ORDER BY e.created_at DESC, e.id DESC
        """, (student["id"],)).fetchall()

    return {
        "count": len(rows),
        "enquiries": [dict(row) for row in rows]
    }


# ROOMMATE MATCHING

def normalize_location_words(value):
    if not value:
        return set()
    ignored = {"near", "area", "the", "at", "in", "of"}
    return {
        w for w in re.findall(r"[a-z0-9]+", value.lower())
        if w not in ignored
    }


def normalize_match_text(value):
    if not value:
        return ""
    return " ".join(value.strip().casefold().split())


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

    if not student["university"] or not student["university"].strip():
        raise HTTPException(
            400,
            "Add your university to your profile before searching for roommates."
        )

    if not student["gender"]:
        raise HTTPException(
            400,
            "Set your gender in your profile before searching for roommates."
        )

    if same_year and student["year_of_study"] is None:
        raise HTTPException(
            400,
            "Add your year of study to your profile before using the same-year filter."
        )

    university = normalize_match_text(student["university"])
    gender = normalize_match_text(student["gender"])
    target_location = normalize_location_words(student["preferred_location"])
    target_faculty = normalize_match_text(student["faculty"])
    target_course = normalize_match_text(student["course"])
    target_year = student["year_of_study"]

    with get_connection() as c:
        candidates = c.execute("""
            SELECT *
            FROM students
            WHERE id != ?
            AND email_verified=1
            AND account_status='active'
            AND role='student'
            AND room_type='shared'
            AND LOWER(TRIM(university))=LOWER(TRIM(?))
            AND LOWER(TRIM(gender))=LOWER(TRIM(?))
        """, (
            student["id"],
            student["university"].strip(),
            student["gender"].strip()
        )).fetchall()

    matches = []

    for candidate in candidates:
        if same_year and candidate["year_of_study"] != target_year:
            continue

        score = 1
        matching_reasons = []

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

        candidate_faculty = normalize_match_text(candidate["faculty"])

        if (
            target_faculty
            and candidate_faculty
            and target_faculty == candidate_faculty
        ):
            score += 2
            matching_reasons.append("Same faculty")

        candidate_course = normalize_match_text(candidate["course"])

        if (
            target_course
            and candidate_course
            and target_course == candidate_course
        ):
            score += 2
            matching_reasons.append("Same course")

        if (
            target_year is not None
            and candidate["year_of_study"] is not None
            and candidate["year_of_study"] == target_year
        ):
            score += 1
            matching_reasons.append("Same year of study")

        # Respect the candidate's contact privacy setting.
        contact_visible = (
            int(candidate["contact_visibility"] or 0) == 1
        )

        matches.append({
            "student_id": candidate["id"],
            "full_name": candidate["full_name"],

            # Only share the phone number when visibility is ON.
            "phone": (
                candidate["phone"]
                if contact_visible
                else None
            ),

            "contact_visibility": contact_visible,

            "university": candidate["university"],
            "faculty": candidate["faculty"],
            "course": candidate["course"],
            "year_of_study": candidate["year_of_study"],
            "preferred_location": candidate["preferred_location"],
            "match_score": score,
            "matching_reasons": matching_reasons
        })

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
            "Scores are indicators, not guarantees of compatibility. "
            "Phone numbers are only shared when the student allows "
            "contact visibility."
        )
    }


# ============================================================
# STUDENT ROOMMATE REQUEST SYSTEM
# ============================================================


def require_student_role(student):
    if student["role"] != "student":
        raise HTTPException(
            403,
            "Only student accounts can use roommate requests."
        )


# 1. SEND ROOMMATE REQUEST

@app.post("/students/roommate-requests/{student_id}", status_code=201)
def send_roommate_request(
    student_id: int,
    student=Depends(get_current_student)
):
    require_student_role(student)

    sender_id = student["id"]

    # Prevent requesting yourself.
    if sender_id == student_id:
        raise HTTPException(
            400,
            "You cannot send a roommate request to yourself."
        )

    with get_connection() as c:

        # Confirm recipient exists and is an eligible student.
        recipient = c.execute("""
            SELECT id, role, account_status, email_verified
            FROM students
            WHERE id = ?
        """, (student_id,)).fetchone()

        if recipient is None:
            raise HTTPException(404, "Student not found.")

        if (
            recipient["role"] != "student"
            or recipient["account_status"] != "active"
            or not recipient["email_verified"]
        ):
            raise HTTPException(
                400,
                "This student is not currently eligible for roommate requests."
            )

        # Check for an existing pending request or accepted connection
        # in either direction.
        existing = c.execute("""
            SELECT id, status
            FROM roommate_requests
            WHERE (
                (sender_id = ? AND recipient_id = ?)
                OR
                (sender_id = ? AND recipient_id = ?)
            )
            AND status IN ('pending', 'accepted')
            LIMIT 1
        """, (
            sender_id,
            student_id,
            student_id,
            sender_id
        )).fetchone()

        if existing:
            if existing["status"] == "pending":
                raise HTTPException(
                    409,
                    "A pending roommate request already exists between these students."
                )

            raise HTTPException(
                409,
                "You are already connected as roommates."
            )

        try:
            cursor = c.execute("""
                INSERT INTO roommate_requests (
                    sender_id,
                    recipient_id,
                    status
                )
                VALUES (?, ?, 'pending')
            """, (
                sender_id,
                student_id
            ))

            request_id = cursor.lastrowid

        except sqlite3.IntegrityError:
            raise HTTPException(
                409,
                "A duplicate roommate request already exists."
            )

    return {
        "success": True,
        "request_id": request_id,
        "recipient_id": student_id,
        "status": "pending",
        "message": "Roommate request sent successfully."
    }


# 2. VIEW RECEIVED ROOMMATE REQUESTS

@app.get("/students/me/roommate-requests")
def get_my_roommate_requests(
    student=Depends(get_current_student)
):
    require_student_role(student)

    with get_connection() as c:
        requests = c.execute("""
            SELECT
                r.id AS request_id,
                r.sender_id AS student_id,
                s.full_name,
                s.university,
                s.faculty,
                s.course,
                s.year_of_study,
                s.preferred_location,

                CASE
                    WHEN COALESCE(s.contact_visibility, 0) = 1
                    THEN s.phone
                    ELSE NULL
                END AS phone,

                COALESCE(s.contact_visibility, 0)
                    AS contact_visibility,

                r.status,
                r.created_at

            FROM roommate_requests r

            JOIN students s
                ON s.id = r.sender_id

            WHERE r.recipient_id = ?
            AND r.status = 'pending'

            ORDER BY r.created_at DESC
        """, (student["id"],)).fetchall()

    return {
        "count": len(requests),
        "requests": [dict(r) for r in requests]
    }


# VIEW SENT ROOMMATE REQUESTS

@app.get("/students/me/roommate-sent-requests")
def get_my_roommate_sent_requests(
    student=Depends(get_current_student)
):
    require_student_role(student)

    with get_connection() as c:
        requests = c.execute("""
            SELECT
                r.id AS request_id,
                r.recipient_id AS student_id,
                s.full_name,
                r.status,
                r.created_at

            FROM roommate_requests r

            JOIN students s
                ON s.id = r.recipient_id

            WHERE r.sender_id = ?
            AND r.status IN ('pending', 'accepted')

            ORDER BY r.created_at DESC
        """, (student["id"],)).fetchall()

    return {
        "count": len(requests),
        "sent_requests": [dict(r) for r in requests]
    }



# 3. ACCEPT OR REJECT A ROOMMATE REQUEST

@app.post("/students/roommate-requests/{request_id}/decision")
def decide_roommate_request(
    request_id: int,
    payload: RoommateRequestDecision,
    student=Depends(get_current_student)
):
    require_student_role(student)

    with get_connection() as c:

        request = c.execute("""
            SELECT *
            FROM roommate_requests
            WHERE id = ?
        """, (request_id,)).fetchone()

        if request is None:
            raise HTTPException(
                404,
                "Roommate request not found."
            )

        # Only the recipient can decide.
        if request["recipient_id"] != student["id"]:
            raise HTTPException(
                403,
                "You are not authorized to decide this roommate request."
            )

        if request["status"] != "pending":
            raise HTTPException(
                409,
                "This roommate request has already been processed."
            )

        new_status = (
            "accepted"
            if payload.decision == "accept"
            else "rejected"
        )

        try:
            cursor = c.execute("""
                UPDATE roommate_requests
                SET status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                AND recipient_id = ?
                AND status = 'pending'
            """, (
                new_status,
                request_id,
                student["id"]
            ))

            if cursor.rowcount != 1:
                raise HTTPException(
                    409,
                    "This roommate request has already been processed."
                )

        except sqlite3.IntegrityError:
            raise HTTPException(
                409,
                "An accepted roommate connection already exists."
            )

    return {
        "success": True,
        "request_id": request_id,
        "status": new_status,
        "message": (
            "Roommate request accepted. You are now connected!"
            if new_status == "accepted"
            else "Roommate request rejected."
        )
    }


# 4. VIEW ACCEPTED ROOMMATE CONNECTIONS

@app.get("/students/me/roommate-connections")
def get_my_roommate_connections(
    student=Depends(get_current_student)
):
    require_student_role(student)

    with get_connection() as c:
        connections = c.execute("""
            SELECT
                r.id AS request_id,

                CASE
                    WHEN r.sender_id = ?
                    THEN r.recipient_id
                    ELSE r.sender_id
                END AS student_id,

                s.full_name,
                s.university,
                s.faculty,
                s.course,
                s.year_of_study,
                s.preferred_location,

                CASE
                    WHEN COALESCE(s.contact_visibility, 0) = 1
                    THEN s.phone
                    ELSE NULL
                END AS phone,

                COALESCE(s.contact_visibility, 0)
                    AS contact_visibility,

                r.created_at AS connected_at

            FROM roommate_requests r

            JOIN students s
                ON s.id = CASE
                    WHEN r.sender_id = ?
                    THEN r.recipient_id
                    ELSE r.sender_id
                END

            WHERE (
                r.sender_id = ?
                OR r.recipient_id = ?
            )
            AND r.status = 'accepted'

            ORDER BY r.updated_at DESC
        """, (
            student["id"],
            student["id"],
            student["id"],
            student["id"]
        )).fetchall()

    return {
        "count": len(connections),
        "connections": [dict(r) for r in connections]
    }


# END OF STUDENT ROOMMATE REQUEST SYSTEM


@app.put("/students/contact-visibility")
def update_contact_visibility(
    visible: bool,
    student=Depends(get_current_student)
):
    with get_connection() as c:
        c.execute("""
            UPDATE students
            SET contact_visibility = ?
            WHERE id = ?
            AND role = 'student'
        """, (
            1 if visible else 0,
            student["id"]
        ))

    return {
        "success": True,
        "contact_visibility": visible,
        "message": (
            "Your phone number is now visible to compatible students."
            if visible
            else "Your phone number is now hidden from compatible students."
        )
    }



# ADMIN ROUTES

from admin_routes import register_admin_routes

register_admin_routes(app, get_connection, get_current_student)

from agent_property_routes import register_agent_property_routes

register_agent_property_routes(app, get_connection, get_current_student)


# AGENT DASHBOARD

@app.get("/agent/dashboard")
def agent_dashboard():
    file_path = Path(__file__).resolve().parent / "agent_dashboard.html"
    if not file_path.exists():
        raise HTTPException(404, "Agent dashboard file not found")
    return FileResponse(file_path)


# AGENT LOGIN PAGE

@app.get("/agent/login")
def agent_login():
    file_path = Path(__file__).resolve().parent / "agent_login.html"
    if not file_path.exists():
        raise HTTPException(404, "Agent login page file not found. Create agent_login.html first.")
    return FileResponse(file_path, media_type="text/html")

# STUDENT SERVICES CENTRE
from student_services_routes import register_student_services_routes

register_student_services_routes(app, get_connection, get_current_student)
