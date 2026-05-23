"""
Access Operations brick — global-admin dashboards, health checks,
system control panel, and metrics snapshots.

Exports:
    register_access_operations_routes(app)
"""

import json

from flask import jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso
from modules.sessions import (
    ensure_login_integrity_action_tables,
    ensure_login_integrity_tables,
)
from modules.subscription_access import ensure_subscription_guard_tables
from modules.users import ensure_user_access_tables


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def ensure_access_operations_snapshot_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS access_operations_metric_snapshots (
        access_operations_snapshot_id TEXT PRIMARY KEY,
        snapshot_status TEXT NOT NULL,
        captured_by_display_name TEXT NOT NULL,
        active_sessions_count INTEGER NOT NULL,
        open_login_integrity_action_count INTEGER NOT NULL,
        open_login_integrity_report_count INTEGER NOT NULL,
        due_retention_jobs_count INTEGER NOT NULL,
        active_temporary_access_count INTEGER NOT NULL,
        expired_temporary_access_count INTEGER NOT NULL,
        suspended_users_count INTEGER NOT NULL,
        expired_users_count INTEGER NOT NULL,
        do_not_bill_organisation_count INTEGER NOT NULL,
        health_status TEXT NOT NULL,
        advisory_summary TEXT NOT NULL,
        metrics_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)


# ---------------------------------------------------------------------------
# Metric collection helper
# ---------------------------------------------------------------------------

def collect_access_operations_metrics(conn):
    ensure_subscription_guard_tables(conn)
    ensure_user_access_tables(conn)
    ensure_login_integrity_tables(conn)
    ensure_login_integrity_action_tables(conn)
    ensure_access_operations_snapshot_tables(conn)

    active_sessions_count = conn.execute(
        "SELECT COUNT(*) AS c FROM user_sessions WHERE session_status = 'ACTIVE'"
    ).fetchone()["c"]

    open_actions_count = conn.execute(
        "SELECT COUNT(*) AS c FROM login_integrity_admin_actions WHERE action_status = 'OPEN'"
    ).fetchone()["c"]

    open_reports_count = conn.execute(
        "SELECT COUNT(*) AS c FROM login_integrity_events WHERE review_status IN ('OPEN', 'ACTION_REQUIRED', 'MONITORING')"
    ).fetchone()["c"]

    due_retention_jobs_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM data_retention_jobs
        WHERE job_status = 'SCHEDULED'
          AND scheduled_for <= ?
        """,
        (now_iso(),)
    ).fetchone()["c"]

    active_temp_access_count = conn.execute(
        "SELECT COUNT(*) AS c FROM temporary_user_access WHERE access_status = 'ACTIVE'"
    ).fetchone()["c"]

    expired_temp_access_count = conn.execute(
        "SELECT COUNT(*) AS c FROM temporary_user_access WHERE access_status = 'EXPIRED'"
    ).fetchone()["c"]

    suspended_users_count = conn.execute(
        "SELECT COUNT(*) AS c FROM user_accounts WHERE access_status = 'SUSPENDED'"
    ).fetchone()["c"]

    expired_users_count = conn.execute(
        "SELECT COUNT(*) AS c FROM user_accounts WHERE access_status = 'EXPIRED'"
    ).fetchone()["c"]

    do_not_bill_orgs_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM organisation_subscriptions
        WHERE do_not_bill = 1
           OR billing_status = 'DO_NOT_BILL'
        """
    ).fetchone()["c"]

    health_status = "GREEN"
    advisory_parts = []

    if due_retention_jobs_count > 0:
        health_status = "AMBER"
        advisory_parts.append(f"{due_retention_jobs_count} due retention job(s) need review.")

    if open_actions_count > 0:
        health_status = "AMBER"
        advisory_parts.append(f"{open_actions_count} open login-integrity action(s) need Global Admin review.")

    if open_reports_count > 5:
        health_status = "AMBER"
        advisory_parts.append(f"{open_reports_count} login-integrity report(s) are still open/action-required/monitoring.")

    if expired_temp_access_count > 0:
        advisory_parts.append(f"{expired_temp_access_count} expired temporary access record(s) exist.")

    if due_retention_jobs_count > 10 or open_actions_count > 10:
        health_status = "RED"
        advisory_parts.append("High unresolved admin workload detected.")

    if not advisory_parts:
        advisory_parts.append("Access operations look healthy.")

    metrics = {
        "active_sessions_count": active_sessions_count,
        "open_login_integrity_action_count": open_actions_count,
        "open_login_integrity_report_count": open_reports_count,
        "due_retention_jobs_count": due_retention_jobs_count,
        "active_temporary_access_count": active_temp_access_count,
        "expired_temporary_access_count": expired_temp_access_count,
        "suspended_users_count": suspended_users_count,
        "expired_users_count": expired_users_count,
        "do_not_bill_organisation_count": do_not_bill_orgs_count,
        "health_status": health_status,
        "advisory_summary": " ".join(advisory_parts),
    }

    return metrics


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_access_operations_routes(app):

    @app.get("/global-admin/access-operations-dashboard")
    def get_access_operations_dashboard():
        conn = get_conn()
        ensure_user_access_tables(conn)
        ensure_login_integrity_tables(conn)
        ensure_login_integrity_action_tables(conn)
        ensure_subscription_guard_tables(conn)

        active_sessions = conn.execute(
            """
            SELECT
                s.*,
                u.display_name,
                u.email,
                u.role,
                o.name AS organisation_name
            FROM user_sessions s
            LEFT JOIN user_accounts u ON u.user_id = s.user_id
            LEFT JOIN organisations o ON o.organisation_id = s.organisation_id
            WHERE s.session_status = 'ACTIVE'
            ORDER BY s.last_seen_at DESC
            LIMIT 50
            """
        ).fetchall()

        open_login_actions = conn.execute(
            """
            SELECT
                a.*,
                e.risk_level,
                e.risk_score,
                e.event_type,
                u.display_name,
                u.email,
                u.role,
                o.name AS organisation_name
            FROM login_integrity_admin_actions a
            LEFT JOIN login_integrity_events e ON e.login_integrity_event_id = a.login_integrity_event_id
            LEFT JOIN user_accounts u ON u.user_id = a.user_id
            LEFT JOIN organisations o ON o.organisation_id = a.organisation_id
            WHERE a.action_status = 'OPEN'
            ORDER BY a.created_at ASC
            LIMIT 50
            """
        ).fetchall()

        temporary_access_summary = conn.execute(
            """
            SELECT
                access_status,
                COUNT(*) AS count,
                COALESCE(SUM(fee_cents), 0) AS fee_cents_total
            FROM temporary_user_access
            GROUP BY access_status
            ORDER BY access_status ASC
            """
        ).fetchall()

        user_status_summary = conn.execute(
            """
            SELECT
                role,
                access_status,
                COUNT(*) AS count
            FROM user_accounts
            GROUP BY role, access_status
            ORDER BY role ASC, access_status ASC
            """
        ).fetchall()

        retention_summary = conn.execute(
            """
            SELECT
                job_type,
                job_status,
                COUNT(*) AS count
            FROM data_retention_jobs
            GROUP BY job_type, job_status
            ORDER BY job_type ASC, job_status ASC
            """
        ).fetchall()

        login_report_summary = conn.execute(
            """
            SELECT
                risk_level,
                review_status,
                COUNT(*) AS count
            FROM login_integrity_events
            GROUP BY risk_level, review_status
            ORDER BY risk_level ASC, review_status ASC
            """
        ).fetchall()

        conn.close()

        return jsonify({
            "dashboard_type": "GLOBAL_ADMIN_ACCESS_OPERATIONS_DASHBOARD",
            "active_session_count": len(active_sessions),
            "open_login_integrity_action_count": len(open_login_actions),
            "active_sessions": [dict(row) for row in active_sessions],
            "open_login_integrity_actions": [dict(row) for row in open_login_actions],
            "temporary_access_summary": [dict(row) for row in temporary_access_summary],
            "user_status_summary": [dict(row) for row in user_status_summary],
            "retention_job_summary": [dict(row) for row in retention_summary],
            "login_report_summary": [dict(row) for row in login_report_summary],
            "rules": [
                "Global Admin sees access operations across sessions, users, temporary access, retention jobs, and login integrity actions.",
                "AI reports remain advisory only.",
                "Global Admin decides the course of action.",
            ],
        }), 200

    @app.get("/global-admin/access-operations-health")
    def get_access_operations_health():
        conn = get_conn()
        ensure_user_access_tables(conn)
        ensure_login_integrity_tables(conn)
        ensure_login_integrity_action_tables(conn)
        ensure_subscription_guard_tables(conn)

        active_sessions_count = conn.execute(
            "SELECT COUNT(*) AS c FROM user_sessions WHERE session_status = 'ACTIVE'"
        ).fetchone()["c"]

        open_actions_count = conn.execute(
            "SELECT COUNT(*) AS c FROM login_integrity_admin_actions WHERE action_status = 'OPEN'"
        ).fetchone()["c"]

        open_reports_count = conn.execute(
            "SELECT COUNT(*) AS c FROM login_integrity_events WHERE review_status IN ('OPEN', 'ACTION_REQUIRED', 'MONITORING')"
        ).fetchone()["c"]

        expired_temp_access_count = conn.execute(
            "SELECT COUNT(*) AS c FROM temporary_user_access WHERE access_status = 'EXPIRED'"
        ).fetchone()["c"]

        active_temp_access_count = conn.execute(
            "SELECT COUNT(*) AS c FROM temporary_user_access WHERE access_status = 'ACTIVE'"
        ).fetchone()["c"]

        due_retention_jobs_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM data_retention_jobs
            WHERE job_status = 'SCHEDULED'
              AND scheduled_for <= ?
            """,
            (now_iso(),)
        ).fetchone()["c"]

        suspended_users_count = conn.execute(
            "SELECT COUNT(*) AS c FROM user_accounts WHERE access_status = 'SUSPENDED'"
        ).fetchone()["c"]

        expired_users_count = conn.execute(
            "SELECT COUNT(*) AS c FROM user_accounts WHERE access_status = 'EXPIRED'"
        ).fetchone()["c"]

        status = "GREEN"
        reasons = []

        if due_retention_jobs_count > 0:
            status = "AMBER"
            reasons.append("There are due data-retention jobs awaiting review/execution.")

        if open_actions_count > 0:
            status = "AMBER"
            reasons.append("There are open login-integrity admin actions.")

        if open_reports_count > 5:
            status = "AMBER"
            reasons.append("There are multiple open login-integrity reports.")

        if due_retention_jobs_count > 10 or open_actions_count > 10:
            status = "RED"
            reasons.append("There is a high volume of due retention or login-integrity work.")

        if not reasons:
            reasons.append("Access operations are healthy.")

        conn.close()

        return jsonify({
            "health_type": "GLOBAL_ADMIN_ACCESS_OPERATIONS_HEALTH",
            "status": status,
            "reasons": reasons,
            "metrics": {
                "active_sessions_count": active_sessions_count,
                "open_login_integrity_action_count": open_actions_count,
                "open_login_integrity_report_count": open_reports_count,
                "active_temporary_access_count": active_temp_access_count,
                "expired_temporary_access_count": expired_temp_access_count,
                "due_retention_jobs_count": due_retention_jobs_count,
                "suspended_users_count": suspended_users_count,
                "expired_users_count": expired_users_count,
            },
            "rule": "This endpoint summarises access operations health for Global Admin triage.",
        }), 200

    @app.get("/global-admin/system-control-panel")
    def get_global_admin_system_control_panel():
        conn = get_conn()
        ensure_subscription_guard_tables(conn)
        ensure_user_access_tables(conn)
        ensure_login_integrity_tables(conn)
        ensure_login_integrity_action_tables(conn)

        active_sessions_count = conn.execute(
            "SELECT COUNT(*) AS c FROM user_sessions WHERE session_status = 'ACTIVE'"
        ).fetchone()["c"]

        open_login_actions_count = conn.execute(
            "SELECT COUNT(*) AS c FROM login_integrity_admin_actions WHERE action_status = 'OPEN'"
        ).fetchone()["c"]

        due_retention_jobs_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM data_retention_jobs
            WHERE job_status = 'SCHEDULED'
              AND scheduled_for <= ?
            """,
            (now_iso(),)
        ).fetchone()["c"]

        do_not_bill_orgs_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM organisation_subscriptions
            WHERE do_not_bill = 1
               OR billing_status = 'DO_NOT_BILL'
            """
        ).fetchone()["c"]

        active_temp_access_count = conn.execute(
            "SELECT COUNT(*) AS c FROM temporary_user_access WHERE access_status = 'ACTIVE'"
        ).fetchone()["c"]

        expired_temp_access_count = conn.execute(
            "SELECT COUNT(*) AS c FROM temporary_user_access WHERE access_status = 'EXPIRED'"
        ).fetchone()["c"]

        latest_billing_export = conn.execute(
            """
            SELECT *
            FROM billing_export_runs
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

        conn.close()

        control_panel_status = "GREEN"
        warnings = []

        if due_retention_jobs_count > 0:
            control_panel_status = "AMBER"
            warnings.append("Due retention jobs need Global Admin review.")

        if open_login_actions_count > 0:
            control_panel_status = "AMBER"
            warnings.append("Open login-integrity actions need review.")

        if due_retention_jobs_count > 10 or open_login_actions_count > 10:
            control_panel_status = "RED"
            warnings.append("High volume of unresolved admin work.")

        return jsonify({
            "panel_type": "GLOBAL_ADMIN_SYSTEM_CONTROL_PANEL",
            "status": control_panel_status,
            "warnings": warnings,
            "metrics": {
                "active_sessions_count": active_sessions_count,
                "open_login_integrity_action_count": open_login_actions_count,
                "due_retention_jobs_count": due_retention_jobs_count,
                "do_not_bill_organisation_count": do_not_bill_orgs_count,
                "active_temporary_access_count": active_temp_access_count,
                "expired_temporary_access_count": expired_temp_access_count,
            },
            "latest_billing_export": dict(latest_billing_export) if latest_billing_export else None,
            "admin_surfaces": [
                {
                    "key": "pricing_dashboard",
                    "label": "Pricing Dashboard",
                    "route": "/global-admin/pricing-dashboard",
                    "purpose": "Review pricing philosophy, pricing table, plans, temp user fees, and billing settings.",
                },
                {
                    "key": "billing_export_preview",
                    "label": "Billing Export Preview",
                    "route": "/global-admin/billing-export-preview",
                    "purpose": "Preview biller export without marking fees as billed.",
                },
                {
                    "key": "billing_export_finalise",
                    "label": "Billing Export Finalise",
                    "route": "/global-admin/billing-export-finalise",
                    "purpose": "Finalise billing export, snapshot line items, and mark included temp fees as billed.",
                },
                {
                    "key": "access_operations_dashboard",
                    "label": "Access Operations Dashboard",
                    "route": "/global-admin/access-operations-dashboard",
                    "purpose": "Review sessions, user status, temporary access, retention, and login-integrity work.",
                },
                {
                    "key": "access_operations_health",
                    "label": "Access Operations Health",
                    "route": "/global-admin/access-operations-health",
                    "purpose": "Quick GREEN/AMBER/RED access operations health check.",
                },
                {
                    "key": "login_integrity_dashboard",
                    "label": "Login Integrity Dashboard",
                    "route": "/global-admin/login-integrity-dashboard",
                    "purpose": "Review AI-advisory login integrity reports for Global Admin eyes only.",
                },
                {
                    "key": "login_integrity_action_queue",
                    "label": "Login Integrity Action Queue",
                    "route": "/global-admin/login-integrity-actions",
                    "purpose": "Review open and completed Global Admin courses of action.",
                },
                {
                    "key": "data_retention_preview",
                    "label": "Data Retention Preview",
                    "route": "/global-admin/data-retention-preview",
                    "purpose": "Preview scheduled deletion work before running destructive actions.",
                },
                {
                    "key": "data_retention_execute",
                    "label": "Data Retention Execute",
                    "route": "/global-admin/data-retention-execute",
                    "purpose": "Run safety-gated operating-data deletion.",
                },
            ],
            "rules": [
                "Global Admin controls the course of action.",
                "AI reports are advisory only.",
                "Billing exports must exclude unsubscribed and do-not-bill organisations.",
                "Pallet Pro must be easy to unsubscribe from.",
                "One worker, one login, one active device for standard and temporary users.",
                "Admins may use multiple devices, but activity is logged and visible to Global Admin.",
            ],
        }), 200

    @app.post("/global-admin/access-operations-metrics-snapshot")
    def create_access_operations_metrics_snapshot():
        body = request.get_json(silent=True) or {}
        captured_by_display_name = (body.get("captured_by_display_name") or "Global Admin").strip()
        confirmation_text = (body.get("confirmation_text") or "").strip()

        required_confirmation = "CAPTURE ACCESS OPERATIONS SNAPSHOT"

        if confirmation_text != required_confirmation:
            return jsonify({
                "error": "Confirmation text is required before capturing an access operations snapshot",
                "required_confirmation_text": required_confirmation,
                "received_confirmation_text": confirmation_text,
                "rule": "Snapshots create an audit-friendly point-in-time operations record.",
            }), 400

        conn = get_conn()
        metrics = collect_access_operations_metrics(conn)

        snapshot_id = make_id("aoms")
        ts = now_iso()

        conn.execute(
            """
            INSERT INTO access_operations_metric_snapshots (
                access_operations_snapshot_id,
                snapshot_status,
                captured_by_display_name,
                active_sessions_count,
                open_login_integrity_action_count,
                open_login_integrity_report_count,
                due_retention_jobs_count,
                active_temporary_access_count,
                expired_temporary_access_count,
                suspended_users_count,
                expired_users_count,
                do_not_bill_organisation_count,
                health_status,
                advisory_summary,
                metrics_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                "CAPTURED",
                captured_by_display_name,
                metrics["active_sessions_count"],
                metrics["open_login_integrity_action_count"],
                metrics["open_login_integrity_report_count"],
                metrics["due_retention_jobs_count"],
                metrics["active_temporary_access_count"],
                metrics["expired_temporary_access_count"],
                metrics["suspended_users_count"],
                metrics["expired_users_count"],
                metrics["do_not_bill_organisation_count"],
                metrics["health_status"],
                metrics["advisory_summary"],
                json.dumps(metrics, sort_keys=True),
                ts,
            )
        )

        audit_event(
            conn,
            entity_type="AccessOperationsMetricSnapshot",
            entity_id=snapshot_id,
            action="CAPTURE",
            summary=f"Access operations metrics snapshot captured with health status {metrics['health_status']}.",
            organisation_id=None,
        )

        conn.commit()

        snapshot = conn.execute(
            """
            SELECT *
            FROM access_operations_metric_snapshots
            WHERE access_operations_snapshot_id = ?
            """,
            (snapshot_id,)
        ).fetchone()

        conn.close()

        return jsonify({
            "snapshot": dict(snapshot),
            "metrics": metrics,
            "rule": "This snapshot is a point-in-time Global Admin operations record. AI-style advisory summary is informational only.",
        }), 201

    @app.get("/global-admin/access-operations-metrics-snapshots")
    def list_access_operations_metrics_snapshots():
        conn = get_conn()
        ensure_access_operations_snapshot_tables(conn)

        rows = conn.execute(
            """
            SELECT *
            FROM access_operations_metric_snapshots
            ORDER BY created_at DESC
            LIMIT 25
            """
        ).fetchall()

        conn.close()

        return jsonify({
            "snapshot_list_type": "ACCESS_OPERATIONS_METRICS_SNAPSHOTS",
            "count": len(rows),
            "items": [dict(row) for row in rows],
        }), 200
