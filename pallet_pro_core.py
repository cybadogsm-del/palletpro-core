import os
import time
import threading
from collections import defaultdict

from flask import Flask, request, jsonify, g
from audit import audit_event
from db import DB, get_conn, make_id, now_iso
from modules.subscription_access import (
    ensure_org_user_cap_column,
    ensure_subscription_guard_tables,
    register_subscription_routes,
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
    ensure_ledger_balance_operational_unit_columns,
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
from modules.org_subscription import register_org_subscription_routes
from modules.operational_units import ensure_operational_unit_tables, register_operational_unit_routes

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
    "http://localhost:3001",
    "http://127.0.0.1:3001",
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
        created_at TEXT NOT NULL,
        operational_unit_id TEXT,
        operational_unit_kind_snapshot TEXT,
        operational_unit_number_snapshot TEXT,
        operational_unit_display_snapshot TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS balance_projection (
        balance_projection_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        depot_id TEXT NOT NULL,
        operational_unit_id TEXT NOT NULL DEFAULT '__NO_OPERATIONAL_UNIT__',
        resource_id TEXT NOT NULL,
        current_quantity INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (organisation_id, depot_id, operational_unit_id, resource_id)
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

    def _safe_add_column(table, column, col_type="TEXT"):
        """Add a column if missing. Ignores duplicate-column errors from worker races."""
        try:
            cols = {row["name"] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise

    _safe_add_column("resources", "category_id")
    _safe_add_column("resources", "brand_id")

    _safe_add_column("resource_requests", "category_id")
    _safe_add_column("resource_requests", "brand_id")
    _safe_add_column("resource_requests", "rejection_reason_code")
    _safe_add_column("resource_requests", "rejection_reason_text")

    _safe_add_column("brand_requests", "rejection_reason_code")
    _safe_add_column("brand_requests", "rejection_reason_text")

    _safe_add_column("category_requests", "rejection_reason_code")
    _safe_add_column("category_requests", "rejection_reason_text")

    _safe_add_column("pending_approval_entries", "rejection_reason_code")
    _safe_add_column("pending_approval_entries", "rejection_reason_text")

    _safe_add_column("transactions", "unresolved_entity_note")
    _safe_add_column("transactions", "unresolved_entity_type")
    _safe_add_column("transactions", "operational_unit_id")
    _safe_add_column("transactions", "operational_unit_kind_snapshot")
    _safe_add_column("transactions", "operational_unit_number_snapshot")
    _safe_add_column("transactions", "operational_unit_display_snapshot")
    _safe_add_column("transactions", "operational_unit_missing", "INTEGER DEFAULT 0")

    ensure_subscription_guard_tables(conn)
    ensure_api_key_tables(conn)
    ensure_error_logging_tables(conn)
    ensure_admin_handover_tables(conn)
    ensure_stocktake_tables(conn)
    ensure_org_user_cap_column(conn)
    ensure_operational_unit_tables(conn)
    ensure_ledger_balance_operational_unit_columns(conn)

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
from modules.route_intelligence import register_route_intelligence_routes
from modules.operational_insights import register_operational_insight_routes

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
register_route_intelligence_routes(app)
register_operational_insight_routes(app)
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
register_org_subscription_routes(app)
register_operational_unit_routes(app)





























































if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=8000, debug=debug)
