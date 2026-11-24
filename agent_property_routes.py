
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

    def require_property_manager(student=Depends(get_current_student)):
        # Owners and Agents use the same property submission and management
        # workflow. The original agent_id column is retained for compatibility
        # with existing records; it stores the submitting account's student ID.
        if student["role"] not in {"agent", "landlord", "owner"}:
            raise HTTPException(
                403,
                "Only approved Owners and Agents can manage property submissions."
            )
        return student

    def require_admin(student=Depends(get_current_student)):
        if student["role"] != "admin":
            raise HTTPException(
                403,
                "Administrator access required."
            )
        return student

    # ---------------------------------------------------------
    # INITIALIZE DATABASE TABLES
    # ---------------------------------------------------------

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

            columns = {
                row["name"] for row in c.execute(
                    "PRAGMA table_info(agent_property_applications)"
                ).fetchall()
            }
            if "submitted_by_role" not in columns:
                c.execute("""
                    ALTER TABLE agent_property_applications
                    ADD COLUMN submitted_by_role TEXT NOT NULL DEFAULT 'agent'
                """)
            if "legacy_ownership_application_id" not in columns:
                c.execute("""
                    ALTER TABLE agent_property_applications
                    ADD COLUMN legacy_ownership_application_id INTEGER
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
            c.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_agent_property_legacy_owner_app
                ON agent_property_applications(legacy_ownership_application_id)
                WHERE legacy_ownership_application_id IS NOT NULL
            """)

            # Bridge pending Owner applications created by the old workflow
            # into the shared review queue. The legacy rows and photo files are
            # retained; this migration only adds linked shared-queue records.
            legacy_tables = {
                row["name"] for row in c.execute("""
                    SELECT name FROM sqlite_master WHERE type='table'
                """).fetchall()
            }
            if {"room_ownership_applications", "room_ownership_photos", "rooms"}.issubset(legacy_tables):
                legacy_rows = c.execute("""
                    SELECT
                        a.id AS legacy_application_id,
                        a.room_id,
                        a.landlord_id,
                        a.phone_number,
                        a.created_at,
                        r.location,
                        r.price,
                        r.available,
                        r.room_type,
                        s.role AS account_role
                    FROM room_ownership_applications a
                    JOIN rooms r ON r.id=a.room_id
                    JOIN students s ON s.id=a.landlord_id
                    WHERE a.status='pending'
                      AND s.role IN ('landlord','owner')
                      AND NOT EXISTS (
                          SELECT 1 FROM agent_property_applications shared
                          WHERE shared.legacy_ownership_application_id=a.id
                      )
                    ORDER BY a.id
                """).fetchall()

                for legacy in legacy_rows:
                    cur = c.execute("""
                        INSERT INTO agent_property_applications(
                            agent_id, room_id, location, room_type,
                            description, price, available, phone_number,
                            status, submitted_by_role, created_at,
                            legacy_ownership_application_id
                        ) VALUES(?,?,?,?,?,?,?,?, 'pending', ?, ?, ?)
                    """, (
                        legacy["landlord_id"],
                        legacy["room_id"],
                        legacy["location"],
                        legacy["room_type"],
                        "",
                        legacy["price"],
                        legacy["available"],
                        legacy["phone_number"] or "",
                        "owner",
                        legacy["created_at"],
                        legacy["legacy_application_id"]
                    ))
                    shared_id = cur.lastrowid

                    legacy_photos = c.execute("""
                        SELECT stored_name, original_name, content_type, created_at
                        FROM room_ownership_photos
                        WHERE application_id=? ORDER BY id
                    """, (legacy["legacy_application_id"],)).fetchall()
                    for photo in legacy_photos:
                        c.execute("""
                            INSERT INTO agent_property_photos(
                                application_id, stored_name, original_name,
                                content_type, created_at
                            ) VALUES(?,?,?,?,?)
                        """, (
                            shared_id,
                            photo["stored_name"],
                            photo["original_name"],
                            photo["content_type"],
                            photo["created_at"]
                        ))

    initialize_tables()

    # ---------------------------------------------------------
    # VALIDATION HELPERS
    # ---------------------------------------------------------

    def validate_phone(phone):
        phone = re.sub(r"[\s()-]", "", phone or "")

        if not re.fullmatch(r"\+?[0-9]{9,15}", phone):
            raise HTTPException(
                400,
                "Enter a valid contact phone number."
            )

        return phone

    def validate_property(
        location,
        room_type,
        description,
        price,
        phone_number
    ):
        location = (location or "").strip()
        description = (description or "").strip()
        phone_number = validate_phone(phone_number)

        if len(location) < 2 or len(location) > 150:
            raise HTTPException(
                400,
                "Enter a valid property location."
            )

        if room_type not in {
            "single", "shared", "self-contained"
        }:
            raise HTTPException(
                400,
                "Invalid room type."
            )

        if price <= 0:
            raise HTTPException(
                400,
                "Rent must be greater than zero."
            )

        if len(description) > 2000:
            raise HTTPException(
                400,
                "Description is too long."
            )

        return {
            "location": location,
            "room_type": room_type,
            "description": description,
            "price": price,
            "phone_number": phone_number
        }

    def get_owned_application(c, application_id, agent_id):
        application = c.execute("""
            SELECT *
            FROM agent_property_applications
            WHERE id=? AND agent_id=?
        """, (application_id, agent_id)).fetchone()

        if application is None:
            raise HTTPException(
                404,
                "Property application not found."
            )

        return application

    def ensure_editable(application):
        if application["status"] == "approved":
            raise HTTPException(
                409,
                "Approved properties cannot be edited here."
            )

        if application["status"] not in {"pending", "rejected"}:
            raise HTTPException(
                409,
                "This property cannot be modified."
            )

    def application_photos(c, application_id):
        rows = c.execute("""
            SELECT id, original_name, content_type
            FROM agent_property_photos
            WHERE application_id=?
            ORDER BY id
        """, (application_id,)).fetchall()

        return [dict(row) for row in rows]

    # ---------------------------------------------------------
    # PHOTO STORAGE
    # ---------------------------------------------------------

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

                original_name = (
                    Path(
                        (photo.filename or "property-photo")
                        .replace("\\", "/")
                    ).name
                )

                saved.append({
                    "stored_name": stored_name,
                    "original_name": original_name,
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

    def remove_photo_files(stored_names):
        for stored_name in stored_names:
            # Only allow filenames, never arbitrary filesystem paths.
            if not stored_name or Path(stored_name).name != stored_name:
                continue

            path = AGENT_PHOTO_DIR / stored_name

            if path.is_file():
                try:
                    path.unlink()
                except OSError:
                    # Database changes should not be reversed because
                    # a physical file could not be removed.
                    pass

    # ---------------------------------------------------------
    # SUBMIT PROPERTY
    # ---------------------------------------------------------

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
        agent=Depends(require_property_manager)
    ):
        values = validate_property(
            location,
            room_type,
            description,
            price,
            phone_number
        )

        with get_connection() as c:
            cur = c.execute("""
                INSERT INTO agent_property_applications(
                    agent_id,
                    location,
                    room_type,
                    description,
                    price,
                    available,
                    phone_number,
                    submitted_by_role
                )
                VALUES(?,?,?,?,?,?,?,?)
            """, (
                agent["id"],
                values["location"],
                values["room_type"],
                values["description"],
                values["price"],
                int(available),
                values["phone_number"],
                agent["role"]
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

    # ---------------------------------------------------------
    # GET AGENT PROPERTIES
    # ---------------------------------------------------------

    @app.get("/agents/me/properties")
    def get_my_agent_properties(agent=Depends(require_property_manager)):
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

    # ---------------------------------------------------------
    # EDIT PROPERTY DETAILS
    # ---------------------------------------------------------

    @app.put("/agents/me/properties/{application_id}")
    def update_agent_property(
        application_id: int,
        location: str | None = Form(None),
        room_type: str | None = Form(None),
        description: str | None = Form(None),
        price: float | None = Form(None),
        available: bool | None = Form(None),
        phone_number: str | None = Form(None),
        agent=Depends(require_property_manager)
    ):
        with get_connection() as c:
            application = get_owned_application(
                c,
                application_id,
                agent["id"]
            )

            ensure_editable(application)

            values = {
                "location": (
                    location if location is not None
                    else application["location"]
                ),
                "room_type": (
                    room_type if room_type is not None
                    else application["room_type"]
                ),
                "description": (
                    description if description is not None
                    else application["description"]
                ),
                "price": (
                    price if price is not None
                    else application["price"]
                ),
                "phone_number": (
                    phone_number if phone_number is not None
                    else application["phone_number"]
                )
            }

            validated = validate_property(
                values["location"],
                values["room_type"],
                values["description"],
                values["price"],
                values["phone_number"]
            )

            if (
                location is None
                and room_type is None
                and description is None
                and price is None
                and available is None
                and phone_number is None
            ):
                raise HTTPException(
                    400,
                    "Provide at least one property detail to update."
                )

            new_status = (
                "pending"
                if application["status"] == "rejected"
                else application["status"]
            )

            new_available = (
                int(available)
                if available is not None
                else application["available"]
            )

            c.execute("""
                UPDATE agent_property_applications
                SET location=?,
                    room_type=?,
                    description=?,
                    price=?,
                    available=?,
                    phone_number=?,
                    status=?,
                    review_note=CASE
                        WHEN ?='pending' AND status='rejected'
                        THEN ''
                        ELSE review_note
                    END,
                    reviewed_by=CASE
                        WHEN ?='pending' AND status='rejected'
                        THEN NULL
                        ELSE reviewed_by
                    END,
                    reviewed_at=CASE
                        WHEN ?='pending' AND status='rejected'
                        THEN NULL
                        ELSE reviewed_at
                    END
                WHERE id=? AND agent_id=?
            """, (
                validated["location"],
                validated["room_type"],
                validated["description"],
                validated["price"],
                new_available,
                validated["phone_number"],
                new_status,
                new_status,
                new_status,
                new_status,
                application_id,
                agent["id"]
            ))

        return {
            "message": (
                "Property updated and resubmitted for review."
                if new_status == "pending"
                and application["status"] == "rejected"
                else "Property updated successfully."
            ),
            "application_id": application_id,
            "status": new_status
        }

    # ---------------------------------------------------------
    # DELETE PROPERTY APPLICATION
    # ---------------------------------------------------------

    @app.delete("/agents/me/properties/{application_id}")
    def delete_agent_property(
        application_id: int,
        agent=Depends(require_property_manager)
    ):
        stored_names = []

        with get_connection() as c:
            # Verify that the application belongs to this agent.
            application = get_owned_application(
                c,
                application_id,
                agent["id"]
            )

            # Approved properties must have a linked room.
            # Deactivate the room; do not physically delete it.
            if application["status"] == "approved":
                room_id = application["room_id"]

                if room_id is None:
                    raise HTTPException(
                        409,
                        "Approved property has no linked room. "
                        "Application was not deleted."
                    )

                room_update = c.execute("""
                    UPDATE rooms
                    SET available = 0
                    WHERE id = ? AND owner_id = ?
                """, (room_id, agent["id"]))

                if room_update.rowcount != 1:
                    raise HTTPException(
                        409,
                        "The linked room could not be deactivated. "
                        "Application was not deleted."
                    )

            # Collect photograph filenames before deleting records.
            photos = c.execute("""
                SELECT stored_name
                FROM agent_property_photos
                WHERE application_id = ?
            """, (application_id,)).fetchall()

            stored_names = [
                row["stored_name"] for row in photos
            ]

            # Delete photograph database records.
            c.execute("""
                DELETE FROM agent_property_photos
                WHERE application_id = ?
            """, (application_id,))

            # Delete the application belonging to this agent.
            deleted = c.execute("""
                DELETE FROM agent_property_applications
                WHERE id = ? AND agent_id = ?
            """, (application_id, agent["id"]))

            if deleted.rowcount != 1:
                raise HTTPException(
                    404,
                    "Property application not found."
                )

        # Remove physical photograph files after database commit.
        remove_photo_files(stored_names)

        return {
            "message": "Property deleted successfully.",
            "application_id": application_id
        }

    # ---------------------------------------------------------
    # ADD PROPERTY PHOTOGRAPHS
    # ---------------------------------------------------------

    @app.post("/agents/me/properties/{application_id}/photos", status_code=201)
    async def add_agent_property_photos(
        application_id: int,
        photos: list[UploadFile] = File(...),
        agent=Depends(require_property_manager)
    ):
        if not photos:
            raise HTTPException(400, "Select at least one photograph.")

        saved = []
        with get_connection() as c:
            application = get_owned_application(c, application_id, agent["id"])
            ensure_editable(application)
            current_count = c.execute(
                "SELECT COUNT(*) FROM agent_property_photos WHERE application_id=?",
                (application_id,)
            ).fetchone()[0]

        if current_count + len(photos) > MAX_PHOTOS:
            raise HTTPException(400, f"A property can have a maximum of {MAX_PHOTOS} photographs.")

        try:
            for photo in photos:
                if photo.content_type not in ALLOWED_TYPES:
                    raise HTTPException(400, "Only JPEG, PNG and WebP photographs are allowed.")
                content = await photo.read()
                if not content:
                    raise HTTPException(400, "One of the uploaded photographs is empty.")
                if len(content) > MAX_PHOTO_SIZE:
                    raise HTTPException(400, "Each photograph must be 5 MB or smaller.")

                extension = ALLOWED_TYPES[photo.content_type]
                stored_name = f"{uuid.uuid4().hex}{extension}"
                with open(AGENT_PHOTO_DIR / stored_name, "wb") as image_file:
                    image_file.write(content)
                saved.append({
                    "stored_name": stored_name,
                    "original_name": Path((photo.filename or "property-photo").replace("\\", "/")).name,
                    "content_type": photo.content_type
                })

            with get_connection() as c:
                # Recheck status and count inside the write transaction to avoid
                # concurrent uploads exceeding the six-photo limit.
                application = get_owned_application(c, application_id, agent["id"])
                ensure_editable(application)
                count = c.execute(
                    "SELECT COUNT(*) FROM agent_property_photos WHERE application_id=?",
                    (application_id,)
                ).fetchone()[0]
                if count + len(saved) > MAX_PHOTOS:
                    raise HTTPException(400, f"A property can have a maximum of {MAX_PHOTOS} photographs.")
                for item in saved:
                    c.execute("""
                        INSERT INTO agent_property_photos(
                            application_id, stored_name, original_name, content_type
                        ) VALUES(?,?,?,?)
                    """, (application_id, item["stored_name"], item["original_name"], item["content_type"]))
        except Exception:
            remove_photo_files([item["stored_name"] for item in saved])
            raise

        return {"message": "Property photographs added successfully.", "added": len(saved)}

    # ---------------------------------------------------------
    # REMOVE A PROPERTY PHOTO
    # ---------------------------------------------------------

    @app.delete(
        "/agents/me/properties/{application_id}/photos/{photo_id}"
    )
    def delete_agent_property_photo(
        application_id: int,
        photo_id: int,
        agent=Depends(require_property_manager)
    ):
        stored_name = None

        with get_connection() as c:
            application = get_owned_application(
                c,
                application_id,
                agent["id"]
            )

            ensure_editable(application)

            photo = c.execute("""
                SELECT *
                FROM agent_property_photos
                WHERE id=? AND application_id=?
            """, (photo_id, application_id)).fetchone()

            if photo is None:
                raise HTTPException(
                    404,
                    "Photograph not found."
                )

            photo_count = c.execute("""
                SELECT COUNT(*)
                FROM agent_property_photos
                WHERE application_id=?
            """, (application_id,)).fetchone()[0]

            if photo_count <= 1:
                raise HTTPException(
                    400,
                    "A property must have at least one photograph."
                )

            stored_name = photo["stored_name"]

            c.execute("""
                DELETE FROM agent_property_photos
                WHERE id=? AND application_id=?
            """, (photo_id, application_id))

        remove_photo_files([stored_name])

        return {
            "message": "Photograph removed successfully.",
            "photo_id": photo_id
        }

    # ---------------------------------------------------------
    # AGENT PROPERTY PHOTO VIEW
    # ---------------------------------------------------------

    @app.get(
        "/agents/me/properties/{application_id}/photos/{photo_id}"
    )
    def get_my_agent_property_photo(
        application_id: int,
        photo_id: int,
        agent=Depends(require_property_manager)
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
            raise HTTPException(
                404,
                "Photograph not found."
            )

        path = AGENT_PHOTO_DIR / photo["stored_name"]
        if not path.is_file():
            legacy_photo_dir = BASE_DIR / "private_ownership_photos"
            path = legacy_photo_dir / photo["stored_name"]

        if not path.is_file():
            raise HTTPException(
                404,
                "Photograph file not found."
            )

        return FileResponse(
            path,
            media_type=photo["content_type"],
            filename=photo["original_name"]
        )

    # ---------------------------------------------------------
    # ADMIN: LIST PROPERTY APPLICATIONS
    # ---------------------------------------------------------

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
                        s.email AS agent_email,
                        COALESCE(a.submitted_by_role, 'agent') AS submitter_role
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
                        s.email AS agent_email,
                        COALESCE(a.submitted_by_role, 'agent') AS submitter_role
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

    # ---------------------------------------------------------
    # ADMIN: VIEW PROPERTY PHOTOS
    # ---------------------------------------------------------

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
            raise HTTPException(
                404,
                "Photograph not found."
            )

        path = AGENT_PHOTO_DIR / photo["stored_name"]
        if not path.is_file():
            path = BASE_DIR / "private_ownership_photos" / photo["stored_name"]

        if not path.is_file():
            raise HTTPException(
                404,
                "Photograph file not found."
            )

        return FileResponse(
            path,
            media_type=photo["content_type"],
            filename=photo["original_name"]
        )

    # ---------------------------------------------------------
    # ADMIN: APPROVE OR REJECT PROPERTY
    # ---------------------------------------------------------

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
            raise HTTPException(
                400,
                "Invalid decision."
            )

        with get_connection() as c:
            application = c.execute("""
                SELECT *
                FROM agent_property_applications
                WHERE id=?
            """, (application_id,)).fetchone()

            if application is None:
                raise HTTPException(
                    404,
                    "Application not found."
                )

            if application["status"] != "pending":
                raise HTTPException(
                    409,
                    "This application has already been reviewed."
                )

            room_id = application["room_id"]

            if decision == "approve":
                if room_id is not None:
                    # Legacy Owner submission already has a room row. Approve
                    # that exact row; never create a duplicate room listing.
                    c.execute("""
                        UPDATE rooms
                        SET owner_id=?, location=?, price=?, available=?,
                            room_type=?, ownership_status='assigned'
                        WHERE id=?
                    """, (
                        application["agent_id"],
                        application["location"],
                        application["price"],
                        application["available"],
                        application["room_type"],
                        room_id
                    ))
                else:
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
            elif room_id is not None:
                c.execute("""
                    UPDATE rooms SET ownership_status='rejected'
                    WHERE id=? AND ownership_status='pending'
                """, (room_id,))

            new_status = (
                "approved"
                if decision == "approve"
                else "rejected"
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
                "Property approved and added to room listings."
                if decision == "approve"
                else "Property submission rejected."
            ),
            "status": new_status,
            "room_id": room_id
        }
