
from fastapi import Depends, FastAPI, HTTPException


def register_agent_dashboard_routes(
    app: FastAPI,
    get_connection,
    get_current_student
):

    # ---------------------------------------------------------
    # ACCESS CONTROL
    # ---------------------------------------------------------

    def require_agent(student=Depends(get_current_student)):
        if student["role"] != "agent":
            raise HTTPException(
                status_code=403,
                detail="Only approved Agents can access this dashboard."
            )

        if student["account_status"] != "active":
            raise HTTPException(
                status_code=403,
                detail="Your account is not active."
            )

        return student

    # ---------------------------------------------------------
    # AGENT DASHBOARD OVERVIEW
    # ---------------------------------------------------------

    @app.get("/agents/me/dashboard-overview")
    def get_agent_dashboard_overview(
        agent=Depends(require_agent)
    ):

        agent_id = agent["id"]

        with get_connection() as c:

            # -------------------------------------------------
            # PROPERTY APPLICATION STATISTICS
            # -------------------------------------------------

            property_stats = c.execute("""
                SELECT
                    COUNT(*) AS total_properties,

                    SUM(
                        CASE
                            WHEN status='approved' THEN 1
                            ELSE 0
                        END
                    ) AS approved_properties,

                    SUM(
                        CASE
                            WHEN status='pending' THEN 1
                            ELSE 0
                        END
                    ) AS pending_properties,

                    SUM(
                        CASE
                            WHEN status='rejected' THEN 1
                            ELSE 0
                        END
                    ) AS rejected_properties

                FROM agent_property_applications
                WHERE agent_id=?
            """, (agent_id,)).fetchone()

            total_properties = property_stats["total_properties"] or 0
            approved_properties = property_stats["approved_properties"] or 0
            pending_properties = property_stats["pending_properties"] or 0
            rejected_properties = property_stats["rejected_properties"] or 0

            # -------------------------------------------------
            # STUDENT ENQUIRY STATISTICS
            # -------------------------------------------------

            enquiry_stats = c.execute("""
                SELECT
                    COUNT(*) AS total_enquiries,

                    SUM(
                        CASE
                            WHEN status='new' THEN 1
                            ELSE 0
                        END
                    ) AS new_enquiries,

                    SUM(
                        CASE
                            WHEN status='replied' THEN 1
                            ELSE 0
                        END
                    ) AS replied_enquiries,

                    SUM(
                        CASE
                            WHEN status='closed' THEN 1
                            ELSE 0
                        END
                    ) AS closed_enquiries

                FROM agent_enquiries
                WHERE agent_id=?
            """, (agent_id,)).fetchone()

            total_enquiries = enquiry_stats["total_enquiries"] or 0

            # -------------------------------------------------
            # ACTIVE ADVERTISING PROPERTIES
            # -------------------------------------------------

            active_advertising = c.execute("""
                SELECT
                    a.id AS authorization_id,
                    a.room_id,
                    a.requested_at,
                    r.location,
                    r.price,
                    r.room_type,
                    r.available,
                    r.created_at AS property_created_at

                FROM agent_room_authorizations a

                JOIN rooms r
                    ON r.id=a.room_id

                WHERE a.agent_id=?
                  AND a.status='approved'
                  AND a.advertising_enabled=1
                  AND r.available=1
                  AND r.ownership_status='assigned'
                  AND r.owner_id IS NOT NULL

                ORDER BY a.requested_at DESC, a.id DESC
            """, (agent_id,)).fetchall()

            active_advertising_properties = [
                dict(row) for row in active_advertising
            ]

            active_advertising_count = len(
                active_advertising_properties
            )

            # -------------------------------------------------
            # RECENT PROPERTY SUBMISSIONS
            # -------------------------------------------------

            recent_submissions = c.execute("""
                SELECT
                    id,
                    location,
                    room_type,
                    description,
                    price,
                    available,
                    status,
                    review_note,
                    created_at,
                    reviewed_at,
                    room_id

                FROM agent_property_applications

                WHERE agent_id=?

                ORDER BY created_at DESC, id DESC

                LIMIT 5
            """, (agent_id,)).fetchall()

            recent_submissions = [
                dict(row) for row in recent_submissions
            ]

            # -------------------------------------------------
            # RECENT STUDENT ENQUIRIES
            # -------------------------------------------------

            recent_enquiries = c.execute("""
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

                    r.location AS property_location

                FROM agent_enquiries e

                JOIN students s
                    ON s.id=e.student_id

                LEFT JOIN rooms r
                    ON r.id=e.room_id

                WHERE e.agent_id=?

                ORDER BY e.created_at DESC, e.id DESC

                LIMIT 5
            """, (agent_id,)).fetchall()

            recent_enquiries = [
                dict(row) for row in recent_enquiries
            ]

            # -------------------------------------------------
            # RECENT AGENT ACTIVITY
            # -------------------------------------------------

            activity = []

            # Property submissions made by this Agent.
            for item in recent_submissions:

                activity.append({
                    "type": "property_submission",
                    "title": "Property submitted",
                    "description": (
                        f"Submitted a property in {item['location']}."
                    ),
                    "status": item["status"],
                    "reference_id": item["id"],
                    "created_at": item["created_at"]
                })

            # Student enquiries received by this Agent.
            for item in recent_enquiries:

                activity.append({
                    "type": "student_enquiry",
                    "title": "Student enquiry received",
                    "description": (
                        f"{item['student_name']} sent you an enquiry."
                    ),
                    "status": item["status"],
                    "reference_id": item["id"],
                    "created_at": item["created_at"]
                })

                # A reply is a separate activity performed by the Agent.
                if item["reply_message"] and item["replied_at"]:

                    activity.append({
                        "type": "enquiry_reply",
                        "title": "Enquiry replied to",
                        "description": (
                            f"You replied to {item['student_name']}."
                        ),
                        "status": "replied",
                        "reference_id": item["id"],
                        "created_at": item["replied_at"]
                    })

            # Advertising requests submitted by this Agent.
            advertising_requests = c.execute("""
                SELECT
                    id,
                    room_id,
                    status,
                    requested_at

                FROM agent_room_authorizations

                WHERE agent_id=?

                ORDER BY requested_at DESC, id DESC

                LIMIT 10
            """, (agent_id,)).fetchall()

            for item in advertising_requests:

                activity.append({
                    "type": "advertising_request",
                    "title": "Advertising permission requested",
                    "description": (
                        f"Requested advertising permission "
                        f"for property #{item['room_id']}."
                    ),
                    "status": item["status"],
                    "reference_id": item["id"],
                    "created_at": item["requested_at"]
                })

            # Sort all activity by timestamp, newest first.
            activity.sort(
                key=lambda item: (
                    item["created_at"] or "",
                    item["reference_id"] or 0
                ),
                reverse=True
            )

            recent_activity = activity[:10]

        # -----------------------------------------------------
        # RESPONSE
        # -----------------------------------------------------

        return {
            "agent": {
                "id": agent_id,
                "full_name": agent["full_name"]
            },

            "statistics": {
                "total_properties": total_properties,
                "approved_properties": approved_properties,
                "pending_properties": pending_properties,
                "rejected_properties": rejected_properties,

                "total_enquiries": total_enquiries,
                "new_enquiries": enquiry_stats["new_enquiries"] or 0,
                "replied_enquiries": enquiry_stats["replied_enquiries"] or 0,
                "closed_enquiries": enquiry_stats["closed_enquiries"] or 0,

                "active_advertising_properties": active_advertising_count
            },

            "recent_enquiries": recent_enquiries,

            "recent_submissions": recent_submissions,

            "active_advertising": active_advertising_properties,

            "recent_activity": recent_activity
        }
