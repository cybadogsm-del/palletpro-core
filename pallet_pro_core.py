from flask import Flask, request, jsonify
import sqlite3
import uuid
from datetime import datetime

app = Flask(__name__)

DB = "pallet_pro.db"


def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.utcnow().isoformat()


def audit_event(conn, entity_type, entity_id, action, summary, organisation_id=None):
    event_id = make_id("audit")
    conn.execute(
        """
        INSERT INTO audit_events (
            event_id,
            organisation_id,
            entity_type,
            entity_id,
            action,
            summary,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            organisation_id,
            entity_type,
            entity_id,
            action,
            summary,
            now_iso()
        )
    )


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

    conn.commit()
    conn.close()


init_db()


@app.get("/")
def root():
    return jsonify({
        "status": "Pallet Pro Core Running",
        "engine": "resources -> brands -> categories -> brand requests -> category requests -> resource requests -> partners -> transactions -> ledger -> stock -> audit -> pending approval -> depot profile -> admin dashboard"
    })


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

    return jsonify({
        "partner_id": partner_id,
        "organisation_id": partner["organisation_id"],
        "partner_name": partner["name"],
        "count": len(rows),
        "items": [dict(r) for r in rows]
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

    return jsonify(dict(row)), 200




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
        partner_addresses.append(a)

    d["partner_address_count"] = len(partner_addresses)
    d["partner_addresses"] = partner_addresses
    d["primary_partner_address"] = next((a for a in partner_addresses if a["is_primary"]), None)
    d["default_dispatch_partner_address"] = next((a for a in partner_addresses if a["is_default_dispatch_site"]), None)
    d["default_receiving_partner_address"] = next((a for a in partner_addresses if a["is_default_receiving_site"]), None)

    return jsonify(d), 200


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

    st = conn.execute(
        "SELECT * FROM shared_transactions WHERE shared_transaction_id = ?",
        (shared_transaction_id,)
    ).fetchone()

    if not st:
        conn.close()
        return jsonify({"error": "Shared transaction not found"}), 404

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
        "qr_scan_path": f"/qr-handoff/{qr_token_id}/scan"
    }), 201


@app.get("/qr-handoff/<qr_token_id>")
def get_qr_handoff_token(qr_token_id):
    conn = get_conn()
    ensure_shared_transaction_tables(conn)
    ensure_qr_token_tables(conn)

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
        WHERE q.qr_token_id = ?
        """,
        (qr_token_id,)
    ).fetchone()

    conn.close()

    if not row:
        return jsonify({"error": "QR handoff token not found"}), 404

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
        "counterparty_org_id": d["counterparty_org_id"],
        "counterparty_org_name": d["counterparty_org_name"],
        "resource_name": d["resource_name"],
        "unit_type": d["unit_type"],
        "quantity": d["quantity"],
        "proposed_quantity": d["proposed_quantity"],
        "reference_number": d["reference_number"],
        "proposed_reference_number": d["proposed_reference_number"],
        "correction_reason_text": d["correction_reason_text"],
        "correction_proposed_by_display_name": d["correction_proposed_by_display_name"]
    }), 200


@app.post("/shared-transactions")
def create_shared_transaction():
    body = request.get_json(silent=True) or {}
    origin_org_id = body.get("origin_org_id")
    counterparty_org_id = body.get("counterparty_org_id")
    origin_partner_id = body.get("origin_partner_id")
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
            reference_number
        )
    ).fetchone()

    if likely_duplicate:
        conn.close()
        return jsonify({
            "error": "A likely matching shared transaction already exists",
            "shared_transaction_id": likely_duplicate["shared_transaction_id"],
            "status": likely_duplicate["shared_status"]
        }), 409

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
            ts
        )
    )

    record_shared_transaction_event(
        conn=conn,
        shared_transaction_id=shared_transaction_id,
        organisation_id=origin_org_id,
        actor_org_role="DISPATCHING",
        action="CREATE",
        summary=f"Shared transaction created for {quantity} {origin_resource['name']} from {origin_org['name']} to {counterparty_org['name']}",
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
        "origin_resource_id": origin_resource_id,
        "resource_name": origin_resource["name"],
        "unit_type": origin_resource["unit_type"],
        "quantity": quantity,
        "movement_type": movement_type,
        "reference_number": reference_number,
        "created_by_display_name": created_by_display_name
    }), 201


@app.get("/organisations/<organisation_id>/shared-transactions")
def list_shared_transactions(organisation_id):
    lane = (request.args.get("lane") or "all").strip().lower()
    status = request.args.get("status")

    conn = get_conn()
    ensure_shared_transaction_tables(conn)

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

    conn.close()

    d = dict(row)

    if organisation_id:
        if organisation_id == d["origin_org_id"]:
            d["perspective_role"] = "DISPATCHING"
        elif organisation_id == d["counterparty_org_id"]:
            d["perspective_role"] = "RECEIVING"

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

    depot = conn.execute(
        "SELECT * FROM depots WHERE depot_id = ? AND organisation_id = ?",
        (depot_id, organisation_id)
    ).fetchone()
    if not depot:
        conn.close()
        return jsonify({"error": "Depot not found"}), 404

    if depot["opening_balance_used"] == 1:
        conn.close()
        return jsonify({"error": "Opening balance has already been used for this depot"}), 400

    resource = conn.execute(
        "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ?",
        (resource_id, organisation_id)
    ).fetchone()
    if not resource:
        conn.close()
        return jsonify({"error": "Resource not found"}), 404

    transaction_id = make_id("txn")
    ledger_entry_id = make_id("led")
    created_at = now_iso()

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
            posted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            created_at
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

    if not organisation_id:
        return jsonify({"error": "organisation_id is required"}), 400
    if not depot_id:
        return jsonify({"error": "depot_id is required"}), 400
    if not transaction_type:
        return jsonify({"error": "transaction_type is required"}), 400
    if not resource_id:
        return jsonify({"error": "resource_id is required"}), 400
    if not isinstance(quantity, int) or quantity <= 0:
        return jsonify({"error": "quantity must be an integer greater than zero"}), 400
    if direction not in ["IN", "OUT"]:
        return jsonify({"error": "direction must be IN or OUT"}), 400

    conn = get_conn()

    org = conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?",
        (organisation_id,)
    ).fetchone()
    if not org:
        conn.close()
        return jsonify({"error": "Organisation not found"}), 404

    depot = conn.execute(
        "SELECT * FROM depots WHERE depot_id = ?",
        (depot_id,)
    ).fetchone()
    if not depot:
        conn.close()
        return jsonify({"error": "Depot not found"}), 404

    resource = conn.execute(
        "SELECT * FROM resources WHERE resource_id = ?",
        (resource_id,)
    ).fetchone()
    if not resource:
        conn.close()
        return jsonify({"error": "Resource not found"}), 404

    transaction_id = make_id("txn")

    if depot["opening_balance_used"] == 0:
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
                posted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transaction_id,
                organisation_id,
                depot_id,
                transaction_type,
                resource_id,
                quantity,
                direction,
                "PENDING_APPROVAL",
                "NIL_OPENING_BALANCE",
                "Opening balance has not been set for this entity.",
                now_iso(),
                None
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
            resource_name=resource["name"],
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
            "status": "PENDING_APPROVAL",
            "approval_reason_code": "NIL_OPENING_BALANCE",
            "approval_reason_text": "Opening balance has not been set for this entity.",
            "pending_entry_id": pending_entry_id,
            "message": "Opening balance has not been set for this entity. This transaction cannot be processed automatically and has been sent to your Org Admin for approval."
        }), 201

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
            posted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transaction_id,
            organisation_id,
            depot_id,
            transaction_type,
            resource_id,
            quantity,
            direction,
            "DRAFT",
            None,
            None,
            now_iso(),
            None
        )
    )

    audit_event(
        conn,
        entity_type="Transaction",
        entity_id=transaction_id,
        action="CREATE",
        summary=f"Created transaction: {transaction_type} {quantity} {direction}",
        organisation_id=organisation_id
    )

    conn.commit()
    conn.close()

    return jsonify({
        "transaction_id": transaction_id,
        "organisation_id": organisation_id,
        "depot_id": depot_id,
        "resource_id": resource_id,
        "quantity": quantity,
        "direction": direction,
        "status": "DRAFT"
    }), 201


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
