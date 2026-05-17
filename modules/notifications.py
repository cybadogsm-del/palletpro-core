"""
Notifications inbox module.

Every significant event in Pallet Pro creates a persistent inbox
notification for the relevant user. The same call also fires a Web Push
so the user's device is alerted immediately.

The key function is notify_user() — import and call it from anywhere:

    from modules.notifications import notify_user

    notify_user(
        user_id="usr_abc",
        notification_type="TCR_APPROVED",
        title="Correction approved",
        body="Your correction for transaction #PP-1234 has been approved.",
        url="/transactions/txn_xyz",
        entity_type="TransactionCorrectionRequest",
        entity_id="tcr_123",
        organisation_id="org_abc",
    )

notify_user() is always best-effort — it never raises, so callers are
never affected by a notification failure.

Notification types:
  TCR_APPROVED          — correction request approved
  TCR_REJECTED          — correction request rejected
  LOSS_CONFIRMED        — resource loss confirmed, balance updated
  LOSS_REJECTED         — resource loss report rejected
  ENTITY_RESOLVED       — missing entity fixed, transaction posted
  PENDING_POSTED        — pending transaction has been posted
  BATCH_ITEM_FAILED     — an offline batch item could not be processed
  GENERAL               — catch-all for ad hoc notifications
"""

from flask import g, jsonify, request

from db import get_conn, make_id, now_iso

_VALID_TYPES = {
    "TCR_APPROVED", "TCR_REJECTED",
    "LOSS_CONFIRMED", "LOSS_REJECTED",
    "ENTITY_RESOLVED", "PENDING_POSTED",
    "BATCH_ITEM_FAILED", "GENERAL",
}


def ensure_notification_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS notifications (
        notification_id     TEXT PRIMARY KEY,
        user_id             TEXT NOT NULL,
        organisation_id     TEXT,
        type                TEXT NOT NULL,
        title               TEXT NOT NULL,
        body                TEXT NOT NULL,
        url                 TEXT,
        entity_type         TEXT,
        entity_id           TEXT,
        is_read             INTEGER NOT NULL DEFAULT 0,
        created_at          TEXT NOT NULL,
        read_at             TEXT
    )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_user "
        "ON notifications (user_id, is_read, created_at DESC)"
    )
    conn.commit()


# ------------------------------------------------------------------ #
# Core utility — import and call from any module                     #
# ------------------------------------------------------------------ #

def notify_user(
    user_id,
    notification_type,
    title,
    body,
    url=None,
    entity_type=None,
    entity_id=None,
    organisation_id=None,
):
    """
    Create a persistent inbox notification for a user AND fire a Web Push.

    Always best-effort — never raises. Returns the notification_id on
    success, or None if something went wrong.
    """
    if not user_id or user_id == "master":
        return None

    try:
        conn = get_conn()
        ensure_notification_tables(conn)

        notification_id = make_id("notif")
        ts = now_iso()

        conn.execute(
            """INSERT INTO notifications
               (notification_id, user_id, organisation_id, type,
                title, body, url, entity_type, entity_id,
                is_read, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (notification_id, user_id, organisation_id,
             notification_type, title, body, url,
             entity_type, entity_id, ts),
        )
        conn.commit()
        conn.close()
    except Exception:
        return None

    # Fire push best-effort (import here to avoid circular imports at module load)
    try:
        from modules.web_push import send_push_to_user
        send_push_to_user(
            user_id,
            title=title,
            body=body,
            url=url,
            tag=f"{notification_type}-{entity_id or notification_id}",
        )
    except Exception:
        pass

    return notification_id


# ------------------------------------------------------------------ #
# Routes                                                             #
# ------------------------------------------------------------------ #

def register_notification_routes(app):

    @app.get("/users/<user_id>/notifications")
    def list_notifications(user_id):
        """
        Fetch the notification inbox for a user.

        Query params:
          unread_only=true   — only unread notifications
          limit=20           — page size (max 100)
          offset=0           — pagination offset
        """
        current_user = g.current_user
        _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}

        if current_user["user_id"] != user_id and current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            return jsonify({"error": "Access denied"}), 403

        unread_only = request.args.get("unread_only", "").lower() in ("1", "true", "yes")
        try:
            limit = min(int(request.args.get("limit", 20)), 100)
            offset = max(int(request.args.get("offset", 0)), 0)
        except (ValueError, TypeError):
            return jsonify({"error": "limit and offset must be integers"}), 400

        conn = get_conn()
        ensure_notification_tables(conn)

        sql = """
            SELECT * FROM notifications
            WHERE user_id = ?
        """
        params = [user_id]

        if unread_only:
            sql += " AND is_read = 0"

        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]

        rows = conn.execute(sql, params).fetchall()

        unread_count = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
            (user_id,),
        ).fetchone()[0]

        conn.close()

        return jsonify({
            "user_id": user_id,
            "unread_count": unread_count,
            "limit": limit,
            "offset": offset,
            "count": len(rows),
            "notifications": [dict(r) for r in rows],
        }), 200


    @app.get("/users/<user_id>/notifications/unread-count")
    def get_unread_count(user_id):
        """
        Return the unread notification count — used for the app badge.
        Lightweight endpoint, safe to poll frequently.
        """
        current_user = g.current_user
        _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}

        if current_user["user_id"] != user_id and current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            return jsonify({"error": "Access denied"}), 403

        conn = get_conn()
        ensure_notification_tables(conn)

        count = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
            (user_id,),
        ).fetchone()[0]
        conn.close()

        return jsonify({"user_id": user_id, "unread_count": count}), 200


    @app.post("/notifications/<notification_id>/mark-read")
    def mark_notification_read(notification_id):
        """Mark a single notification as read."""
        current_user = g.current_user

        conn = get_conn()
        ensure_notification_tables(conn)

        row = conn.execute(
            "SELECT * FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Notification not found"}), 404

        _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
        if row["user_id"] != current_user["user_id"] and current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            conn.close()
            return jsonify({"error": "Access denied"}), 403

        if row["is_read"]:
            conn.close()
            return jsonify({"notification_id": notification_id, "is_read": True}), 200

        ts = now_iso()
        conn.execute(
            "UPDATE notifications SET is_read = 1, read_at = ? WHERE notification_id = ?",
            (ts, notification_id),
        )
        conn.commit()
        conn.close()

        return jsonify({
            "notification_id": notification_id,
            "is_read": True,
            "read_at": ts,
        }), 200


    @app.post("/users/<user_id>/notifications/mark-all-read")
    def mark_all_notifications_read(user_id):
        """Mark every unread notification for a user as read."""
        current_user = g.current_user
        _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}

        if current_user["user_id"] != user_id and current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            return jsonify({"error": "Access denied"}), 403

        conn = get_conn()
        ensure_notification_tables(conn)

        ts = now_iso()
        result = conn.execute(
            "UPDATE notifications SET is_read = 1, read_at = ? WHERE user_id = ? AND is_read = 0",
            (ts, user_id),
        )
        marked = result.rowcount
        conn.commit()
        conn.close()

        return jsonify({
            "user_id": user_id,
            "marked_read": marked,
            "message": f"{marked} notification(s) marked as read.",
        }), 200


    @app.delete("/notifications/<notification_id>")
    def delete_notification(notification_id):
        """Remove a single notification from the inbox."""
        current_user = g.current_user

        conn = get_conn()
        ensure_notification_tables(conn)

        row = conn.execute(
            "SELECT user_id FROM notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Notification not found"}), 404

        _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
        if row["user_id"] != current_user["user_id"] and current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            conn.close()
            return jsonify({"error": "Access denied"}), 403

        conn.execute(
            "DELETE FROM notifications WHERE notification_id = ?",
            (notification_id,),
        )
        conn.commit()
        conn.close()

        return jsonify({
            "notification_id": notification_id,
            "deleted": True,
        }), 200
