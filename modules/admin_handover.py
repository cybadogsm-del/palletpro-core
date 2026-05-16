from datetime import datetime, timedelta, timezone

from flask import jsonify, request

from audit import audit_event
from db import get_conn as _get_conn, make_id, now_iso


def ensure_admin_handover_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS admin_handovers (
        handover_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        outgoing_admin_user_id TEXT NOT NULL,
        incoming_admin_user_id TEXT NOT NULL,
        overlap_days INTEGER NOT NULL,
        initiated_at TEXT NOT NULL,
        overlap_ends_at TEXT NOT NULL,
        status TEXT NOT NULL,
        initiated_by_display_name TEXT NOT NULL,
        completed_at TEXT,
        cancelled_at TEXT,
        cancelled_by_display_name TEXT,
        notes TEXT
    )
    """)


def _calc_overlap_ends_at(overlap_days):
    return (datetime.now(timezone.utc) + timedelta(days=overlap_days)).strftime("%Y-%m-%dT%H:%M:%S")


def _get_active_handover(conn, organisation_id):
    return conn.execute(
        "SELECT * FROM admin_handovers WHERE organisation_id = ? AND status = 'ACTIVE'",
        (organisation_id,)
    ).fetchone()


def _get_user(conn, user_id, organisation_id):
    return conn.execute(
        "SELECT * FROM user_accounts WHERE user_id = ? AND organisation_id = ?",
        (user_id, organisation_id)
    ).fetchone()


def _set_user_role(conn, user_id, role, changed_by_display_name, organisation_id):
    conn.execute(
        "UPDATE user_accounts SET role = ?, updated_at = ? WHERE user_id = ?",
        (role, now_iso(), user_id)
    )
    conn.execute(
        """
        INSERT INTO user_access_events (
            user_access_event_id, organisation_id, user_id, action, summary,
            changed_by_display_name, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            make_id("uae"),
            organisation_id,
            user_id,
            "ROLE_CHANGE",
            f"Role changed to {role} by {changed_by_display_name}.",
            changed_by_display_name,
            now_iso(),
        )
    )


def register_admin_handover_routes(app):

    @app.post("/organisations/<organisation_id>/admin-handover")
    def initiate_admin_handover(organisation_id):
        body = request.get_json(silent=True) or {}
        incoming_admin_user_id = (body.get("incoming_admin_user_id") or "").strip()
        overlap_days = int(body.get("overlap_days") or 14)
        initiated_by_display_name = (body.get("initiated_by_display_name") or "Org Admin").strip()
        notes = (body.get("notes") or "").strip() or None

        if not incoming_admin_user_id:
            return jsonify({"error": "incoming_admin_user_id is required"}), 400

        if not (1 <= overlap_days <= 14):
            return jsonify({
                "error": "overlap_days must be between 1 and 14",
                "received": overlap_days,
            }), 400

        conn = _get_conn()
        ensure_admin_handover_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        if _get_active_handover(conn, organisation_id):
            conn.close()
            return jsonify({
                "error": "HANDOVER_ALREADY_ACTIVE",
                "dialog": {
                    "title": "Handover already in progress",
                    "message": (
                        "This organisation already has an active admin handover in progress. "
                        "Complete or cancel it before starting a new one."
                    ),
                    "primary_action": {
                        "label": "View Handover",
                        "route": f"/organisations/{organisation_id}/admin-handover",
                        "action_type": "NAVIGATE",
                    },
                },
            }), 409

        incoming_user = _get_user(conn, incoming_admin_user_id, organisation_id)
        if not incoming_user:
            conn.close()
            return jsonify({
                "error": "Incoming admin user not found in this organisation",
                "incoming_admin_user_id": incoming_admin_user_id,
            }), 404

        if incoming_user["role"] == "ORG_ADMIN":
            conn.close()
            return jsonify({"error": "This user is already an ORG_ADMIN"}), 400

        if incoming_user["access_status"] != "ACTIVE":
            conn.close()
            return jsonify({
                "error": "Incoming admin user must have ACTIVE status",
                "current_status": incoming_user["access_status"],
            }), 400

        outgoing_admin = conn.execute(
            """
            SELECT * FROM user_accounts
            WHERE organisation_id = ? AND role = 'ORG_ADMIN' AND access_status = 'ACTIVE'
            LIMIT 1
            """,
            (organisation_id,)
        ).fetchone()

        if not outgoing_admin:
            conn.close()
            return jsonify({"error": "No active ORG_ADMIN found for this organisation"}), 400

        handover_id = make_id("hdvr")
        initiated_at = now_iso()
        overlap_ends_at = _calc_overlap_ends_at(overlap_days)

        _set_user_role(conn, incoming_admin_user_id, "ORG_ADMIN", initiated_by_display_name, organisation_id)

        conn.execute(
            """
            INSERT INTO admin_handovers (
                handover_id, organisation_id, outgoing_admin_user_id, incoming_admin_user_id,
                overlap_days, initiated_at, overlap_ends_at, status,
                initiated_by_display_name, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
            """,
            (
                handover_id, organisation_id,
                outgoing_admin["user_id"], incoming_admin_user_id,
                overlap_days, initiated_at, overlap_ends_at,
                initiated_by_display_name, notes,
            )
        )

        audit_event(
            conn,
            entity_type="AdminHandover",
            entity_id=handover_id,
            action="INITIATE",
            summary=(
                f"Admin handover initiated in org {organisation_id}. "
                f"Outgoing: {outgoing_admin['display_name']}, "
                f"Incoming: {incoming_user['display_name']}. "
                f"Overlap: {overlap_days} day(s), ending {overlap_ends_at}."
            ),
        )

        conn.commit()
        conn.close()

        return jsonify({
            "handover_id": handover_id,
            "organisation_id": organisation_id,
            "outgoing_admin_user_id": outgoing_admin["user_id"],
            "outgoing_admin_display_name": outgoing_admin["display_name"],
            "incoming_admin_user_id": incoming_admin_user_id,
            "incoming_admin_display_name": incoming_user["display_name"],
            "overlap_days": overlap_days,
            "initiated_at": initiated_at,
            "overlap_ends_at": overlap_ends_at,
            "status": "ACTIVE",
            "message": (
                f"Handover initiated. {incoming_user['display_name']} is now also an ORG_ADMIN. "
                f"Both admins will co-exist for up to {overlap_days} day(s)."
            ),
        }), 201

    @app.get("/organisations/<organisation_id>/admin-handover")
    def get_admin_handover(organisation_id):
        conn = _get_conn()
        ensure_admin_handover_tables(conn)

        rows = conn.execute(
            """
            SELECT * FROM admin_handovers
            WHERE organisation_id = ?
            ORDER BY initiated_at DESC
            LIMIT 10
            """,
            (organisation_id,)
        ).fetchall()
        conn.close()

        items = [dict(r) for r in rows]
        active = next((i for i in items if i["status"] == "ACTIVE"), None)

        return jsonify({
            "organisation_id": organisation_id,
            "active_handover": active,
            "history": items,
        }), 200

    @app.post("/organisations/<organisation_id>/admin-handover/<handover_id>/complete")
    def complete_admin_handover(organisation_id, handover_id):
        body = request.get_json(silent=True) or {}
        completed_by = (body.get("completed_by_display_name") or "Org Admin").strip()

        conn = _get_conn()
        ensure_admin_handover_tables(conn)

        handover = conn.execute(
            "SELECT * FROM admin_handovers WHERE handover_id = ? AND organisation_id = ?",
            (handover_id, organisation_id)
        ).fetchone()

        if not handover:
            conn.close()
            return jsonify({"error": "Handover not found"}), 404

        if handover["status"] != "ACTIVE":
            conn.close()
            return jsonify({
                "error": "Handover is not active",
                "current_status": handover["status"],
            }), 400

        outgoing_user = conn.execute(
            "SELECT * FROM user_accounts WHERE user_id = ?",
            (handover["outgoing_admin_user_id"],)
        ).fetchone()

        _set_user_role(
            conn, handover["outgoing_admin_user_id"], "USER",
            completed_by, organisation_id
        )

        completed_at = now_iso()
        conn.execute(
            """
            UPDATE admin_handovers
            SET status = 'COMPLETED', completed_at = ?
            WHERE handover_id = ?
            """,
            (completed_at, handover_id)
        )

        audit_event(
            conn,
            entity_type="AdminHandover",
            entity_id=handover_id,
            action="COMPLETE",
            summary=(
                f"Admin handover completed in org {organisation_id}. "
                f"Outgoing admin {outgoing_user['display_name'] if outgoing_user else handover['outgoing_admin_user_id']} "
                f"demoted to USER by {completed_by}."
            ),
        )

        conn.commit()
        conn.close()

        return jsonify({
            "handover_id": handover_id,
            "status": "COMPLETED",
            "completed_at": completed_at,
            "completed_by_display_name": completed_by,
            "message": "Handover complete. Outgoing admin has been set to USER role.",
        }), 200

    @app.post("/organisations/<organisation_id>/admin-handover/<handover_id>/cancel")
    def cancel_admin_handover(organisation_id, handover_id):
        body = request.get_json(silent=True) or {}
        cancelled_by = (body.get("cancelled_by_display_name") or "Org Admin").strip()

        conn = _get_conn()
        ensure_admin_handover_tables(conn)

        handover = conn.execute(
            "SELECT * FROM admin_handovers WHERE handover_id = ? AND organisation_id = ?",
            (handover_id, organisation_id)
        ).fetchone()

        if not handover:
            conn.close()
            return jsonify({"error": "Handover not found"}), 404

        if handover["status"] != "ACTIVE":
            conn.close()
            return jsonify({
                "error": "Handover is not active",
                "current_status": handover["status"],
            }), 400

        incoming_user = conn.execute(
            "SELECT * FROM user_accounts WHERE user_id = ?",
            (handover["incoming_admin_user_id"],)
        ).fetchone()

        _set_user_role(
            conn, handover["incoming_admin_user_id"], "USER",
            cancelled_by, organisation_id
        )

        cancelled_at = now_iso()
        conn.execute(
            """
            UPDATE admin_handovers
            SET status = 'CANCELLED', cancelled_at = ?, cancelled_by_display_name = ?
            WHERE handover_id = ?
            """,
            (cancelled_at, cancelled_by, handover_id)
        )

        audit_event(
            conn,
            entity_type="AdminHandover",
            entity_id=handover_id,
            action="CANCEL",
            summary=(
                f"Admin handover cancelled in org {organisation_id}. "
                f"Incoming admin {incoming_user['display_name'] if incoming_user else handover['incoming_admin_user_id']} "
                f"reverted to USER by {cancelled_by}."
            ),
        )

        conn.commit()
        conn.close()

        return jsonify({
            "handover_id": handover_id,
            "status": "CANCELLED",
            "cancelled_at": cancelled_at,
            "cancelled_by_display_name": cancelled_by,
            "message": "Handover cancelled. Incoming admin has been reverted to USER role.",
        }), 200

    @app.post("/global-admin/organisations/<organisation_id>/appoint-admin")
    def appoint_admin_emergency(organisation_id):
        body = request.get_json(silent=True) or {}
        user_id = (body.get("user_id") or "").strip()
        appointed_by = (body.get("appointed_by_display_name") or "Global Admin").strip()
        notes = (body.get("notes") or "").strip() or None

        if not user_id:
            return jsonify({"error": "user_id is required"}), 400

        conn = _get_conn()
        ensure_admin_handover_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        user = conn.execute(
            "SELECT * FROM user_accounts WHERE user_id = ? AND organisation_id = ?",
            (user_id, organisation_id)
        ).fetchone()
        if not user:
            conn.close()
            return jsonify({"error": "User not found in this organisation"}), 404

        if user["role"] == "ORG_ADMIN":
            conn.close()
            return jsonify({"error": "User is already an ORG_ADMIN"}), 400

        _set_user_role(conn, user_id, "ORG_ADMIN", appointed_by, organisation_id)

        audit_event(
            conn,
            entity_type="UserAccount",
            entity_id=user_id,
            action="EMERGENCY_APPOINT_ADMIN",
            summary=(
                f"Global Admin appointed {user['display_name']} as ORG_ADMIN "
                f"for org {organisation_id}. Appointed by {appointed_by}."
                + (f" Notes: {notes}" if notes else "")
            ),
        )

        conn.commit()
        conn.close()

        return jsonify({
            "user_id": user_id,
            "display_name": user["display_name"],
            "organisation_id": organisation_id,
            "new_role": "ORG_ADMIN",
            "appointed_by_display_name": appointed_by,
            "notes": notes,
            "message": f"{user['display_name']} has been appointed as ORG_ADMIN.",
        }), 200

    @app.post("/global-admin/admin-handover-expiry-sweep")
    def admin_handover_expiry_sweep():
        conn = _get_conn()
        ensure_admin_handover_tables(conn)

        now = now_iso()
        expired = conn.execute(
            """
            SELECT * FROM admin_handovers
            WHERE status = 'ACTIVE' AND overlap_ends_at <= ?
            """,
            (now,)
        ).fetchall()

        completed_ids = []
        for handover in expired:
            outgoing_user = conn.execute(
                "SELECT * FROM user_accounts WHERE user_id = ?",
                (handover["outgoing_admin_user_id"],)
            ).fetchone()

            if outgoing_user and outgoing_user["role"] == "ORG_ADMIN":
                _set_user_role(
                    conn, handover["outgoing_admin_user_id"], "USER",
                    "System (handover expiry sweep)", handover["organisation_id"]
                )

            conn.execute(
                "UPDATE admin_handovers SET status = 'COMPLETED', completed_at = ? WHERE handover_id = ?",
                (now, handover["handover_id"])
            )

            audit_event(
                conn,
                entity_type="AdminHandover",
                entity_id=handover["handover_id"],
                action="AUTO_COMPLETE",
                summary=(
                    f"Handover auto-completed by expiry sweep for org {handover['organisation_id']}. "
                    f"Outgoing admin {outgoing_user['display_name'] if outgoing_user else handover['outgoing_admin_user_id']} "
                    f"demoted to USER after {handover['overlap_days']}-day overlap period."
                ),
            )

            completed_ids.append(handover["handover_id"])

        conn.commit()
        conn.close()

        return jsonify({
            "sweep_type": "ADMIN_HANDOVER_EXPIRY_SWEEP",
            "swept_at": now,
            "completed_count": len(completed_ids),
            "completed_handover_ids": completed_ids,
        }), 200
