import hashlib
import os
import secrets

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso

MASTER_KEY = os.environ.get("PALLET_PRO_MASTER_KEY")

_EXEMPT_PATHS = {
    "/health",
    "/",
    "/pricing-table",
    "/pricing-philosophy",
    "/auth/bootstrap",
    "/auth/login",
    "/sessions/login",
    "/auth/set-password",
    "/auth/setup-token-info",
    "/auth/forgot-password",
    "/auth/reset-password",
    "/auth/webauthn/authenticate/begin",
    "/auth/webauthn/authenticate/complete",
    "/install",
    "/install-events",
    "/vapid-public-key",
}
_GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}


def ensure_api_key_tables(conn):
    # user_accounts is included here so the auth middleware can JOIN against it
    # before any route has lazily called ensure_user_access_tables.
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_accounts (
        user_id TEXT PRIMARY KEY,
        organisation_id TEXT,
        display_name TEXT NOT NULL,
        email TEXT,
        role TEXT NOT NULL,
        access_status TEXT NOT NULL,
        temporary_user_access_id TEXT,
        created_by_display_name TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_api_keys (
        api_key_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        organisation_id TEXT,
        key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT NOT NULL,
        label TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        expires_at TEXT,
        last_used_at TEXT,
        created_at TEXT NOT NULL,
        created_by_display_name TEXT
    )
    """)


def _generate_raw_key():
    return "ppk_" + secrets.token_hex(32)


def _hash_key(raw_key):
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _lookup_key(conn, raw_key):
    if not raw_key or not raw_key.startswith("ppk_"):
        return None
    key_hash = _hash_key(raw_key)
    now = now_iso()
    row = conn.execute(
        """
        SELECT k.api_key_id, k.user_id, k.label,
               u.role, u.access_status,
               u.organisation_id AS user_org_id,
               u.display_name
        FROM user_api_keys k
        JOIN user_accounts u ON u.user_id = k.user_id
        WHERE k.key_hash = ?
          AND k.is_active = 1
          AND (k.expires_at IS NULL OR k.expires_at > ?)
        """,
        (key_hash, now),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE user_api_keys SET last_used_at = ? WHERE api_key_id = ?",
            (now, row["api_key_id"]),
        )
        conn.commit()
        return dict(row)
    return None


def _lookup_session(conn, session_id):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_sessions (
        session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, organisation_id TEXT,
        role TEXT NOT NULL, device_id TEXT NOT NULL, device_label TEXT,
        ip_address_hash TEXT, user_agent_hash TEXT, session_status TEXT NOT NULL,
        login_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, logout_at TEXT,
        ended_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    now = now_iso()
    row = conn.execute(
        """SELECT s.session_id, s.user_id, s.organisation_id, s.role,
                  u.display_name, u.access_status
           FROM user_sessions s
           JOIN user_accounts u ON u.user_id = s.user_id
           WHERE s.session_id = ? AND s.session_status = 'ACTIVE'""",
        (session_id,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE user_sessions SET last_seen_at = ?, updated_at = ? WHERE session_id = ?",
            (now, now, session_id),
        )
        conn.commit()
        return {
            "user_id": row["user_id"],
            "role": row["role"],
            "user_org_id": row["organisation_id"],
            "access_status": row["access_status"],
            "display_name": row["display_name"],
        }
    return None


def register_auth_middleware(app):
    @app.before_request
    def enforce_auth():
        if request.method == "OPTIONS":
            return None
        if request.path in _EXEMPT_PATHS:
            return None

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({
                "error": "AUTHENTICATION_REQUIRED",
                "message": "Include your API key as: Authorization: Bearer <key>",
            }), 401

        raw_key = auth_header[7:]

        if MASTER_KEY and raw_key == MASTER_KEY:
            g.current_user = {
                "user_id": "master",
                "role": "SUPER_GLOBAL_ADMIN",
                "user_org_id": None,
                "access_status": "ACTIVE",
                "display_name": "Master Key",
            }
            return None

        conn = get_conn()
        ensure_api_key_tables(conn)

        if raw_key.startswith("sess_"):
            user = _lookup_session(conn, raw_key)
        else:
            user = _lookup_key(conn, raw_key)

        conn.close()

        if not user:
            return jsonify({
                "error": "INVALID_API_KEY",
                "message": "The provided API key or session token is invalid, expired, or revoked.",
            }), 401

        if user["access_status"] != "ACTIVE":
            return jsonify({
                "error": "ACCOUNT_NOT_ACTIVE",
                "access_status": user["access_status"],
            }), 403

        g.current_user = dict(user)

        if request.path.startswith("/global-admin/"):
            if user["role"] not in _GLOBAL_ADMIN_ROLES:
                return jsonify({
                    "error": "INSUFFICIENT_ROLE",
                    "message": "This endpoint requires GLOBAL_ADMIN or SUPER_GLOBAL_ADMIN role.",
                    "your_role": user["role"],
                }), 403

        org_id_in_url = (request.view_args or {}).get("organisation_id")
        if org_id_in_url and user["role"] not in _GLOBAL_ADMIN_ROLES:
            if user.get("user_org_id") != org_id_in_url:
                return jsonify({
                    "error": "ORG_ACCESS_DENIED",
                    "message": "You do not have access to this organisation.",
                }), 403

        return None


def register_auth_routes(app):

    @app.post("/auth/bootstrap")
    def auth_bootstrap():
        body = request.get_json(silent=True) or {}
        display_name = (body.get("display_name") or "").strip()
        email = (body.get("email") or "").strip() or None
        label = (body.get("label") or "Bootstrap key").strip()

        if not display_name:
            return jsonify({"error": "display_name is required"}), 400

        conn = get_conn()
        ensure_api_key_tables(conn)

        existing = conn.execute("SELECT COUNT(*) AS c FROM user_accounts").fetchone()
        if existing["c"] > 0:
            conn.close()
            return jsonify({
                "error": "BOOTSTRAP_UNAVAILABLE",
                "message": "Bootstrap is only available when no users exist.",
            }), 409

        ts = now_iso()
        user_id = make_id("usr")

        conn.execute(
            """
            INSERT INTO user_accounts (
                user_id, organisation_id, display_name, email, role,
                access_status, created_by_display_name, created_at, updated_at
            ) VALUES (?, NULL, ?, ?, 'SUPER_GLOBAL_ADMIN', 'ACTIVE', 'bootstrap', ?, ?)
            """,
            (user_id, display_name, email, ts, ts),
        )

        raw_key = _generate_raw_key()
        key_id = make_id("apik")

        conn.execute(
            """
            INSERT INTO user_api_keys (
                api_key_id, user_id, organisation_id, key_hash, key_prefix,
                label, is_active, created_at, created_by_display_name
            ) VALUES (?, ?, NULL, ?, ?, ?, 1, ?, 'bootstrap')
            """,
            (key_id, user_id, _hash_key(raw_key), raw_key[:12], label, ts),
        )

        audit_event(conn, entity_type="UserAccount", entity_id=user_id, action="BOOTSTRAP",
                    summary=f"SUPER_GLOBAL_ADMIN '{display_name}' created via bootstrap.")

        conn.commit()
        conn.close()

        return jsonify({
            "user_id": user_id,
            "display_name": display_name,
            "role": "SUPER_GLOBAL_ADMIN",
            "api_key": raw_key,
            "api_key_id": key_id,
            "warning": "Store this key securely. It will not be shown again.",
        }), 201

    @app.post("/auth/keys")
    def issue_api_key():
        body = request.get_json(silent=True) or {}
        label = (body.get("label") or "API Key").strip()
        expires_at = body.get("expires_at") or None
        target_user_id = body.get("user_id")
        current_user = g.current_user

        if target_user_id and target_user_id != current_user["user_id"]:
            if current_user["role"] not in _GLOBAL_ADMIN_ROLES:
                return jsonify({
                    "error": "Only GLOBAL_ADMIN or SUPER_GLOBAL_ADMIN can issue keys for other users",
                }), 403

        final_user_id = target_user_id or current_user["user_id"]

        if final_user_id == "master":
            return jsonify({"error": "Cannot issue DB keys for the master-key user"}), 400

        conn = get_conn()
        ensure_api_key_tables(conn)

        user = conn.execute(
            "SELECT * FROM user_accounts WHERE user_id = ?", (final_user_id,)
        ).fetchone()
        if not user:
            conn.close()
            return jsonify({"error": "User not found"}), 404

        raw_key = _generate_raw_key()
        key_id = make_id("apik")
        ts = now_iso()

        conn.execute(
            """
            INSERT INTO user_api_keys (
                api_key_id, user_id, organisation_id, key_hash, key_prefix,
                label, is_active, expires_at, created_at, created_by_display_name
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (key_id, final_user_id, user["organisation_id"], _hash_key(raw_key),
             raw_key[:12], label, expires_at, ts, current_user["display_name"]),
        )

        audit_event(conn, entity_type="ApiKey", entity_id=key_id, action="ISSUE",
                    summary=f"API key '{label}' issued for user {final_user_id} by {current_user['display_name']}.",
                    organisation_id=user["organisation_id"])

        conn.commit()
        conn.close()

        return jsonify({
            "api_key_id": key_id,
            "user_id": final_user_id,
            "label": label,
            "key_prefix": raw_key[:12],
            "api_key": raw_key,
            "expires_at": expires_at,
            "warning": "Store this key securely. It will not be shown again.",
        }), 201

    @app.get("/auth/keys")
    def list_api_keys():
        current_user = g.current_user
        target_user_id = request.args.get("user_id") or current_user["user_id"]

        if target_user_id != current_user["user_id"] and current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            return jsonify({"error": "Only GLOBAL_ADMIN or SUPER_GLOBAL_ADMIN can list keys for other users"}), 403

        if target_user_id == "master":
            return jsonify({"user_id": "master", "count": 0, "keys": []}), 200

        conn = get_conn()
        ensure_api_key_tables(conn)

        rows = conn.execute(
            """
            SELECT api_key_id, user_id, organisation_id, key_prefix, label,
                   is_active, expires_at, last_used_at, created_at, created_by_display_name
            FROM user_api_keys
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (target_user_id,),
        ).fetchall()
        conn.close()

        return jsonify({
            "user_id": target_user_id,
            "count": len(rows),
            "keys": [dict(r) for r in rows],
        }), 200

    @app.post("/auth/keys/<api_key_id>/revoke")
    def revoke_api_key(api_key_id):
        current_user = g.current_user

        conn = get_conn()
        ensure_api_key_tables(conn)

        key = conn.execute(
            "SELECT * FROM user_api_keys WHERE api_key_id = ?", (api_key_id,)
        ).fetchone()

        if not key:
            conn.close()
            return jsonify({"error": "API key not found"}), 404

        if key["user_id"] != current_user["user_id"] and current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            conn.close()
            return jsonify({"error": "You can only revoke your own keys"}), 403

        conn.execute(
            "UPDATE user_api_keys SET is_active = 0 WHERE api_key_id = ?", (api_key_id,)
        )

        audit_event(conn, entity_type="ApiKey", entity_id=api_key_id, action="REVOKE",
                    summary=f"API key {api_key_id} revoked by {current_user['display_name']}.",
                    organisation_id=key["organisation_id"])

        conn.commit()
        conn.close()

        return jsonify({
            "api_key_id": api_key_id,
            "revoked": True,
            "revoked_by": current_user["display_name"],
        }), 200
