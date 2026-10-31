import os
import re
import uuid
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from fastapi import (
    Depends, HTTPException, UploadFile, File, Form
)
from fastapi.responses import FileResponse


BASE_DIR = Path(__file__).resolve().parent
AGENT_PHOTO_DIR = BASE_DIR / "private_agent_property_photos"

MAX_PHOTOS = 6
MAX_PHOTO_SIZE = 5 * 1024 * 1024

ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def register_agent_property_routes(
    app,
    get_connection,
    get_current_student
):
    AGENT_PHOTO_DIR.mkdir(parents=True, exist_ok=True)

    def require_agent(student=Depends(get_current_student)):
        if student["role"] != "agent":
            raise HTTPException(
                403,
                "Only Agents can submit Agent-owned properties."
            )
        return student

    def require_admin(student=Depends(get_current_student)):
        if student["role"] != "admin":
            raise HTTPException(
                403,
                "Administrator access required."
            )
        return student

    def initialize_tables():
        with get_connection() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS agent_property_applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id INTEGER NOT NULL REFERENCES students(id),
                    room_id INTEGER REFERENCES rooms(id),
                    location TEXT NOT NULL,
                    room_type TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    price REAL NOT NULL,
                    available INTEGER NOT NULL DEFAULT 1,
                    phone_number TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','rejected')),
                    review_note TEXT NOT NULL DEFAULT '',
                    reviewed_by INTEGER REFERENCES students(id),
                    reviewed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS agent_property_photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id INTEGER NOT NULL
                        REFERENCES agent_property_applications(id)
                        ON DELETE CASCADE,
                    stored_name TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

            c.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_agent_property_status
                ON agent_property_applications(status)
            """)

            c.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_agent_property_agent
                ON agent_property_applications(agent_id)
            """)

    initialize_tables()

    def validate_phone(phone):
        phone = re.sub(r"[\s()-]", "", phone or "")
        if not re.fullmatch(r"\+?[0-9]{9,15}", phone):
            raise HTTPException(
                400,
                "Enter a valid contact phone number."
            )
        return phone

    async def save_photos(application_id, photos):
        if not photos:
            raise HTTPException(
                400,
                "Upload at least one property photograph."
            )

        if len(photos) > MAX_PHOTOS:
            raise HTTPException(
                400,
                f"You can upload a maximum of {MAX_PHOTOS} photographs."
            )

        saved = []

        try:
            for photo in photos:
                if photo.content_type not in ALLOWED_TYPES:
                    raise HTTPException(
                        400,
                        "Only JPEG, PNG and WebP photographs are allowed."
                    )

                content = await photo.read()

                if not content:
                    raise HTTPException(
                        400,
                        "One of the uploaded photographs is empty."
                    )

                if len(content) > MAX_PHOTO_SIZE:
                    raise HTTPException(
                        400,
                        "Each photograph must be 5 MB or smaller."
                    )

                extension = ALLOWED_TYPES[photo.content_type]
                stored_name = f"{uuid.uuid4().hex}{extension}"
                destination = AGENT_PHOTO_DIR / stored_name

                with open(destination, "wb") as image_file:
                    image_file.write(content)

                saved.append({
                    "stored_name": stored_name,
                    "original_name": os.path.basename(
                        photo.filename or "property-photo"
                    ),
                    "content_type": photo.content_type
                })

            with get_connection() as c:
                for item in saved:
                    c.execute("""
                        INSERT INTO agent_property_photos(
                            application_id,
                            stored_name,
                            original_name,
                            content_type
                        )
                        VALUES(?,?,?,?)
                    """, (
                        application_id,
                        item["stored_name"],
                        item["original_name"],
                        item["content_type"]
                    ))

        except Exception:
            for item in saved:
                path = AGENT_PHOTO_DIR / item["stored_name"]
                if path.exists():
                    path.unlink()
            raise

    def application_photos(c, application_id):
        rows = c.execute("""
            SELECT id, original_name, content_type
            FROM agent_property_photos
            WHERE application_id=?
            ORDER BY id
        """, (application_id,)).fetchall()

        return [dict(row) for row in rows]

    @app.post(
        "/agents/me/properties/submit",
        status_code=201
    )
    async def submit_agent_property(
        location: str = Form(...),
        room_type: str = Form(...),
        description: str = Form(""),
        price: float = Form(...),
        available: bool = Form(True),
        phone_number: str = Form(...),
        photos: list[UploadFile] = File(...),
        agent=Depends(require_agent)
    ):
        location = location.strip()
        description = description.strip()
        phone_number = validate_phone(phone_number)

        if len(location) < 2 or len(location) > 150:
            raise HTTPException(400, "Enter a valid property location.")

        if room_type not in {
            "single", "shared", "self-contained"
        }:
            raise HTTPException(400, "Invalid room type.")

        if price <= 0:
            raise HTTPException(400, "Rent must be greater than zero.")

        if len(description) > 2000:
            raise HTTPException(400, "Description is too long.")

        with get_connection() as c:
            cur = c.execute("""
                INSERT INTO agent_property_applications(
                    agent_id,
                    location,
                    room_type,
                    description,
                    price,
                    available,
                    phone_number
                )
                VALUES(?,?,?,?,?,?,?)
            """, (
                agent["id"],
                location,
                room_type,
                description,
                price,
                int(available),
                phone_number
            ))

            application_id = cur.lastrowid

        try:
            await save_photos(application_id, photos)
        except Exception:
            with get_connection() as c:
                c.execute("""
                    DELETE FROM agent_property_applications
                    WHERE id=? AND agent_id=?
                """, (application_id, agent["id"]))
            raise

        return {
            "message": (
                "Property submitted successfully. "
                "It is awaiting administrator verification."
            ),
            "application_id": application_id,
            "status": "pending"
        }

    @app.get("/agents/me/properties")
    def get_my_agent_properties(agent=Depends(require_agent)):
        with get_connection() as c:
            rows = c.execute("""
                SELECT *
                FROM agent_property_applications
                WHERE agent_id=?
                ORDER BY id DESC
            """, (agent["id"],)).fetchall()

            applications = []

            for row in rows:
                item = dict(row)
                item["photos"] = application_photos(c, row["id"])
                applications.append(item)

        return {"applications": applications}

    @app.get(
        "/agents/me/properties/{application_id}/photos/{photo_id}"
    )
    def get_my_agent_property_photo(
        application_id: int,
        photo_id: int,
        agent=Depends(require_agent)
    ):
        with get_connection() as c:
            photo = c.execute("""
                SELECT p.*
                FROM agent_property_photos p
                JOIN agent_property_applications a
                    ON a.id=p.application_id
                WHERE p.id=?
                AND a.id=?
                AND a.agent_id=?
            """, (
                photo_id,
                application_id,
                agent["id"]
            )).fetchone()

        if photo is None:
            raise HTTPException(404, "Photograph not found.")

        path = AGENT_PHOTO_DIR / photo["stored_name"]

        if not path.is_file():
            raise HTTPException(404, "Photograph file not found.")

        return FileResponse(
            path,
            media_type=photo["content_type"],
            filename=photo["original_name"]
        )

    @app.get("/admin/agent-property-applications")
    def admin_agent_property_applications(
        status_filter: str | None = None,
        admin=Depends(require_admin)
    ):
        with get_connection() as c:
            if status_filter in {"pending", "approved", "rejected"}:
                rows = c.execute("""
                    SELECT
                        a.*,
                        s.full_name AS agent_name,
                        s.email AS agent_email
                    FROM agent_property_applications a
                    JOIN students s ON s.id=a.agent_id
                    WHERE a.status=?
                    ORDER BY a.id DESC
                """, (status_filter,)).fetchall()
            else:
                rows = c.execute("""
                    SELECT
                        a.*,
                        s.full_name AS agent_name,
                        s.email AS agent_email
                    FROM agent_property_applications a
                    JOIN students s ON s.id=a.agent_id
                    ORDER BY
                        CASE WHEN a.status='pending' THEN 0 ELSE 1 END,
                        a.id DESC
                """).fetchall()

            applications = []

            for row in rows:
                item = dict(row)
                item["photos"] = application_photos(c, row["id"])
                applications.append(item)

        return {"applications": applications}

    @app.get(
        "/admin/agent-property-applications/{application_id}/photos/{photo_id}"
    )
    def admin_agent_property_photo(
        application_id: int,
        photo_id: int,
        admin=Depends(require_admin)
    ):
        with get_connection() as c:
            photo = c.execute("""
                SELECT *
                FROM agent_property_photos
                WHERE id=? AND application_id=?
            """, (photo_id, application_id)).fetchone()

        if photo is None:
            raise HTTPException(404, "Photograph not found.")

        path = AGENT_PHOTO_DIR / photo["stored_name"]

        if not path.is_file():
            raise HTTPException(404, "Photograph file not found.")

        return FileResponse(
            path,
            media_type=photo["content_type"],
            filename=photo["original_name"]
        )

    @app.post(
        "/admin/agent-property-applications/{application_id}/decision"
    )
    def decide_agent_property(
        application_id: int,
        decision: str = Form(...),
        reason: str = Form(""),
        admin=Depends(require_admin)
    ):
        if decision not in {"approve", "reject"}:
            raise HTTPException(400, "Invalid decision.")

        with get_connection() as c:
            application = c.execute("""
                SELECT *
                FROM agent_property_applications
                WHERE id=?
            """, (application_id,)).fetchone()

            if application is None:
                raise HTTPException(404, "Application not found.")

            if application["status"] != "pending":
                raise HTTPException(
                    409,
                    "This application has already been reviewed."
                )

            room_id = None

            if decision == "approve":
                cur = c.execute("""
                    INSERT INTO rooms(
                        location,
                        price,
                        available,
                        room_type,
                        owner_id,
                        ownership_status
                    )
                    VALUES(?,?,?,?,?,'assigned')
                """, (
                    application["location"],
                    application["price"],
                    application["available"],
                    application["room_type"],
                    application["agent_id"]
                ))

                room_id = cur.lastrowid

            new_status = (
                "approved" if decision == "approve" else "rejected"
            )

            c.execute("""
                UPDATE agent_property_applications
                SET status=?,
                    room_id=?,
                    review_note=?,
                    reviewed_by=?,
                    reviewed_at=?
                WHERE id=?
            """, (
                new_status,
                room_id,
                reason.strip(),
                admin["id"],
                datetime.now(timezone.utc).isoformat(),
                application_id
            ))

        return {
            "message": (
                "Agent property approved."
                if decision == "approve"
                else "Agent property rejected."
            ),
            "status": new_status,
            "room_id": room_id
        }