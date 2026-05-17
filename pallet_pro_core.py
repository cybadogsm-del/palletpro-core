import os

from flask import Flask, request, jsonify
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
from modules.feature_flags import register_feature_flag_routes
from modules.stock_position import register_stock_position_routes
from modules.system_routes import register_system_routes
from modules.transaction_reporting import register_transaction_reporting_routes

app = Flask(__name__)


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


def update_pending_entries_ready_for_opening_balance(conn, organisation_id, depot_id):
    rows = conn.execute(
        """
        SELECT pending_entry_id
        FROM pending_approval_entries
        WHERE organisation_id = ?
          AND related_entity_id = ?
          AND reason_code = 'NIL_OPENING_BALANCE'
          AND status = 'AWAITING_FIX'
        """,
        (organisation_id, depot_id)
    ).fetchall()

    for row in rows:
        conn.execute(
            """
            UPDATE pending_approval_entries
            SET status = ?, can_approve_now = ?, updated_at = ?
            WHERE pending_entry_id = ?
            """,
            ("READY_TO_APPROVE", 1, now_iso(), row["pending_entry_id"])
        )

        audit_event(
            conn,
            entity_type="PendingApprovalEntry",
            entity_id=row["pending_entry_id"],
            action="READY_TO_APPROVE",
            summary="Pending entry is now ready to approve after opening balance was set",
            organisation_id=organisation_id
        )


def post_transaction_to_ledger(conn, txn):
    quantity_delta = txn["quantity"] if txn["direction"] == "IN" else -txn["quantity"]
    ledger_entry_id = make_id("led")

    conn.execute(
        """
        INSERT INTO ledger_entries (
            ledger_entry_id,
            transaction_id,
            organisation_id,
            depot_id,
            resource_id,
            quantity_delta,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ledger_entry_id,
            txn["transaction_id"],
            txn["organisation_id"],
            txn["depot_id"],
            txn["resource_id"],
            quantity_delta,
            now_iso()
        )
    )

    existing_balance = conn.execute(
        """
        SELECT * FROM balance_projection
        WHERE organisation_id = ? AND depot_id = ? AND resource_id = ?
        """,
        (txn["organisation_id"], txn["depot_id"], txn["resource_id"])
    ).fetchone()

    if existing_balance:
        new_qty = existing_balance["current_quantity"] + quantity_delta
        conn.execute(
            """
            UPDATE balance_projection
            SET current_quantity = ?, updated_at = ?
            WHERE balance_projection_id = ?
            """,
            (new_qty, now_iso(), existing_balance["balance_projection_id"])
        )
    else:
        conn.execute(
            """
            INSERT INTO balance_projection (
                balance_projection_id,
                organisation_id,
                depot_id,
                resource_id,
                current_quantity,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("bal"),
                txn["organisation_id"],
                txn["depot_id"],
                txn["resource_id"],
                quantity_delta,
                now_iso()
            )
        )

    conn.execute(
        """
        UPDATE transactions
        SET status = ?, posted_at = ?, approval_reason_code = ?, approval_reason_text = ?
        WHERE transaction_id = ?
        """,
        ("POSTED", now_iso(), None, None, txn["transaction_id"])
    )

    audit_event(
        conn,
        entity_type="Transaction",
        entity_id=txn["transaction_id"],
        action="POST",
        summary=f"Posted transaction with delta {quantity_delta}",
        organisation_id=txn["organisation_id"]
    )



def ensure_resource_cleanup_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(resources)").fetchall()}
    if "merged_into_resource_id" not in cols:
        conn.execute("ALTER TABLE resources ADD COLUMN merged_into_resource_id TEXT")
    if "inactive_reason_code" not in cols:
        conn.execute("ALTER TABLE resources ADD COLUMN inactive_reason_code TEXT")
    if "inactive_reason_text" not in cols:
        conn.execute("ALTER TABLE resources ADD COLUMN inactive_reason_text TEXT")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE resources ADD COLUMN updated_at TEXT")



def ensure_partner_connection_tables(conn):
    changed = False

    partner_cols = {row["name"] for row in conn.execute("PRAGMA table_info(partners)").fetchall()}
    if "linked_org_id" not in partner_cols:
        conn.execute("ALTER TABLE partners ADD COLUMN linked_org_id TEXT")
        changed = True
    if "connection_status" not in partner_cols:
        conn.execute("ALTER TABLE partners ADD COLUMN connection_status TEXT")
        changed = True
    if "updated_at" not in partner_cols:
        conn.execute("ALTER TABLE partners ADD COLUMN updated_at TEXT")
        changed = True

    conn.execute("""
    CREATE TABLE IF NOT EXISTS org_connection_requests (
        connection_request_id TEXT PRIMARY KEY,
        requesting_org_id TEXT NOT NULL,
        requesting_partner_id TEXT NOT NULL,
        target_org_id TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    if changed:
        conn.commit()



def ensure_shared_transaction_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS shared_transactions (
        shared_transaction_id TEXT PRIMARY KEY,
        origin_org_id TEXT NOT NULL,
        counterparty_org_id TEXT NOT NULL,
        origin_partner_id TEXT NOT NULL,
        counterparty_partner_id TEXT,
        origin_resource_id TEXT NOT NULL,
        resource_name TEXT NOT NULL,
        unit_type TEXT,
        quantity INTEGER NOT NULL,
        movement_type TEXT NOT NULL,
        reference_number TEXT,
        shared_status TEXT NOT NULL,
        created_by_display_name TEXT,
        confirmed_by_display_name TEXT,
        disputed_by_display_name TEXT,
        confirmed_at TEXT,
        disputed_at TEXT,
        dispute_reason_code TEXT,
        dispute_reason_text TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS shared_transaction_events (
        shared_transaction_event_id TEXT PRIMARY KEY,
        shared_transaction_id TEXT NOT NULL,
        organisation_id TEXT NOT NULL,
        actor_org_role TEXT NOT NULL,
        action TEXT NOT NULL,
        previous_status TEXT,
        new_status TEXT,
        summary TEXT NOT NULL,
        created_by_display_name TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(shared_transactions)").fetchall()}
    if "proposed_quantity" not in cols:
        conn.execute("ALTER TABLE shared_transactions ADD COLUMN proposed_quantity INTEGER")
    if "proposed_reference_number" not in cols:
        conn.execute("ALTER TABLE shared_transactions ADD COLUMN proposed_reference_number TEXT")
    if "correction_reason_text" not in cols:
        conn.execute("ALTER TABLE shared_transactions ADD COLUMN correction_reason_text TEXT")
    if "correction_proposed_by_display_name" not in cols:
        conn.execute("ALTER TABLE shared_transactions ADD COLUMN correction_proposed_by_display_name TEXT")
    if "correction_proposed_at" not in cols:
        conn.execute("ALTER TABLE shared_transactions ADD COLUMN correction_proposed_at TEXT")
    if "resolution_code" not in cols:
        conn.execute("ALTER TABLE shared_transactions ADD COLUMN resolution_code TEXT")
    if "resolution_notes" not in cols:
        conn.execute("ALTER TABLE shared_transactions ADD COLUMN resolution_notes TEXT")
    if "resolved_by_display_name" not in cols:
        conn.execute("ALTER TABLE shared_transactions ADD COLUMN resolved_by_display_name TEXT")
    if "resolved_at" not in cols:
        conn.execute("ALTER TABLE shared_transactions ADD COLUMN resolved_at TEXT")

    conn.commit()




def ensure_partner_address_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS partner_addresses (
        partner_address_id TEXT PRIMARY KEY,
        partner_id TEXT NOT NULL,
        organisation_id TEXT NOT NULL,
        label TEXT NOT NULL,
        category TEXT NOT NULL,
        custom_category_label TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        is_primary INTEGER NOT NULL DEFAULT 0,
        is_default_dispatch_site INTEGER NOT NULL DEFAULT 0,
        is_default_receiving_site INTEGER NOT NULL DEFAULT 0,
        address_line_1 TEXT,
        address_line_2 TEXT,
        suburb TEXT,
        state TEXT,
        postcode TEXT,
        country TEXT,
        gate_number TEXT,
        door_number TEXT,
        entry_instructions TEXT,
        truck_access_notes TEXT,
        latitude REAL,
        longitude REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS location_update_requests (
        location_update_request_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        current_latitude REAL,
        current_longitude REAL,
        proposed_latitude REAL NOT NULL,
        proposed_longitude REAL NOT NULL,
        reason_text TEXT,
        status TEXT NOT NULL,
        submitted_by_display_name TEXT NOT NULL,
        reviewed_by_display_name TEXT,
        review_notes TEXT,
        reviewed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    conn.commit()


def ensure_qr_token_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS qr_handoff_tokens (
        qr_token_id TEXT PRIMARY KEY,
        shared_transaction_id TEXT NOT NULL,
        purpose TEXT NOT NULL,
        issuing_org_id TEXT NOT NULL,
        target_org_id TEXT NOT NULL,
        actor_org_role TEXT NOT NULL,
        token_status TEXT NOT NULL,
        created_by_display_name TEXT,
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        scanned_at TEXT,
        scanned_by_display_name TEXT
    )
    """)

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(qr_handoff_tokens)").fetchall()}
    if "consumed_at" not in cols:
        conn.execute("ALTER TABLE qr_handoff_tokens ADD COLUMN consumed_at TEXT")
    if "consumed_by_display_name" not in cols:
        conn.execute("ALTER TABLE qr_handoff_tokens ADD COLUMN consumed_by_display_name TEXT")
    if "consumed_action" not in cols:
        conn.execute("ALTER TABLE qr_handoff_tokens ADD COLUMN consumed_action TEXT")

    conn.commit()




def ensure_shared_transaction_partner_address_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS shared_transaction_partner_addresses (
        shared_transaction_id TEXT PRIMARY KEY,
        origin_partner_address_id TEXT,
        counterparty_partner_address_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

def validate_qr_handoff_token_for_action(conn, qr_token_id, shared_transaction_id, organisation_id, expected_purpose):
    if not qr_token_id:
        return None, None

    row = conn.execute(
        "SELECT * FROM qr_handoff_tokens WHERE qr_token_id = ?",
        (qr_token_id,)
    ).fetchone()

    if not row:
        return None, ({"error": "QR token not found"}, 404)

    if row["shared_transaction_id"] != shared_transaction_id:
        return None, ({"error": "QR token does not belong to this shared transaction"}, 409)

    if row["target_org_id"] != organisation_id:
        return None, ({"error": "QR token is not intended for this organisation"}, 403)

    if row["purpose"] != expected_purpose:
        return None, ({"error": f"QR token purpose must be {expected_purpose}"}, 409)

    if row["token_status"] not in ("ACTIVE", "SCANNED"):
        return None, ({"error": "QR token is not available for this action"}, 400)

    import datetime as _dt
    expires = _dt.datetime.fromisoformat(row["expires_at"])
    current_dt = _dt.datetime.fromisoformat(now_iso())
    if current_dt > expires:
        conn.execute(
            "UPDATE qr_handoff_tokens SET token_status = ? WHERE qr_token_id = ?",
            ("EXPIRED", qr_token_id)
        )
        conn.commit()
        return None, ({"error": "QR token has expired"}, 410)

    return row, None


def consume_qr_handoff_token(conn, qr_token_id, consumed_by_display_name, consumed_action):
    if not qr_token_id:
        return

    conn.execute(
        """
        UPDATE qr_handoff_tokens
        SET token_status = ?,
            consumed_at = ?,
            consumed_by_display_name = ?,
            consumed_action = ?
        WHERE qr_token_id = ?
        """,
        ("CONSUMED", now_iso(), consumed_by_display_name, consumed_action, qr_token_id)
    )


def record_shared_transaction_event(
    conn,
    shared_transaction_id,
    organisation_id,
    actor_org_role,
    action,
    summary,
    previous_status,
    new_status,
    created_by_display_name
):
    conn.execute(
        """
        INSERT INTO shared_transaction_events (
            shared_transaction_event_id,
            shared_transaction_id,
            organisation_id,
            actor_org_role,
            action,
            previous_status,
            new_status,
            summary,
            created_by_display_name,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            make_id("stev"),
            shared_transaction_id,
            organisation_id,
            actor_org_role,
            action,
            previous_status,
            new_status,
            summary,
            created_by_display_name,
            now_iso()
        )
    )


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
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()

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





def build_partner_address_navigation_contract(addr_like, default_nav_app="google_maps"):
    from urllib.parse import quote

    addr = dict(addr_like)

    def clean(value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    label = clean(addr.get("label")) or "Partner Address"
    gate_number = clean(addr.get("gate_number"))
    door_number = clean(addr.get("door_number"))

    address_parts = [
        clean(addr.get("address_line_1")),
        clean(addr.get("address_line_2")),
        clean(addr.get("suburb")),
        clean(addr.get("state")),
        clean(addr.get("postcode")),
        clean(addr.get("country")),
    ]
    formatted_address = ", ".join([x for x in address_parts if x]) or None

    latitude = addr.get("latitude")
    longitude = addr.get("longitude")

    has_gps = latitude is not None and longitude is not None
    destination_type = "gps" if has_gps else ("address" if formatted_address else None)
    can_navigate = destination_type is not None

    arrival_bits = []
    if gate_number:
        arrival_bits.append(f"Gate {gate_number}")
    if door_number:
        arrival_bits.append(f"Door {door_number}")
    arrival_hint = ", ".join(arrival_bits) or None

    apps = []
    if can_navigate:
        if has_gps:
            gps_pair = f"{latitude},{longitude}"
            google_uri = f"google.navigation:q={gps_pair}"
            waze_uri = f"waze://?ll={gps_pair}&navigate=yes"
            apple_uri = f"http://maps.apple.com/?ll={gps_pair}&q={quote(label)}"
            geo_uri = f"geo:{gps_pair}?q={gps_pair}({quote(label)})"
        else:
            encoded_address = quote(formatted_address)
            google_uri = f"google.navigation:q={encoded_address}"
            waze_uri = f"waze://?q={encoded_address}&navigate=yes"
            apple_uri = f"http://maps.apple.com/?address={encoded_address}"
            geo_uri = f"geo:0,0?q={encoded_address}"

        apps = [
            {
                "app_key": "google_maps",
                "app_label": "Google Maps",
                "is_default": False,
                "launch_uri": google_uri,
            },
            {
                "app_key": "waze",
                "app_label": "Waze",
                "is_default": False,
                "launch_uri": waze_uri,
            },
            {
                "app_key": "apple_maps",
                "app_label": "Apple Maps",
                "is_default": False,
                "launch_uri": apple_uri,
            },
            {
                "app_key": "generic_geo",
                "app_label": "Default Navigation App",
                "is_default": False,
                "launch_uri": geo_uri,
            },
        ]

        wanted = (default_nav_app or "google_maps").strip().lower()
        order = [wanted] + [a["app_key"] for a in apps if a["app_key"] != wanted]
        app_map = {a["app_key"]: a for a in apps}
        ordered = []
        for key in order:
            if key in app_map:
                item = dict(app_map[key])
                item["is_default"] = (key == wanted)
                ordered.append(item)
        apps = ordered

    navigation = {
        "can_navigate": can_navigate,
        "destination_type": destination_type,
        "latitude": latitude,
        "longitude": longitude,
        "formatted_address": formatted_address,
        "gate_number": gate_number,
        "door_number": door_number,
        "arrival_hint": arrival_hint,
        "preferred_label": label,
        "default_nav_app": (default_nav_app or "google_maps").strip().lower() or "google_maps",
    }

    return {
        "navigation": navigation,
        "navigation_apps": apps,
    }



@app.post("/partners/<partner_id>/addresses")
def create_partner_address(partner_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    label = (body.get("label") or "").strip()
    category = (body.get("category") or "").strip().upper()
    custom_category_label = (body.get("custom_category_label") or "").strip() or None

    address_line_1 = (body.get("address_line_1") or "").strip() or None
    address_line_2 = (body.get("address_line_2") or "").strip() or None
    suburb = (body.get("suburb") or "").strip() or None
    state = (body.get("state") or "").strip() or None
    postcode = (body.get("postcode") or "").strip() or None
    country = (body.get("country") or "").strip() or None
    gate_number = (body.get("gate_number") or "").strip() or None
    door_number = (body.get("door_number") or "").strip() or None
    entry_instructions = (body.get("entry_instructions") or "").strip() or None
    truck_access_notes = (body.get("truck_access_notes") or "").strip() or None

    latitude = body.get("latitude")
    longitude = body.get("longitude")

    is_primary = 1 if bool(body.get("is_primary")) else 0
    is_default_dispatch_site = 1 if bool(body.get("is_default_dispatch_site")) else 0
    is_default_receiving_site = 1 if bool(body.get("is_default_receiving_site")) else 0

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400
    if not label:
        return jsonify({"error": "label is required"}), 400

    allowed_categories = {
        "HEAD_OFFICE",
        "WORK_SITE",
        "WAREHOUSE",
        "FACTORY",
        "YARD",
        "DEPOT",
        "DISTRIBUTION_CENTRE",
        "OFFICE",
        "RETURN_SITE",
        "OTHER",
        "CUSTOM",
    }
    if category not in allowed_categories:
        return jsonify({"error": "Invalid category"}), 400
    if category == "CUSTOM" and not custom_category_label:
        return jsonify({"error": "custom_category_label is required when category is CUSTOM"}), 400

    if latitude is not None:
        try:
            latitude = float(latitude)
        except Exception:
            return jsonify({"error": "latitude must be a number"}), 400
    if longitude is not None:
        try:
            longitude = float(longitude)
        except Exception:
            return jsonify({"error": "longitude must be a number"}), 400

    conn = get_conn()
    ensure_partner_address_tables(conn)

    partner = conn.execute(
        "SELECT partner_id, organisation_id, name FROM partners WHERE partner_id = ? AND organisation_id = ?",
        (partner_id, organisation_id)
    ).fetchone()

    if not partner:
        conn.close()
        return jsonify({"error": "Partner not found"}), 404

    if is_primary:
        conn.execute(
            "UPDATE partner_addresses SET is_primary = 0 WHERE partner_id = ? AND organisation_id = ?",
            (partner_id, organisation_id)
        )
    if is_default_dispatch_site:
        conn.execute(
            "UPDATE partner_addresses SET is_default_dispatch_site = 0 WHERE partner_id = ? AND organisation_id = ?",
            (partner_id, organisation_id)
        )
    if is_default_receiving_site:
        conn.execute(
            "UPDATE partner_addresses SET is_default_receiving_site = 0 WHERE partner_id = ? AND organisation_id = ?",
            (partner_id, organisation_id)
        )

    partner_address_id = make_id("paddr")
    now = now_iso()

    conn.execute(
        """
        INSERT INTO partner_addresses (
            partner_address_id,
            partner_id,
            organisation_id,
            label,
            category,
            custom_category_label,
            is_active,
            is_primary,
            is_default_dispatch_site,
            is_default_receiving_site,
            address_line_1,
            address_line_2,
            suburb,
            state,
            postcode,
            country,
            gate_number,
            door_number,
            entry_instructions,
            truck_access_notes,
            latitude,
            longitude,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            partner_address_id,
            partner_id,
            organisation_id,
            label,
            category,
            custom_category_label,
            1,
            is_primary,
            is_default_dispatch_site,
            is_default_receiving_site,
            address_line_1,
            address_line_2,
            suburb,
            state,
            postcode,
            country,
            gate_number,
            door_number,
            entry_instructions,
            truck_access_notes,
            latitude,
            longitude,
            now,
            now
        )
    )

    audit_event(
        conn,
        entity_type="Partner",
        entity_id=partner_id,
        action="PARTNER_ADDRESS_CREATED",
        summary=f"Created partner address: {label}",
        organisation_id=organisation_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "partner_address_id": partner_address_id,
        "partner_id": partner_id,
        "organisation_id": organisation_id,
        "partner_name": partner["name"],
        "label": label,
        "category": category,
        "custom_category_label": custom_category_label,
        "is_active": True,
        "is_primary": bool(is_primary),
        "is_default_dispatch_site": bool(is_default_dispatch_site),
        "is_default_receiving_site": bool(is_default_receiving_site),
        "address_line_1": address_line_1,
        "address_line_2": address_line_2,
        "suburb": suburb,
        "state": state,
        "postcode": postcode,
        "country": country,
        "gate_number": gate_number,
        "door_number": door_number,
        "entry_instructions": entry_instructions,
        "truck_access_notes": truck_access_notes,
        "latitude": latitude,
        "longitude": longitude,
        "created_at": now,
        "updated_at": now
    }), 201


@app.get("/partners/<partner_id>/addresses")
def list_partner_addresses(partner_id):
    conn = get_conn()
    ensure_partner_address_tables(conn)

    partner = conn.execute(
        "SELECT partner_id, organisation_id, name FROM partners WHERE partner_id = ?",
        (partner_id,)
    ).fetchone()

    if not partner:
        conn.close()
        return jsonify({"error": "Partner not found"}), 404

    rows = conn.execute(
        """
        SELECT
            partner_address_id,
            partner_id,
            organisation_id,
            label,
            category,
            custom_category_label,
            is_active,
            is_primary,
            is_default_dispatch_site,
            is_default_receiving_site,
            address_line_1,
            address_line_2,
            suburb,
            state,
            postcode,
            country,
            gate_number,
            door_number,
            entry_instructions,
            truck_access_notes,
            latitude,
            longitude,
            created_at,
            updated_at
        FROM partner_addresses
        WHERE partner_id = ?
        ORDER BY is_primary DESC, label ASC, created_at DESC
        """,
        (partner_id,)
    ).fetchall()

    conn.close()

    items = []
    for row in rows:
        d = dict(row)
        d["is_active"] = bool(d["is_active"])
        d["is_primary"] = bool(d["is_primary"])
        d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
        d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
        nav_contract = build_partner_address_navigation_contract(d)
        d["navigation"] = nav_contract["navigation"]
        d["navigation_apps"] = nav_contract["navigation_apps"]
        items.append(d)

    return jsonify({
        "partner_id": partner_id,
        "organisation_id": partner["organisation_id"],
        "partner_name": partner["name"],
        "count": len(items),
        "items": items
    }), 200


@app.get("/partner-addresses/<partner_address_id>")
def get_partner_address(partner_address_id):
    conn = get_conn()
    ensure_partner_address_tables(conn)

    row = conn.execute(
        """
        SELECT
            pa.partner_address_id,
            pa.partner_id,
            pa.organisation_id,
            p.name AS partner_name,
            pa.label,
            pa.category,
            pa.custom_category_label,
            pa.is_active,
            pa.is_primary,
            pa.is_default_dispatch_site,
            pa.is_default_receiving_site,
            pa.address_line_1,
            pa.address_line_2,
            pa.suburb,
            pa.state,
            pa.postcode,
            pa.country,
            pa.gate_number,
            pa.door_number,
            pa.entry_instructions,
            pa.truck_access_notes,
            pa.latitude,
            pa.longitude,
            pa.created_at,
            pa.updated_at
        FROM partner_addresses pa
        LEFT JOIN partners p ON p.partner_id = pa.partner_id
        WHERE pa.partner_address_id = ?
        """,
        (partner_address_id,)
    ).fetchone()

    conn.close()

    if not row:
        return jsonify({"error": "Partner address not found"}), 404

    d = dict(row)
    d["is_active"] = bool(d["is_active"])
    d["is_primary"] = bool(d["is_primary"])
    d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
    d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
    nav_contract = build_partner_address_navigation_contract(d)
    d["navigation"] = nav_contract["navigation"]
    d["navigation_apps"] = nav_contract["navigation_apps"]

    return jsonify(d), 200




@app.patch("/partner-addresses/<partner_address_id>")
def update_partner_address(partner_address_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    updated_by_display_name = (body.get("updated_by_display_name") or "Unknown Admin").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400

    conn = get_conn()
    ensure_partner_address_tables(conn)

    addr = conn.execute(
        """
        SELECT pa.*, p.name AS partner_name
        FROM partner_addresses pa
        LEFT JOIN partners p ON p.partner_id = pa.partner_id
        WHERE pa.partner_address_id = ? AND pa.organisation_id = ?
        """,
        (partner_address_id, organisation_id)
    ).fetchone()

    if not addr:
        conn.close()
        return jsonify({"error": "Partner address not found"}), 404

    allowed_categories = {
        "HEAD_OFFICE",
        "WORK_SITE",
        "WAREHOUSE",
        "FACTORY",
        "YARD",
        "DEPOT",
        "DISTRIBUTION_CENTRE",
        "OFFICE",
        "RETURN_SITE",
        "OTHER",
        "CUSTOM",
    }

    new_category = body.get("category", addr["category"])
    if isinstance(new_category, str):
        new_category = new_category.strip().upper()

    new_custom_category_label = body.get("custom_category_label", addr["custom_category_label"])
    if isinstance(new_custom_category_label, str):
        new_custom_category_label = new_custom_category_label.strip() or None

    if new_category not in allowed_categories:
        conn.close()
        return jsonify({"error": "Invalid category"}), 400

    if new_category == "CUSTOM" and not new_custom_category_label:
        conn.close()
        return jsonify({"error": "custom_category_label is required when category is CUSTOM"}), 400

    if "label" in body:
        label_check = (body.get("label") or "").strip()
        if not label_check:
            conn.close()
            return jsonify({"error": "label cannot be blank"}), 400

    updates = []
    params = []

    text_fields = [
        "label",
        "category",
        "custom_category_label",
        "address_line_1",
        "address_line_2",
        "suburb",
        "state",
        "postcode",
        "country",
        "gate_number",
        "door_number",
        "entry_instructions",
        "truck_access_notes",
    ]

    for field in text_fields:
        if field in body:
            value = body.get(field)
            if isinstance(value, str):
                value = value.strip()
            if field == "category" and value is not None:
                value = value.upper()
            if field not in ("label", "category") and value == "":
                value = None
            updates.append(f"{field} = ?")
            params.append(value)

    for field in ("latitude", "longitude"):
        if field in body:
            value = body.get(field)
            if value in ("", None):
                value = None
            else:
                try:
                    value = float(value)
                except Exception:
                    conn.close()
                    return jsonify({"error": f"{field} must be a number"}), 400
            updates.append(f"{field} = ?")
            params.append(value)

    if not updates:
        conn.close()
        return jsonify({"error": "No editable fields were provided"}), 400

    updates.append("updated_at = ?")
    params.append(now_iso())
    params.append(partner_address_id)

    conn.execute(
        f"UPDATE partner_addresses SET {', '.join(updates)} WHERE partner_address_id = ?",
        params
    )

    audit_event(
        conn,
        entity_type="PartnerAddress",
        entity_id=partner_address_id,
        action="PARTNER_ADDRESS_UPDATED",
        summary=f"Partner address updated by {updated_by_display_name}",
        organisation_id=organisation_id
    )

    updated = conn.execute(
        """
        SELECT pa.*, p.name AS partner_name
        FROM partner_addresses pa
        LEFT JOIN partners p ON p.partner_id = pa.partner_id
        WHERE pa.partner_address_id = ?
        """,
        (partner_address_id,)
    ).fetchone()

    conn.commit()
    conn.close()

    d = dict(updated)
    d["is_active"] = bool(d["is_active"])
    d["is_primary"] = bool(d["is_primary"])
    d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
    d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
    return jsonify(d), 200


@app.post("/partner-addresses/<partner_address_id>/deactivate")
def deactivate_partner_address(partner_address_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    deactivated_by_display_name = (body.get("deactivated_by_display_name") or "Unknown Admin").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400

    conn = get_conn()
    ensure_partner_address_tables(conn)

    addr = conn.execute(
        """
        SELECT pa.*, p.name AS partner_name
        FROM partner_addresses pa
        LEFT JOIN partners p ON p.partner_id = pa.partner_id
        WHERE pa.partner_address_id = ? AND pa.organisation_id = ?
        """,
        (partner_address_id, organisation_id)
    ).fetchone()

    if not addr:
        conn.close()
        return jsonify({"error": "Partner address not found"}), 404

    conn.execute(
        """
        UPDATE partner_addresses
        SET is_active = 0,
            is_primary = 0,
            is_default_dispatch_site = 0,
            is_default_receiving_site = 0,
            updated_at = ?
        WHERE partner_address_id = ?
        """,
        (now_iso(), partner_address_id)
    )

    audit_event(
        conn,
        entity_type="PartnerAddress",
        entity_id=partner_address_id,
        action="PARTNER_ADDRESS_DEACTIVATED",
        summary=f"Partner address deactivated by {deactivated_by_display_name}",
        organisation_id=organisation_id
    )

    updated = conn.execute(
        """
        SELECT pa.*, p.name AS partner_name
        FROM partner_addresses pa
        LEFT JOIN partners p ON p.partner_id = pa.partner_id
        WHERE pa.partner_address_id = ?
        """,
        (partner_address_id,)
    ).fetchone()

    conn.commit()
    conn.close()

    d = dict(updated)
    d["is_active"] = bool(d["is_active"])
    d["is_primary"] = bool(d["is_primary"])
    d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
    d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
    return jsonify(d), 200


@app.post("/partner-addresses/<partner_address_id>/set-primary")
def set_primary_partner_address(partner_address_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    updated_by_display_name = (body.get("updated_by_display_name") or "Unknown Admin").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400

    conn = get_conn()
    ensure_partner_address_tables(conn)

    addr = conn.execute(
        "SELECT * FROM partner_addresses WHERE partner_address_id = ? AND organisation_id = ?",
        (partner_address_id, organisation_id)
    ).fetchone()

    if not addr:
        conn.close()
        return jsonify({"error": "Partner address not found"}), 404
    if not addr["is_active"]:
        conn.close()
        return jsonify({"error": "Inactive address cannot be set as primary"}), 400

    conn.execute(
        "UPDATE partner_addresses SET is_primary = 0 WHERE partner_id = ? AND organisation_id = ?",
        (addr["partner_id"], organisation_id)
    )
    conn.execute(
        "UPDATE partner_addresses SET is_primary = 1, updated_at = ? WHERE partner_address_id = ?",
        (now_iso(), partner_address_id)
    )

    audit_event(
        conn,
        entity_type="PartnerAddress",
        entity_id=partner_address_id,
        action="PARTNER_ADDRESS_SET_PRIMARY",
        summary=f"Primary partner address set by {updated_by_display_name}",
        organisation_id=organisation_id
    )

    updated = conn.execute(
        """
        SELECT pa.*, p.name AS partner_name
        FROM partner_addresses pa
        LEFT JOIN partners p ON p.partner_id = pa.partner_id
        WHERE pa.partner_address_id = ?
        """,
        (partner_address_id,)
    ).fetchone()

    conn.commit()
    conn.close()

    d = dict(updated)
    d["is_active"] = bool(d["is_active"])
    d["is_primary"] = bool(d["is_primary"])
    d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
    d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
    return jsonify(d), 200


@app.post("/partner-addresses/<partner_address_id>/set-default-dispatch")
def set_default_dispatch_partner_address(partner_address_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    updated_by_display_name = (body.get("updated_by_display_name") or "Unknown Admin").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400

    conn = get_conn()
    ensure_partner_address_tables(conn)

    addr = conn.execute(
        "SELECT * FROM partner_addresses WHERE partner_address_id = ? AND organisation_id = ?",
        (partner_address_id, organisation_id)
    ).fetchone()

    if not addr:
        conn.close()
        return jsonify({"error": "Partner address not found"}), 404
    if not addr["is_active"]:
        conn.close()
        return jsonify({"error": "Inactive address cannot be default dispatch"}), 400

    conn.execute(
        "UPDATE partner_addresses SET is_default_dispatch_site = 0 WHERE partner_id = ? AND organisation_id = ?",
        (addr["partner_id"], organisation_id)
    )
    conn.execute(
        "UPDATE partner_addresses SET is_default_dispatch_site = 1, updated_at = ? WHERE partner_address_id = ?",
        (now_iso(), partner_address_id)
    )

    audit_event(
        conn,
        entity_type="PartnerAddress",
        entity_id=partner_address_id,
        action="PARTNER_ADDRESS_SET_DEFAULT_DISPATCH",
        summary=f"Default dispatch partner address set by {updated_by_display_name}",
        organisation_id=organisation_id
    )

    updated = conn.execute(
        """
        SELECT pa.*, p.name AS partner_name
        FROM partner_addresses pa
        LEFT JOIN partners p ON p.partner_id = pa.partner_id
        WHERE pa.partner_address_id = ?
        """,
        (partner_address_id,)
    ).fetchone()

    conn.commit()
    conn.close()

    d = dict(updated)
    d["is_active"] = bool(d["is_active"])
    d["is_primary"] = bool(d["is_primary"])
    d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
    d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
    return jsonify(d), 200


@app.post("/partner-addresses/<partner_address_id>/set-default-receiving")
def set_default_receiving_partner_address(partner_address_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    updated_by_display_name = (body.get("updated_by_display_name") or "Unknown Admin").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400

    conn = get_conn()
    ensure_partner_address_tables(conn)

    addr = conn.execute(
        "SELECT * FROM partner_addresses WHERE partner_address_id = ? AND organisation_id = ?",
        (partner_address_id, organisation_id)
    ).fetchone()

    if not addr:
        conn.close()
        return jsonify({"error": "Partner address not found"}), 404
    if not addr["is_active"]:
        conn.close()
        return jsonify({"error": "Inactive address cannot be default receiving"}), 400

    conn.execute(
        "UPDATE partner_addresses SET is_default_receiving_site = 0 WHERE partner_id = ? AND organisation_id = ?",
        (addr["partner_id"], organisation_id)
    )
    conn.execute(
        "UPDATE partner_addresses SET is_default_receiving_site = 1, updated_at = ? WHERE partner_address_id = ?",
        (now_iso(), partner_address_id)
    )

    audit_event(
        conn,
        entity_type="PartnerAddress",
        entity_id=partner_address_id,
        action="PARTNER_ADDRESS_SET_DEFAULT_RECEIVING",
        summary=f"Default receiving partner address set by {updated_by_display_name}",
        organisation_id=organisation_id
    )

    updated = conn.execute(
        """
        SELECT pa.*, p.name AS partner_name
        FROM partner_addresses pa
        LEFT JOIN partners p ON p.partner_id = pa.partner_id
        WHERE pa.partner_address_id = ?
        """,
        (partner_address_id,)
    ).fetchone()

    conn.commit()
    conn.close()

    d = dict(updated)
    d["is_active"] = bool(d["is_active"])
    d["is_primary"] = bool(d["is_primary"])
    d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
    d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
    return jsonify(d), 200







@app.get("/partner-addresses/<partner_address_id>/audit")
def get_partner_address_audit(partner_address_id):
    conn = get_conn()
    ensure_partner_address_tables(conn)

    addr = conn.execute(
        """
        SELECT
            pa.partner_address_id,
            pa.partner_id,
            pa.organisation_id,
            p.name AS partner_name,
            pa.label,
            pa.category,
            pa.custom_category_label,
            pa.is_active,
            pa.is_primary,
            pa.is_default_dispatch_site,
            pa.is_default_receiving_site,
            pa.address_line_1,
            pa.address_line_2,
            pa.suburb,
            pa.state,
            pa.postcode,
            pa.country,
            pa.gate_number,
            pa.door_number,
            pa.entry_instructions,
            pa.truck_access_notes,
            pa.latitude,
            pa.longitude,
            pa.created_at,
            pa.updated_at
        FROM partner_addresses pa
        LEFT JOIN partners p ON p.partner_id = pa.partner_id
        WHERE pa.partner_address_id = ?
        """,
        (partner_address_id,)
    ).fetchone()

    if not addr:
        conn.close()
        return jsonify({"error": "Partner address not found"}), 404

    audit_rows = conn.execute(
        """
        SELECT *
        FROM audit_events
        WHERE entity_type = 'PartnerAddress'
          AND entity_id = ?
        ORDER BY created_at DESC
        """,
        (partner_address_id,)
    ).fetchall()

    location_rows = conn.execute(
        """
        SELECT *
        FROM location_update_requests
        WHERE entity_type = 'PartnerAddress'
          AND entity_id = ?
        ORDER BY created_at DESC
        """,
        (partner_address_id,)
    ).fetchall()

    conn.close()

    address = dict(addr)
    address["is_active"] = bool(address["is_active"])
    address["is_primary"] = bool(address["is_primary"])
    address["is_default_dispatch_site"] = bool(address["is_default_dispatch_site"])
    address["is_default_receiving_site"] = bool(address["is_default_receiving_site"])

    nav_contract = build_partner_address_navigation_contract(address)
    address["navigation"] = nav_contract["navigation"]
    address["navigation_apps"] = nav_contract["navigation_apps"]

    audit_events = [dict(r) for r in audit_rows]
    location_update_requests = [dict(r) for r in location_rows]

    return jsonify({
        "partner_address_id": address["partner_address_id"],
        "partner_id": address["partner_id"],
        "organisation_id": address["organisation_id"],
        "partner_name": address["partner_name"],
        "label": address["label"],
        "address": address,
        "audit_event_count": len(audit_events),
        "location_update_request_count": len(location_update_requests),
        "audit_events": audit_events,
        "location_update_requests": location_update_requests,
    }), 200


@app.get("/partner-addresses/<partner_address_id>/history")
def get_partner_address_history(partner_address_id):
    conn = get_conn()
    ensure_partner_address_tables(conn)

    addr = conn.execute(
        """
        SELECT
            pa.partner_address_id,
            pa.partner_id,
            pa.organisation_id,
            p.name AS partner_name,
            pa.label,
            pa.category
        FROM partner_addresses pa
        LEFT JOIN partners p ON p.partner_id = pa.partner_id
        WHERE pa.partner_address_id = ?
        """,
        (partner_address_id,)
    ).fetchone()

    if not addr:
        conn.close()
        return jsonify({"error": "Partner address not found"}), 404

    audit_rows = conn.execute(
        """
        SELECT *
        FROM audit_events
        WHERE entity_type = 'PartnerAddress'
          AND entity_id = ?
        """,
        (partner_address_id,)
    ).fetchall()

    location_rows = conn.execute(
        """
        SELECT *
        FROM location_update_requests
        WHERE entity_type = 'PartnerAddress'
          AND entity_id = ?
        """,
        (partner_address_id,)
    ).fetchall()

    conn.close()

    history = []

    for row in audit_rows:
        d = dict(row)
        history.append({
            "history_type": "AUDIT_EVENT",
            "sort_at": d.get("created_at"),
            "partner_address_id": partner_address_id,
            "partner_address_label": addr["label"],
            "partner_id": addr["partner_id"],
            "partner_name": addr["partner_name"],
            "payload": d,
        })

    for row in location_rows:
        d = dict(row)
        history.append({
            "history_type": "LOCATION_UPDATE_REQUEST",
            "sort_at": d.get("reviewed_at") or d.get("updated_at") or d.get("created_at"),
            "partner_address_id": partner_address_id,
            "partner_address_label": addr["label"],
            "partner_id": addr["partner_id"],
            "partner_name": addr["partner_name"],
            "payload": d,
        })

    history.sort(key=lambda x: x.get("sort_at") or "", reverse=True)

    return jsonify({
        "partner_address_id": addr["partner_address_id"],
        "partner_id": addr["partner_id"],
        "organisation_id": addr["organisation_id"],
        "partner_name": addr["partner_name"],
        "label": addr["label"],
        "category": addr["category"],
        "history_count": len(history),
        "history": history,
    }), 200


@app.get("/partners/<partner_id>/address-history")
def get_partner_address_history_for_partner(partner_id):
    conn = get_conn()
    ensure_partner_address_tables(conn)

    partner = conn.execute(
        "SELECT partner_id, organisation_id, name FROM partners WHERE partner_id = ?",
        (partner_id,)
    ).fetchone()

    if not partner:
        conn.close()
        return jsonify({"error": "Partner not found"}), 404

    address_rows = conn.execute(
        """
        SELECT
            partner_address_id,
            label,
            category,
            is_active,
            is_primary,
            is_default_dispatch_site,
            is_default_receiving_site
        FROM partner_addresses
        WHERE partner_id = ?
        ORDER BY created_at DESC
        """,
        (partner_id,)
    ).fetchall()

    address_map = {}
    for row in address_rows:
        d = dict(row)
        d["is_active"] = bool(d["is_active"])
        d["is_primary"] = bool(d["is_primary"])
        d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
        d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
        address_map[d["partner_address_id"]] = d

    history = []

    for partner_address_id, address_info in address_map.items():
        audit_rows = conn.execute(
            """
            SELECT *
            FROM audit_events
            WHERE entity_type = 'PartnerAddress'
              AND entity_id = ?
            """,
            (partner_address_id,)
        ).fetchall()

        location_rows = conn.execute(
            """
            SELECT *
            FROM location_update_requests
            WHERE entity_type = 'PartnerAddress'
              AND entity_id = ?
            """,
            (partner_address_id,)
        ).fetchall()

        for row in audit_rows:
            d = dict(row)
            history.append({
                "history_type": "AUDIT_EVENT",
                "sort_at": d.get("created_at"),
                "partner_address_id": partner_address_id,
                "partner_address_label": address_info["label"],
                "partner_address_category": address_info["category"],
                "partner_address_flags": {
                    "is_active": address_info["is_active"],
                    "is_primary": address_info["is_primary"],
                    "is_default_dispatch_site": address_info["is_default_dispatch_site"],
                    "is_default_receiving_site": address_info["is_default_receiving_site"],
                },
                "payload": d,
            })

        for row in location_rows:
            d = dict(row)
            history.append({
                "history_type": "LOCATION_UPDATE_REQUEST",
                "sort_at": d.get("reviewed_at") or d.get("updated_at") or d.get("created_at"),
                "partner_address_id": partner_address_id,
                "partner_address_label": address_info["label"],
                "partner_address_category": address_info["category"],
                "partner_address_flags": {
                    "is_active": address_info["is_active"],
                    "is_primary": address_info["is_primary"],
                    "is_default_dispatch_site": address_info["is_default_dispatch_site"],
                    "is_default_receiving_site": address_info["is_default_receiving_site"],
                },
                "payload": d,
            })

    conn.close()

    history.sort(key=lambda x: x.get("sort_at") or "", reverse=True)

    return jsonify({
        "partner_id": partner["partner_id"],
        "organisation_id": partner["organisation_id"],
        "partner_name": partner["name"],
        "address_count": len(address_map),
        "address_history_count": len(history),
        "items": history,
    }), 200



@app.get("/partner-addresses/<partner_address_id>/navigation-options")
def get_partner_address_navigation_options(partner_address_id):
    default_nav_app = (request.args.get("default_nav_app") or "google_maps").strip().lower() or "google_maps"

    conn = get_conn()
    ensure_partner_address_tables(conn)

    row = conn.execute(
        """
        SELECT
            pa.partner_address_id,
            pa.partner_id,
            pa.organisation_id,
            p.name AS partner_name,
            pa.label,
            pa.category,
            pa.custom_category_label,
            pa.is_active,
            pa.is_primary,
            pa.is_default_dispatch_site,
            pa.is_default_receiving_site,
            pa.address_line_1,
            pa.address_line_2,
            pa.suburb,
            pa.state,
            pa.postcode,
            pa.country,
            pa.gate_number,
            pa.door_number,
            pa.entry_instructions,
            pa.truck_access_notes,
            pa.latitude,
            pa.longitude,
            pa.created_at,
            pa.updated_at
        FROM partner_addresses pa
        LEFT JOIN partners p ON p.partner_id = pa.partner_id
        WHERE pa.partner_address_id = ?
        """,
        (partner_address_id,)
    ).fetchone()

    conn.close()

    if not row:
        return jsonify({"error": "Partner address not found"}), 404

    d = dict(row)
    d["is_active"] = bool(d["is_active"])
    d["is_primary"] = bool(d["is_primary"])
    d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
    d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])

    nav_contract = build_partner_address_navigation_contract(d, default_nav_app=default_nav_app)

    return jsonify({
        "partner_address_id": d["partner_address_id"],
        "partner_id": d["partner_id"],
        "organisation_id": d["organisation_id"],
        "partner_name": d["partner_name"],
        "label": d["label"],
        "navigation": nav_contract["navigation"],
        "navigation_apps": nav_contract["navigation_apps"],
    }), 200



@app.post("/partner-addresses/<partner_address_id>/location-update-request")
def submit_partner_address_location_update_request(partner_address_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    proposed_latitude = body.get("proposed_latitude")
    proposed_longitude = body.get("proposed_longitude")
    reason_text = (body.get("reason_text") or "Field user suggested GPS update").strip()
    submitted_by_display_name = (body.get("submitted_by_display_name") or "Unknown User").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400
    if proposed_latitude is None or proposed_longitude is None:
        return jsonify({"error": "proposed_latitude and proposed_longitude are required"}), 400

    try:
        proposed_latitude = float(proposed_latitude)
        proposed_longitude = float(proposed_longitude)
    except Exception:
        return jsonify({"error": "proposed_latitude and proposed_longitude must be numbers"}), 400

    conn = get_conn()
    ensure_partner_address_tables(conn)

    addr = conn.execute(
        """
        SELECT pa.*, p.name AS partner_name
        FROM partner_addresses pa
        LEFT JOIN partners p ON p.partner_id = pa.partner_id
        WHERE pa.partner_address_id = ? AND pa.organisation_id = ?
        """,
        (partner_address_id, organisation_id)
    ).fetchone()

    if not addr:
        conn.close()
        return jsonify({"error": "Partner address not found"}), 404

    req_id = make_id("lreq")
    now = now_iso()

    conn.execute(
        """
        INSERT INTO location_update_requests (
            location_update_request_id,
            organisation_id,
            entity_type,
            entity_id,
            current_latitude,
            current_longitude,
            proposed_latitude,
            proposed_longitude,
            reason_text,
            status,
            submitted_by_display_name,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            req_id,
            organisation_id,
            "PartnerAddress",
            partner_address_id,
            addr["latitude"],
            addr["longitude"],
            proposed_latitude,
            proposed_longitude,
            reason_text,
            "PENDING_APPROVAL",
            submitted_by_display_name,
            now,
            now
        )
    )

    audit_event(
        conn,
        entity_type="PartnerAddress",
        entity_id=partner_address_id,
        action="LOCATION_UPDATE_REQUESTED",
        summary=f"GPS update requested for partner address by {submitted_by_display_name}",
        organisation_id=organisation_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "location_update_request_id": req_id,
        "organisation_id": organisation_id,
        "entity_type": "PartnerAddress",
        "entity_id": partner_address_id,
        "partner_id": addr["partner_id"],
        "partner_name": addr["partner_name"],
        "label": addr["label"],
        "status": "PENDING_APPROVAL",
        "current_latitude": addr["latitude"],
        "current_longitude": addr["longitude"],
        "proposed_latitude": proposed_latitude,
        "proposed_longitude": proposed_longitude,
        "reason_text": reason_text,
        "submitted_by_display_name": submitted_by_display_name
    }), 201


@app.get("/organisations/<organisation_id>/location-update-requests")
def list_location_update_requests(organisation_id):
    status = (request.args.get("status") or "PENDING_APPROVAL").strip().upper()
    entity_type = (request.args.get("entity_type") or "").strip()

    conn = get_conn()
    ensure_partner_address_tables(conn)

    sql = """
        SELECT
            lur.location_update_request_id,
            lur.organisation_id,
            lur.entity_type,
            lur.entity_id,
            pa.partner_id,
            p.name AS partner_name,
            pa.label AS entity_name,
            lur.current_latitude,
            lur.current_longitude,
            lur.proposed_latitude,
            lur.proposed_longitude,
            lur.reason_text,
            lur.status,
            lur.submitted_by_display_name,
            lur.reviewed_by_display_name,
            lur.review_notes,
            lur.reviewed_at,
            lur.created_at,
            lur.updated_at
        FROM location_update_requests lur
        LEFT JOIN partner_addresses pa
            ON lur.entity_type = 'PartnerAddress' AND pa.partner_address_id = lur.entity_id
        LEFT JOIN partners p
            ON p.partner_id = pa.partner_id
        WHERE lur.organisation_id = ?
          AND lur.status = ?
    """
    params = [organisation_id, status]

    if entity_type:
        sql += " AND lur.entity_type = ?"
        params.append(entity_type)

    sql += " ORDER BY lur.created_at DESC"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    return jsonify({
        "organisation_id": organisation_id,
        "count": len(rows),
        "items": [dict(r) for r in rows]
    }), 200


@app.post("/location-update-requests/<location_update_request_id>/approve")
def approve_location_update_request(location_update_request_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    reviewed_by_display_name = (body.get("reviewed_by_display_name") or "Unknown Admin").strip()
    review_notes = (body.get("review_notes") or "").strip() or None

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400

    conn = get_conn()
    ensure_partner_address_tables(conn)

    req = conn.execute(
        "SELECT * FROM location_update_requests WHERE location_update_request_id = ? AND organisation_id = ?",
        (location_update_request_id, organisation_id)
    ).fetchone()

    if not req:
        conn.close()
        return jsonify({"error": "Location update request not found"}), 404

    if req["status"] != "PENDING_APPROVAL":
        conn.close()
        return jsonify({"error": "Location update request is not pending approval"}), 400

    if req["entity_type"] != "PartnerAddress":
        conn.close()
        return jsonify({"error": "Unsupported entity_type for this approval route"}), 400

    conn.execute(
        "UPDATE partner_addresses SET latitude = ?, longitude = ?, updated_at = ? WHERE partner_address_id = ?",
        (req["proposed_latitude"], req["proposed_longitude"], now_iso(), req["entity_id"])
    )

    conn.execute(
        """
        UPDATE location_update_requests
        SET status = 'APPROVED',
            reviewed_by_display_name = ?,
            review_notes = ?,
            reviewed_at = ?,
            updated_at = ?
        WHERE location_update_request_id = ?
        """,
        (reviewed_by_display_name, review_notes, now_iso(), now_iso(), location_update_request_id)
    )

    audit_event(
        conn,
        entity_type="PartnerAddress",
        entity_id=req["entity_id"],
        action="LOCATION_UPDATE_APPROVED",
        summary=f"GPS update approved by {reviewed_by_display_name}",
        organisation_id=organisation_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "location_update_request_id": location_update_request_id,
        "status": "APPROVED",
        "entity_type": "PartnerAddress",
        "entity_id": req["entity_id"],
        "latitude": req["proposed_latitude"],
        "longitude": req["proposed_longitude"],
        "reviewed_by_display_name": reviewed_by_display_name,
        "review_notes": review_notes
    }), 200


@app.post("/location-update-requests/<location_update_request_id>/reject")
def reject_location_update_request(location_update_request_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    reviewed_by_display_name = (body.get("reviewed_by_display_name") or "Unknown Admin").strip()
    review_notes = (body.get("review_notes") or "").strip() or None

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400

    conn = get_conn()
    ensure_partner_address_tables(conn)

    req = conn.execute(
        "SELECT * FROM location_update_requests WHERE location_update_request_id = ? AND organisation_id = ?",
        (location_update_request_id, organisation_id)
    ).fetchone()

    if not req:
        conn.close()
        return jsonify({"error": "Location update request not found"}), 404

    if req["status"] != "PENDING_APPROVAL":
        conn.close()
        return jsonify({"error": "Location update request is not pending approval"}), 400

    conn.execute(
        """
        UPDATE location_update_requests
        SET status = 'REJECTED',
            reviewed_by_display_name = ?,
            review_notes = ?,
            reviewed_at = ?,
            updated_at = ?
        WHERE location_update_request_id = ?
        """,
        (reviewed_by_display_name, review_notes, now_iso(), now_iso(), location_update_request_id)
    )

    audit_event(
        conn,
        entity_type=req["entity_type"],
        entity_id=req["entity_id"],
        action="LOCATION_UPDATE_REJECTED",
        summary=f"GPS update rejected by {reviewed_by_display_name}",
        organisation_id=organisation_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "location_update_request_id": location_update_request_id,
        "status": "REJECTED",
        "entity_type": req["entity_type"],
        "entity_id": req["entity_id"],
        "reviewed_by_display_name": reviewed_by_display_name,
        "review_notes": review_notes
    }), 200


@app.post("/depots")
def create_depot():
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    name = (body.get("name") or "").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400
    if not name:
        return jsonify({"error": "name is required"}), 400

    conn = get_conn()
    org = conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchone()

    if not org:
        conn.close()
        return jsonify({"error": "Organisation not found"}), 404

    depot_id = make_id("depot")

    conn.execute(
        "INSERT INTO depots (depot_id, organisation_id, name, opening_balance_used, created_at) VALUES (?, ?, ?, ?, ?)",
        (depot_id, organisation_id, name, 0, now_iso())
    )

    audit_event(
        conn,
        entity_type="Depot",
        entity_id=depot_id,
        action="CREATE",
        summary=f"Created depot: {name}",
        organisation_id=organisation_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "depot_id": depot_id,
        "organisation_id": organisation_id,
        "name": name,
        "opening_balance_used": False
    }), 201


@app.get("/depots/<depot_id>")
def get_depot_profile(depot_id):
    conn = get_conn()

    depot = conn.execute(
        """
        SELECT
            d.depot_id,
            d.organisation_id,
            d.name,
            d.opening_balance_used,
            d.created_at,
            o.name AS organisation_name
        FROM depots d
        LEFT JOIN organisations o ON o.organisation_id = d.organisation_id
        WHERE d.depot_id = ?
        """,
        (depot_id,)
    ).fetchone()

    if not depot:
        conn.close()
        return jsonify({"error": "Depot not found"}), 404

    stock_rows = conn.execute(
        """
        SELECT
            bp.resource_id,
            r.name AS resource_name,
            r.resource_type,
            r.unit_type,
            bp.current_quantity,
            bp.updated_at
        FROM balance_projection bp
        LEFT JOIN resources r ON r.resource_id = bp.resource_id
        WHERE bp.depot_id = ?
        ORDER BY r.name
        """,
        (depot_id,)
    ).fetchall()

    pending_rows = conn.execute(
        """
        SELECT
            pending_entry_id,
            entry_type,
            status,
            reason_code,
            reason_text,
            direct_action_label,
            direct_action_target_id,
            resource_id,
            resource_name,
            source_record_id,
            created_at
        FROM pending_approval_entries
        WHERE related_entity_id = ?
          AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
        ORDER BY created_at DESC
        """,
        (depot_id,)
    ).fetchall()

    history_rows = conn.execute(
        """
        SELECT
            pending_entry_id,
            entry_type,
            status,
            reason_code,
            reason_text,
            direct_action_label,
            direct_action_target_id,
            resource_id,
            resource_name,
            source_record_id,
            created_at
        FROM pending_approval_entries
        WHERE related_entity_id = ?
          AND status IN ('RESOLVED', 'REJECTED')
        ORDER BY created_at DESC
        """,
        (depot_id,)
    ).fetchall()

    conn.close()

    opening_balance_used = bool(depot["opening_balance_used"])

    return jsonify({
        "depot_id": depot["depot_id"],
        "organisation_id": depot["organisation_id"],
        "organisation_name": depot["organisation_name"],
        "name": depot["name"],
        "created_at": depot["created_at"],
        "opening_balance_used": opening_balance_used,
        "opening_balance_available": not opening_balance_used,
        "opening_balance_action_label": "Enter Opening Balance" if not opening_balance_used else None,
        "stock_count": len(stock_rows),
        "stock_items": [dict(r) for r in stock_rows],
        "pending_count": len(pending_rows),
        "pending_items": [dict(r) for r in pending_rows],
        "history_count": len(history_rows),
        "history_items": [dict(r) for r in history_rows]
    }), 200


def _ensure_depot_update_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(depots)")}
    if "is_active" not in cols:
        conn.execute("ALTER TABLE depots ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE depots ADD COLUMN updated_at TEXT")
    conn.commit()


@app.patch("/depots/<depot_id>")
def update_depot(depot_id):
    body = request.get_json(silent=True) or {}
    new_name = (body.get("name") or "").strip() or None

    if not new_name:
        return jsonify({"error": "name is required"}), 400

    conn = get_conn()
    _ensure_depot_update_columns(conn)

    depot = conn.execute(
        "SELECT * FROM depots WHERE depot_id = ?", (depot_id,)
    ).fetchone()

    if not depot:
        conn.close()
        return jsonify({"error": "Depot not found"}), 404

    old_name = depot["name"]
    ts = now_iso()

    conn.execute(
        "UPDATE depots SET name = ?, updated_at = ? WHERE depot_id = ?",
        (new_name, ts, depot_id),
    )

    audit_event(
        conn,
        entity_type="Depot",
        entity_id=depot_id,
        action="UPDATE",
        summary=f"Depot name updated from '{old_name}' to '{new_name}'.",
        organisation_id=depot["organisation_id"],
    )

    conn.commit()
    conn.close()

    return jsonify({
        "depot_id": depot_id,
        "organisation_id": depot["organisation_id"],
        "name": new_name,
        "updated_at": ts,
    }), 200


@app.post("/depots/<depot_id>/deactivate")
def deactivate_depot(depot_id):
    conn = get_conn()
    _ensure_depot_update_columns(conn)

    depot = conn.execute(
        "SELECT * FROM depots WHERE depot_id = ?", (depot_id,)
    ).fetchone()

    if not depot:
        conn.close()
        return jsonify({"error": "Depot not found"}), 404

    depot_cols = {row["name"] for row in conn.execute("PRAGMA table_info(depots)")}
    if "is_active" in depot_cols and depot["is_active"] == 0:
        conn.close()
        return jsonify({"error": "Depot is already inactive"}), 409

    ts = now_iso()
    conn.execute(
        "UPDATE depots SET is_active = 0, updated_at = ? WHERE depot_id = ?",
        (ts, depot_id),
    )

    audit_event(
        conn,
        entity_type="Depot",
        entity_id=depot_id,
        action="DEACTIVATE",
        summary=f"Depot '{depot['name']}' deactivated.",
        organisation_id=depot["organisation_id"],
    )

    conn.commit()
    conn.close()

    return jsonify({
        "depot_id": depot_id,
        "name": depot["name"],
        "is_active": False,
    }), 200


@app.post("/depots/<depot_id>/reactivate")
def reactivate_depot(depot_id):
    conn = get_conn()
    _ensure_depot_update_columns(conn)

    depot = conn.execute(
        "SELECT * FROM depots WHERE depot_id = ?", (depot_id,)
    ).fetchone()

    if not depot:
        conn.close()
        return jsonify({"error": "Depot not found"}), 404

    ts = now_iso()
    conn.execute(
        "UPDATE depots SET is_active = 1, updated_at = ? WHERE depot_id = ?",
        (ts, depot_id),
    )

    audit_event(
        conn,
        entity_type="Depot",
        entity_id=depot_id,
        action="REACTIVATE",
        summary=f"Depot '{depot['name']}' reactivated.",
        organisation_id=depot["organisation_id"],
    )

    conn.commit()
    conn.close()

    return jsonify({
        "depot_id": depot_id,
        "name": depot["name"],
        "is_active": True,
    }), 200


@app.get("/organisations/<organisation_id>/partner-module")
def get_partner_module_overview(organisation_id):
    conn = get_conn()
    ensure_partner_connection_tables(conn)

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

    partner_count_total = conn.execute(
        "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchone()["c"]

    partner_count_active = conn.execute(
        "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1",
        (organisation_id,)
    ).fetchone()["c"]

    partner_count_inactive = conn.execute(
        "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 0",
        (organisation_id,)
    ).fetchone()["c"]

    customer_count = conn.execute(
        "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1 AND is_customer = 1",
        (organisation_id,)
    ).fetchone()["c"]

    supplier_count = conn.execute(
        "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1 AND is_supplier = 1",
        (organisation_id,)
    ).fetchone()["c"]

    customer_only_count = conn.execute(
        "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1 AND is_customer = 1 AND is_supplier = 0",
        (organisation_id,)
    ).fetchone()["c"]

    supplier_only_count = conn.execute(
        "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1 AND is_supplier = 1 AND is_customer = 0",
        (organisation_id,)
    ).fetchone()["c"]

    both_count = conn.execute(
        "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1 AND is_customer = 1 AND is_supplier = 1",
        (organisation_id,)
    ).fetchone()["c"]

    connected_partner_count = conn.execute(
        "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1 AND COALESCE(connection_status, 'LOCAL_ONLY') = 'CONNECTED'",
        (organisation_id,)
    ).fetchone()["c"]

    pending_incoming_count = conn.execute(
        "SELECT COUNT(*) AS c FROM org_connection_requests WHERE target_org_id = ? AND status = 'PENDING_APPROVAL'",
        (organisation_id,)
    ).fetchone()["c"]

    pending_outgoing_count = conn.execute(
        "SELECT COUNT(*) AS c FROM org_connection_requests WHERE requesting_org_id = ? AND status = 'PENDING_APPROVAL'",
        (organisation_id,)
    ).fetchone()["c"]

    all_rows = conn.execute(
        """
        SELECT
            p.partner_id,
            p.organisation_id,
            p.name,
            p.is_active,
            p.is_customer,
            p.is_supplier,
            p.created_at,
            p.updated_at,
            p.linked_org_id,
            lo.name AS linked_org_name,
            COALESCE(p.connection_status, 'LOCAL_ONLY') AS connection_status
        FROM partners p
        LEFT JOIN organisations lo ON lo.organisation_id = p.linked_org_id
        WHERE p.organisation_id = ?
        ORDER BY p.is_active DESC, p.created_at DESC
        LIMIT 50
        """,
        (organisation_id,)
    ).fetchall()

    customer_rows = conn.execute(
        """
        SELECT
            p.partner_id,
            p.organisation_id,
            p.name,
            p.is_active,
            p.is_customer,
            p.is_supplier,
            p.created_at,
            p.updated_at,
            p.linked_org_id,
            lo.name AS linked_org_name,
            COALESCE(p.connection_status, 'LOCAL_ONLY') AS connection_status
        FROM partners p
        LEFT JOIN organisations lo ON lo.organisation_id = p.linked_org_id
        WHERE p.organisation_id = ?
          AND p.is_customer = 1
        ORDER BY p.is_active DESC, p.created_at DESC
        LIMIT 50
        """,
        (organisation_id,)
    ).fetchall()

    supplier_rows = conn.execute(
        """
        SELECT
            p.partner_id,
            p.organisation_id,
            p.name,
            p.is_active,
            p.is_customer,
            p.is_supplier,
            p.created_at,
            p.updated_at,
            p.linked_org_id,
            lo.name AS linked_org_name,
            COALESCE(p.connection_status, 'LOCAL_ONLY') AS connection_status
        FROM partners p
        LEFT JOIN organisations lo ON lo.organisation_id = p.linked_org_id
        WHERE p.organisation_id = ?
          AND p.is_supplier = 1
        ORDER BY p.is_active DESC, p.created_at DESC
        LIMIT 50
        """,
        (organisation_id,)
    ).fetchall()

    connection_rows = conn.execute(
        """
        SELECT
            ocr.connection_request_id,
            ocr.requesting_org_id,
            ro.name AS requesting_org_name,
            ocr.requesting_partner_id,
            p.name AS partner_name,
            ocr.target_org_id,
            to2.name AS target_org_name,
            ocr.status,
            ocr.created_at,
            ocr.updated_at
        FROM org_connection_requests ocr
        LEFT JOIN organisations ro ON ro.organisation_id = ocr.requesting_org_id
        LEFT JOIN organisations to2 ON to2.organisation_id = ocr.target_org_id
        LEFT JOIN partners p ON p.partner_id = ocr.requesting_partner_id
        WHERE (ocr.requesting_org_id = ? OR ocr.target_org_id = ?)
          AND ocr.status = 'PENDING_APPROVAL'
        ORDER BY ocr.created_at DESC
        LIMIT 20
        """,
        (organisation_id, organisation_id)
    ).fetchall()

    conn.close()

    def decorate_partner(row):
        d = dict(row)
        d["is_active"] = bool(d["is_active"])
        d["is_customer"] = bool(d["is_customer"])
        d["is_supplier"] = bool(d["is_supplier"])

        if d["is_customer"] and d["is_supplier"]:
            d["role_label"] = "Customer + Supplier"
        elif d["is_customer"]:
            d["role_label"] = "Customer"
        elif d["is_supplier"]:
            d["role_label"] = "Supplier"
        else:
            d["role_label"] = "Partner"

        if d["connection_status"] == "CONNECTED":
            d["connection_label"] = "Connected Pallet Pro Org"
        elif d["connection_status"] == "REQUESTED":
            d["connection_label"] = "Connection Requested"
        else:
            d["connection_label"] = "Local Partner Only"

        d["entry_target_section"] = "all_partners"
        d["entry_target_entity_type"] = "Partner"
        d["entry_target_action"] = "open_partner_profile"
        d["highlight_key"] = d["partner_id"]
        return d

    recent_all_partners = [decorate_partner(r) for r in all_rows]
    recent_customers = [decorate_partner(r) for r in customer_rows]
    recent_suppliers = [decorate_partner(r) for r in supplier_rows]

    recent_connection_requests = []
    for row in connection_rows:
        d = dict(row)
        d["request_direction"] = "INCOMING" if d["target_org_id"] == organisation_id else "OUTGOING"
        d["entry_target_section"] = "connection_requests"
        d["entry_target_entity_type"] = "OrgConnectionRequest"
        d["entry_target_action"] = "review_connection_request"
        d["highlight_key"] = d["connection_request_id"]
        recent_connection_requests.append(d)

    return jsonify({
        "module_key": "partner-module",
        "organisation_id": org["organisation_id"],
        "organisation_name": org["name"],
        "created_at": org["created_at"],
        "default_section": "all_partners",
        "navigation": {
            "customers": {
                "label": "Customers",
                "count": customer_count
            },
            "suppliers": {
                "label": "Suppliers",
                "count": supplier_count
            },
            "all_partners": {
                "label": "All Partners",
                "count": partner_count_active
            }
        },
        "summary": {
            "partner_count": partner_count_active,
            "partner_count_active": partner_count_active,
            "partner_count_inactive": partner_count_inactive,
            "partner_count_total": partner_count_total,
            "customer_count": customer_count,
            "supplier_count": supplier_count,
            "customer_only_count": customer_only_count,
            "supplier_only_count": supplier_only_count,
            "customer_supplier_both_count": both_count,
            "connected_partner_count": connected_partner_count,
            "incoming_connection_request_count": pending_incoming_count,
            "outgoing_connection_request_count": pending_outgoing_count
        },
        "recent_customers": recent_customers,
        "recent_suppliers": recent_suppliers,
        "recent_all_partners": recent_all_partners,
        "recent_connection_requests": recent_connection_requests
    }), 200


@app.get("/organisations/<organisation_id>/resource-module")
def get_resource_module_overview(organisation_id):
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

    resource_count_total = conn.execute(
        "SELECT COUNT(*) AS c FROM resources WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchone()["c"]

    resource_count_active = conn.execute(
        "SELECT COUNT(*) AS c FROM resources WHERE organisation_id = ? AND is_active = 1",
        (organisation_id,)
    ).fetchone()["c"]

    resource_count_inactive = conn.execute(
        "SELECT COUNT(*) AS c FROM resources WHERE organisation_id = ? AND is_active = 0",
        (organisation_id,)
    ).fetchone()["c"]

    brand_count = conn.execute(
        "SELECT COUNT(*) AS c FROM brands WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchone()["c"]

    category_count = conn.execute(
        "SELECT COUNT(*) AS c FROM categories WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchone()["c"]

    status_rows = conn.execute(
        """
        SELECT status, COUNT(*) AS item_count
        FROM pending_approval_entries
        WHERE organisation_id = ?
          AND entry_type IN ('ResourceRequest', 'BrandRequest', 'CategoryRequest')
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
          AND entry_type IN ('ResourceRequest', 'BrandRequest', 'CategoryRequest')
          AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
        ORDER BY created_at DESC
        LIMIT 10
        """,
        (organisation_id,)
    ).fetchall()

    recent_history_rows = conn.execute(
        """
        SELECT
            pending_entry_id,
            entry_type,
            status,
            reason_code,
            reason_text,
            rejection_reason_code,
            rejection_reason_text,
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
          AND entry_type IN ('ResourceRequest', 'BrandRequest', 'CategoryRequest')
          AND status IN ('RESOLVED', 'REJECTED')
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 10
        """,
        (organisation_id,)
    ).fetchall()

    recent_resources = conn.execute(
        """
        SELECT
            r.resource_id,
            r.name,
            r.resource_type,
            r.unit_type,
            r.is_active,
            r.created_at,
            r.updated_at,
            r.category_id,
            c.name AS category_name,
            r.brand_id,
            b.name AS brand_name,
            r.merged_into_resource_id,
            r.inactive_reason_code,
            r.inactive_reason_text
        FROM resources r
        LEFT JOIN categories c ON c.category_id = r.category_id
        LEFT JOIN brands b ON b.brand_id = r.brand_id
        WHERE r.organisation_id = ?
        ORDER BY r.is_active DESC, r.created_at DESC
        LIMIT 10
        """,
        (organisation_id,)
    ).fetchall()

    recent_brands = conn.execute(
        """
        SELECT
            brand_id,
            name,
            is_active,
            created_at
        FROM brands
        WHERE organisation_id = ?
        ORDER BY created_at DESC
        LIMIT 10
        """,
        (organisation_id,)
    ).fetchall()

    recent_categories = conn.execute(
        """
        SELECT
            category_id,
            name,
            is_active,
            created_at
        FROM categories
        WHERE organisation_id = ?
        ORDER BY created_at DESC
        LIMIT 10
        """,
        (organisation_id,)
    ).fetchall()

    conn.close()

    def decorate_request_item(row_dict):
        mapping = {
            "ResourceRequest": {
                "entry_target_section": "requests/resources",
                "entry_target_entity_type": "ResourceRequest",
                "entry_target_action": "review_create_resource",
                "highlight_key": row_dict.get("source_record_id"),
            },
            "BrandRequest": {
                "entry_target_section": "requests/brands",
                "entry_target_entity_type": "BrandRequest",
                "entry_target_action": "review_create_brand",
                "highlight_key": row_dict.get("source_record_id"),
            },
            "CategoryRequest": {
                "entry_target_section": "requests/categories",
                "entry_target_entity_type": "CategoryRequest",
                "entry_target_action": "review_create_category",
                "highlight_key": row_dict.get("source_record_id"),
            },
        }
        row_dict.update(mapping.get(row_dict.get("entry_type"), {}))
        return row_dict

    recent_open_items = [decorate_request_item(dict(r)) for r in recent_open_rows]
    recent_history_items = [decorate_request_item(dict(r)) for r in recent_history_rows]

    recent_resource_items = []
    for row in recent_resources:
        d = dict(row)
        d["is_active"] = bool(d["is_active"])
        d["entry_target_action"] = "open_resource_profile"
        d["entry_target_entity_type"] = "Resource"
        d["entry_target_section"] = "resources"
        d["highlight_key"] = d["resource_id"]
        recent_resource_items.append(d)

    recent_brand_items = []
    for row in recent_brands:
        d = dict(row)
        d["is_active"] = bool(d["is_active"])
        d["entry_target_action"] = "open_brand_profile"
        d["entry_target_entity_type"] = "Brand"
        d["entry_target_section"] = "brands"
        d["highlight_key"] = d["brand_id"]
        recent_brand_items.append(d)

    recent_category_items = []
    for row in recent_categories:
        d = dict(row)
        d["is_active"] = bool(d["is_active"])
        d["entry_target_action"] = "open_category_profile"
        d["entry_target_entity_type"] = "Category"
        d["entry_target_section"] = "categories"
        d["highlight_key"] = d["category_id"]
        recent_category_items.append(d)

    open_count = (
        counts["PENDING_APPROVAL"]
        + counts["AWAITING_FIX"]
        + counts["READY_TO_APPROVE"]
    )

    return jsonify({
        "module_key": "resource-module",
        "organisation_id": org["organisation_id"],
        "organisation_name": org["name"],
        "created_at": org["created_at"],
        "default_section": "open_requests",
        "navigation": {
            "open_requests": {
                "label": "Open Requests",
                "count": open_count
            },
            "history": {
                "label": "History",
                "count": counts["RESOLVED"] + counts["REJECTED"]
            },
            "resources": {
                "label": "Resources",
                "count": resource_count_active
            },
            "brands": {
                "label": "Brands",
                "count": brand_count
            },
            "categories": {
                "label": "Categories",
                "count": category_count
            }
        },
        "summary": {
            "resource_count": resource_count_active,
            "resource_count_active": resource_count_active,
            "resource_count_inactive": resource_count_inactive,
            "resource_count_total": resource_count_total,
            "brand_count": brand_count,
            "category_count": category_count,
            "open_count": open_count,
            "pending_approval_count": counts["PENDING_APPROVAL"],
            "awaiting_fix_count": counts["AWAITING_FIX"],
            "ready_to_approve_count": counts["READY_TO_APPROVE"],
            "resolved_count": counts["RESOLVED"],
            "rejected_count": counts["REJECTED"]
        },
        "recent_open_items": recent_open_items,
        "recent_history_items": recent_history_items,
        "recent_resources": recent_resource_items,
        "recent_brands": recent_brand_items,
        "recent_categories": recent_category_items
    }), 200



@app.post("/partners")
def create_partner():
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    name = (body.get("name") or "").strip()
    is_active = bool(body.get("is_active", True))
    is_customer = bool(body.get("is_customer", False))
    is_supplier = bool(body.get("is_supplier", False))

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400
    if not name:
        return jsonify({"error": "name is required"}), 400

    conn = get_conn()
    ensure_partner_connection_tables(conn)

    org = conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchone()

    if not org:
        conn.close()
        return jsonify({"error": "Organisation not found"}), 404

    partner_id = make_id("partner")

    conn.execute(
        """
        INSERT INTO partners (
            partner_id,
            organisation_id,
            name,
            is_active,
            is_customer,
            is_supplier,
            created_at,
            linked_org_id,
            connection_status,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            partner_id,
            organisation_id,
            name,
            1 if is_active else 0,
            1 if is_customer else 0,
            1 if is_supplier else 0,
            now_iso(),
            None,
            "LOCAL_ONLY",
            now_iso()
        )
    )

    audit_event(
        conn,
        entity_type="Partner",
        entity_id=partner_id,
        action="CREATE",
        summary=f"Created partner: {name}",
        organisation_id=organisation_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "partner_id": partner_id,
        "organisation_id": organisation_id,
        "name": name,
        "is_active": is_active,
        "is_customer": is_customer,
        "is_supplier": is_supplier,
        "linked_org_id": None,
        "connection_status": "LOCAL_ONLY"
    }), 201

@app.get("/partners")
def list_partners():
    organisation_id = request.args.get("organisation_id")
    is_active = request.args.get("is_active")
    role = request.args.get("role")
    connection_status = request.args.get("connection_status")

    conn = get_conn()
    ensure_partner_connection_tables(conn)

    sql = """
        SELECT
            p.partner_id,
            p.organisation_id,
            o.name AS organisation_name,
            p.name,
            p.is_active,
            p.is_customer,
            p.is_supplier,
            p.created_at,
            p.updated_at,
            p.linked_org_id,
            lo.name AS linked_org_name,
            COALESCE(p.connection_status, 'LOCAL_ONLY') AS connection_status
        FROM partners p
        LEFT JOIN organisations o ON o.organisation_id = p.organisation_id
        LEFT JOIN organisations lo ON lo.organisation_id = p.linked_org_id
        WHERE 1=1
    """
    params = []

    if organisation_id:
        sql += " AND p.organisation_id = ?"
        params.append(organisation_id)

    if is_active == "true":
        sql += " AND p.is_active = 1"
    elif is_active == "false":
        sql += " AND p.is_active = 0"

    if role == "customer":
        sql += " AND p.is_customer = 1"
    elif role == "supplier":
        sql += " AND p.is_supplier = 1"

    if connection_status:
        sql += " AND COALESCE(p.connection_status, 'LOCAL_ONLY') = ?"
        params.append(connection_status)

    sql += " ORDER BY p.is_active DESC, p.name"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    items = []
    for row in rows:
        d = dict(row)
        d["is_active"] = bool(d["is_active"])
        d["is_customer"] = bool(d["is_customer"])
        d["is_supplier"] = bool(d["is_supplier"])

        if d["is_customer"] and d["is_supplier"]:
            d["role_label"] = "Customer + Supplier"
        elif d["is_customer"]:
            d["role_label"] = "Customer"
        elif d["is_supplier"]:
            d["role_label"] = "Supplier"
        else:
            d["role_label"] = "Partner"

        if d["connection_status"] == "CONNECTED":
            d["connection_label"] = "Connected Pallet Pro Org"
        elif d["connection_status"] == "REQUESTED":
            d["connection_label"] = "Connection Requested"
        else:
            d["connection_label"] = "Local Partner Only"

        items.append(d)

    return jsonify({
        "count": len(items),
        "items": items
    }), 200

@app.get("/partners/<partner_id>")
def get_partner_profile(partner_id):
    conn = get_conn()
    ensure_partner_connection_tables(conn)
    ensure_partner_address_tables(conn)

    row = conn.execute(
        """
        SELECT
            p.partner_id,
            p.organisation_id,
            o.name AS organisation_name,
            p.name,
            p.is_active,
            p.is_customer,
            p.is_supplier,
            p.created_at,
            p.updated_at,
            p.linked_org_id,
            lo.name AS linked_org_name,
            COALESCE(p.connection_status, 'LOCAL_ONLY') AS connection_status
        FROM partners p
        LEFT JOIN organisations o ON o.organisation_id = p.organisation_id
        LEFT JOIN organisations lo ON lo.organisation_id = p.linked_org_id
        WHERE p.partner_id = ?
        """,
        (partner_id,)
    ).fetchone()

    address_rows = conn.execute(
        """
        SELECT
            partner_address_id,
            partner_id,
            organisation_id,
            label,
            category,
            custom_category_label,
            is_active,
            is_primary,
            is_default_dispatch_site,
            is_default_receiving_site,
            address_line_1,
            address_line_2,
            suburb,
            state,
            postcode,
            country,
            gate_number,
            door_number,
            entry_instructions,
            truck_access_notes,
            latitude,
            longitude,
            created_at,
            updated_at
        FROM partner_addresses
        WHERE partner_id = ?
        ORDER BY
            is_primary DESC,
            is_default_dispatch_site DESC,
            is_default_receiving_site DESC,
            label ASC,
            created_at DESC
        """,
        (partner_id,)
    ).fetchall()

    conn.close()

    if not row:
        return jsonify({"error": "Partner not found"}), 404

    d = dict(row)
    d["is_active"] = bool(d["is_active"])
    d["is_customer"] = bool(d["is_customer"])
    d["is_supplier"] = bool(d["is_supplier"])

    if d["is_customer"] and d["is_supplier"]:
        d["role_label"] = "Customer + Supplier"
    elif d["is_customer"]:
        d["role_label"] = "Customer"
    elif d["is_supplier"]:
        d["role_label"] = "Supplier"
    else:
        d["role_label"] = "Partner"

    if d["connection_status"] == "CONNECTED":
        d["connection_label"] = "Connected Pallet Pro Org"
    elif d["connection_status"] == "REQUESTED":
        d["connection_label"] = "Connection Requested"
    else:
        d["connection_label"] = "Local Partner Only"

    partner_addresses = []
    for addr in address_rows:
        a = dict(addr)
        a["is_active"] = bool(a["is_active"])
        a["is_primary"] = bool(a["is_primary"])
        a["is_default_dispatch_site"] = bool(a["is_default_dispatch_site"])
        a["is_default_receiving_site"] = bool(a["is_default_receiving_site"])
        nav_contract = build_partner_address_navigation_contract(a)
        a["navigation"] = nav_contract["navigation"]
        a["navigation_apps"] = nav_contract["navigation_apps"]
        partner_addresses.append(a)

    d["partner_address_count"] = len(partner_addresses)
    d["partner_addresses"] = partner_addresses
    d["primary_partner_address"] = next((a for a in partner_addresses if a["is_primary"]), None)
    d["default_dispatch_partner_address"] = next((a for a in partner_addresses if a["is_default_dispatch_site"]), None)
    d["default_receiving_partner_address"] = next((a for a in partner_addresses if a["is_default_receiving_site"]), None)

    return jsonify(d), 200


@app.patch("/partners/<partner_id>")
def update_partner(partner_id):
    body = request.get_json(silent=True) or {}

    conn = get_conn()
    ensure_partner_connection_tables(conn)

    partner = conn.execute(
        "SELECT * FROM partners WHERE partner_id = ?", (partner_id,)
    ).fetchone()

    if not partner:
        conn.close()
        return jsonify({"error": "Partner not found"}), 404

    new_name = (body.get("name") or "").strip() or partner["name"]
    new_is_customer = body.get("is_customer")
    new_is_supplier = body.get("is_supplier")

    if new_is_customer is None:
        new_is_customer = bool(partner["is_customer"])
    else:
        new_is_customer = bool(new_is_customer)

    if new_is_supplier is None:
        new_is_supplier = bool(partner["is_supplier"])
    else:
        new_is_supplier = bool(new_is_supplier)

    changes = []
    if new_name != partner["name"]:
        changes.append(f"name '{partner['name']}' → '{new_name}'")
    if new_is_customer != bool(partner["is_customer"]):
        changes.append(f"is_customer → {new_is_customer}")
    if new_is_supplier != bool(partner["is_supplier"]):
        changes.append(f"is_supplier → {new_is_supplier}")

    if not changes:
        conn.close()
        return jsonify({"message": "No changes made", "partner_id": partner_id}), 200

    ts = now_iso()
    conn.execute(
        """UPDATE partners SET name = ?, is_customer = ?, is_supplier = ?, updated_at = ?
           WHERE partner_id = ?""",
        (new_name, 1 if new_is_customer else 0, 1 if new_is_supplier else 0, ts, partner_id),
    )

    audit_event(
        conn,
        entity_type="Partner",
        entity_id=partner_id,
        action="UPDATE",
        summary=f"Partner updated: {'; '.join(changes)}.",
        organisation_id=partner["organisation_id"],
    )

    conn.commit()
    conn.close()

    return jsonify({
        "partner_id": partner_id,
        "organisation_id": partner["organisation_id"],
        "name": new_name,
        "is_customer": new_is_customer,
        "is_supplier": new_is_supplier,
        "updated_at": ts,
    }), 200


@app.post("/partners/<partner_id>/deactivate")
def deactivate_partner(partner_id):
    conn = get_conn()
    ensure_partner_connection_tables(conn)

    partner = conn.execute(
        "SELECT * FROM partners WHERE partner_id = ?", (partner_id,)
    ).fetchone()

    if not partner:
        conn.close()
        return jsonify({"error": "Partner not found"}), 404

    if not partner["is_active"]:
        conn.close()
        return jsonify({"error": "Partner is already inactive"}), 409

    ts = now_iso()
    conn.execute(
        "UPDATE partners SET is_active = 0, updated_at = ? WHERE partner_id = ?",
        (ts, partner_id),
    )

    audit_event(
        conn,
        entity_type="Partner",
        entity_id=partner_id,
        action="DEACTIVATE",
        summary=f"Partner '{partner['name']}' deactivated.",
        organisation_id=partner["organisation_id"],
    )

    conn.commit()
    conn.close()

    return jsonify({
        "partner_id": partner_id,
        "name": partner["name"],
        "is_active": False,
    }), 200


@app.post("/partners/<partner_id>/reactivate")
def reactivate_partner(partner_id):
    conn = get_conn()
    ensure_partner_connection_tables(conn)

    partner = conn.execute(
        "SELECT * FROM partners WHERE partner_id = ?", (partner_id,)
    ).fetchone()

    if not partner:
        conn.close()
        return jsonify({"error": "Partner not found"}), 404

    if partner["is_active"]:
        conn.close()
        return jsonify({"error": "Partner is already active"}), 409

    ts = now_iso()
    conn.execute(
        "UPDATE partners SET is_active = 1, updated_at = ? WHERE partner_id = ?",
        (ts, partner_id),
    )

    audit_event(
        conn,
        entity_type="Partner",
        entity_id=partner_id,
        action="REACTIVATE",
        summary=f"Partner '{partner['name']}' reactivated.",
        organisation_id=partner["organisation_id"],
    )

    conn.commit()
    conn.close()

    return jsonify({
        "partner_id": partner_id,
        "name": partner["name"],
        "is_active": True,
    }), 200


@app.post("/partners/<partner_id>/request-org-connection")
def request_org_connection(partner_id):
    conn = get_conn()
    ensure_partner_connection_tables(conn)

    partner = conn.execute(
        "SELECT * FROM partners WHERE partner_id = ?",
        (partner_id,)
    ).fetchone()

    if not partner:
        conn.close()
        return jsonify({"error": "Partner not found"}), 404

    matched_org = conn.execute(
        """
        SELECT organisation_id, name
        FROM organisations
        WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))
          AND organisation_id != ?
        LIMIT 1
        """,
        (partner["name"], partner["organisation_id"])
    ).fetchone()

    if not matched_org:
        conn.close()
        return jsonify({
            "error": "No matching Pallet Pro org found for this partner",
            "partner_id": partner_id,
            "partner_name": partner["name"]
        }), 404

    existing_connected = conn.execute(
        """
        SELECT partner_id
        FROM partners
        WHERE partner_id = ?
          AND linked_org_id = ?
          AND COALESCE(connection_status, 'LOCAL_ONLY') = 'CONNECTED'
        LIMIT 1
        """,
        (partner_id, matched_org["organisation_id"])
    ).fetchone()

    if existing_connected:
        conn.close()
        return jsonify({
            "error": "Partner is already connected to this Pallet Pro org",
            "partner_id": partner_id,
            "linked_org_id": matched_org["organisation_id"],
            "linked_org_name": matched_org["name"]
        }), 409

    existing_request = conn.execute(
        """
        SELECT connection_request_id, status
        FROM org_connection_requests
        WHERE requesting_partner_id = ?
          AND target_org_id = ?
          AND status IN ('PENDING_APPROVAL', 'CONNECTED')
        LIMIT 1
        """,
        (partner_id, matched_org["organisation_id"])
    ).fetchone()

    if existing_request:
        conn.close()
        return jsonify({
            "error": "Connection request already exists",
            "connection_request_id": existing_request["connection_request_id"],
            "status": existing_request["status"]
        }), 409

    connection_request_id = make_id("ocon")

    conn.execute(
        """
        INSERT INTO org_connection_requests (
            connection_request_id,
            requesting_org_id,
            requesting_partner_id,
            target_org_id,
            status,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            connection_request_id,
            partner["organisation_id"],
            partner_id,
            matched_org["organisation_id"],
            "PENDING_APPROVAL",
            now_iso(),
            now_iso()
        )
    )

    conn.execute(
        """
        UPDATE partners
        SET linked_org_id = ?, connection_status = ?, updated_at = ?
        WHERE partner_id = ?
        """,
        (matched_org["organisation_id"], "REQUESTED", now_iso(), partner_id)
    )

    audit_event(
        conn,
        entity_type="OrgConnectionRequest",
        entity_id=connection_request_id,
        action="CREATE",
        summary=f"Requested Pallet Pro org connection for partner {partner['name']}",
        organisation_id=partner["organisation_id"]
    )

    conn.commit()
    conn.close()

    return jsonify({
        "connection_request_id": connection_request_id,
        "partner_id": partner_id,
        "partner_name": partner["name"],
        "matched_org_id": matched_org["organisation_id"],
        "matched_org_name": matched_org["name"],
        "status": "PENDING_APPROVAL",
        "message": "Matched Pallet Pro partner found. Connection request created."
    }), 201


@app.get("/organisations/<organisation_id>/org-connection-requests")
def list_org_connection_requests(organisation_id):
    conn = get_conn()
    ensure_partner_connection_tables(conn)

    incoming_rows = conn.execute(
        """
        SELECT
            ocr.connection_request_id,
            ocr.requesting_org_id,
            ro.name AS requesting_org_name,
            ocr.requesting_partner_id,
            p.name AS partner_name,
            ocr.target_org_id,
            to2.name AS target_org_name,
            ocr.status,
            ocr.created_at,
            ocr.updated_at
        FROM org_connection_requests ocr
        LEFT JOIN organisations ro ON ro.organisation_id = ocr.requesting_org_id
        LEFT JOIN organisations to2 ON to2.organisation_id = ocr.target_org_id
        LEFT JOIN partners p ON p.partner_id = ocr.requesting_partner_id
        WHERE ocr.target_org_id = ?
        ORDER BY ocr.created_at DESC
        """,
        (organisation_id,)
    ).fetchall()

    outgoing_rows = conn.execute(
        """
        SELECT
            ocr.connection_request_id,
            ocr.requesting_org_id,
            ro.name AS requesting_org_name,
            ocr.requesting_partner_id,
            p.name AS partner_name,
            ocr.target_org_id,
            to2.name AS target_org_name,
            ocr.status,
            ocr.created_at,
            ocr.updated_at
        FROM org_connection_requests ocr
        LEFT JOIN organisations ro ON ro.organisation_id = ocr.requesting_org_id
        LEFT JOIN organisations to2 ON to2.organisation_id = ocr.target_org_id
        LEFT JOIN partners p ON p.partner_id = ocr.requesting_partner_id
        WHERE ocr.requesting_org_id = ?
        ORDER BY ocr.created_at DESC
        """,
        (organisation_id,)
    ).fetchall()

    conn.close()

    incoming = []
    for row in incoming_rows:
        d = dict(row)
        d["request_direction"] = "INCOMING"
        d["entry_target_section"] = "connection_requests"
        d["entry_target_entity_type"] = "OrgConnectionRequest"
        d["entry_target_action"] = "review_incoming_connection_request"
        d["highlight_key"] = d["connection_request_id"]
        incoming.append(d)

    outgoing = []
    for row in outgoing_rows:
        d = dict(row)
        d["request_direction"] = "OUTGOING"
        d["entry_target_section"] = "connection_requests"
        d["entry_target_entity_type"] = "OrgConnectionRequest"
        d["entry_target_action"] = "review_outgoing_connection_request"
        d["highlight_key"] = d["connection_request_id"]
        outgoing.append(d)

    return jsonify({
        "organisation_id": organisation_id,
        "incoming_count": len(incoming),
        "outgoing_count": len(outgoing),
        "incoming_requests": incoming,
        "outgoing_requests": outgoing
    }), 200


@app.post("/org-connection-requests/<connection_request_id>/approve")
def approve_org_connection_request(connection_request_id):
    conn = get_conn()
    ensure_partner_connection_tables(conn)

    req = conn.execute(
        "SELECT * FROM org_connection_requests WHERE connection_request_id = ?",
        (connection_request_id,)
    ).fetchone()

    if not req:
        conn.close()
        return jsonify({"error": "Connection request not found"}), 404

    if req["status"] != "PENDING_APPROVAL":
        conn.close()
        return jsonify({"error": "Connection request is not pending approval"}), 400

    requesting_org = conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?",
        (req["requesting_org_id"],)
    ).fetchone()

    target_org = conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?",
        (req["target_org_id"],)
    ).fetchone()

    requesting_partner = conn.execute(
        "SELECT * FROM partners WHERE partner_id = ?",
        (req["requesting_partner_id"],)
    ).fetchone()

    if not requesting_org or not target_org or not requesting_partner:
        conn.close()
        return jsonify({"error": "Connection request references missing records"}), 404

    conn.execute(
        """
        UPDATE org_connection_requests
        SET status = ?, updated_at = ?
        WHERE connection_request_id = ?
        """,
        ("CONNECTED", now_iso(), connection_request_id)
    )

    conn.execute(
        """
        UPDATE partners
        SET linked_org_id = ?, connection_status = ?, updated_at = ?
        WHERE partner_id = ?
        """,
        (req["target_org_id"], "CONNECTED", now_iso(), req["requesting_partner_id"])
    )

    reciprocal_partner = conn.execute(
        """
        SELECT *
        FROM partners
        WHERE organisation_id = ?
          AND (
                linked_org_id = ?
                OR LOWER(TRIM(name)) = LOWER(TRIM(?))
              )
        ORDER BY CASE WHEN linked_org_id = ? THEN 0 ELSE 1 END, created_at DESC
        LIMIT 1
        """,
        (req["target_org_id"], req["requesting_org_id"], requesting_org["name"], req["requesting_org_id"])
    ).fetchone()

    if reciprocal_partner:
        conn.execute(
            """
            UPDATE partners
            SET linked_org_id = ?,
                connection_status = ?,
                is_active = 1,
                updated_at = ?
            WHERE partner_id = ?
            """,
            (req["requesting_org_id"], "CONNECTED", now_iso(), reciprocal_partner["partner_id"])
        )
        reciprocal_partner_id = reciprocal_partner["partner_id"]
    else:
        reciprocal_partner_id = make_id("partner")
        conn.execute(
            """
            INSERT INTO partners (
                partner_id,
                organisation_id,
                name,
                is_active,
                is_customer,
                is_supplier,
                created_at,
                linked_org_id,
                connection_status,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reciprocal_partner_id,
                req["target_org_id"],
                requesting_org["name"],
                1,
                1,
                1,
                now_iso(),
                req["requesting_org_id"],
                "CONNECTED",
                now_iso()
            )
        )

    audit_event(
        conn,
        entity_type="OrgConnectionRequest",
        entity_id=connection_request_id,
        action="APPROVE",
        summary=f"Approved Pallet Pro org connection between {requesting_org['name']} and {target_org['name']}",
        organisation_id=req["target_org_id"]
    )

    audit_event(
        conn,
        entity_type="Partner",
        entity_id=req["requesting_partner_id"],
        action="CONNECT",
        summary=f"Partner connected to Pallet Pro org {target_org['name']}",
        organisation_id=req["requesting_org_id"]
    )

    audit_event(
        conn,
        entity_type="Partner",
        entity_id=reciprocal_partner_id,
        action="CONNECT",
        summary=f"Partner connected to Pallet Pro org {requesting_org['name']}",
        organisation_id=req["target_org_id"]
    )

    conn.commit()
    conn.close()

    return jsonify({
        "connection_request_id": connection_request_id,
        "status": "CONNECTED",
        "requesting_org_id": req["requesting_org_id"],
        "target_org_id": req["target_org_id"],
        "requesting_partner_id": req["requesting_partner_id"],
        "reciprocal_partner_id": reciprocal_partner_id
    }), 200


@app.post("/org-connection-requests/<connection_request_id>/reject")
def reject_org_connection_request(connection_request_id):
    conn = get_conn()
    ensure_partner_connection_tables(conn)

    req = conn.execute(
        "SELECT * FROM org_connection_requests WHERE connection_request_id = ?",
        (connection_request_id,)
    ).fetchone()

    if not req:
        conn.close()
        return jsonify({"error": "Connection request not found"}), 404

    if req["status"] != "PENDING_APPROVAL":
        conn.close()
        return jsonify({"error": "Connection request is not pending approval"}), 400

    conn.execute(
        """
        UPDATE org_connection_requests
        SET status = ?, updated_at = ?
        WHERE connection_request_id = ?
        """,
        ("REJECTED", now_iso(), connection_request_id)
    )

    conn.execute(
        """
        UPDATE partners
        SET linked_org_id = NULL,
            connection_status = 'LOCAL_ONLY',
            updated_at = ?
        WHERE partner_id = ?
          AND linked_org_id = ?
          AND COALESCE(connection_status, 'LOCAL_ONLY') = 'REQUESTED'
        """,
        (now_iso(), req["requesting_partner_id"], req["target_org_id"])
    )

    audit_event(
        conn,
        entity_type="OrgConnectionRequest",
        entity_id=connection_request_id,
        action="REJECT",
        summary="Rejected Pallet Pro org connection request",
        organisation_id=req["target_org_id"]
    )

    conn.commit()
    conn.close()

    return jsonify({
        "connection_request_id": connection_request_id,
        "status": "REJECTED"
    }), 200





@app.get("/organisations/<organisation_id>/shared-transaction-disputes")
def list_shared_transaction_disputes(organisation_id):
    conn = get_conn()
    ensure_shared_transaction_tables(conn)

    rows = conn.execute(
        """
        SELECT
            st.shared_transaction_id,
            st.origin_org_id,
            oo.name AS origin_org_name,
            st.counterparty_org_id,
            co.name AS counterparty_org_name,
            st.origin_partner_id,
            op.name AS origin_partner_name,
            st.counterparty_partner_id,
            cp.name AS counterparty_partner_name,
            st.origin_resource_id,
            st.resource_name,
            st.unit_type,
            st.quantity,
            st.proposed_quantity,
            st.reference_number,
            st.proposed_reference_number,
            st.shared_status,
            st.dispute_reason_code,
            st.dispute_reason_text,
            st.disputed_by_display_name,
            st.disputed_at,
            st.created_at,
            st.updated_at
        FROM shared_transactions st
        LEFT JOIN organisations oo ON oo.organisation_id = st.origin_org_id
        LEFT JOIN organisations co ON co.organisation_id = st.counterparty_org_id
        LEFT JOIN partners op ON op.partner_id = st.origin_partner_id
        LEFT JOIN partners cp ON cp.partner_id = st.counterparty_partner_id
        WHERE (st.origin_org_id = ? OR st.counterparty_org_id = ?)
          AND st.shared_status = 'DISPUTED'
        ORDER BY COALESCE(st.disputed_at, st.updated_at) DESC
        """,
        (organisation_id, organisation_id)
    ).fetchall()

    conn.close()

    items = []
    for row in rows:
        d = dict(row)
        if d["origin_org_id"] == organisation_id:
            d["perspective_role"] = "DISPATCHING"
            d["lane"] = "outgoing"
            d["counterparty_label"] = d["counterparty_org_name"]
        else:
            d["perspective_role"] = "RECEIVING"
            d["lane"] = "incoming"
            d["counterparty_label"] = d["origin_org_name"]

        d["next_action"] = "admin_resolve"
        d["entry_target_section"] = "shared_transaction_disputes"
        d["entry_target_entity_type"] = "SharedTransaction"
        d["entry_target_action"] = "open_dispute_review"
        d["highlight_key"] = d["shared_transaction_id"]
        items.append(d)

    return jsonify({
        "organisation_id": organisation_id,
        "count": len(items),
        "items": items
    }), 200


@app.post("/shared-transactions/<shared_transaction_id>/admin-resolve")
def admin_resolve_shared_transaction(shared_transaction_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    resolution_action = (body.get("resolution_action") or "").strip().upper()
    resolved_by_display_name = (body.get("resolved_by_display_name") or "Unknown Admin").strip()
    resolution_notes = (body.get("resolution_notes") or "").strip() or None

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400
    if resolution_action not in ("KEEP_ORIGINAL", "ACCEPT_PROPOSED_CORRECTION"):
        return jsonify({"error": "resolution_action must be KEEP_ORIGINAL or ACCEPT_PROPOSED_CORRECTION"}), 400

    conn = get_conn()
    ensure_shared_transaction_tables(conn)

    st = conn.execute(
        "SELECT * FROM shared_transactions WHERE shared_transaction_id = ?",
        (shared_transaction_id,)
    ).fetchone()

    if not st:
        conn.close()
        return jsonify({"error": "Shared transaction not found"}), 404

    if organisation_id not in (st["origin_org_id"], st["counterparty_org_id"]):
        conn.close()
        return jsonify({"error": "Organisation is not part of this shared transaction"}), 403

    if st["shared_status"] != "DISPUTED":
        conn.close()
        return jsonify({"error": "Shared transaction is not currently disputed"}), 400

    actor_org_role = "DISPATCHING" if organisation_id == st["origin_org_id"] else "RECEIVING"
    other_org_id = st["counterparty_org_id"] if organisation_id == st["origin_org_id"] else st["origin_org_id"]
    previous_status = st["shared_status"]

    final_quantity = st["quantity"]
    final_reference = st["reference_number"]

    if resolution_action == "ACCEPT_PROPOSED_CORRECTION":
        if st["proposed_quantity"] is not None:
            final_quantity = st["proposed_quantity"]
        if st["proposed_reference_number"] not in (None, ""):
            final_reference = st["proposed_reference_number"]
        resolution_code = "ADMIN_ACCEPTED_PROPOSED_CORRECTION"
        event_action = "ADMIN_RESOLVED_ACCEPT_PROPOSED_CORRECTION"
        summary = f"Org Admin resolved dispute by accepting proposed correction. Final quantity {final_quantity}, reference {final_reference}"
    else:
        resolution_code = "ADMIN_KEPT_ORIGINAL"
        event_action = "ADMIN_RESOLVED_KEEP_ORIGINAL"
        summary = f"Org Admin resolved dispute by keeping original values. Final quantity {final_quantity}, reference {final_reference}"

    if resolution_notes:
        summary = summary + f". Notes: {resolution_notes}"

    conn.execute(
        """
        UPDATE shared_transactions
        SET shared_status = ?,
            quantity = ?,
            reference_number = ?,
            confirmed_by_display_name = ?,
            confirmed_at = ?,
            proposed_quantity = NULL,
            proposed_reference_number = NULL,
            correction_reason_text = NULL,
            correction_proposed_by_display_name = NULL,
            correction_proposed_at = NULL,
            dispute_reason_code = NULL,
            dispute_reason_text = NULL,
            disputed_by_display_name = NULL,
            disputed_at = NULL,
            resolution_code = ?,
            resolution_notes = ?,
            resolved_by_display_name = ?,
            resolved_at = ?,
            updated_at = ?
        WHERE shared_transaction_id = ?
        """,
        (
            "CONFIRMED",
            final_quantity,
            final_reference,
            resolved_by_display_name,
            now_iso(),
            resolution_code,
            resolution_notes,
            resolved_by_display_name,
            now_iso(),
            now_iso(),
            shared_transaction_id
        )
    )

    record_shared_transaction_event(
        conn=conn,
        shared_transaction_id=shared_transaction_id,
        organisation_id=organisation_id,
        actor_org_role=actor_org_role,
        action=event_action,
        summary=summary,
        previous_status=previous_status,
        new_status="CONFIRMED",
        created_by_display_name=resolved_by_display_name
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="ADMIN_RESOLVED",
        summary=summary,
        organisation_id=organisation_id
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="ADMIN_RESOLVED",
        summary=summary,
        organisation_id=other_org_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "shared_transaction_id": shared_transaction_id,
        "shared_status": "CONFIRMED",
        "resolution_action": resolution_action,
        "resolution_code": resolution_code,
        "resolution_notes": resolution_notes,
        "resolved_by_display_name": resolved_by_display_name,
        "quantity": final_quantity,
        "reference_number": final_reference
    }), 200



@app.post("/shared-transactions/<shared_transaction_id>/generate-qr-token")
def generate_shared_transaction_qr_token(shared_transaction_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    purpose = (body.get("purpose") or "").strip().upper()
    created_by_display_name = (body.get("created_by_display_name") or "Unknown User").strip()
    expires_in_minutes = body.get("expires_in_minutes", 30)

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400
    if purpose not in ("INITIAL_CONFIRMATION", "CORRECTION_REVIEW"):
        return jsonify({"error": "purpose must be INITIAL_CONFIRMATION or CORRECTION_REVIEW"}), 400

    try:
        expires_in_minutes = int(expires_in_minutes)
    except Exception:
        return jsonify({"error": "expires_in_minutes must be an integer"}), 400

    if expires_in_minutes <= 0:
        return jsonify({"error": "expires_in_minutes must be greater than zero"}), 400

    conn = get_conn()
    ensure_shared_transaction_tables(conn)
    ensure_qr_token_tables(conn)
    ensure_partner_address_tables(conn)
    ensure_shared_transaction_partner_address_tables(conn)

    st = conn.execute(
        "SELECT * FROM shared_transactions WHERE shared_transaction_id = ?",
        (shared_transaction_id,)
    ).fetchone()

    if not st:
        conn.close()
        return jsonify({"error": "Shared transaction not found"}), 404

    address_payloads = get_shared_transaction_partner_address_payloads(conn, shared_transaction_id)

    if purpose == "INITIAL_CONFIRMATION":
        if organisation_id != st["origin_org_id"]:
            conn.close()
            return jsonify({"error": "Only the origin org can generate the initial confirmation QR"}), 403
        if st["shared_status"] != "AWAITING_COUNTERPARTY_CONFIRMATION":
            conn.close()
            return jsonify({"error": "Shared transaction is not awaiting counterparty confirmation"}), 400

        target_org_id = st["counterparty_org_id"]
        actor_org_role = "RECEIVING"
        screen_title = "Incoming Transaction"
        role_banner = "You are Receiving"
        next_expected_action = "confirm_or_dispute"
    else:
        if organisation_id != st["counterparty_org_id"]:
            conn.close()
            return jsonify({"error": "Only the counterparty org can generate the correction review QR"}), 403

        if st["shared_status"] != "CORRECTION_PROPOSED":
            conn.close()
            return jsonify({"error": "Shared transaction is not awaiting correction review"}), 400

        target_org_id = st["origin_org_id"]
        actor_org_role = "DISPATCHING"
        screen_title = "Correction Requested"
        role_banner = "You are Dispatching"
        next_expected_action = "accept_or_reject_correction"

    existing_active = conn.execute(
        """
        SELECT qr_token_id
        FROM qr_handoff_tokens
        WHERE shared_transaction_id = ?
          AND purpose = ?
          AND target_org_id = ?
          AND token_status IN ('ACTIVE', 'SCANNED')
        ORDER BY issued_at DESC
        LIMIT 1
        """,
        (shared_transaction_id, purpose, target_org_id)
    ).fetchone()

    if existing_active:
        conn.close()
        return jsonify({
            "error": "An active QR token already exists for this transaction and purpose",
            "qr_token_id": existing_active["qr_token_id"]
        }), 409

    qr_token_id = make_id("qrtok")
    issued_at = now_iso()

    import datetime as _dt
    issued_dt = _dt.datetime.fromisoformat(issued_at)
    expires_at = (issued_dt + _dt.timedelta(minutes=expires_in_minutes)).isoformat()

    conn.execute(
        """
        INSERT INTO qr_handoff_tokens (
            qr_token_id,
            shared_transaction_id,
            purpose,
            issuing_org_id,
            target_org_id,
            actor_org_role,
            token_status,
            created_by_display_name,
            issued_at,
            expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            qr_token_id,
            shared_transaction_id,
            purpose,
            organisation_id,
            target_org_id,
            actor_org_role,
            "ACTIVE",
            created_by_display_name,
            issued_at,
            expires_at
        )
    )

    issuing_role = "DISPATCHING" if organisation_id == st["origin_org_id"] else "RECEIVING"

    record_shared_transaction_event(
        conn=conn,
        shared_transaction_id=shared_transaction_id,
        organisation_id=organisation_id,
        actor_org_role=issuing_role,
        action="QR_GENERATED",
        summary=f"QR generated for {purpose}",
        previous_status=st["shared_status"],
        new_status=st["shared_status"],
        created_by_display_name=created_by_display_name
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="QR_GENERATED",
        summary=f"QR generated for {purpose}",
        organisation_id=organisation_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "qr_token_id": qr_token_id,
        "shared_transaction_id": shared_transaction_id,
        "purpose": purpose,
        "token_status": "ACTIVE",
        "issuing_org_id": organisation_id,
        "target_org_id": target_org_id,
        "actor_org_role": actor_org_role,
        "screen_title": screen_title,
        "role_banner": role_banner,
        "next_expected_action": next_expected_action,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "qr_open_path": f"/qr-handoff/{qr_token_id}",
        "qr_scan_path": f"/qr-handoff/{qr_token_id}/scan",
        "origin_partner_address_id": address_payloads["origin_partner_address_id"],
        "counterparty_partner_address_id": address_payloads["counterparty_partner_address_id"],
        "origin_partner_address": address_payloads["origin_partner_address"],
        "counterparty_partner_address": address_payloads["counterparty_partner_address"],
    }), 201


@app.get("/qr-handoff/<qr_token_id>")
def get_qr_handoff_token(qr_token_id):
    conn = get_conn()
    ensure_shared_transaction_tables(conn)
    ensure_qr_token_tables(conn)
    ensure_partner_address_tables(conn)
    ensure_shared_transaction_partner_address_tables(conn)

    row = conn.execute(
        """
        SELECT
            q.qr_token_id,
            q.shared_transaction_id,
            q.purpose,
            q.issuing_org_id,
            q.target_org_id,
            q.actor_org_role,
            q.token_status,
            q.created_by_display_name,
            q.issued_at,
            q.expires_at,
            q.scanned_at,
            q.scanned_by_display_name,
            q.consumed_at,
            q.consumed_by_display_name,
            q.consumed_action,
            st.shared_status,
            st.origin_org_id,
            oo.name AS origin_org_name,
            st.counterparty_org_id,
            co.name AS counterparty_org_name,
            st.origin_partner_id,
            op.name AS origin_partner_name,
            st.counterparty_partner_id,
            cp.name AS counterparty_partner_name,
            st.resource_name,
            st.unit_type,
            st.quantity,
            st.proposed_quantity,
            st.reference_number,
            st.proposed_reference_number,
            st.correction_reason_text,
            st.correction_proposed_by_display_name,
            st.movement_type
        FROM qr_handoff_tokens q
        LEFT JOIN shared_transactions st ON st.shared_transaction_id = q.shared_transaction_id
        LEFT JOIN organisations oo ON oo.organisation_id = st.origin_org_id
        LEFT JOIN organisations co ON co.organisation_id = st.counterparty_org_id
        LEFT JOIN partners op ON op.partner_id = st.origin_partner_id
        LEFT JOIN partners cp ON cp.partner_id = st.counterparty_partner_id
        WHERE q.qr_token_id = ?
        """,
        (qr_token_id,)
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "QR handoff token not found"}), 404

    address_payloads = get_shared_transaction_partner_address_payloads(conn, row["shared_transaction_id"])
    conn.close()

    d = dict(row)

    import datetime as _dt
    expires = _dt.datetime.fromisoformat(d["expires_at"])
    current_dt = _dt.datetime.fromisoformat(now_iso())
    d["is_expired"] = current_dt > expires

    if d["purpose"] == "INITIAL_CONFIRMATION":
        d["screen_title"] = "Incoming Transaction"
        d["role_banner"] = "You are Receiving"
        d["next_expected_action"] = "confirm_or_dispute"
    else:
        d["screen_title"] = "Correction Requested"
        d["role_banner"] = "You are Dispatching"
        d["next_expected_action"] = "accept_or_reject_correction"

    d["origin_partner_address_id"] = address_payloads["origin_partner_address_id"]
    d["counterparty_partner_address_id"] = address_payloads["counterparty_partner_address_id"]
    d["origin_partner_address"] = address_payloads["origin_partner_address"]
    d["counterparty_partner_address"] = address_payloads["counterparty_partner_address"]

    return jsonify(d), 200


@app.post("/qr-handoff/<qr_token_id>/scan")
def scan_qr_handoff_token(qr_token_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    scanned_by_display_name = (body.get("scanned_by_display_name") or "Unknown User").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400

    conn = get_conn()
    ensure_shared_transaction_tables(conn)
    ensure_qr_token_tables(conn)
    ensure_partner_address_tables(conn)
    ensure_shared_transaction_partner_address_tables(conn)

    row = conn.execute(
        """
        SELECT
            q.qr_token_id,
            q.shared_transaction_id,
            q.purpose,
            q.issuing_org_id,
            q.target_org_id,
            q.actor_org_role,
            q.token_status,
            q.created_by_display_name,
            q.issued_at,
            q.expires_at,
            q.scanned_at,
            q.scanned_by_display_name,
            q.consumed_at,
            q.consumed_by_display_name,
            q.consumed_action,
            st.shared_status,
            st.origin_org_id,
            oo.name AS origin_org_name,
            st.counterparty_org_id,
            co.name AS counterparty_org_name,
            st.origin_partner_id,
            op.name AS origin_partner_name,
            st.counterparty_partner_id,
            cp.name AS counterparty_partner_name,
            st.resource_name,
            st.unit_type,
            st.quantity,
            st.proposed_quantity,
            st.reference_number,
            st.proposed_reference_number,
            st.correction_reason_text,
            st.correction_proposed_by_display_name,
            st.movement_type
        FROM qr_handoff_tokens q
        LEFT JOIN shared_transactions st ON st.shared_transaction_id = q.shared_transaction_id
        LEFT JOIN organisations oo ON oo.organisation_id = st.origin_org_id
        LEFT JOIN organisations co ON co.organisation_id = st.counterparty_org_id
        LEFT JOIN partners op ON op.partner_id = st.origin_partner_id
        LEFT JOIN partners cp ON cp.partner_id = st.counterparty_partner_id
        WHERE q.qr_token_id = ?
        """,
        (qr_token_id,)
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "QR handoff token not found"}), 404

    d = dict(row)

    if organisation_id != d["target_org_id"]:
        conn.close()
        return jsonify({"error": "This QR token is not intended for this organisation"}), 403

    if d["token_status"] == "CONSUMED":
        conn.close()
        return jsonify({"error": "QR token has already been consumed"}), 409

    if d["token_status"] == "EXPIRED":
        conn.close()
        return jsonify({"error": "QR token has expired"}), 410

    import datetime as _dt
    expires = _dt.datetime.fromisoformat(d["expires_at"])
    current_dt = _dt.datetime.fromisoformat(now_iso())

    if current_dt > expires:
        conn.execute(
            "UPDATE qr_handoff_tokens SET token_status = ? WHERE qr_token_id = ?",
            ("EXPIRED", qr_token_id)
        )
        conn.commit()
        conn.close()
        return jsonify({"error": "QR token has expired"}), 410

    if d["token_status"] == "ACTIVE":
        conn.execute(
            """
            UPDATE qr_handoff_tokens
            SET token_status = ?,
                scanned_at = ?,
                scanned_by_display_name = ?
            WHERE qr_token_id = ?
            """,
            ("SCANNED", now_iso(), scanned_by_display_name, qr_token_id)
        )

        record_shared_transaction_event(
            conn=conn,
            shared_transaction_id=d["shared_transaction_id"],
            organisation_id=organisation_id,
            actor_org_role=d["actor_org_role"],
            action="QR_SCANNED",
            summary=f"QR scanned for {d['purpose']}",
            previous_status=d["shared_status"],
            new_status=d["shared_status"],
            created_by_display_name=scanned_by_display_name
        )

        audit_event(
            conn,
            entity_type="SharedTransaction",
            entity_id=d["shared_transaction_id"],
            action="QR_SCANNED",
            summary=f"QR scanned for {d['purpose']}",
            organisation_id=organisation_id
        )

        conn.commit()

    address_payloads = get_shared_transaction_partner_address_payloads(conn, d["shared_transaction_id"])
    conn.close()

    if d["purpose"] == "INITIAL_CONFIRMATION":
        screen_title = "Incoming Transaction"
        role_banner = "You are Receiving"
        next_expected_action = "confirm_or_dispute"
    else:
        screen_title = "Correction Requested"
        role_banner = "You are Dispatching"
        next_expected_action = "accept_or_reject_correction"

    return jsonify({
        "qr_token_id": qr_token_id,
        "shared_transaction_id": d["shared_transaction_id"],
        "purpose": d["purpose"],
        "token_status": "SCANNED",
        "scanned_by_display_name": scanned_by_display_name,
        "screen_title": screen_title,
        "role_banner": role_banner,
        "next_expected_action": next_expected_action,
        "shared_status": d["shared_status"],
        "origin_org_id": d["origin_org_id"],
        "origin_org_name": d["origin_org_name"],
        "origin_partner_id": d["origin_partner_id"],
        "origin_partner_name": d["origin_partner_name"],
        "counterparty_org_id": d["counterparty_org_id"],
        "counterparty_org_name": d["counterparty_org_name"],
        "counterparty_partner_id": d["counterparty_partner_id"],
        "counterparty_partner_name": d["counterparty_partner_name"],
        "origin_partner_address_id": address_payloads["origin_partner_address_id"],
        "counterparty_partner_address_id": address_payloads["counterparty_partner_address_id"],
        "origin_partner_address": address_payloads["origin_partner_address"],
        "counterparty_partner_address": address_payloads["counterparty_partner_address"],
        "resource_name": d["resource_name"],
        "unit_type": d["unit_type"],
        "quantity": d["quantity"],
        "proposed_quantity": d["proposed_quantity"],
        "reference_number": d["reference_number"],
        "proposed_reference_number": d["proposed_reference_number"],
        "correction_reason_text": d["correction_reason_text"],
        "correction_proposed_by_display_name": d["correction_proposed_by_display_name"]
    }), 200





# === SHARED TRANSACTION CREATE ROUTE START ===
@app.post("/shared-transactions")
def create_shared_transaction():
    body = request.get_json(silent=True) or {}
    origin_org_id = body.get("origin_org_id")
    counterparty_org_id = body.get("counterparty_org_id")
    origin_partner_id = body.get("origin_partner_id")
    origin_partner_address_id = body.get("origin_partner_address_id")
    counterparty_partner_address_id = body.get("counterparty_partner_address_id")
    origin_resource_id = body.get("origin_resource_id")
    quantity = body.get("quantity")
    movement_type = (body.get("movement_type") or "DISPATCH").strip()
    reference_number = (body.get("reference_number") or "").strip() or None
    created_by_display_name = (body.get("created_by_display_name") or "Unknown User").strip()

    if not origin_org_id:
        return jsonify({"error": "origin_org_id is required"}), 400
    if not counterparty_org_id:
        return jsonify({"error": "counterparty_org_id is required"}), 400
    if not origin_partner_id:
        return jsonify({"error": "origin_partner_id is required"}), 400
    if not origin_resource_id:
        return jsonify({"error": "origin_resource_id is required"}), 400
    if quantity is None:
        return jsonify({"error": "quantity is required"}), 400

    try:
        quantity = int(quantity)
    except Exception:
        return jsonify({"error": "quantity must be an integer"}), 400

    if quantity <= 0:
        return jsonify({"error": "quantity must be greater than zero"}), 400

    if origin_org_id == counterparty_org_id:
        return jsonify({"error": "origin_org_id and counterparty_org_id must be different"}), 400

    conn = get_conn()
    ensure_shared_transaction_tables(conn)
    ensure_partner_address_tables(conn)
    ensure_shared_transaction_partner_address_tables(conn)

    origin_org = conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?",
        (origin_org_id,)
    ).fetchone()
    counterparty_org = conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?",
        (counterparty_org_id,)
    ).fetchone()

    if not origin_org or not counterparty_org:
        conn.close()
        return jsonify({"error": "Origin or counterparty org not found"}), 404

    origin_partner = conn.execute(
        """
        SELECT *
        FROM partners
        WHERE partner_id = ?
          AND organisation_id = ?
        """,
        (origin_partner_id, origin_org_id)
    ).fetchone()
    if not origin_partner:
        conn.close()
        return jsonify({"error": "Origin partner not found"}), 404

    if origin_partner["linked_org_id"] != counterparty_org_id or (origin_partner["connection_status"] or "LOCAL_ONLY") != "CONNECTED":
        conn.close()
        return jsonify({"error": "Origin partner is not connected to the target Pallet Pro org"}), 409

    counterparty_partner = conn.execute(
        """
        SELECT *
        FROM partners
        WHERE organisation_id = ?
          AND linked_org_id = ?
          AND COALESCE(connection_status, 'LOCAL_ONLY') = 'CONNECTED'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (counterparty_org_id, origin_org_id)
    ).fetchone()
    if not counterparty_partner:
        conn.close()
        return jsonify({"error": "Counterparty connected partner record not found"}), 409

    origin_resource = conn.execute(
        """
        SELECT *
        FROM resources
        WHERE resource_id = ?
          AND organisation_id = ?
        """,
        (origin_resource_id, origin_org_id)
    ).fetchone()
    if not origin_resource:
        conn.close()
        return jsonify({"error": "Origin resource not found"}), 404

    likely_duplicate = conn.execute(
        """
        SELECT shared_transaction_id, shared_status
        FROM shared_transactions
        WHERE origin_org_id = ?
          AND counterparty_org_id = ?
          AND origin_resource_id = ?
          AND quantity = ?
          AND movement_type = ?
          AND COALESCE(reference_number, '') = COALESCE(?, '')
          AND shared_status IN ('AWAITING_COUNTERPARTY_CONFIRMATION', 'CONFIRMED', 'DISPUTED')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (
            origin_org_id,
            counterparty_org_id,
            origin_resource_id,
            quantity,
            movement_type,
            reference_number,
        )
    ).fetchone()
    if likely_duplicate:
        conn.close()
        return jsonify({
            "error": "A likely matching shared transaction already exists",
            "shared_transaction_id": likely_duplicate["shared_transaction_id"],
            "status": likely_duplicate["shared_status"]
        }), 409

    if origin_partner_address_id:
        origin_partner_address = get_partner_address_for_shared_transaction(
            conn,
            origin_partner_address_id,
            origin_partner_id,
            origin_org_id,
        )
        if not origin_partner_address:
            conn.close()
            return jsonify({"error": "Origin partner address not found"}), 404
    else:
        origin_partner_address = get_default_partner_address_for_shared_transaction(
            conn,
            origin_partner_id,
            origin_org_id,
            "dispatch",
        )

    if counterparty_partner_address_id:
        counterparty_partner_address = get_partner_address_for_shared_transaction(
            conn,
            counterparty_partner_address_id,
            counterparty_partner["partner_id"],
            counterparty_org_id,
        )
        if not counterparty_partner_address:
            conn.close()
            return jsonify({"error": "Counterparty partner address not found"}), 404
    else:
        counterparty_partner_address = get_default_partner_address_for_shared_transaction(
            conn,
            counterparty_partner["partner_id"],
            counterparty_org_id,
            "receiving",
        )

    shared_transaction_id = make_id("stxn")
    ts = now_iso()

    conn.execute(
        """
        INSERT INTO shared_transactions (
            shared_transaction_id,
            origin_org_id,
            counterparty_org_id,
            origin_partner_id,
            counterparty_partner_id,
            origin_resource_id,
            resource_name,
            unit_type,
            quantity,
            movement_type,
            reference_number,
            shared_status,
            created_by_display_name,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            shared_transaction_id,
            origin_org_id,
            counterparty_org_id,
            origin_partner_id,
            counterparty_partner["partner_id"],
            origin_resource_id,
            origin_resource["name"],
            origin_resource["unit_type"],
            quantity,
            movement_type,
            reference_number,
            "AWAITING_COUNTERPARTY_CONFIRMATION",
            created_by_display_name,
            ts,
            ts,
        )
    )

    conn.execute(
        """
        INSERT INTO shared_transaction_partner_addresses (
            shared_transaction_id,
            origin_partner_address_id,
            counterparty_partner_address_id,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            shared_transaction_id,
            origin_partner_address["partner_address_id"] if origin_partner_address else None,
            counterparty_partner_address["partner_address_id"] if counterparty_partner_address else None,
            ts,
            ts,
        )
    )

    origin_site_suffix = f" from site {origin_partner_address['label']}" if origin_partner_address else ""
    counterparty_site_suffix = f" to site {counterparty_partner_address['label']}" if counterparty_partner_address else ""

    record_shared_transaction_event(
        conn=conn,
        shared_transaction_id=shared_transaction_id,
        organisation_id=origin_org_id,
        actor_org_role="DISPATCHING",
        action="CREATE",
        summary=f"Shared transaction created for {quantity} {origin_resource['name']} from {origin_org['name']}{origin_site_suffix} to {counterparty_org['name']}{counterparty_site_suffix}",
        previous_status=None,
        new_status="AWAITING_COUNTERPARTY_CONFIRMATION",
        created_by_display_name=created_by_display_name
    )

    record_shared_transaction_event(
        conn=conn,
        shared_transaction_id=shared_transaction_id,
        organisation_id=counterparty_org_id,
        actor_org_role="RECEIVING",
        action="INCOMING_PENDING",
        summary=f"Incoming shared transaction awaiting confirmation from {origin_org['name']}",
        previous_status=None,
        new_status="AWAITING_COUNTERPARTY_CONFIRMATION",
        created_by_display_name=created_by_display_name
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="CREATE",
        summary=f"Created shared transaction to {counterparty_org['name']} for {quantity} {origin_resource['name']}",
        organisation_id=origin_org_id
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="INCOMING_PENDING",
        summary=f"Incoming shared transaction from {origin_org['name']} awaiting confirmation",
        organisation_id=counterparty_org_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "shared_transaction_id": shared_transaction_id,
        "shared_status": "AWAITING_COUNTERPARTY_CONFIRMATION",
        "origin_org_id": origin_org_id,
        "origin_org_name": origin_org["name"],
        "counterparty_org_id": counterparty_org_id,
        "counterparty_org_name": counterparty_org["name"],
        "origin_partner_id": origin_partner_id,
        "counterparty_partner_id": counterparty_partner["partner_id"],
        "origin_partner_address_id": origin_partner_address["partner_address_id"] if origin_partner_address else None,
        "counterparty_partner_address_id": counterparty_partner_address["partner_address_id"] if counterparty_partner_address else None,
        "origin_partner_address": origin_partner_address,
        "counterparty_partner_address": counterparty_partner_address,
        "origin_resource_id": origin_resource_id,
        "resource_name": origin_resource["name"],
        "unit_type": origin_resource["unit_type"],
        "quantity": quantity,
        "movement_type": movement_type,
        "reference_number": reference_number,
        "created_by_display_name": created_by_display_name
    }), 201
# === SHARED TRANSACTION CREATE ROUTE END ===


def get_shared_transaction_partner_address_payloads(conn, shared_transaction_id):
    ensure_partner_address_tables(conn)
    ensure_shared_transaction_partner_address_tables(conn)

    payload = {
        "origin_partner_address_id": None,
        "counterparty_partner_address_id": None,
        "origin_partner_address": None,
        "counterparty_partner_address": None,
    }

    link = conn.execute(
        """
        SELECT
            origin_partner_address_id,
            counterparty_partner_address_id
        FROM shared_transaction_partner_addresses
        WHERE shared_transaction_id = ?
        """,
        (shared_transaction_id,)
    ).fetchone()

    if not link:
        return payload

    payload["origin_partner_address_id"] = link["origin_partner_address_id"]
    payload["counterparty_partner_address_id"] = link["counterparty_partner_address_id"]

    st = conn.execute(
        """
        SELECT
            origin_partner_id,
            counterparty_partner_id,
            origin_org_id,
            counterparty_org_id
        FROM shared_transactions
        WHERE shared_transaction_id = ?
        """,
        (shared_transaction_id,)
    ).fetchone()

    if not st:
        return payload

    if payload["origin_partner_address_id"]:
        payload["origin_partner_address"] = get_partner_address_for_shared_transaction(
            conn,
            payload["origin_partner_address_id"],
            st["origin_partner_id"],
            st["origin_org_id"],
        )

    if payload["counterparty_partner_address_id"]:
        payload["counterparty_partner_address"] = get_partner_address_for_shared_transaction(
            conn,
            payload["counterparty_partner_address_id"],
            st["counterparty_partner_id"],
            st["counterparty_org_id"],
        )

    return payload

# === SHARED TRANSACTION ADDRESS HELPERS START ===
def get_partner_address_for_shared_transaction(conn, partner_address_id, partner_id, organisation_id):
    ensure_partner_address_tables(conn)

    row = conn.execute(
        """
        SELECT
            pa.partner_address_id,
            pa.partner_id,
            p.name AS partner_name,
            pa.organisation_id,
            pa.label,
            pa.category,
            pa.custom_category_label,
            pa.address_line_1,
            pa.address_line_2,
            pa.suburb,
            pa.state,
            pa.postcode,
            pa.country,
            pa.gate_number,
            pa.door_number,
            pa.entry_instructions,
            pa.truck_access_notes,
            pa.latitude,
            pa.longitude,
            pa.is_primary,
            pa.is_default_dispatch_site,
            pa.is_default_receiving_site,
            pa.is_active,
            pa.created_at,
            pa.updated_at
        FROM partner_addresses pa
        LEFT JOIN partners p ON p.partner_id = pa.partner_id
        WHERE pa.partner_address_id = ?
          AND pa.partner_id = ?
          AND pa.organisation_id = ?
        LIMIT 1
        """,
        (partner_address_id, partner_id, organisation_id),
    ).fetchone()

    if not row:
        return None

    d = dict(row)
    d["is_primary"] = bool(d["is_primary"])
    d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
    d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
    d["is_active"] = bool(d["is_active"])

    nav = build_partner_address_navigation_contract(d)
    d["navigation"] = nav["navigation"]
    d["navigation_apps"] = nav["navigation_apps"]
    return d


def get_default_partner_address_for_shared_transaction(conn, partner_id, organisation_id, mode):
    ensure_partner_address_tables(conn)

    mode = (mode or "").strip().upper()
    default_flag = "is_default_dispatch_site" if mode == "DISPATCH" else "is_default_receiving_site"

    row = conn.execute(
        f"""
        SELECT partner_address_id
        FROM partner_addresses
        WHERE partner_id = ?
          AND organisation_id = ?
          AND is_active = 1
          AND {default_flag} = 1
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (partner_id, organisation_id),
    ).fetchone()

    if row:
        return get_partner_address_for_shared_transaction(
            conn, row["partner_address_id"], partner_id, organisation_id
        )

    row = conn.execute(
        """
        SELECT partner_address_id
        FROM partner_addresses
        WHERE partner_id = ?
          AND organisation_id = ?
          AND is_active = 1
          AND is_primary = 1
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (partner_id, organisation_id),
    ).fetchone()

    if row:
        return get_partner_address_for_shared_transaction(
            conn, row["partner_address_id"], partner_id, organisation_id
        )

    row = conn.execute(
        """
        SELECT partner_address_id
        FROM partner_addresses
        WHERE partner_id = ?
          AND organisation_id = ?
          AND is_active = 1
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (partner_id, organisation_id),
    ).fetchone()

    if row:
        return get_partner_address_for_shared_transaction(
            conn, row["partner_address_id"], partner_id, organisation_id
        )

    return None


def get_shared_transaction_partner_address_payloads(conn, shared_transaction_id):
    ensure_partner_address_tables(conn)
    ensure_shared_transaction_partner_address_tables(conn)

    payload = {
        "origin_partner_address_id": None,
        "counterparty_partner_address_id": None,
        "origin_partner_address": None,
        "counterparty_partner_address": None,
    }

    st = conn.execute(
        """
        SELECT
            shared_transaction_id,
            origin_partner_id,
            counterparty_partner_id,
            origin_org_id,
            counterparty_org_id,
            movement_type
        FROM shared_transactions
        WHERE shared_transaction_id = ?
        """,
        (shared_transaction_id,),
    ).fetchone()

    if not st:
        return payload

    link = conn.execute(
        """
        SELECT
            origin_partner_address_id,
            counterparty_partner_address_id
        FROM shared_transaction_partner_addresses
        WHERE shared_transaction_id = ?
        LIMIT 1
        """,
        (shared_transaction_id,),
    ).fetchone()

    if link and link["origin_partner_address_id"]:
        payload["origin_partner_address"] = get_partner_address_for_shared_transaction(
            conn,
            link["origin_partner_address_id"],
            st["origin_partner_id"],
            st["origin_org_id"],
        )
    else:
        payload["origin_partner_address"] = get_default_partner_address_for_shared_transaction(
            conn,
            st["origin_partner_id"],
            st["origin_org_id"],
            "DISPATCH",
        )

    if link and link["counterparty_partner_address_id"]:
        payload["counterparty_partner_address"] = get_partner_address_for_shared_transaction(
            conn,
            link["counterparty_partner_address_id"],
            st["counterparty_partner_id"],
            st["counterparty_org_id"],
        )
    else:
        payload["counterparty_partner_address"] = get_default_partner_address_for_shared_transaction(
            conn,
            st["counterparty_partner_id"],
            st["counterparty_org_id"],
            "RECEIVING",
        )

    payload["origin_partner_address_id"] = (
        payload["origin_partner_address"]["partner_address_id"]
        if payload["origin_partner_address"]
        else (link["origin_partner_address_id"] if link else None)
    )

    payload["counterparty_partner_address_id"] = (
        payload["counterparty_partner_address"]["partner_address_id"]
        if payload["counterparty_partner_address"]
        else (link["counterparty_partner_address_id"] if link else None)
    )

    return payload
# === SHARED TRANSACTION ADDRESS HELPERS END ===

@app.get("/organisations/<organisation_id>/shared-transactions")
def list_shared_transactions(organisation_id):
    lane = (request.args.get("lane") or "all").strip().lower()
    status = request.args.get("status")

    conn = get_conn()
    ensure_shared_transaction_tables(conn)
    ensure_partner_address_tables(conn)
    ensure_shared_transaction_partner_address_tables(conn)

    sql = """
        SELECT
            st.shared_transaction_id,
            st.origin_org_id,
            oo.name AS origin_org_name,
            st.counterparty_org_id,
            co.name AS counterparty_org_name,
            st.origin_partner_id,
            op.name AS origin_partner_name,
            st.counterparty_partner_id,
            cp.name AS counterparty_partner_name,
            st.origin_resource_id,
            st.resource_name,
            st.unit_type,
            st.quantity,
            st.proposed_quantity,
            st.reference_number,
            st.proposed_reference_number,
            st.correction_reason_text,
            st.correction_proposed_by_display_name,
            st.correction_proposed_at,
            st.movement_type,
            st.shared_status,
            st.created_by_display_name,
            st.confirmed_by_display_name,
            st.disputed_by_display_name,
            st.confirmed_at,
            st.disputed_at,
            st.dispute_reason_code,
            st.dispute_reason_text,
            st.resolution_code,
            st.resolution_notes,
            st.resolved_by_display_name,
            st.resolved_at,
            st.created_at,
            st.updated_at
        FROM shared_transactions st
        LEFT JOIN organisations oo ON oo.organisation_id = st.origin_org_id
        LEFT JOIN organisations co ON co.organisation_id = st.counterparty_org_id
        LEFT JOIN partners op ON op.partner_id = st.origin_partner_id
        LEFT JOIN partners cp ON cp.partner_id = st.counterparty_partner_id
        WHERE (st.origin_org_id = ? OR st.counterparty_org_id = ?)
    """
    params = [organisation_id, organisation_id]

    if lane == "outgoing":
        sql += " AND st.origin_org_id = ?"
        params.append(organisation_id)
    elif lane == "incoming":
        sql += " AND st.counterparty_org_id = ?"
        params.append(organisation_id)

    if status:
        sql += " AND st.shared_status = ?"
        params.append(status)

    sql += " ORDER BY st.created_at DESC"

    rows = conn.execute(sql, params).fetchall()

    items = []
    for row in rows:
        d = dict(row)

        address_payloads = get_shared_transaction_partner_address_payloads(conn, d["shared_transaction_id"])
        d["origin_partner_address_id"] = address_payloads["origin_partner_address_id"]
        d["counterparty_partner_address_id"] = address_payloads["counterparty_partner_address_id"]
        d["origin_partner_address"] = address_payloads["origin_partner_address"]
        d["counterparty_partner_address"] = address_payloads["counterparty_partner_address"]

        if d["origin_org_id"] == organisation_id:
            d["perspective_role"] = "DISPATCHING"
            d["lane"] = "outgoing"
            d["counterparty_label"] = d["counterparty_org_name"]
        else:
            d["perspective_role"] = "RECEIVING"
            d["lane"] = "incoming"
            d["counterparty_label"] = d["origin_org_name"]

        if d["shared_status"] == "AWAITING_COUNTERPARTY_CONFIRMATION" and d["perspective_role"] == "RECEIVING":
            d["next_action"] = "confirm_or_dispute"
        elif d["shared_status"] == "CORRECTION_PROPOSED" and d["perspective_role"] == "DISPATCHING":
            d["next_action"] = "accept_or_reject_correction"
        elif d["shared_status"] == "CORRECTION_PROPOSED" and d["perspective_role"] == "RECEIVING":
            d["next_action"] = "awaiting_origin_review"
        elif d["shared_status"] == "DISPUTED":
            d["next_action"] = "admin_review"
        else:
            d["next_action"] = "view_only"

        d["entry_target_section"] = "shared_transactions"
        d["entry_target_entity_type"] = "SharedTransaction"
        d["entry_target_action"] = "open_shared_transaction"
        d["highlight_key"] = d["shared_transaction_id"]
        items.append(d)

    conn.close()

    return jsonify({
        "organisation_id": organisation_id,
        "count": len(items),
        "items": items
    }), 200


@app.get("/shared-transactions/<shared_transaction_id>")
def get_shared_transaction(shared_transaction_id):
    organisation_id = request.args.get("organisation_id")

    conn = get_conn()
    ensure_shared_transaction_tables(conn)
    ensure_partner_address_tables(conn)
    ensure_shared_transaction_partner_address_tables(conn)

    row = conn.execute(
        """
        SELECT
            st.shared_transaction_id,
            st.origin_org_id,
            oo.name AS origin_org_name,
            st.counterparty_org_id,
            co.name AS counterparty_org_name,
            st.origin_partner_id,
            op.name AS origin_partner_name,
            st.counterparty_partner_id,
            cp.name AS counterparty_partner_name,
            st.origin_resource_id,
            st.resource_name,
            st.unit_type,
            st.quantity,
            st.proposed_quantity,
            st.reference_number,
            st.proposed_reference_number,
            st.correction_reason_text,
            st.correction_proposed_by_display_name,
            st.correction_proposed_at,
            st.movement_type,
            st.shared_status,
            st.created_by_display_name,
            st.confirmed_by_display_name,
            st.disputed_by_display_name,
            st.confirmed_at,
            st.disputed_at,
            st.dispute_reason_code,
            st.dispute_reason_text,
            st.resolution_code,
            st.resolution_notes,
            st.resolved_by_display_name,
            st.resolved_at,
            st.created_at,
            st.updated_at
        FROM shared_transactions st
        LEFT JOIN organisations oo ON oo.organisation_id = st.origin_org_id
        LEFT JOIN organisations co ON co.organisation_id = st.counterparty_org_id
        LEFT JOIN partners op ON op.partner_id = st.origin_partner_id
        LEFT JOIN partners cp ON cp.partner_id = st.counterparty_partner_id
        WHERE st.shared_transaction_id = ?
        """,
        (shared_transaction_id,)
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "Shared transaction not found"}), 404

    events = conn.execute(
        """
        SELECT
            shared_transaction_event_id,
            organisation_id,
            actor_org_role,
            action,
            previous_status,
            new_status,
            summary,
            created_by_display_name,
            created_at
        FROM shared_transaction_events
        WHERE shared_transaction_id = ?
        ORDER BY created_at ASC
        """,
        (shared_transaction_id,)
    ).fetchall()

    address_payloads = get_shared_transaction_partner_address_payloads(conn, shared_transaction_id)
    conn.close()

    d = dict(row)

    if organisation_id:
        if organisation_id == d["origin_org_id"]:
            d["perspective_role"] = "DISPATCHING"
        elif organisation_id == d["counterparty_org_id"]:
            d["perspective_role"] = "RECEIVING"

    d["origin_partner_address_id"] = address_payloads["origin_partner_address_id"]
    d["counterparty_partner_address_id"] = address_payloads["counterparty_partner_address_id"]
    d["origin_partner_address"] = address_payloads["origin_partner_address"]
    d["counterparty_partner_address"] = address_payloads["counterparty_partner_address"]
    d["events"] = [dict(r) for r in events]

    return jsonify(d), 200


@app.post("/shared-transactions/<shared_transaction_id>/propose-correction")
def propose_shared_transaction_correction(shared_transaction_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    qr_token_id = body.get("qr_token_id")
    proposed_quantity = body.get("proposed_quantity")
    proposed_reference_number = body.get("proposed_reference_number")
    correction_reason_text = (body.get("correction_reason_text") or "Correction proposed by receiving user").strip()
    correction_proposed_by_display_name = (body.get("correction_proposed_by_display_name") or "Unknown User").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400

    conn = get_conn()
    ensure_shared_transaction_tables(conn)
    ensure_qr_token_tables(conn)

    st = conn.execute(
        "SELECT * FROM shared_transactions WHERE shared_transaction_id = ?",
        (shared_transaction_id,)
    ).fetchone()

    if not st:
        conn.close()
        return jsonify({"error": "Shared transaction not found"}), 404

    if organisation_id != st["counterparty_org_id"]:
        conn.close()
        return jsonify({"error": "Only the counterparty org can propose a correction at this stage"}), 403

    if st["shared_status"] != "AWAITING_COUNTERPARTY_CONFIRMATION":
        conn.close()
        return jsonify({"error": "Shared transaction is not awaiting counterparty confirmation"}), 400

    _, qr_error = validate_qr_handoff_token_for_action(
        conn,
        qr_token_id,
        shared_transaction_id,
        organisation_id,
        "INITIAL_CONFIRMATION"
    )
    if qr_error:
        conn.close()
        return jsonify(qr_error[0]), qr_error[1]

    final_proposed_quantity = st["quantity"]
    if proposed_quantity is not None:
        try:
            final_proposed_quantity = int(proposed_quantity)
        except Exception:
            conn.close()
            return jsonify({"error": "proposed_quantity must be an integer"}), 400
        if final_proposed_quantity <= 0:
            conn.close()
            return jsonify({"error": "proposed_quantity must be greater than zero"}), 400

    final_proposed_reference = proposed_reference_number if proposed_reference_number not in (None, "") else st["reference_number"]

    if final_proposed_quantity == st["quantity"] and final_proposed_reference == st["reference_number"]:
        conn.close()
        return jsonify({"error": "No correction changes were proposed"}), 400

    previous_status = st["shared_status"]

    conn.execute(
        """
        UPDATE shared_transactions
        SET shared_status = ?,
            proposed_quantity = ?,
            proposed_reference_number = ?,
            correction_reason_text = ?,
            correction_proposed_by_display_name = ?,
            correction_proposed_at = ?,
            updated_at = ?
        WHERE shared_transaction_id = ?
        """,
        (
            "CORRECTION_PROPOSED",
            final_proposed_quantity,
            final_proposed_reference,
            correction_reason_text,
            correction_proposed_by_display_name,
            now_iso(),
            now_iso(),
            shared_transaction_id
        )
    )

    consume_qr_handoff_token(
        conn,
        qr_token_id,
        correction_proposed_by_display_name,
        "CORRECTION_PROPOSED"
    )

    summary_bits = []
    if final_proposed_quantity != st["quantity"]:
        summary_bits.append(f"quantity {st['quantity']} -> {final_proposed_quantity}")
    if final_proposed_reference != st["reference_number"]:
        summary_bits.append(f"reference {st['reference_number']} -> {final_proposed_reference}")

    record_shared_transaction_event(
        conn=conn,
        shared_transaction_id=shared_transaction_id,
        organisation_id=organisation_id,
        actor_org_role="RECEIVING",
        action="CORRECTION_PROPOSED",
        summary="Correction proposed: " + ", ".join(summary_bits),
        previous_status=previous_status,
        new_status="CORRECTION_PROPOSED",
        created_by_display_name=correction_proposed_by_display_name
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="CORRECTION_PROPOSED",
        summary="Receiving org proposed a correction to shared transaction",
        organisation_id=st["counterparty_org_id"]
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="COUNTERPARTY_CORRECTION_PROPOSED",
        summary="Counterparty proposed a correction to shared transaction",
        organisation_id=st["origin_org_id"]
    )

    conn.commit()
    conn.close()

    return jsonify({
        "shared_transaction_id": shared_transaction_id,
        "shared_status": "CORRECTION_PROPOSED",
        "proposed_quantity": final_proposed_quantity,
        "proposed_reference_number": final_proposed_reference,
        "correction_reason_text": correction_reason_text,
        "correction_proposed_by_display_name": correction_proposed_by_display_name,
        "qr_token_id": qr_token_id
    }), 200


@app.post("/shared-transactions/<shared_transaction_id>/accept-correction")
def accept_shared_transaction_correction(shared_transaction_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    qr_token_id = body.get("qr_token_id")
    accepted_by_display_name = (body.get("accepted_by_display_name") or "Unknown User").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400

    conn = get_conn()
    ensure_shared_transaction_tables(conn)
    ensure_qr_token_tables(conn)

    st = conn.execute(
        "SELECT * FROM shared_transactions WHERE shared_transaction_id = ?",
        (shared_transaction_id,)
    ).fetchone()

    if not st:
        conn.close()
        return jsonify({"error": "Shared transaction not found"}), 404

    if organisation_id != st["origin_org_id"]:
        conn.close()
        return jsonify({"error": "Only the origin org can accept a correction at this stage"}), 403

    if st["shared_status"] != "CORRECTION_PROPOSED":
        conn.close()
        return jsonify({"error": "Shared transaction is not awaiting correction review"}), 400

    _, qr_error = validate_qr_handoff_token_for_action(
        conn,
        qr_token_id,
        shared_transaction_id,
        organisation_id,
        "CORRECTION_REVIEW"
    )
    if qr_error:
        conn.close()
        return jsonify(qr_error[0]), qr_error[1]

    final_quantity = st["proposed_quantity"] if st["proposed_quantity"] is not None else st["quantity"]
    final_reference = st["proposed_reference_number"] if st["proposed_reference_number"] not in (None, "") else st["reference_number"]
    previous_status = st["shared_status"]

    conn.execute(
        """
        UPDATE shared_transactions
        SET shared_status = ?,
            quantity = ?,
            reference_number = ?,
            confirmed_by_display_name = ?,
            confirmed_at = ?,
            proposed_quantity = NULL,
            proposed_reference_number = NULL,
            correction_reason_text = NULL,
            correction_proposed_by_display_name = NULL,
            correction_proposed_at = NULL,
            updated_at = ?
        WHERE shared_transaction_id = ?
        """,
        (
            "CONFIRMED",
            final_quantity,
            final_reference,
            accepted_by_display_name,
            now_iso(),
            now_iso(),
            shared_transaction_id
        )
    )

    consume_qr_handoff_token(
        conn,
        qr_token_id,
        accepted_by_display_name,
        "CORRECTION_ACCEPTED"
    )

    record_shared_transaction_event(
        conn=conn,
        shared_transaction_id=shared_transaction_id,
        organisation_id=organisation_id,
        actor_org_role="DISPATCHING",
        action="CORRECTION_ACCEPTED",
        summary=f"Correction accepted. Final quantity {final_quantity}, reference {final_reference}",
        previous_status=previous_status,
        new_status="CONFIRMED",
        created_by_display_name=accepted_by_display_name
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="CORRECTION_ACCEPTED",
        summary="Origin org accepted correction and confirmed shared transaction",
        organisation_id=st["origin_org_id"]
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="CORRECTION_ACCEPTED",
        summary="Origin org accepted correction and confirmed shared transaction",
        organisation_id=st["counterparty_org_id"]
    )

    conn.commit()
    conn.close()

    return jsonify({
        "shared_transaction_id": shared_transaction_id,
        "shared_status": "CONFIRMED",
        "quantity": final_quantity,
        "reference_number": final_reference,
        "accepted_by_display_name": accepted_by_display_name,
        "qr_token_id": qr_token_id
    }), 200


@app.post("/shared-transactions/<shared_transaction_id>/reject-correction")
def reject_shared_transaction_correction(shared_transaction_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    qr_token_id = body.get("qr_token_id")
    rejected_by_display_name = (body.get("rejected_by_display_name") or "Unknown User").strip()
    rejection_reason_text = (body.get("rejection_reason_text") or "Origin org rejected correction proposal").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400

    conn = get_conn()
    ensure_shared_transaction_tables(conn)
    ensure_qr_token_tables(conn)

    st = conn.execute(
        "SELECT * FROM shared_transactions WHERE shared_transaction_id = ?",
        (shared_transaction_id,)
    ).fetchone()

    if not st:
        conn.close()
        return jsonify({"error": "Shared transaction not found"}), 404

    if organisation_id != st["origin_org_id"]:
        conn.close()
        return jsonify({"error": "Only the origin org can reject a correction at this stage"}), 403

    if st["shared_status"] != "CORRECTION_PROPOSED":
        conn.close()
        return jsonify({"error": "Shared transaction is not awaiting correction review"}), 400

    _, qr_error = validate_qr_handoff_token_for_action(
        conn,
        qr_token_id,
        shared_transaction_id,
        organisation_id,
        "CORRECTION_REVIEW"
    )
    if qr_error:
        conn.close()
        return jsonify(qr_error[0]), qr_error[1]

    previous_status = st["shared_status"]

    conn.execute(
        """
        UPDATE shared_transactions
        SET shared_status = ?,
            disputed_by_display_name = ?,
            disputed_at = ?,
            dispute_reason_code = ?,
            dispute_reason_text = ?,
            updated_at = ?
        WHERE shared_transaction_id = ?
        """,
        (
            "DISPUTED",
            rejected_by_display_name,
            now_iso(),
            "CORRECTION_REJECTED",
            rejection_reason_text,
            now_iso(),
            shared_transaction_id
        )
    )

    consume_qr_handoff_token(
        conn,
        qr_token_id,
        rejected_by_display_name,
        "CORRECTION_REJECTED"
    )

    record_shared_transaction_event(
        conn=conn,
        shared_transaction_id=shared_transaction_id,
        organisation_id=organisation_id,
        actor_org_role="DISPATCHING",
        action="CORRECTION_REJECTED",
        summary="Correction rejected by origin org",
        previous_status=previous_status,
        new_status="DISPUTED",
        created_by_display_name=rejected_by_display_name
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="CORRECTION_REJECTED",
        summary="Origin org rejected correction proposal",
        organisation_id=st["origin_org_id"]
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="CORRECTION_REJECTED",
        summary="Origin org rejected correction proposal",
        organisation_id=st["counterparty_org_id"]
    )

    conn.commit()
    conn.close()

    return jsonify({
        "shared_transaction_id": shared_transaction_id,
        "shared_status": "DISPUTED",
        "dispute_reason_code": "CORRECTION_REJECTED",
        "dispute_reason_text": rejection_reason_text,
        "rejected_by_display_name": rejected_by_display_name,
        "qr_token_id": qr_token_id
    }), 200


@app.post("/shared-transactions/<shared_transaction_id>/confirm")
def confirm_shared_transaction(shared_transaction_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    qr_token_id = body.get("qr_token_id")
    confirmed_by_display_name = (body.get("confirmed_by_display_name") or "Unknown User").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400

    conn = get_conn()
    ensure_shared_transaction_tables(conn)
    ensure_qr_token_tables(conn)

    st = conn.execute(
        "SELECT * FROM shared_transactions WHERE shared_transaction_id = ?",
        (shared_transaction_id,)
    ).fetchone()

    if not st:
        conn.close()
        return jsonify({"error": "Shared transaction not found"}), 404

    if organisation_id != st["counterparty_org_id"]:
        conn.close()
        return jsonify({"error": "Only the counterparty org can confirm this transaction at this stage"}), 403

    if st["shared_status"] != "AWAITING_COUNTERPARTY_CONFIRMATION":
        conn.close()
        return jsonify({"error": "Shared transaction is not awaiting counterparty confirmation"}), 400

    _, qr_error = validate_qr_handoff_token_for_action(
        conn,
        qr_token_id,
        shared_transaction_id,
        organisation_id,
        "INITIAL_CONFIRMATION"
    )
    if qr_error:
        conn.close()
        return jsonify(qr_error[0]), qr_error[1]

    previous_status = st["shared_status"]

    conn.execute(
        """
        UPDATE shared_transactions
        SET shared_status = ?,
            confirmed_by_display_name = ?,
            confirmed_at = ?,
            updated_at = ?
        WHERE shared_transaction_id = ?
        """,
        ("CONFIRMED", confirmed_by_display_name, now_iso(), now_iso(), shared_transaction_id)
    )

    consume_qr_handoff_token(
        conn,
        qr_token_id,
        confirmed_by_display_name,
        "CONFIRM_RECEIVE"
    )

    record_shared_transaction_event(
        conn=conn,
        shared_transaction_id=shared_transaction_id,
        organisation_id=organisation_id,
        actor_org_role="RECEIVING",
        action="CONFIRM_RECEIVE",
        summary=f"Receiving org confirmed shared transaction for {st['quantity']} {st['resource_name']}",
        previous_status=previous_status,
        new_status="CONFIRMED",
        created_by_display_name=confirmed_by_display_name
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="CONFIRM_RECEIVE",
        summary="Shared transaction confirmed by counterparty org",
        organisation_id=st["counterparty_org_id"]
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="CONFIRMED",
        summary="Counterparty org confirmed shared transaction",
        organisation_id=st["origin_org_id"]
    )

    conn.commit()
    conn.close()

    return jsonify({
        "shared_transaction_id": shared_transaction_id,
        "shared_status": "CONFIRMED",
        "confirmed_by_display_name": confirmed_by_display_name,
        "qr_token_id": qr_token_id
    }), 200


@app.post("/shared-transactions/<shared_transaction_id>/dispute")
def dispute_shared_transaction(shared_transaction_id):
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    dispute_reason_code = (body.get("dispute_reason_code") or "DISPUTED_BY_USER").strip()
    dispute_reason_text = (body.get("dispute_reason_text") or "Transaction disputed by user").strip()
    disputed_by_display_name = (body.get("disputed_by_display_name") or "Unknown User").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400

    conn = get_conn()
    ensure_shared_transaction_tables(conn)

    st = conn.execute(
        "SELECT * FROM shared_transactions WHERE shared_transaction_id = ?",
        (shared_transaction_id,)
    ).fetchone()

    if not st:
        conn.close()
        return jsonify({"error": "Shared transaction not found"}), 404

    if organisation_id not in (st["origin_org_id"], st["counterparty_org_id"]):
        conn.close()
        return jsonify({"error": "Organisation is not part of this shared transaction"}), 403

    if st["shared_status"] in ("CANCELLED",):
        conn.close()
        return jsonify({"error": "Shared transaction can no longer be disputed"}), 400

    previous_status = st["shared_status"]
    actor_role = "DISPATCHING" if organisation_id == st["origin_org_id"] else "RECEIVING"

    conn.execute(
        """
        UPDATE shared_transactions
        SET shared_status = ?,
            disputed_by_display_name = ?,
            disputed_at = ?,
            dispute_reason_code = ?,
            dispute_reason_text = ?,
            updated_at = ?
        WHERE shared_transaction_id = ?
        """,
        (
            "DISPUTED",
            disputed_by_display_name,
            now_iso(),
            dispute_reason_code,
            dispute_reason_text,
            now_iso(),
            shared_transaction_id
        )
    )

    record_shared_transaction_event(
        conn=conn,
        shared_transaction_id=shared_transaction_id,
        organisation_id=organisation_id,
        actor_org_role=actor_role,
        action="DISPUTE",
        summary=f"Shared transaction disputed: {dispute_reason_code}",
        previous_status=previous_status,
        new_status="DISPUTED",
        created_by_display_name=disputed_by_display_name
    )

    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="DISPUTE",
        summary=f"Shared transaction disputed: {dispute_reason_code}",
        organisation_id=organisation_id
    )

    other_org_id = st["counterparty_org_id"] if organisation_id == st["origin_org_id"] else st["origin_org_id"]
    audit_event(
        conn,
        entity_type="SharedTransaction",
        entity_id=shared_transaction_id,
        action="COUNTERPARTY_DISPUTE",
        summary=f"Counterparty disputed shared transaction: {dispute_reason_code}",
        organisation_id=other_org_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "shared_transaction_id": shared_transaction_id,
        "shared_status": "DISPUTED",
        "dispute_reason_code": dispute_reason_code,
        "dispute_reason_text": dispute_reason_text,
        "disputed_by_display_name": disputed_by_display_name
    }), 200



@app.post("/brand-requests")
def create_brand_request():
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    requested_name = (body.get("requested_name") or "").strip()
    note = (body.get("note") or "").strip() or None
    submitted_by_display_name = (body.get("submitted_by_display_name") or "Unknown User").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400
    if not requested_name:
        return jsonify({"error": "requested_name is required"}), 400

    conn = get_conn()
    org = conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchone()
    if not org:
        conn.close()
        return jsonify({"error": "Organisation not found"}), 404

    existing_brand = conn.execute(
        """
        SELECT brand_id, name
        FROM brands
        WHERE organisation_id = ?
          AND LOWER(TRIM(name)) = LOWER(TRIM(?))
        LIMIT 1
        """,
        (organisation_id, requested_name)
    ).fetchone()

    if existing_brand:
        conn.close()
        return jsonify({
            "error": "Brand already exists",
            "brand_id": existing_brand["brand_id"],
            "name": existing_brand["name"]
        }), 409

    existing_request = conn.execute(
        """
        SELECT brand_request_id, status, requested_name
        FROM brand_requests
        WHERE organisation_id = ?
          AND LOWER(TRIM(requested_name)) = LOWER(TRIM(?))
          AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
        LIMIT 1
        """,
        (organisation_id, requested_name)
    ).fetchone()

    if existing_request:
        conn.close()
        return jsonify({
            "error": "Duplicate open brand request already exists",
            "brand_request_id": existing_request["brand_request_id"],
            "status": existing_request["status"],
            "requested_name": existing_request["requested_name"]
        }), 409

    brand_request_id = make_id("breq")

    conn.execute(
        """
        INSERT INTO brand_requests (
            brand_request_id,
            organisation_id,
            requested_name,
            note,
            submitted_by_display_name,
            status,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            brand_request_id,
            organisation_id,
            requested_name,
            note,
            submitted_by_display_name,
            "PENDING_APPROVAL",
            now_iso(),
            now_iso()
        )
    )

    audit_event(
        conn,
        entity_type="BrandRequest",
        entity_id=brand_request_id,
        action="CREATE",
        summary=f"Created brand request: {requested_name}",
        organisation_id=organisation_id
    )

    pending_entry_id = create_pending_entry(
        conn=conn,
        organisation_id=organisation_id,
        entry_type="BrandRequest",
        source_record_id=brand_request_id,
        source_module="BrandRequests",
        submitted_by_display_name=submitted_by_display_name,
        related_entity_type="ResourceModule",
        related_entity_id="resource-module",
        related_entity_name="Resource Module",
        reason_code="MISSING_DROPDOWN_OPTION",
        reason_text="User requested a new brand because the required dropdown option was not available.",
        direct_action_type="GoToResourceModule",
        direct_action_target_id="resource-module",
        direct_action_label="Review / Create Brand",
        can_approve_now=True,
        can_reject_now=True,
        resource_id=None,
        resource_name=requested_name,
        status="PENDING_APPROVAL"
    )

    conn.commit()
    conn.close()

    return jsonify({
        "brand_request_id": brand_request_id,
        "status": "PENDING_APPROVAL",
        "pending_entry_id": pending_entry_id,
        "requested_name": requested_name,
        "note": note,
        "submitted_by_display_name": submitted_by_display_name,
        "message": "Your request has been saved and sent to your Org Admin for approval. You can continue with your entry."
    }), 201

@app.get("/brand-requests")
def list_brand_requests():
    organisation_id = request.args.get("organisation_id")
    status = request.args.get("status")

    conn = get_conn()

    sql = """
        SELECT
            br.brand_request_id,
            br.organisation_id,
            o.name AS organisation_name,
            br.requested_name,
            br.note,
            br.submitted_by_display_name,
            br.status,
            br.rejection_reason_code,
            br.rejection_reason_text,
            br.created_at,
            br.updated_at
        FROM brand_requests br
        LEFT JOIN organisations o ON o.organisation_id = br.organisation_id
        WHERE 1=1
    """
    params = []

    if organisation_id:
        sql += " AND br.organisation_id = ?"
        params.append(organisation_id)

    if status:
        sql += " AND br.status = ?"
        params.append(status)

    sql += " ORDER BY br.created_at DESC"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    return jsonify({
        "count": len(rows),
        "items": [dict(r) for r in rows]
    }), 200

@app.get("/brand-requests/<brand_request_id>")
def get_brand_request(brand_request_id):
    conn = get_conn()

    row = conn.execute(
        """
        SELECT
            br.brand_request_id,
            br.organisation_id,
            o.name AS organisation_name,
            br.requested_name,
            br.note,
            br.submitted_by_display_name,
            br.status,
            br.rejection_reason_code,
            br.rejection_reason_text,
            br.created_at,
            br.updated_at
        FROM brand_requests br
        LEFT JOIN organisations o ON o.organisation_id = br.organisation_id
        WHERE br.brand_request_id = ?
        """,
        (brand_request_id,)
    ).fetchone()

    conn.close()

    if not row:
        return jsonify({"error": "Brand request not found"}), 404

    return jsonify(dict(row)), 200

@app.post("/brand-requests/<brand_request_id>/approve")
def approve_brand_request(brand_request_id):
    conn = get_conn()

    req = conn.execute(
        "SELECT * FROM brand_requests WHERE brand_request_id = ?",
        (brand_request_id,)
    ).fetchone()

    if not req:
        conn.close()
        return jsonify({"error": "Brand request not found"}), 404

    if req["status"] != "PENDING_APPROVAL":
        conn.close()
        return jsonify({"error": "Brand request is not pending approval"}), 400

    brand_id = make_id("brand")

    conn.execute(
        """
        INSERT INTO brands (
            brand_id,
            organisation_id,
            name,
            is_active,
            created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            brand_id,
            req["organisation_id"],
            req["requested_name"],
            1,
            now_iso()
        )
    )

    conn.execute(
        """
        UPDATE brand_requests
        SET status = ?, updated_at = ?
        WHERE brand_request_id = ?
        """,
        ("RESOLVED", now_iso(), brand_request_id)
    )

    conn.execute(
        """
        UPDATE pending_approval_entries
        SET status = ?, updated_at = ?
        WHERE source_record_id = ?
          AND entry_type = 'BrandRequest'
          AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
        """,
        ("RESOLVED", now_iso(), brand_request_id)
    )

    audit_event(
        conn,
        entity_type="Brand",
        entity_id=brand_id,
        action="CREATE",
        summary=f"Created brand from request: {req['requested_name']}",
        organisation_id=req["organisation_id"]
    )

    audit_event(
        conn,
        entity_type="BrandRequest",
        entity_id=brand_request_id,
        action="APPROVE",
        summary=f"Approved brand request: {req['requested_name']}",
        organisation_id=req["organisation_id"]
    )

    pending_rows = conn.execute(
        """
        SELECT pending_entry_id
        FROM pending_approval_entries
        WHERE source_record_id = ?
          AND entry_type = 'BrandRequest'
        """,
        (brand_request_id,)
    ).fetchall()

    for row in pending_rows:
        audit_event(
            conn,
            entity_type="PendingApprovalEntry",
            entity_id=row["pending_entry_id"],
            action="APPROVE",
            summary="Pending approval entry approved and resolved for brand request",
            organisation_id=req["organisation_id"]
        )

    conn.commit()
    conn.close()

    return jsonify({
        "brand_request_id": brand_request_id,
        "brand_id": brand_id,
        "requested_name": req["requested_name"],
        "status": "RESOLVED",
        "brand_status": "CREATED"
    }), 200


@app.post("/category-requests")
def create_category_request():
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    requested_name = (body.get("requested_name") or "").strip()
    note = (body.get("note") or "").strip() or None
    submitted_by_display_name = (body.get("submitted_by_display_name") or "Unknown User").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400
    if not requested_name:
        return jsonify({"error": "requested_name is required"}), 400

    conn = get_conn()
    org = conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchone()
    if not org:
        conn.close()
        return jsonify({"error": "Organisation not found"}), 404

    existing_category = conn.execute(
        """
        SELECT category_id, name
        FROM categories
        WHERE organisation_id = ?
          AND LOWER(TRIM(name)) = LOWER(TRIM(?))
        LIMIT 1
        """,
        (organisation_id, requested_name)
    ).fetchone()

    if existing_category:
        conn.close()
        return jsonify({
            "error": "Category already exists",
            "category_id": existing_category["category_id"],
            "name": existing_category["name"]
        }), 409

    existing_request = conn.execute(
        """
        SELECT category_request_id, status, requested_name
        FROM category_requests
        WHERE organisation_id = ?
          AND LOWER(TRIM(requested_name)) = LOWER(TRIM(?))
          AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
        LIMIT 1
        """,
        (organisation_id, requested_name)
    ).fetchone()

    if existing_request:
        conn.close()
        return jsonify({
            "error": "Duplicate open category request already exists",
            "category_request_id": existing_request["category_request_id"],
            "status": existing_request["status"],
            "requested_name": existing_request["requested_name"]
        }), 409

    category_request_id = make_id("creq")

    conn.execute(
        """
        INSERT INTO category_requests (
            category_request_id,
            organisation_id,
            requested_name,
            note,
            submitted_by_display_name,
            status,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            category_request_id,
            organisation_id,
            requested_name,
            note,
            submitted_by_display_name,
            "PENDING_APPROVAL",
            now_iso(),
            now_iso()
        )
    )

    audit_event(
        conn,
        entity_type="CategoryRequest",
        entity_id=category_request_id,
        action="CREATE",
        summary=f"Created category request: {requested_name}",
        organisation_id=organisation_id
    )

    pending_entry_id = create_pending_entry(
        conn=conn,
        organisation_id=organisation_id,
        entry_type="CategoryRequest",
        source_record_id=category_request_id,
        source_module="CategoryRequests",
        submitted_by_display_name=submitted_by_display_name,
        related_entity_type="ResourceModule",
        related_entity_id="resource-module",
        related_entity_name="Resource Module",
        reason_code="MISSING_DROPDOWN_OPTION",
        reason_text="User requested a new category because the required dropdown option was not available.",
        direct_action_type="GoToResourceModule",
        direct_action_target_id="resource-module",
        direct_action_label="Review / Create Category",
        can_approve_now=True,
        can_reject_now=True,
        resource_id=None,
        resource_name=requested_name,
        status="PENDING_APPROVAL"
    )

    conn.commit()
    conn.close()

    return jsonify({
        "category_request_id": category_request_id,
        "status": "PENDING_APPROVAL",
        "pending_entry_id": pending_entry_id,
        "requested_name": requested_name,
        "note": note,
        "submitted_by_display_name": submitted_by_display_name,
        "message": "Your request has been saved and sent to your Org Admin for approval. You can continue with your entry."
    }), 201

@app.get("/category-requests")
def list_category_requests():
    organisation_id = request.args.get("organisation_id")
    status = request.args.get("status")

    conn = get_conn()

    sql = """
        SELECT
            cr.category_request_id,
            cr.organisation_id,
            o.name AS organisation_name,
            cr.requested_name,
            cr.note,
            cr.submitted_by_display_name,
            cr.status,
            cr.rejection_reason_code,
            cr.rejection_reason_text,
            cr.created_at,
            cr.updated_at
        FROM category_requests cr
        LEFT JOIN organisations o ON o.organisation_id = cr.organisation_id
        WHERE 1=1
    """
    params = []

    if organisation_id:
        sql += " AND cr.organisation_id = ?"
        params.append(organisation_id)

    if status:
        sql += " AND cr.status = ?"
        params.append(status)

    sql += " ORDER BY cr.created_at DESC"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    return jsonify({
        "count": len(rows),
        "items": [dict(r) for r in rows]
    }), 200

@app.get("/category-requests/<category_request_id>")
def get_category_request(category_request_id):
    conn = get_conn()

    row = conn.execute(
        """
        SELECT
            cr.category_request_id,
            cr.organisation_id,
            o.name AS organisation_name,
            cr.requested_name,
            cr.note,
            cr.submitted_by_display_name,
            cr.status,
            cr.rejection_reason_code,
            cr.rejection_reason_text,
            cr.created_at,
            cr.updated_at
        FROM category_requests cr
        LEFT JOIN organisations o ON o.organisation_id = cr.organisation_id
        WHERE cr.category_request_id = ?
        """,
        (category_request_id,)
    ).fetchone()

    conn.close()

    if not row:
        return jsonify({"error": "Category request not found"}), 404

    return jsonify(dict(row)), 200

@app.post("/category-requests/<category_request_id>/approve")
def approve_category_request(category_request_id):
    conn = get_conn()

    req = conn.execute(
        "SELECT * FROM category_requests WHERE category_request_id = ?",
        (category_request_id,)
    ).fetchone()

    if not req:
        conn.close()
        return jsonify({"error": "Category request not found"}), 404

    if req["status"] != "PENDING_APPROVAL":
        conn.close()
        return jsonify({"error": "Category request is not pending approval"}), 400

    category_id = make_id("cat")

    conn.execute(
        """
        INSERT INTO categories (
            category_id,
            organisation_id,
            name,
            is_active,
            created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            category_id,
            req["organisation_id"],
            req["requested_name"],
            1,
            now_iso()
        )
    )

    conn.execute(
        """
        UPDATE category_requests
        SET status = ?, updated_at = ?
        WHERE category_request_id = ?
        """,
        ("RESOLVED", now_iso(), category_request_id)
    )

    conn.execute(
        """
        UPDATE pending_approval_entries
        SET status = ?, updated_at = ?
        WHERE source_record_id = ?
          AND entry_type = 'CategoryRequest'
          AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
        """,
        ("RESOLVED", now_iso(), category_request_id)
    )

    audit_event(
        conn,
        entity_type="Category",
        entity_id=category_id,
        action="CREATE",
        summary=f"Created category from request: {req['requested_name']}",
        organisation_id=req["organisation_id"]
    )

    audit_event(
        conn,
        entity_type="CategoryRequest",
        entity_id=category_request_id,
        action="APPROVE",
        summary=f"Approved category request: {req['requested_name']}",
        organisation_id=req["organisation_id"]
    )

    pending_rows = conn.execute(
        """
        SELECT pending_entry_id
        FROM pending_approval_entries
        WHERE source_record_id = ?
          AND entry_type = 'CategoryRequest'
        """,
        (category_request_id,)
    ).fetchall()

    for row in pending_rows:
        audit_event(
            conn,
            entity_type="PendingApprovalEntry",
            entity_id=row["pending_entry_id"],
            action="APPROVE",
            summary="Pending approval entry approved and resolved for category request",
            organisation_id=req["organisation_id"]
        )

    conn.commit()
    conn.close()

    return jsonify({
        "category_request_id": category_request_id,
        "category_id": category_id,
        "requested_name": req["requested_name"],
        "status": "RESOLVED",
        "category_status": "CREATED"
    }), 200


@app.post("/resource-requests")
def create_resource_request():
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    category_id = body.get("category_id")
    brand_id = body.get("brand_id")
    requested_name = (body.get("requested_name") or "").strip()
    resource_type = (body.get("resource_type") or "pallet").strip()
    unit_type = (body.get("unit_type") or "each").strip()
    note = (body.get("note") or "").strip() or None
    submitted_by_display_name = (body.get("submitted_by_display_name") or "Unknown User").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400
    if not category_id:
        return jsonify({"error": "category_id is required"}), 400
    if not requested_name:
        return jsonify({"error": "requested_name is required"}), 400

    conn = get_conn()
    org = conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchone()
    if not org:
        conn.close()
        return jsonify({"error": "Organisation not found"}), 404

    category = conn.execute(
        "SELECT * FROM categories WHERE category_id = ? AND organisation_id = ?",
        (category_id, organisation_id)
    ).fetchone()
    if not category:
        conn.close()
        return jsonify({"error": "Category not found"}), 404

    brand = None
    if brand_id:
        brand = conn.execute(
            "SELECT * FROM brands WHERE brand_id = ? AND organisation_id = ?",
            (brand_id, organisation_id)
        ).fetchone()
        if not brand:
            conn.close()
            return jsonify({"error": "Brand not found"}), 404

    existing_resource = conn.execute(
        """
        SELECT resource_id, name
        FROM resources
        WHERE organisation_id = ?
          AND category_id = ?
          AND COALESCE(brand_id, '') = COALESCE(?, '')
          AND LOWER(TRIM(name)) = LOWER(TRIM(?))
          AND resource_type = ?
          AND unit_type = ?
        LIMIT 1
        """,
        (organisation_id, category_id, brand_id, requested_name, resource_type, unit_type)
    ).fetchone()

    if existing_resource:
        conn.close()
        return jsonify({
            "error": "Resource already exists",
            "resource_id": existing_resource["resource_id"],
            "name": existing_resource["name"]
        }), 409

    existing_request = conn.execute(
        """
        SELECT resource_request_id, status, requested_name
        FROM resource_requests
        WHERE organisation_id = ?
          AND category_id = ?
          AND COALESCE(brand_id, '') = COALESCE(?, '')
          AND LOWER(TRIM(requested_name)) = LOWER(TRIM(?))
          AND resource_type = ?
          AND unit_type = ?
          AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
        LIMIT 1
        """,
        (organisation_id, category_id, brand_id, requested_name, resource_type, unit_type)
    ).fetchone()

    if existing_request:
        conn.close()
        return jsonify({
            "error": "Duplicate open resource request already exists",
            "resource_request_id": existing_request["resource_request_id"],
            "status": existing_request["status"],
            "requested_name": existing_request["requested_name"]
        }), 409

    resource_request_id = make_id("rreq")

    conn.execute(
        """
        INSERT INTO resource_requests (
            resource_request_id,
            organisation_id,
            category_id,
            brand_id,
            requested_name,
            resource_type,
            unit_type,
            note,
            submitted_by_display_name,
            status,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            resource_request_id,
            organisation_id,
            category_id,
            brand_id,
            requested_name,
            resource_type,
            unit_type,
            note,
            submitted_by_display_name,
            "PENDING_APPROVAL",
            now_iso(),
            now_iso()
        )
    )

    audit_event(
        conn,
        entity_type="ResourceRequest",
        entity_id=resource_request_id,
        action="CREATE",
        summary=f"Created resource request: {requested_name}",
        organisation_id=organisation_id
    )

    pending_entry_id = create_pending_entry(
        conn=conn,
        organisation_id=organisation_id,
        entry_type="ResourceRequest",
        source_record_id=resource_request_id,
        source_module="ResourceRequests",
        submitted_by_display_name=submitted_by_display_name,
        related_entity_type="ResourceModule",
        related_entity_id="resource-module",
        related_entity_name="Resource Module",
        reason_code="MISSING_DROPDOWN_OPTION",
        reason_text="User requested a new resource because the required dropdown option was not available.",
        direct_action_type="GoToResourceModule",
        direct_action_target_id="resource-module",
        direct_action_label="Review / Create Resource",
        can_approve_now=True,
        can_reject_now=True,
        resource_id=None,
        resource_name=requested_name,
        status="PENDING_APPROVAL"
    )

    conn.commit()
    conn.close()

    return jsonify({
        "resource_request_id": resource_request_id,
        "status": "PENDING_APPROVAL",
        "pending_entry_id": pending_entry_id,
        "category_id": category_id,
        "category_name": category["name"],
        "brand_id": brand_id,
        "brand_name": brand["name"] if brand else None,
        "requested_name": requested_name,
        "resource_type": resource_type,
        "unit_type": unit_type,
        "note": note,
        "submitted_by_display_name": submitted_by_display_name,
        "message": "Your request has been saved and sent to your Org Admin for approval. You can continue with your entry."
    }), 201

@app.get("/resource-requests")
def list_resource_requests():
    organisation_id = request.args.get("organisation_id")
    status = request.args.get("status")

    conn = get_conn()

    sql = """
        SELECT
            rr.resource_request_id,
            rr.organisation_id,
            o.name AS organisation_name,
            rr.category_id,
            c.name AS category_name,
            rr.brand_id,
            b.name AS brand_name,
            rr.requested_name,
            rr.resource_type,
            rr.unit_type,
            rr.note,
            rr.submitted_by_display_name,
            rr.status,
            rr.rejection_reason_code,
            rr.rejection_reason_text,
            rr.created_at,
            rr.updated_at
        FROM resource_requests rr
        LEFT JOIN organisations o ON o.organisation_id = rr.organisation_id
        LEFT JOIN categories c ON c.category_id = rr.category_id
        LEFT JOIN brands b ON b.brand_id = rr.brand_id
        WHERE 1=1
    """
    params = []

    if organisation_id:
        sql += " AND rr.organisation_id = ?"
        params.append(organisation_id)

    if status:
        sql += " AND rr.status = ?"
        params.append(status)

    sql += " ORDER BY rr.created_at DESC"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    return jsonify({
        "count": len(rows),
        "items": [dict(r) for r in rows]
    }), 200

@app.get("/resource-requests/<resource_request_id>")
def get_resource_request(resource_request_id):
    conn = get_conn()

    row = conn.execute(
        """
        SELECT
            rr.resource_request_id,
            rr.organisation_id,
            o.name AS organisation_name,
            rr.category_id,
            c.name AS category_name,
            rr.brand_id,
            b.name AS brand_name,
            rr.requested_name,
            rr.resource_type,
            rr.unit_type,
            rr.note,
            rr.submitted_by_display_name,
            rr.status,
            rr.rejection_reason_code,
            rr.rejection_reason_text,
            rr.created_at,
            rr.updated_at
        FROM resource_requests rr
        LEFT JOIN organisations o ON o.organisation_id = rr.organisation_id
        LEFT JOIN categories c ON c.category_id = rr.category_id
        LEFT JOIN brands b ON b.brand_id = rr.brand_id
        WHERE rr.resource_request_id = ?
        """,
        (resource_request_id,)
    ).fetchone()

    conn.close()

    if not row:
        return jsonify({"error": "Resource request not found"}), 404

    return jsonify(dict(row)), 200

@app.post("/resource-requests/<resource_request_id>/approve")
def approve_resource_request(resource_request_id):
    conn = get_conn()

    req = conn.execute(
        """
        SELECT *
        FROM resource_requests
        WHERE resource_request_id = ?
        """,
        (resource_request_id,)
    ).fetchone()

    if not req:
        conn.close()
        return jsonify({"error": "Resource request not found"}), 404

    if req["status"] != "PENDING_APPROVAL":
        conn.close()
        return jsonify({"error": "Resource request is not pending approval"}), 400

    resource_id = make_id("res")

    conn.execute(
        """
        INSERT INTO resources (
            resource_id,
            organisation_id,
            category_id,
            brand_id,
            name,
            resource_type,
            unit_type,
            is_active,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            resource_id,
            req["organisation_id"],
            req["category_id"],
            req["brand_id"],
            req["requested_name"],
            req["resource_type"],
            req["unit_type"],
            1,
            now_iso()
        )
    )

    conn.execute(
        """
        UPDATE resource_requests
        SET status = ?, updated_at = ?
        WHERE resource_request_id = ?
        """,
        ("RESOLVED", now_iso(), resource_request_id)
    )

    conn.execute(
        """
        UPDATE pending_approval_entries
        SET status = ?, updated_at = ?
        WHERE source_record_id = ?
          AND entry_type = 'ResourceRequest'
          AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
        """,
        ("RESOLVED", now_iso(), resource_request_id)
    )

    audit_event(
        conn,
        entity_type="Resource",
        entity_id=resource_id,
        action="CREATE",
        summary=f"Created resource from request: {req['requested_name']}",
        organisation_id=req["organisation_id"]
    )

    audit_event(
        conn,
        entity_type="ResourceRequest",
        entity_id=resource_request_id,
        action="APPROVE",
        summary=f"Approved resource request: {req['requested_name']}",
        organisation_id=req["organisation_id"]
    )

    pending_rows = conn.execute(
        """
        SELECT pending_entry_id
        FROM pending_approval_entries
        WHERE source_record_id = ?
          AND entry_type = 'ResourceRequest'
        """,
        (resource_request_id,)
    ).fetchall()

    for row in pending_rows:
        audit_event(
            conn,
            entity_type="PendingApprovalEntry",
            entity_id=row["pending_entry_id"],
            action="APPROVE",
            summary="Pending approval entry approved and resolved for resource request",
            organisation_id=req["organisation_id"]
        )

    conn.commit()
    conn.close()

    return jsonify({
        "resource_request_id": resource_request_id,
        "resource_id": resource_id,
        "requested_name": req["requested_name"],
        "status": "RESOLVED",
        "resource_status": "CREATED"
    }), 200

@app.get("/resources")
def list_resources():
    organisation_id = request.args.get("organisation_id")
    resource_type = request.args.get("resource_type")
    is_active = request.args.get("is_active")
    category_id = request.args.get("category_id")
    brand_id = request.args.get("brand_id")

    conn = get_conn()
    ensure_resource_cleanup_columns(conn)

    sql = """
        SELECT
            r.resource_id,
            r.organisation_id,
            o.name AS organisation_name,
            r.category_id,
            c.name AS category_name,
            r.brand_id,
            b.name AS brand_name,
            r.name,
            r.resource_type,
            r.unit_type,
            r.is_active,
            r.merged_into_resource_id,
            r.inactive_reason_code,
            r.inactive_reason_text,
            r.created_at,
            r.updated_at
        FROM resources r
        LEFT JOIN organisations o ON o.organisation_id = r.organisation_id
        LEFT JOIN categories c ON c.category_id = r.category_id
        LEFT JOIN brands b ON b.brand_id = r.brand_id
        WHERE 1=1
    """
    params = []

    if organisation_id:
        sql += " AND r.organisation_id = ?"
        params.append(organisation_id)

    if resource_type:
        sql += " AND r.resource_type = ?"
        params.append(resource_type)

    if category_id:
        sql += " AND r.category_id = ?"
        params.append(category_id)

    if brand_id:
        sql += " AND r.brand_id = ?"
        params.append(brand_id)

    if is_active == "true":
        sql += " AND r.is_active = 1"
    elif is_active == "false":
        sql += " AND r.is_active = 0"

    sql += " ORDER BY r.name"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    items = []
    for row in rows:
        d = dict(row)
        d["is_active"] = bool(d["is_active"])
        items.append(d)

    return jsonify({
        "count": len(items),
        "items": items
    }), 200

@app.get("/resources/<resource_id>")
def get_resource_profile(resource_id):
    conn = get_conn()
    ensure_resource_cleanup_columns(conn)

    row = conn.execute(
        """
        SELECT
            r.resource_id,
            r.organisation_id,
            o.name AS organisation_name,
            r.category_id,
            c.name AS category_name,
            r.brand_id,
            b.name AS brand_name,
            r.name,
            r.resource_type,
            r.unit_type,
            r.is_active,
            r.merged_into_resource_id,
            r.inactive_reason_code,
            r.inactive_reason_text,
            r.created_at,
            r.updated_at
        FROM resources r
        LEFT JOIN organisations o ON o.organisation_id = r.organisation_id
        LEFT JOIN categories c ON c.category_id = r.category_id
        LEFT JOIN brands b ON b.brand_id = r.brand_id
        WHERE r.resource_id = ?
        """,
        (resource_id,)
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "Resource not found"}), 404

    stock_rows = conn.execute(
        """
        SELECT
            bp.depot_id,
            d.name AS depot_name,
            bp.current_quantity,
            bp.updated_at
        FROM balance_projection bp
        LEFT JOIN depots d ON d.depot_id = bp.depot_id
        WHERE bp.resource_id = ?
        ORDER BY d.name
        """,
        (resource_id,)
    ).fetchall()

    conn.close()

    d = dict(row)
    d["is_active"] = bool(d["is_active"])
    d["stock_count"] = len(stock_rows)
    d["stock_items"] = [dict(r) for r in stock_rows]

    return jsonify(d), 200

@app.post("/resources")
def create_resource():
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    category_id = body.get("category_id")
    brand_id = body.get("brand_id")
    name = (body.get("name") or "").strip()
    resource_type = (body.get("resource_type") or "pallet").strip()
    unit_type = (body.get("unit_type") or "each").strip()

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400
    if not category_id:
        return jsonify({"error": "category_id is required"}), 400
    if not name:
        return jsonify({"error": "name is required"}), 400

    conn = get_conn()
    org = conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchone()

    if not org:
        conn.close()
        return jsonify({"error": "Organisation not found"}), 404

    category = conn.execute(
        "SELECT * FROM categories WHERE category_id = ? AND organisation_id = ?",
        (category_id, organisation_id)
    ).fetchone()
    if not category:
        conn.close()
        return jsonify({"error": "Category not found"}), 404

    brand = None
    if brand_id:
        brand = conn.execute(
            "SELECT * FROM brands WHERE brand_id = ? AND organisation_id = ?",
            (brand_id, organisation_id)
        ).fetchone()
        if not brand:
            conn.close()
            return jsonify({"error": "Brand not found"}), 404

    existing_resource = conn.execute(
        """
        SELECT resource_id, name
        FROM resources
        WHERE organisation_id = ?
          AND category_id = ?
          AND COALESCE(brand_id, '') = COALESCE(?, '')
          AND LOWER(TRIM(name)) = LOWER(TRIM(?))
          AND resource_type = ?
          AND unit_type = ?
        LIMIT 1
        """,
        (organisation_id, category_id, brand_id, name, resource_type, unit_type)
    ).fetchone()

    if existing_resource:
        conn.close()
        return jsonify({
            "error": "Resource already exists",
            "resource_id": existing_resource["resource_id"],
            "name": existing_resource["name"]
        }), 409

    resource_id = make_id("res")

    conn.execute(
        """
        INSERT INTO resources (
            resource_id,
            organisation_id,
            category_id,
            brand_id,
            name,
            resource_type,
            unit_type,
            is_active,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            resource_id,
            organisation_id,
            category_id,
            brand_id,
            name,
            resource_type,
            unit_type,
            1,
            now_iso()
        )
    )

    audit_event(
        conn,
        entity_type="Resource",
        entity_id=resource_id,
        action="CREATE",
        summary=f"Created resource: {name}",
        organisation_id=organisation_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "resource_id": resource_id,
        "organisation_id": organisation_id,
        "category_id": category_id,
        "category_name": category["name"],
        "brand_id": brand_id,
        "brand_name": brand["name"] if brand else None,
        "name": name,
        "resource_type": resource_type,
        "unit_type": unit_type,
        "is_active": True
    }), 201


@app.patch("/resources/<resource_id>")
def update_resource(resource_id):
    body = request.get_json(silent=True) or {}

    conn = get_conn()
    ensure_resource_cleanup_columns(conn)

    resource = conn.execute(
        "SELECT * FROM resources WHERE resource_id = ?", (resource_id,)
    ).fetchone()

    if not resource:
        conn.close()
        return jsonify({"error": "Resource not found"}), 404

    new_name = (body.get("name") or "").strip() or resource["name"]
    new_resource_type = (body.get("resource_type") or "").strip() or resource["resource_type"]
    new_unit_type = (body.get("unit_type") or "").strip() or resource["unit_type"]
    new_category_id = body.get("category_id") or resource["category_id"]
    new_brand_id = body.get("brand_id") if "brand_id" in body else resource["brand_id"]

    if new_category_id != resource["category_id"]:
        category = conn.execute(
            "SELECT * FROM categories WHERE category_id = ? AND organisation_id = ?",
            (new_category_id, resource["organisation_id"]),
        ).fetchone()
        if not category:
            conn.close()
            return jsonify({"error": "Category not found"}), 404

    if new_brand_id and new_brand_id != resource["brand_id"]:
        brand = conn.execute(
            "SELECT * FROM brands WHERE brand_id = ? AND organisation_id = ?",
            (new_brand_id, resource["organisation_id"]),
        ).fetchone()
        if not brand:
            conn.close()
            return jsonify({"error": "Brand not found"}), 404

    changes = []
    if new_name != resource["name"]:
        changes.append(f"name '{resource['name']}' → '{new_name}'")
    if new_resource_type != resource["resource_type"]:
        changes.append(f"resource_type '{resource['resource_type']}' → '{new_resource_type}'")
    if new_unit_type != resource["unit_type"]:
        changes.append(f"unit_type '{resource['unit_type']}' → '{new_unit_type}'")
    if new_category_id != resource["category_id"]:
        changes.append(f"category_id → '{new_category_id}'")
    if new_brand_id != resource["brand_id"]:
        changes.append(f"brand_id → '{new_brand_id}'")

    if not changes:
        conn.close()
        return jsonify({"message": "No changes made", "resource_id": resource_id}), 200

    ts = now_iso()
    conn.execute(
        """UPDATE resources
           SET name = ?, resource_type = ?, unit_type = ?, category_id = ?, brand_id = ?, updated_at = ?
           WHERE resource_id = ?""",
        (new_name, new_resource_type, new_unit_type, new_category_id, new_brand_id, ts, resource_id),
    )

    audit_event(
        conn,
        entity_type="Resource",
        entity_id=resource_id,
        action="UPDATE",
        summary=f"Resource updated: {'; '.join(changes)}.",
        organisation_id=resource["organisation_id"],
    )

    conn.commit()
    conn.close()

    return jsonify({
        "resource_id": resource_id,
        "organisation_id": resource["organisation_id"],
        "name": new_name,
        "resource_type": new_resource_type,
        "unit_type": new_unit_type,
        "category_id": new_category_id,
        "brand_id": new_brand_id,
        "updated_at": ts,
    }), 200


@app.post("/resources/<resource_id>/classify")
def classify_resource(resource_id):
    body = request.get_json(silent=True) or {}
    category_id = body.get("category_id")
    brand_id = body.get("brand_id")

    if not category_id:
        return jsonify({"error": "category_id is required"}), 400

    conn = get_conn()
    ensure_resource_cleanup_columns(conn)

    resource = conn.execute(
        "SELECT * FROM resources WHERE resource_id = ?",
        (resource_id,)
    ).fetchone()

    if not resource:
        conn.close()
        return jsonify({"error": "Resource not found"}), 404

    category = conn.execute(
        "SELECT * FROM categories WHERE category_id = ? AND organisation_id = ?",
        (category_id, resource["organisation_id"])
    ).fetchone()

    if not category:
        conn.close()
        return jsonify({"error": "Category not found"}), 404

    brand = None
    if brand_id:
        brand = conn.execute(
            "SELECT * FROM brands WHERE brand_id = ? AND organisation_id = ?",
            (brand_id, resource["organisation_id"])
        ).fetchone()
        if not brand:
            conn.close()
            return jsonify({"error": "Brand not found"}), 404

    conn.execute(
        """
        UPDATE resources
        SET category_id = ?, brand_id = ?, updated_at = ?
        WHERE resource_id = ?
        """,
        (category_id, brand_id, now_iso(), resource_id)
    )

    audit_event(
        conn,
        entity_type="Resource",
        entity_id=resource_id,
        action="CLASSIFY",
        summary=f"Updated resource classification for {resource['name']}",
        organisation_id=resource["organisation_id"]
    )

    conn.commit()
    conn.close()

    return jsonify({
        "resource_id": resource_id,
        "status": "CLASSIFIED",
        "category_id": category_id,
        "category_name": category["name"],
        "brand_id": brand_id,
        "brand_name": brand["name"] if brand else None
    }), 200


@app.post("/resources/<resource_id>/deactivate")
def deactivate_resource(resource_id):
    body = request.get_json(silent=True) or {}
    merged_into_resource_id = body.get("merged_into_resource_id")
    inactive_reason_code = (body.get("inactive_reason_code") or "INACTIVE_BY_ADMIN").strip()
    inactive_reason_text = (body.get("inactive_reason_text") or "Marked inactive by Org Admin").strip()

    conn = get_conn()
    ensure_resource_cleanup_columns(conn)

    resource = conn.execute(
        "SELECT * FROM resources WHERE resource_id = ?",
        (resource_id,)
    ).fetchone()

    if not resource:
        conn.close()
        return jsonify({"error": "Resource not found"}), 404

    if merged_into_resource_id == resource_id:
        conn.close()
        return jsonify({"error": "Resource cannot merge into itself"}), 400

    if merged_into_resource_id:
        merge_target = conn.execute(
            """
            SELECT *
            FROM resources
            WHERE resource_id = ? AND organisation_id = ?
            """,
            (merged_into_resource_id, resource["organisation_id"])
        ).fetchone()

        if not merge_target:
            conn.close()
            return jsonify({"error": "Merge target resource not found"}), 404

    conn.execute(
        """
        UPDATE resources
        SET is_active = 0,
            merged_into_resource_id = ?,
            inactive_reason_code = ?,
            inactive_reason_text = ?,
            updated_at = ?
        WHERE resource_id = ?
        """,
        (
            merged_into_resource_id,
            inactive_reason_code,
            inactive_reason_text,
            now_iso(),
            resource_id
        )
    )

    audit_event(
        conn,
        entity_type="Resource",
        entity_id=resource_id,
        action="DEACTIVATE",
        summary=f"Deactivated resource: {resource['name']} ({inactive_reason_code})",
        organisation_id=resource["organisation_id"]
    )

    conn.commit()
    conn.close()

    return jsonify({
        "resource_id": resource_id,
        "status": "INACTIVE",
        "merged_into_resource_id": merged_into_resource_id,
        "inactive_reason_code": inactive_reason_code,
        "inactive_reason_text": inactive_reason_text
    }), 200


@app.post("/opening-balances")
def create_opening_balance():
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    depot_id = body.get("depot_id")
    resource_id = body.get("resource_id")
    quantity = body.get("quantity")

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400
    if not depot_id:
        return jsonify({"error": "depot_id is required"}), 400
    if not resource_id:
        return jsonify({"error": "resource_id is required"}), 400
    if not isinstance(quantity, int) or quantity < 0:
        return jsonify({"error": "quantity must be an integer greater than or equal to zero"}), 400

    conn = get_conn()
    ensure_transaction_numbering_tables(conn)

    depot = conn.execute(
        "SELECT * FROM depots WHERE depot_id = ? AND organisation_id = ?",
        (depot_id, organisation_id)
    ).fetchone()
    if not depot:
        conn.close()
        return jsonify({"error": "Depot not found"}), 404

    resource = conn.execute(
        "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ?",
        (resource_id, organisation_id)
    ).fetchone()
    if not resource:
        conn.close()
        return jsonify({"error": "Resource not found"}), 404

    existing_ob = conn.execute(
        """
        SELECT transaction_id FROM transactions
        WHERE depot_id = ? AND resource_id = ? AND transaction_type = 'OpeningBalance'
        LIMIT 1
        """,
        (depot_id, resource_id)
    ).fetchone()
    if existing_ob:
        conn.close()
        return jsonify({
            "error": "OPENING_BALANCE_ALREADY_SET",
            "message": (
                f"An opening balance has already been entered for "
                f"'{resource['name']}' at this depot. "
                "Use a regular transaction to adjust the balance."
            ),
            "resource_id": resource_id,
            "resource_name": resource["name"],
            "existing_transaction_id": existing_ob["transaction_id"],
        }), 400

    transaction_id = make_id("txn")
    ledger_entry_id = make_id("led")
    created_at = now_iso()
    ob_reference_number, ob_org_seq = generate_transaction_reference(conn, organisation_id)

    conn.execute(
        """
        INSERT INTO transactions (
            transaction_id,
            organisation_id,
            depot_id,
            transaction_type,
            resource_id,
            quantity,
            direction,
            status,
            approval_reason_code,
            approval_reason_text,
            created_at,
            posted_at,
            reference_number,
            org_sequence_number
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transaction_id,
            organisation_id,
            depot_id,
            "OpeningBalance",
            resource_id,
            quantity,
            "IN",
            "POSTED",
            None,
            None,
            created_at,
            created_at,
            ob_reference_number,
            ob_org_seq
        )
    )

    conn.execute(
        """
        INSERT INTO ledger_entries (
            ledger_entry_id,
            transaction_id,
            organisation_id,
            depot_id,
            resource_id,
            quantity_delta,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ledger_entry_id,
            transaction_id,
            organisation_id,
            depot_id,
            resource_id,
            quantity,
            created_at
        )
    )

    conn.execute(
        """
        INSERT INTO balance_projection (
            balance_projection_id,
            organisation_id,
            depot_id,
            resource_id,
            current_quantity,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(organisation_id, depot_id, resource_id)
        DO UPDATE SET current_quantity = excluded.current_quantity, updated_at = excluded.updated_at
        """,
        (
            make_id("bal"),
            organisation_id,
            depot_id,
            resource_id,
            quantity,
            created_at
        )
    )

    conn.execute(
        "UPDATE depots SET opening_balance_used = 1 WHERE depot_id = ?",
        (depot_id,)
    )

    update_pending_entries_ready_for_opening_balance(conn, organisation_id, depot_id)

    audit_event(
        conn,
        entity_type="Depot",
        entity_id=depot_id,
        action="OPENING_BALANCE_SET",
        summary=f"Opening balance set for depot with quantity {quantity}",
        organisation_id=organisation_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "transaction_id": transaction_id,
        "status": "POSTED",
        "opening_balance_used": True
    }), 201



# === REGULAR TRANSACTION PARTNER ADDRESS HELPERS START ===

def ensure_transaction_partner_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}

    if "partner_id" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN partner_id TEXT")

    if "partner_address_id" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN partner_address_id TEXT")


def get_partner_address_for_transaction(conn, partner_address_id, partner_id, organisation_id):
    if not partner_address_id or not partner_id or not organisation_id:
        return None

    ensure_partner_address_tables(conn)

    row = conn.execute(
        """
        SELECT *
        FROM partner_addresses
        WHERE partner_address_id = ?
          AND partner_id = ?
          AND organisation_id = ?
          AND is_active = 1
        """,
        (partner_address_id, partner_id, organisation_id)
    ).fetchone()

    return dict(row) if row else None


def get_default_partner_address_for_transaction(conn, partner_id, organisation_id, direction):
    if not partner_id or not organisation_id:
        return None

    ensure_partner_address_tables(conn)
    direction = (direction or "").strip().upper()

    if direction == "OUT":
        row = conn.execute(
            """
            SELECT *
            FROM partner_addresses
            WHERE partner_id = ?
              AND organisation_id = ?
              AND is_active = 1
              AND is_default_dispatch_site = 1
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (partner_id, organisation_id)
        ).fetchone()
        if row:
            return dict(row)

    if direction == "IN":
        row = conn.execute(
            """
            SELECT *
            FROM partner_addresses
            WHERE partner_id = ?
              AND organisation_id = ?
              AND is_active = 1
              AND is_default_receiving_site = 1
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (partner_id, organisation_id)
        ).fetchone()
        if row:
            return dict(row)

    row = conn.execute(
        """
        SELECT *
        FROM partner_addresses
        WHERE partner_id = ?
          AND organisation_id = ?
          AND is_active = 1
        ORDER BY is_primary DESC, updated_at DESC
        LIMIT 1
        """,
        (partner_id, organisation_id)
    ).fetchone()

    return dict(row) if row else None


def build_transaction_payload(conn, txn_row):
    d = dict(txn_row)
    d["partner_address"] = None

    if d.get("partner_id") and d.get("partner_address_id"):
        d["partner_address"] = get_partner_address_for_transaction(
            conn,
            d["partner_address_id"],
            d["partner_id"],
            d["organisation_id"],
        )

    return d


# === REGULAR TRANSACTION PARTNER ADDRESS HELPERS END ===


@app.post("/transactions")
def create_transaction():
    body = request.get_json(silent=True) or {}

    organisation_id = body.get("organisation_id")
    depot_id = body.get("depot_id")
    transaction_type = (body.get("transaction_type") or "").strip()
    resource_id = body.get("resource_id")
    quantity = body.get("quantity")
    direction = (body.get("direction") or "").strip().upper()
    submitted_by_display_name = (body.get("submitted_by_display_name") or "Unknown User").strip()
    submitted_by_user_id = body.get("submitted_by_user_id")
    partner_id = body.get("partner_id")
    partner_address_id = body.get("partner_address_id")
    unresolved_entity_note = (body.get("unresolved_entity_note") or "").strip() or None
    unresolved_entity_type = (body.get("unresolved_entity_type") or "").strip().upper() or None

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400
    if not depot_id:
        return jsonify({"error": "depot_id is required"}), 400
    if not transaction_type:
        return jsonify({"error": "transaction_type is required"}), 400
    if not resource_id and not unresolved_entity_note:
        return jsonify({"error": "resource_id is required (or provide unresolved_entity_note if entity is missing)"}), 400

    try:
        quantity = int(quantity)
    except Exception:
        return jsonify({"error": "quantity must be an integer greater than zero"}), 400

    if quantity <= 0:
        return jsonify({"error": "quantity must be an integer greater than zero"}), 400
    if direction not in ["IN", "OUT"]:
        return jsonify({"error": "direction must be IN or OUT"}), 400

    conn = get_conn()
    ensure_transaction_partner_columns(conn)
    ensure_partner_address_tables(conn)
    ensure_transaction_user_attribution_columns(conn)
    ensure_transaction_numbering_tables(conn)

    access_error = require_active_org_access(conn, organisation_id)
    if access_error:
        conn.close()
        return jsonify(access_error), 403

    user_access_error = require_user_org_operation_access(conn, submitted_by_user_id, organisation_id)
    if user_access_error:
        conn.close()
        return jsonify(user_access_error), 403

    submitted_by_display_name = get_transaction_submitter_display_snapshot(
        conn,
        submitted_by_user_id,
        submitted_by_display_name,
    )

    org = conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchone()
    if not org:
        conn.close()
        return jsonify({"error": "Organisation not found"}), 404

    depot = conn.execute(
        "SELECT * FROM depots WHERE depot_id = ? AND organisation_id = ?",
        (depot_id, organisation_id)
    ).fetchone()
    if not depot:
        conn.close()
        return jsonify({"error": "Depot not found"}), 404

    resource = None
    if resource_id:
        resource = conn.execute(
            "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ?",
            (resource_id, organisation_id)
        ).fetchone()
        if not resource and not unresolved_entity_note:
            conn.close()
            return jsonify({"error": "Resource not found"}), 404

    if not resource_id and unresolved_entity_note:
        resource_id = "UNRESOLVED"

    partner = None
    partner_address = None

    if partner_id and not unresolved_entity_note:
        partner = conn.execute(
            """
            SELECT *
            FROM partners
            WHERE partner_id = ?
              AND organisation_id = ?
              AND is_active = 1
            """,
            (partner_id, organisation_id)
        ).fetchone()

        if not partner:
            conn.close()
            return jsonify({"error": "Partner not found for this organisation"}), 404

        if partner_address_id:
            partner_address = get_partner_address_for_transaction(conn, partner_address_id, partner_id, organisation_id)
            if not partner_address:
                conn.close()
                return jsonify({"error": "Partner address not found for this partner"}), 404
        else:
            partner_address = get_default_partner_address_for_transaction(conn, partner_id, organisation_id, direction)
            partner_address_id = partner_address["partner_address_id"] if partner_address else None
    elif partner_id and unresolved_entity_note:
        # Partner ID provided but we're in unresolved mode — store it without strict validation
        partner_address_id = None

    transaction_id = make_id("txn")
    created_at = now_iso()
    reference_number, org_sequence_number = generate_transaction_reference(conn, organisation_id)

    insert_sql = """
        INSERT INTO transactions (
            transaction_id,
            organisation_id,
            depot_id,
            transaction_type,
            resource_id,
            quantity,
            direction,
            status,
            approval_reason_code,
            approval_reason_text,
            partner_id,
            partner_address_id,
            submitted_by_user_id,
            submitted_by_display_name,
            created_at,
            posted_at,
            reference_number,
            org_sequence_number,
            unresolved_entity_note,
            unresolved_entity_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    resource_name = resource["name"] if resource else None

    if unresolved_entity_note:
        conn.execute(
            insert_sql,
            (
                transaction_id, organisation_id, depot_id, transaction_type, resource_id,
                quantity, direction, "PENDING_APPROVAL", "MISSING_ENTITY",
                unresolved_entity_note, partner_id,
                None, submitted_by_user_id, submitted_by_display_name,
                created_at, None, reference_number, org_sequence_number,
                unresolved_entity_note, unresolved_entity_type,
            )
        )

        pending_entry_id = create_pending_entry(
            conn=conn,
            organisation_id=organisation_id,
            entry_type="Transaction",
            source_record_id=transaction_id,
            source_module="Transactions",
            submitted_by_display_name=submitted_by_display_name,
            related_entity_type=unresolved_entity_type or "UNKNOWN",
            related_entity_id=None,
            related_entity_name=None,
            reason_code="MISSING_ENTITY",
            reason_text=unresolved_entity_note,
            direct_action_type="ResolveEntity",
            direct_action_target_id=transaction_id,
            direct_action_label="Resolve Missing Entity",
            can_approve_now=False,
            can_reject_now=True,
            resource_id=resource_id if resource_id != "UNRESOLVED" else None,
            resource_name=resource_name,
            status="AWAITING_FIX",
        )

        audit_event(
            conn,
            entity_type="Transaction",
            entity_id=transaction_id,
            action="PENDING_APPROVAL",
            summary=(
                f"{submitted_by_display_name} submitted transaction with unresolved entity: "
                f"{unresolved_entity_note}"
            ),
            organisation_id=organisation_id,
        )

        conn.commit()
        conn.close()

        return jsonify({
            "transaction_id": transaction_id,
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "depot_name": depot["name"],
            "transaction_type": transaction_type,
            "resource_id": resource_id,
            "resource_name": resource_name,
            "quantity": quantity,
            "direction": direction,
            "status": "PENDING_APPROVAL",
            "approval_reason_code": "MISSING_ENTITY",
            "approval_reason_text": unresolved_entity_note,
            "unresolved_entity_note": unresolved_entity_note,
            "unresolved_entity_type": unresolved_entity_type,
            "pending_entry_id": pending_entry_id,
            "partner_id": partner_id,
            "submitted_by_user_id": submitted_by_user_id,
            "submitted_by_display_name": submitted_by_display_name,
            "reference_number": reference_number,
            "org_sequence_number": org_sequence_number,
            "message": "Transaction saved. Your Org Admin has been notified to resolve the missing entity and complete the transaction.",
        }), 201

    if depot["opening_balance_used"] == 0:
        conn.execute(
            insert_sql,
            (
                transaction_id, organisation_id, depot_id, transaction_type, resource_id,
                quantity, direction, "PENDING_APPROVAL", "NIL_OPENING_BALANCE",
                "Opening balance has not been set for this entity.", partner_id,
                partner_address_id, submitted_by_user_id, submitted_by_display_name,
                created_at, None, reference_number, org_sequence_number,
                None, None,
            )
        )

        pending_entry_id = create_pending_entry(
            conn=conn,
            organisation_id=organisation_id,
            entry_type="Transaction",
            source_record_id=transaction_id,
            source_module="Transactions",
            submitted_by_display_name=submitted_by_display_name,
            related_entity_type="Depot",
            related_entity_id=depot_id,
            related_entity_name=depot["name"],
            reason_code="NIL_OPENING_BALANCE",
            reason_text="Opening balance has not been set for this entity.",
            direct_action_type="GoToEntityProfile",
            direct_action_target_id=depot_id,
            direct_action_label="Enter Opening Balance",
            can_approve_now=False,
            can_reject_now=True,
            resource_id=resource_id,
            resource_name=resource_name,
            status="AWAITING_FIX"
        )

        audit_event(
            conn,
            entity_type="Transaction",
            entity_id=transaction_id,
            action="PENDING_APPROVAL",
            summary="Transaction saved pending approval due to nil opening balance",
            organisation_id=organisation_id
        )

        conn.commit()
        conn.close()

        return jsonify({
            "transaction_id": transaction_id,
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "depot_name": depot["name"],
            "transaction_type": transaction_type,
            "resource_id": resource_id,
            "resource_name": resource_name,
            "quantity": quantity,
            "direction": direction,
            "status": "PENDING_APPROVAL",
            "approval_reason_code": "NIL_OPENING_BALANCE",
            "approval_reason_text": "Opening balance has not been set for this entity.",
            "pending_entry_id": pending_entry_id,
            "partner_id": partner_id,
            "partner_name": partner["name"] if partner else None,
            "partner_address_id": partner_address_id,
            "partner_address": partner_address,
            "submitted_by_user_id": submitted_by_user_id,
            "submitted_by_display_name": submitted_by_display_name,
            "reference_number": reference_number,
            "org_sequence_number": org_sequence_number,
            "message": "Opening balance has not been set for this entity. This transaction cannot be processed automatically and has been sent to your Org Admin for approval."
        }), 201

    conn.execute(
        insert_sql,
        (
            transaction_id, organisation_id, depot_id, transaction_type, resource_id,
            quantity, direction, "DRAFT", None, None, partner_id,
            partner_address_id, submitted_by_user_id, submitted_by_display_name,
            created_at, None, reference_number, org_sequence_number,
            None, None,
        )
    )

    partner_suffix = f" for partner {partner['name']}" if partner else ""
    site_suffix = f" at site {partner_address['label']}" if partner_address else ""

    audit_event(
        conn,
        entity_type="Transaction",
        entity_id=transaction_id,
        action="CREATE",
        summary=f"Created transaction: {transaction_type} {quantity} {direction}{partner_suffix}{site_suffix}",
        organisation_id=organisation_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "transaction_id": transaction_id,
        "organisation_id": organisation_id,
        "depot_id": depot_id,
        "depot_name": depot["name"],
        "transaction_type": transaction_type,
        "resource_id": resource_id,
        "resource_name": resource_name,
        "quantity": quantity,
        "direction": direction,
        "status": "DRAFT",
        "partner_id": partner_id,
        "partner_name": partner["name"] if partner else None,
        "partner_address_id": partner_address_id,
        "partner_address": partner_address,
        "submitted_by_user_id": submitted_by_user_id,
        "submitted_by_display_name": submitted_by_display_name,
        "reference_number": reference_number,
        "org_sequence_number": org_sequence_number
    }), 201


@app.get("/organisations/<organisation_id>/transactions")
def list_transactions(organisation_id):
    status = request.args.get("status")
    direction = (request.args.get("direction") or "").strip().upper() or None
    reference_number_filter = (request.args.get("reference_number") or "").strip() or None

    conn = get_conn()
    ensure_transaction_partner_columns(conn)
    ensure_partner_address_tables(conn)
    ensure_transaction_numbering_tables(conn)

    sql = """
        SELECT
            t.transaction_id,
            t.organisation_id,
            o.name AS organisation_name,
            t.depot_id,
            d.name AS depot_name,
            t.transaction_type,
            t.resource_id,
            r.name AS resource_name,
            r.unit_type,
            t.quantity,
            t.direction,
            t.status,
            t.approval_reason_code,
            t.approval_reason_text,
            t.partner_id,
            p.name AS partner_name,
            t.partner_address_id,
            t.submitted_by_user_id,
            t.submitted_by_display_name,
            t.created_at,
            t.posted_at,
            t.reference_number,
            t.org_sequence_number
        FROM transactions t
        LEFT JOIN organisations o ON o.organisation_id = t.organisation_id
        LEFT JOIN depots d ON d.depot_id = t.depot_id
        LEFT JOIN resources r ON r.resource_id = t.resource_id
        LEFT JOIN partners p ON p.partner_id = t.partner_id
        WHERE t.organisation_id = ?
    """
    params = [organisation_id]

    if status:
        sql += " AND t.status = ?"
        params.append(status)

    if direction:
        sql += " AND t.direction = ?"
        params.append(direction)

    if reference_number_filter:
        sql += " AND t.reference_number = ?"
        params.append(reference_number_filter)

    sql += " ORDER BY t.org_sequence_number ASC, t.created_at DESC"

    rows = conn.execute(sql, params).fetchall()
    items = [build_transaction_payload(conn, row) for row in rows]

    conn.close()

    return jsonify({
        "organisation_id": organisation_id,
        "count": len(items),
        "items": items
    }), 200


@app.get("/transactions/<transaction_id>")
def get_transaction(transaction_id):
    organisation_id = request.args.get("organisation_id")

    conn = get_conn()
    ensure_transaction_partner_columns(conn)
    ensure_partner_address_tables(conn)

    row = conn.execute(
        """
        SELECT
            t.transaction_id,
            t.organisation_id,
            o.name AS organisation_name,
            t.depot_id,
            d.name AS depot_name,
            t.transaction_type,
            t.resource_id,
            r.name AS resource_name,
            r.unit_type,
            t.quantity,
            t.direction,
            t.status,
            t.approval_reason_code,
            t.approval_reason_text,
            t.partner_id,
            p.name AS partner_name,
            t.partner_address_id,
            t.submitted_by_user_id,
            t.submitted_by_display_name,
            t.created_at,
            t.posted_at,
            t.reference_number,
            t.org_sequence_number
        FROM transactions t
        LEFT JOIN organisations o ON o.organisation_id = t.organisation_id
        LEFT JOIN depots d ON d.depot_id = t.depot_id
        LEFT JOIN resources r ON r.resource_id = t.resource_id
        LEFT JOIN partners p ON p.partner_id = t.partner_id
        WHERE t.transaction_id = ?
        """,
        (transaction_id,)
    ).fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "Transaction not found"}), 404

    d = build_transaction_payload(conn, row)

    if organisation_id and d["organisation_id"] != organisation_id:
        conn.close()
        return jsonify({"error": "Organisation does not own this transaction"}), 403

    conn.close()

    return jsonify(d), 200


@app.post("/transactions/<transaction_id>/post")
def post_transaction(transaction_id):
    conn = get_conn()

    txn = conn.execute(
        "SELECT * FROM transactions WHERE transaction_id = ?",
        (transaction_id,)
    ).fetchone()

    if not txn:
        conn.close()
        return jsonify({"error": "Transaction not found"}), 404

    if txn["status"] == "POSTED":
        conn.close()
        return jsonify({
            "transaction_id": transaction_id,
            "status": "POSTED",
            "message": "Transaction already posted"
        }), 200

    if txn["status"] == "PENDING_APPROVAL":
        conn.close()
        return jsonify({
            "error": "Transaction is pending approval and cannot be posted yet"
        }), 400

    post_transaction_to_ledger(conn, txn)

    conn.commit()
    conn.close()

    return jsonify({
        "transaction_id": transaction_id,
        "status": "POSTED"
    }), 200


@app.patch("/transactions/<transaction_id>/resolve-entity")
def resolve_transaction_entity(transaction_id):
    """
    Org Admin resolves a MISSING_ENTITY pending transaction.

    Once the missing resource / partner has been created, call this route
    with the correct IDs. The transaction is updated and immediately posted
    to the ledger. No dead ends — the field worker's transaction completes.
    """
    current_user = g.current_user
    _ORG_ADMIN_ROLES = {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
    if current_user["role"] not in _ORG_ADMIN_ROLES:
        return jsonify({"error": "Only Org Admin or above can resolve entity issues"}), 403

    body = request.get_json(silent=True) or {}
    new_resource_id = (body.get("resource_id") or "").strip() or None
    new_partner_id = (body.get("partner_id") or "").strip() or None
    review_notes = (body.get("review_notes") or "").strip() or None

    conn = get_conn()
    ensure_transaction_partner_columns(conn)
    ensure_transaction_numbering_tables(conn)

    txn = conn.execute(
        "SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,)
    ).fetchone()

    if not txn:
        conn.close()
        return jsonify({"error": "Transaction not found"}), 404

    if txn["approval_reason_code"] != "MISSING_ENTITY":
        conn.close()
        return jsonify({"error": "Transaction is not pending due to a missing entity"}), 400

    if txn["status"] != "PENDING_APPROVAL":
        conn.close()
        return jsonify({"error": f"Transaction status is '{txn['status']}' — only PENDING_APPROVAL transactions can be resolved"}), 400

    organisation_id = txn["organisation_id"]

    # Validate new resource if provided (required when current resource_id is the sentinel)
    if txn["resource_id"] == "UNRESOLVED":
        if not new_resource_id:
            conn.close()
            return jsonify({"error": "resource_id is required — the original transaction had no resource"}), 400
        resource = conn.execute(
            "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ? AND is_active = 1",
            (new_resource_id, organisation_id),
        ).fetchone()
        if not resource:
            conn.close()
            return jsonify({"error": "Resource not found or inactive"}), 404
    else:
        resource = conn.execute(
            "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ?",
            (txn["resource_id"], organisation_id),
        ).fetchone()
        new_resource_id = txn["resource_id"]

    # Validate new partner if provided
    if new_partner_id:
        partner = conn.execute(
            "SELECT * FROM partners WHERE partner_id = ? AND organisation_id = ? AND is_active = 1",
            (new_partner_id, organisation_id),
        ).fetchone()
        if not partner:
            conn.close()
            return jsonify({"error": "Partner not found or inactive"}), 404
    else:
        new_partner_id = txn["partner_id"]
        partner = None

    ts = now_iso()

    conn.execute(
        """UPDATE transactions
           SET resource_id = ?, partner_id = ?,
               unresolved_entity_note = NULL, unresolved_entity_type = NULL,
               approval_reason_code = NULL, approval_reason_text = NULL
           WHERE transaction_id = ?""",
        (new_resource_id, new_partner_id, transaction_id),
    )

    # Re-fetch with the corrected resource_id before posting to ledger
    txn_updated = conn.execute(
        "SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,)
    ).fetchone()
    post_transaction_to_ledger(conn, txn_updated)

    # Resolve the associated pending entry
    conn.execute(
        """UPDATE pending_approval_entries
           SET status = 'RESOLVED', updated_at = ?
           WHERE source_record_id = ? AND reason_code = 'MISSING_ENTITY'""",
        (ts, transaction_id),
    )

    audit_event(
        conn,
        entity_type="Transaction",
        entity_id=transaction_id,
        action="ENTITY_RESOLVED",
        summary=(
            f"{current_user['display_name']} resolved missing entity on transaction {transaction_id}. "
            f"Resource: {resource['name'] if resource else new_resource_id}. "
            f"Notes: {review_notes or 'none'}."
        ),
        organisation_id=organisation_id,
    )

    conn.commit()
    conn.close()

    return jsonify({
        "transaction_id": transaction_id,
        "status": "POSTED",
        "resource_id": new_resource_id,
        "resource_name": resource["name"] if resource else None,
        "partner_id": new_partner_id,
        "reviewed_by": current_user["display_name"],
        "review_notes": review_notes,
        "message": "Entity resolved. Transaction posted to ledger.",
    }), 200


@app.post("/pending-approval/<pending_entry_id>/approve")
def approve_pending_entry(pending_entry_id):
    conn = get_conn()

    pending = conn.execute(
        "SELECT * FROM pending_approval_entries WHERE pending_entry_id = ?",
        (pending_entry_id,)
    ).fetchone()

    if not pending:
        conn.close()
        return jsonify({"error": "Pending approval entry not found"}), 404

    if pending["status"] != "READY_TO_APPROVE":
        conn.close()
        return jsonify({"error": "Pending entry is not ready to approve"}), 400

    if pending["entry_type"] == "Transaction":
        txn = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?",
            (pending["source_record_id"],)
        ).fetchone()

        if not txn:
            conn.close()
            return jsonify({"error": "Source transaction not found"}), 404

        post_transaction_to_ledger(conn, txn)

        conn.execute(
            """
            UPDATE pending_approval_entries
            SET status = ?, updated_at = ?
            WHERE pending_entry_id = ?
            """,
            ("RESOLVED", now_iso(), pending_entry_id)
        )

        audit_event(
            conn,
            entity_type="PendingApprovalEntry",
            entity_id=pending_entry_id,
            action="APPROVE",
            summary="Pending approval entry approved and resolved",
            organisation_id=pending["organisation_id"]
        )

        conn.commit()
        conn.close()

        return jsonify({
            "pending_entry_id": pending_entry_id,
            "status": "RESOLVED",
            "transaction_status": "POSTED"
        }), 200

    conn.close()
    return jsonify({"error": "Unsupported entry type for approval"}), 400


@app.post("/pending-approval/<pending_entry_id>/reject")
def reject_pending_entry(pending_entry_id):
    body = request.get_json(silent=True) or {}
    rejection_reason_code = (body.get("rejection_reason_code") or "REJECTED_BY_ADMIN").strip()
    rejection_reason_text = (body.get("rejection_reason_text") or "Rejected by Org Admin").strip()

    conn = get_conn()

    pending = conn.execute(
        "SELECT * FROM pending_approval_entries WHERE pending_entry_id = ?",
        (pending_entry_id,)
    ).fetchone()

    if not pending:
        conn.close()
        return jsonify({"error": "Pending approval entry not found"}), 404

    conn.execute(
        """
        UPDATE pending_approval_entries
        SET status = ?, rejection_reason_code = ?, rejection_reason_text = ?, updated_at = ?
        WHERE pending_entry_id = ?
        """,
        ("REJECTED", rejection_reason_code, rejection_reason_text, now_iso(), pending_entry_id)
    )

    if pending["entry_type"] == "Transaction":
        conn.execute(
            """
            UPDATE transactions
            SET status = ?, approval_reason_code = ?, approval_reason_text = ?
            WHERE transaction_id = ?
            """,
            ("REJECTED", rejection_reason_code, rejection_reason_text, pending["source_record_id"])
        )
    elif pending["entry_type"] == "ResourceRequest":
        conn.execute(
            """
            UPDATE resource_requests
            SET status = ?, rejection_reason_code = ?, rejection_reason_text = ?, updated_at = ?
            WHERE resource_request_id = ?
            """,
            ("REJECTED", rejection_reason_code, rejection_reason_text, now_iso(), pending["source_record_id"])
        )
    elif pending["entry_type"] == "BrandRequest":
        conn.execute(
            """
            UPDATE brand_requests
            SET status = ?, rejection_reason_code = ?, rejection_reason_text = ?, updated_at = ?
            WHERE brand_request_id = ?
            """,
            ("REJECTED", rejection_reason_code, rejection_reason_text, now_iso(), pending["source_record_id"])
        )
    elif pending["entry_type"] == "CategoryRequest":
        conn.execute(
            """
            UPDATE category_requests
            SET status = ?, rejection_reason_code = ?, rejection_reason_text = ?, updated_at = ?
            WHERE category_request_id = ?
            """,
            ("REJECTED", rejection_reason_code, rejection_reason_text, now_iso(), pending["source_record_id"])
        )

    audit_event(
        conn,
        entity_type="PendingApprovalEntry",
        entity_id=pending_entry_id,
        action="REJECT",
        summary=f"Pending approval entry rejected: {rejection_reason_code} / {rejection_reason_text}",
        organisation_id=pending["organisation_id"]
    )

    conn.commit()
    conn.close()

    return jsonify({
        "pending_entry_id": pending_entry_id,
        "status": "REJECTED",
        "rejection_reason_code": rejection_reason_code,
        "rejection_reason_text": rejection_reason_text
    }), 200

@app.get("/stock")
def get_stock():
    organisation_id = request.args.get("organisation_id")
    depot_id = request.args.get("depot_id")
    resource_id = request.args.get("resource_id")

    conn = get_conn()

    sql = """
        SELECT
            bp.organisation_id,
            o.name AS organisation_name,
            bp.depot_id,
            d.name AS depot_name,
            bp.resource_id,
            r.name AS resource_name,
            r.resource_type,
            r.unit_type,
            bp.current_quantity,
            bp.updated_at
        FROM balance_projection bp
        LEFT JOIN organisations o ON o.organisation_id = bp.organisation_id
        LEFT JOIN depots d ON d.depot_id = bp.depot_id
        LEFT JOIN resources r ON r.resource_id = bp.resource_id
        WHERE 1=1
    """
    params = []

    if organisation_id:
        sql += " AND bp.organisation_id = ?"
        params.append(organisation_id)

    if depot_id:
        sql += " AND bp.depot_id = ?"
        params.append(depot_id)

    if resource_id:
        sql += " AND bp.resource_id = ?"
        params.append(resource_id)

    sql += " ORDER BY o.name, d.name, r.name"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    return jsonify({
        "count": len(rows),
        "items": [dict(r) for r in rows]
    }), 200


@app.get("/audit")
def get_audit():
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT *
        FROM audit_events
        ORDER BY created_at DESC, event_id DESC
        """
    ).fetchall()
    conn.close()

    return jsonify([dict(r) for r in rows]), 200


@app.get("/pending-approval")
def get_pending_approval():
    organisation_id = request.args.get("organisation_id")
    entry_type = request.args.get("entry_type")

    conn = get_conn()

    sql = """
        SELECT *
        FROM pending_approval_entries
        WHERE status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
    """
    params = []

    if organisation_id:
        sql += " AND organisation_id = ?"
        params.append(organisation_id)

    if entry_type:
        sql += " AND entry_type = ?"
        params.append(entry_type)

    sql += " ORDER BY created_at DESC, pending_entry_id DESC"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    return jsonify({
        "count": len(rows),
        "items": [dict(r) for r in rows]
    }), 200


@app.get("/pending-approval-history")
def get_pending_approval_history():
    organisation_id = request.args.get("organisation_id")
    entry_type = request.args.get("entry_type")

    conn = get_conn()

    sql = """
        SELECT *
        FROM pending_approval_entries
        WHERE status IN ('RESOLVED', 'REJECTED')
    """
    params = []

    if organisation_id:
        sql += " AND organisation_id = ?"
        params.append(organisation_id)

    if entry_type:
        sql += " AND entry_type = ?"
        params.append(entry_type)

    sql += " ORDER BY updated_at DESC, created_at DESC, pending_entry_id DESC"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    return jsonify({
        "count": len(rows),
        "items": [dict(r) for r in rows]
    }), 200

@app.get("/pending-approval/<pending_entry_id>")
def get_pending_approval_detail(pending_entry_id):
    conn = get_conn()
    row = conn.execute(
        """
        SELECT *
        FROM pending_approval_entries
        WHERE pending_entry_id = ?
        """,
        (pending_entry_id,)
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "Pending approval entry not found"}), 404

    return jsonify(dict(row)), 200



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

# === USER ENFORCED TRANSACTION ACCESS V0.1 START ===

def require_user_org_operation_access(conn, user_id, organisation_id):
    if not user_id:
        return None

    ensure_user_access_tables(conn)

    user = get_user_account(conn, user_id)

    if not user:
        return {
            "error": "Submitting user not found",
            "submitted_by_user_id": user_id,
        }

    if user["role"] not in ("SUPER_GLOBAL_ADMIN", "GLOBAL_ADMIN") and user["organisation_id"] != organisation_id:
        return {
            "error": "Submitting user does not belong to this organisation",
            "submitted_by_user_id": user_id,
            "organisation_id": organisation_id,
            "user_organisation_id": user["organisation_id"],
        }

    policy = build_user_access_policy(conn, user)

    if not policy["can_use_org_operations"]:
        return {
            "error": "Submitting user does not have permission to create operational transactions",
            "submitted_by_user_id": user_id,
            "organisation_id": organisation_id,
            "access_policy": policy,
        }

    return None

# === USER ENFORCED TRANSACTION ACCESS V0.1 END ===

# === TRANSACTION USER ATTRIBUTION V0.1 START ===

def ensure_transaction_user_attribution_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}

    if "submitted_by_user_id" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN submitted_by_user_id TEXT")

    if "submitted_by_display_name" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN submitted_by_display_name TEXT")


# === TRANSACTION NUMBERING V0.1 START ===

def ensure_transaction_numbering_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS transaction_sequences (
        sequence_key TEXT PRIMARY KEY,
        next_value INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    )
    """)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    if "reference_number" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN reference_number TEXT")
    if "org_sequence_number" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN org_sequence_number INTEGER")


def generate_transaction_reference(conn, organisation_id):
    from datetime import datetime, UTC
    year = datetime.now(UTC).year

    def next_seq(key):
        row = conn.execute(
            "SELECT next_value FROM transaction_sequences WHERE sequence_key = ?",
            (key,)
        ).fetchone()
        if row:
            val = row["next_value"]
            conn.execute(
                "UPDATE transaction_sequences SET next_value = ?, updated_at = ? WHERE sequence_key = ?",
                (val + 1, now_iso(), key)
            )
        else:
            val = 1
            conn.execute(
                "INSERT INTO transaction_sequences (sequence_key, next_value, updated_at) VALUES (?, ?, ?)",
                (key, 2, now_iso())
            )
        return val

    global_seq = next_seq(f"global_{year}")
    org_seq = next_seq(f"org_{organisation_id}")
    return f"PP-{year}-{global_seq:06d}", org_seq

# === TRANSACTION NUMBERING V0.1 END ===


def get_transaction_submitter_display_snapshot(conn, submitted_by_user_id, fallback_display_name):
    fallback_display_name = (fallback_display_name or "Unknown User").strip() or "Unknown User"

    if not submitted_by_user_id:
        return fallback_display_name

    ensure_user_access_tables(conn)

    user = get_user_account(conn, submitted_by_user_id)

    if not user:
        return fallback_display_name

    return (user["display_name"] or user["email"] or fallback_display_name).strip() or fallback_display_name

# === TRANSACTION USER ATTRIBUTION V0.1 END ===

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
