
import sqlite3
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator


ServiceCategory = Literal[
    "technical_support",
    "accommodation_assistance",
    "account_registration",
    "payment_assistance",
    "general_enquiries",
    "other"
]

TicketStatus = Literal[
    "open",
    "in_progress",
    "waiting_for_student",
    "resolved",
    "closed"
]


class ServiceTicketCreate(BaseModel):
    category: ServiceCategory
    message: str = Field(min_length=5, max_length=5000)

    @field_validator("message")
    @classmethod
    def clean_text(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be empty.")
        return value


class ServiceTicketMessageCreate(BaseModel):
    message: str = Field(min_length=2, max_length=5000)

    @field_validator("message")
    @classmethod
    def clean_message(cls, value):
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Please enter a message.")
        return value


class ServiceTicketStatusUpdate(BaseModel):
    status: TicketStatus
    admin_note: str = Field(default="", max_length=1000)

    @field_validator("admin_note")
    @classmethod
    def clean_note(cls, value):
        return value.strip()


def register_student_services_routes(
    app: FastAPI,
    get_connection,
    get_current_student
):
    """
    Register RumCHUO Student Services Centre routes.
    """

    # --------------------------------------------------------
    # DATABASE TABLES
    # --------------------------------------------------------

    with get_connection() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS service_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_number TEXT UNIQUE,
                student_id INTEGER NOT NULL
                    REFERENCES students(id) ON DELETE CASCADE,
                category TEXT NOT NULL CHECK(category IN (
                    'technical_support',
                    'accommodation_assistance',
                    'account_registration',
                    'payment_assistance',
                    'general_enquiries',
                    'other'
                )),
                subject TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK(status IN (
                        'open',
                        'in_progress',
                        'waiting_for_student',
                        'resolved',
                        'closed'
                    )),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT,
                closed_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS service_ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL
                    REFERENCES service_tickets(id) ON DELETE CASCADE,
                sender_id INTEGER NOT NULL
                    REFERENCES students(id),
                sender_role TEXT NOT NULL
                    CHECK(sender_role IN ('student', 'admin')),
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_service_tickets_student
            ON service_tickets(student_id, created_at)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_service_tickets_status
            ON service_tickets(status, updated_at)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_service_ticket_messages_ticket
            ON service_ticket_messages(ticket_id, created_at)
        """)

    # --------------------------------------------------------
    # HELPERS
    # --------------------------------------------------------

    def require_student(student):
        if student["role"] != "student":
            raise HTTPException(
                403,
                "Only student accounts can use the Student Services Centre."
            )

        return student

    def require_admin(student):
        if student["role"] != "admin":
            raise HTTPException(
                403,
                "Only administrators can access this service."
            )

        return student

    def get_ticket(c, ticket_id):
        ticket = c.execute("""
            SELECT
                t.*,
                s.full_name AS student_name,
                s.email AS student_email,
                s.phone AS student_phone
            FROM service_tickets t
            JOIN students s ON s.id=t.student_id
            WHERE t.id=?
        """, (ticket_id,)).fetchone()

        if ticket is None:
            raise HTTPException(404, "Service ticket not found.")

        return ticket

    def ticket_summary(c, ticket):
        latest = c.execute("""
            SELECT
                message,
                sender_role,
                created_at
            FROM service_ticket_messages
            WHERE ticket_id=?
            ORDER BY id DESC
            LIMIT 1
        """, (ticket["id"],)).fetchone()

        return {
            "id": ticket["id"],
            "ticket_number": ticket["ticket_number"],
            "student_id": ticket["student_id"],
            "category": ticket["category"],
            "status": ticket["status"],
            "created_at": ticket["created_at"],
            "updated_at": ticket["updated_at"],
            "resolved_at": ticket["resolved_at"],
            "closed_at": ticket["closed_at"],
            "message_count": c.execute("""
                SELECT COUNT(*) AS total
                FROM service_ticket_messages
                WHERE ticket_id=?
            """, (ticket["id"],)).fetchone()["total"],
            "latest_message": dict(latest) if latest else None
        }

    def ticket_details(c, ticket):
        messages = c.execute("""
            SELECT
                m.id,
                m.ticket_id,
                m.sender_id,
                m.sender_role,
                m.message,
                m.created_at,
                s.full_name AS sender_name
            FROM service_ticket_messages m
            JOIN students s ON s.id=m.sender_id
            WHERE m.ticket_id=?
            ORDER BY m.id ASC
        """, (ticket["id"],)).fetchall()

        result = dict(ticket)
        result.pop("subject", None)
        result["messages"] = [dict(message) for message in messages]
        result["message_count"] = len(messages)

        return result

    # --------------------------------------------------------
    # STUDENT: CREATE TICKET
    # --------------------------------------------------------

    @app.post("/students/me/service-tickets", status_code=201)
    def create_service_ticket(
        data: ServiceTicketCreate,
        student=Depends(get_current_student)
    ):
        require_student(student)

        with get_connection() as c:
            cursor = c.execute("""
                INSERT INTO service_tickets(
                    student_id,
                    category,
                    subject,
                    status
                )
                VALUES(?,?,?,'open')
            """, (
                student["id"],
                data.category,
                ""
            ))

            ticket_id = cursor.lastrowid

            year = datetime.now(timezone.utc).year
            ticket_number = f"RCH-{year}-{ticket_id:06d}"

            c.execute("""
                UPDATE service_tickets
                SET ticket_number=?
                WHERE id=?
            """, (ticket_number, ticket_id))

            c.execute("""
                INSERT INTO service_ticket_messages(
                    ticket_id,
                    sender_id,
                    sender_role,
                    message
                )
                VALUES(?,?,'student',?)
            """, (
                ticket_id,
                student["id"],
                data.message
            ))

            ticket = c.execute("""
                SELECT * FROM service_tickets WHERE id=?
            """, (ticket_id,)).fetchone()

            result = ticket_summary(c, ticket)

        return {
            "success": True,
            "message": "Your service request has been submitted.",
            "ticket": result
        }

    # --------------------------------------------------------
    # STUDENT: LIST OWN TICKETS
    # --------------------------------------------------------

    @app.get("/students/me/service-tickets")
    def get_my_service_tickets(
        status: TicketStatus | None = None,
        category: ServiceCategory | None = None,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        student=Depends(get_current_student)
    ):
        require_student(student)

        conditions = ["student_id=?"]
        params = [student["id"]]

        if status:
            conditions.append("status=?")
            params.append(status)

        if category:
            conditions.append("category=?")
            params.append(category)

        where = "WHERE " + " AND ".join(conditions)

        with get_connection() as c:
            total = c.execute(
                f"SELECT COUNT(*) AS total FROM service_tickets {where}",
                params
            ).fetchone()["total"]

            rows = c.execute(f"""
                SELECT *
                FROM service_tickets
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ? OFFSET ?
            """, params + [limit, offset]).fetchall()

            tickets = [
                ticket_summary(c, ticket)
                for ticket in rows
            ]

        return {
            "count": total,
            "returned": len(tickets),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(tickets) < total,
            "tickets": tickets
        }

    # --------------------------------------------------------
    # STUDENT: VIEW ONE TICKET AND ITS CONVERSATION
    # --------------------------------------------------------

    @app.get("/students/me/service-tickets/{ticket_id}")
    def get_my_service_ticket(
        ticket_id: int,
        student=Depends(get_current_student)
    ):
        require_student(student)

        with get_connection() as c:
            ticket = get_ticket(c, ticket_id)

            if ticket["student_id"] != student["id"]:
                raise HTTPException(
                    403,
                    "You are not authorized to view this ticket."
                )

            return ticket_details(c, ticket)

    # --------------------------------------------------------
    # STUDENT: SEND A MESSAGE
    # --------------------------------------------------------

    @app.post(
        "/students/me/service-tickets/{ticket_id}/messages",
        status_code=201
    )
    def student_reply_to_ticket(
        ticket_id: int,
        data: ServiceTicketMessageCreate,
        student=Depends(get_current_student)
    ):
        require_student(student)

        with get_connection() as c:
            ticket = get_ticket(c, ticket_id)

            if ticket["student_id"] != student["id"]:
                raise HTTPException(
                    403,
                    "You are not authorized to reply to this ticket."
                )

            if ticket["status"] == "closed":
                raise HTTPException(
                    409,
                    "This ticket is closed. Please create a new service request."
                )

            c.execute("""
                INSERT INTO service_ticket_messages(
                    ticket_id,
                    sender_id,
                    sender_role,
                    message
                )
                VALUES(?,?,'student',?)
            """, (
                ticket_id,
                student["id"],
                data.message
            ))

            # A student reply reopens a resolved ticket.
            # Otherwise, it indicates that the student has responded.
            new_status = (
                "open"
                if ticket["status"] == "resolved"
                else "in_progress"
            )

            c.execute("""
                UPDATE service_tickets
                SET status=?,
                    updated_at=CURRENT_TIMESTAMP,
                    resolved_at=NULL
                WHERE id=?
            """, (new_status, ticket_id))

        return {
            "success": True,
            "ticket_id": ticket_id,
            "status": new_status,
            "message": "Your message has been added to the ticket."
        }

    # --------------------------------------------------------
    # ADMIN: LIST ALL TICKETS
    # --------------------------------------------------------

    @app.get("/admin/service-tickets")
    def admin_get_service_tickets(
        status: TicketStatus | None = None,
        category: ServiceCategory | None = None,
        search: str | None = Query(default=None, max_length=150),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        admin=Depends(get_current_student)
    ):
        require_admin(admin)

        conditions = []
        params = []

        if status:
            conditions.append("t.status=?")
            params.append(status)

        if category:
            conditions.append("t.category=?")
            params.append(category)

        if search and search.strip():
            term = f"%{search.strip()}%"
            conditions.append("""
                (
                    t.ticket_number LIKE ?
                    OR s.full_name LIKE ?
                    OR s.email LIKE ?
                )
            """)
            params.extend([term, term, term])

        where = (
            "WHERE " + " AND ".join(conditions)
            if conditions else ""
        )

        with get_connection() as c:
            total = c.execute(f"""
                SELECT COUNT(*) AS total
                FROM service_tickets t
                JOIN students s ON s.id=t.student_id
                {where}
            """, params).fetchone()["total"]

            rows = c.execute(f"""
                SELECT t.*, s.full_name AS student_name,
                       s.email AS student_email
                FROM service_tickets t
                JOIN students s ON s.id=t.student_id
                {where}
                ORDER BY
                    CASE t.status
                        WHEN 'open' THEN 1
                        WHEN 'in_progress' THEN 2
                        WHEN 'waiting_for_student' THEN 3
                        WHEN 'resolved' THEN 4
                        WHEN 'closed' THEN 5
                    END,
                    t.updated_at DESC,
                    t.id DESC
                LIMIT ? OFFSET ?
            """, params + [limit, offset]).fetchall()

            tickets = []

            for ticket in rows:
                item = ticket_summary(c, ticket)
                item["student_name"] = ticket["student_name"]
                item["student_email"] = ticket["student_email"]
                tickets.append(item)

            counts = c.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open,
                    SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END) AS in_progress,
                    SUM(CASE WHEN status='waiting_for_student' THEN 1 ELSE 0 END) AS waiting_for_student,
                    SUM(CASE WHEN status='resolved' THEN 1 ELSE 0 END) AS resolved,
                    SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed
                FROM service_tickets
            """).fetchone()

        return {
            "count": total,
            "returned": len(tickets),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(tickets) < total,
            "summary": {
                key: counts[key] or 0
                for key in (
                    "total",
                    "open",
                    "in_progress",
                    "waiting_for_student",
                    "resolved",
                    "closed"
                )
            },
            "tickets": tickets
        }

    # --------------------------------------------------------
    # ADMIN: VIEW TICKET AND CONVERSATION
    # --------------------------------------------------------

    @app.get("/admin/service-tickets/{ticket_id}")
    def admin_get_service_ticket(
        ticket_id: int,
        admin=Depends(get_current_student)
    ):
        require_admin(admin)

        with get_connection() as c:
            ticket = get_ticket(c, ticket_id)
            return ticket_details(c, ticket)

    # --------------------------------------------------------
    # ADMIN: REPLY TO TICKET
    # --------------------------------------------------------

    @app.post(
        "/admin/service-tickets/{ticket_id}/messages",
        status_code=201
    )
    def admin_reply_to_ticket(
        ticket_id: int,
        data: ServiceTicketMessageCreate,
        admin=Depends(get_current_student)
    ):
        require_admin(admin)

        with get_connection() as c:
            ticket = get_ticket(c, ticket_id)

            if ticket["status"] == "closed":
                raise HTTPException(
                    409,
                    "This ticket is closed and cannot receive new messages."
                )

            c.execute("""
                INSERT INTO service_ticket_messages(
                    ticket_id,
                    sender_id,
                    sender_role,
                    message
                )
                VALUES(?,?,'admin',?)
            """, (
                ticket_id,
                admin["id"],
                data.message
            ))

            c.execute("""
                UPDATE service_tickets
                SET status='waiting_for_student',
                    updated_at=CURRENT_TIMESTAMP,
                    resolved_at=NULL
                WHERE id=?
            """, (ticket_id,))

        return {
            "success": True,
            "ticket_id": ticket_id,
            "status": "waiting_for_student",
            "message": "Your response has been sent to the student."
        }

    # --------------------------------------------------------
    # ADMIN: UPDATE TICKET STATUS
    # --------------------------------------------------------

    @app.put("/admin/service-tickets/{ticket_id}/status")
    def admin_update_ticket_status(
        ticket_id: int,
        data: ServiceTicketStatusUpdate,
        admin=Depends(get_current_student)
    ):
        require_admin(admin)

        with get_connection() as c:
            ticket = get_ticket(c, ticket_id)

            if ticket["status"] == "closed" and data.status != "closed":
                raise HTTPException(
                    409,
                    "A closed ticket cannot be reopened. Create a new ticket instead."
                )

            resolved_at = (
                "CURRENT_TIMESTAMP"
                if data.status == "resolved"
                else "NULL"
            )

            closed_at = (
                "CURRENT_TIMESTAMP"
                if data.status == "closed"
                else "NULL"
            )

            c.execute(f"""
                UPDATE service_tickets
                SET status=?,
                    updated_at=CURRENT_TIMESTAMP,
                    resolved_at={resolved_at},
                    closed_at={closed_at}
                WHERE id=?
            """, (data.status, ticket_id))

            updated = get_ticket(c, ticket_id)

            # Record an optional administrator note as a conversation message.
            if data.admin_note:
                c.execute("""
                    INSERT INTO service_ticket_messages(
                        ticket_id,
                        sender_id,
                        sender_role,
                        message
                    )
                    VALUES(?,?,'admin',?)
                """, (
                    ticket_id,
                    admin["id"],
                    f"Status update: {data.status.replace('_', ' ').title()}. "
                    f"{data.admin_note}"
                ))

        return {
            "success": True,
            "ticket_id": ticket_id,
            "status": data.status,
            "message": "Ticket status updated successfully."
        }
