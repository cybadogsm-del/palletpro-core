"""
Org Subscription brick — organisation-level subscription management routes.

Covers:
  - Org self-serve user count selection
  - Global Admin override of user count
  - Unsubscribe (cancels billing, schedules data retention jobs)
  - Organisation access-status query
  - Organisation exit dashboard

Exports:
    register_org_subscription_routes(app)
"""

from datetime import datetime, timedelta

from flask import jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso
from modules.subscription_access import (
    ORG_SELF_SERVE_USER_LIMIT,
    count_active_permanent_users,
    ensure_org_user_cap_column,
    ensure_subscription_guard_tables,
    get_or_create_subscription,
    get_org_access_status_payload,
)
from modules.users import ensure_user_access_tables


def register_org_subscription_routes(app):

    # === USER CAP SELF-SERVE V0.1 ===

    @app.post("/organisations/<organisation_id>/subscription/select-users")
    def org_select_user_count(organisation_id):
        body = request.get_json(silent=True) or {}
        selected_user_count = body.get("selected_user_count")
        changed_by_display_name = (body.get("changed_by_display_name") or "Org Admin").strip()

        if not isinstance(selected_user_count, int) or selected_user_count < 1:
            return jsonify({"error": "selected_user_count must be an integer of 1 or more"}), 400

        if selected_user_count > ORG_SELF_SERVE_USER_LIMIT:
            return jsonify({
                "error": f"Self-serve user selection is limited to {ORG_SELF_SERVE_USER_LIMIT} users.",
                "message": f"For {ORG_SELF_SERVE_USER_LIMIT + 1}+ users, contact Pallet Pro for a tailored plan.",
                "requested": selected_user_count,
                "self_serve_limit": ORG_SELF_SERVE_USER_LIMIT,
            }), 400

        conn = get_conn()
        ensure_subscription_guard_tables(conn)
        ensure_org_user_cap_column(conn)
        ensure_user_access_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?", (organisation_id,)
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        active_count = count_active_permanent_users(conn, organisation_id)
        if selected_user_count < active_count:
            conn.close()
            return jsonify({
                "error": "Cannot set user count below current active user count",
                "current_active_users": active_count,
                "requested_user_count": selected_user_count,
            }), 400

        get_or_create_subscription(conn, organisation_id)
        conn.execute(
            "UPDATE organisation_subscriptions SET selected_user_count = ?, updated_at = ? WHERE organisation_id = ?",
            (selected_user_count, now_iso(), organisation_id)
        )

        audit_event(
            conn,
            entity_type="OrganisationSubscription",
            entity_id=organisation_id,
            action="SELECT_USER_COUNT",
            summary=f"Org selected {selected_user_count} user(s). Changed by {changed_by_display_name}.",
            organisation_id=organisation_id,
        )

        conn.commit()
        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "selected_user_count": selected_user_count,
            "self_serve_limit": ORG_SELF_SERVE_USER_LIMIT,
            "current_active_users": active_count,
            "message": f"User count set to {selected_user_count}. This is your billing quantity and user cap.",
        }), 200

    @app.post("/global-admin/organisations/<organisation_id>/set-user-count")
    def global_admin_set_user_count(organisation_id):
        body = request.get_json(silent=True) or {}
        selected_user_count = body.get("selected_user_count")
        changed_by_display_name = (body.get("changed_by_display_name") or "Global Admin").strip()

        if not isinstance(selected_user_count, int) or selected_user_count < 1:
            return jsonify({"error": "selected_user_count must be an integer of 1 or more"}), 400

        conn = get_conn()
        ensure_subscription_guard_tables(conn)
        ensure_org_user_cap_column(conn)
        ensure_user_access_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?", (organisation_id,)
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        active_count = count_active_permanent_users(conn, organisation_id)
        if selected_user_count < active_count:
            conn.close()
            return jsonify({
                "error": "Cannot set user count below current active user count",
                "current_active_users": active_count,
                "requested_user_count": selected_user_count,
            }), 400

        get_or_create_subscription(conn, organisation_id)
        conn.execute(
            "UPDATE organisation_subscriptions SET selected_user_count = ?, updated_at = ? WHERE organisation_id = ?",
            (selected_user_count, now_iso(), organisation_id)
        )

        audit_event(
            conn,
            entity_type="OrganisationSubscription",
            entity_id=organisation_id,
            action="GLOBAL_ADMIN_SET_USER_COUNT",
            summary=f"Global Admin set user count to {selected_user_count} for org {organisation_id}. Changed by {changed_by_display_name}.",
            organisation_id=organisation_id,
        )

        conn.commit()
        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "selected_user_count": selected_user_count,
            "current_active_users": active_count,
            "is_custom_plan": selected_user_count > ORG_SELF_SERVE_USER_LIMIT,
            "message": f"User count set to {selected_user_count} by Global Admin.",
        }), 200

    # === UNSUBSCRIBE ===

    @app.post("/organisations/<organisation_id>/unsubscribe")
    def unsubscribe_organisation(organisation_id):
        body = request.get_json(silent=True) or {}
        unsubscribed_by_display_name = (body.get("unsubscribed_by_display_name") or "Org Admin").strip()
        reason_text = (body.get("reason_text") or "").strip() or None

        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        ts = now_iso()
        now_dt = datetime.fromisoformat(ts)
        operating_delete_after = (now_dt + timedelta(days=7)).isoformat()
        historical_delete_after = (now_dt + timedelta(days=365 * 7)).isoformat()

        get_or_create_subscription(conn, organisation_id)

        conn.execute(
            """
            UPDATE organisation_subscriptions
            SET subscription_mode = ?,
                subscription_status = ?,
                billing_status = ?,
                do_not_bill = ?,
                unsubscribed_at = ?,
                unsubscribed_by_display_name = ?,
                operating_data_delete_after = ?,
                historical_data_delete_after = ?,
                updated_at = ?
            WHERE organisation_id = ?
            """,
            (
                "CANCELLED",
                "CANCELLED",
                "DO_NOT_BILL",
                1,
                ts,
                unsubscribed_by_display_name,
                operating_delete_after,
                historical_delete_after,
                ts,
                organisation_id,
            )
        )

        unsubscribe_event_id = make_id("unsub")
        conn.execute(
            """
            INSERT INTO unsubscribe_events (
                unsubscribe_event_id,
                organisation_id,
                unsubscribed_by_display_name,
                reason_text,
                billing_stopped_at,
                operating_data_delete_after,
                historical_data_delete_after,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                unsubscribe_event_id,
                organisation_id,
                unsubscribed_by_display_name,
                reason_text,
                ts,
                operating_delete_after,
                historical_delete_after,
                ts,
            )
        )

        conn.execute(
            """
            INSERT INTO data_retention_jobs (
                data_retention_job_id,
                organisation_id,
                job_type,
                scheduled_for,
                job_status,
                created_at,
                completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("ret"),
                organisation_id,
                "DELETE_OPERATING_DATA",
                operating_delete_after,
                "SCHEDULED",
                ts,
                None,
            )
        )

        conn.execute(
            """
            INSERT INTO data_retention_jobs (
                data_retention_job_id,
                organisation_id,
                job_type,
                scheduled_for,
                job_status,
                created_at,
                completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("ret"),
                organisation_id,
                "DELETE_HISTORICAL_ACCOUNT_DATA",
                historical_delete_after,
                "SCHEDULED",
                ts,
                None,
            )
        )

        audit_event(
            conn,
            entity_type="OrganisationSubscription",
            entity_id=organisation_id,
            action="UNSUBSCRIBE",
            summary="Organisation unsubscribed. Billing stopped immediately and data retention jobs scheduled.",
            organisation_id=organisation_id,
        )

        conn.commit()

        sub = conn.execute(
            "SELECT * FROM organisation_subscriptions WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()

        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "organisation_name": org["name"],
            "unsubscribe_event_id": unsubscribe_event_id,
            "subscription": dict(sub),
            "billing_rule": "This organisation is marked DO_NOT_BILL and must not be included in future billing exports.",
            "operating_data_rule": "Operating data is retained for 7 days after unsubscribe, then scheduled for deletion.",
            "historical_data_rule": "Minimal historical organisation and billing records are retained for 7 years, then scheduled for deletion.",
        }), 200

    # === UNSUBSCRIBED ORG ACCESS GUARD ===

    @app.get("/organisations/<organisation_id>/access-status")
    def get_organisation_access_status(organisation_id):
        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        payload = get_org_access_status_payload(conn, organisation_id)
        payload["organisation_name"] = org["name"]

        conn.close()
        return jsonify(payload), 200

    @app.get("/organisations/<organisation_id>/exit-dashboard")
    def get_organisation_exit_dashboard(organisation_id):
        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        payload = get_org_access_status_payload(conn, organisation_id)

        if payload["access_state"] == "ACTIVE":
            conn.close()
            return jsonify({
                "organisation_id": organisation_id,
                "organisation_name": org["name"],
                "access_state": "ACTIVE",
                "message": "Organisation is active. Exit dashboard is not required.",
            }), 200

        if not payload["exit_only_access_allowed"]:
            conn.close()
            return jsonify({
                "organisation_id": organisation_id,
                "organisation_name": org["name"],
                "access_state": payload["access_state"],
                "message": "Exit access is no longer available.",
                "reason": payload["reason"],
            }), 403

        conn.close()
        return jsonify({
            "organisation_id": organisation_id,
            "organisation_name": org["name"],
            "access_state": payload["access_state"],
            "message": "Subscription is cancelled. Billing has stopped. Limited exit access is available during the operating data retention window.",
            "normal_access_allowed": False,
            "billing_stopped": True,
            "operating_data_delete_after": payload["subscription"]["operating_data_delete_after"],
            "historical_data_delete_after": payload["subscription"]["historical_data_delete_after"],
            "allowed_exit_actions": payload["allowed_exit_actions"],
            "blocked_operational_actions": payload["blocked_operational_actions"],
        }), 200
