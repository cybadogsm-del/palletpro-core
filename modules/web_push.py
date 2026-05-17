"""
Web Push notifications module.

SETUP (one time):
  1. python generate_vapid_keys.py
  2. Add VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY, VAPID_CONTACT to .env

FLOW:
  1. Frontend calls GET /vapid-public-key to get the server's public key.
  2. Browser's pushManager.subscribe(vapidPublicKey) returns a PushSubscription.
  3. Frontend POSTs the subscription to POST /push-subscriptions.
  4. Backend stores it against the user.
  5. Any module calls send_push_to_user(user_id, title, body, ...) to
     deliver a notification. Best-effort — failures never crash the caller.

Graceful degradation:
  If pywebpush is not installed or VAPID keys are not configured, push is
  silently disabled. Routes still exist but return a clear message.
"""

import json
import os

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso

try:
    from pywebpush import WebPushException, webpush
    _PUSH_AVAILABLE = True
except ImportError:
    _PUSH_AVAILABLE = False

_VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip() or None
_VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "").strip() or None
_VAPID_CONTACT = os.environ.get("VAPID_CONTACT", "mailto:admin@palletpro.app").strip()

_PUSH_ENABLED = _PUSH_AVAILABLE and bool(_VAPID_PRIVATE_KEY) and bool(_VAPID_PUBLIC_KEY)


def ensure_push_subscription_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS push_subscriptions (
        subscription_id     TEXT PRIMARY KEY,
        user_id             TEXT NOT NULL,
        organisation_id     TEXT,
        device_label        TEXT,
        endpoint            TEXT NOT NULL,
        p256dh              TEXT NOT NULL,
        auth_key            TEXT NOT NULL,
        is_active           INTEGER NOT NULL DEFAULT 1,
        created_at          TEXT NOT NULL,
        last_used_at        TEXT
    )
    """)
    conn.commit()


# ------------------------------------------------------------------ #
# Send utility — importable by other modules                         #
# ------------------------------------------------------------------ #

def send_push_to_user(user_id, title, body, url=None, data=None, tag=None):
    """
    Best-effort push notification to all active subscriptions for a user.

    Designed to be called from any module. Opens its own DB connection.
    Never raises — failures are silently swallowed so the caller's
    transaction is never affected by a push failure.

    Returns the number of subscriptions successfully notified.
    """
    if not _PUSH_ENABLED:
        return 0

    try:
        conn = get_conn()
        ensure_push_subscription_tables(conn)

        subs = conn.execute(
            """SELECT subscription_id, endpoint, p256dh, auth_key
               FROM push_subscriptions
               WHERE user_id = ? AND is_active = 1""",
            (user_id,),
        ).fetchall()

        if not subs:
            conn.close()
            return 0

        payload = {"title": title, "body": body}
        if url:
            payload["url"] = url
        if tag:
            payload["tag"] = tag
        if data:
            payload.update(data)

        success_count = 0
        expired_ids = []

        for sub in subs:
            try:
                webpush(
                    subscription_info={
                        "endpoint": sub["endpoint"],
                        "keys": {
                            "p256dh": sub["p256dh"],
                            "auth": sub["auth_key"],
                        },
                    },
                    data=json.dumps(payload),
                    vapid_private_key=_VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": _VAPID_CONTACT},
                    ttl=86400,
                )
                conn.execute(
                    "UPDATE push_subscriptions SET last_used_at = ? WHERE subscription_id = ?",
                    (now_iso(), sub["subscription_id"]),
                )
                success_count += 1
            except WebPushException as exc:
                # 410 Gone = subscription is no longer valid; remove it
                if exc.response is not None and exc.response.status_code in (404, 410):
                    expired_ids.append(sub["subscription_id"])
            except Exception:
                pass  # network error, timeout, etc. — ignore

        if expired_ids:
            conn.execute(
                f"UPDATE push_subscriptions SET is_active = 0 WHERE subscription_id IN "
                f"({','.join('?' * len(expired_ids))})",
                expired_ids,
            )

        conn.commit()
        conn.close()
        return success_count

    except Exception:
        return 0


def send_push_to_org(organisation_id, title, body, url=None, data=None, tag=None,
                     exclude_user_id=None):
    """
    Best-effort push to all users in an organisation.
    Pass exclude_user_id to skip the user who triggered the event.
    """
    if not _PUSH_ENABLED:
        return 0

    try:
        conn = get_conn()
        ensure_push_subscription_tables(conn)

        sql = """SELECT DISTINCT user_id FROM push_subscriptions
                 WHERE organisation_id = ? AND is_active = 1"""
        params = [organisation_id]
        if exclude_user_id:
            sql += " AND user_id != ?"
            params.append(exclude_user_id)

        user_ids = [r["user_id"] for r in conn.execute(sql, params).fetchall()]
        conn.close()
    except Exception:
        return 0

    total = 0
    for uid in user_ids:
        total += send_push_to_user(uid, title, body, url=url, data=data, tag=tag)
    return total


# ------------------------------------------------------------------ #
# Routes                                                              #
# ------------------------------------------------------------------ #

def register_web_push_routes(app):

    @app.get("/vapid-public-key")
    def get_vapid_public_key():
        """
        Return the VAPID public key so the frontend can subscribe.
        Public route — no auth required.
        Called once when the user enables push notifications.
        """
        if not _PUSH_ENABLED:
            return jsonify({
                "enabled": False,
                "message": (
                    "Push notifications are not configured on this server. "
                    "Set VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY environment variables."
                    if not _VAPID_PUBLIC_KEY
                    else "pywebpush is not installed on this server."
                ),
            }), 200

        return jsonify({
            "enabled": True,
            "vapid_public_key": _VAPID_PUBLIC_KEY,
        }), 200


    @app.post("/push-subscriptions")
    def create_push_subscription():
        """
        Store a browser PushSubscription for the authenticated user.

        Body (JSON — mirrors the browser PushSubscription object):
          endpoint     — the push service URL
          keys.p256dh  — client public key
          keys.auth    — auth secret
          device_label — optional friendly name e.g. "iPhone 15"
        """
        current_user = g.current_user
        body = request.get_json(silent=True) or {}

        endpoint = (body.get("endpoint") or "").strip()
        keys = body.get("keys") or {}
        p256dh = (keys.get("p256dh") or "").strip()
        auth_key = (keys.get("auth") or "").strip()
        device_label = (body.get("device_label") or "").strip() or None

        if not endpoint:
            return jsonify({"error": "endpoint is required"}), 400
        if not p256dh:
            return jsonify({"error": "keys.p256dh is required"}), 400
        if not auth_key:
            return jsonify({"error": "keys.auth is required"}), 400

        conn = get_conn()
        ensure_push_subscription_tables(conn)

        # Check for an existing subscription with the same endpoint
        existing = conn.execute(
            "SELECT subscription_id FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
            (endpoint, current_user["user_id"]),
        ).fetchone()

        ts = now_iso()

        if existing:
            # Re-register (keys may have rotated)
            conn.execute(
                """UPDATE push_subscriptions
                   SET p256dh = ?, auth_key = ?, device_label = ?,
                       is_active = 1, last_used_at = ?
                   WHERE subscription_id = ?""",
                (p256dh, auth_key, device_label, ts, existing["subscription_id"]),
            )
            subscription_id = existing["subscription_id"]
        else:
            subscription_id = make_id("psub")
            conn.execute(
                """INSERT INTO push_subscriptions
                   (subscription_id, user_id, organisation_id, device_label,
                    endpoint, p256dh, auth_key, is_active, created_at, last_used_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (subscription_id, current_user["user_id"],
                 current_user.get("user_org_id"),
                 device_label, endpoint, p256dh, auth_key, ts, ts),
            )

        conn.commit()
        conn.close()

        return jsonify({
            "subscription_id": subscription_id,
            "user_id": current_user["user_id"],
            "device_label": device_label,
            "push_enabled": _PUSH_ENABLED,
            "message": (
                "Push subscription registered."
                if _PUSH_ENABLED
                else "Subscription saved, but push is not yet configured on this server."
            ),
        }), 201


    @app.delete("/push-subscriptions/<subscription_id>")
    def delete_push_subscription(subscription_id):
        """Deactivate a push subscription (user disables notifications)."""
        current_user = g.current_user

        conn = get_conn()
        ensure_push_subscription_tables(conn)

        sub = conn.execute(
            "SELECT * FROM push_subscriptions WHERE subscription_id = ?",
            (subscription_id,),
        ).fetchone()

        if not sub:
            conn.close()
            return jsonify({"error": "Subscription not found"}), 404

        # Users can only remove their own; Global Admin can remove any
        _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
        if sub["user_id"] != current_user["user_id"] and current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            conn.close()
            return jsonify({"error": "You can only remove your own push subscriptions"}), 403

        conn.execute(
            "UPDATE push_subscriptions SET is_active = 0 WHERE subscription_id = ?",
            (subscription_id,),
        )
        conn.commit()
        conn.close()

        return jsonify({
            "subscription_id": subscription_id,
            "removed": True,
            "message": "Push subscription removed. You will no longer receive notifications on this device.",
        }), 200


    @app.get("/users/<user_id>/push-subscriptions")
    def list_user_push_subscriptions(user_id):
        """List active push subscriptions for a user (without keys)."""
        current_user = g.current_user
        _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}

        if current_user["user_id"] != user_id and current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            return jsonify({"error": "Access denied"}), 403

        conn = get_conn()
        ensure_push_subscription_tables(conn)

        rows = conn.execute(
            """SELECT subscription_id, device_label, is_active, created_at, last_used_at
               FROM push_subscriptions
               WHERE user_id = ? AND is_active = 1
               ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()
        conn.close()

        return jsonify({
            "user_id": user_id,
            "push_enabled_server": _PUSH_ENABLED,
            "count": len(rows),
            "subscriptions": [dict(r) for r in rows],
        }), 200
