

import os

import re

import uuid

import sqlite3

from pathlib import Path

from datetime import datetime, timezone



from fastapi import (

          Depends, FastAPI, HTTPException, UploadFile, File, Form, Request

)

from fastapi.responses import FileResponse, JSONResponse

from pydantic import BaseModel, Field





BASE_DIR = Path(__file__).resolve().parent



DASHBOARD_FILE = BASE_DIR / "admin_dashboard.html"

LANDLORD_DASHBOARD_FILE = BASE_DIR / "landlord_dashboard.html"

AGENT_DASHBOARD_FILE = BASE_DIR / "agent_dashboard.html"



# This directory is private. Do not mount it as a public static directory.

PRIVATE_PHOTO_DIR = BASE_DIR / "private_ownership_photos"

PRIVATE_PHOTO_DIR.mkdir(parents=True, exist_ok=True)



MAX_PHOTO_SIZE = 5 * 1024 * 1024

MAX_PHOTOS = 6



ALLOWED_PHOTO_TYPES = {

          "image/jpeg": ".jpg",

          "image/png": ".png",

          "image/webp": ".webp",

}





class AgentApplicationDecision(BaseModel):

          decision: str = Field(pattern="^(approve|reject)$")

          reason: str = Field(default="", max_length=500)





class AccountSuspension(BaseModel):

          suspended: bool

          reason: str = Field(default="", max_length=500)





class LandlordRoleAssignment(BaseModel):

          confirmation_note: str = Field(min_length=5, max_length=500)





class LandlordApplication(BaseModel):

          relationship_to_property: str = Field(

                    pattern="^(owner|manager|authorized_representative)$"

          )

          application_note: str = Field(min_length=20, max_length=1000)

          declaration_confirmed: bool





class LandlordApplicationDecision(BaseModel):

          decision: str = Field(pattern="^(approve|reject)$")

          reason: str = Field(default="", max_length=500)





class RoomOwnerAssignment(BaseModel):

          owner_id: int = Field(gt=0)

          confirmation_note: str = Field(min_length=5, max_length=500)





class PropertyApplicationDecision(BaseModel):

          decision: str = Field(pattern="^(approve|reject)$")

          reason: str = Field(default="", max_length=500)





def validate_phone_number(value):

          phone = (value or "").strip()



          # Permit international prefixes, digits, spaces, brackets and hyphens.

          if not re.fullmatch(r"[+0-9()**\-**\s]{5,25}", phone):

                    raise HTTPException(

                              status_code=422,

                              detail="Enter a valid contact phone number."

                    )



          digits = re.sub(r"\D", "", phone)



          if len(digits) < 5 or len(digits) > 20:

                    raise HTTPException(

                              status_code=422,

                              detail="Enter a valid contact phone number."

                    )



          return phone





def register_admin_routes(app: FastAPI, get_connection, get_current_student):



          # ---------------------------------------------------------

          # DATABASE SETUP

          # ---------------------------------------------------------



          with get_connection() as c:



                    cols = {

                              r["name"]

                              for r in c.execute("PRAGMA table_info(students)").fetchall()

                    }



                    if "account_status" not in cols:

                              c.execute(

                                        "ALTER TABLE students ADD COLUMN account_status "

                                        "TEXT NOT NULL DEFAULT 'active'"

                              )



                    c.execute("""

                              CREATE TABLE IF NOT EXISTS agent_applications (

                                        id INTEGER PRIMARY KEY AUTOINCREMENT,

                                        student_id INTEGER NOT NULL UNIQUE,

                                        application_note TEXT NOT NULL DEFAULT '',

                                        status TEXT NOT NULL DEFAULT 'pending',

                                        reviewed_by INTEGER,

                                        review_reason TEXT NOT NULL DEFAULT '',

                                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                                        reviewed_at TEXT,

                                        FOREIGN KEY(student_id) REFERENCES students(id),

                                        FOREIGN KEY(reviewed_by) REFERENCES students(id)

                              )

                    """)



                    c.execute("""

                              CREATE TABLE IF NOT EXISTS admin_audit_logs (

                                        id INTEGER PRIMARY KEY AUTOINCREMENT,

                                        admin_id INTEGER NOT NULL,

                                        action TEXT NOT NULL,

                                        target_type TEXT NOT NULL,

                                        target_id INTEGER,

                                        details TEXT NOT NULL DEFAULT '',

                                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP

                              )

                    """)



                    c.execute("""

                              CREATE TABLE IF NOT EXISTS landlord_applications (

                                        id INTEGER PRIMARY KEY AUTOINCREMENT,

                                        student_id INTEGER NOT NULL UNIQUE REFERENCES students(id),

                                        relationship_to_property TEXT NOT NULL

                                                  CHECK(relationship_to_property IN (

                                                            'owner','manager','authorized_representative'

                                                  )),

                                        application_note TEXT NOT NULL,

                                        declaration_confirmed INTEGER NOT NULL DEFAULT 0,

                                        status TEXT NOT NULL DEFAULT 'pending'

                                                  CHECK(status IN ('pending','approved','rejected')),

                                        reviewed_by INTEGER REFERENCES students(id),

                                        review_reason TEXT NOT NULL DEFAULT '',

                                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                                        reviewed_at TEXT

                              )

                    """)



                    # Property ownership applications.

                    c.execute("""

                              CREATE TABLE IF NOT EXISTS room_ownership_applications (

                                        id INTEGER PRIMARY KEY AUTOINCREMENT,

                                        room_id INTEGER NOT NULL REFERENCES rooms(id),

                                        landlord_id INTEGER NOT NULL REFERENCES students(id),

                                        application_type TEXT NOT NULL

                                                  CHECK(application_type IN ('new_room','claim_existing')),

                                        status TEXT NOT NULL DEFAULT 'pending'

                                                  CHECK(status IN ('pending','approved','rejected')),

                                        review_reason TEXT NOT NULL DEFAULT '',

                                        reviewed_by INTEGER REFERENCES students(id),

                                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

                                        reviewed_at TEXT

                              )

                    """)



                    # Safe additive migration: add the phone column if it is missing.

                    # Existing applications and approved properties are preserved.

                    ownership_columns = {

                              r["name"]

                              for r in c.execute(

                                        "PRAGMA table_info(room_ownership_applications)"

                              ).fetchall()

                    }



                    if "phone_number" not in ownership_columns:

                              c.execute("""

                                        ALTER TABLE room_ownership_applications

                                        ADD COLUMN phone_number TEXT NOT NULL DEFAULT ''

                              """)



                    # Private photographs belonging to an ownership application.

                    c.execute("""

                              CREATE TABLE IF NOT EXISTS room_ownership_photos (

                                        id INTEGER PRIMARY KEY AUTOINCREMENT,

                                        application_id INTEGER NOT NULL

                                                  REFERENCES room_ownership_applications(id)

                                                  ON DELETE CASCADE,

                                        stored_name TEXT NOT NULL UNIQUE,

                                        original_name TEXT NOT NULL,

                                        content_type TEXT NOT NULL,

                                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP

                              )

                    """)



                    c.execute("""

                              CREATE INDEX IF NOT EXISTS

                              idx_agent_applications_status

                              ON agent_applications(status)

                    """)



                    c.execute("""

                              CREATE INDEX IF NOT EXISTS

                              idx_landlord_applications_status

                              ON landlord_applications(status)

                    """)



                    c.execute("""

                              CREATE INDEX IF NOT EXISTS

                              idx_admin_audit_created

                              ON admin_audit_logs(created_at)

                    """)



                    c.execute("""

                              CREATE INDEX IF NOT EXISTS

                              idx_ownership_applications_status

                              ON room_ownership_applications(status)

                    """)



                    c.execute("""

                              CREATE INDEX IF NOT EXISTS

                              idx_ownership_applications_landlord

                              ON room_ownership_applications(landlord_id)

                    """)



                    # Prevent two pending claims for the same room.

                    c.execute("""

                              CREATE UNIQUE INDEX IF NOT EXISTS

                              idx_one_pending_ownership_application_per_room

                              ON room_ownership_applications(room_id)

                              WHERE status='pending'

                    """)



          # ---------------------------------------------------------

          # ACCESS CONTROL

          # ---------------------------------------------------------



          def require_admin(student=Depends(get_current_student)):

                    if student["role"] != "admin":

                              raise HTTPException(

                                        status_code=403,

                                        detail="Administrator access is required."

                              )

                    return student



          def require_landlord(student=Depends(get_current_student)):

                    if student["role"] != "landlord":

                              raise HTTPException(

                                        status_code=403,

                                        detail="Only an approved landlord can perform this action."

                              )

                    return student



          def write_audit(

                    c, admin_id, action, target_type, target_id=None, details=""

          ):

                    c.execute("""

                              INSERT INTO admin_audit_logs(

                                        admin_id, action, target_type, target_id, details

                              )

                              VALUES(?,?,?,?,?)

                    """, (

                              admin_id, action, target_type, target_id, details

                    ))



          # Prevent landlords from bypassing photo verification

          # through the existing POST /rooms endpoint in main.py.

          #

          # Admins can still use the existing endpoint.

          @app.middleware("http")

          async def block_unverified_landlord_room_creation(request: Request, call_next):



                    if request.method == "POST" and request.url.path == "/rooms":



                              authorization = request.headers.get("Authorization", "")



                              if authorization.lower().startswith("bearer "):

                                        from fastapi.security import HTTPAuthorizationCredentials



                                        credentials = HTTPAuthorizationCredentials(

                                                  scheme="Bearer",

                                                  credentials=authorization.split(" ", 1)[1]

                                        )



                                        try:

                                                  student = get_current_student(credentials)



                                                  if student["role"] == "landlord":

                                                            return JSONResponse(

                                                                      status_code=403,

                                                                      content={

                                                                                "detail": (

                                                                                          "Landlords must submit properties through "

                                                                                          "the ownership verification process."

                                                                                )

                                                                      }

                                                            )



                                        except HTTPException:

                                                  pass



                    return await call_next(request)



          # ---------------------------------------------------------

          # PHOTO VALIDATION AND STORAGE

          # ---------------------------------------------------------



          def detect_image_type(data):

                    if data.startswith(b"\xff\xd8\xff"):

                              return "image/jpeg"



                    if data.startswith(b"\x89PNG\r\n\x1a\n"):

                              return "image/png"



                    if (

                              len(data) >= 12

                              and data[:4] == b"RIFF"

                              and data[8:12] == b"WEBP"

                    ):

                              return "image/webp"



                    return None



          async def validate_and_store_photos(photos):



                    if not photos:

                              raise HTTPException(

                                        status_code=422,

                                        detail="Upload at least one actual property photograph."

                              )



                    if len(photos) > MAX_PHOTOS:

                              raise HTTPException(

                                        status_code=422,

                                        detail=f"You can upload a maximum of {MAX_PHOTOS} photographs."

                              )



                    stored = []



                    try:

                              for photo in photos:



                                        if not photo.filename:

                                                  raise HTTPException(

                                                            status_code=422,

                                                            detail="A photograph has no filename."

                                                  )



                                        data = await photo.read(MAX_PHOTO_SIZE + 1)



                                        if not data:

                                                  raise HTTPException(

                                                            status_code=422,

                                                            detail="An uploaded photograph is empty."

                                                  )



                                        if len(data) > MAX_PHOTO_SIZE:

                                                  raise HTTPException(

                                                            status_code=413,

                                                            detail="Each photograph must be 5 MB or smaller."

                                                  )



                                        actual_type = detect_image_type(data)



                                        if actual_type not in ALLOWED_PHOTO_TYPES:

                                                  raise HTTPException(

                                                            status_code=415,

                                                            detail=(

                                                                      "Only genuine JPEG, PNG and WebP photographs "

                                                                      "are accepted."

                                                            )

                                                  )



                                        extension = ALLOWED_PHOTO_TYPES[actual_type]

                                        stored_name = f"{uuid.uuid4().hex}{extension}"



                                        destination = PRIVATE_PHOTO_DIR / stored_name

                                        destination.write_bytes(data)



                                        original_name = Path(photo.filename).name

                                        original_name = re.sub(

                                                  r"[^A-Za-z0-9_. -]", "_", original_name

                                        )[:200]



                                        stored.append({

                                                  "stored_name": stored_name,

                                                  "original_name": original_name,

                                                  "content_type": actual_type

                                        })



                              return stored



                    except Exception:

                              for item in stored:

                                        path = PRIVATE_PHOTO_DIR / item["stored_name"]

                                        if path.is_file():

                                                  path.unlink()

                              raise



                    finally:

                              for photo in photos:

                                        await photo.close()



          def delete_stored_files(items):

                    for item in items:

                              path = PRIVATE_PHOTO_DIR / item["stored_name"]

                              if path.is_file():

                                        path.unlink()



          def save_application_photos(c, application_id, photos):

                    for photo in photos:

                              c.execute("""

                                        INSERT INTO room_ownership_photos(

                                                  application_id, stored_name,

                                                  original_name, content_type

                                        )

                                        VALUES(?,?,?,?)

                              """, (

                                        application_id,

                                        photo["stored_name"],

                                        photo["original_name"],

                                        photo["content_type"]

                              ))



          # ---------------------------------------------------------

          # DASHBOARD PAGES

          # ---------------------------------------------------------



          @app.get("/admin/dashboard", include_in_schema=False)

          def admin_dashboard():

                    if not DASHBOARD_FILE.is_file():

                              raise HTTPException(

                                        status_code=404,

                                        detail="Admin dashboard HTML file was not found."

                              )

                    return FileResponse(DASHBOARD_FILE)



          @app.get("/landlord/dashboard", include_in_schema=False)

          def landlord_dashboard():

                    if not LANDLORD_DASHBOARD_FILE.is_file():

                              raise HTTPException(

                                        status_code=404,

                                        detail="Landlord dashboard HTML file was not found."

                              )

                    return FileResponse(LANDLORD_DASHBOARD_FILE)



          @app.get("/agent/dashboard", include_in_schema=False)

          def agent_dashboard():

                    if not AGENT_DASHBOARD_FILE.is_file():

                              raise HTTPException(

                                        status_code=404,

                                        detail="Agent dashboard HTML file was not found."

                              )

                    return FileResponse(AGENT_DASHBOARD_FILE)



          # ---------------------------------------------------------

          # ADMIN OVERVIEW

          # ---------------------------------------------------------



          @app.get("/admin/overview")

          def admin_overview(admin=Depends(require_admin)):



                    with get_connection() as c:



                              accounts = c.execute(

                                        "SELECT COUNT(*) n FROM students"

                              ).fetchone()["n"]



                              agents = c.execute(

                                        "SELECT COUNT(*) n FROM students WHERE role='agent'"

                              ).fetchone()["n"]



                              pending = c.execute(

                                        "SELECT COUNT(*) n FROM agent_applications WHERE status='pending'"

                              ).fetchone()["n"]



                              pending_landlords = c.execute(

                                        "SELECT COUNT(*) n FROM landlord_applications WHERE status='pending'"

                              ).fetchone()["n"]



                              rooms = c.execute(

                                        "SELECT COUNT(*) n FROM rooms"

                              ).fetchone()["n"]



                              unassigned = c.execute("""

                                        SELECT COUNT(*) n FROM rooms

                                        WHERE owner_id IS NULL OR ownership_status!='assigned'

                              """).fetchone()["n"]



                              pending_ownership = c.execute("""

                                        SELECT COUNT(*) n

                                        FROM room_ownership_applications

                                        WHERE status='pending'

                              """).fetchone()["n"]



                    return {

                              "total_accounts": accounts,

                              "approved_agents": agents,

                              "pending_agent_applications": pending,

                              "pending_landlord_applications": pending_landlords,

                              "total_rooms": rooms,

                              "unassigned_rooms": unassigned,

                              "pending_room_ownership_applications": pending_ownership

                    }



          # ---------------------------------------------------------

          # AGENT APPLICATIONS

          # ---------------------------------------------------------



          @app.post("/agents/apply", status_code=201)

          def apply_to_be_agent(

                    application_note: str = "",

                    student=Depends(get_current_student)

          ):



                    note = application_note.strip()



                    if len(note) > 1000:

                              raise HTTPException(

                                        status_code=422,

                                        detail="Application note must be 1,000 characters or fewer."

                              )



                    if student["role"] in {"admin", "agent", "landlord"}:

                              raise HTTPException(

                                        status_code=400,

                                        detail="This account is not eligible to submit a new Agent application."

                              )



                    with get_connection() as c:



                              existing = c.execute(

                                        "SELECT id FROM agent_applications WHERE student_id=?",

                                        (student["id"],)

                              ).fetchone()



                              if existing:

                                        raise HTTPException(

                                                  status_code=409,

                                                  detail="An Agent application already exists."

                                        )



                              c.execute("""

                                        INSERT INTO agent_applications(

                                                  student_id,application_note

                                        )

                                        VALUES(?,?)

                              """, (student["id"], note))



                    return {

                              "message": "Your Agent application was submitted for review.",

                              "status": "pending"

                    }



          # ---------------------------------------------------------

          # LANDLORD APPLICATIONS

          # ---------------------------------------------------------



          @app.post("/landlords/apply", status_code=201)

          def apply_to_be_landlord(

                    data: LandlordApplication,

                    student=Depends(get_current_student)

          ):



                    if student["role"] in {"admin", "agent", "landlord"}:

                              raise HTTPException(

                                        status_code=400,

                                        detail="This account is not eligible to submit a landlord application."

                              )



                    if not data.declaration_confirmed:

                              raise HTTPException(

                                        status_code=422,

                                        detail="Confirm the declaration before submitting your application."

                              )



                    note = data.application_note.strip()



                    if len(note) < 20:

                              raise HTTPException(

                                        status_code=422,

                                        detail="Please provide at least 20 characters describing your property or management authority."

                              )



                    with get_connection() as c:



                              existing = c.execute(

                                        "SELECT id,status FROM landlord_applications WHERE student_id=?",

                                        (student["id"],)

                              ).fetchone()



                              if existing:

                                        raise HTTPException(

                                                  status_code=409,

                                                  detail="A landlord application already exists for this account."

                                        )



                              c.execute("""

                                        INSERT INTO landlord_applications(

                                                  student_id, relationship_to_property,

                                                  application_note, declaration_confirmed

                                        )

                                        VALUES(?,?,?,1)

                              """, (

                                        student["id"],

                                        data.relationship_to_property,

                                        note

                              ))



                    return {

                              "message": "Your landlord application was submitted for review.",

                              "status": "pending"

                    }



          @app.get("/landlords/me/application")

          def my_landlord_application(student=Depends(get_current_student)):



                    with get_connection() as c:

                              row = c.execute("""

                                        SELECT id,relationship_to_property,application_note,

                                                           status,review_reason,created_at,reviewed_at

                                        FROM landlord_applications

                                        WHERE student_id=?

                              """, (student["id"],)).fetchone()



                    return {

                              "application": dict(row) if row else None,

                              "role": student["role"]

                    }



          @app.get("/admin/landlord-applications")

          def list_landlord_applications(admin=Depends(require_admin)):



                    with get_connection() as c:

                              rows = c.execute("""

                                        SELECT a.id,a.student_id,a.relationship_to_property,

                                                           a.application_note,a.declaration_confirmed,a.status,

                                                           a.review_reason,a.created_at,a.reviewed_at,

                                                           s.full_name,s.email,s.email_verified,

                                                           s.account_status,s.role

                                        FROM landlord_applications a

                                        JOIN students s ON s.id=a.student_id

                                        ORDER BY CASE WHEN a.status='pending' THEN 0 ELSE 1 END,

                                                                a.created_at DESC

                              """).fetchall()



                    return {"applications": [dict(r) for r in rows]}



          @app.post("/admin/landlord-applications/{application_id}/decision")

          def decide_landlord_application(

                    application_id: int,

                    data: LandlordApplicationDecision,

                    admin=Depends(require_admin)

          ):



                    reason = data.reason.strip()



                    with get_connection() as c:



                              a = c.execute("""

                                        SELECT a.*,s.role,s.account_status,s.email_verified

                                        FROM landlord_applications a

                                        JOIN students s ON s.id=a.student_id

                                        WHERE a.id=?

                              """, (application_id,)).fetchone()



                              if a is None:

                                        raise HTTPException(404, "Landlord application not found.")



                              if a["status"] != "pending":

                                        raise HTTPException(409, "This application has already been reviewed.")



                              if data.decision == "approve":



                                        if not a["email_verified"]:

                                                  raise HTTPException(409, "The applicant must verify their email first.")



                                        if a["account_status"] != "active":

                                                  raise HTTPException(409, "Unsuspend the account before approval.")



                                        if a["role"] != "student":

                                                  raise HTTPException(409, "Only a student account can be approved as a landlord.")



                                        if not a["declaration_confirmed"]:

                                                  raise HTTPException(409, "The required declaration has not been confirmed.")



                                        c.execute(

                                                  "UPDATE students SET role='landlord' WHERE id=?",

                                                  (a["student_id"],)

                                        )



                                        status = "approved"



                              else:

                                        status = "rejected"



                              c.execute("""

                                        UPDATE landlord_applications

                                        SET status=?,reviewed_by=?,review_reason=?,

                                                  reviewed_at=CURRENT_TIMESTAMP

                                        WHERE id=?

                              """, (

                                        status, admin["id"], reason, application_id

                              ))



                              write_audit(

                                        c, admin["id"],

                                        f"landlord_application_{status}",

                                        "landlord_application",

                                        application_id,

                                        reason

                              )



                    return {

                              "message": f"Landlord application {status}.",

                              "application_id": application_id,

                              "status": status,

                              "role": "landlord" if status == "approved" else "student"

                    }



          # ---------------------------------------------------------

          # ADMIN: AGENT APPLICATION REVIEW

          # ---------------------------------------------------------



          @app.get("/admin/agent-applications")

          def list_agent_applications(admin=Depends(require_admin)):



                    with get_connection() as c:

                              rows = c.execute("""

                                        SELECT a.id,a.student_id,a.application_note,a.status,

                                                           a.review_reason,a.created_at,a.reviewed_at,

                                                           s.full_name,s.email,s.email_verified,s.account_status

                                        FROM agent_applications a

                                        JOIN students s ON s.id=a.student_id

                                        ORDER BY CASE WHEN a.status='pending' THEN 0 ELSE 1 END,

                                                                a.created_at DESC

                              """).fetchall()



                    return {"applications": [dict(r) for r in rows]}



          @app.post("/admin/agent-applications/{application_id}/decision")

          def decide_agent_application(

                    application_id: int,

                    data: AgentApplicationDecision,

                    admin=Depends(require_admin)

          ):



                    with get_connection() as c:



                              a = c.execute("""

                                        SELECT a.*,s.role,s.account_status,s.email_verified

                                        FROM agent_applications a

                                        JOIN students s ON s.id=a.student_id

                                        WHERE a.id=?

                              """, (application_id,)).fetchone()



                              if a is None:

                                        raise HTTPException(404, "Agent application not found.")



                              if a["status"] != "pending":

                                        raise HTTPException(409, "This application has already been reviewed.")



                              status = "approved" if data.decision == "approve" else "rejected"



                              if data.decision == "approve":



                                        if a["account_status"] == "suspended":

                                                  raise HTTPException(409, "Unsuspend the account before approval.")



                                        if not a["email_verified"]:

                                                  raise HTTPException(409, "The applicant must verify their email first.")



                                        if a["role"] != "student":

                                                  raise HTTPException(409, "Only student accounts can become Agents.")



                                        c.execute(

                                                  "UPDATE students SET role='agent' WHERE id=?",

                                                  (a["student_id"],)

                                        )



                              reason = data.reason.strip()



                              c.execute("""

                                        UPDATE agent_applications

                                        SET status=?,reviewed_by=?,review_reason=?,

                                                  reviewed_at=CURRENT_TIMESTAMP

                                        WHERE id=?

                              """, (

                                        status, admin["id"], reason, application_id

                              ))



                              write_audit(

                                        c, admin["id"],

                                        f"agent_application_{status}",

                                        "agent_application",

                                        application_id,

                                        reason

                              )



                    return {

                              "message": f"Agent application {status}.",

                              "application_id": application_id,

                              "status": status

                    }



          # ---------------------------------------------------------

          # ACCOUNT MANAGEMENT

          # ---------------------------------------------------------



          @app.get("/admin/accounts")

          def list_accounts(admin=Depends(require_admin)):



                    with get_connection() as c:

                              rows = c.execute("""

                                        SELECT id,full_name,email,role,email_verified,

                                                           account_status,created_at

                                        FROM students

                                        ORDER BY id DESC LIMIT 200

                              """).fetchall()



                    return {"accounts": [dict(r) for r in rows]}



          @app.post("/admin/accounts/{account_id}/suspension")

          def set_account_suspension(

                    account_id: int,

                    data: AccountSuspension,

                    admin=Depends(require_admin)

          ):



                    with get_connection() as c:



                              target = c.execute(

                                        "SELECT id,role FROM students WHERE id=?",

                                        (account_id,)

                              ).fetchone()



                              if target is None:

                                        raise HTTPException(404, "Account not found.")



                              if target["role"] == "admin":

                                        raise HTTPException(400, "Administrator accounts cannot be suspended here.")



                              status = "suspended" if data.suspended else "active"

                              reason = data.reason.strip()



                              c.execute(

                                        "UPDATE students SET account_status=? WHERE id=?",

                                        (status, account_id)

                              )



                              write_audit(

                                        c, admin["id"],

                                        "account_suspended" if data.suspended else "account_unsuspended",

                                        "student_account",

                                        account_id,

                                        reason

                              )



                    return {

                              "account_id": account_id,

                              "account_status": status

                    }



          # ---------------------------------------------------------

          # ROOM OWNERSHIP MANAGEMENT

          # ---------------------------------------------------------



          @app.get("/admin/rooms/unassigned")

          def list_unassigned_rooms(admin=Depends(require_admin)):



                    with get_connection() as c:

                              rows = c.execute("""

                                        SELECT id,location,price,available,room_type,created_at,

                                                           owner_id,ownership_status

                                        FROM rooms

                                        WHERE owner_id IS NULL OR ownership_status!='assigned'

                                        ORDER BY id

                              """).fetchall()



                    return {

                              "rooms": [dict(r) for r in rows],

                              "note": "Assign only after ownership has been checked."

                    }



          @app.get("/admin/landlord-candidates")

          def landlord_candidates(admin=Depends(require_admin)):



                    with get_connection() as c:

                              rows = c.execute("""

                                        SELECT id,full_name,email,email_verified,account_status,role

                                        FROM students

                                        WHERE role IN ('student','landlord')

                                        ORDER BY full_name

                              """).fetchall()



                    return {"accounts": [dict(r) for r in rows]}



          @app.post("/admin/accounts/{account_id}/landlord-role")

          def assign_landlord_role(

                    account_id: int,

                    data: LandlordRoleAssignment,

                    admin=Depends(require_admin)

          ):



                    note = data.confirmation_note.strip()



                    with get_connection() as c:



                              target = c.execute("""

                                        SELECT id,role,email_verified,account_status

                                        FROM students WHERE id=?

                              """, (account_id,)).fetchone()



                              if target is None:

                                        raise HTTPException(404, "Account not found.")



                              if target["role"] in {"admin", "agent"}:

                                        raise HTTPException(409, "This account cannot be changed to a landlord.")



                              if not target["email_verified"]:

                                        raise HTTPException(409, "The account must verify its email first.")



                              if target["account_status"] != "active":

                                        raise HTTPException(409, "Unsuspend the account first.")



                              c.execute(

                                        "UPDATE students SET role='landlord' WHERE id=?",

                                        (account_id,)

                              )



                              write_audit(

                                        c, admin["id"],

                                        "landlord_role_assigned",

                                        "student_account",

                                        account_id,

                                        note

                              )



                    return {

                              "account_id": account_id,

                              "role": "landlord",

                              "message": "Landlord role assigned. This does not verify ownership of a room."

                    }



          @app.post("/admin/rooms/{room_id}/assign-owner")

          def assign_room_owner(

                    room_id: int,

                    data: RoomOwnerAssignment,

                    admin=Depends(require_admin)

          ):



                    note = data.confirmation_note.strip()



                    with get_connection() as c:



                              room = c.execute("""

                                        SELECT id,owner_id,ownership_status

                                        FROM rooms WHERE id=?

                              """, (room_id,)).fetchone()



                              if room is None:

                                        raise HTTPException(404, "Room not found.")



                              pending = c.execute("""

                                        SELECT id FROM room_ownership_applications

                                        WHERE room_id=? AND status='pending'

                              """, (room_id,)).fetchone()



                              if pending:

                                        raise HTTPException(

                                                  409,

                                                  "A property ownership application is pending review. Review it before manually assigning this room."

                                        )



                              owner = c.execute("""

                                        SELECT id,role,email_verified,account_status

                                        FROM students WHERE id=?

                              """, (data.owner_id,)).fetchone()



                              if owner is None:

                                        raise HTTPException(404, "Landlord account not found.")



                              if owner["role"] != "landlord":

                                        raise HTTPException(409, "The selected account must first be approved as a landlord.")



                              if not owner["email_verified"] or owner["account_status"] != "active":

                                        raise HTTPException(409, "The landlord account must be verified and active.")



                              if room["owner_id"] is not None and room["owner_id"] != data.owner_id:

                                        raise HTTPException(409, "This room already has a different owner.")



                              c.execute("""

                                        UPDATE rooms

                                        SET owner_id=?,ownership_status='assigned'

                                        WHERE id=?

                              """, (data.owner_id, room_id))



                              write_audit(

                                        c, admin["id"],

                                        "room_owner_assigned",

                                        "room",

                                        room_id,

                                        f"owner_id={data.owner_id}; confirmation={note}"

                              )



                    return {

                              "room_id": room_id,

                              "owner_id": data.owner_id,

                              "ownership_status": "assigned",

                              "message": "Room owner assigned and recorded."

                    }



          # ---------------------------------------------------------

          # LANDLORD: SUBMIT A NEW PROPERTY

          # ---------------------------------------------------------



          @app.post("/landlord/me/rooms/submit", status_code=201)

          async def submit_new_property(

                    location: str = Form(...),

                    price: float = Form(...),

                    phone_number: str = Form(...),

                    available: bool = Form(True),

                    room_type: str = Form("single"),

                    photos: list[UploadFile] = File(...),

                    landlord=Depends(require_landlord)

          ):



                    location = location.strip()

                    phone_number = validate_phone_number(phone_number)



                    if len(location) < 2 or len(location) > 150:

                              raise HTTPException(422, "Enter a valid property location.")



                    if price <= 0:

                              raise HTTPException(422, "Rent must be greater than zero.")



                    if room_type not in {"single", "shared", "self-contained"}:

                              raise HTTPException(422, "Select a valid room type.")



                    stored = await validate_and_store_photos(photos)



                    try:

                              with get_connection() as c:



                                        cur = c.execute("""

                                                  INSERT INTO rooms(

                                                            location,price,available,room_type,

                                                            owner_id,ownership_status

                                                  )

                                                  VALUES(?,?,?,?,?,'pending')

                                        """, (

                                                  location,

                                                  price,

                                                  int(available),

                                                  room_type,

                                                  landlord["id"]

                                        ))



                                        room_id = cur.lastrowid



                                        app_cur = c.execute("""

                                                  INSERT INTO room_ownership_applications(

                                                            room_id,landlord_id,application_type,phone_number

                                                  )

                                                  VALUES(?,?,'new_room',?)

                                        """, (

                                                  room_id,

                                                  landlord["id"],

                                                  phone_number

                                        ))



                                        application_id = app_cur.lastrowid



                                        save_application_photos(c, application_id, stored)



                    except Exception:

                              delete_stored_files(stored)

                              raise



                    return {

                              "message": "Property submitted successfully. It is awaiting Admin verification.",

                              "room_id": room_id,

                              "application_id": application_id,

                              "ownership_status": "pending",

                              "status": "pending"

                    }



          # ---------------------------------------------------------

          # LANDLORD: LIST ROOMS THAT CAN BE CLAIMED

          # ---------------------------------------------------------



          @app.get("/landlord/me/claimable-rooms")

          def claimable_rooms(landlord=Depends(require_landlord)):



                    with get_connection() as c:

                              rows = c.execute("""

                                        SELECT r.id,r.location,r.price,r.available,

                                                           r.room_type,r.created_at

                                        FROM rooms r

                                        WHERE r.owner_id IS NULL

                                             AND r.ownership_status='unassigned'

                                             AND NOT EXISTS (

                                                  SELECT 1

                                                  FROM room_ownership_applications a

                                                  WHERE a.room_id=r.id AND a.status='pending'

                                             )

                                        ORDER BY r.id DESC

                              """).fetchall()



                    return {"rooms": [dict(r) for r in rows]}



          # ---------------------------------------------------------

          # LANDLORD: CLAIM AN EXISTING ROOM

          # ---------------------------------------------------------



          @app.post("/landlord/me/rooms/{room_id}/claim", status_code=201)

          async def claim_existing_room(

                    room_id: int,

                    phone_number: str = Form(...),

                    photos: list[UploadFile] = File(...),

                    landlord=Depends(require_landlord)

          ):



                    phone_number = validate_phone_number(phone_number)

                    stored = await validate_and_store_photos(photos)



                    try:

                              with get_connection() as c:



                                        room = c.execute("""

                                                  SELECT id,owner_id,ownership_status

                                                  FROM rooms WHERE id=?

                                        """, (room_id,)).fetchone()



                                        if room is None:

                                                  raise HTTPException(404, "Room not found.")



                                        if room["owner_id"] is not None or room["ownership_status"] != "unassigned":

                                                  raise HTTPException(

                                                            409,

                                                            "This room is no longer available to claim."

                                                  )



                                        pending = c.execute("""

                                                  SELECT id FROM room_ownership_applications

                                                  WHERE room_id=? AND status='pending'

                                        """, (room_id,)).fetchone()



                                        if pending:

                                                  raise HTTPException(

                                                            409,

                                                            "Another ownership application is already pending for this room."

                                                  )



                                        c.execute("""

                                                  UPDATE rooms

                                                  SET owner_id=?,ownership_status='pending'

                                                  WHERE id=? AND owner_id IS NULL

                                                       AND ownership_status='unassigned'

                                        """, (landlord["id"], room_id))



                                        if c.execute("SELECT changes()").fetchone()[0] != 1:

                                                  raise HTTPException(

                                                            409,

                                                            "The room has already been claimed by another account."

                                                  )



                                        app_cur = c.execute("""

                                                  INSERT INTO room_ownership_applications(

                                                            room_id,landlord_id,application_type,phone_number

                                                  )

                                                  VALUES(?,?,'claim_existing',?)

                                        """, (

                                                  room_id,

                                                  landlord["id"],

                                                  phone_number

                                        ))



                                        application_id = app_cur.lastrowid



                                        save_application_photos(c, application_id, stored)



                    except Exception:

                              delete_stored_files(stored)

                              raise



                    return {

                              "message": "Room claim submitted. The property is awaiting Admin verification.",

                              "room_id": room_id,

                              "application_id": application_id,

                              "ownership_status": "pending",

                              "status": "pending"

                    }


          # ---------------------------------------------------------
          # LANDLORD: VIEW OWNERSHIP APPLICATIONS
          # ---------------------------------------------------------

          @app.get("/landlord/me/ownership-applications")
          def landlord_ownership_applications(landlord=Depends(require_landlord)):

                    with get_connection() as c:

                              rows = c.execute("""
                                        SELECT
                                                  a.id,
                                                  a.room_id,
                                                  a.application_type,
                                                  a.status,
                                                  a.phone_number,
                                                  a.review_reason,
                                                  a.created_at,
                                                  a.reviewed_at,
                                                  r.location,
                                                  r.price,
                                                  r.room_type,
                                                  COUNT(p.id) AS photo_count
                                        FROM room_ownership_applications a
                                        JOIN rooms r ON r.id = a.room_id
                                        LEFT JOIN room_ownership_photos p
                                                  ON p.application_id = a.id
                                        WHERE a.landlord_id = ?
                                        GROUP BY a.id
                                        ORDER BY a.created_at DESC
                              """, (landlord["id"],)).fetchall()

                              applications = []

                              for row in rows:

                                        item = dict(row)

                                        photo_rows = c.execute("""
                                                  SELECT
                                                            id,
                                                            original_name,
                                                            content_type
                                                  FROM room_ownership_photos
                                                  WHERE application_id = ?
                                                  ORDER BY id
                                        """, (row["id"],)).fetchall()

                                        item["photos"] = [
                                                  dict(photo)
                                                  for photo in photo_rows
                                        ]

                                        item["photo_count"] = len(item["photos"])

                                        applications.append(item)

                    return {"applications": applications}

          # ---------------------------------------------------------
          # LANDLORD: VIEW A PRIVATE PROPERTY PHOTOGRAPH
          # ---------------------------------------------------------

          @app.get(
              "/landlord/me/ownership-applications/{application_id}/photos/{photo_id}"
          )
          def landlord_view_ownership_photo(
              application_id: int,
              photo_id: int,
              landlord=Depends(require_landlord)
          ):
                    with get_connection() as c:
                              photo = c.execute("""
                                        SELECT
                                                  p.stored_name,
                                                  p.content_type
                                        FROM room_ownership_photos p
                                        JOIN room_ownership_applications a
                                            ON a.id = p.application_id
                                        WHERE p.id = ?
                                          AND a.id = ?
                                          AND a.landlord_id = ?
                              """, (
                                        photo_id,
                                        application_id,
                                        landlord["id"]
                              )).fetchone()

                    if photo is None:
                              raise HTTPException(
                                  404, "Property photograph not found."
                              )

                    filename = Path(photo["stored_name"]).name
                    path = PRIVATE_PHOTO_DIR / filename

                    if not path.is_file():
                              raise HTTPException(
                                  404, "The stored photograph could not be found."
                              )

                    return FileResponse(
                        path,
                        media_type=photo["content_type"],
                        headers={
                            "Cache-Control": "private, no-store",
                            "X-Content-Type-Options": "nosniff"
                        }
                    )




# ---------------------------------------------------------
          # ADMIN: LIST OWNERSHIP APPLICATIONS
          # ---------------------------------------------------------

          @app.get("/admin/room-ownership-applications")
          def admin_ownership_applications(
              status_filter: str = "all",
              admin=Depends(require_admin)
          ):

              if status_filter not in {"all", "pending", "approved", "rejected"}:
                  raise HTTPException(422, "Invalid application status filter.")

              with get_connection() as c:

                  where = ""
                  params = []

                  if status_filter != "all":
                      where = "WHERE a.status=?"
                      params.append(status_filter)

                  rows = c.execute(f"""
                      SELECT
                          a.id,a.room_id,a.landlord_id,
                          a.application_type,a.status,
                          a.phone_number,
                          a.review_reason,a.created_at,a.reviewed_at,
                          r.location,r.price,r.available,r.room_type,
                          s.full_name AS landlord_name,
                          s.email AS landlord_email,
                          s.email_verified,
                          s.account_status,
                          COUNT(p.id) AS photo_count
                      FROM room_ownership_applications a
                      JOIN rooms r ON r.id=a.room_id
                      JOIN students s ON s.id=a.landlord_id
                      LEFT JOIN room_ownership_photos p
                          ON p.application_id=a.id
                      {where}
                      GROUP BY a.id
                      ORDER BY
                          CASE WHEN a.status='pending' THEN 0 ELSE 1 END,
                          a.created_at DESC
                  """, params).fetchall()

                  applications = []

                  for row in rows:

                      item = dict(row)

                      photo_rows = c.execute("""
                          SELECT id,original_name,content_type
                          FROM room_ownership_photos
                          WHERE application_id=?
                          ORDER BY id
                      """, (row["id"],)).fetchall()

                      item["photos"] = [dict(p) for p in photo_rows]
                      applications.append(item)

              return {"applications": applications}



          # ---------------------------------------------------------
          # ADMIN: VIEW A PRIVATE PROPERTY PHOTOGRAPH
          # ---------------------------------------------------------

          @app.get(
              "/admin/room-ownership-applications/{application_id}/photos/{photo_id}"
          )
          def admin_view_ownership_photo(
              application_id: int,
              photo_id: int,
              admin=Depends(require_admin)
          ):

              with get_connection() as c:
                  photo = c.execute("""
                      SELECT stored_name, original_name, content_type
                      FROM room_ownership_photos
                      WHERE id=? AND application_id=?
                  """, (photo_id, application_id)).fetchone()

              if photo is None:
                  raise HTTPException(404, "Property photograph not found.")

              filename = Path(photo["stored_name"]).name
              path = PRIVATE_PHOTO_DIR / filename

              if not path.is_file():
                  raise HTTPException(404, "The stored photograph could not be found.")

              return FileResponse(
                  path,
                  media_type=photo["content_type"],
                  headers={
                      "Cache-Control": "private, no-store",
                      "X-Content-Type-Options": "nosniff"
                  }
              )



          # ---------------------------------------------------------
          # AGENT: VERIFIED LANDLORD PROPERTY DETAILS AND PHOTOS
          # ---------------------------------------------------------

          def require_agent_for_landlord_properties(
              student=Depends(get_current_student)
          ):
              if student["role"] != "agent":
                  raise HTTPException(
                      403,
                      "Only an approved Agent can view landlord contact details and property photographs."
                  )
              return student


          @app.get("/agents/landlord-properties")
          def agent_list_landlord_properties(
              location: str | None = None,
              max_price: float | None = None,
              room_type: str | None = None,
              available_only: bool = True,
              limit: int = 100,
              offset: int = 0,
              agent=Depends(require_agent_for_landlord_properties)
          ):
              if max_price is not None and max_price <= 0:
                  raise HTTPException(422, "Maximum price must be greater than zero.")

              if room_type is not None and room_type not in {
                  "single", "shared", "self-contained"
              }:
                  raise HTTPException(422, "Invalid room type filter.")

              if limit < 1 or limit > 100 or offset < 0:
                  raise HTTPException(422, "Invalid pagination values.")

              conditions = [
                  "r.ownership_status='assigned'",
                  "r.owner_id IS NOT NULL",
                  "a.status='approved'",
                  "a.landlord_id=r.owner_id",
                  "s.role='landlord'",
                  "s.account_status='active'",
                  "a.id=(SELECT MAX(a2.id) FROM room_ownership_applications a2 "
                  "WHERE a2.room_id=r.id AND a2.landlord_id=r.owner_id "
                  "AND a2.status='approved')"
              ]

              params = []

              if available_only:
                  conditions.append("r.available=1")

              if location and location.strip():
                  conditions.append("LOWER(r.location) LIKE ?")
                  params.append(f"%{location.strip().lower()}%")

              if max_price is not None:
                  conditions.append("r.price<=?")
                  params.append(max_price)

              if room_type:
                  conditions.append("r.room_type=?")
                  params.append(room_type)

              where = "WHERE " + " AND ".join(conditions)

              with get_connection() as c:
                  total = c.execute(f"""
                      SELECT COUNT(*) AS n
                      FROM rooms r
                      JOIN room_ownership_applications a ON a.room_id=r.id
                      JOIN students s ON s.id=a.landlord_id
                      {where}
                  """, params).fetchone()["n"]

                  rows = c.execute(f"""
                      SELECT
                          r.id, r.location, r.price, r.available, r.room_type,
                          r.created_at, r.ownership_status,
                          a.id AS application_id,
                          a.phone_number AS landlord_phone,
                          s.full_name AS landlord_name
                      FROM rooms r
                      JOIN room_ownership_applications a ON a.room_id=r.id
                      JOIN students s ON s.id=a.landlord_id
                      {where}
                      ORDER BY r.price, r.id
                      LIMIT ? OFFSET ?
                  """, params + [limit, offset]).fetchall()

                  properties = []

                  for row in rows:
                      item = dict(row)

                      photo_rows = c.execute("""
                          SELECT id, original_name, content_type
                          FROM room_ownership_photos
                          WHERE application_id=?
                          ORDER BY id
                      """, (row["application_id"],)).fetchall()

                      item["photos"] = [dict(photo) for photo in photo_rows]
                      properties.append(item)

              return {
                  "total_rooms": total,
                  "limit": limit,
                  "offset": offset,
                  "returned_rooms": len(properties),
                  "has_more": offset + len(properties) < total,
                  "rooms": properties
              }


          @app.get("/agents/landlord-properties/{room_id}/photos/{photo_id}")
          def agent_view_verified_landlord_photo(
              room_id: int,
              photo_id: int,
              agent=Depends(require_agent_for_landlord_properties)
          ):
              with get_connection() as c:
                  photo = c.execute("""
                      SELECT p.stored_name, p.content_type
                      FROM rooms r
                      JOIN room_ownership_applications a
                          ON a.room_id=r.id AND a.landlord_id=r.owner_id
                      JOIN students s ON s.id=a.landlord_id
                      JOIN room_ownership_photos p ON p.application_id=a.id
                      WHERE r.id=?
                        AND p.id=?
                        AND r.ownership_status='assigned'
                        AND r.owner_id IS NOT NULL
                        AND a.status='approved'
                        AND s.role='landlord'
                        AND s.account_status='active'
                        AND a.id=(
                            SELECT MAX(a2.id)
                            FROM room_ownership_applications a2
                            WHERE a2.room_id=r.id
                              AND a2.landlord_id=r.owner_id
                              AND a2.status='approved'
                        )
                  """, (room_id, photo_id)).fetchone()

              if photo is None:
                  raise HTTPException(404, "Verified property photograph not found.")

              filename = Path(photo["stored_name"]).name
              path = PRIVATE_PHOTO_DIR / filename

              if not path.is_file():
                  raise HTTPException(404, "The stored photograph could not be found.")

              return FileResponse(
                  path,
                  media_type=photo["content_type"],
                  headers={
                      "Cache-Control": "private, no-store",
                      "X-Content-Type-Options": "nosniff"
                  }
              )

                    # ADMIN: APPROVE OR REJECT PROPERTY OWNERSHIP

                    # ---------------------------------------------------------



          @app.post(

                              "/admin/room-ownership-applications/{application_id}/decision"

                    )

          def decide_property_ownership(

                              application_id: int,

                              data: PropertyApplicationDecision,

                              admin=Depends(require_admin)

                    ):



                              reason = data.reason.strip()



                              with get_connection() as c:



                                        application = c.execute("""

                                                  SELECT

                                                            a.*,

                                                            s.role AS landlord_role,

                                                            s.email_verified,

                                                            s.account_status,

                                                            r.owner_id,

                                                            r.ownership_status

                                                  FROM room_ownership_applications a

                                                  JOIN students s ON s.id=a.landlord_id

                                                  JOIN rooms r ON r.id=a.room_id

                                                  WHERE a.id=?

                                        """, (application_id,)).fetchone()



                                        if application is None:

                                                  raise HTTPException(404, "Property application not found.")



                                        if application["status"] != "pending":

                                                  raise HTTPException(

                                                            409,

                                                            "This property application has already been reviewed."

                                                  )



                                        if data.decision == "approve":



                                                  if application["landlord_role"] != "landlord":

                                                            raise HTTPException(

                                                                      409,

                                                                      "The applicant is no longer an approved landlord."

                                                            )



                                                  if not application["email_verified"]:

                                                            raise HTTPException(

                                                                      409,

                                                                      "The landlord must have a verified email."

                                                            )



                                                  if application["account_status"] != "active":

                                                            raise HTTPException(

                                                                      409,

                                                                      "The landlord account must be active."

                                                            )



                                                  if (

                                                            application["owner_id"] is not None

                                                            and application["owner_id"] != application["landlord_id"]

                                                  ):

                                                            raise HTTPException(

                                                                      409,

                                                                      "This room is assigned to another owner."

                                                            )



                                                  c.execute("""

                                                            UPDATE rooms

                                                            SET owner_id=?,ownership_status='assigned'

                                                            WHERE id=?

                                                  """, (

                                                            application["landlord_id"],

                                                            application["room_id"]

                                                  ))



                                                  status = "approved"



                                        else:



                                                  # Preserve the room itself. Only clear the pending

                                                  # ownership assignment when it belongs to this applicant.

                                                  c.execute("""

                                                            UPDATE rooms

                                                            SET owner_id=NULL,ownership_status='unassigned'

                                                            WHERE id=? AND owner_id=?

                                                  """, (

                                                            application["room_id"],

                                                            application["landlord_id"]

                                                  ))



                                                  status = "rejected"



                                        c.execute("""

                                                  UPDATE room_ownership_applications

                                                  SET status=?,review_reason=?,reviewed_by=?,

                                                            reviewed_at=CURRENT_TIMESTAMP

                                                  WHERE id=?

                                        """, (

                                                  status,

                                                  reason,

                                                  admin["id"],

                                                  application_id

                                        ))



                                        write_audit(

                                                  c,

                                                  admin["id"],

                                                  f"property_ownership_{status}",

                                                  "room_ownership_application",

                                                  application_id,

                                                  (

                                                            f"room_id={application['room_id']}; "

                                                            f"landlord_id={application['landlord_id']}; "

                                                            f"reason={reason}"

                                                  )

                                        )



                              return {

                                        "message": f"Property ownership application {status}.",

                                        "application_id": application_id,

                                        "room_id": application["room_id"],

                                        "status": status

                              }



          # ---------------------------------------------------------
          # ROOM AUTHORIZATION OVERVIEW
          # ---------------------------------------------------------

          @app.get("/admin/room-authorization-overview")
          def room_authorization_overview(admin=Depends(require_admin)):

              with get_connection() as c:

                  pending = c.execute(
                      "SELECT COUNT(*) n FROM agent_room_authorizations WHERE status='pending'"
                  ).fetchone()["n"]

                  approved = c.execute(
                      "SELECT COUNT(*) n FROM agent_room_authorizations WHERE status='approved'"
                  ).fetchone()["n"]

                  reports = c.execute(
                      "SELECT COUNT(*) n FROM room_rental_reports WHERE status='pending'"
                  ).fetchone()["n"]

              return {
                  "pending_agent_room_requests": pending,
                  "approved_agent_room_permissions": approved,
                  "pending_rental_reports": reports
              }





          # ---------------------------------------------------------
          # ADMIN AUDIT LOGS
          # ---------------------------------------------------------

          @app.get("/admin/audit-logs")
          def list_audit_logs(admin=Depends(require_admin)):

              with get_connection() as c:
                  rows = c.execute("""
                      SELECT
                          l.id,
                          l.admin_id,
                          s.full_name AS admin_name,
                          l.action,
                          l.target_type,
                          l.target_id,
                          l.details,
                          l.created_at
                      FROM admin_audit_logs l
                      LEFT JOIN students s ON s.id=l.admin_id
                      ORDER BY l.id DESC
                      LIMIT 200
                  """).fetchall()

              return {"audit_logs": [dict(r) for r in rows]}
