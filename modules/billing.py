"""
modules/billing.py — Billing & Pricing brick

Covers:
  - Schema helper: ensure_billing_export_snapshot_tables
  - Routes:
      POST /global-admin/billing-export-preview
      POST /global-admin/billing-export-finalise
      GET  /global-admin/billing-export-runs/<id>
      GET  /global-admin/billing-export-runs/<id>/third-party-payload
      POST /global-admin/organisations/<id>/subscription-mode
      POST /global-admin/pricing-settings
      POST /global-admin/pricing-plans/<id>
"""

import json

from flask import jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso
from modules.subscription_access import (
    ensure_subscription_guard_tables,
    ensure_org_commercial_settings_columns,
    ensure_temporary_user_billing_columns,
    get_org_admin_billing_summary,
    get_or_create_subscription,
    get_org_access_status_payload,
)


# ── Schema helper ──────────────────────────────────────────────────────────────

def ensure_billing_export_snapshot_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS billing_export_line_items (
        billing_export_line_item_id TEXT PRIMARY KEY,
        billing_export_run_id TEXT NOT NULL,
        organisation_id TEXT NOT NULL,
        organisation_name TEXT NOT NULL,
        currency TEXT NOT NULL,
        subscription_subtotal_cents INTEGER NOT NULL,
        temporary_user_count INTEGER NOT NULL,
        temporary_user_fee_cents INTEGER NOT NULL,
        subtotal_cents INTEGER NOT NULL,
        gst_rate_percent REAL NOT NULL,
        gst_cents INTEGER NOT NULL,
        total_cents INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL,
        billing_instruction TEXT NOT NULL,
        line_item_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)


def _to_int_or_default(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def resolve_organisation_billing_for_export(
    conn,
    org_row,
    pricing_settings,
    billing_period_start,
    billing_period_end,
    billing_instruction,
):
    d = dict(org_row)

    temp_rows = conn.execute(
        """
        SELECT *
        FROM temporary_user_access
        WHERE organisation_id = ?
          AND charged_on_next_billing_cycle = 1
          AND billed_at IS NULL
          AND access_status = 'ACTIVE'
        ORDER BY created_at ASC
        """,
        (d["organisation_id"],)
    ).fetchall()

    temporary_user_count = len(temp_rows)
    temporary_user_fee_cents = sum(int(r["fee_cents"]) for r in temp_rows)

    org_admin_billing = get_org_admin_billing_summary(conn, d["organisation_id"])
    additional_org_admin_count = org_admin_billing["billable_additional_org_admin_count"]
    additional_org_admin_fee_cents = org_admin_billing["additional_org_admin_subtotal_cents"]

    base_subscription_subtotal_cents = _to_int_or_default(d["commercial_custom_price_cents"], 0)

    subtotal_cents = (
        base_subscription_subtotal_cents
        + temporary_user_fee_cents
        + additional_org_admin_fee_cents
    )

    free_period_days = _to_int_or_default(d["commercial_free_period_days"], 0)
    beta_tester = _to_int_or_default(d["commercial_beta_tester"], 0) == 1
    discount_percent = _to_int_or_default(d["commercial_discount_percent"], 0)

    if discount_percent < 0:
        discount_percent = 0
    if discount_percent > 100:
        discount_percent = 100

    if free_period_days > 0:
        subtotal_cents = 0
    else:
        if beta_tester:
            subtotal_cents = int(round(subtotal_cents * 0.5))

        if discount_percent > 0:
            subtotal_cents = int(round(subtotal_cents * (100 - discount_percent) / 100))

    gst_rate_percent = pricing_settings["gst_rate_percent"] if pricing_settings else 10.0
    gst_cents = int(round(subtotal_cents * (gst_rate_percent / 100.0)))
    total_cents = subtotal_cents + gst_cents

    return {
        "item": {
            "organisation_id": d["organisation_id"],
            "organisation_name": d["organisation_name"],
            "subscription_mode": d["subscription_mode"],
            "subscription_status": d["subscription_status"],
            "billing_status": d["billing_status"],
            "pricing_plan_id": d["pricing_plan_id"],
            "billing_period_start": billing_period_start,
            "billing_period_end": billing_period_end,
            "currency": "AUD",
            "subscription_subtotal_cents": base_subscription_subtotal_cents,
            "temporary_user_count": temporary_user_count,
            "temporary_user_fee_cents": temporary_user_fee_cents,
            "additional_org_admin_count": additional_org_admin_count,
            "additional_org_admin_fee_cents": additional_org_admin_fee_cents,
            "org_admin_billing": org_admin_billing,
            "subtotal_cents": subtotal_cents,
            "gst_rate_percent": gst_rate_percent,
            "gst_cents": gst_cents,
            "total_cents": total_cents,
            "amount_cents": total_cents,
            "commercial_free_period_days": free_period_days,
            "commercial_beta_tester": 1 if beta_tester else 0,
            "commercial_discount_percent": discount_percent,
            "commercial_custom_price_cents": base_subscription_subtotal_cents,
            "commercial_package_name": d.get("commercial_package_name"),
            "commercial_package_description": d.get("commercial_package_description"),
            "commercial_package_details": d.get("commercial_package_details"),
            "commercial_approved_user_limit": d.get("commercial_approved_user_limit"),
            "commercial_top_user_limit": d.get("commercial_top_user_limit"),
            "commercial_review_threshold": d.get("commercial_review_threshold"),
            "commercial_settings_effective_from": d.get("commercial_settings_effective_from"),
            "commercial_settings_effective_to": d.get("commercial_settings_effective_to"),
            "temporary_user_access_ids": [r["temporary_user_access_id"] for r in temp_rows],
            "billing_instruction": billing_instruction,
        }
    }


# ── Route registration ─────────────────────────────────────────────────────────

def register_billing_routes(app):

    @app.post("/global-admin/billing-export-preview")
    def billing_export_preview():
        body = request.get_json(silent=True) or {}
        billing_period_start = body.get("billing_period_start")
        billing_period_end = body.get("billing_period_end")
        created_by_display_name = (body.get("created_by_display_name") or "Global Admin").strip()

        conn = get_conn()
        ensure_subscription_guard_tables(conn)
        ensure_temporary_user_billing_columns(conn)
        ensure_org_commercial_settings_columns(conn)

        settings = conn.execute(
            "SELECT * FROM pricing_settings ORDER BY created_at ASC LIMIT 1"
        ).fetchone()

        gst_rate_percent = settings["gst_rate_percent"] if settings else 10.0

        orgs = conn.execute(
            """
            SELECT
                o.organisation_id,
                o.name AS organisation_name,
                COALESCE(s.subscription_mode, 'STANDARD') AS subscription_mode,
                COALESCE(s.subscription_status, 'ACTIVE') AS subscription_status,
                COALESCE(s.billing_status, 'BILLABLE') AS billing_status,
                COALESCE(s.do_not_bill, 0) AS do_not_bill,
                COALESCE(s.commercial_free_period_days, 0) AS commercial_free_period_days,
                COALESCE(s.commercial_beta_tester, 0) AS commercial_beta_tester,
                COALESCE(s.commercial_discount_percent, 0) AS commercial_discount_percent,
                s.commercial_custom_price_cents,
                s.commercial_package_name,
                s.commercial_package_description,
                s.commercial_package_details,
                s.commercial_approved_user_limit,
                s.commercial_top_user_limit,
                s.commercial_review_threshold,
                s.commercial_settings_effective_from,
                s.commercial_settings_effective_to,
                s.unsubscribed_at,
                s.pricing_plan_id
            FROM organisations o
            LEFT JOIN organisation_subscriptions s
                ON s.organisation_id = o.organisation_id
            ORDER BY o.name ASC
            """
        ).fetchall()

        export_items = []
        excluded = []

        for row in orgs:
            d = dict(row)

            if d["do_not_bill"] == 1 or d["subscription_status"] in ("CANCELLED", "UNSUBSCRIBED") or d["billing_status"] == "DO_NOT_BILL":
                excluded.append({
                    "organisation_id": d["organisation_id"],
                    "organisation_name": d["organisation_name"],
                    "reason": "Organisation is marked do-not-bill / unsubscribed.",
                })
                continue
            resolved = resolve_organisation_billing_for_export(
                conn=conn,
                org_row=d,
                pricing_settings=settings,
                billing_period_start=billing_period_start,
                billing_period_end=billing_period_end,
                billing_instruction="PREVIEW_ONLY",
            )
            export_items.append(resolved["item"])

        run_id = make_id("bexp")
        ts = now_iso()

        conn.execute(
            """
            INSERT INTO billing_export_runs (
                billing_export_run_id,
                export_status,
                export_type,
                created_by_display_name,
                billing_period_start,
                billing_period_end,
                organisation_count,
                do_not_bill_excluded_count,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "PREVIEW",
                "THIRD_PARTY_BILLER",
                created_by_display_name,
                billing_period_start,
                billing_period_end,
                len(export_items),
                len(excluded),
                ts,
            )
        )

        conn.commit()
        conn.close()

        return jsonify({
            "billing_export_run_id": run_id,
            "export_status": "PREVIEW",
            "export_type": "THIRD_PARTY_BILLER",
            "organisation_count": len(export_items),
            "do_not_bill_excluded_count": len(excluded),
            "items": export_items,
            "excluded": excluded,
            "rule": "Organisations marked unsubscribed or do-not-bill must not be exported for billing.",
            "temporary_user_rule": "Active temporary user access marked for next-cycle billing is included in export preview.",
        }), 200


    @app.post("/global-admin/billing-export-finalise")
    def billing_export_finalise():
        body = request.get_json(silent=True) or {}
        billing_period_start = body.get("billing_period_start")
        billing_period_end = body.get("billing_period_end")
        created_by_display_name = (body.get("created_by_display_name") or "Global Admin").strip()

        conn = get_conn()
        ensure_subscription_guard_tables(conn)
        ensure_temporary_user_billing_columns(conn)
        ensure_billing_export_snapshot_tables(conn)
        ensure_org_commercial_settings_columns(conn)

        settings = conn.execute(
            "SELECT * FROM pricing_settings ORDER BY created_at ASC LIMIT 1"
        ).fetchone()

        gst_rate_percent = settings["gst_rate_percent"] if settings else 10.0

        orgs = conn.execute(
            """
            SELECT
                o.organisation_id,
                o.name AS organisation_name,
                COALESCE(s.subscription_mode, 'STANDARD') AS subscription_mode,
                COALESCE(s.subscription_status, 'ACTIVE') AS subscription_status,
                COALESCE(s.billing_status, 'BILLABLE') AS billing_status,
                COALESCE(s.do_not_bill, 0) AS do_not_bill,
                COALESCE(s.commercial_free_period_days, 0) AS commercial_free_period_days,
                COALESCE(s.commercial_beta_tester, 0) AS commercial_beta_tester,
                COALESCE(s.commercial_discount_percent, 0) AS commercial_discount_percent,
                s.commercial_custom_price_cents,
                s.commercial_package_name,
                s.commercial_package_description,
                s.commercial_package_details,
                s.commercial_approved_user_limit,
                s.commercial_top_user_limit,
                s.commercial_review_threshold,
                s.commercial_settings_effective_from,
                s.commercial_settings_effective_to,
                s.unsubscribed_at,
                s.pricing_plan_id
            FROM organisations o
            LEFT JOIN organisation_subscriptions s
                ON s.organisation_id = o.organisation_id
            ORDER BY o.name ASC
            """
        ).fetchall()

        run_id = make_id("bexp")
        ts = now_iso()

        export_items = []
        excluded = []
        temp_ids_to_mark = []

        for row in orgs:
            d = dict(row)

            if d["do_not_bill"] == 1 or d["subscription_status"] in ("CANCELLED", "UNSUBSCRIBED") or d["billing_status"] == "DO_NOT_BILL":
                excluded.append({
                    "organisation_id": d["organisation_id"],
                    "organisation_name": d["organisation_name"],
                    "reason": "Organisation is marked do-not-bill / unsubscribed.",
                })
                continue
            resolved = resolve_organisation_billing_for_export(
                conn=conn,
                org_row=d,
                pricing_settings=settings,
                billing_period_start=billing_period_start,
                billing_period_end=billing_period_end,
                billing_instruction="FINALISE_FOR_THIRD_PARTY_BILLER",
            )
            item = resolved["item"]
            export_items.append(item)
            temp_ids_to_mark.extend(item["temporary_user_access_ids"])

        conn.execute(
            """
            INSERT INTO billing_export_runs (
                billing_export_run_id,
                export_status,
                export_type,
                created_by_display_name,
                billing_period_start,
                billing_period_end,
                organisation_count,
                do_not_bill_excluded_count,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "FINALISED",
                "THIRD_PARTY_BILLER",
                created_by_display_name,
                billing_period_start,
                billing_period_end,
                len(export_items),
                len(excluded),
                ts,
            )
        )

        for item in export_items:
            conn.execute(
                """
                INSERT INTO billing_export_line_items (
                    billing_export_line_item_id,
                    billing_export_run_id,
                    organisation_id,
                    organisation_name,
                    currency,
                    subscription_subtotal_cents,
                    temporary_user_count,
                    temporary_user_fee_cents,
                    subtotal_cents,
                    gst_rate_percent,
                    gst_cents,
                    total_cents,
                    amount_cents,
                    billing_instruction,
                    line_item_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    make_id("bline"),
                    run_id,
                    item["organisation_id"],
                    item["organisation_name"],
                    item["currency"],
                    item["subscription_subtotal_cents"],
                    item["temporary_user_count"],
                    item["temporary_user_fee_cents"],
                    item["subtotal_cents"],
                    item["gst_rate_percent"],
                    item["gst_cents"],
                    item["total_cents"],
                    item["amount_cents"],
                    item["billing_instruction"],
                    json.dumps(item, sort_keys=True),
                    ts,
                )
            )

        for temp_id in temp_ids_to_mark:
            conn.execute(
                """
                UPDATE temporary_user_access
                SET billed_at = ?,
                    billing_export_run_id = ?,
                    updated_at = ?
                WHERE temporary_user_access_id = ?
                """,
                (ts, run_id, ts, temp_id)
            )

        audit_event(
            conn,
            entity_type="BillingExportRun",
            entity_id=run_id,
            action="FINALISE",
            summary=f"Billing export finalised with {len(export_items)} billable organisations and {len(excluded)} do-not-bill exclusions. Line-item snapshots stored.",
            organisation_id=None,
        )

        conn.commit()
        conn.close()

        return jsonify({
            "billing_export_run_id": run_id,
            "export_status": "FINALISED",
            "export_type": "THIRD_PARTY_BILLER",
            "organisation_count": len(export_items),
            "do_not_bill_excluded_count": len(excluded),
            "temporary_user_access_marked_billed_count": len(temp_ids_to_mark),
            "line_item_snapshot_count": len(export_items),
            "items": export_items,
            "excluded": excluded,
            "rule": "Finalised billing exports exclude unsubscribed/do-not-bill organisations, mark included temporary user fees as billed, and store billing line snapshots for audit.",
        }), 200


    @app.get("/global-admin/billing-export-runs")
    def list_billing_export_runs():
        conn = get_conn()
        ensure_subscription_guard_tables(conn)
        ensure_billing_export_snapshot_tables(conn)

        runs = conn.execute(
            """
            SELECT *
            FROM billing_export_runs
            ORDER BY created_at DESC
            LIMIT 50
            """
        ).fetchall()

        conn.close()

        return jsonify({
            "count": len(runs),
            "items": [dict(r) for r in runs],
        }), 200


    @app.get("/global-admin/billing-export-runs/<billing_export_run_id>")
    def get_billing_export_run(billing_export_run_id):
        conn = get_conn()
        ensure_subscription_guard_tables(conn)
        ensure_billing_export_snapshot_tables(conn)

        run = conn.execute(
            """
            SELECT *
            FROM billing_export_runs
            WHERE billing_export_run_id = ?
            """,
            (billing_export_run_id,)
        ).fetchone()

        if not run:
            conn.close()
            return jsonify({"error": "Billing export run not found"}), 404

        rows = conn.execute(
            """
            SELECT *
            FROM billing_export_line_items
            WHERE billing_export_run_id = ?
            ORDER BY organisation_name ASC
            """,
            (billing_export_run_id,)
        ).fetchall()

        items = []
        for row in rows:
            d = dict(row)
            d["line_item"] = json.loads(d["line_item_json"])
            items.append(d)

        conn.close()

        return jsonify({
            "billing_export_run": dict(run),
            "line_item_count": len(items),
            "line_items": items,
        }), 200


    @app.get("/global-admin/billing-export-runs/<billing_export_run_id>/third-party-payload")
    def get_third_party_biller_payload(billing_export_run_id):
        conn = get_conn()
        ensure_subscription_guard_tables(conn)
        ensure_billing_export_snapshot_tables(conn)

        run = conn.execute(
            """
            SELECT *
            FROM billing_export_runs
            WHERE billing_export_run_id = ?
            """,
            (billing_export_run_id,)
        ).fetchone()

        if not run:
            conn.close()
            return jsonify({"error": "Billing export run not found"}), 404

        if run["export_status"] != "FINALISED":
            conn.close()
            return jsonify({
                "error": "Only finalised billing exports can be sent to the third-party biller",
                "export_status": run["export_status"],
            }), 400

        rows = conn.execute(
            """
            SELECT *
            FROM billing_export_line_items
            WHERE billing_export_run_id = ?
            ORDER BY organisation_name ASC
            """,
            (billing_export_run_id,)
        ).fetchall()

        payload_items = []

        for row in rows:
            line = json.loads(row["line_item_json"])

            payload_items.append({
                "external_customer_reference": row["organisation_id"],
                "customer_name": row["organisation_name"],
                "billing_export_run_id": billing_export_run_id,
                "billing_period_start": line.get("billing_period_start"),
                "billing_period_end": line.get("billing_period_end"),
                "currency": row["currency"],
                "subtotal_cents": row["subtotal_cents"],
                "gst_cents": row["gst_cents"],
                "total_cents": row["total_cents"],
                "amount_cents": row["amount_cents"],
                "line_items": [
                    {
                        "description": "Subscription subtotal",
                        "amount_cents": row["subscription_subtotal_cents"],
                    },
                    {
                        "description": "Temporary user access fees",
                        "quantity": row["temporary_user_count"],
                        "amount_cents": row["temporary_user_fee_cents"],
                        "temporary_user_access_ids": line.get("temporary_user_access_ids", []),
                    },
                    {
                        "description": "Additional Org Admin upgrades",
                        "quantity": line.get("additional_org_admin_count", 0),
                        "amount_cents": line.get("additional_org_admin_fee_cents", 0),
                        "rule": "One Org Admin is included. Additional active Org Admins are paid upgrades.",
                    },
                    {
                        "description": "GST",
                        "gst_rate_percent": row["gst_rate_percent"],
                        "amount_cents": row["gst_cents"],
                    },
                ],
                "billing_instruction": row["billing_instruction"],
            })

        total_amount_cents = sum(int(item["amount_cents"]) for item in payload_items)

        conn.close()

        return jsonify({
            "payload_type": "THIRD_PARTY_BILLER_EXPORT",
            "billing_export_run_id": billing_export_run_id,
            "export_status": run["export_status"],
            "export_type": run["export_type"],
            "billing_period_start": run["billing_period_start"],
            "billing_period_end": run["billing_period_end"],
            "created_at": run["created_at"],
            "created_by_display_name": run["created_by_display_name"],
            "organisation_count": len(payload_items),
            "total_amount_cents": total_amount_cents,
            "currency": "AUD",
            "items": payload_items,
            "privacy_rule": "This payload contains billing/accounting data only. It does not include operational pallet transaction data.",
        }), 200


    @app.post("/global-admin/organisations/<organisation_id>/subscription-mode")
    def set_organisation_subscription_mode(organisation_id):
        body = request.get_json(silent=True) or {}

        subscription_mode = (body.get("subscription_mode") or "").strip().upper()
        custom_pricing_notes = (body.get("custom_pricing_notes") or "").strip() or None
        billing_anniversary_day = body.get("billing_anniversary_day")
        pricing_plan_id = body.get("pricing_plan_id")
        changed_by_display_name = (body.get("changed_by_display_name") or "Super Global Admin").strip()
        confirmation_text = (body.get("confirmation_text") or "").strip()

        required_confirmation = "CHANGE SUBSCRIPTION MODE"

        allowed_modes = {
            "STANDARD",
            "CUSTOM",
            "FREE",
            "BETA_TESTER",
            "QUOTED",
            "SUSPENDED",
        }

        if subscription_mode not in allowed_modes:
            return jsonify({
                "error": "Invalid subscription_mode",
                "allowed_modes": sorted(allowed_modes),
            }), 400

        if confirmation_text != required_confirmation:
            return jsonify({
                "error": "Confirmation text is required before changing subscription mode",
                "required_confirmation_text": required_confirmation,
                "received_confirmation_text": confirmation_text,
                "rule": "Only Super Global Admin should change subscription mode. This action must be deliberate and audited.",
            }), 400

        if billing_anniversary_day is not None:
            try:
                billing_anniversary_day = int(billing_anniversary_day)
            except Exception:
                return jsonify({"error": "billing_anniversary_day must be an integer from 1 to 28"}), 400

            if billing_anniversary_day < 1 or billing_anniversary_day > 28:
                return jsonify({"error": "billing_anniversary_day must be between 1 and 28"}), 400

        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        get_or_create_subscription(conn, organisation_id)

        ts = now_iso()

        if subscription_mode in ("STANDARD", "CUSTOM"):
            subscription_status = "ACTIVE"
            billing_status = "BILLABLE"
            do_not_bill = 0
        elif subscription_mode == "QUOTED":
            subscription_status = "ACTIVE"
            billing_status = "MANUAL_REVIEW"
            do_not_bill = 1
        elif subscription_mode in ("FREE", "BETA_TESTER"):
            subscription_status = "ACTIVE"
            billing_status = "FREE"
            do_not_bill = 1
        elif subscription_mode == "SUSPENDED":
            subscription_status = "SUSPENDED"
            billing_status = "DO_NOT_BILL"
            do_not_bill = 1

        conn.execute(
            """
            UPDATE organisation_subscriptions
            SET subscription_mode = ?,
                subscription_status = ?,
                billing_status = ?,
                do_not_bill = ?,
                billing_anniversary_day = COALESCE(?, billing_anniversary_day),
                pricing_plan_id = ?,
                custom_pricing_notes = ?,
                updated_at = ?
            WHERE organisation_id = ?
            """,
            (
                subscription_mode,
                subscription_status,
                billing_status,
                do_not_bill,
                billing_anniversary_day,
                pricing_plan_id,
                custom_pricing_notes,
                ts,
                organisation_id,
            )
        )

        audit_event(
            conn,
            entity_type="OrganisationSubscription",
            entity_id=organisation_id,
            action="SET_SUBSCRIPTION_MODE",
            summary=f"Subscription mode changed to {subscription_mode} by {changed_by_display_name}.",
            organisation_id=organisation_id,
        )

        conn.commit()

        sub = conn.execute(
            "SELECT * FROM organisation_subscriptions WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()

        access_payload = get_org_access_status_payload(conn, organisation_id)

        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "organisation_name": org["name"],
            "subscription": dict(sub),
            "access_status": access_payload,
            "changed_by_display_name": changed_by_display_name,
            "rule": "Subscription mode changes are Super Global Admin actions and must be audited.",
        }), 200


    @app.post("/global-admin/pricing-settings")
    def update_global_pricing_settings():
        body = request.get_json(silent=True) or {}

        confirmation_text = (body.get("confirmation_text") or "").strip()
        changed_by_display_name = (body.get("changed_by_display_name") or "Super Global Admin").strip()

        required_confirmation = "UPDATE PRICING SETTINGS"

        if confirmation_text != required_confirmation:
            return jsonify({
                "error": "Confirmation text is required before changing pricing settings",
                "required_confirmation_text": required_confirmation,
                "received_confirmation_text": confirmation_text,
                "rule": "Pricing settings affect billing and must be changed deliberately.",
            }), 400

        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        settings = conn.execute(
            "SELECT * FROM pricing_settings ORDER BY created_at ASC LIMIT 1"
        ).fetchone()

        if not settings:
            conn.close()
            return jsonify({"error": "Pricing settings not found"}), 404

        updates = {}
        errors = []

        if "temporary_user_access_fee_cents" in body:
            try:
                value = int(body["temporary_user_access_fee_cents"])
                if value < 0:
                    errors.append("temporary_user_access_fee_cents must be zero or greater")
                else:
                    updates["temporary_user_access_fee_cents"] = value
            except Exception:
                errors.append("temporary_user_access_fee_cents must be an integer")

        if "temporary_access_days" in body:
            try:
                value = int(body["temporary_access_days"])
                if value < 1:
                    errors.append("temporary_access_days must be at least 1")
                else:
                    updates["temporary_access_days"] = value
            except Exception:
                errors.append("temporary_access_days must be an integer")

        if "gst_rate_percent" in body:
            try:
                value = float(body["gst_rate_percent"])
                if value < 0:
                    errors.append("gst_rate_percent must be zero or greater")
                else:
                    updates["gst_rate_percent"] = value
            except Exception:
                errors.append("gst_rate_percent must be a number")

        if "currency" in body:
            value = (body.get("currency") or "").strip().upper()
            if not value:
                errors.append("currency cannot be blank")
            else:
                updates["currency"] = value

        if errors:
            conn.close()
            return jsonify({"error": "Invalid pricing settings", "details": errors}), 400

        if not updates:
            conn.close()
            return jsonify({"error": "No pricing setting changes supplied"}), 400

        ts = now_iso()
        updates["updated_at"] = ts

        set_clause = ", ".join([f"{key} = ?" for key in updates.keys()])
        values = list(updates.values())
        values.append(settings["pricing_settings_id"])

        conn.execute(
            f"""
            UPDATE pricing_settings
            SET {set_clause}
            WHERE pricing_settings_id = ?
            """,
            values
        )

        audit_event(
            conn,
            entity_type="PricingSettings",
            entity_id=settings["pricing_settings_id"],
            action="UPDATE",
            summary=f"Pricing settings updated by {changed_by_display_name}.",
            organisation_id=None,
        )

        conn.commit()

        updated = conn.execute(
            "SELECT * FROM pricing_settings WHERE pricing_settings_id = ?",
            (settings["pricing_settings_id"],)
        ).fetchone()

        conn.close()

        return jsonify({
            "pricing_settings": dict(updated),
            "changed_by_display_name": changed_by_display_name,
            "rule": "Pricing settings updates are Super Global Admin actions and must be audited.",
        }), 200


    @app.post("/global-admin/pricing-plans/<pricing_plan_id>")
    def update_global_pricing_plan(pricing_plan_id):
        body = request.get_json(silent=True) or {}

        confirmation_text = (body.get("confirmation_text") or "").strip()
        changed_by_display_name = (body.get("changed_by_display_name") or "Super Global Admin").strip()

        required_confirmation = "UPDATE PRICING PLAN"

        if confirmation_text != required_confirmation:
            return jsonify({
                "error": "Confirmation text is required before changing a pricing plan",
                "required_confirmation_text": required_confirmation,
                "received_confirmation_text": confirmation_text,
                "rule": "Pricing plan changes affect billing and must be changed deliberately.",
            }), 400

        conn = get_conn()
        ensure_subscription_guard_tables(conn)

        plan = conn.execute(
            "SELECT * FROM pricing_plans WHERE pricing_plan_id = ?",
            (pricing_plan_id,)
        ).fetchone()

        if not plan:
            conn.close()
            return jsonify({"error": "Pricing plan not found"}), 404

        allowed_text_fields = {
            "plan_name",
            "plan_type",
            "notes",
        }

        allowed_integer_fields = {
            "min_permanent_users",
            "max_permanent_users",
            "price_per_user_cents",
            "package_price_cents",
            "requires_custom_pricing",
            "sort_order",
            "is_active",
        }

        updates = {}
        errors = []

        for field in allowed_text_fields:
            if field in body:
                updates[field] = (body.get(field) or "").strip()

        for field in allowed_integer_fields:
            if field in body:
                value = body.get(field)
                if value is None or value == "":
                    updates[field] = None
                    continue
                try:
                    updates[field] = int(value)
                except Exception:
                    errors.append(f"{field} must be an integer or null")

        if "requires_custom_pricing" in updates and updates["requires_custom_pricing"] not in (0, 1, None):
            errors.append("requires_custom_pricing must be 0 or 1")

        if "is_active" in updates and updates["is_active"] not in (0, 1, None):
            errors.append("is_active must be 0 or 1")

        if "min_permanent_users" in updates and updates["min_permanent_users"] is not None and updates["min_permanent_users"] < 0:
            errors.append("min_permanent_users must be zero or greater")

        if "max_permanent_users" in updates and updates["max_permanent_users"] is not None and updates["max_permanent_users"] < 0:
            errors.append("max_permanent_users must be zero or greater")

        if "price_per_user_cents" in updates and updates["price_per_user_cents"] is not None and updates["price_per_user_cents"] < 0:
            errors.append("price_per_user_cents must be zero or greater")

        if "package_price_cents" in updates and updates["package_price_cents"] is not None and updates["package_price_cents"] < 0:
            errors.append("package_price_cents must be zero or greater")

        if errors:
            conn.close()
            return jsonify({"error": "Invalid pricing plan update", "details": errors}), 400

        if not updates:
            conn.close()
            return jsonify({"error": "No pricing plan changes supplied"}), 400

        updates["updated_at"] = now_iso()

        set_clause = ", ".join([f"{key} = ?" for key in updates.keys()])
        values = list(updates.values())
        values.append(pricing_plan_id)

        conn.execute(
            f"""
            UPDATE pricing_plans
            SET {set_clause}
            WHERE pricing_plan_id = ?
            """,
            values
        )

        audit_event(
            conn,
            entity_type="PricingPlan",
            entity_id=pricing_plan_id,
            action="UPDATE",
            summary=f"Pricing plan updated by {changed_by_display_name}.",
            organisation_id=None,
        )

        conn.commit()

        updated = conn.execute(
            "SELECT * FROM pricing_plans WHERE pricing_plan_id = ?",
            (pricing_plan_id,)
        ).fetchone()

        conn.close()

        return jsonify({
            "pricing_plan": dict(updated),
            "changed_by_display_name": changed_by_display_name,
            "rule": "Pricing plan updates are Super Global Admin actions and must be audited.",
        }), 200
