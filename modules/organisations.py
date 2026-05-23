"""
modules/organisations.py — Organisations brick

Covers:
  - Organisation CRUD (create, list-all-global-admin, admin-dashboard)
  - Operating-data export
  - Data-retention jobs (list, preview, execute)
  - Organisation reactivation
"""

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso
from modules.subscription_access import (
    classify_org_access_state,
    ensure_subscription_guard_tables,
    get_org_access_status_payload,
    get_subscription_for_access_guard,
)


# ── Exported helpers (used by data-retention routes and operating-data export) ─

def table_exists(conn, table_name):
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,)
    ).fetchone()
    return row is not None


def export_table_for_org(conn, table_name, organisation_id):
    if not table_exists(conn, table_name):
        return []

    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}

    if "organisation_id" not in cols:
        return []

    rows = conn.execute(
        f"SELECT * FROM {table_name} WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchall()

    return [dict(row) for row in rows]


def get_operating_data_tables_for_retention():
    return [
        "depots",
        "resources",
        "partners",
        "partner_addresses",
        "transactions",
        "ledger_entries",
        "balance_projection",
        "shared_transactions",
        "shared_transaction_partner_addresses",
        "temporary_user_access",
        "pending_approval_entries",
        "audit_events",
    ]


def count_org_rows_for_table(conn, table_name, organisation_id):
    if not table_exists(conn, table_name):
        return 0

    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}

    if "organisation_id" not in cols:
        return 0

    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM {table_name} WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchone()

    return row["c"] if row else 0


# ── Route registration ─────────────────────────────────────────────────────────

def register_organisation_routes(app):

    _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}

    @app.post("/organisations")
    def create_organisation():
        if g.current_user.get("role") not in _GLOBAL_ADMIN_ROLES:
            return jsonify({"error": "INSUFFICIENT_ROLE", "message": "Only Global Admin can create organisations."}), 403

        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()[:255]

        if not name:
            return jsonify({"error": "name is required"}), 400

        organisation_id = make_id("org")

        conn = get_conn()
        conn.execute(
            "INSERT INTO organisations (organisation_id, name, created_at) VALUES (?, ?, ?)",
            (organisation_id, name, now_iso())
        )

        audit_event(
            conn,
            entity_type="Organisation",
            entity_id=organisation_id,
            action="CREATE",
            summary=f"Created organisation: {name}",
            organisation_id=organisation_id
        )

        conn.commit()
        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "name": name
        }), 201


    @app.get("/organisations/<organisation_id>/admin-dashboard")
    def get_admin_dashboard(organisation_id):
        conn = get_conn()

        org = conn.execute(
            """
            SELECT organisation_id, name, created_at
            FROM organisations
            WHERE organisation_id = ?
            """,
            (organisation_id,)
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        status_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS item_count
            FROM pending_approval_entries
            WHERE organisation_id = ?
            GROUP BY status
            """,
            (organisation_id,)
        ).fetchall()

        counts = {
            "PENDING_APPROVAL": 0,
            "AWAITING_FIX": 0,
            "READY_TO_APPROVE": 0,
            "RESOLVED": 0,
            "REJECTED": 0,
        }

        for row in status_rows:
            counts[row["status"]] = row["item_count"]

        recent_open_rows = conn.execute(
            """
            SELECT
                pending_entry_id,
                entry_type,
                status,
                reason_code,
                reason_text,
                related_entity_type,
                related_entity_id,
                related_entity_name,
                resource_id,
                resource_name,
                direct_action_label,
                direct_action_target_id,
                source_record_id,
                submitted_by_display_name,
                created_at,
                updated_at
            FROM pending_approval_entries
            WHERE organisation_id = ?
              AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (organisation_id,)
        ).fetchall()

        recent_resolved_rows = conn.execute(
            """
            SELECT
                pending_entry_id,
                entry_type,
                status,
                reason_code,
                reason_text,
                related_entity_type,
                related_entity_id,
                related_entity_name,
                resource_id,
                resource_name,
                direct_action_label,
                direct_action_target_id,
                source_record_id,
                submitted_by_display_name,
                created_at,
                updated_at
            FROM pending_approval_entries
            WHERE organisation_id = ?
              AND status IN ('RESOLVED', 'REJECTED')
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 10
            """,
            (organisation_id,)
        ).fetchall()

        conn.close()

        open_count = (
            counts["PENDING_APPROVAL"]
            + counts["AWAITING_FIX"]
            + counts["READY_TO_APPROVE"]
        )

        return jsonify({
            "organisation_id": org["organisation_id"],
            "organisation_name": org["name"],
            "created_at": org["created_at"],
            "summary": {
                "open_count": open_count,
                "pending_approval_count": counts["PENDING_APPROVAL"],
                "awaiting_fix_count": counts["AWAITING_FIX"],
                "ready_to_approve_count": counts["READY_TO_APPROVE"],
                "resolved_count": counts["RESOLVED"],
                "rejected_count": counts["REJECTED"]
            },
            "recent_open_items": [dict(r) for r in recent_open_rows],
            "recent_resolved_items": [dict(r) for r in recent_resolved_rows]
        }), 200


    @app.get("/organisations/<organisation_id>/operating-data-export")
    def export_organisation_operating_data(organisation_id):
        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        access = get_org_access_status_payload(conn, organisation_id)

        if not access["normal_access_allowed"] and not access["exit_only_access_allowed"]:
            conn.close()
            return jsonify({
                "error": "Operating data export is no longer available",
                "organisation_id": organisation_id,
                "access_state": access["access_state"],
                "reason": access["reason"],
            }), 403

        export_tables = get_operating_data_tables_for_retention()

        exported_data = {}
        counts = {}

        for table in export_tables:
            rows = export_table_for_org(conn, table, organisation_id)
            exported_data[table] = rows
            counts[table] = len(rows)

        subscription = conn.execute(
            """
            SELECT *
            FROM organisation_subscriptions
            WHERE organisation_id = ?
            """,
            (organisation_id,)
        ).fetchone()

        conn.close()

        return jsonify({
            "export_type": "PALLET_PRO_OPERATING_DATA_EXPORT",
            "organisation_id": organisation_id,
            "organisation_name": org["name"],
            "generated_at": now_iso(),
            "access_state": access["access_state"],
            "normal_access_allowed": access["normal_access_allowed"],
            "exit_only_access_allowed": access["exit_only_access_allowed"],
            "operating_data_delete_after": access["subscription"]["operating_data_delete_after"] if access["subscription"] else None,
            "subscription": access["subscription"],
            "counts": counts,
            "data": exported_data,
            "rule": "During the unsubscribe retention window, an organisation may export operating data before scheduled deletion.",
        }), 200


    @app.get("/global-admin/data-retention-jobs")
    def list_data_retention_jobs():
        status = request.args.get("status")
        organisation_id = request.args.get("organisation_id")

        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        sql = """
            SELECT
                j.*,
                o.name AS organisation_name
            FROM data_retention_jobs j
            LEFT JOIN organisations o ON o.organisation_id = j.organisation_id
            WHERE 1 = 1
        """
        params = []

        if status:
            sql += " AND j.job_status = ?"
            params.append(status)

        if organisation_id:
            sql += " AND j.organisation_id = ?"
            params.append(organisation_id)

        sql += " ORDER BY j.scheduled_for ASC"

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        return jsonify({
            "count": len(rows),
            "items": [dict(row) for row in rows],
        }), 200


    @app.post("/global-admin/data-retention-preview")
    def preview_due_data_retention_jobs():
        body = request.get_json(silent=True) or {}
        as_of = body.get("as_of") or now_iso()

        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        jobs = conn.execute(
            """
            SELECT
                j.*,
                o.name AS organisation_name
            FROM data_retention_jobs j
            LEFT JOIN organisations o ON o.organisation_id = j.organisation_id
            WHERE j.job_status = 'SCHEDULED'
              AND j.scheduled_for <= ?
            ORDER BY j.scheduled_for ASC
            """,
            (as_of,)
        ).fetchall()

        previews = []

        for job in jobs:
            d = dict(job)

            if d["job_type"] == "DELETE_OPERATING_DATA":
                counts = {}
                total_rows = 0

                for table in get_operating_data_tables_for_retention():
                    c = count_org_rows_for_table(conn, table, d["organisation_id"])
                    counts[table] = c
                    total_rows += c

                d["preview"] = {
                    "delete_type": "OPERATING_DATA",
                    "destructive_action_required": True,
                    "would_delete_row_count": total_rows,
                    "table_counts": counts,
                    "safety_note": "Preview only. No rows were deleted.",
                }

            elif d["job_type"] == "DELETE_HISTORICAL_ACCOUNT_DATA":
                d["preview"] = {
                    "delete_type": "HISTORICAL_ACCOUNT_DATA",
                    "destructive_action_required": True,
                    "would_delete_row_count": 0,
                    "table_counts": {},
                    "safety_note": "Historical account deletion is not implemented in v0.1. Preview only.",
                }

            else:
                d["preview"] = {
                    "delete_type": "UNKNOWN",
                    "destructive_action_required": False,
                    "would_delete_row_count": 0,
                    "table_counts": {},
                    "safety_note": "Unknown job type. No action proposed.",
                }

            previews.append(d)

        conn.close()

        return jsonify({
            "preview_type": "DATA_RETENTION_DUE_JOBS_PREVIEW",
            "as_of": as_of,
            "due_job_count": len(previews),
            "items": previews,
            "rule": "This endpoint previews scheduled data retention deletion work only. It does not delete data.",
        }), 200


    @app.post("/global-admin/data-retention-execute")
    def execute_due_data_retention_jobs():
        body = request.get_json(silent=True) or {}
        confirmation_text = (body.get("confirmation_text") or "").strip()
        organisation_id = body.get("organisation_id")
        as_of = body.get("as_of") or now_iso()
        executed_by_display_name = (body.get("executed_by_display_name") or "Global Admin").strip()

        required_confirmation = "DELETE OPERATING DATA"

        if confirmation_text != required_confirmation:
            return jsonify({
                "error": "Confirmation text is required before deleting operating data",
                "required_confirmation_text": required_confirmation,
                "received_confirmation_text": confirmation_text,
                "rule": "This is a destructive action. It will not run without the exact confirmation phrase.",
            }), 400

        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        sql = """
            SELECT
                j.*,
                o.name AS organisation_name
            FROM data_retention_jobs j
            LEFT JOIN organisations o ON o.organisation_id = j.organisation_id
            WHERE j.job_status = 'SCHEDULED'
              AND j.job_type = 'DELETE_OPERATING_DATA'
              AND j.scheduled_for <= ?
        """
        params = [as_of]

        if organisation_id:
            sql += " AND j.organisation_id = ?"
            params.append(organisation_id)

        sql += " ORDER BY j.scheduled_for ASC"

        jobs = conn.execute(sql, params).fetchall()

        executed_jobs = []

        for job in jobs:
            job_dict = dict(job)
            org_id = job_dict["organisation_id"]

            table_counts_before = {}
            table_counts_deleted = {}
            total_deleted = 0

            for table in get_operating_data_tables_for_retention():
                before_count = count_org_rows_for_table(conn, table, org_id)
                table_counts_before[table] = before_count

                if before_count == 0:
                    table_counts_deleted[table] = 0
                    continue

                conn.execute(
                    f"DELETE FROM {table} WHERE organisation_id = ?",
                    (org_id,)
                )

                after_count = count_org_rows_for_table(conn, table, org_id)
                deleted_count = before_count - after_count
                table_counts_deleted[table] = deleted_count
                total_deleted += deleted_count

            completed_at = now_iso()

            conn.execute(
                """
                UPDATE data_retention_jobs
                SET job_status = ?,
                    completed_at = ?
                WHERE data_retention_job_id = ?
                """,
                ("COMPLETED", completed_at, job_dict["data_retention_job_id"])
            )

            audit_event(
                conn,
                entity_type="DataRetentionJob",
                entity_id=job_dict["data_retention_job_id"],
                action="EXECUTE_DELETE_OPERATING_DATA",
                summary=f"Operating data deletion executed for unsubscribed organisation. Rows deleted: {total_deleted}.",
                organisation_id=org_id,
            )

            executed_jobs.append({
                "data_retention_job_id": job_dict["data_retention_job_id"],
                "organisation_id": org_id,
                "organisation_name": job_dict.get("organisation_name"),
                "job_type": job_dict["job_type"],
                "job_status": "COMPLETED",
                "completed_at": completed_at,
                "rows_deleted_total": total_deleted,
                "table_counts_before": table_counts_before,
                "table_counts_deleted": table_counts_deleted,
            })

        conn.commit()
        conn.close()

        return jsonify({
            "execution_type": "DATA_RETENTION_DELETE_OPERATING_DATA",
            "as_of": as_of,
            "executed_by_display_name": executed_by_display_name,
            "executed_job_count": len(executed_jobs),
            "executed_jobs": executed_jobs,
            "rule": "Only operating data is deleted by this endpoint. Historical organisation/account records are retained separately according to the 7-year retention rule.",
        }), 200


    @app.post("/organisations/<organisation_id>/reactivate")
    def reactivate_organisation(organisation_id):
        body = request.get_json(silent=True) or {}
        reactivated_by_display_name = (body.get("reactivated_by_display_name") or "Org Admin").strip()
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

        sub = get_subscription_for_access_guard(conn, organisation_id)

        if not sub:
            conn.close()
            return jsonify({
                "error": "Organisation has no subscription cancellation record",
                "organisation_id": organisation_id,
            }), 400

        access = classify_org_access_state(sub)

        if access["access_state"] == "ACTIVE":
            conn.close()
            return jsonify({
                "organisation_id": organisation_id,
                "organisation_name": org["name"],
                "subscription_status": sub["subscription_status"],
                "message": "Organisation is already active.",
            }), 200

        if access["access_state"] != "CANCELLED_WITHIN_RETENTION":
            conn.close()
            return jsonify({
                "error": "Organisation cannot be reactivated through the simple reactivation flow",
                "organisation_id": organisation_id,
                "access_state": access["access_state"],
                "reason": "The operating data retention window has ended or exit access is no longer available.",
            }), 409

        ts = now_iso()

        conn.execute(
            """
            UPDATE organisation_subscriptions
            SET subscription_mode = ?,
                subscription_status = ?,
                billing_status = ?,
                do_not_bill = ?,
                unsubscribed_at = NULL,
                unsubscribed_by_display_name = NULL,
                operating_data_delete_after = NULL,
                historical_data_delete_after = NULL,
                updated_at = ?
            WHERE organisation_id = ?
            """,
            (
                "STANDARD",
                "ACTIVE",
                "BILLABLE",
                0,
                ts,
                organisation_id,
            )
        )

        conn.execute(
            """
            UPDATE data_retention_jobs
            SET job_status = ?,
                completed_at = ?
            WHERE organisation_id = ?
              AND job_status = 'SCHEDULED'
            """,
            (
                "CANCELLED",
                ts,
                organisation_id,
            )
        )

        audit_event(
            conn,
            entity_type="OrganisationSubscription",
            entity_id=organisation_id,
            action="REACTIVATE",
            summary="Organisation reactivated within retention window. Billing restored and scheduled retention jobs cancelled.",
            organisation_id=organisation_id,
        )

        conn.commit()

        sub2 = conn.execute(
            "SELECT * FROM organisation_subscriptions WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()

        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "organisation_name": org["name"],
            "subscription": dict(sub2),
            "reactivated_by_display_name": reactivated_by_display_name,
            "reason_text": reason_text,
            "billing_rule": "Organisation is active and billable again from reactivation.",
            "data_retention_rule": "Scheduled deletion jobs were cancelled because the organisation reactivated within the retention window.",
        }), 200


    @app.get("/global-admin/organisations")
    def list_all_organisations():
        conn = get_conn()
        rows = conn.execute(
            """
            SELECT o.organisation_id, o.name, o.created_at,
                   COUNT(u.user_id) AS user_count
            FROM organisations o
            LEFT JOIN user_accounts u ON u.organisation_id = o.organisation_id
            GROUP BY o.organisation_id
            ORDER BY o.name ASC
            """
        ).fetchall()
        conn.close()
        return jsonify({
            "count": len(rows),
            "organisations": [dict(r) for r in rows],
        }), 200
