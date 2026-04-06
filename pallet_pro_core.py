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
    CREATE TABLE IF NOT EXISTS resources (
        resource_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
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

    conn.commit()
    conn.close()


init_db()


@app.get("/")
def root():
    return jsonify({
        "status": "Pallet Pro Core Running",
        "engine": "resources -> transactions -> ledger -> stock -> audit -> pending approval -> resolve pending"
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


@app.post("/resources")
def create_resource():
    body = request.get_json(silent=True) or {}
    organisation_id = body.get("organisation_id")
    name = (body.get("name") or "").strip()
    resource_type = (body.get("resource_type") or "pallet").strip()
    unit_type = (body.get("unit_type") or "each").strip()

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

    resource_id = make_id("res")

    conn.execute(
        """
        INSERT INTO resources (
            resource_id,
            organisation_id,
            name,
            resource_type,
            unit_type,
            is_active,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            resource_id,
            organisation_id,
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
        "name": name,
        "resource_type": resource_type,
        "unit_type": unit_type,
        "is_active": True
    }), 201


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
        SET status = ?, updated_at = ?
        WHERE pending_entry_id = ?
        """,
        ("REJECTED", now_iso(), pending_entry_id)
    )

    if pending["entry_type"] == "Transaction":
        conn.execute(
            """
            UPDATE transactions
            SET status = ?, approval_reason_code = ?, approval_reason_text = ?
            WHERE transaction_id = ?
            """,
            ("REJECTED", "REJECTED_BY_ADMIN", "Rejected by Org Admin", pending["source_record_id"])
        )

    audit_event(
        conn,
        entity_type="PendingApprovalEntry",
        entity_id=pending_entry_id,
        action="REJECT",
        summary="Pending approval entry rejected",
        organisation_id=pending["organisation_id"]
    )

    conn.commit()
    conn.close()

    return jsonify({
        "pending_entry_id": pending_entry_id,
        "status": "REJECTED"
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
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT *
        FROM pending_approval_entries
        ORDER BY created_at DESC, pending_entry_id DESC
        """
    ).fetchall()
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
