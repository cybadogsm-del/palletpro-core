"""
modules/users.py — Users brick

Covers:
  - USER_ROLES / USER_ACCESS_STATUSES constants
  - Schema helpers: ensure_user_access_tables
  - Core helpers: get_user_account, record_user_access_event,
    build_user_access_policy
  - Routes: POST /global-admin/users,
    GET /organisations/<id>/users,
    GET /users/<id>/access-policy,
    POST /global-admin/users/<id>/access
"""

from datetime import datetime

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso
from modules.subscription_access import (
    count_active_permanent_users,
    ensure_org_user_cap_column,
    ensure_subscription_guard_tables,
    get_org_access_status_payload,
    ORG_SELF_SERVE_USER_LIMIT,
)


# ── Constants ──────────────────────────────────────────────────────────────────

USER_ROLES = {
    "SUPER_GLOBAL_ADMIN",
    "GLOBAL_ADMIN",
    "ORG_ADMIN",
    "USER",
    "TEMPORARY_USER",
}

USER_ACCESS_STATUSES = {
    "INVITED",
    "ACTIVE",
    "SUSPENDED",
    "EXPIRED",
}


# ── Exported helpers ───────────────────────────────────────────────────────────

def ensure_user_access_tables(conn):
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
    CREATE TABLE IF NOT EXISTS user_access_events (
        user_access_event_id TEXT PRIMARY KEY,
        organisation_id TEXT,
        user_id TEXT NOT NULL,
        action TEXT NOT NULL,
        summary TEXT NOT NULL,
        changed_by_display_name TEXT,
        created_at TEXT NOT NULL
    )
    """)


def get_user_account(conn, user_id):
    ensure_user_access_tables(conn)

    return conn.execute(
        """
        SELECT *
        FROM user_accounts
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchone()


def record_user_access_event(conn, user_id, organisation_id, action, summary, changed_by_display_name):
    ensure_user_access_tables(conn)

    conn.execute(
        """
        INSERT INTO user_access_events (
            user_access_event_id,
            organisation_id,
            user_id,
            action,
            summary,
            changed_by_display_name,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            make_id("uae"),
            organisation_id,
            user_id,
            action,
            summary,
            changed_by_display_name,
            now_iso(),
        )
    )


def build_user_access_policy(conn, user_row):
    role = user_row["role"]
    status = user_row["access_status"]
    organisation_id = user_row["organisation_id"]

    base = {
        "user_id": user_row["user_id"],
        "organisation_id": organisation_id,
        "display_name": user_row["display_name"],
        "email": user_row["email"],
        "role": role,
        "access_status": status,
        "can_use_platform": False,
        "can_use_org_operations": False,
        "can_manage_org_subscription": False,
        "can_manage_org_users": False,
        "can_manage_global_pricing": False,
        "can_run_billing_exports": False,
        "can_execute_data_retention": False,
        "can_change_subscription_modes": False,
        "can_view_exit_dashboard": False,
        "can_export_operating_data": False,
        "reason": None,
    }

    if status != "ACTIVE":
        base["reason"] = f"User access status is {status}."
        return base

    if role == "SUPER_GLOBAL_ADMIN":
        base.update({
            "can_use_platform": True,
            "can_use_org_operations": True,
            "can_manage_org_subscription": True,
            "can_manage_org_users": True,
            "can_manage_global_pricing": True,
            "can_run_billing_exports": True,
            "can_execute_data_retention": True,
            "can_change_subscription_modes": True,
            "can_view_exit_dashboard": True,
            "can_export_operating_data": True,
            "reason": "Super Global Admin has full platform control.",
        })
        return base

    if role == "GLOBAL_ADMIN":
        base.update({
            "can_use_platform": True,
            "can_manage_global_pricing": True,
            "can_run_billing_exports": True,
            "can_execute_data_retention": True,
            "can_change_subscription_modes": True,
            "reason": "Global Admin has platform administration access.",
        })
        return base

    if not organisation_id:
        base["reason"] = "Organisation-scoped user has no organisation_id."
        return base

    org_access = get_org_access_status_payload(conn, organisation_id)

    base["can_view_exit_dashboard"] = org_access["exit_only_access_allowed"]
    base["can_export_operating_data"] = org_access["exit_only_access_allowed"]

    if not org_access["normal_access_allowed"]:
        base["reason"] = org_access["reason"]
        return base

    if role == "ORG_ADMIN":
        base.update({
            "can_use_platform": True,
            "can_use_org_operations": True,
            "can_manage_org_subscription": True,
            "can_manage_org_users": True,
            "reason": "Org Admin has organisation administration access.",
        })
        return base

    if role == "USER":
        base.update({
            "can_use_platform": True,
            "can_use_org_operations": True,
            "reason": "Standard user has normal organisation operation access.",
        })
        return base

    if role == "TEMPORARY_USER":
        temp_id = user_row["temporary_user_access_id"]

        if not temp_id:
            base["reason"] = "Temporary user has no linked temporary access record."
            return base

        temp = conn.execute(
            """
            SELECT *
            FROM temporary_user_access
            WHERE temporary_user_access_id = ?
              AND organisation_id = ?
            """,
            (temp_id, organisation_id)
        ).fetchone()

        if not temp:
            base["reason"] = "Linked temporary access record not found."
            return base

        if temp["access_status"] != "ACTIVE":
            base["reason"] = f"Temporary access status is {temp['access_status']}."
            return base

        now_dt = datetime.fromisoformat(now_iso())
        starts = datetime.fromisoformat(temp["access_starts_at"])
        ends = datetime.fromisoformat(temp["access_ends_at"])

        if now_dt < starts:
            base["reason"] = "Temporary access has not started yet."
            return base

        if now_dt > ends:
            base["reason"] = "Temporary access has expired."
            return base

        base.update({
            "can_use_platform": True,
            "can_use_org_operations": True,
            "reason": "Temporary user has active temporary access.",
        })
        return base

    base["reason"] = "Role is not recognised."
    return base


# ── Route registration ─────────────────────────────────────────────────────────

def register_user_routes(app):

    @app.post("/global-admin/users")
    def create_user_account():
        body = request.get_json(silent=True) or {}

        organisation_id = body.get("organisation_id")
        display_name = (body.get("display_name") or "").strip()
        email = (body.get("email") or "").strip() or None
        mobile_number = (body.get("mobile_number") or "").strip() or None
        role = (body.get("role") or "").strip().upper()
        access_status = (body.get("access_status") or "ACTIVE").strip().upper()
        temporary_user_access_id = body.get("temporary_user_access_id")
        created_by_display_name = (body.get("created_by_display_name") or "Global Admin").strip()
        confirmation_text = (body.get("confirmation_text") or "").strip()
        access_method = (body.get("access_method") or "").strip().upper()

        _VALID_ACCESS_METHODS = {"MOBILE", "TABLET", "DESKTOP", "BOTH"}

        required_confirmation = "CREATE USER"

        if confirmation_text != required_confirmation:
            return jsonify({
                "error": "Confirmation text is required before creating a user",
                "required_confirmation_text": required_confirmation,
                "received_confirmation_text": confirmation_text,
            }), 400

        if not display_name:
            return jsonify({"error": "display_name is required"}), 400

        if role not in USER_ROLES:
            return jsonify({"error": "Invalid role", "allowed_roles": sorted(USER_ROLES)}), 400

        # ── Access method validation ──────────────────────────────────────────
        if not access_method or access_method not in _VALID_ACCESS_METHODS:
            return jsonify({
                "error": "access_method is required",
                "allowed": sorted(_VALID_ACCESS_METHODS),
                "hint": "MOBILE=phone/SMS, TABLET=wifi tablet/email, DESKTOP=email (admin only), BOTH=phone+email",
            }), 400

        if access_method in ("MOBILE", "BOTH") and not mobile_number:
            return jsonify({"error": "mobile_number is required for MOBILE or BOTH access"}), 400

        if access_method in ("TABLET", "DESKTOP", "BOTH") and not email:
            return jsonify({"error": "email is required for TABLET, DESKTOP, or BOTH access"}), 400

        if access_method == "DESKTOP" and role not in ("GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN", "ORG_ADMIN"):
            return jsonify({
                "error": "DESKTOP_ADMIN_ONLY",
                "message": "Desktop-only access is reserved for ORG_ADMIN or Global Admin roles. "
                           "Field users need a mobile or tablet to use Pallet Pro.",
            }), 400

        if access_status not in USER_ACCESS_STATUSES:
            return jsonify({"error": "Invalid access_status", "allowed_statuses": sorted(USER_ACCESS_STATUSES)}), 400

        if role in ("ORG_ADMIN", "USER", "TEMPORARY_USER") and not organisation_id:
            return jsonify({"error": "organisation_id is required for organisation-scoped users"}), 400

        if role == "TEMPORARY_USER" and not temporary_user_access_id:
            return jsonify({"error": "temporary_user_access_id is required for TEMPORARY_USER"}), 400

        conn = get_conn()
        ensure_user_access_tables(conn)
        ensure_subscription_guard_tables(conn)
        ensure_org_user_cap_column(conn)

        if organisation_id:
            org = conn.execute(
                "SELECT * FROM organisations WHERE organisation_id = ?",
                (organisation_id,)
            ).fetchone()

            if not org:
                conn.close()
                return jsonify({"error": "Organisation not found"}), 404

            if role in ("ORG_ADMIN", "USER"):
                sub = conn.execute(
                    "SELECT selected_user_count FROM organisation_subscriptions WHERE organisation_id = ?",
                    (organisation_id,)
                ).fetchone()
                cap = sub["selected_user_count"] if sub and sub["selected_user_count"] is not None else None
                if cap is not None:
                    active_count = count_active_permanent_users(conn, organisation_id)
                    if active_count >= cap:
                        conn.close()
                        can_self_serve = cap < ORG_SELF_SERVE_USER_LIMIT
                        return jsonify({
                            "error": "USER_CAP_REACHED",
                            "dialog": {
                                "title": "User limit reached",
                                "message": (
                                    f"This organisation has {active_count} active user{'s' if active_count != 1 else ''} "
                                    f"and is currently set to a limit of {cap}. "
                                    + (
                                        f"You can increase your user count up to {ORG_SELF_SERVE_USER_LIMIT} from your subscription page."
                                        if can_self_serve else
                                        "Your plan has a custom user limit set by Pallet Pro. Please contact Pallet Pro to increase it."
                                    )
                                ),
                                "primary_action": {
                                    "label": "Go to Subscription",
                                    "route": f"/organisations/{organisation_id}/subscription-dashboard",
                                    "action_type": "NAVIGATE",
                                },
                            },
                            "current_active_users": active_count,
                            "selected_user_count": cap,
                            "self_serve_limit": ORG_SELF_SERVE_USER_LIMIT,
                            "can_self_serve_increase": can_self_serve,
                        }), 403

        if temporary_user_access_id:
            temp = conn.execute(
                """
                SELECT *
                FROM temporary_user_access
                WHERE temporary_user_access_id = ?
                  AND organisation_id = ?
                """,
                (temporary_user_access_id, organisation_id)
            ).fetchone()

            if not temp:
                conn.close()
                return jsonify({"error": "Temporary user access record not found for this organisation"}), 404

        from modules.password_auth import ensure_password_auth_columns, generate_setup_token
        ensure_password_auth_columns(conn)

        user_id = make_id("usr")
        ts = now_iso()

        conn.execute(
            """
            INSERT INTO user_accounts (
                user_id,
                organisation_id,
                display_name,
                email,
                mobile_number,
                role,
                access_status,
                temporary_user_access_id,
                created_by_display_name,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                organisation_id,
                display_name,
                email,
                mobile_number,
                role,
                access_status,
                temporary_user_access_id,
                created_by_display_name,
                ts,
                ts,
            )
        )

        setup_token = generate_setup_token(conn, user_id)

        record_user_access_event(
            conn,
            user_id=user_id,
            organisation_id=organisation_id,
            action="CREATE_USER",
            summary=f"User {display_name} created with role {role}.",
            changed_by_display_name=created_by_display_name,
        )

        audit_event(
            conn,
            entity_type="UserAccount",
            entity_id=user_id,
            action="CREATE",
            summary=f"User account created with role {role}.",
            organisation_id=organisation_id,
        )

        conn.commit()

        user = get_user_account(conn, user_id)
        policy = build_user_access_policy(conn, user)

        conn.close()

        # ── Invite delivery ───────────────────────────────────────────────────
        # SMS for MOBILE / BOTH; email for TABLET / DESKTOP / BOTH.
        sms_sent = False
        email_sent = False

        if access_method in ("MOBILE", "BOTH") and mobile_number:
            from modules.sms import send_invite_sms
            sms_sent = send_invite_sms(
                to=mobile_number,
                setup_token=setup_token,
                invited_by=created_by_display_name,
            )

        if access_method in ("TABLET", "DESKTOP", "BOTH") and email:
            from modules.email import send_invite_email
            email_sent = send_invite_email(
                to=email,
                setup_token=setup_token,
                display_name=display_name,
                invited_by=created_by_display_name,
            )

        delivery_parts = []
        if sms_sent:
            delivery_parts.append("SMS sent to mobile")
        if email_sent:
            delivery_parts.append("email sent")
        delivery_note = (
            " and ".join(delivery_parts).capitalize() + "."
            if delivery_parts else
            "No invite sent (credentials not configured). Share this setup token securely — "
            "user must call POST /auth/set-password. Expires in 7 days."
        )

        return jsonify({
            "user": dict(user),
            "access_policy": policy,
            "setup_token": setup_token,
            "setup_token_note": delivery_note,
            "sms_sent": sms_sent,
            "email_sent": email_sent,
            "rule": "User access is role-based and organisation-aware.",
        }), 201


    @app.get("/organisations/<organisation_id>/users")
    def list_organisation_users(organisation_id):
        conn = get_conn()
        ensure_user_access_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        rows = conn.execute(
            """
            SELECT *
            FROM user_accounts
            WHERE organisation_id = ?
            ORDER BY role ASC, display_name ASC
            """,
            (organisation_id,)
        ).fetchall()

        items = []
        for row in rows:
            items.append({
                "user": dict(row),
                "access_policy": build_user_access_policy(conn, row),
            })

        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "organisation_name": org["name"],
            "count": len(items),
            "items": items,
        }), 200


    @app.get("/users/<user_id>/access-policy")
    def get_user_access_policy(user_id):
        conn = get_conn()
        ensure_user_access_tables(conn)

        user = get_user_account(conn, user_id)

        if not user:
            conn.close()
            return jsonify({"error": "User not found"}), 404

        policy = build_user_access_policy(conn, user)

        conn.close()

        return jsonify(policy), 200


    @app.post("/global-admin/users/<user_id>/access")
    def update_user_access(user_id):
        body = request.get_json(silent=True) or {}

        role = (body.get("role") or "").strip().upper() if "role" in body else None
        access_status = (body.get("access_status") or "").strip().upper() if "access_status" in body else None
        temporary_user_access_id = body.get("temporary_user_access_id") if "temporary_user_access_id" in body else None
        changed_by_display_name = (body.get("changed_by_display_name") or "Global Admin").strip()
        confirmation_text = (body.get("confirmation_text") or "").strip()

        required_confirmation = "CHANGE USER ACCESS"

        if confirmation_text != required_confirmation:
            return jsonify({
                "error": "Confirmation text is required before changing user access",
                "required_confirmation_text": required_confirmation,
                "received_confirmation_text": confirmation_text,
            }), 400

        conn = get_conn()
        ensure_user_access_tables(conn)

        user = get_user_account(conn, user_id)

        if not user:
            conn.close()
            return jsonify({"error": "User not found"}), 404

        updates = {}

        if role is not None:
            if role not in USER_ROLES:
                conn.close()
                return jsonify({"error": "Invalid role", "allowed_roles": sorted(USER_ROLES)}), 400
            updates["role"] = role

        if access_status is not None:
            if access_status not in USER_ACCESS_STATUSES:
                conn.close()
                return jsonify({"error": "Invalid access_status", "allowed_statuses": sorted(USER_ACCESS_STATUSES)}), 400
            updates["access_status"] = access_status

        if "temporary_user_access_id" in body:
            updates["temporary_user_access_id"] = temporary_user_access_id

        if not updates:
            conn.close()
            return jsonify({"error": "No user access changes supplied"}), 400

        updates["updated_at"] = now_iso()

        set_clause = ", ".join([f"{key} = ?" for key in updates.keys()])
        values = list(updates.values())
        values.append(user_id)

        conn.execute(
            f"""
            UPDATE user_accounts
            SET {set_clause}
            WHERE user_id = ?
            """,
            values
        )

        record_user_access_event(
            conn,
            user_id=user_id,
            organisation_id=user["organisation_id"],
            action="UPDATE_ACCESS",
            summary=f"User access updated by {changed_by_display_name}.",
            changed_by_display_name=changed_by_display_name,
        )

        audit_event(
            conn,
            entity_type="UserAccount",
            entity_id=user_id,
            action="UPDATE_ACCESS",
            summary="User access updated.",
            organisation_id=user["organisation_id"],
        )

        conn.commit()

        updated_user = get_user_account(conn, user_id)
        policy = build_user_access_policy(conn, updated_user)

        conn.close()

        return jsonify({
            "user": dict(updated_user),
            "access_policy": policy,
            "changed_by_display_name": changed_by_display_name,
            "rule": "User role/access changes are audited.",
        }), 200
