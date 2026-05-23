import os
import time
import threading
from collections import defaultdict

from flask import Flask, request, jsonify, g
from audit import audit_event
from db import DB, get_conn, make_id, now_iso
from modules.subscription_access import (
    count_active_permanent_users,
    ensure_org_user_cap_column,
    ensure_subscription_guard_tables,
    get_or_create_subscription,
    get_org_access_status_payload,
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
from modules.organisations import register_organisation_routes
from modules.users import (
    ensure_user_access_tables,
    get_user_account,
    build_user_access_policy,
    record_user_access_event,
    register_user_routes,
)
from modules.sessions import register_session_routes
from modules.billing import register_billing_routes
from modules.access_operations import register_access_operations_routes

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
register_organisation_routes(app)
register_user_routes(app)
register_session_routes(app, is_rate_limited=_is_rate_limited)
register_billing_routes(app)
register_access_operations_routes(app)































































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
