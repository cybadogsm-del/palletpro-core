import os
import time
import threading
from collections import defaultdict

from flask import Flask, request, jsonify, g
from audit import audit_event
from db import DB, get_conn, make_id, now_iso
from modules.subscription_access import (
    classify_org_access_state,
    count_active_permanent_users,
    ensure_org_user_cap_column,
    ensure_subscription_guard_tables,
    ensure_temporary_user_billing_columns,
    get_or_create_subscription,
    get_org_access_status_payload,
    get_subscription_for_access_guard,
    register_subscription_routes,
    require_active_org_access,
    ORG_SELF_SERVE_USER_LIMIT,
)
from modules.admin_handover import ensure_admin_handover_tables, register_admin_handover_routes
from modules.auth import ensure_api_key_tables, register_auth_middleware, register_auth_routes
from modules.stocktake import ensure_stocktake_tables, register_stocktake_routes
from modules.error_logging import ensure_error_logging_tables, log_error_event, register_error_logging_routes
from modules.depots import register_depot_routes
from modules.feature_flags import register_feature_flag_routes
from modules.partners import (
    ensure_partner_address_tables,
    ensure_partner_connection_tables,
    build_partner_address_navigation_contract,
    register_partner_routes,
)
from modules.resources import register_resource_routes
from modules.stock_position import register_stock_position_routes
from modules.system_routes import register_system_routes
from modules.transaction_reporting import register_transaction_reporting_routes
from modules.transactions import (
    ensure_transaction_partner_columns,
    ensure_transaction_user_attribution_columns,
    ensure_transaction_numbering_tables,
    generate_transaction_reference,
    post_transaction_to_ledger,
    register_transaction_routes,
)
from modules.shared_transactions import (
    record_shared_transaction_event,
    register_shared_transaction_routes,
)

app = Flask(__name__)

# ── Security: rate limiter (login brute-force protection) ─────────────────────
_rate_lock  = threading.Lock()
_rate_store: dict = defaultdict(list)   # ip -> [timestamp, ...]
_RATE_WINDOW = 60       # seconds
_RATE_MAX    = 10       # max attempts per window

_PRIVATE_PREFIXES = ("127.", "10.", "172.16.", "172.17.", "172.18.", "172.19.",
                     "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
                     "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.",
                     "192.168.", "::1", "fd", "fc")

def _get_client_ip() -> str:
    """
    Return the real client IP, safe against X-Forwarded-For spoofing.
    - Direct connection: use remote_addr (TCP-level, un-spoofable).
    - Behind a trusted proxy (remote_addr is private/loopback): use the
      RIGHTMOST entry in X-Forwarded-For — that is the IP the proxy added,
      which the client cannot forge (they can only prepend to the list).
    """
    remote = request.remote_addr or "unknown"
    if not any(remote.startswith(p) for p in _PRIVATE_PREFIXES):
        return remote
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        ips = [ip.strip() for ip in xff.split(",") if ip.strip()]
        if ips:
            return ips[-1]
    return remote

def _is_rate_limited() -> bool:
    ip = _get_client_ip()
    now = time.time()
    with _rate_lock:
        attempts = [t for t in _rate_store[ip] if now - t < _RATE_WINDOW]
        attempts.append(now)
        _rate_store[ip] = attempts
        return len(attempts) > _RATE_MAX

# ── Security: CORS + strip Server header ─────────────────────────────────────
ALLOWED_ORIGINS = {
    "http://localhost:3000",
    "http://localhost:5173",
}

@app.after_request
def apply_security_headers(response):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"]  = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Max-Age"]       = "600"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"]        = "DENY"
    response.headers["Server"]                 = "Pallet Pro"
    return response

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        from flask import make_response
        origin = request.headers.get("Origin", "")
        resp = make_response("", 204)
        if origin in ALLOWED_ORIGINS:
            resp.headers["Access-Control-Allow-Origin"]  = origin
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            resp.headers["Access-Control-Max-Age"]       = "600"
        return resp

# ─────────────────────────────────────────────────────────────────────────────


def create_pending_entry(
    conn,
    organisation_id,
    entry_type,
    source_record_id,
    source_module,
    submitted_by_display_name,
    related_entity_type,
    related_entity_id,
    related_entity_name,
    reason_code,
    reason_text,
    direct_action_type,
    direct_action_target_id,
    direct_action_label,
    can_approve_now,
    can_reject_now,
    resource_id=None,
    resource_name=None,
    status="PENDING_APPROVAL"
):
    pending_entry_id = make_id("pend")

    conn.execute(
        """
        INSERT INTO pending_approval_entries (
            pending_entry_id,
            organisation_id,
            entry_type,
            source_record_id,
            source_module,
            submitted_by_display_name,
            related_entity_type,
            related_entity_id,
            related_entity_name,
            resource_id,
            resource_name,
            status,
            reason_code,
            reason_text,
            direct_action_type,
            direct_action_target_id,
            direct_action_label,
            can_approve_now,
            can_reject_now,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pending_entry_id,
            organisation_id,
            entry_type,
            source_record_id,
            source_module,
            submitted_by_display_name,
            related_entity_type,
            related_entity_id,
            related_entity_name,
            resource_id,
            resource_name,
            status,
            reason_code,
            reason_text,
            direct_action_type,
            direct_action_target_id,
            direct_action_label,
            1 if can_approve_now else 0,
            1 if can_reject_now else 0,
            now_iso(),
            now_iso()
        )
    )

    audit_event(
        conn,
        entity_type="PendingApprovalEntry",
        entity_id=pending_entry_id,
        action="CREATE",
        summary=f"Created pending approval entry: {entry_type} / {reason_code}",
        organisation_id=organisation_id
    )

    return pending_entry_id


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS organisations (
        organisation_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS depots (
        depot_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        name TEXT NOT NULL,
        opening_balance_used INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS partners (
        partner_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        name TEXT NOT NULL,
        is_customer INTEGER NOT NULL,
        is_supplier INTEGER NOT NULL,
        is_active INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS brands (
        brand_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        name TEXT NOT NULL,
        is_active INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        category_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        name TEXT NOT NULL,
        is_active INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS brand_requests (
        brand_request_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        requested_name TEXT NOT NULL,
        note TEXT,
        submitted_by_display_name TEXT,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS category_requests (
        category_request_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        requested_name TEXT NOT NULL,
        note TEXT,
        submitted_by_display_name TEXT,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS resource_requests (
        resource_request_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        category_id TEXT,
        brand_id TEXT,
        requested_name TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        unit_type TEXT NOT NULL,
        note TEXT,
        submitted_by_display_name TEXT,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS resources (
        resource_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        category_id TEXT,
        brand_id TEXT,
        name TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        unit_type TEXT NOT NULL,
        is_active INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        transaction_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        depot_id TEXT NOT NULL,
        transaction_type TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        direction TEXT NOT NULL,
        status TEXT NOT NULL,
        approval_reason_code TEXT,
        approval_reason_text TEXT,
        created_at TEXT NOT NULL,
        posted_at TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS ledger_entries (
        ledger_entry_id TEXT PRIMARY KEY,
        transaction_id TEXT NOT NULL,
        organisation_id TEXT NOT NULL,
        depot_id TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        quantity_delta INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS balance_projection (
        balance_projection_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        depot_id TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        current_quantity INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (organisation_id, depot_id, resource_id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS audit_events (
        event_id TEXT PRIMARY KEY,
        organisation_id TEXT,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS pending_approval_entries (
        pending_entry_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        entry_type TEXT NOT NULL,
        source_record_id TEXT NOT NULL,
        source_module TEXT NOT NULL,
        submitted_by_display_name TEXT,
        related_entity_type TEXT,
        related_entity_id TEXT,
        related_entity_name TEXT,
        resource_id TEXT,
        resource_name TEXT,
        status TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        reason_text TEXT NOT NULL,
        direct_action_type TEXT NOT NULL,
        direct_action_target_id TEXT,
        direct_action_label TEXT NOT NULL,
        can_approve_now INTEGER NOT NULL,
        can_reject_now INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    resource_cols = {row["name"] for row in c.execute("PRAGMA table_info(resources)").fetchall()}
    if "category_id" not in resource_cols:
        c.execute("ALTER TABLE resources ADD COLUMN category_id TEXT")
    if "brand_id" not in resource_cols:
        c.execute("ALTER TABLE resources ADD COLUMN brand_id TEXT")

    request_cols = {row["name"] for row in c.execute("PRAGMA table_info(resource_requests)").fetchall()}
    if "category_id" not in request_cols:
        c.execute("ALTER TABLE resource_requests ADD COLUMN category_id TEXT")
    if "brand_id" not in request_cols:
        c.execute("ALTER TABLE resource_requests ADD COLUMN brand_id TEXT")
    if "rejection_reason_code" not in request_cols:
        c.execute("ALTER TABLE resource_requests ADD COLUMN rejection_reason_code TEXT")
    if "rejection_reason_text" not in request_cols:
        c.execute("ALTER TABLE resource_requests ADD COLUMN rejection_reason_text TEXT")

    brand_request_cols = {row["name"] for row in c.execute("PRAGMA table_info(brand_requests)").fetchall()}
    if "rejection_reason_code" not in brand_request_cols:
        c.execute("ALTER TABLE brand_requests ADD COLUMN rejection_reason_code TEXT")
    if "rejection_reason_text" not in brand_request_cols:
        c.execute("ALTER TABLE brand_requests ADD COLUMN rejection_reason_text TEXT")

    category_request_cols = {row["name"] for row in c.execute("PRAGMA table_info(category_requests)").fetchall()}
    if "rejection_reason_code" not in category_request_cols:
        c.execute("ALTER TABLE category_requests ADD COLUMN rejection_reason_code TEXT")
    if "rejection_reason_text" not in category_request_cols:
        c.execute("ALTER TABLE category_requests ADD COLUMN rejection_reason_text TEXT")

    pending_cols = {row["name"] for row in c.execute("PRAGMA table_info(pending_approval_entries)").fetchall()}
    if "rejection_reason_code" not in pending_cols:
        c.execute("ALTER TABLE pending_approval_entries ADD COLUMN rejection_reason_code TEXT")
    if "rejection_reason_text" not in pending_cols:
        c.execute("ALTER TABLE pending_approval_entries ADD COLUMN rejection_reason_text TEXT")

    txn_cols = {row["name"] for row in c.execute("PRAGMA table_info(transactions)").fetchall()}
    if "unresolved_entity_note" not in txn_cols:
        c.execute("ALTER TABLE transactions ADD COLUMN unresolved_entity_note TEXT")
    if "unresolved_entity_type" not in txn_cols:
        c.execute("ALTER TABLE transactions ADD COLUMN unresolved_entity_type TEXT")

    ensure_subscription_guard_tables(conn)
    ensure_api_key_tables(conn)
    ensure_error_logging_tables(conn)
    ensure_admin_handover_tables(conn)
    ensure_stocktake_tables(conn)
    ensure_org_user_cap_column(conn)

    conn.commit()
    conn.close()


init_db()


@app.post("/organisations")
def create_organisation():
    _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
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


@app.post("/global-admin/billing-export-preview")
def billing_export_preview():
    body = request.get_json(silent=True) or {}
    billing_period_start = body.get("billing_period_start")
    billing_period_end = body.get("billing_period_end")
    created_by_display_name = (body.get("created_by_display_name") or "Global Admin").strip()

    conn = get_conn()
    ensure_subscription_guard_tables(conn)
    ensure_temporary_user_billing_columns(conn)

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
        subscription_subtotal_cents = 0
        subtotal_cents = subscription_subtotal_cents + temporary_user_fee_cents
        gst_cents = int(round(subtotal_cents * (gst_rate_percent / 100.0)))
        total_cents = subtotal_cents + gst_cents

        export_items.append({
            "organisation_id": d["organisation_id"],
            "organisation_name": d["organisation_name"],
            "subscription_mode": d["subscription_mode"],
            "subscription_status": d["subscription_status"],
            "billing_status": d["billing_status"],
            "pricing_plan_id": d["pricing_plan_id"],
            "billing_period_start": billing_period_start,
            "billing_period_end": billing_period_end,
            "currency": "AUD",
            "subscription_subtotal_cents": subscription_subtotal_cents,
            "temporary_user_count": temporary_user_count,
            "temporary_user_fee_cents": temporary_user_fee_cents,
            "subtotal_cents": subtotal_cents,
            "gst_rate_percent": gst_rate_percent,
            "gst_cents": gst_cents,
            "total_cents": total_cents,
            "amount_cents": total_cents,
            "temporary_user_access_ids": [r["temporary_user_access_id"] for r in temp_rows],
            "billing_instruction": "PREVIEW_ONLY",
        })

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





# === BILLING EXPORT FINALISE V0.2 START ===

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


@app.post("/global-admin/billing-export-finalise")
def billing_export_finalise():
    import json

    body = request.get_json(silent=True) or {}
    billing_period_start = body.get("billing_period_start")
    billing_period_end = body.get("billing_period_end")
    created_by_display_name = (body.get("created_by_display_name") or "Global Admin").strip()

    conn = get_conn()
    ensure_subscription_guard_tables(conn)
    ensure_temporary_user_billing_columns(conn)
    ensure_billing_export_snapshot_tables(conn)

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
        subscription_subtotal_cents = 0
        subtotal_cents = subscription_subtotal_cents + temporary_user_fee_cents
        gst_cents = int(round(subtotal_cents * (gst_rate_percent / 100.0)))
        total_cents = subtotal_cents + gst_cents
        temp_ids = [r["temporary_user_access_id"] for r in temp_rows]
        temp_ids_to_mark.extend(temp_ids)

        item = {
            "organisation_id": d["organisation_id"],
            "organisation_name": d["organisation_name"],
            "subscription_mode": d["subscription_mode"],
            "subscription_status": d["subscription_status"],
            "billing_status": d["billing_status"],
            "pricing_plan_id": d["pricing_plan_id"],
            "billing_period_start": billing_period_start,
            "billing_period_end": billing_period_end,
            "currency": "AUD",
            "subscription_subtotal_cents": subscription_subtotal_cents,
            "temporary_user_count": temporary_user_count,
            "temporary_user_fee_cents": temporary_user_fee_cents,
            "subtotal_cents": subtotal_cents,
            "gst_rate_percent": gst_rate_percent,
            "gst_cents": gst_cents,
            "total_cents": total_cents,
            "amount_cents": total_cents,
            "temporary_user_access_ids": temp_ids,
            "billing_instruction": "FINALISE_FOR_THIRD_PARTY_BILLER",
        }

        export_items.append(item)

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


@app.get("/global-admin/billing-export-runs/<billing_export_run_id>")
def get_billing_export_run(billing_export_run_id):
    import json

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

# === BILLING EXPORT FINALISE V0.2 END ===

# === THIRD PARTY BILLER PAYLOAD V0.1 START ===

@app.get("/global-admin/billing-export-runs/<billing_export_run_id>/third-party-payload")
def get_third_party_biller_payload(billing_export_run_id):
    import json

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

# === THIRD PARTY BILLER PAYLOAD V0.1 END ===

# === OPERATING DATA EXPORT V0.1 START ===

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

    export_tables = [
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

# === OPERATING DATA EXPORT V0.1 END ===

# === DATA RETENTION PREVIEW V0.1 START ===

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

# === DATA RETENTION PREVIEW V0.1 END ===

# === DATA RETENTION EXECUTE V0.1 START ===

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

# === DATA RETENTION EXECUTE V0.1 END ===

# === REACTIVATION GUARD V0.1 START ===

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

# === REACTIVATION GUARD V0.1 END ===

# === SUBSCRIPTION MODE CONTROLS V0.1 START ===

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

# === SUBSCRIPTION MODE CONTROLS V0.1 END ===

# === GLOBAL ADMIN PRICING EDIT V0.1 START ===

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

# === GLOBAL ADMIN PRICING EDIT V0.1 END ===

# === USER ROLE ACCESS LAYER V0.1 START ===

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

        from datetime import datetime
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

    return jsonify({
        "user": dict(user),
        "access_policy": policy,
        "setup_token": setup_token,
        "setup_token_note": "Share this token securely with the user. They must call POST /auth/set-password to activate their account. Expires in 7 days.",
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

# === USER ROLE ACCESS LAYER V0.1 END ===

# === LOGIN INTEGRITY GUARD V0.1 START ===

ONE_DEVICE_ROLES = {
    "USER",
    "TEMPORARY_USER",
}

MULTI_DEVICE_ALLOWED_ROLES = {
    "ORG_ADMIN",
    "GLOBAL_ADMIN",
    "SUPER_GLOBAL_ADMIN",
}


def ensure_login_integrity_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_sessions (
        session_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        organisation_id TEXT,
        role TEXT NOT NULL,
        device_id TEXT NOT NULL,
        device_label TEXT,
        ip_address_hash TEXT,
        user_agent_hash TEXT,
        session_status TEXT NOT NULL,
        login_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        logout_at TEXT,
        ended_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS login_integrity_events (
        login_integrity_event_id TEXT PRIMARY KEY,
        organisation_id TEXT,
        user_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        risk_level TEXT NOT NULL,
        risk_score INTEGER NOT NULL,
        summary TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        ai_report_summary TEXT NOT NULL,
        review_status TEXT NOT NULL,
        global_admin_only INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)


def create_login_integrity_event(
    conn,
    organisation_id,
    user_id,
    event_type,
    risk_level,
    risk_score,
    summary,
    evidence,
    ai_report_summary,
):
    import json

    ensure_login_integrity_tables(conn)
    ts = now_iso()

    event_id = make_id("lie")

    conn.execute(
        """
        INSERT INTO login_integrity_events (
            login_integrity_event_id,
            organisation_id,
            user_id,
            event_type,
            risk_level,
            risk_score,
            summary,
            evidence_json,
            ai_report_summary,
            review_status,
            global_admin_only,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            organisation_id,
            user_id,
            event_type,
            risk_level,
            risk_score,
            summary,
            json.dumps(evidence, sort_keys=True),
            ai_report_summary,
            "OPEN",
            1,
            ts,
            ts,
        )
    )

    return event_id


def build_login_integrity_ai_report(event_type, role, active_session_count, ended_session_count, device_id, device_label):
    if event_type == "ONE_DEVICE_RULE_PREVIOUS_SESSION_ENDED":
        return (
            "Login Integrity Guard detected a standard/temporary user logging in from a new device while another "
            "session was active. The previous session was ended to preserve one-user-one-device integrity. "
            "Global Admin should review only if this repeats or appears connected to shared login behaviour."
        )

    if event_type == "ADMIN_MULTI_DEVICE_ACTIVITY":
        return (
            "Login Integrity Guard detected multi-device activity for an admin role. This is allowed, but recorded "
            "for Global Admin visibility because admin accounts have wider authority."
        )

    return (
        "Login Integrity Guard created a report for Global Admin review. AI reports are advisory only. "
        "Global Admin decides any action."
    )


@app.post("/sessions/login")
def create_user_session():
    if _is_rate_limited():
        return jsonify({"error": "TOO_MANY_REQUESTS", "message": "Too many login attempts. Wait a minute and try again."}), 429

    body = request.get_json(silent=True) or {}

    mobile_number = (body.get("mobile_number") or "").strip() or None
    email = (body.get("email") or "").strip() or None
    password = (body.get("password") or "").strip()
    device_id = (body.get("device_id") or "").strip()
    device_label = (body.get("device_label") or "").strip() or None
    ip_address_hash = (body.get("ip_address_hash") or "").strip() or None
    user_agent_hash = (body.get("user_agent_hash") or "").strip() or None

    if not mobile_number and not email:
        return jsonify({"error": "mobile_number or email is required"}), 400
    if not password:
        return jsonify({"error": "password is required"}), 400
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    conn = get_conn()
    ensure_user_access_tables(conn)
    ensure_login_integrity_tables(conn)

    from modules.password_auth import ensure_password_auth_columns, verify_password
    ensure_password_auth_columns(conn)

    user = None
    if mobile_number:
        user = conn.execute(
            "SELECT * FROM user_accounts WHERE mobile_number = ?", (mobile_number,)
        ).fetchone()
    if not user and email:
        user = conn.execute(
            "SELECT * FROM user_accounts WHERE email = ?", (email,)
        ).fetchone()

    # Use a constant-time response to avoid leaking whether the account exists
    password_ok = verify_password(password, user["password_hash"] if user else None)

    if not user or not password_ok:
        conn.close()
        return jsonify({"error": "Invalid credentials"}), 401

    user_id = user["user_id"]

    policy = build_user_access_policy(conn, user)

    if not policy["can_use_platform"]:
        conn.close()
        return jsonify({
            "error": "User cannot log in",
            "access_policy": policy,
        }), 403

    ts = now_iso()
    role = user["role"]
    organisation_id = user["organisation_id"]

    existing_active_sessions = conn.execute(
        """
        SELECT *
        FROM user_sessions
        WHERE user_id = ?
          AND session_status = 'ACTIVE'
        ORDER BY login_at ASC
        """,
        (user_id,)
    ).fetchall()

    ended_sessions = []

    if role in ONE_DEVICE_ROLES:
        for session in existing_active_sessions:
            if session["device_id"] != device_id:
                conn.execute(
                    """
                    UPDATE user_sessions
                    SET session_status = ?,
                        logout_at = ?,
                        ended_reason = ?,
                        updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        "ENDED",
                        ts,
                        "ONE_DEVICE_RULE_NEW_LOGIN",
                        ts,
                        session["session_id"],
                    )
                )
                ended_sessions.append(dict(session))

        if ended_sessions:
            create_login_integrity_event(
                conn=conn,
                organisation_id=organisation_id,
                user_id=user_id,
                event_type="ONE_DEVICE_RULE_PREVIOUS_SESSION_ENDED",
                risk_level="MEDIUM",
                risk_score=55,
                summary="One-user-one-device rule ended previous active session for standard/temporary user.",
                evidence={
                    "role": role,
                    "new_device_id": device_id,
                    "new_device_label": device_label,
                    "ended_session_ids": [s["session_id"] for s in ended_sessions],
                    "ended_device_ids": [s["device_id"] for s in ended_sessions],
                    "active_session_count_before_login": len(existing_active_sessions),
                },
                ai_report_summary=build_login_integrity_ai_report(
                    "ONE_DEVICE_RULE_PREVIOUS_SESSION_ENDED",
                    role,
                    len(existing_active_sessions),
                    len(ended_sessions),
                    device_id,
                    device_label,
                ),
            )

    elif role in MULTI_DEVICE_ALLOWED_ROLES and len(existing_active_sessions) >= 1:
        create_login_integrity_event(
            conn=conn,
            organisation_id=organisation_id,
            user_id=user_id,
            event_type="ADMIN_MULTI_DEVICE_ACTIVITY",
            risk_level="LOW",
            risk_score=20,
            summary="Admin user logged in while another active session already existed.",
            evidence={
                "role": role,
                "new_device_id": device_id,
                "new_device_label": device_label,
                "active_session_count_before_login": len(existing_active_sessions),
                "existing_session_ids": [s["session_id"] for s in existing_active_sessions],
            },
            ai_report_summary=build_login_integrity_ai_report(
                "ADMIN_MULTI_DEVICE_ACTIVITY",
                role,
                len(existing_active_sessions),
                0,
                device_id,
                device_label,
            ),
        )

    # Reuse same active session on same device when possible.
    same_device_session = conn.execute(
        """
        SELECT *
        FROM user_sessions
        WHERE user_id = ?
          AND device_id = ?
          AND session_status = 'ACTIVE'
        ORDER BY login_at DESC
        LIMIT 1
        """,
        (user_id, device_id)
    ).fetchone()

    if same_device_session:
        conn.execute(
            """
            UPDATE user_sessions
            SET last_seen_at = ?,
                updated_at = ?
            WHERE session_id = ?
            """,
            (ts, ts, same_device_session["session_id"])
        )

        session_id = same_device_session["session_id"]
    else:
        session_id = make_id("sess")

        conn.execute(
            """
            INSERT INTO user_sessions (
                session_id,
                user_id,
                organisation_id,
                role,
                device_id,
                device_label,
                ip_address_hash,
                user_agent_hash,
                session_status,
                login_at,
                last_seen_at,
                logout_at,
                ended_reason,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                user_id,
                organisation_id,
                role,
                device_id,
                device_label,
                ip_address_hash,
                user_agent_hash,
                "ACTIVE",
                ts,
                ts,
                None,
                None,
                ts,
                ts,
            )
        )

    conn.commit()

    session = conn.execute(
        "SELECT * FROM user_sessions WHERE session_id = ?",
        (session_id,)
    ).fetchone()

    active_sessions_now = conn.execute(
        """
        SELECT *
        FROM user_sessions
        WHERE user_id = ?
          AND session_status = 'ACTIVE'
        ORDER BY login_at DESC
        """,
        (user_id,)
    ).fetchall()

    conn.close()

    return jsonify({
        "session": dict(session),
        "user": dict(user),
        "access_policy": policy,
        "one_device_rule_applies": role in ONE_DEVICE_ROLES,
        "multi_device_allowed": role in MULTI_DEVICE_ALLOWED_ROLES,
        "ended_previous_session_count": len(ended_sessions),
        "active_session_count": len(active_sessions_now),
        "rule": "Standard and temporary users are limited to one active device. Org/Admin roles may use multiple devices but are monitored.",
    }), 201


@app.post("/sessions/<session_id>/logout")
def logout_user_session(session_id):
    body = request.get_json(silent=True) or {}
    ended_reason = (body.get("ended_reason") or "USER_LOGOUT").strip()

    conn = get_conn()
    ensure_login_integrity_tables(conn)

    session = conn.execute(
        "SELECT * FROM user_sessions WHERE session_id = ?",
        (session_id,)
    ).fetchone()

    if not session:
        conn.close()
        return jsonify({"error": "Session not found"}), 404

    ts = now_iso()

    conn.execute(
        """
        UPDATE user_sessions
        SET session_status = ?,
            logout_at = ?,
            ended_reason = ?,
            updated_at = ?
        WHERE session_id = ?
        """,
        (
            "ENDED",
            ts,
            ended_reason,
            ts,
            session_id,
        )
    )

    conn.commit()

    updated = conn.execute(
        "SELECT * FROM user_sessions WHERE session_id = ?",
        (session_id,)
    ).fetchone()

    conn.close()

    return jsonify({
        "session": dict(updated),
        "message": "Session ended.",
    }), 200


@app.get("/users/<user_id>/sessions")
def list_user_sessions(user_id):
    conn = get_conn()
    ensure_login_integrity_tables(conn)
    ensure_user_access_tables(conn)

    user = get_user_account(conn, user_id)

    if not user:
        conn.close()
        return jsonify({"error": "User not found"}), 404

    rows = conn.execute(
        """
        SELECT *
        FROM user_sessions
        WHERE user_id = ?
        ORDER BY login_at DESC
        """,
        (user_id,)
    ).fetchall()

    conn.close()

    return jsonify({
        "user": dict(user),
        "count": len(rows),
        "items": [dict(row) for row in rows],
    }), 200


@app.get("/global-admin/login-integrity-reports")
def list_login_integrity_reports():
    organisation_id = request.args.get("organisation_id")
    user_id = request.args.get("user_id")
    review_status = request.args.get("review_status")
    risk_level = request.args.get("risk_level")

    conn = get_conn()
    ensure_login_integrity_tables(conn)

    sql = """
        SELECT
            e.*,
            u.display_name,
            u.email,
            u.role,
            o.name AS organisation_name
        FROM login_integrity_events e
        LEFT JOIN user_accounts u ON u.user_id = e.user_id
        LEFT JOIN organisations o ON o.organisation_id = e.organisation_id
        WHERE e.global_admin_only = 1
    """
    params = []

    if organisation_id:
        sql += " AND e.organisation_id = ?"
        params.append(organisation_id)

    if user_id:
        sql += " AND e.user_id = ?"
        params.append(user_id)

    if review_status:
        sql += " AND e.review_status = ?"
        params.append(review_status)

    if risk_level:
        sql += " AND e.risk_level = ?"
        params.append(risk_level)

    sql += " ORDER BY e.created_at DESC"

    rows = conn.execute(sql, params).fetchall()

    conn.close()

    return jsonify({
        "report_type": "GLOBAL_ADMIN_LOGIN_INTEGRITY_REPORTS",
        "count": len(rows),
        "items": [dict(row) for row in rows],
        "rule": "Login integrity reports are for Global Admin eyes only. AI reports are advisory. Global Admin decides any course of action.",
    }), 200


@app.post("/global-admin/login-integrity-reports/<login_integrity_event_id>/review")
def review_login_integrity_report(login_integrity_event_id):
    body = request.get_json(silent=True) or {}
    review_status = (body.get("review_status") or "").strip().upper()
    reviewed_by_display_name = (body.get("reviewed_by_display_name") or "Global Admin").strip()
    review_notes = (body.get("review_notes") or "").strip() or None

    allowed_statuses = {"OPEN", "MONITORING", "DISMISSED", "ACTION_REQUIRED", "RESOLVED"}

    if review_status not in allowed_statuses:
        return jsonify({
            "error": "Invalid review_status",
            "allowed_statuses": sorted(allowed_statuses),
        }), 400

    conn = get_conn()
    ensure_login_integrity_tables(conn)

    event = conn.execute(
        """
        SELECT *
        FROM login_integrity_events
        WHERE login_integrity_event_id = ?
        """,
        (login_integrity_event_id,)
    ).fetchone()

    if not event:
        conn.close()
        return jsonify({"error": "Login integrity report not found"}), 404

    ts = now_iso()

    summary_suffix = f" Reviewed by {reviewed_by_display_name}."
    if review_notes:
        summary_suffix += f" Notes: {review_notes}"

    conn.execute(
        """
        UPDATE login_integrity_events
        SET review_status = ?,
            summary = summary || ?,
            updated_at = ?
        WHERE login_integrity_event_id = ?
        """,
        (
            review_status,
            summary_suffix,
            ts,
            login_integrity_event_id,
        )
    )

    conn.commit()

    updated = conn.execute(
        """
        SELECT *
        FROM login_integrity_events
        WHERE login_integrity_event_id = ?
        """,
        (login_integrity_event_id,)
    ).fetchone()

    conn.close()

    return jsonify({
        "login_integrity_report": dict(updated),
        "reviewed_by_display_name": reviewed_by_display_name,
        "review_notes": review_notes,
        "rule": "Global Admin reviews and decides the course of action. AI does not automatically penalise users or organisations.",
    }), 200

# === LOGIN INTEGRITY GUARD V0.1 END ===

# === LOGIN INTEGRITY DASHBOARD V0.1 START ===

@app.get("/global-admin/login-integrity-dashboard")
def get_login_integrity_dashboard():
    conn = get_conn()
    ensure_login_integrity_tables(conn)
    ensure_user_access_tables(conn)

    reports = conn.execute(
        """
        SELECT
            e.*,
            u.display_name,
            u.email,
            u.role,
            o.name AS organisation_name
        FROM login_integrity_events e
        LEFT JOIN user_accounts u ON u.user_id = e.user_id
        LEFT JOIN organisations o ON o.organisation_id = e.organisation_id
        WHERE e.global_admin_only = 1
        ORDER BY e.created_at DESC
        LIMIT 50
        """
    ).fetchall()

    active_sessions = conn.execute(
        """
        SELECT
            s.*,
            u.display_name,
            u.email,
            o.name AS organisation_name
        FROM user_sessions s
        LEFT JOIN user_accounts u ON u.user_id = s.user_id
        LEFT JOIN organisations o ON o.organisation_id = s.organisation_id
        WHERE s.session_status = 'ACTIVE'
        ORDER BY s.last_seen_at DESC
        LIMIT 100
        """
    ).fetchall()

    risk_counts = {}
    review_counts = {}
    event_type_counts = {}

    for row in reports:
        risk_counts[row["risk_level"]] = risk_counts.get(row["risk_level"], 0) + 1
        review_counts[row["review_status"]] = review_counts.get(row["review_status"], 0) + 1
        event_type_counts[row["event_type"]] = event_type_counts.get(row["event_type"], 0) + 1

    one_device_roles_active_sessions = [
        dict(row) for row in active_sessions
        if row["role"] in ("USER", "TEMPORARY_USER")
    ]

    admin_active_sessions = [
        dict(row) for row in active_sessions
        if row["role"] in ("ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN")
    ]

    conn.close()

    return jsonify({
        "dashboard_type": "GLOBAL_ADMIN_LOGIN_INTEGRITY_DASHBOARD",
        "report_count": len(reports),
        "active_session_count": len(active_sessions),
        "standard_user_active_session_count": len(one_device_roles_active_sessions),
        "admin_active_session_count": len(admin_active_sessions),
        "risk_counts": risk_counts,
        "review_status_counts": review_counts,
        "event_type_counts": event_type_counts,
        "recent_reports": [dict(row) for row in reports],
        "active_sessions": [dict(row) for row in active_sessions],
        "rules": [
            "Login integrity reports are for Global Admin eyes only.",
            "AI reports are advisory only. Global Admin decides any course of action.",
            "Standard and temporary users are limited to one active device.",
            "Org Admins and platform admins may use multiple devices, but their sessions remain logged and monitored.",
            "The purpose is to protect who/where/when integrity and the Pallet Pro pricing philosophy.",
        ],
    }), 200

# === LOGIN INTEGRITY DASHBOARD V0.1 END ===

# === LOGIN INTEGRITY REVIEW HISTORY V0.1 START ===

def ensure_login_integrity_review_tables(conn):
    ensure_login_integrity_tables(conn)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS login_integrity_review_history (
        login_integrity_review_id TEXT PRIMARY KEY,
        login_integrity_event_id TEXT NOT NULL,
        organisation_id TEXT,
        user_id TEXT NOT NULL,
        previous_review_status TEXT,
        new_review_status TEXT NOT NULL,
        reviewed_by_display_name TEXT NOT NULL,
        review_notes TEXT,
        created_at TEXT NOT NULL
    )
    """)


@app.post("/global-admin/login-integrity-reports/<login_integrity_event_id>/review-v2")
def review_login_integrity_report_v2(login_integrity_event_id):
    body = request.get_json(silent=True) or {}
    review_status = (body.get("review_status") or "").strip().upper()
    reviewed_by_display_name = (body.get("reviewed_by_display_name") or "Global Admin").strip()
    review_notes = (body.get("review_notes") or "").strip() or None

    allowed_statuses = {"OPEN", "MONITORING", "DISMISSED", "ACTION_REQUIRED", "RESOLVED"}

    if review_status not in allowed_statuses:
        return jsonify({
            "error": "Invalid review_status",
            "allowed_statuses": sorted(allowed_statuses),
        }), 400

    conn = get_conn()
    ensure_login_integrity_review_tables(conn)

    event = conn.execute(
        """
        SELECT *
        FROM login_integrity_events
        WHERE login_integrity_event_id = ?
        """,
        (login_integrity_event_id,)
    ).fetchone()

    if not event:
        conn.close()
        return jsonify({"error": "Login integrity report not found"}), 404

    previous_status = event["review_status"]
    ts = now_iso()

    conn.execute(
        """
        INSERT INTO login_integrity_review_history (
            login_integrity_review_id,
            login_integrity_event_id,
            organisation_id,
            user_id,
            previous_review_status,
            new_review_status,
            reviewed_by_display_name,
            review_notes,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            make_id("lirh"),
            login_integrity_event_id,
            event["organisation_id"],
            event["user_id"],
            previous_status,
            review_status,
            reviewed_by_display_name,
            review_notes,
            ts,
        )
    )

    summary_suffix = f" Review status changed from {previous_status} to {review_status} by {reviewed_by_display_name}."
    if review_notes:
        summary_suffix += f" Notes: {review_notes}"

    conn.execute(
        """
        UPDATE login_integrity_events
        SET review_status = ?,
            summary = summary || ?,
            updated_at = ?
        WHERE login_integrity_event_id = ?
        """,
        (
            review_status,
            summary_suffix,
            ts,
            login_integrity_event_id,
        )
    )

    conn.commit()

    updated = conn.execute(
        """
        SELECT *
        FROM login_integrity_events
        WHERE login_integrity_event_id = ?
        """,
        (login_integrity_event_id,)
    ).fetchone()

    history = conn.execute(
        """
        SELECT *
        FROM login_integrity_review_history
        WHERE login_integrity_event_id = ?
        ORDER BY created_at ASC
        """,
        (login_integrity_event_id,)
    ).fetchall()

    conn.close()

    return jsonify({
        "login_integrity_report": dict(updated),
        "review_history_count": len(history),
        "review_history": [dict(row) for row in history],
        "rule": "Global Admin review decisions are stored as review history. AI reports are advisory only.",
    }), 200


@app.get("/global-admin/login-integrity-reports/<login_integrity_event_id>/review-history")
def get_login_integrity_review_history(login_integrity_event_id):
    conn = get_conn()
    ensure_login_integrity_review_tables(conn)

    event = conn.execute(
        """
        SELECT *
        FROM login_integrity_events
        WHERE login_integrity_event_id = ?
        """,
        (login_integrity_event_id,)
    ).fetchone()

    if not event:
        conn.close()
        return jsonify({"error": "Login integrity report not found"}), 404

    history = conn.execute(
        """
        SELECT *
        FROM login_integrity_review_history
        WHERE login_integrity_event_id = ?
        ORDER BY created_at ASC
        """,
        (login_integrity_event_id,)
    ).fetchall()

    conn.close()

    return jsonify({
        "login_integrity_event_id": login_integrity_event_id,
        "current_review_status": event["review_status"],
        "review_history_count": len(history),
        "review_history": [dict(row) for row in history],
    }), 200

# === LOGIN INTEGRITY REVIEW HISTORY V0.1 END ===

# === LOGIN INTEGRITY ADMIN ACTIONS V0.1 START ===

def ensure_login_integrity_action_tables(conn):
    ensure_login_integrity_review_tables(conn)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS login_integrity_admin_actions (
        login_integrity_action_id TEXT PRIMARY KEY,
        login_integrity_event_id TEXT NOT NULL,
        organisation_id TEXT,
        user_id TEXT NOT NULL,
        action_type TEXT NOT NULL,
        action_status TEXT NOT NULL,
        assigned_to_display_name TEXT,
        action_notes TEXT,
        due_at TEXT,
        completed_at TEXT,
        created_by_display_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)


@app.post("/global-admin/login-integrity-reports/<login_integrity_event_id>/actions")
def create_login_integrity_admin_action(login_integrity_event_id):
    body = request.get_json(silent=True) or {}

    action_type = (body.get("action_type") or "").strip().upper()
    assigned_to_display_name = (body.get("assigned_to_display_name") or "").strip() or None
    action_notes = (body.get("action_notes") or "").strip() or None
    due_at = body.get("due_at")
    created_by_display_name = (body.get("created_by_display_name") or "Global Admin").strip()
    confirmation_text = (body.get("confirmation_text") or "").strip()

    required_confirmation = "RECORD LOGIN INTEGRITY ACTION"

    allowed_action_types = {
        "MONITOR_ONLY",
        "CONTACT_ORG_ADMIN",
        "REQUIRE_SEPARATE_LOGINS",
        "REVIEW_WITH_CUSTOMER",
        "MARK_FALSE_POSITIVE",
        "ESCALATE_TO_SUPER_GLOBAL_ADMIN",
    }

    if confirmation_text != required_confirmation:
        return jsonify({
            "error": "Confirmation text is required before recording a login integrity action",
            "required_confirmation_text": required_confirmation,
            "received_confirmation_text": confirmation_text,
            "rule": "Global Admin decides and records the course of action. AI does not automatically penalise users or organisations.",
        }), 400

    if action_type not in allowed_action_types:
        return jsonify({
            "error": "Invalid action_type",
            "allowed_action_types": sorted(allowed_action_types),
        }), 400

    conn = get_conn()
    ensure_login_integrity_action_tables(conn)

    event = conn.execute(
        """
        SELECT *
        FROM login_integrity_events
        WHERE login_integrity_event_id = ?
        """,
        (login_integrity_event_id,)
    ).fetchone()

    if not event:
        conn.close()
        return jsonify({"error": "Login integrity report not found"}), 404

    ts = now_iso()
    action_id = make_id("lia")

    conn.execute(
        """
        INSERT INTO login_integrity_admin_actions (
            login_integrity_action_id,
            login_integrity_event_id,
            organisation_id,
            user_id,
            action_type,
            action_status,
            assigned_to_display_name,
            action_notes,
            due_at,
            completed_at,
            created_by_display_name,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action_id,
            login_integrity_event_id,
            event["organisation_id"],
            event["user_id"],
            action_type,
            "OPEN",
            assigned_to_display_name,
            action_notes,
            due_at,
            None,
            created_by_display_name,
            ts,
            ts,
        )
    )

    conn.execute(
        """
        UPDATE login_integrity_events
        SET review_status = ?,
            summary = summary || ?,
            updated_at = ?
        WHERE login_integrity_event_id = ?
        """,
        (
            "ACTION_REQUIRED" if action_type not in ("MONITOR_ONLY", "MARK_FALSE_POSITIVE") else "MONITORING",
            f" Global Admin action recorded: {action_type}.",
            ts,
            login_integrity_event_id,
        )
    )

    audit_event(
        conn,
        entity_type="LoginIntegrityEvent",
        entity_id=login_integrity_event_id,
        action="RECORD_ADMIN_ACTION",
        summary=f"Global Admin recorded login integrity action: {action_type}.",
        organisation_id=event["organisation_id"],
    )

    conn.commit()

    action = conn.execute(
        """
        SELECT *
        FROM login_integrity_admin_actions
        WHERE login_integrity_action_id = ?
        """,
        (action_id,)
    ).fetchone()

    updated_event = conn.execute(
        """
        SELECT *
        FROM login_integrity_events
        WHERE login_integrity_event_id = ?
        """,
        (login_integrity_event_id,)
    ).fetchone()

    conn.close()

    return jsonify({
        "login_integrity_action": dict(action),
        "login_integrity_report": dict(updated_event),
        "rule": "AI reports are advisory only. Global Admin records and decides the course of action.",
    }), 201


@app.get("/global-admin/login-integrity-reports/<login_integrity_event_id>/actions")
def list_login_integrity_admin_actions(login_integrity_event_id):
    conn = get_conn()
    ensure_login_integrity_action_tables(conn)

    event = conn.execute(
        """
        SELECT *
        FROM login_integrity_events
        WHERE login_integrity_event_id = ?
        """,
        (login_integrity_event_id,)
    ).fetchone()

    if not event:
        conn.close()
        return jsonify({"error": "Login integrity report not found"}), 404

    rows = conn.execute(
        """
        SELECT *
        FROM login_integrity_admin_actions
        WHERE login_integrity_event_id = ?
        ORDER BY created_at ASC
        """,
        (login_integrity_event_id,)
    ).fetchall()

    conn.close()

    return jsonify({
        "login_integrity_event_id": login_integrity_event_id,
        "action_count": len(rows),
        "items": [dict(row) for row in rows],
    }), 200

# === LOGIN INTEGRITY ADMIN ACTIONS V0.1 END ===

# === LOGIN INTEGRITY ACTION COMPLETION V0.1 START ===

@app.post("/global-admin/login-integrity-actions/<login_integrity_action_id>/complete")
def complete_login_integrity_admin_action(login_integrity_action_id):
    body = request.get_json(silent=True) or {}

    completed_by_display_name = (body.get("completed_by_display_name") or "Global Admin").strip()
    completion_notes = (body.get("completion_notes") or "").strip() or None
    confirmation_text = (body.get("confirmation_text") or "").strip()

    required_confirmation = "COMPLETE LOGIN INTEGRITY ACTION"

    if confirmation_text != required_confirmation:
        return jsonify({
            "error": "Confirmation text is required before completing a login integrity action",
            "required_confirmation_text": required_confirmation,
            "received_confirmation_text": confirmation_text,
            "rule": "Global Admin must deliberately complete recorded login integrity actions.",
        }), 400

    conn = get_conn()
    ensure_login_integrity_action_tables(conn)

    action = conn.execute(
        """
        SELECT *
        FROM login_integrity_admin_actions
        WHERE login_integrity_action_id = ?
        """,
        (login_integrity_action_id,)
    ).fetchone()

    if not action:
        conn.close()
        return jsonify({"error": "Login integrity action not found"}), 404

    ts = now_iso()

    conn.execute(
        """
        UPDATE login_integrity_admin_actions
        SET action_status = ?,
            completed_at = ?,
            action_notes = COALESCE(action_notes, '') || ?,
            updated_at = ?
        WHERE login_integrity_action_id = ?
        """,
        (
            "COMPLETED",
            ts,
            f" Completion by {completed_by_display_name}: {completion_notes or 'No notes supplied.'}",
            ts,
            login_integrity_action_id,
        )
    )

    open_actions = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM login_integrity_admin_actions
        WHERE login_integrity_event_id = ?
          AND action_status != 'COMPLETED'
        """,
        (action["login_integrity_event_id"],)
    ).fetchone()["c"]

    if open_actions == 0:
        conn.execute(
            """
            UPDATE login_integrity_events
            SET review_status = ?,
                summary = summary || ?,
                updated_at = ?
            WHERE login_integrity_event_id = ?
            """,
            (
                "RESOLVED",
                f" All recorded Global Admin actions completed by {completed_by_display_name}.",
                ts,
                action["login_integrity_event_id"],
            )
        )

    audit_event(
        conn,
        entity_type="LoginIntegrityAction",
        entity_id=login_integrity_action_id,
        action="COMPLETE",
        summary=f"Login integrity admin action completed by {completed_by_display_name}.",
        organisation_id=action["organisation_id"],
    )

    conn.commit()

    updated_action = conn.execute(
        """
        SELECT *
        FROM login_integrity_admin_actions
        WHERE login_integrity_action_id = ?
        """,
        (login_integrity_action_id,)
    ).fetchone()

    updated_event = conn.execute(
        """
        SELECT *
        FROM login_integrity_events
        WHERE login_integrity_event_id = ?
        """,
        (action["login_integrity_event_id"],)
    ).fetchone()

    conn.close()

    return jsonify({
        "login_integrity_action": dict(updated_action),
        "login_integrity_report": dict(updated_event),
        "open_action_count_after_completion": open_actions,
        "rule": "Completing the final open action resolves the login integrity report.",
    }), 200

# === LOGIN INTEGRITY ACTION COMPLETION V0.1 END ===

# === LOGIN INTEGRITY ACTION QUEUE V0.1 START ===

@app.get("/global-admin/login-integrity-actions")
def list_login_integrity_action_queue():
    action_status = request.args.get("action_status")
    organisation_id = request.args.get("organisation_id")
    user_id = request.args.get("user_id")

    conn = get_conn()
    ensure_login_integrity_action_tables(conn)

    sql = """
        SELECT
            a.*,
            e.event_type,
            e.risk_level,
            e.risk_score,
            e.review_status,
            e.ai_report_summary,
            u.display_name,
            u.email,
            u.role,
            o.name AS organisation_name
        FROM login_integrity_admin_actions a
        LEFT JOIN login_integrity_events e
            ON e.login_integrity_event_id = a.login_integrity_event_id
        LEFT JOIN user_accounts u
            ON u.user_id = a.user_id
        LEFT JOIN organisations o
            ON o.organisation_id = a.organisation_id
        WHERE 1 = 1
    """
    params = []

    if action_status:
        sql += " AND a.action_status = ?"
        params.append(action_status)

    if organisation_id:
        sql += " AND a.organisation_id = ?"
        params.append(organisation_id)

    if user_id:
        sql += " AND a.user_id = ?"
        params.append(user_id)

    sql += """
        ORDER BY
            CASE a.action_status
                WHEN 'OPEN' THEN 0
                WHEN 'COMPLETED' THEN 1
                ELSE 2
            END,
            a.created_at DESC
    """

    rows = conn.execute(sql, params).fetchall()

    status_counts = {}
    type_counts = {}

    for row in rows:
        status_counts[row["action_status"]] = status_counts.get(row["action_status"], 0) + 1
        type_counts[row["action_type"]] = type_counts.get(row["action_type"], 0) + 1

    conn.close()

    return jsonify({
        "queue_type": "GLOBAL_ADMIN_LOGIN_INTEGRITY_ACTION_QUEUE",
        "count": len(rows),
        "action_status_counts": status_counts,
        "action_type_counts": type_counts,
        "items": [dict(row) for row in rows],
        "rule": "Global Admin owns the course of action. AI reports are advisory only.",
    }), 200

# === LOGIN INTEGRITY ACTION QUEUE V0.1 END ===

# === LOGIN INTEGRITY ACTION QUEUE SUMMARY V0.2 START ===

@app.get("/global-admin/login-integrity-actions-summary")
def get_login_integrity_action_summary():
    conn = get_conn()
    ensure_login_integrity_action_tables(conn)

    rows = conn.execute(
        """
        SELECT
            a.action_status,
            a.action_type,
            e.risk_level,
            COUNT(*) AS count
        FROM login_integrity_admin_actions a
        LEFT JOIN login_integrity_events e
            ON e.login_integrity_event_id = a.login_integrity_event_id
        GROUP BY a.action_status, a.action_type, e.risk_level
        ORDER BY a.action_status ASC, e.risk_level ASC, a.action_type ASC
        """
    ).fetchall()

    open_rows = conn.execute(
        """
        SELECT
            a.*,
            e.event_type,
            e.risk_level,
            e.risk_score,
            e.ai_report_summary,
            u.display_name,
            u.email,
            u.role,
            o.name AS organisation_name
        FROM login_integrity_admin_actions a
        LEFT JOIN login_integrity_events e
            ON e.login_integrity_event_id = a.login_integrity_event_id
        LEFT JOIN user_accounts u
            ON u.user_id = a.user_id
        LEFT JOIN organisations o
            ON o.organisation_id = a.organisation_id
        WHERE a.action_status = 'OPEN'
        ORDER BY
            CASE e.risk_level
                WHEN 'CRITICAL' THEN 0
                WHEN 'HIGH' THEN 1
                WHEN 'MEDIUM' THEN 2
                WHEN 'LOW' THEN 3
                ELSE 4
            END,
            a.created_at ASC
        LIMIT 20
        """
    ).fetchall()

    total_open = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM login_integrity_admin_actions
        WHERE action_status = 'OPEN'
        """
    ).fetchone()["c"]

    total_completed = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM login_integrity_admin_actions
        WHERE action_status = 'COMPLETED'
        """
    ).fetchone()["c"]

    conn.close()

    return jsonify({
        "summary_type": "GLOBAL_ADMIN_LOGIN_INTEGRITY_ACTION_SUMMARY",
        "open_action_count": total_open,
        "completed_action_count": total_completed,
        "grouped_counts": [dict(row) for row in rows],
        "top_open_actions": [dict(row) for row in open_rows],
        "rule": "This summary is for Global Admin dashboard badges and triage. AI reports remain advisory only.",
    }), 200

# === LOGIN INTEGRITY ACTION QUEUE SUMMARY V0.2 END ===

# === SESSION HEARTBEAT V0.1 START ===

@app.post("/sessions/<session_id>/heartbeat")
def heartbeat_user_session(session_id):
    conn = get_conn()
    ensure_login_integrity_tables(conn)
    ensure_user_access_tables(conn)

    session = conn.execute(
        "SELECT * FROM user_sessions WHERE session_id = ?",
        (session_id,)
    ).fetchone()

    if not session:
        conn.close()
        return jsonify({"error": "Session not found"}), 404

    user = get_user_account(conn, session["user_id"])

    if not user:
        conn.close()
        return jsonify({"error": "User not found for session"}), 404

    policy = build_user_access_policy(conn, user)

    if session["session_status"] != "ACTIVE":
        conn.close()
        return jsonify({
            "error": "Session is not active",
            "session": dict(session),
            "access_policy": policy,
        }), 403

    if not policy["can_use_platform"]:
        ts = now_iso()

        conn.execute(
            """
            UPDATE user_sessions
            SET session_status = ?,
                logout_at = ?,
                ended_reason = ?,
                updated_at = ?
            WHERE session_id = ?
            """,
            (
                "ENDED",
                ts,
                "ACCESS_POLICY_NO_LONGER_ALLOWS_PLATFORM_USE",
                ts,
                session_id,
            )
        )

        conn.commit()

        updated = conn.execute(
            "SELECT * FROM user_sessions WHERE session_id = ?",
            (session_id,)
        ).fetchone()

        conn.close()

        return jsonify({
            "error": "Session ended because user access is no longer allowed",
            "session": dict(updated),
            "access_policy": policy,
        }), 403

    ts = now_iso()

    conn.execute(
        """
        UPDATE user_sessions
        SET last_seen_at = ?,
            updated_at = ?
        WHERE session_id = ?
        """,
        (ts, ts, session_id)
    )

    conn.commit()

    updated = conn.execute(
        "SELECT * FROM user_sessions WHERE session_id = ?",
        (session_id,)
    ).fetchone()

    conn.close()

    return jsonify({
        "session": dict(updated),
        "access_policy": policy,
        "message": "Session heartbeat accepted.",
        "rule": "Active sessions update last_seen_at so Login Integrity Guard can track live device activity.",
    }), 200

# === SESSION HEARTBEAT V0.1 END ===

# === SESSION EXPIRY SWEEP V0.1 START ===

@app.post("/global-admin/session-expiry-sweep")
def sweep_expired_sessions():
    from datetime import datetime, timedelta

    body = request.get_json(silent=True) or {}

    inactive_minutes = body.get("inactive_minutes", 480)
    swept_by_display_name = (body.get("swept_by_display_name") or "Global Admin").strip()
    confirmation_text = (body.get("confirmation_text") or "").strip()

    required_confirmation = "SWEEP EXPIRED SESSIONS"

    if confirmation_text != required_confirmation:
        return jsonify({
            "error": "Confirmation text is required before sweeping expired sessions",
            "required_confirmation_text": required_confirmation,
            "received_confirmation_text": confirmation_text,
            "rule": "Session expiry sweep changes session state and must be deliberate.",
        }), 400

    try:
        inactive_minutes = int(inactive_minutes)
    except Exception:
        return jsonify({"error": "inactive_minutes must be an integer"}), 400

    if inactive_minutes < 1:
        return jsonify({"error": "inactive_minutes must be at least 1"}), 400

    conn = get_conn()
    ensure_login_integrity_tables(conn)

    cutoff = (datetime.fromisoformat(now_iso()) - timedelta(minutes=inactive_minutes)).isoformat()
    ts = now_iso()

    expired = conn.execute(
        """
        SELECT *
        FROM user_sessions
        WHERE session_status = 'ACTIVE'
          AND last_seen_at < ?
        ORDER BY last_seen_at ASC
        """,
        (cutoff,)
    ).fetchall()

    for session in expired:
        conn.execute(
            """
            UPDATE user_sessions
            SET session_status = ?,
                logout_at = ?,
                ended_reason = ?,
                updated_at = ?
            WHERE session_id = ?
            """,
            (
                "ENDED",
                ts,
                "SESSION_EXPIRED_INACTIVITY_SWEEP",
                ts,
                session["session_id"],
            )
        )

    audit_event(
        conn,
        entity_type="UserSession",
        entity_id="session_expiry_sweep",
        action="SWEEP_EXPIRED_SESSIONS",
        summary=f"Session expiry sweep ended {len(expired)} inactive sessions after {inactive_minutes} minutes.",
        organisation_id=None,
    )

    conn.commit()

    ended_sessions = conn.execute(
        """
        SELECT *
        FROM user_sessions
        WHERE ended_reason = 'SESSION_EXPIRED_INACTIVITY_SWEEP'
          AND logout_at = ?
        ORDER BY logout_at DESC
        """,
        (ts,)
    ).fetchall()

    conn.close()

    return jsonify({
        "sweep_type": "SESSION_EXPIRY_SWEEP",
        "inactive_minutes": inactive_minutes,
        "cutoff_last_seen_before": cutoff,
        "swept_by_display_name": swept_by_display_name,
        "expired_session_count": len(ended_sessions),
        "expired_sessions": [dict(row) for row in ended_sessions],
        "rule": "Inactive active sessions are ended so Login Integrity Guard has an accurate live-session picture.",
    }), 200

# === SESSION EXPIRY SWEEP V0.1 END ===

# === TEMPORARY USER EXPIRY SWEEP V0.1 START ===

@app.post("/global-admin/temporary-user-expiry-sweep")
def sweep_expired_temporary_users():
    body = request.get_json(silent=True) or {}

    swept_by_display_name = (body.get("swept_by_display_name") or "Global Admin").strip()
    confirmation_text = (body.get("confirmation_text") or "").strip()
    as_of = body.get("as_of") or now_iso()

    required_confirmation = "SWEEP EXPIRED TEMPORARY USERS"

    if confirmation_text != required_confirmation:
        return jsonify({
            "error": "Confirmation text is required before sweeping expired temporary users",
            "required_confirmation_text": required_confirmation,
            "received_confirmation_text": confirmation_text,
            "rule": "Temporary user expiry changes access state and must be deliberate.",
        }), 400

    conn = get_conn()
    ensure_subscription_guard_tables(conn)
    ensure_user_access_tables(conn)

    expired_access_rows = conn.execute(
        """
        SELECT *
        FROM temporary_user_access
        WHERE access_status = 'ACTIVE'
          AND access_ends_at < ?
        ORDER BY access_ends_at ASC
        """,
        (as_of,)
    ).fetchall()

    expired_access_ids = [row["temporary_user_access_id"] for row in expired_access_rows]
    ts = now_iso()

    for row in expired_access_rows:
        conn.execute(
            """
            UPDATE temporary_user_access
            SET access_status = ?,
                updated_at = ?
            WHERE temporary_user_access_id = ?
            """,
            (
                "EXPIRED",
                ts,
                row["temporary_user_access_id"],
            )
        )

        conn.execute(
            """
            UPDATE user_accounts
            SET access_status = ?,
                updated_at = ?
            WHERE temporary_user_access_id = ?
              AND role = 'TEMPORARY_USER'
              AND access_status = 'ACTIVE'
            """,
            (
                "EXPIRED",
                ts,
                row["temporary_user_access_id"],
            )
        )

        record_user_access_event(
            conn,
            user_id=row["temporary_user_access_id"],
            organisation_id=row["organisation_id"],
            action="TEMPORARY_ACCESS_EXPIRED",
            summary="Temporary user access expired and linked temporary users were marked expired.",
            changed_by_display_name=swept_by_display_name,
        )

    audit_event(
        conn,
        entity_type="TemporaryUserAccess",
        entity_id="temporary_user_expiry_sweep",
        action="SWEEP_EXPIRED_TEMPORARY_USERS",
        summary=f"Temporary user expiry sweep expired {len(expired_access_rows)} temporary access records.",
        organisation_id=None,
    )

    conn.commit()

    linked_users = []
    if expired_access_ids:
        placeholders = ",".join(["?"] * len(expired_access_ids))
        linked_users = conn.execute(
            f"""
            SELECT *
            FROM user_accounts
            WHERE temporary_user_access_id IN ({placeholders})
            ORDER BY display_name ASC
            """,
            expired_access_ids,
        ).fetchall()

    conn.close()

    return jsonify({
        "sweep_type": "TEMPORARY_USER_EXPIRY_SWEEP",
        "as_of": as_of,
        "swept_by_display_name": swept_by_display_name,
        "expired_temporary_access_count": len(expired_access_rows),
        "expired_temporary_access_ids": expired_access_ids,
        "linked_temporary_users": [dict(row) for row in linked_users],
        "rule": "Expired temporary access records and linked temporary users are marked EXPIRED.",
    }), 200

# === TEMPORARY USER EXPIRY SWEEP V0.1 END ===

# === ACCESS OPERATIONS DASHBOARD V0.1 START ===

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

# === ACCESS OPERATIONS DASHBOARD V0.1 END ===

# === ACCESS OPERATIONS HEALTH CHECK V0.1 START ===

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

# === ACCESS OPERATIONS HEALTH CHECK V0.1 END ===

# === GLOBAL ADMIN ORGANISATIONS LIST V0.1 START ===

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

# === GLOBAL ADMIN ORGANISATIONS LIST V0.1 END ===

# === SYSTEM CONTROL PANEL V0.1 START ===

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

# === SYSTEM CONTROL PANEL V0.1 END ===

# === ACCESS OPERATIONS METRICS SNAPSHOT V0.1 START ===

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


def collect_access_operations_metrics(conn):
    import json

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


@app.post("/global-admin/access-operations-metrics-snapshot")
def create_access_operations_metrics_snapshot():
    import json

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

# === ACCESS OPERATIONS METRICS SNAPSHOT V0.1 END ===

from modules.password_auth import register_password_auth_routes
from modules.webauthn_auth import register_webauthn_routes
from modules.tcr import register_tcr_routes
from modules.resource_loss import register_resource_loss_routes
from modules.offline_batch import register_offline_batch_routes
from modules.referral import register_referral_routes
from modules.org_branding import register_org_branding_routes
from modules.help_text import register_help_text_routes
from modules.web_push import register_web_push_routes
from modules.notifications import register_notification_routes, notify_user
from modules.resource_sample_photos import register_resource_sample_photo_routes
from modules.ai_counting import register_ai_counting_routes

register_auth_middleware(app)
register_auth_routes(app)
register_password_auth_routes(app)
register_webauthn_routes(app)
register_tcr_routes(
    app,
    post_transaction_to_ledger=post_transaction_to_ledger,
    generate_transaction_reference=generate_transaction_reference,
    ensure_transaction_numbering_tables=ensure_transaction_numbering_tables,
    ensure_transaction_partner_columns=ensure_transaction_partner_columns,
    ensure_transaction_user_attribution_columns=ensure_transaction_user_attribution_columns,
)
register_resource_loss_routes(
    app,
    post_transaction_to_ledger=post_transaction_to_ledger,
    generate_transaction_reference=generate_transaction_reference,
    ensure_transaction_numbering_tables=ensure_transaction_numbering_tables,
    ensure_transaction_partner_columns=ensure_transaction_partner_columns,
    ensure_transaction_user_attribution_columns=ensure_transaction_user_attribution_columns,
)
register_offline_batch_routes(
    app,
    post_transaction_to_ledger=post_transaction_to_ledger,
    generate_transaction_reference=generate_transaction_reference,
    ensure_transaction_numbering_tables=ensure_transaction_numbering_tables,
    ensure_transaction_partner_columns=ensure_transaction_partner_columns,
    ensure_partner_address_tables=ensure_partner_address_tables,
    ensure_transaction_user_attribution_columns=ensure_transaction_user_attribution_columns,
    create_pending_entry=create_pending_entry,
)
register_referral_routes(app)
register_org_branding_routes(app)
register_help_text_routes(app)
register_web_push_routes(app)
register_notification_routes(app)
register_resource_sample_photo_routes(app)
register_ai_counting_routes(app)
register_admin_handover_routes(app)
register_feature_flag_routes(app)
register_stock_position_routes(app)
register_stocktake_routes(
    app,
    post_transaction_to_ledger=post_transaction_to_ledger,
    generate_transaction_reference=generate_transaction_reference,
    ensure_transaction_numbering_tables=ensure_transaction_numbering_tables,
    ensure_transaction_partner_columns=ensure_transaction_partner_columns,
    ensure_transaction_user_attribution_columns=ensure_transaction_user_attribution_columns,
)
register_error_logging_routes(app)
register_system_routes(app)
register_subscription_routes(app)
register_transaction_reporting_routes(
    app,
    get_conn=get_conn,
    ensure_transaction_partner_columns=ensure_transaction_partner_columns,
    ensure_partner_address_tables=ensure_partner_address_tables,
    ensure_transaction_user_attribution_columns=ensure_transaction_user_attribution_columns,
    ensure_transaction_numbering_tables=ensure_transaction_numbering_tables,
)
register_partner_routes(
    app,
    record_shared_transaction_event=record_shared_transaction_event,
)
register_depot_routes(app)
register_resource_routes(
    app,
    create_pending_entry=create_pending_entry,
    generate_transaction_reference=generate_transaction_reference,
    ensure_transaction_numbering_tables=ensure_transaction_numbering_tables,
)
register_transaction_routes(
    app,
    create_pending_entry=create_pending_entry,
    ensure_user_access_tables=ensure_user_access_tables,
    get_user_account=get_user_account,
    build_user_access_policy=build_user_access_policy,
)
register_shared_transaction_routes(app)































































# === USER CAP SELF-SERVE V0.1 START ===

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

    sub = get_or_create_subscription(conn, organisation_id)
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

# === USER CAP SELF-SERVE V0.1 END ===


@app.post("/organisations/<organisation_id>/unsubscribe")
def unsubscribe_organisation(organisation_id):
    from datetime import datetime, timedelta

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



# === UNSUBSCRIBED ORG ACCESS GUARD V0.1 START ===

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

# === UNSUBSCRIBED ORG ACCESS GUARD V0.1 END ===


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=8000, debug=debug)
