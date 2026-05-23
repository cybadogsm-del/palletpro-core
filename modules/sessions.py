"""
modules/sessions.py — Sessions & Login-Integrity brick

Covers:
  - Schema helpers: ensure_login_integrity_tables,
    ensure_login_integrity_review_tables,
    ensure_login_integrity_action_tables
  - Core helpers: create_login_integrity_event,
    build_login_integrity_ai_report
  - Routes:
      POST /sessions/login
      POST /sessions/<id>/logout
      GET  /users/<id>/sessions
      POST /sessions/<id>/heartbeat
      POST /global-admin/session-expiry-sweep
      POST /global-admin/temporary-user-expiry-sweep
      GET  /global-admin/login-integrity-reports
      POST /global-admin/login-integrity-reports/<id>/review
      GET  /global-admin/login-integrity-dashboard
      POST /global-admin/login-integrity-reports/<id>/review-v2
      GET  /global-admin/login-integrity-reports/<id>/review-history
      POST /global-admin/login-integrity-reports/<id>/actions
      GET  /global-admin/login-integrity-reports/<id>/actions
      POST /global-admin/login-integrity-actions/<id>/complete
      GET  /global-admin/login-integrity-actions
      GET  /global-admin/login-integrity-actions-summary
"""

import json
from datetime import datetime, timedelta

from flask import jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso
from modules.subscription_access import ensure_subscription_guard_tables
from modules.users import (
    build_user_access_policy,
    ensure_user_access_tables,
    get_user_account,
    record_user_access_event,
)


# ── Constants ──────────────────────────────────────────────────────────────────

ONE_DEVICE_ROLES = {
    "USER",
    "TEMPORARY_USER",
}

MULTI_DEVICE_ALLOWED_ROLES = {
    "ORG_ADMIN",
    "GLOBAL_ADMIN",
    "SUPER_GLOBAL_ADMIN",
}


# ── Exported schema helpers ────────────────────────────────────────────────────

def ensure_login_integrity_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_sessions (
        session_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        organisation_id TEXT,
        role TEXT NOT NULL,
        device_id TEXT NOT NULL,
        device_label TEXT,
        ip_address_hash TEXT,
        user_agent_hash TEXT,
        session_status TEXT NOT NULL,
        login_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        logout_at TEXT,
        ended_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS login_integrity_events (
        login_integrity_event_id TEXT PRIMARY KEY,
        organisation_id TEXT,
        user_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        risk_level TEXT NOT NULL,
        risk_score INTEGER NOT NULL,
        summary TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        ai_report_summary TEXT NOT NULL,
        review_status TEXT NOT NULL,
        global_admin_only INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)


def ensure_login_integrity_review_tables(conn):
    ensure_login_integrity_tables(conn)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS login_integrity_review_history (
        login_integrity_review_id TEXT PRIMARY KEY,
        login_integrity_event_id TEXT NOT NULL,
        organisation_id TEXT,
        user_id TEXT NOT NULL,
        previous_review_status TEXT,
        new_review_status TEXT NOT NULL,
        reviewed_by_display_name TEXT NOT NULL,
        review_notes TEXT,
        created_at TEXT NOT NULL
    )
    """)


def ensure_login_integrity_action_tables(conn):
    ensure_login_integrity_review_tables(conn)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS login_integrity_admin_actions (
        login_integrity_action_id TEXT PRIMARY KEY,
        login_integrity_event_id TEXT NOT NULL,
        organisation_id TEXT,
        user_id TEXT NOT NULL,
        action_type TEXT NOT NULL,
        action_status TEXT NOT NULL,
        assigned_to_display_name TEXT,
        action_notes TEXT,
        due_at TEXT,
        completed_at TEXT,
        created_by_display_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)


# ── Internal helpers ───────────────────────────────────────────────────────────

def create_login_integrity_event(
    conn,
    organisation_id,
    user_id,
    event_type,
    risk_level,
    risk_score,
    summary,
    evidence,
    ai_report_summary,
):
    ensure_login_integrity_tables(conn)
    ts = now_iso()

    event_id = make_id("lie")

    conn.execute(
        """
        INSERT INTO login_integrity_events (
            login_integrity_event_id,
            organisation_id,
            user_id,
            event_type,
            risk_level,
            risk_score,
            summary,
            evidence_json,
            ai_report_summary,
            review_status,
            global_admin_only,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            organisation_id,
            user_id,
            event_type,
            risk_level,
            risk_score,
            summary,
            json.dumps(evidence, sort_keys=True),
            ai_report_summary,
            "OPEN",
            1,
            ts,
            ts,
        )
    )

    return event_id


def build_login_integrity_ai_report(event_type, role, active_session_count, ended_session_count, device_id, device_label):
    if event_type == "ONE_DEVICE_RULE_PREVIOUS_SESSION_ENDED":
        return (
            "Login Integrity Guard detected a standard/temporary user logging in from a new device while another "
            f"active session existed. The previous session was ended automatically to enforce the one-user-one-device rule. "
            f"Role: {role}. New device: {device_label or device_id}. "
            f"Sessions ended: {ended_session_count}. Sessions before login: {active_session_count}. "
            "This is a medium-risk event. If this activity is unexpected, Global Admin should investigate."
        )
    if event_type == "ADMIN_MULTI_DEVICE_ACTIVITY":
        return (
            f"Login Integrity Guard detected an admin-role user logging in while {active_session_count} other active "
            f"session(s) already existed. Multi-device access is permitted for admin roles but is logged for monitoring. "
            f"Role: {role}. New device: {device_label or device_id}. "
            "This is a low-risk event. No action is required unless the activity appears unusual."
        )
    return (
        f"Login Integrity Guard recorded an event of type {event_type}. "
        f"Role: {role}. Device: {device_label or device_id}. "
        "Review the evidence and decide whether any action is required."
    )


# ── Route registration ─────────────────────────────────────────────────────────

def register_session_routes(app, is_rate_limited):

    @app.post("/sessions/login")
    def create_user_session():
        if is_rate_limited():
            return jsonify({"error": "TOO_MANY_REQUESTS", "message": "Too many login attempts. Wait a minute and try again."}), 429

        body = request.get_json(silent=True) or {}

        mobile_number = (body.get("mobile_number") or "").strip() or None
        email = (body.get("email") or "").strip() or None
        password = (body.get("password") or "").strip()
        device_id = (body.get("device_id") or "").strip()
        device_label = (body.get("device_label") or "").strip() or None
        ip_address_hash = (body.get("ip_address_hash") or "").strip() or None
        user_agent_hash = (body.get("user_agent_hash") or "").strip() or None

        if not mobile_number and not email:
            return jsonify({"error": "mobile_number or email is required"}), 400
        if not password:
            return jsonify({"error": "password is required"}), 400
        if not device_id:
            return jsonify({"error": "device_id is required"}), 400

        conn = get_conn()
        ensure_user_access_tables(conn)
        ensure_login_integrity_tables(conn)

        from modules.password_auth import ensure_password_auth_columns, verify_password
        ensure_password_auth_columns(conn)

        user = None
        if mobile_number:
            user = conn.execute(
                "SELECT * FROM user_accounts WHERE mobile_number = ?", (mobile_number,)
            ).fetchone()
        if not user and email:
            user = conn.execute(
                "SELECT * FROM user_accounts WHERE email = ?", (email,)
            ).fetchone()

        # Use a constant-time response to avoid leaking whether the account exists
        password_ok = verify_password(password, user["password_hash"] if user else None)

        if not user or not password_ok:
            conn.close()
            return jsonify({"error": "Invalid credentials"}), 401

        user_id = user["user_id"]

        policy = build_user_access_policy(conn, user)

        if not policy["can_use_platform"]:
            conn.close()
            return jsonify({
                "error": "User cannot log in",
                "access_policy": policy,
            }), 403

        ts = now_iso()
        role = user["role"]
        organisation_id = user["organisation_id"]

        existing_active_sessions = conn.execute(
            """
            SELECT *
            FROM user_sessions
            WHERE user_id = ?
              AND session_status = 'ACTIVE'
            ORDER BY login_at ASC
            """,
            (user_id,)
        ).fetchall()

        ended_sessions = []

        if role in ONE_DEVICE_ROLES:
            for session in existing_active_sessions:
                if session["device_id"] != device_id:
                    conn.execute(
                        """
                        UPDATE user_sessions
                        SET session_status = ?,
                            logout_at = ?,
                            ended_reason = ?,
                            updated_at = ?
                        WHERE session_id = ?
                        """,
                        (
                            "ENDED",
                            ts,
                            "ONE_DEVICE_RULE_NEW_LOGIN",
                            ts,
                            session["session_id"],
                        )
                    )
                    ended_sessions.append(dict(session))

            if ended_sessions:
                create_login_integrity_event(
                    conn=conn,
                    organisation_id=organisation_id,
                    user_id=user_id,
                    event_type="ONE_DEVICE_RULE_PREVIOUS_SESSION_ENDED",
                    risk_level="MEDIUM",
                    risk_score=55,
                    summary="One-user-one-device rule ended previous active session for standard/temporary user.",
                    evidence={
                        "role": role,
                        "new_device_id": device_id,
                        "new_device_label": device_label,
                        "ended_session_ids": [s["session_id"] for s in ended_sessions],
                        "ended_device_ids": [s["device_id"] for s in ended_sessions],
                        "active_session_count_before_login": len(existing_active_sessions),
                    },
                    ai_report_summary=build_login_integrity_ai_report(
                        "ONE_DEVICE_RULE_PREVIOUS_SESSION_ENDED",
                        role,
                        len(existing_active_sessions),
                        len(ended_sessions),
                        device_id,
                        device_label,
                    ),
                )

        elif role in MULTI_DEVICE_ALLOWED_ROLES and len(existing_active_sessions) >= 1:
            create_login_integrity_event(
                conn=conn,
                organisation_id=organisation_id,
                user_id=user_id,
                event_type="ADMIN_MULTI_DEVICE_ACTIVITY",
                risk_level="LOW",
                risk_score=20,
                summary="Admin user logged in while another active session already existed.",
                evidence={
                    "role": role,
                    "new_device_id": device_id,
                    "new_device_label": device_label,
                    "active_session_count_before_login": len(existing_active_sessions),
                    "existing_session_ids": [s["session_id"] for s in existing_active_sessions],
                },
                ai_report_summary=build_login_integrity_ai_report(
                    "ADMIN_MULTI_DEVICE_ACTIVITY",
                    role,
                    len(existing_active_sessions),
                    0,
                    device_id,
                    device_label,
                ),
            )

        # Reuse same active session on same device when possible.
        same_device_session = conn.execute(
            """
            SELECT *
            FROM user_sessions
            WHERE user_id = ?
              AND device_id = ?
              AND session_status = 'ACTIVE'
            ORDER BY login_at DESC
            LIMIT 1
            """,
            (user_id, device_id)
        ).fetchone()

        if same_device_session:
            conn.execute(
                """
                UPDATE user_sessions
                SET last_seen_at = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (ts, ts, same_device_session["session_id"])
            )

            session_id = same_device_session["session_id"]
        else:
            session_id = make_id("sess")

            conn.execute(
                """
                INSERT INTO user_sessions (
                    session_id,
                    user_id,
                    organisation_id,
                    role,
                    device_id,
                    device_label,
                    ip_address_hash,
                    user_agent_hash,
                    session_status,
                    login_at,
                    last_seen_at,
                    logout_at,
                    ended_reason,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    organisation_id,
                    role,
                    device_id,
                    device_label,
                    ip_address_hash,
                    user_agent_hash,
                    "ACTIVE",
                    ts,
                    ts,
                    None,
                    None,
                    ts,
                    ts,
                )
            )

        conn.commit()

        session = conn.execute(
            "SELECT * FROM user_sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()

        active_sessions_now = conn.execute(
            """
            SELECT *
            FROM user_sessions
            WHERE user_id = ?
              AND session_status = 'ACTIVE'
            ORDER BY login_at DESC
            """,
            (user_id,)
        ).fetchall()

        conn.close()

        return jsonify({
            "session": dict(session),
            "user": dict(user),
            "access_policy": policy,
            "one_device_rule_applies": role in ONE_DEVICE_ROLES,
            "multi_device_allowed": role in MULTI_DEVICE_ALLOWED_ROLES,
            "ended_previous_session_count": len(ended_sessions),
            "active_session_count": len(active_sessions_now),
            "rule": "Standard and temporary users are limited to one active device. Org/Admin roles may use multiple devices but are monitored.",
        }), 201


    @app.post("/sessions/<session_id>/logout")
    def logout_user_session(session_id):
        body = request.get_json(silent=True) or {}
        ended_reason = (body.get("ended_reason") or "USER_LOGOUT").strip()

        conn = get_conn()
        ensure_login_integrity_tables(conn)

        session = conn.execute(
            "SELECT * FROM user_sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()

        if not session:
            conn.close()
            return jsonify({"error": "Session not found"}), 404

        ts = now_iso()

        conn.execute(
            """
            UPDATE user_sessions
            SET session_status = ?,
                logout_at = ?,
                ended_reason = ?,
                updated_at = ?
            WHERE session_id = ?
            """,
            (
                "ENDED",
                ts,
                ended_reason,
                ts,
                session_id,
            )
        )

        conn.commit()

        updated = conn.execute(
            "SELECT * FROM user_sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()

        conn.close()

        return jsonify({
            "session": dict(updated),
            "message": "Session ended.",
        }), 200


    @app.get("/users/<user_id>/sessions")
    def list_user_sessions(user_id):
        conn = get_conn()
        ensure_login_integrity_tables(conn)
        ensure_user_access_tables(conn)

        user = get_user_account(conn, user_id)

        if not user:
            conn.close()
            return jsonify({"error": "User not found"}), 404

        rows = conn.execute(
            """
            SELECT *
            FROM user_sessions
            WHERE user_id = ?
            ORDER BY login_at DESC
            """,
            (user_id,)
        ).fetchall()

        conn.close()

        return jsonify({
            "user": dict(user),
            "count": len(rows),
            "items": [dict(row) for row in rows],
        }), 200


    @app.get("/global-admin/login-integrity-reports")
    def list_login_integrity_reports():
        organisation_id = request.args.get("organisation_id")
        user_id = request.args.get("user_id")
        review_status = request.args.get("review_status")
        risk_level = request.args.get("risk_level")

        conn = get_conn()
        ensure_login_integrity_tables(conn)

        sql = """
            SELECT
                e.*,
                u.display_name,
                u.email,
                u.role,
                o.name AS organisation_name
            FROM login_integrity_events e
            LEFT JOIN user_accounts u ON u.user_id = e.user_id
            LEFT JOIN organisations o ON o.organisation_id = e.organisation_id
            WHERE e.global_admin_only = 1
        """
        params = []

        if organisation_id:
            sql += " AND e.organisation_id = ?"
            params.append(organisation_id)

        if user_id:
            sql += " AND e.user_id = ?"
            params.append(user_id)

        if review_status:
            sql += " AND e.review_status = ?"
            params.append(review_status)

        if risk_level:
            sql += " AND e.risk_level = ?"
            params.append(risk_level)

        sql += " ORDER BY e.created_at DESC"

        rows = conn.execute(sql, params).fetchall()

        conn.close()

        return jsonify({
            "report_type": "GLOBAL_ADMIN_LOGIN_INTEGRITY_REPORTS",
            "count": len(rows),
            "items": [dict(row) for row in rows],
            "rule": "Login integrity reports are for Global Admin eyes only. AI reports are advisory. Global Admin decides any course of action.",
        }), 200


    @app.post("/global-admin/login-integrity-reports/<login_integrity_event_id>/review")
    def review_login_integrity_report(login_integrity_event_id):
        body = request.get_json(silent=True) or {}
        review_status = (body.get("review_status") or "").strip().upper()
        reviewed_by_display_name = (body.get("reviewed_by_display_name") or "Global Admin").strip()
        review_notes = (body.get("review_notes") or "").strip() or None

        allowed_statuses = {"OPEN", "MONITORING", "DISMISSED", "ACTION_REQUIRED", "RESOLVED"}

        if review_status not in allowed_statuses:
            return jsonify({
                "error": "Invalid review_status",
                "allowed_statuses": sorted(allowed_statuses),
            }), 400

        conn = get_conn()
        ensure_login_integrity_tables(conn)

        event = conn.execute(
            """
            SELECT *
            FROM login_integrity_events
            WHERE login_integrity_event_id = ?
            """,
            (login_integrity_event_id,)
        ).fetchone()

        if not event:
            conn.close()
            return jsonify({"error": "Login integrity report not found"}), 404

        ts = now_iso()

        summary_suffix = f" Reviewed by {reviewed_by_display_name}."
        if review_notes:
            summary_suffix += f" Notes: {review_notes}"

        conn.execute(
            """
            UPDATE login_integrity_events
            SET review_status = ?,
                summary = summary || ?,
                updated_at = ?
            WHERE login_integrity_event_id = ?
            """,
            (
                review_status,
                summary_suffix,
                ts,
                login_integrity_event_id,
            )
        )

        conn.commit()

        updated = conn.execute(
            """
            SELECT *
            FROM login_integrity_events
            WHERE login_integrity_event_id = ?
            """,
            (login_integrity_event_id,)
        ).fetchone()

        conn.close()

        return jsonify({
            "login_integrity_report": dict(updated),
            "reviewed_by_display_name": reviewed_by_display_name,
            "review_notes": review_notes,
            "rule": "Global Admin reviews and decides the course of action. AI does not automatically penalise users or organisations.",
        }), 200


    @app.get("/global-admin/login-integrity-dashboard")
    def get_login_integrity_dashboard():
        conn = get_conn()
        ensure_login_integrity_tables(conn)
        ensure_user_access_tables(conn)

        reports = conn.execute(
            """
            SELECT
                e.*,
                u.display_name,
                u.email,
                u.role,
                o.name AS organisation_name
            FROM login_integrity_events e
            LEFT JOIN user_accounts u ON u.user_id = e.user_id
            LEFT JOIN organisations o ON o.organisation_id = e.organisation_id
            WHERE e.global_admin_only = 1
            ORDER BY e.created_at DESC
            LIMIT 50
            """
        ).fetchall()

        active_sessions = conn.execute(
            """
            SELECT
                s.*,
                u.display_name,
                u.email,
                o.name AS organisation_name
            FROM user_sessions s
            LEFT JOIN user_accounts u ON u.user_id = s.user_id
            LEFT JOIN organisations o ON o.organisation_id = s.organisation_id
            WHERE s.session_status = 'ACTIVE'
            ORDER BY s.last_seen_at DESC
            LIMIT 100
            """
        ).fetchall()

        risk_counts = {}
        review_counts = {}
        event_type_counts = {}

        for row in reports:
            risk_counts[row["risk_level"]] = risk_counts.get(row["risk_level"], 0) + 1
            review_counts[row["review_status"]] = review_counts.get(row["review_status"], 0) + 1
            event_type_counts[row["event_type"]] = event_type_counts.get(row["event_type"], 0) + 1

        one_device_roles_active_sessions = [
            dict(row) for row in active_sessions
            if row["role"] in ("USER", "TEMPORARY_USER")
        ]

        admin_active_sessions = [
            dict(row) for row in active_sessions
            if row["role"] in ("ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN")
        ]

        conn.close()

        return jsonify({
            "dashboard_type": "GLOBAL_ADMIN_LOGIN_INTEGRITY_DASHBOARD",
            "report_count": len(reports),
            "active_session_count": len(active_sessions),
            "standard_user_active_session_count": len(one_device_roles_active_sessions),
            "admin_active_session_count": len(admin_active_sessions),
            "risk_counts": risk_counts,
            "review_status_counts": review_counts,
            "event_type_counts": event_type_counts,
            "recent_reports": [dict(row) for row in reports],
            "active_sessions": [dict(row) for row in active_sessions],
            "rules": [
                "Login integrity reports are for Global Admin eyes only.",
                "AI reports are advisory only. Global Admin decides any course of action.",
                "Standard and temporary users are limited to one active device.",
                "Org Admins and platform admins may use multiple devices, but their sessions remain logged and monitored.",
                "The purpose is to protect who/where/when integrity and the Pallet Pro pricing philosophy.",
            ],
        }), 200


    @app.post("/global-admin/login-integrity-reports/<login_integrity_event_id>/review-v2")
    def review_login_integrity_report_v2(login_integrity_event_id):
        body = request.get_json(silent=True) or {}
        review_status = (body.get("review_status") or "").strip().upper()
        reviewed_by_display_name = (body.get("reviewed_by_display_name") or "Global Admin").strip()
        review_notes = (body.get("review_notes") or "").strip() or None

        allowed_statuses = {"OPEN", "MONITORING", "DISMISSED", "ACTION_REQUIRED", "RESOLVED"}

        if review_status not in allowed_statuses:
            return jsonify({
                "error": "Invalid review_status",
                "allowed_statuses": sorted(allowed_statuses),
            }), 400

        conn = get_conn()
        ensure_login_integrity_review_tables(conn)

        event = conn.execute(
            """
            SELECT *
            FROM login_integrity_events
            WHERE login_integrity_event_id = ?
            """,
            (login_integrity_event_id,)
        ).fetchone()

        if not event:
            conn.close()
            return jsonify({"error": "Login integrity report not found"}), 404

        previous_status = event["review_status"]
        ts = now_iso()

        conn.execute(
            """
            INSERT INTO login_integrity_review_history (
                login_integrity_review_id,
                login_integrity_event_id,
                organisation_id,
                user_id,
                previous_review_status,
                new_review_status,
                reviewed_by_display_name,
                review_notes,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("lirh"),
                login_integrity_event_id,
                event["organisation_id"],
                event["user_id"],
                previous_status,
                review_status,
                reviewed_by_display_name,
                review_notes,
                ts,
            )
        )

        summary_suffix = f" Review status changed from {previous_status} to {review_status} by {reviewed_by_display_name}."
        if review_notes:
            summary_suffix += f" Notes: {review_notes}"

        conn.execute(
            """
            UPDATE login_integrity_events
            SET review_status = ?,
                summary = summary || ?,
                updated_at = ?
            WHERE login_integrity_event_id = ?
            """,
            (
                review_status,
                summary_suffix,
                ts,
                login_integrity_event_id,
            )
        )

        conn.commit()

        updated = conn.execute(
            """
            SELECT *
            FROM login_integrity_events
            WHERE login_integrity_event_id = ?
            """,
            (login_integrity_event_id,)
        ).fetchone()

        history = conn.execute(
            """
            SELECT *
            FROM login_integrity_review_history
            WHERE login_integrity_event_id = ?
            ORDER BY created_at ASC
            """,
            (login_integrity_event_id,)
        ).fetchall()

        conn.close()

        return jsonify({
            "login_integrity_report": dict(updated),
            "review_history_count": len(history),
            "review_history": [dict(row) for row in history],
            "rule": "Global Admin review decisions are stored as review history. AI reports are advisory only.",
        }), 200


    @app.get("/global-admin/login-integrity-reports/<login_integrity_event_id>/review-history")
    def get_login_integrity_review_history(login_integrity_event_id):
        conn = get_conn()
        ensure_login_integrity_review_tables(conn)

        event = conn.execute(
            """
            SELECT *
            FROM login_integrity_events
            WHERE login_integrity_event_id = ?
            """,
            (login_integrity_event_id,)
        ).fetchone()

        if not event:
            conn.close()
            return jsonify({"error": "Login integrity report not found"}), 404

        history = conn.execute(
            """
            SELECT *
            FROM login_integrity_review_history
            WHERE login_integrity_event_id = ?
            ORDER BY created_at ASC
            """,
            (login_integrity_event_id,)
        ).fetchall()

        conn.close()

        return jsonify({
            "login_integrity_event_id": login_integrity_event_id,
            "current_review_status": event["review_status"],
            "review_history_count": len(history),
            "review_history": [dict(row) for row in history],
        }), 200


    @app.post("/global-admin/login-integrity-reports/<login_integrity_event_id>/actions")
    def create_login_integrity_admin_action(login_integrity_event_id):
        body = request.get_json(silent=True) or {}

        action_type = (body.get("action_type") or "").strip().upper()
        assigned_to_display_name = (body.get("assigned_to_display_name") or "").strip() or None
        action_notes = (body.get("action_notes") or "").strip() or None
        due_at = body.get("due_at")
        created_by_display_name = (body.get("created_by_display_name") or "Global Admin").strip()
        confirmation_text = (body.get("confirmation_text") or "").strip()

        required_confirmation = "RECORD LOGIN INTEGRITY ACTION"

        allowed_action_types = {
            "MONITOR_ONLY",
            "CONTACT_ORG_ADMIN",
            "REQUIRE_SEPARATE_LOGINS",
            "REVIEW_WITH_CUSTOMER",
            "MARK_FALSE_POSITIVE",
            "ESCALATE_TO_SUPER_GLOBAL_ADMIN",
        }

        if confirmation_text != required_confirmation:
            return jsonify({
                "error": "Confirmation text is required before recording a login integrity action",
                "required_confirmation_text": required_confirmation,
                "received_confirmation_text": confirmation_text,
                "rule": "Global Admin decides and records the course of action. AI does not automatically penalise users or organisations.",
            }), 400

        if action_type not in allowed_action_types:
            return jsonify({
                "error": "Invalid action_type",
                "allowed_action_types": sorted(allowed_action_types),
            }), 400

        conn = get_conn()
        ensure_login_integrity_action_tables(conn)

        event = conn.execute(
            """
            SELECT *
            FROM login_integrity_events
            WHERE login_integrity_event_id = ?
            """,
            (login_integrity_event_id,)
        ).fetchone()

        if not event:
            conn.close()
            return jsonify({"error": "Login integrity report not found"}), 404

        ts = now_iso()
        action_id = make_id("lia")

        conn.execute(
            """
            INSERT INTO login_integrity_admin_actions (
                login_integrity_action_id,
                login_integrity_event_id,
                organisation_id,
                user_id,
                action_type,
                action_status,
                assigned_to_display_name,
                action_notes,
                due_at,
                completed_at,
                created_by_display_name,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                login_integrity_event_id,
                event["organisation_id"],
                event["user_id"],
                action_type,
                "OPEN",
                assigned_to_display_name,
                action_notes,
                due_at,
                None,
                created_by_display_name,
                ts,
                ts,
            )
        )

        conn.execute(
            """
            UPDATE login_integrity_events
            SET review_status = ?,
                summary = summary || ?,
                updated_at = ?
            WHERE login_integrity_event_id = ?
            """,
            (
                "ACTION_REQUIRED" if action_type not in ("MONITOR_ONLY", "MARK_FALSE_POSITIVE") else "MONITORING",
                f" Global Admin action recorded: {action_type}.",
                ts,
                login_integrity_event_id,
            )
        )

        audit_event(
            conn,
            entity_type="LoginIntegrityEvent",
            entity_id=login_integrity_event_id,
            action="RECORD_ADMIN_ACTION",
            summary=f"Global Admin recorded login integrity action: {action_type}.",
            organisation_id=event["organisation_id"],
        )

        conn.commit()

        action = conn.execute(
            """
            SELECT *
            FROM login_integrity_admin_actions
            WHERE login_integrity_action_id = ?
            """,
            (action_id,)
        ).fetchone()

        updated_event = conn.execute(
            """
            SELECT *
            FROM login_integrity_events
            WHERE login_integrity_event_id = ?
            """,
            (login_integrity_event_id,)
        ).fetchone()

        conn.close()

        return jsonify({
            "login_integrity_action": dict(action),
            "login_integrity_report": dict(updated_event),
            "rule": "AI reports are advisory only. Global Admin records and decides the course of action.",
        }), 201


    @app.get("/global-admin/login-integrity-reports/<login_integrity_event_id>/actions")
    def list_login_integrity_admin_actions(login_integrity_event_id):
        conn = get_conn()
        ensure_login_integrity_action_tables(conn)

        event = conn.execute(
            """
            SELECT *
            FROM login_integrity_events
            WHERE login_integrity_event_id = ?
            """,
            (login_integrity_event_id,)
        ).fetchone()

        if not event:
            conn.close()
            return jsonify({"error": "Login integrity report not found"}), 404

        rows = conn.execute(
            """
            SELECT *
            FROM login_integrity_admin_actions
            WHERE login_integrity_event_id = ?
            ORDER BY created_at ASC
            """,
            (login_integrity_event_id,)
        ).fetchall()

        conn.close()

        return jsonify({
            "login_integrity_event_id": login_integrity_event_id,
            "action_count": len(rows),
            "items": [dict(row) for row in rows],
        }), 200


    @app.post("/global-admin/login-integrity-actions/<login_integrity_action_id>/complete")
    def complete_login_integrity_admin_action(login_integrity_action_id):
        body = request.get_json(silent=True) or {}

        completed_by_display_name = (body.get("completed_by_display_name") or "Global Admin").strip()
        completion_notes = (body.get("completion_notes") or "").strip() or None
        confirmation_text = (body.get("confirmation_text") or "").strip()

        required_confirmation = "COMPLETE LOGIN INTEGRITY ACTION"

        if confirmation_text != required_confirmation:
            return jsonify({
                "error": "Confirmation text is required before completing a login integrity action",
                "required_confirmation_text": required_confirmation,
                "received_confirmation_text": confirmation_text,
                "rule": "Global Admin must deliberately complete recorded login integrity actions.",
            }), 400

        conn = get_conn()
        ensure_login_integrity_action_tables(conn)

        action = conn.execute(
            """
            SELECT *
            FROM login_integrity_admin_actions
            WHERE login_integrity_action_id = ?
            """,
            (login_integrity_action_id,)
        ).fetchone()

        if not action:
            conn.close()
            return jsonify({"error": "Login integrity action not found"}), 404

        ts = now_iso()

        conn.execute(
            """
            UPDATE login_integrity_admin_actions
            SET action_status = ?,
                completed_at = ?,
                action_notes = COALESCE(action_notes, '') || ?,
                updated_at = ?
            WHERE login_integrity_action_id = ?
            """,
            (
                "COMPLETED",
                ts,
                f" Completion by {completed_by_display_name}: {completion_notes or 'No notes supplied.'}",
                ts,
                login_integrity_action_id,
            )
        )

        open_actions = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM login_integrity_admin_actions
            WHERE login_integrity_event_id = ?
              AND action_status != 'COMPLETED'
            """,
            (action["login_integrity_event_id"],)
        ).fetchone()["c"]

        if open_actions == 0:
            conn.execute(
                """
                UPDATE login_integrity_events
                SET review_status = ?,
                    summary = summary || ?,
                    updated_at = ?
                WHERE login_integrity_event_id = ?
                """,
                (
                    "RESOLVED",
                    f" All recorded Global Admin actions completed by {completed_by_display_name}.",
                    ts,
                    action["login_integrity_event_id"],
                )
            )

        audit_event(
            conn,
            entity_type="LoginIntegrityAction",
            entity_id=login_integrity_action_id,
            action="COMPLETE",
            summary=f"Login integrity admin action completed by {completed_by_display_name}.",
            organisation_id=action["organisation_id"],
        )

        conn.commit()

        updated_action = conn.execute(
            """
            SELECT *
            FROM login_integrity_admin_actions
            WHERE login_integrity_action_id = ?
            """,
            (login_integrity_action_id,)
        ).fetchone()

        updated_event = conn.execute(
            """
            SELECT *
            FROM login_integrity_events
            WHERE login_integrity_event_id = ?
            """,
            (action["login_integrity_event_id"],)
        ).fetchone()

        conn.close()

        return jsonify({
            "login_integrity_action": dict(updated_action),
            "login_integrity_report": dict(updated_event),
            "open_action_count_after_completion": open_actions,
            "rule": "Completing the final open action resolves the login integrity report.",
        }), 200


    @app.get("/global-admin/login-integrity-actions")
    def list_login_integrity_action_queue():
        action_status = request.args.get("action_status")
        organisation_id = request.args.get("organisation_id")
        user_id = request.args.get("user_id")

        conn = get_conn()
        ensure_login_integrity_action_tables(conn)

        sql = """
            SELECT
                a.*,
                e.event_type,
                e.risk_level,
                e.risk_score,
                e.review_status,
                e.ai_report_summary,
                u.display_name,
                u.email,
                u.role,
                o.name AS organisation_name
            FROM login_integrity_admin_actions a
            LEFT JOIN login_integrity_events e
                ON e.login_integrity_event_id = a.login_integrity_event_id
            LEFT JOIN user_accounts u
                ON u.user_id = a.user_id
            LEFT JOIN organisations o
                ON o.organisation_id = a.organisation_id
            WHERE 1 = 1
        """
        params = []

        if action_status:
            sql += " AND a.action_status = ?"
            params.append(action_status)

        if organisation_id:
            sql += " AND a.organisation_id = ?"
            params.append(organisation_id)

        if user_id:
            sql += " AND a.user_id = ?"
            params.append(user_id)

        sql += """
            ORDER BY
                CASE a.action_status
                    WHEN 'OPEN' THEN 0
                    WHEN 'COMPLETED' THEN 1
                    ELSE 2
                END,
                a.created_at DESC
        """

        rows = conn.execute(sql, params).fetchall()

        status_counts = {}
        type_counts = {}

        for row in rows:
            status_counts[row["action_status"]] = status_counts.get(row["action_status"], 0) + 1
            type_counts[row["action_type"]] = type_counts.get(row["action_type"], 0) + 1

        conn.close()

        return jsonify({
            "queue_type": "GLOBAL_ADMIN_LOGIN_INTEGRITY_ACTION_QUEUE",
            "count": len(rows),
            "action_status_counts": status_counts,
            "action_type_counts": type_counts,
            "items": [dict(row) for row in rows],
            "rule": "Global Admin owns the course of action. AI reports are advisory only.",
        }), 200


    @app.get("/global-admin/login-integrity-actions-summary")
    def get_login_integrity_action_summary():
        conn = get_conn()
        ensure_login_integrity_action_tables(conn)

        rows = conn.execute(
            """
            SELECT
                a.action_status,
                a.action_type,
                e.risk_level,
                COUNT(*) AS count
            FROM login_integrity_admin_actions a
            LEFT JOIN login_integrity_events e
                ON e.login_integrity_event_id = a.login_integrity_event_id
            GROUP BY a.action_status, a.action_type, e.risk_level
            ORDER BY a.action_status ASC, e.risk_level ASC, a.action_type ASC
            """
        ).fetchall()

        open_count = sum(r["count"] for r in rows if r["action_status"] == "OPEN")
        completed_count = sum(r["count"] for r in rows if r["action_status"] == "COMPLETED")

        conn.close()

        return jsonify({
            "summary_type": "LOGIN_INTEGRITY_ACTION_QUEUE_SUMMARY",
            "open_action_count": open_count,
            "completed_action_count": completed_count,
            "breakdown": [dict(row) for row in rows],
            "rule": "Global Admin owns the course of action. AI does not automatically penalise users or organisations.",
        }), 200


    @app.post("/sessions/<session_id>/heartbeat")
    def heartbeat_user_session(session_id):
        conn = get_conn()
        ensure_login_integrity_tables(conn)
        ensure_user_access_tables(conn)

        session = conn.execute(
            "SELECT * FROM user_sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()

        if not session:
            conn.close()
            return jsonify({"error": "Session not found"}), 404

        user = get_user_account(conn, session["user_id"])

        if not user:
            conn.close()
            return jsonify({"error": "User not found for session"}), 404

        policy = build_user_access_policy(conn, user)

        if session["session_status"] != "ACTIVE":
            conn.close()
            return jsonify({
                "error": "Session is not active",
                "session": dict(session),
                "access_policy": policy,
            }), 403

        if not policy["can_use_platform"]:
            ts = now_iso()

            conn.execute(
                """
                UPDATE user_sessions
                SET session_status = ?,
                    logout_at = ?,
                    ended_reason = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (
                    "ENDED",
                    ts,
                    "ACCESS_POLICY_NO_LONGER_ALLOWS_PLATFORM_USE",
                    ts,
                    session_id,
                )
            )

            conn.commit()

            updated = conn.execute(
                "SELECT * FROM user_sessions WHERE session_id = ?",
                (session_id,)
            ).fetchone()

            conn.close()

            return jsonify({
                "error": "Session ended because user access is no longer allowed",
                "session": dict(updated),
                "access_policy": policy,
            }), 403

        ts = now_iso()

        conn.execute(
            """
            UPDATE user_sessions
            SET last_seen_at = ?,
                updated_at = ?
            WHERE session_id = ?
            """,
            (ts, ts, session_id)
        )

        conn.commit()

        updated = conn.execute(
            "SELECT * FROM user_sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()

        conn.close()

        return jsonify({
            "session": dict(updated),
            "access_policy": policy,
            "message": "Session heartbeat accepted.",
            "rule": "Active sessions update last_seen_at so Login Integrity Guard can track live device activity.",
        }), 200


    @app.post("/global-admin/session-expiry-sweep")
    def sweep_expired_sessions():
        body = request.get_json(silent=True) or {}

        inactive_minutes = body.get("inactive_minutes", 480)
        swept_by_display_name = (body.get("swept_by_display_name") or "Global Admin").strip()
        confirmation_text = (body.get("confirmation_text") or "").strip()

        required_confirmation = "SWEEP EXPIRED SESSIONS"

        if confirmation_text != required_confirmation:
            return jsonify({
                "error": "Confirmation text is required before sweeping expired sessions",
                "required_confirmation_text": required_confirmation,
                "received_confirmation_text": confirmation_text,
                "rule": "Session expiry sweep changes session state and must be deliberate.",
            }), 400

        try:
            inactive_minutes = int(inactive_minutes)
        except Exception:
            return jsonify({"error": "inactive_minutes must be an integer"}), 400

        if inactive_minutes < 1:
            return jsonify({"error": "inactive_minutes must be at least 1"}), 400

        conn = get_conn()
        ensure_login_integrity_tables(conn)

        cutoff = (datetime.fromisoformat(now_iso()) - timedelta(minutes=inactive_minutes)).isoformat()
        ts = now_iso()

        expired = conn.execute(
            """
            SELECT *
            FROM user_sessions
            WHERE session_status = 'ACTIVE'
              AND last_seen_at < ?
            ORDER BY last_seen_at ASC
            """,
            (cutoff,)
        ).fetchall()

        for session in expired:
            conn.execute(
                """
                UPDATE user_sessions
                SET session_status = ?,
                    logout_at = ?,
                    ended_reason = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (
                    "ENDED",
                    ts,
                    "SESSION_EXPIRED_INACTIVITY_SWEEP",
                    ts,
                    session["session_id"],
                )
            )

        audit_event(
            conn,
            entity_type="UserSession",
            entity_id="session_expiry_sweep",
            action="SWEEP_EXPIRED_SESSIONS",
            summary=f"Session expiry sweep ended {len(expired)} inactive sessions after {inactive_minutes} minutes.",
            organisation_id=None,
        )

        conn.commit()

        ended_sessions = conn.execute(
            """
            SELECT *
            FROM user_sessions
            WHERE ended_reason = 'SESSION_EXPIRED_INACTIVITY_SWEEP'
              AND logout_at = ?
            ORDER BY logout_at DESC
            """,
            (ts,)
        ).fetchall()

        conn.close()

        return jsonify({
            "sweep_type": "SESSION_EXPIRY_SWEEP",
            "inactive_minutes": inactive_minutes,
            "cutoff_last_seen_before": cutoff,
            "swept_by_display_name": swept_by_display_name,
            "expired_session_count": len(ended_sessions),
            "expired_sessions": [dict(row) for row in ended_sessions],
            "rule": "Inactive active sessions are ended so Login Integrity Guard has an accurate live-session picture.",
        }), 200


    @app.post("/global-admin/temporary-user-expiry-sweep")
    def sweep_expired_temporary_users():
        body = request.get_json(silent=True) or {}

        swept_by_display_name = (body.get("swept_by_display_name") or "Global Admin").strip()
        confirmation_text = (body.get("confirmation_text") or "").strip()
        as_of = body.get("as_of") or now_iso()

        required_confirmation = "SWEEP EXPIRED TEMPORARY USERS"

        if confirmation_text != required_confirmation:
            return jsonify({
                "error": "Confirmation text is required before sweeping expired temporary users",
                "required_confirmation_text": required_confirmation,
                "received_confirmation_text": confirmation_text,
                "rule": "Temporary user expiry changes access state and must be deliberate.",
            }), 400

        conn = get_conn()
        ensure_subscription_guard_tables(conn)
        ensure_user_access_tables(conn)

        expired_access_rows = conn.execute(
            """
            SELECT *
            FROM temporary_user_access
            WHERE access_status = 'ACTIVE'
              AND access_ends_at < ?
            ORDER BY access_ends_at ASC
            """,
            (as_of,)
        ).fetchall()

        expired_access_ids = [row["temporary_user_access_id"] for row in expired_access_rows]
        ts = now_iso()

        for row in expired_access_rows:
            conn.execute(
                """
                UPDATE temporary_user_access
                SET access_status = ?,
                    updated_at = ?
                WHERE temporary_user_access_id = ?
                """,
                (
                    "EXPIRED",
                    ts,
                    row["temporary_user_access_id"],
                )
            )

            conn.execute(
                """
                UPDATE user_accounts
                SET access_status = ?,
                    updated_at = ?
                WHERE temporary_user_access_id = ?
                  AND role = 'TEMPORARY_USER'
                  AND access_status = 'ACTIVE'
                """,
                (
                    "EXPIRED",
                    ts,
                    row["temporary_user_access_id"],
                )
            )

            record_user_access_event(
                conn,
                user_id=row["temporary_user_access_id"],
                organisation_id=row["organisation_id"],
                action="TEMPORARY_ACCESS_EXPIRED",
                summary="Temporary user access expired and linked temporary users were marked expired.",
                changed_by_display_name=swept_by_display_name,
            )

        audit_event(
            conn,
            entity_type="TemporaryUserAccess",
            entity_id="temporary_user_expiry_sweep",
            action="SWEEP_EXPIRED_TEMPORARY_USERS",
            summary=f"Temporary user expiry sweep expired {len(expired_access_rows)} temporary access records.",
            organisation_id=None,
        )

        conn.commit()

        linked_users = []
        if expired_access_ids:
            placeholders = ",".join(["?"] * len(expired_access_ids))
            linked_users = conn.execute(
                f"""
                SELECT *
                FROM user_accounts
                WHERE temporary_user_access_id IN ({placeholders})
                ORDER BY display_name ASC
                """,
                expired_access_ids,
            ).fetchall()

        conn.close()

        return jsonify({
            "sweep_type": "TEMPORARY_USER_EXPIRY_SWEEP",
            "as_of": as_of,
            "swept_by_display_name": swept_by_display_name,
            "expired_temporary_access_count": len(expired_access_rows),
            "expired_temporary_access_ids": expired_access_ids,
            "linked_temporary_users": [dict(row) for row in linked_users],
            "rule": "Expired temporary access records and linked temporary users are marked EXPIRED.",
        }), 200
