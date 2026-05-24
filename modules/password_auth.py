import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from flask import g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from audit import audit_event
from db import get_conn, make_id, now_iso


def ensure_password_auth_columns(conn):
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(user_accounts)")}
    if "password_hash" not in existing:
        conn.execute("ALTER TABLE user_accounts ADD COLUMN password_hash TEXT")
    if "mobile_number" not in existing:
        conn.execute("ALTER TABLE user_accounts ADD COLUMN mobile_number TEXT")
    conn.commit()


def ensure_password_token_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_setup_tokens (
        token_id     TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
        token_hash   TEXT NOT NULL UNIQUE,
        expires_at   TEXT NOT NULL,
        used_at      TEXT,
        created_at   TEXT NOT NULL
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS password_reset_tokens (
        token_id     TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
        token_hash   TEXT NOT NULL UNIQUE,
        expires_at   TEXT NOT NULL,
        used_at      TEXT,
        created_at   TEXT NOT NULL
    )
    """)
    conn.commit()


def hash_password(plain):
    return generate_password_hash(plain)


def verify_password(plain, hashed):
    if not hashed:
        return False
    return check_password_hash(hashed, plain)


def _generate_token():
    return secrets.token_urlsafe(32)


def _hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_setup_token(conn, user_id):
    """Generate a one-time setup token for a newly created user. Returns the raw token."""
    ensure_password_token_tables(conn)
    raw = _generate_token()
    token_id = make_id("ust")
    ts = now_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        """INSERT INTO user_setup_tokens (token_id, user_id, token_hash, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (token_id, user_id, _hash_token(raw), expires_at, ts),
    )
    return raw


def register_password_auth_routes(app):

    # --- exempt paths handled in auth middleware ---

    @app.get("/auth/setup-token-info")
    def get_setup_token_info():
        """
        Public. Returns display info for a setup token without consuming it.
        Used by the /setup page to show the user's name and who invited them
        before they submit the form.
        """
        raw_token = request.args.get("token", "").strip()
        if not raw_token:
            return jsonify({"error": "token is required"}), 400

        conn = get_conn()
        ensure_password_token_tables(conn)

        ts = now_iso()
        token_hash = _hash_token(raw_token)

        row = conn.execute(
            """SELECT user_id FROM user_setup_tokens
               WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?""",
            (token_hash, ts),
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "This setup link has expired or already been used."}), 400

        user = conn.execute(
            """SELECT display_name, created_by_display_name, mobile_number, email
               FROM user_accounts WHERE user_id = ?""",
            (row["user_id"],),
        ).fetchone()

        conn.close()

        if not user:
            return jsonify({"error": "User not found"}), 404

        return jsonify({
            "display_name": user["display_name"],
            "invited_by": user["created_by_display_name"] or "your admin",
            "has_mobile": bool(user["mobile_number"]),
            "has_email": bool(user["email"]),
        }), 200

    @app.post("/auth/set-password")
    def set_password():
        """First-time password setup using a one-time setup token issued at user creation."""
        body = request.get_json(silent=True) or {}
        setup_token = (body.get("setup_token") or "").strip()
        new_password = (body.get("password") or "").strip()
        mobile_number = (body.get("mobile_number") or "").strip() or None

        if not setup_token:
            return jsonify({"error": "setup_token is required"}), 400
        if not new_password or len(new_password) < 8:
            return jsonify({"error": "password must be at least 8 characters"}), 400

        conn = get_conn()
        ensure_password_auth_columns(conn)
        ensure_password_token_tables(conn)

        ts = now_iso()
        token_hash = _hash_token(setup_token)

        row = conn.execute(
            """SELECT * FROM user_setup_tokens
               WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?""",
            (token_hash, ts),
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Invalid or expired setup token"}), 400

        user_id = row["user_id"]
        user = conn.execute(
            "SELECT * FROM user_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()

        if not user:
            conn.close()
            return jsonify({"error": "User not found"}), 404

        if mobile_number:
            existing = conn.execute(
                "SELECT user_id FROM user_accounts WHERE mobile_number = ? AND user_id != ?",
                (mobile_number, user_id),
            ).fetchone()
            if existing:
                conn.close()
                return jsonify({"error": "Mobile number already registered to another account"}), 409

        conn.execute(
            "UPDATE user_accounts SET password_hash = ?, updated_at = ? WHERE user_id = ?",
            (hash_password(new_password), ts, user_id),
        )
        if mobile_number:
            conn.execute(
                "UPDATE user_accounts SET mobile_number = ?, updated_at = ? WHERE user_id = ?",
                (mobile_number, ts, user_id),
            )
        conn.execute(
            "UPDATE user_setup_tokens SET used_at = ? WHERE token_hash = ?",
            (ts, token_hash),
        )

        audit_event(
            conn,
            entity_type="UserAccount",
            entity_id=user_id,
            action="PASSWORD_SET",
            summary=f"Initial password set for user {user['display_name']}.",
            organisation_id=user["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({"message": "Password set. You can now log in."}), 200


    @app.post("/auth/change-password")
    def change_password():
        """Change password. Requires an active session. Verifies current password first."""
        current_user = g.current_user
        body = request.get_json(silent=True) or {}
        current_password = (body.get("current_password") or "").strip()
        new_password = (body.get("new_password") or "").strip()

        if not current_password or not new_password:
            return jsonify({"error": "current_password and new_password are required"}), 400
        if len(new_password) < 8:
            return jsonify({"error": "new_password must be at least 8 characters"}), 400
        if current_user["user_id"] == "master":
            return jsonify({"error": "Cannot change master key via this endpoint"}), 400

        conn = get_conn()
        ensure_password_auth_columns(conn)

        user = conn.execute(
            "SELECT * FROM user_accounts WHERE user_id = ?",
            (current_user["user_id"],),
        ).fetchone()

        if not user or not verify_password(current_password, user["password_hash"]):
            conn.close()
            return jsonify({"error": "Current password is incorrect"}), 400

        ts = now_iso()
        conn.execute(
            "UPDATE user_accounts SET password_hash = ?, updated_at = ? WHERE user_id = ?",
            (hash_password(new_password), ts, current_user["user_id"]),
        )

        audit_event(
            conn,
            entity_type="UserAccount",
            entity_id=current_user["user_id"],
            action="PASSWORD_CHANGED",
            summary=f"Password changed for user {current_user['display_name']}.",
            organisation_id=user["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({"message": "Password changed successfully."}), 200


    @app.post("/auth/forgot-password")
    def forgot_password():
        """Request a password reset. Accepts mobile_number or email. No auth required."""
        body = request.get_json(silent=True) or {}
        mobile_number = (body.get("mobile_number") or "").strip() or None
        email = (body.get("email") or "").strip() or None

        if not mobile_number and not email:
            return jsonify({"error": "mobile_number or email is required"}), 400

        conn = get_conn()
        ensure_password_auth_columns(conn)
        ensure_password_token_tables(conn)

        user = None
        if mobile_number:
            user = conn.execute(
                "SELECT * FROM user_accounts WHERE mobile_number = ? AND access_status = 'ACTIVE'",
                (mobile_number,),
            ).fetchone()
        if not user and email:
            user = conn.execute(
                "SELECT * FROM user_accounts WHERE email = ? AND access_status = 'ACTIVE'",
                (email,),
            ).fetchone()

        # Always return 200 — never confirm whether the account exists
        if not user:
            conn.close()
            return jsonify({"message": "If an account exists, a reset link has been sent."}), 200

        raw = _generate_token()
        token_id = make_id("prt")
        ts = now_iso()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")

        conn.execute(
            """INSERT INTO password_reset_tokens (token_id, user_id, token_hash, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (token_id, user["user_id"], _hash_token(raw), expires_at, ts),
        )

        audit_event(
            conn,
            entity_type="UserAccount",
            entity_id=user["user_id"],
            action="PASSWORD_RESET_REQUESTED",
            summary=f"Password reset requested for user {user['display_name']}.",
            organisation_id=user["organisation_id"],
        )

        conn.commit()
        conn.close()

        # TODO: send reset link via email when email service is wired up
        return jsonify({
            "message": "If an account exists, a reset link has been sent.",
            "_dev_reset_token": raw,  # remove once email is live
        }), 200


    @app.post("/auth/reset-password")
    def reset_password():
        """Consume a reset token and set a new password. No auth required."""
        body = request.get_json(silent=True) or {}
        reset_token = (body.get("reset_token") or "").strip()
        new_password = (body.get("password") or "").strip()

        if not reset_token:
            return jsonify({"error": "reset_token is required"}), 400
        if not new_password or len(new_password) < 8:
            return jsonify({"error": "password must be at least 8 characters"}), 400

        conn = get_conn()
        ensure_password_token_tables(conn)
        ensure_password_auth_columns(conn)

        ts = now_iso()
        token_hash = _hash_token(reset_token)

        row = conn.execute(
            """SELECT * FROM password_reset_tokens
               WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?""",
            (token_hash, ts),
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Invalid or expired reset token"}), 400

        user_id = row["user_id"]
        user = conn.execute(
            "SELECT * FROM user_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()

        conn.execute(
            "UPDATE user_accounts SET password_hash = ?, updated_at = ? WHERE user_id = ?",
            (hash_password(new_password), ts, user_id),
        )
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE token_hash = ?",
            (ts, token_hash),
        )

        audit_event(
            conn,
            entity_type="UserAccount",
            entity_id=user_id,
            action="PASSWORD_RESET",
            summary=f"Password reset completed for user {user['display_name'] if user else user_id}.",
            organisation_id=user["organisation_id"] if user else None,
        )

        conn.commit()
        conn.close()

        return jsonify({"message": "Password reset. You can now log in."}), 200


    @app.post("/global-admin/users/<user_id>/force-password-reset")
    def force_password_reset(user_id):
        """Global Admin generates a new setup token for a user (e.g. lost access)."""
        conn = get_conn()
        ensure_password_auth_columns(conn)
        ensure_password_token_tables(conn)

        user = conn.execute(
            "SELECT * FROM user_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()

        if not user:
            conn.close()
            return jsonify({"error": "User not found"}), 404

        raw = generate_setup_token(conn, user_id)

        audit_event(
            conn,
            entity_type="UserAccount",
            entity_id=user_id,
            action="FORCE_PASSWORD_RESET",
            summary=f"Force password reset token issued for user {user['display_name']} by {g.current_user['display_name']}.",
            organisation_id=user["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({
            "user_id": user_id,
            "setup_token": raw,
            "warning": "Share this token securely with the user. It expires in 7 days and can only be used once.",
        }), 200
