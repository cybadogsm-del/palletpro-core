from datetime import datetime, timedelta

from flask import jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso


PRICING_PHILOSOPHY_STATEMENT = {
    "title": "Pallet Pro Pricing Philosophy",
    "statement": "Pallet Pro’s pricing philosophy is simple: keep powerful pallet management accessible to SMEs.",
    "principles": [
        "To protect that philosophy and the integrity of customer data, each user should have their own login.",
        "Shared logins weaken the audit trail because Pallet Pro depends on knowing who, where, and when.",
        "If multiple people use one login, the who is no longer provable.",
        "Operations with 26 or more users require Pallet Pro review and a tailored package.",
        "Temporary users receive 28 days of access from the day after registration.",
        "Temporary user fees are charged on the organisation’s next billing cycle, while the access period is calculated from the temporary user’s registration date.",
        "Pallet Pro must be easy to unsubscribe from, and unsubscribed organisations must not be included in future billing exports.",
    ],
}


def ensure_subscription_guard_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS pricing_settings (
        pricing_settings_id TEXT PRIMARY KEY,
        temporary_user_access_fee_cents INTEGER NOT NULL,
        temporary_access_days INTEGER NOT NULL,
        gst_rate_percent REAL NOT NULL,
        currency TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS pricing_plans (
        pricing_plan_id TEXT PRIMARY KEY,
        plan_name TEXT NOT NULL,
        plan_type TEXT NOT NULL,
        min_permanent_users INTEGER,
        max_permanent_users INTEGER,
        price_per_user_cents INTEGER,
        package_price_cents INTEGER,
        requires_custom_pricing INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER NOT NULL,
        notes TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS organisation_subscriptions (
        organisation_id TEXT PRIMARY KEY,
        subscription_mode TEXT NOT NULL,
        subscription_status TEXT NOT NULL,
        billing_status TEXT NOT NULL,
        do_not_bill INTEGER NOT NULL DEFAULT 0,
        billing_anniversary_day INTEGER,
        pricing_plan_id TEXT,
        custom_pricing_notes TEXT,
        unsubscribed_at TEXT,
        unsubscribed_by_display_name TEXT,
        operating_data_delete_after TEXT,
        historical_data_delete_after TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    ensure_org_commercial_settings_columns(conn)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS temporary_user_access (
        temporary_user_access_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        user_display_name TEXT,
        user_email TEXT,
        access_status TEXT NOT NULL,
        fee_cents INTEGER NOT NULL,
        access_days INTEGER NOT NULL,
        activated_at TEXT,
        access_starts_at TEXT,
        access_ends_at TEXT,
        charged_on_next_billing_cycle INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS billing_export_runs (
        billing_export_run_id TEXT PRIMARY KEY,
        export_status TEXT NOT NULL,
        export_type TEXT NOT NULL,
        created_by_display_name TEXT,
        billing_period_start TEXT,
        billing_period_end TEXT,
        organisation_count INTEGER NOT NULL,
        do_not_bill_excluded_count INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS unsubscribe_events (
        unsubscribe_event_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        unsubscribed_by_display_name TEXT,
        reason_text TEXT,
        billing_stopped_at TEXT NOT NULL,
        operating_data_delete_after TEXT NOT NULL,
        historical_data_delete_after TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS data_retention_jobs (
        data_retention_job_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        job_type TEXT NOT NULL,
        scheduled_for TEXT NOT NULL,
        job_status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        completed_at TEXT
    )
    """)

    ts = now_iso()

    existing_settings = conn.execute(
        "SELECT pricing_settings_id FROM pricing_settings LIMIT 1"
    ).fetchone()

    if not existing_settings:
        conn.execute(
            """
            INSERT INTO pricing_settings (
                pricing_settings_id,
                temporary_user_access_fee_cents,
                temporary_access_days,
                gst_rate_percent,
                currency,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pricing_settings_default",
                2790,
                28,
                10.0,
                "AUD",
                ts,
                ts,
            )
        )

    existing_plans = conn.execute(
        "SELECT COUNT(*) AS c FROM pricing_plans"
    ).fetchone()["c"]

    if existing_plans == 0:
        default_plans = [
            ("plan_single_operation", "Single truck/forklift operation", "SINGLE_OPERATION", 1, 1, None, 2050, 0, 10, "For single truck and/or forklift operations."),
            ("plan_self_service_users", "2 to 25 users", "SELF_SERVICE_PER_USER", 2, 25, 2950, None, 0, 20, "Operations with 2 to 25 users can self-service subscribe at $29.50 per user per month."),
            ("plan_tailored_26_plus", "26+ Tailored Package", "CUSTOM", 26, None, None, None, 1, 30, "For 26 or more users, contact Pallet Pro for a tailored package to suit your operation."),
            ("plan_additional_org_admin", "Additional Org Admin", "ADDITIONAL_ORG_ADMIN_FEE", None, None, None, 625, 0, 40, "One Org Admin is included. Additional active Org Admins are $6.25/month each ex-GST to cover extra portal, reporting, search, and admin data usage."),
            ("plan_temp_user_access", "Temporary User Access Fee", "TEMPORARY_ACCESS_FEE", None, None, None, 2790, 0, 999, "Temporary users receive 28 days of access from the day after registration. Fee is charged on the organisation's next billing cycle."),
        ]

        conn.executemany(
            """
            INSERT INTO pricing_plans (
                pricing_plan_id,
                plan_name,
                plan_type,
                min_permanent_users,
                max_permanent_users,
                price_per_user_cents,
                package_price_cents,
                requires_custom_pricing,
                sort_order,
                notes,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [row + (ts, ts) for row in default_plans]
        )

    # Pricing Alignment v0.1 baseline upgrade.
    #
    # This keeps older dev/test databases from continuing to show the old
    # Starter/Small/Medium/Large defaults after the code baseline changes.
    # It only switches to the new simplified defaults when the new plan IDs
    # do not already exist.
    new_plan_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM pricing_plans
        WHERE pricing_plan_id IN (
            'plan_single_operation',
            'plan_self_service_users',
            'plan_tailored_26_plus'
        )
        """
    ).fetchone()["c"]

    current_default_plans = [
        ("plan_single_operation", "Single truck/forklift operation", "SINGLE_OPERATION", 1, 1, None, 2050, 0, 10, "For single truck and/or forklift operations."),
        ("plan_self_service_users", "2 to 25 users", "SELF_SERVICE_PER_USER", 2, 25, 2950, None, 0, 20, "Operations with 2 to 25 users can self-service subscribe at $29.50 per user per month."),
        ("plan_tailored_26_plus", "26+ Tailored Package", "CUSTOM", 26, None, None, None, 1, 30, "For 26 or more users, contact Pallet Pro for a tailored package to suit your operation."),
        ("plan_additional_org_admin", "Additional Org Admin", "ADDITIONAL_ORG_ADMIN_FEE", None, None, None, 625, 0, 40, "One Org Admin is included. Additional active Org Admins are $6.25/month each ex-GST to cover extra portal, reporting, search, and admin data usage."),
        ("plan_temp_user_access", "Temporary User Access Fee", "TEMPORARY_ACCESS_FEE", None, None, None, 2790, 0, 999, "Temporary users receive 28 days of access from the day after registration. Fee is charged on the organisation's next billing cycle."),
    ]

    if new_plan_count == 0:
        conn.execute(
            """
            UPDATE pricing_plans
            SET is_active = 0,
                updated_at = ?
            WHERE pricing_plan_id IN (
                'plan_starter',
                'plan_small',
                'plan_medium',
                'plan_large',
                'plan_custom_warehouse'
            )
            """,
            (ts,),
        )

        conn.executemany(
            """
            INSERT OR REPLACE INTO pricing_plans (
                pricing_plan_id,
                plan_name,
                plan_type,
                min_permanent_users,
                max_permanent_users,
                price_per_user_cents,
                package_price_cents,
                requires_custom_pricing,
                sort_order,
                notes,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM pricing_plans WHERE pricing_plan_id = ?), ?), ?)
            """,
            [row + (row[0], ts, ts) for row in current_default_plans],
        )

    conn.execute(
        """
        INSERT OR IGNORE INTO pricing_plans (
            pricing_plan_id,
            plan_name,
            plan_type,
            min_permanent_users,
            max_permanent_users,
            price_per_user_cents,
            package_price_cents,
            requires_custom_pricing,
            sort_order,
            notes,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "plan_additional_org_admin",
            "Additional Org Admin",
            "ADDITIONAL_ORG_ADMIN_FEE",
            None,
            None,
            None,
            625,
            0,
            40,
            "One Org Admin is included. Additional active Org Admins are $6.25/month each ex-GST to cover extra portal, reporting, search, and admin data usage.",
            ts,
            ts,
        ),
    )

    conn.execute(
        """
        UPDATE pricing_plans
        SET package_price_cents = ?,
            requires_custom_pricing = ?,
            notes = ?,
            updated_at = ?
        WHERE pricing_plan_id = 'plan_additional_org_admin'
          AND (package_price_cents IS NULL OR package_price_cents = 0)
        """,
        (
            625,
            0,
            "One Org Admin is included. Additional active Org Admins are $6.25/month each ex-GST to cover extra portal, reporting, search, and admin data usage.",
            ts,
        ),
    )

    conn.execute(
        """
        UPDATE pricing_settings
        SET temporary_user_access_fee_cents = ?,
            temporary_access_days = ?,
            updated_at = ?
        WHERE pricing_settings_id = 'pricing_settings_default'
          AND temporary_user_access_fee_cents IN (1000, 1500)
          AND temporary_access_days = 28
        """,
        (2790, 28, ts),
    )

    conn.execute(
        """
        UPDATE pricing_plans
        SET package_price_cents = ?,
            notes = ?,
            updated_at = ?
        WHERE pricing_plan_id = 'plan_temp_user_access'
          AND package_price_cents = 1000
        """,
        (
            2790,
            "Temporary users receive 28 days of access from the day after registration. Fee is charged on the organisation's next billing cycle.",
            ts,
        ),
    )


def get_or_create_subscription(conn, organisation_id):
    ensure_subscription_guard_tables(conn)

    sub = conn.execute(
        "SELECT * FROM organisation_subscriptions WHERE organisation_id = ?",
        (organisation_id,),
    ).fetchone()

    if sub:
        return sub

    ts = now_iso()

    conn.execute(
        """
        INSERT INTO organisation_subscriptions (
            organisation_id,
            subscription_mode,
            subscription_status,
            billing_status,
            do_not_bill,
            billing_anniversary_day,
            pricing_plan_id,
            custom_pricing_notes,
            unsubscribed_at,
            unsubscribed_by_display_name,
            operating_data_delete_after,
            historical_data_delete_after,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            organisation_id,
            "STANDARD",
            "ACTIVE",
            "BILLABLE",
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            ts,
            ts,
        )
    )

    return conn.execute(
        "SELECT * FROM organisation_subscriptions WHERE organisation_id = ?",
        (organisation_id,),
    ).fetchone()


def ensure_org_user_cap_column(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(organisation_subscriptions)").fetchall()}
    if "selected_user_count" not in cols:
        conn.execute("ALTER TABLE organisation_subscriptions ADD COLUMN selected_user_count INTEGER")


def ensure_org_commercial_settings_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(organisation_subscriptions)").fetchall()}

    if "commercial_free_period_days" not in cols:
        conn.execute(
            "ALTER TABLE organisation_subscriptions ADD COLUMN commercial_free_period_days INTEGER NOT NULL DEFAULT 0"
        )

    if "commercial_beta_tester" not in cols:
        conn.execute(
            "ALTER TABLE organisation_subscriptions ADD COLUMN commercial_beta_tester INTEGER NOT NULL DEFAULT 0"
        )

    if "commercial_discount_percent" not in cols:
        conn.execute(
            "ALTER TABLE organisation_subscriptions ADD COLUMN commercial_discount_percent INTEGER NOT NULL DEFAULT 0"
        )

    if "commercial_custom_price_cents" not in cols:
        conn.execute(
            "ALTER TABLE organisation_subscriptions ADD COLUMN commercial_custom_price_cents INTEGER"
        )

    if "commercial_package_name" not in cols:
        conn.execute(
            "ALTER TABLE organisation_subscriptions ADD COLUMN commercial_package_name TEXT"
        )

    if "commercial_package_description" not in cols:
        conn.execute(
            "ALTER TABLE organisation_subscriptions ADD COLUMN commercial_package_description TEXT"
        )

    if "commercial_package_details" not in cols:
        conn.execute(
            "ALTER TABLE organisation_subscriptions ADD COLUMN commercial_package_details TEXT"
        )

    if "commercial_approved_user_limit" not in cols:
        conn.execute(
            "ALTER TABLE organisation_subscriptions ADD COLUMN commercial_approved_user_limit INTEGER"
        )

    if "commercial_top_user_limit" not in cols:
        conn.execute(
            "ALTER TABLE organisation_subscriptions ADD COLUMN commercial_top_user_limit INTEGER"
        )

    if "commercial_review_threshold" not in cols:
        conn.execute(
            "ALTER TABLE organisation_subscriptions ADD COLUMN commercial_review_threshold INTEGER"
        )

    if "commercial_settings_effective_from" not in cols:
        conn.execute(
            "ALTER TABLE organisation_subscriptions ADD COLUMN commercial_settings_effective_from TEXT"
        )

    if "commercial_settings_effective_to" not in cols:
        conn.execute(
            "ALTER TABLE organisation_subscriptions ADD COLUMN commercial_settings_effective_to TEXT"
        )


ORG_SELF_SERVE_USER_LIMIT = 25
INCLUDED_ORG_ADMIN_COUNT = 1
ADDITIONAL_ORG_ADMIN_PLAN_ID = "plan_additional_org_admin"


def count_active_org_admins(conn, organisation_id):
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM user_accounts
        WHERE organisation_id = ?
          AND access_status = 'ACTIVE'
          AND role = 'ORG_ADMIN'
        """,
        (organisation_id,),
    ).fetchone()
    return int(row["c"]) if row else 0


def get_additional_org_admin_plan(conn):
    ensure_subscription_guard_tables(conn)
    return conn.execute(
        "SELECT * FROM pricing_plans WHERE pricing_plan_id = ?",
        (ADDITIONAL_ORG_ADMIN_PLAN_ID,),
    ).fetchone()


def get_org_admin_billing_summary(conn, organisation_id):
    active_org_admin_count = count_active_org_admins(conn, organisation_id)
    billable_additional_count = max(0, active_org_admin_count - INCLUDED_ORG_ADMIN_COUNT)

    plan = get_additional_org_admin_plan(conn)
    price_cents = plan["package_price_cents"] if plan else None
    subtotal_cents = billable_additional_count * int(price_cents or 0)

    if billable_additional_count == 0:
        billing_status = "NO_ADDITIONAL_ORG_ADMINS"
    elif price_cents is None:
        billing_status = "PRICE_NOT_SET"
    else:
        billing_status = "BILLABLE"

    return {
        "pricing_plan_id": ADDITIONAL_ORG_ADMIN_PLAN_ID,
        "included_org_admin_count": INCLUDED_ORG_ADMIN_COUNT,
        "active_org_admin_count": active_org_admin_count,
        "billable_additional_org_admin_count": billable_additional_count,
        "additional_org_admin_price_cents": price_cents,
        "additional_org_admin_subtotal_cents": subtotal_cents,
        "billing_status": billing_status,
        "rule": "One Org Admin is included. Additional active Org Admins are paid upgrades.",
    }


def count_active_permanent_users(conn, organisation_id):
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM user_accounts
        WHERE organisation_id = ?
          AND access_status = 'ACTIVE'
          AND role IN ('ORG_ADMIN', 'USER')
        """,
        (organisation_id,)
    ).fetchone()
    return row["cnt"] if row else 0


def ensure_temporary_user_billing_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(temporary_user_access)").fetchall()}

    if "billed_at" not in cols:
        conn.execute("ALTER TABLE temporary_user_access ADD COLUMN billed_at TEXT")

    if "billing_export_run_id" not in cols:
        conn.execute("ALTER TABLE temporary_user_access ADD COLUMN billing_export_run_id TEXT")


def get_subscription_for_access_guard(conn, organisation_id):
    if not organisation_id:
        return None

    ensure_subscription_guard_tables(conn)

    return conn.execute(
        """
        SELECT *
        FROM organisation_subscriptions
        WHERE organisation_id = ?
        """,
        (organisation_id,),
    ).fetchone()


def classify_org_access_state(subscription_row):
    if not subscription_row:
        return {
            "access_state": "ACTIVE",
            "normal_access_allowed": True,
            "exit_only_access_allowed": False,
            "reason": "No cancellation record found.",
        }

    subscription_mode = subscription_row["subscription_mode"]
    subscription_status = subscription_row["subscription_status"]
    operating_data_delete_after = subscription_row["operating_data_delete_after"]

    if subscription_mode == "SUSPENDED" or subscription_status == "SUSPENDED":
        return {
            "access_state": "SUSPENDED",
            "normal_access_allowed": False,
            "exit_only_access_allowed": False,
            "reason": "Organisation subscription is suspended.",
        }

    if subscription_status not in ("CANCELLED", "UNSUBSCRIBED") and subscription_mode not in ("CANCELLED", "UNSUBSCRIBED"):
        return {
            "access_state": "ACTIVE",
            "normal_access_allowed": True,
            "exit_only_access_allowed": False,
            "reason": "Organisation subscription is active.",
        }

    if operating_data_delete_after:
        delete_after = datetime.fromisoformat(operating_data_delete_after)
        now_dt = datetime.fromisoformat(now_iso())

        if now_dt <= delete_after:
            return {
                "access_state": "CANCELLED_WITHIN_RETENTION",
                "normal_access_allowed": False,
                "exit_only_access_allowed": True,
                "reason": "Organisation has unsubscribed. Normal access is blocked, but exit/export access is available until operating data deletion.",
            }

    return {
        "access_state": "CANCELLED_DATA_DELETED",
        "normal_access_allowed": False,
        "exit_only_access_allowed": False,
        "reason": "Organisation has unsubscribed and the operating data retention window has ended.",
    }


def get_org_access_status_payload(conn, organisation_id):
    sub = get_subscription_for_access_guard(conn, organisation_id)
    state = classify_org_access_state(sub)

    return {
        "organisation_id": organisation_id,
        "access_state": state["access_state"],
        "normal_access_allowed": state["normal_access_allowed"],
        "exit_only_access_allowed": state["exit_only_access_allowed"],
        "reason": state["reason"],
        "subscription": dict(sub) if sub else None,
        "allowed_exit_actions": [
            "view_unsubscribe_status",
            "export_operating_data",
            "view_deletion_dates",
            "contact_support",
            "reactivate_within_retention_window",
            "view_billing_stopped_status",
        ] if state["exit_only_access_allowed"] else [],
        "blocked_operational_actions": [
            "create_transactions",
            "post_transactions",
            "add_users",
            "add_depots",
            "add_resources",
            "add_partners",
            "generate_qr_handoffs",
            "sync_field_activity",
        ] if not state["normal_access_allowed"] else [],
    }


def require_active_org_access(conn, organisation_id):
    payload = get_org_access_status_payload(conn, organisation_id)

    if payload["normal_access_allowed"]:
        return None

    return {
        "error": "Organisation does not have active operational access",
        "organisation_id": organisation_id,
        "access_state": payload["access_state"],
        "reason": payload["reason"],
        "exit_only_access_allowed": payload["exit_only_access_allowed"],
        "allowed_exit_actions": payload["allowed_exit_actions"],
        "blocked_operational_actions": payload["blocked_operational_actions"],
    }


def register_subscription_routes(app):
    @app.get("/pricing-philosophy")
    def get_pricing_philosophy():
        return jsonify(PRICING_PHILOSOPHY_STATEMENT), 200

    @app.get("/pricing-table")
    def get_pricing_table():
        conn = get_conn()
        ensure_subscription_guard_tables(conn)
        conn.commit()

        settings = conn.execute(
            "SELECT * FROM pricing_settings ORDER BY created_at ASC LIMIT 1"
        ).fetchone()

        plans = conn.execute(
            """
            SELECT *
            FROM pricing_plans
            WHERE is_active = 1
            ORDER BY sort_order ASC, plan_name ASC
            """
        ).fetchall()

        conn.close()

        return jsonify({
            "pricing_philosophy": PRICING_PHILOSOPHY_STATEMENT,
            "settings": dict(settings) if settings else None,
            "items": [dict(row) for row in plans],
        }), 200

    @app.get("/global-admin/pricing-dashboard")
    def get_global_admin_pricing_dashboard():
        conn = get_conn()
        ensure_subscription_guard_tables(conn)
        ensure_temporary_user_billing_columns(conn)
        conn.commit()

        settings = conn.execute(
            "SELECT * FROM pricing_settings ORDER BY created_at ASC LIMIT 1"
        ).fetchone()

        plans = conn.execute(
            """
            SELECT *
            FROM pricing_plans
            ORDER BY sort_order ASC, plan_name ASC
            """
        ).fetchall()

        subscription_rows = conn.execute(
            """
            SELECT
                s.*,
                o.name AS organisation_name
            FROM organisation_subscriptions s
            LEFT JOIN organisations o ON o.organisation_id = s.organisation_id
            ORDER BY o.name ASC
            """
        ).fetchall()

        export_runs = conn.execute(
            """
            SELECT *
            FROM billing_export_runs
            ORDER BY created_at DESC
            LIMIT 10
            """
        ).fetchall()

        temp_summary = conn.execute(
            """
            SELECT
                access_status,
                COUNT(*) AS count,
                COALESCE(SUM(fee_cents), 0) AS fee_cents_total,
                COALESCE(SUM(CASE WHEN billed_at IS NULL THEN fee_cents ELSE 0 END), 0) AS unbilled_fee_cents_total
            FROM temporary_user_access
            GROUP BY access_status
            ORDER BY access_status ASC
            """
        ).fetchall()

        mode_counts = {}
        billing_status_counts = {}

        for row in subscription_rows:
            mode = row["subscription_mode"]
            billing_status = row["billing_status"]
            mode_counts[mode] = mode_counts.get(mode, 0) + 1
            billing_status_counts[billing_status] = billing_status_counts.get(billing_status, 0) + 1

        conn.close()

        return jsonify({
            "dashboard_type": "GLOBAL_ADMIN_PRICING_DASHBOARD",
            "pricing_philosophy": PRICING_PHILOSOPHY_STATEMENT,
            "settings": dict(settings) if settings else None,
            "pricing_plan_count": len(plans),
            "pricing_plans": [dict(row) for row in plans],
            "subscription_count": len(subscription_rows),
            "subscription_mode_counts": mode_counts,
            "billing_status_counts": billing_status_counts,
            "subscriptions": [dict(row) for row in subscription_rows],
            "temporary_user_access_summary": [dict(row) for row in temp_summary],
            "recent_billing_export_runs": [dict(row) for row in export_runs],
            "rules": [
                "Temporary User Access Fee is shown as a pricing extra.",
                "One Org Admin is included. Additional active Org Admins are paid upgrades.",
                "Operations with 26 or more users require Pallet Pro review and a tailored package.",
                "Free, Beta Tester, Quoted, Suspended, and Cancelled organisations are do-not-bill unless explicitly changed.",
                "Unsubscribed organisations must not be included in billing exports.",
            ],
        }), 200

    @app.get("/organisations/<organisation_id>/subscription-dashboard")
    def get_org_admin_subscription_dashboard(organisation_id):
        conn = get_conn()
        ensure_subscription_guard_tables(conn)
        ensure_temporary_user_billing_columns(conn)
        conn.commit()

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        sub = get_or_create_subscription(conn, organisation_id)
        access = get_org_access_status_payload(conn, organisation_id)
        org_admin_billing = get_org_admin_billing_summary(conn, organisation_id)

        settings = conn.execute(
            "SELECT * FROM pricing_settings ORDER BY created_at ASC LIMIT 1"
        ).fetchone()

        temp_users = conn.execute(
            """
            SELECT *
            FROM temporary_user_access
            WHERE organisation_id = ?
            ORDER BY created_at DESC
            """,
            (organisation_id,),
        ).fetchall()

        scheduled_jobs = conn.execute(
            """
            SELECT *
            FROM data_retention_jobs
            WHERE organisation_id = ?
            ORDER BY scheduled_for ASC
            """,
            (organisation_id,),
        ).fetchall()

        unbilled_temp_fee_cents = 0
        active_temp_user_count = 0

        for row in temp_users:
            if row["access_status"] == "ACTIVE":
                active_temp_user_count += 1
            if row["charged_on_next_billing_cycle"] == 1 and row["billed_at"] is None:
                unbilled_temp_fee_cents += int(row["fee_cents"])

        dashboard_actions = []

        if access["access_state"] == "ACTIVE":
            dashboard_actions.append({
                "action_key": "unsubscribe",
                "label": "Unsubscribe",
                "route": f"/organisations/{organisation_id}/unsubscribe",
                "method": "POST",
                "requires_confirmation": True,
                "confirmation_guidance": "Unsubscribing stops future billing and starts the 7-day operating data retention window.",
            })

        if access["exit_only_access_allowed"]:
            dashboard_actions.extend([
                {
                    "action_key": "export_operating_data",
                    "label": "Export operating data",
                    "route": f"/organisations/{organisation_id}/operating-data-export",
                    "method": "GET",
                    "requires_confirmation": False,
                },
                {
                    "action_key": "reactivate",
                    "label": "Reactivate subscription",
                    "route": f"/organisations/{organisation_id}/reactivate",
                    "method": "POST",
                    "requires_confirmation": True,
                    "confirmation_guidance": "Reactivation is available during the 7-day retention window before operating data deletion.",
                },
            ])

        conn.close()

        return jsonify({
            "dashboard_type": "ORG_ADMIN_SUBSCRIPTION_DASHBOARD",
            "organisation_id": organisation_id,
            "organisation_name": org["name"],
            "subscription": dict(sub),
            "access_status": access,
            "billing": {
                "billing_status": sub["billing_status"],
                "do_not_bill": sub["do_not_bill"],
                "billing_anniversary_day": sub["billing_anniversary_day"],
                "unbilled_temporary_user_fee_cents": unbilled_temp_fee_cents,
                "currency": settings["currency"] if settings else "AUD",
                "temporary_user_access_fee_cents": settings["temporary_user_access_fee_cents"] if settings else None,
                "temporary_access_days": settings["temporary_access_days"] if settings else None,
            },
            "temporary_users": {
                "active_count": active_temp_user_count,
                "total_count": len(temp_users),
                "items": [dict(row) for row in temp_users],
            },
            "org_admin_billing": org_admin_billing,
            "data_retention": {
                "operating_data_delete_after": sub["operating_data_delete_after"],
                "historical_data_delete_after": sub["historical_data_delete_after"],
                "scheduled_jobs": [dict(row) for row in scheduled_jobs],
            },
            "dashboard_actions": dashboard_actions,
            "rules": [
                "Pallet Pro must be easy to unsubscribe from.",
                "Unsubscribing stops future billing immediately.",
                "After unsubscribe, normal operational access is blocked but exit/export access remains available during the 7-day retention window.",
                "Operating data is deleted after 7 days unless the organisation reactivates before deletion.",
            ],
        }), 200

    @app.get("/global-admin/organisations/<organisation_id>/org-admin-subscription-preview")
    def get_sga_org_admin_subscription_preview(organisation_id):
        """SUPER_GLOBAL_ADMIN preview of the Org Admin subscription dashboard."""
        from flask import g

        if g.current_user.get("role") != "SUPER_GLOBAL_ADMIN":
            return jsonify({
                "error": "INSUFFICIENT_ROLE",
                "message": "Super Global Admin only.",
            }), 403

        conn = get_conn()
        ensure_subscription_guard_tables(conn)
        ensure_temporary_user_billing_columns(conn)
        conn.commit()

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        sub = get_or_create_subscription(conn, organisation_id)
        access = get_org_access_status_payload(conn, organisation_id)
        org_admin_billing = get_org_admin_billing_summary(conn, organisation_id)

        settings = conn.execute(
            "SELECT * FROM pricing_settings ORDER BY created_at ASC LIMIT 1"
        ).fetchone()

        temp_users = conn.execute(
            """
            SELECT *
            FROM temporary_user_access
            WHERE organisation_id = ?
            ORDER BY created_at DESC
            """,
            (organisation_id,),
        ).fetchall()

        scheduled_jobs = conn.execute(
            """
            SELECT *
            FROM data_retention_jobs
            WHERE organisation_id = ?
            ORDER BY scheduled_for ASC
            """,
            (organisation_id,),
        ).fetchall()

        unbilled_temp_fee_cents = 0
        active_temp_user_count = 0

        for row in temp_users:
            if row["access_status"] == "ACTIVE":
                active_temp_user_count += 1
            if row["charged_on_next_billing_cycle"] == 1 and row["billed_at"] is None:
                unbilled_temp_fee_cents += int(row["fee_cents"])

        dashboard_actions = []

        if access["access_state"] == "ACTIVE":
            dashboard_actions.append({
                "action_key": "unsubscribe",
                "label": "Unsubscribe",
                "route": f"/organisations/{organisation_id}/unsubscribe",
                "method": "POST",
                "requires_confirmation": True,
                "confirmation_guidance": "Unsubscribing stops future billing and starts the 7-day operating data retention window.",
            })

        if access["exit_only_access_allowed"]:
            dashboard_actions.extend([
                {
                    "action_key": "export_operating_data",
                    "label": "Export operating data",
                    "route": f"/organisations/{organisation_id}/operating-data-export",
                    "method": "GET",
                    "requires_confirmation": False,
                },
                {
                    "action_key": "reactivate",
                    "label": "Reactivate subscription",
                    "route": f"/organisations/{organisation_id}/reactivate",
                    "method": "POST",
                    "requires_confirmation": True,
                    "confirmation_guidance": "Reactivation is available during the 7-day retention window before operating data deletion.",
                },
            ])

        viewer = g.current_user

        audit_event(
            conn,
            entity_type="OrganisationSubscription",
            entity_id=organisation_id,
            action="SGA_PREVIEW_ORG_ADMIN_SUBSCRIPTION_DASHBOARD",
            summary=f"Super Global Admin {viewer.get('display_name', 'Unknown')} previewed Org Admin subscription dashboard for {org['name']}.",
            organisation_id=organisation_id,
        )

        conn.commit()

        payload = {
            "dashboard_type": "SGA_ORG_ADMIN_SUBSCRIPTION_PREVIEW",
            "preview_mode": {
                "enabled": True,
                "viewer_role": viewer.get("role"),
                "viewer_display_name": viewer.get("display_name"),
                "previewing_as": "ORG_ADMIN",
                "rule": "This is a read-only Super Global Admin preview of what an Org Admin will see.",
            },
            "org_admin_dashboard": {
                "dashboard_type": "ORG_ADMIN_SUBSCRIPTION_DASHBOARD",
                "organisation_id": organisation_id,
                "organisation_name": org["name"],
                "subscription": dict(sub),
                "access_status": access,
                "billing": {
                    "billing_status": sub["billing_status"],
                    "do_not_bill": sub["do_not_bill"],
                    "billing_anniversary_day": sub["billing_anniversary_day"],
                    "unbilled_temporary_user_fee_cents": unbilled_temp_fee_cents,
                    "currency": settings["currency"] if settings else "AUD",
                    "temporary_user_access_fee_cents": settings["temporary_user_access_fee_cents"] if settings else None,
                    "temporary_access_days": settings["temporary_access_days"] if settings else None,
                },
                "temporary_users": {
                    "active_count": active_temp_user_count,
                    "total_count": len(temp_users),
                    "items": [dict(row) for row in temp_users],
                    "rules": [
                        "Temporary access starts on the day after registration.",
                        "If a Temporary User is converted to a Permanent User during the temporary access period, the Temporary User fee is waived.",
                        "Billing for the new Permanent User starts on the subscriber’s next billing cycle.",
                        "If not converted, the Temporary User fee is charged on the organisation’s next billing cycle.",
                    ],
                },
                "org_admin_billing": org_admin_billing,
                "data_retention": {
                    "operating_data_delete_after": sub["operating_data_delete_after"],
                    "historical_data_delete_after": sub["historical_data_delete_after"],
                    "scheduled_jobs": [dict(row) for row in scheduled_jobs],
                },
                "dashboard_actions": dashboard_actions,
                "rules": [
                    "This preview shows the Org Admin subscription dashboard payload.",
                    "One Org Admin is included. Additional active Org Admins are paid upgrades.",
                    "Temporary User billing rules must be visible before billing disputes happen.",
                    "SGA preview does not impersonate the Org Admin and must remain auditable.",
                ],
            },
        }

        conn.close()

        return jsonify(payload), 200

    @app.patch("/global-admin/organisations/<organisation_id>/commercial-settings")
    def update_org_commercial_settings(organisation_id):
        """SUPER_GLOBAL_ADMIN only — update commercial settings for billing calculations."""
        from flask import g

        if g.current_user.get("role") != "SUPER_GLOBAL_ADMIN":
            return jsonify({
                "error": "INSUFFICIENT_ROLE",
                "message": "Super Global Admin only.",
            }), 403

        body = request.get_json(silent=True) or {}

        audit_reason = (body.get("audit_reason") or "").strip()
        if not audit_reason:
            return jsonify({
                "error": "Missing audit reason",
                "required_field": "audit_reason",
            }), 400

        allowed_fields = {
            "commercial_free_period_days",
            "commercial_beta_tester",
            "commercial_discount_percent",
            "commercial_custom_price_cents",
            "commercial_package_name",
            "commercial_package_description",
            "commercial_package_details",
            "commercial_approved_user_limit",
            "commercial_top_user_limit",
            "commercial_review_threshold",
            "commercial_settings_effective_from",
            "commercial_settings_effective_to",
            "audit_reason",
        }

        unknown_fields = [k for k in body.keys() if k not in allowed_fields]
        if unknown_fields:
            return jsonify({
                "error": "Unknown fields provided",
                "unknown_fields": unknown_fields,
                "allowed_fields": sorted(list(allowed_fields - {"audit_reason"})),
            }), 400

        updates = {}

        if "commercial_free_period_days" in body:
            value = body["commercial_free_period_days"]
            if value is None:
                value = 0
            elif isinstance(value, bool) or not isinstance(value, int):
                return jsonify({
                    "error": "commercial_free_period_days must be an integer >= 0 or null",
                    "field": "commercial_free_period_days",
                }), 400
            elif value < 0:
                return jsonify({
                    "error": "commercial_free_period_days must be an integer >= 0",
                    "field": "commercial_free_period_days",
                }), 400
            updates["commercial_free_period_days"] = value

        if "commercial_beta_tester" in body:
            value = body["commercial_beta_tester"]
            if not isinstance(value, bool):
                return jsonify({
                    "error": "commercial_beta_tester must be true or false",
                    "field": "commercial_beta_tester",
                }), 400
            updates["commercial_beta_tester"] = 1 if value else 0

        if "commercial_discount_percent" in body:
            value = body["commercial_discount_percent"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return jsonify({
                    "error": "commercial_discount_percent must be a number from 0 to 100",
                    "field": "commercial_discount_percent",
                }), 400

            if int(value) != value:
                return jsonify({
                    "error": "commercial_discount_percent must be a whole number from 0 to 100",
                    "field": "commercial_discount_percent",
                }), 400

            value = int(value)
            if value < 0 or value > 100:
                return jsonify({
                    "error": "commercial_discount_percent must be between 0 and 100",
                    "field": "commercial_discount_percent",
                }), 400
            updates["commercial_discount_percent"] = value

        if "commercial_custom_price_cents" in body:
            value = body["commercial_custom_price_cents"]
            if value is None:
                updates["commercial_custom_price_cents"] = None
            elif isinstance(value, bool) or not isinstance(value, int):
                return jsonify({
                    "error": "commercial_custom_price_cents must be an integer >= 0 or null",
                    "field": "commercial_custom_price_cents",
                }), 400
            elif value < 0:
                return jsonify({
                    "error": "commercial_custom_price_cents must be an integer >= 0",
                    "field": "commercial_custom_price_cents",
                }), 400
            updates["commercial_custom_price_cents"] = value

        if "commercial_package_name" in body:
            value = body["commercial_package_name"]
            if value is None:
                updates["commercial_package_name"] = None
            elif not isinstance(value, str):
                return jsonify({
                    "error": "commercial_package_name must be text or null",
                    "field": "commercial_package_name",
                }), 400
            else:
                trimmed = value.strip()
                updates["commercial_package_name"] = trimmed if trimmed else None

        if "commercial_package_description" in body:
            value = body["commercial_package_description"]
            if value is None:
                updates["commercial_package_description"] = None
            elif not isinstance(value, str):
                return jsonify({
                    "error": "commercial_package_description must be text or null",
                    "field": "commercial_package_description",
                }), 400
            else:
                trimmed = value.strip()
                updates["commercial_package_description"] = trimmed if trimmed else None

        if "commercial_package_details" in body:
            value = body["commercial_package_details"]
            if value is None:
                updates["commercial_package_details"] = None
            elif not isinstance(value, str):
                return jsonify({
                    "error": "commercial_package_details must be text or null",
                    "field": "commercial_package_details",
                }), 400
            else:
                trimmed = value.strip()
                updates["commercial_package_details"] = trimmed if trimmed else None

        if "commercial_approved_user_limit" in body:
            value = body["commercial_approved_user_limit"]
            if value is None:
                updates["commercial_approved_user_limit"] = None
            elif isinstance(value, bool) or not isinstance(value, int):
                return jsonify({
                    "error": "commercial_approved_user_limit must be an integer >= 0 or null",
                    "field": "commercial_approved_user_limit",
                }), 400
            elif value < 0:
                return jsonify({
                    "error": "commercial_approved_user_limit must be an integer >= 0 or null",
                    "field": "commercial_approved_user_limit",
                }), 400
            updates["commercial_approved_user_limit"] = value

        if "commercial_top_user_limit" in body:
            value = body["commercial_top_user_limit"]
            if value is None:
                updates["commercial_top_user_limit"] = None
            elif isinstance(value, bool) or not isinstance(value, int):
                return jsonify({
                    "error": "commercial_top_user_limit must be an integer >= 0 or null",
                    "field": "commercial_top_user_limit",
                }), 400
            elif value < 0:
                return jsonify({
                    "error": "commercial_top_user_limit must be an integer >= 0 or null",
                    "field": "commercial_top_user_limit",
                }), 400
            updates["commercial_top_user_limit"] = value

            if (
                "commercial_approved_user_limit" in body
                and value is not None
                and body.get("commercial_approved_user_limit") is not None
                and value < body["commercial_approved_user_limit"]
            ):
                return jsonify({
                    "error": "commercial_top_user_limit must be greater than or equal to commercial_approved_user_limit",
                    "field": "commercial_top_user_limit",
                }), 400

        if "commercial_review_threshold" in body:
            value = body["commercial_review_threshold"]
            if value is None:
                updates["commercial_review_threshold"] = None
            elif isinstance(value, bool) or not isinstance(value, int):
                return jsonify({
                    "error": "commercial_review_threshold must be a whole number from 0 to 100",
                    "field": "commercial_review_threshold",
                }), 400
            elif value < 0 or value > 100:
                return jsonify({
                    "error": "commercial_review_threshold must be a whole number from 0 to 100",
                    "field": "commercial_review_threshold",
                }), 400
            updates["commercial_review_threshold"] = value

        if "commercial_settings_effective_from" in body:
            value = body["commercial_settings_effective_from"]
            if value is None:
                updates["commercial_settings_effective_from"] = None
            else:
                if not isinstance(value, str):
                    return jsonify({
                        "error": "commercial_settings_effective_from must be a date in YYYY-MM-DD format or null",
                        "field": "commercial_settings_effective_from",
                    }), 400
                value = value.strip()
                if value == "":
                    updates["commercial_settings_effective_from"] = None
                else:
                    try:
                        datetime.strptime(value, "%Y-%m-%d")
                    except Exception:
                        return jsonify({
                            "error": "commercial_settings_effective_from must be a date in YYYY-MM-DD format",
                            "field": "commercial_settings_effective_from",
                        }), 400
                    updates["commercial_settings_effective_from"] = value

        if "commercial_settings_effective_to" in body:
            value = body["commercial_settings_effective_to"]
            if value is None:
                updates["commercial_settings_effective_to"] = None
            else:
                if not isinstance(value, str):
                    return jsonify({
                        "error": "commercial_settings_effective_to must be a date in YYYY-MM-DD format or null",
                        "field": "commercial_settings_effective_to",
                    }), 400
                value = value.strip()
                if value == "":
                    updates["commercial_settings_effective_to"] = None
                else:
                    try:
                        datetime.strptime(value, "%Y-%m-%d")
                    except Exception:
                        return jsonify({
                            "error": "commercial_settings_effective_to must be a date in YYYY-MM-DD format",
                            "field": "commercial_settings_effective_to",
                        }), 400
                    updates["commercial_settings_effective_to"] = value

        if (
            "commercial_settings_effective_from" in body
            and "commercial_settings_effective_to" in body
        ):
            effective_from = updates.get("commercial_settings_effective_from")
            effective_to = updates.get("commercial_settings_effective_to")
            if effective_from is not None and effective_to is not None:
                if datetime.strptime(effective_from, "%Y-%m-%d") > datetime.strptime(effective_to, "%Y-%m-%d"):
                    return jsonify({
                        "error": "commercial_settings_effective_to must not be earlier than commercial_settings_effective_from",
                        "field": "commercial_settings_effective_to",
                    }), 400

        if not updates:
            return jsonify({
                "error": "No updateable commercial fields provided",
                "allowed_fields": sorted(list(allowed_fields - {"audit_reason"})),
            }), 400

        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        get_or_create_subscription(conn, organisation_id)

        current = conn.execute(
            "SELECT * FROM organisation_subscriptions WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()

        old_values = {
            "commercial_free_period_days": current["commercial_free_period_days"],
            "commercial_beta_tester": current["commercial_beta_tester"],
            "commercial_discount_percent": current["commercial_discount_percent"],
            "commercial_custom_price_cents": current["commercial_custom_price_cents"],
            "commercial_package_name": current["commercial_package_name"],
            "commercial_package_description": current["commercial_package_description"],
            "commercial_package_details": current["commercial_package_details"],
            "commercial_approved_user_limit": current["commercial_approved_user_limit"],
            "commercial_top_user_limit": current["commercial_top_user_limit"],
            "commercial_review_threshold": current["commercial_review_threshold"],
            "commercial_settings_effective_from": current["commercial_settings_effective_from"],
            "commercial_settings_effective_to": current["commercial_settings_effective_to"],
        }

        set_clause = ", ".join([f"{field} = ?" for field in updates])
        conn.execute(
            f"UPDATE organisation_subscriptions SET {set_clause}, updated_at = ? WHERE organisation_id = ?",
            [*updates.values(), now_iso(), organisation_id],
        )

        new_values = {
            "commercial_free_period_days": current["commercial_free_period_days"] if "commercial_free_period_days" not in updates else updates["commercial_free_period_days"],
            "commercial_beta_tester": current["commercial_beta_tester"] if "commercial_beta_tester" not in updates else updates["commercial_beta_tester"],
            "commercial_discount_percent": current["commercial_discount_percent"] if "commercial_discount_percent" not in updates else updates["commercial_discount_percent"],
            "commercial_custom_price_cents": current["commercial_custom_price_cents"] if "commercial_custom_price_cents" not in updates else updates["commercial_custom_price_cents"],
            "commercial_package_name": current["commercial_package_name"] if "commercial_package_name" not in updates else updates["commercial_package_name"],
            "commercial_package_description": current["commercial_package_description"] if "commercial_package_description" not in updates else updates["commercial_package_description"],
            "commercial_package_details": current["commercial_package_details"] if "commercial_package_details" not in updates else updates["commercial_package_details"],
            "commercial_approved_user_limit": current["commercial_approved_user_limit"] if "commercial_approved_user_limit" not in updates else updates["commercial_approved_user_limit"],
            "commercial_top_user_limit": current["commercial_top_user_limit"] if "commercial_top_user_limit" not in updates else updates["commercial_top_user_limit"],
            "commercial_review_threshold": current["commercial_review_threshold"] if "commercial_review_threshold" not in updates else updates["commercial_review_threshold"],
            "commercial_settings_effective_from": current["commercial_settings_effective_from"] if "commercial_settings_effective_from" not in updates else updates["commercial_settings_effective_from"],
            "commercial_settings_effective_to": current["commercial_settings_effective_to"] if "commercial_settings_effective_to" not in updates else updates["commercial_settings_effective_to"],
        }

        audit_event(
            conn,
            entity_type="OrganisationSubscription",
            entity_id=organisation_id,
            action="UPDATE_ORG_COMMERCIAL_SETTINGS",
            summary=(
                f"Super Global Admin updated commercial settings for {org['name']} (reason: {audit_reason}). "
                f"Old: {old_values}, New: {new_values}"
            ),
            organisation_id=organisation_id,
        )

        conn.commit()

        updated = conn.execute(
            "SELECT * FROM organisation_subscriptions WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()

        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "organisation_name": org["name"],
            "subscription": dict(updated),
            "commercial_settings": {
                "commercial_free_period_days": updated["commercial_free_period_days"],
                "commercial_beta_tester": updated["commercial_beta_tester"],
                "commercial_discount_percent": updated["commercial_discount_percent"],
                "commercial_custom_price_cents": updated["commercial_custom_price_cents"],
                "commercial_package_name": updated["commercial_package_name"],
                "commercial_package_description": updated["commercial_package_description"],
                "commercial_package_details": updated["commercial_package_details"],
                "commercial_approved_user_limit": updated["commercial_approved_user_limit"],
                "commercial_top_user_limit": updated["commercial_top_user_limit"],
                "commercial_review_threshold": updated["commercial_review_threshold"],
                "commercial_settings_effective_from": updated["commercial_settings_effective_from"],
                "commercial_settings_effective_to": updated["commercial_settings_effective_to"],
            },
            "audit_reason": audit_reason,
            "rule": "Only Super Global Admin can edit commercial settings that change billing totals.",
        }), 200

    @app.post("/organisations/<organisation_id>/temporary-users")
    def create_temporary_user_access(organisation_id):
        body = request.get_json(silent=True) or {}
        user_display_name = (body.get("user_display_name") or "").strip() or None
        user_email = (body.get("user_email") or "").strip() or None
        created_by_display_name = (body.get("created_by_display_name") or "Org Admin").strip()

        if not user_display_name and not user_email:
            return jsonify({"error": "user_display_name or user_email is required"}), 400

        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        access_error = require_active_org_access(conn, organisation_id)
        if access_error:
            conn.close()
            return jsonify(access_error), 403

        settings = conn.execute(
            "SELECT * FROM pricing_settings ORDER BY created_at ASC LIMIT 1"
        ).fetchone()

        if not settings:
            conn.close()
            return jsonify({"error": "Pricing settings not configured"}), 500

        ts = now_iso()
        activated_at = datetime.fromisoformat(ts)
        access_starts_at = activated_at + timedelta(days=1)
        access_ends_at = access_starts_at + timedelta(days=settings["temporary_access_days"] - 1)

        temporary_user_access_id = make_id("tua")

        conn.execute(
            """
            INSERT INTO temporary_user_access (
                temporary_user_access_id,
                organisation_id,
                user_display_name,
                user_email,
                access_status,
                fee_cents,
                access_days,
                activated_at,
                access_starts_at,
                access_ends_at,
                charged_on_next_billing_cycle,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                temporary_user_access_id,
                organisation_id,
                user_display_name,
                user_email,
                "ACTIVE",
                settings["temporary_user_access_fee_cents"],
                settings["temporary_access_days"],
                ts,
                access_starts_at.isoformat(),
                access_ends_at.isoformat(),
                1,
                ts,
                ts,
            )
        )

        audit_event(
            conn,
            entity_type="TemporaryUserAccess",
            entity_id=temporary_user_access_id,
            action="CREATE",
            summary=f"Temporary user access created for {user_display_name or user_email}",
            organisation_id=organisation_id,
        )

        conn.commit()

        row = conn.execute(
            "SELECT * FROM temporary_user_access WHERE temporary_user_access_id = ?",
            (temporary_user_access_id,),
        ).fetchone()

        conn.close()

        return jsonify({
            "temporary_user_access": dict(row),
            "pricing_rule": "Temporary users receive 28 days of access from the day after registration.",
            "billing_rule": "Temporary user fee is charged on the organisation’s next billing cycle.",
            "created_by_display_name": created_by_display_name,
        }), 201

    @app.get("/organisations/<organisation_id>/temporary-users")
    def list_temporary_user_access(organisation_id):
        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        rows = conn.execute(
            """
            SELECT *
            FROM temporary_user_access
            WHERE organisation_id = ?
            ORDER BY created_at DESC
            """,
            (organisation_id,),
        ).fetchall()

        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "organisation_name": org["name"],
            "count": len(rows),
            "items": [dict(row) for row in rows],
        }), 200

    @app.patch("/global-admin/pricing-plans/<pricing_plan_id>")
    def update_pricing_plan(pricing_plan_id):
        """SUPER_GLOBAL_ADMIN only — update price fields on a pricing plan."""
        from flask import g
        _SGA = {"SUPER_GLOBAL_ADMIN"}
        if g.current_user.get("role") not in _SGA:
            return jsonify({"error": "INSUFFICIENT_ROLE", "message": "Super Global Admin only."}), 403

        body = request.get_json(silent=True) or {}
        audit_reason = (body.get("audit_reason") or "").strip()
        if not audit_reason:
            return jsonify({
                "error": "Missing audit reason",
                "required_field": "audit_reason",
            }), 400

        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        plan = conn.execute(
            "SELECT * FROM pricing_plans WHERE pricing_plan_id = ?", (pricing_plan_id,)
        ).fetchone()
        if not plan:
            conn.close()
            return jsonify({"error": "Pricing plan not found"}), 404

        fields = {}
        if "package_price_cents" in body:
            v = body["package_price_cents"]
            fields["package_price_cents"] = int(v) if v is not None else None
        if "price_per_user_cents" in body:
            v = body["price_per_user_cents"]
            fields["price_per_user_cents"] = int(v) if v is not None else None
        if "plan_name" in body:
            fields["plan_name"] = (body["plan_name"] or "").strip()[:255] or None
        if "notes" in body:
            fields["notes"] = (body["notes"] or "").strip()[:1000] or None
        if "is_active" in body:
            fields["is_active"] = 1 if body["is_active"] else 0

        if not fields:
            conn.close()
            return jsonify({"error": "No updateable fields provided"}), 400

        fields["updated_at"] = now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE pricing_plans SET {set_clause} WHERE pricing_plan_id = ?",
            [*fields.values(), pricing_plan_id],
        )

        audit_event(
            conn,
            entity_type="PricingPlan",
            entity_id=pricing_plan_id,
            action="UPDATE",
            summary=f"Pricing plan updated by Super Global Admin: {pricing_plan_id}",
            organisation_id=None,
        )

        conn.commit()
        updated = conn.execute(
            "SELECT * FROM pricing_plans WHERE pricing_plan_id = ?", (pricing_plan_id,)
        ).fetchone()
        conn.close()

        return jsonify(dict(updated)), 200

    @app.patch("/global-admin/pricing-settings")
    def update_pricing_settings():
        """SUPER_GLOBAL_ADMIN only — update GST rate, temp access days, temp fee."""
        from flask import g
        _SGA = {"SUPER_GLOBAL_ADMIN"}
        if g.current_user.get("role") not in _SGA:
            return jsonify({"error": "INSUFFICIENT_ROLE", "message": "Super Global Admin only."}), 403

        body = request.get_json(silent=True) or {}
        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        fields = {}
        if "gst_rate_percent" in body:
            fields["gst_rate_percent"] = float(body["gst_rate_percent"])
        if "temporary_access_days" in body:
            fields["temporary_access_days"] = int(body["temporary_access_days"])
        if "temporary_user_access_fee_cents" in body:
            fields["temporary_user_access_fee_cents"] = int(body["temporary_user_access_fee_cents"])

        if not fields:
            conn.close()
            return jsonify({"error": "No updateable fields provided"}), 400

        fields["updated_at"] = now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE pricing_settings SET {set_clause} WHERE pricing_settings_id = 'pricing_settings_default'",
            list(fields.values()),
        )

        audit_event(
            conn,
            entity_type="PricingSettings",
            entity_id="pricing_settings_default",
            action="UPDATE",
            summary="Pricing settings updated by Super Global Admin",
            organisation_id=None,
        )

        conn.commit()
        updated = conn.execute("SELECT * FROM pricing_settings LIMIT 1").fetchone()
        conn.close()

        return jsonify(dict(updated)), 200
