"""
modules/transactions.py — Transactions brick

Covers:
  - Schema migration helpers
      ensure_transaction_partner_columns
      ensure_transaction_user_attribution_columns
      ensure_transaction_numbering_tables
  - Core helpers
      post_transaction_to_ledger
      generate_transaction_reference
      get_transaction_submitter_display_snapshot
      require_user_org_operation_access
      get_partner_address_for_transaction
      get_default_partner_address_for_transaction
      build_transaction_payload
  - Routes
      POST   /transactions                                      (create)
      GET    /organisations/<org_id>/transactions               (list)
      GET    /transactions/<transaction_id>                     (get)
      POST   /transactions/<transaction_id>/post                (post to ledger)
      PATCH  /transactions/<transaction_id>/resolve-entity      (resolve missing entity)
      POST   /pending-approval/<pending_entry_id>/approve
      POST   /pending-approval/<pending_entry_id>/reject
      GET    /stock
      GET    /audit
      GET    /pending-approval
      GET    /pending-approval-history
      GET    /pending-approval/<pending_entry_id>
"""

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso
from modules.operational_units import user_can_operate_unit
from modules.notifications import notify_user
from modules.partners import ensure_partner_address_tables
from modules.subscription_access import require_active_org_access

GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}


def _current_user_can_access_org(organisation_id):
    return (
        g.current_user.get("role") in GLOBAL_ADMIN_ROLES
        or g.current_user.get("user_org_id") == organisation_id
    )


def _require_org_admin_role():
    if g.current_user.get("role") not in {"ORG_ADMIN", *GLOBAL_ADMIN_ROLES}:
        return jsonify({"error": "Only Org Admin or above can review Pending Approval"}), 403
    return None


# ── Schema migration helpers ───────────────────────────────────────────────────

def ensure_transaction_partner_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    if "partner_id" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN partner_id TEXT")
    if "partner_address_id" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN partner_address_id TEXT")


def ensure_transaction_user_attribution_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    if "submitted_by_user_id" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN submitted_by_user_id TEXT")
    if "submitted_by_display_name" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN submitted_by_display_name TEXT")


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


def ensure_transaction_reference_columns(conn):
    """Add customer_reference and admin_orgs_reference columns if not present."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    if "customer_reference" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN customer_reference TEXT")
    if "admin_orgs_reference" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN admin_orgs_reference TEXT")


def ensure_transaction_operational_unit_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    additions = {
        "operational_unit_id": "TEXT",
        "operational_unit_kind_snapshot": "TEXT",
        "operational_unit_number_snapshot": "TEXT",
        "operational_unit_display_snapshot": "TEXT",
        "operational_unit_missing": "INTEGER DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {definition}")


def ensure_ledger_balance_operational_unit_columns(conn):
    ledger_cols = {row["name"] for row in conn.execute("PRAGMA table_info(ledger_entries)").fetchall()}
    ledger_additions = {
        "operational_unit_id": "TEXT",
        "operational_unit_kind_snapshot": "TEXT",
        "operational_unit_number_snapshot": "TEXT",
        "operational_unit_display_snapshot": "TEXT",
    }
    for name, definition in ledger_additions.items():
        if name not in ledger_cols:
            conn.execute(f"ALTER TABLE ledger_entries ADD COLUMN {name} {definition}")

    balance_cols = {row["name"] for row in conn.execute("PRAGMA table_info(balance_projection)").fetchall()}
    if "operational_unit_id" not in balance_cols:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS balance_projection_v2 (
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
        conn.execute("""
            INSERT OR IGNORE INTO balance_projection_v2 (
                balance_projection_id, organisation_id, depot_id, operational_unit_id,
                resource_id, current_quantity, updated_at
            )
            SELECT balance_projection_id, organisation_id, depot_id, '__NO_OPERATIONAL_UNIT__',
                   resource_id, current_quantity, updated_at
            FROM balance_projection
        """)
        conn.execute("DROP TABLE balance_projection")
        conn.execute("ALTER TABLE balance_projection_v2 RENAME TO balance_projection")


# ── Core helpers ───────────────────────────────────────────────────────────────

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


def _begin_immediate_if_needed(conn):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _txn_value(txn, key, default=None):
    if hasattr(txn, "keys") and key in txn.keys():
        return txn[key]
    if isinstance(txn, dict):
        return txn.get(key, default)
    return default


def post_transaction_to_ledger(conn, txn):
    ensure_ledger_balance_operational_unit_columns(conn)
    _begin_immediate_if_needed(conn)

    current_txn = conn.execute(
        "SELECT transaction_id FROM transactions WHERE transaction_id = ?",
        (txn["transaction_id"],),
    ).fetchone()

    if not current_txn:
        raise ValueError("Transaction not found")

    existing_ledger = conn.execute(
        "SELECT ledger_entry_id FROM ledger_entries WHERE transaction_id = ? LIMIT 1",
        (txn["transaction_id"],),
    ).fetchone()

    if existing_ledger:
        return False

    quantity_delta = txn["quantity"] if txn["direction"] == "IN" else -txn["quantity"]
    ledger_entry_id = make_id("led")
    operational_unit_id = _txn_value(txn, "operational_unit_id") or "__NO_OPERATIONAL_UNIT__"
    operational_unit_kind_snapshot = _txn_value(txn, "operational_unit_kind_snapshot")
    operational_unit_number_snapshot = _txn_value(txn, "operational_unit_number_snapshot")
    operational_unit_display_snapshot = _txn_value(txn, "operational_unit_display_snapshot")

    conn.execute(
        """
        INSERT INTO ledger_entries (
            ledger_entry_id,
            transaction_id,
            organisation_id,
            depot_id,
            resource_id,
            quantity_delta,
            created_at,
            operational_unit_id,
            operational_unit_kind_snapshot,
            operational_unit_number_snapshot,
            operational_unit_display_snapshot
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ledger_entry_id,
            txn["transaction_id"],
            txn["organisation_id"],
            txn["depot_id"],
            txn["resource_id"],
            quantity_delta,
            now_iso(),
            operational_unit_id,
            operational_unit_kind_snapshot,
            operational_unit_number_snapshot,
            operational_unit_display_snapshot,
        )
    )

    conn.execute(
        """
        INSERT INTO balance_projection (
            balance_projection_id,
            organisation_id,
            depot_id,
            operational_unit_id,
            resource_id,
            current_quantity,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(organisation_id, depot_id, operational_unit_id, resource_id)
        DO UPDATE SET
            current_quantity = current_quantity + excluded.current_quantity,
            updated_at = excluded.updated_at
        """,
        (
            make_id("bal"),
            txn["organisation_id"],
            txn["depot_id"],
            operational_unit_id,
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

    return True


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


# ── Route registration ─────────────────────────────────────────────────────────

def register_transaction_routes(
    app,
    create_pending_entry,
    ensure_user_access_tables,
    get_user_account,
    build_user_access_policy,
):

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

    def get_transaction_submitter_display_snapshot(conn, submitted_by_user_id, fallback_display_name):
        fallback_display_name = (fallback_display_name or "Unknown User").strip() or "Unknown User"

        if not submitted_by_user_id:
            return fallback_display_name

        ensure_user_access_tables(conn)

        user = get_user_account(conn, submitted_by_user_id)

        if not user:
            return fallback_display_name

        return (user["display_name"] or user["email"] or fallback_display_name).strip() or fallback_display_name


    @app.post("/transactions")
    def create_transaction():
        _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
        body = request.get_json(silent=True) or {}

        # Enforce org isolation: non-global-admins always write to their own org
        if g.current_user.get("role") not in _GLOBAL_ADMIN_ROLES:
            organisation_id = g.current_user.get("user_org_id")
        else:
            organisation_id = body.get("organisation_id")
        depot_id = body.get("depot_id")
        transaction_type = (body.get("transaction_type") or "").strip()
        resource_id = body.get("resource_id")
        quantity = body.get("quantity")
        direction = (body.get("direction") or "").strip().upper()
        if g.current_user.get("role") in _GLOBAL_ADMIN_ROLES:
            submitted_by_display_name = (body.get("submitted_by_display_name") or "Unknown User").strip()
            submitted_by_user_id = body.get("submitted_by_user_id")
        else:
            submitted_by_display_name = g.current_user.get("display_name") or "Unknown User"
            submitted_by_user_id = g.current_user.get("user_id")
        partner_id = body.get("partner_id")
        partner_address_id = body.get("partner_address_id")
        unresolved_entity_note = (body.get("unresolved_entity_note") or "").strip() or None
        unresolved_entity_type = (body.get("unresolved_entity_type") or "").strip().upper() or None
        customer_reference = (body.get("customer_reference") or "").strip()[:255] or None
        admin_orgs_reference = (body.get("admin_orgs_reference") or "").strip()[:255] or None
        operational_unit_id = (body.get("operational_unit_id") or "").strip() or None
        operational_unit_missing = bool(body.get("operational_unit_missing"))
        operational_unit_missing_note = (body.get("operational_unit_missing_note") or "").strip() or None
        operational_unit = None
        operational_unit_kind_snapshot = None
        operational_unit_number_snapshot = None
        operational_unit_display_snapshot = None

        if operational_unit_missing:
            if resource_id == "UNRESOLVED" and unresolved_entity_type not in (None, "OPERATIONAL_UNIT"):
                return jsonify({"error": "Resolve the missing entity before marking Fleet/Unit missing"}), 400
            unresolved_entity_note = operational_unit_missing_note or unresolved_entity_note or "@missing fleet/unit"
            unresolved_entity_type = "OPERATIONAL_UNIT"

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
        ensure_transaction_reference_columns(conn)
        ensure_transaction_operational_unit_columns(conn)

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

        if operational_unit_id:
            operational_unit = conn.execute(
                """
                SELECT * FROM operational_units
                WHERE operational_unit_id = ?
                  AND organisation_id = ?
                  AND depot_id = ?
                  AND status = 'ACTIVE'
                """,
                (operational_unit_id, organisation_id, depot_id),
            ).fetchone()
            if not operational_unit:
                conn.close()
                return jsonify({"error": "Fleet/Unit not found for this location"}), 404
            if g.current_user.get("role") not in {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"} and not user_can_operate_unit(
                conn, organisation_id, submitted_by_user_id, operational_unit_id
            ):
                conn.close()
                return jsonify({"error": "OPERATE permission is required for this Fleet/Unit"}), 403
            operational_unit_kind_snapshot = operational_unit["unit_kind"]
            operational_unit_number_snapshot = operational_unit["unit_number"]
            operational_unit_display_snapshot = operational_unit["display_name"]

        resource = None
        if unresolved_entity_type and not unresolved_entity_note:
            conn.close()
            return jsonify({"error": "unresolved_entity_note is required when unresolved_entity_type is set"}), 400
        if unresolved_entity_note and unresolved_entity_type not in {"RESOURCE", "PARTNER", "OPERATIONAL_UNIT"}:
            conn.close()
            return jsonify({"error": "unresolved_entity_type must be RESOURCE, PARTNER, or OPERATIONAL_UNIT"}), 400
        resource_is_missing = bool(unresolved_entity_note and unresolved_entity_type == "RESOURCE")
        if resource_id and not (resource_is_missing and resource_id == "UNRESOLVED"):
            resource = conn.execute(
                "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ? AND is_active = 1",
                (resource_id, organisation_id)
            ).fetchone()
            if not resource:
                conn.close()
                return jsonify({"error": "Resource not found"}), 404

        if (not resource_id or resource_id == "UNRESOLVED") and resource_is_missing:
            resource_id = "UNRESOLVED"
        elif not resource_id:
            conn.close()
            return jsonify({"error": "resource_id is required"}), 400

        partner = None
        partner_address = None

        if partner_id:
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
                unresolved_entity_type,
                customer_reference,
                admin_orgs_reference,
                operational_unit_id,
                operational_unit_kind_snapshot,
                operational_unit_number_snapshot,
                operational_unit_display_snapshot,
                operational_unit_missing
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        resource_name = resource["name"] if resource else None

        if unresolved_entity_note:
            missing_reason_code = "MISSING_OPERATIONAL_UNIT" if operational_unit_missing else "MISSING_ENTITY"
            missing_action_label = "Resolve Missing Fleet/Unit" if operational_unit_missing else "Resolve Missing Entity"
            conn.execute(
                insert_sql,
                (
                    transaction_id, organisation_id, depot_id, transaction_type, resource_id,
                    quantity, direction, "PENDING_APPROVAL", missing_reason_code,
                    unresolved_entity_note, partner_id,
                    None, submitted_by_user_id, submitted_by_display_name,
                    created_at, None, reference_number, org_sequence_number,
                    unresolved_entity_note, unresolved_entity_type,
                    customer_reference, admin_orgs_reference,
                    operational_unit_id, operational_unit_kind_snapshot,
                    operational_unit_number_snapshot, operational_unit_display_snapshot,
                    1 if operational_unit_missing else 0,
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
                reason_code=missing_reason_code,
                reason_text=unresolved_entity_note,
                direct_action_type="ResolveEntity",
                direct_action_target_id=transaction_id,
                direct_action_label=missing_action_label,
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
                "approval_reason_code": missing_reason_code,
                "approval_reason_text": unresolved_entity_note,
                "unresolved_entity_note": unresolved_entity_note,
                "unresolved_entity_type": unresolved_entity_type,
                "pending_entry_id": pending_entry_id,
                "partner_id": partner_id,
                "submitted_by_user_id": submitted_by_user_id,
                "submitted_by_display_name": submitted_by_display_name,
                "reference_number": reference_number,
                "org_sequence_number": org_sequence_number,
                "customer_reference": customer_reference,
                "admin_orgs_reference": admin_orgs_reference,
                "operational_unit_id": operational_unit_id,
                "operational_unit_kind_snapshot": operational_unit_kind_snapshot,
                "operational_unit_number_snapshot": operational_unit_number_snapshot,
                "operational_unit_display_snapshot": operational_unit_display_snapshot,
                "operational_unit_missing": bool(operational_unit_missing),
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
                    customer_reference, admin_orgs_reference,
                    operational_unit_id, operational_unit_kind_snapshot,
                    operational_unit_number_snapshot, operational_unit_display_snapshot,
                    0,
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
                "customer_reference": customer_reference,
                "admin_orgs_reference": admin_orgs_reference,
                "operational_unit_id": operational_unit_id,
                "operational_unit_kind_snapshot": operational_unit_kind_snapshot,
                "operational_unit_number_snapshot": operational_unit_number_snapshot,
                "operational_unit_display_snapshot": operational_unit_display_snapshot,
                "operational_unit_missing": bool(operational_unit_missing),
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
                customer_reference, admin_orgs_reference,
                operational_unit_id, operational_unit_kind_snapshot,
                operational_unit_number_snapshot, operational_unit_display_snapshot,
                0,
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
            "org_sequence_number": org_sequence_number,
            "customer_reference": customer_reference,
            "admin_orgs_reference": admin_orgs_reference,
            "operational_unit_id": operational_unit_id,
            "operational_unit_kind_snapshot": operational_unit_kind_snapshot,
            "operational_unit_number_snapshot": operational_unit_number_snapshot,
            "operational_unit_display_snapshot": operational_unit_display_snapshot,
            "operational_unit_missing": False,
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
        ensure_transaction_reference_columns(conn)
        ensure_transaction_operational_unit_columns(conn)

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
                t.org_sequence_number,
                t.customer_reference,
                t.admin_orgs_reference,
                t.operational_unit_id,
                t.operational_unit_kind_snapshot,
                t.operational_unit_number_snapshot,
                t.operational_unit_display_snapshot,
                t.operational_unit_missing,
                t.unresolved_entity_note,
                t.unresolved_entity_type
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
        ensure_transaction_reference_columns(conn)
        ensure_transaction_operational_unit_columns(conn)

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
                t.org_sequence_number,
                t.customer_reference,
                t.admin_orgs_reference,
                t.operational_unit_id,
                t.operational_unit_kind_snapshot,
                t.operational_unit_number_snapshot,
                t.operational_unit_display_snapshot,
                t.operational_unit_missing,
                t.unresolved_entity_note,
                t.unresolved_entity_type
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

        if not _current_user_can_access_org(row["organisation_id"]):
            conn.close()
            return jsonify({"error": "ORG_ACCESS_DENIED", "message": "You do not have access to this organisation."}), 403

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

        if not _current_user_can_access_org(txn["organisation_id"]):
            conn.close()
            return jsonify({"error": "ORG_ACCESS_DENIED", "message": "You do not have access to this organisation."}), 403
        if g.current_user.get("role") == "USER" and txn["submitted_by_user_id"] != g.current_user.get("user_id"):
            conn.close()
            return jsonify({"error": "You can only post your own transactions"}), 403

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

        # Route learning — update visit history for morning sync predictions
        if txn["submitted_by_user_id"] and txn["partner_address_id"]:
            from modules.route_intelligence import upsert_route_learning, ACTION_TO_DIRECTION
            direction_to_action = {v: k for k, v in ACTION_TO_DIRECTION.items()}
            learned_action = direction_to_action.get(txn["direction"], "Dropoff")
            upsert_route_learning(
                conn=conn,
                user_id=txn["submitted_by_user_id"],
                organisation_id=txn["organisation_id"],
                partner_address_id=txn["partner_address_id"],
                resource_id=txn["resource_id"],
                action=learned_action,
                transaction_created_at=txn["created_at"],
            )

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

        if not _current_user_can_access_org(txn["organisation_id"]):
            conn.close()
            return jsonify({"error": "ORG_ACCESS_DENIED", "message": "You do not have access to this organisation."}), 403

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
                "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ? AND is_active = 1",
                (txn["resource_id"], organisation_id),
            ).fetchone()
            if not resource:
                conn.close()
                return jsonify({"error": "Existing resource not found for this organisation"}), 404
            new_resource_id = txn["resource_id"]

        # Validate new partner if provided
        if txn["unresolved_entity_type"] == "PARTNER" and not new_partner_id:
            conn.close()
            return jsonify({"error": "partner_id is required — the original transaction had no partner"}), 400
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
            if new_partner_id:
                partner = conn.execute(
                    "SELECT * FROM partners WHERE partner_id = ? AND organisation_id = ? AND is_active = 1",
                    (new_partner_id, organisation_id),
                ).fetchone()
                if not partner:
                    conn.close()
                    return jsonify({"error": "Existing partner not found for this organisation"}), 404

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

        if txn["submitted_by_user_id"]:
            notify_user(
                user_id=txn["submitted_by_user_id"],
                notification_type="ENTITY_RESOLVED",
                title="Your transaction is complete",
                body=(
                    f"The missing entity has been added and your transaction "
                    f"#{txn['reference_number'] or transaction_id} has been posted."
                ),
                url=f"/transactions/{transaction_id}",
                entity_type="Transaction",
                entity_id=transaction_id,
                organisation_id=organisation_id,
            )

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


    @app.patch("/transactions/<transaction_id>/resolve-operational-unit")
    def resolve_transaction_operational_unit(transaction_id):
        """
        Org Admin resolves a MISSING_OPERATIONAL_UNIT pending transaction.

        This is the @missing fleet/unit recovery path: the field worker keeps
        moving, then Org Admin assigns the correct Fleet/Unit and the
        transaction posts with real operational context.
        """
        current_user = g.current_user
        org_admin_roles = {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
        if current_user["role"] not in org_admin_roles:
            return jsonify({"error": "Only Org Admin or above can resolve missing Fleet/Unit issues"}), 403

        body = request.get_json(silent=True) or {}
        operational_unit_id = (body.get("operational_unit_id") or "").strip()
        review_notes = (body.get("review_notes") or "").strip() or None

        if not operational_unit_id:
            return jsonify({"error": "operational_unit_id is required"}), 400

        conn = get_conn()
        ensure_transaction_partner_columns(conn)
        ensure_transaction_numbering_tables(conn)
        ensure_transaction_reference_columns(conn)
        ensure_transaction_user_attribution_columns(conn)
        ensure_transaction_operational_unit_columns(conn)
        ensure_ledger_balance_operational_unit_columns(conn)

        txn = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        if not txn:
            conn.close()
            return jsonify({"error": "Transaction not found"}), 404

        if current_user["role"] not in {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"} and current_user.get("user_org_id") != txn["organisation_id"]:
            conn.close()
            return jsonify({"error": "ORG_ACCESS_DENIED", "message": "You do not have access to this organisation."}), 403

        resolvable_reason_codes = {"MISSING_OPERATIONAL_UNIT", "OFFLINE_OPERATIONAL_UNIT_CONFLICT"}
        if txn["approval_reason_code"] not in resolvable_reason_codes:
            conn.close()
            return jsonify({"error": "Transaction is not pending due to a resolvable Fleet/Unit issue"}), 400

        if txn["status"] != "PENDING_APPROVAL":
            conn.close()
            return jsonify({"error": f"Transaction status is '{txn['status']}' — only PENDING_APPROVAL transactions can be resolved"}), 400

        organisation_id = txn["organisation_id"]
        unit = conn.execute(
            """
            SELECT * FROM operational_units
            WHERE operational_unit_id = ?
              AND organisation_id = ?
              AND status = 'ACTIVE'
            """,
            (operational_unit_id, organisation_id),
        ).fetchone()
        if not unit:
            conn.close()
            return jsonify({"error": "Fleet/Unit not found or inactive"}), 404

        if unit["depot_id"] != txn["depot_id"]:
            conn.close()
            return jsonify({"error": "Fleet/Unit is not assigned to the transaction location"}), 400

        ts = now_iso()
        conn.execute(
            """
            UPDATE transactions
            SET operational_unit_id = ?,
                operational_unit_kind_snapshot = ?,
                operational_unit_number_snapshot = ?,
                operational_unit_display_snapshot = ?,
                operational_unit_missing = 0,
                unresolved_entity_note = NULL,
                unresolved_entity_type = NULL,
                approval_reason_code = NULL,
                approval_reason_text = NULL
            WHERE transaction_id = ?
            """,
            (
                unit["operational_unit_id"],
                unit["unit_kind"],
                unit["unit_number"],
                unit["display_name"],
                transaction_id,
            ),
        )

        txn_updated = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        post_transaction_to_ledger(conn, txn_updated)

        conn.execute(
            """
            UPDATE pending_approval_entries
            SET status = 'RESOLVED', updated_at = ?
            WHERE source_record_id = ?
              AND reason_code IN ('MISSING_OPERATIONAL_UNIT', 'OFFLINE_OPERATIONAL_UNIT_CONFLICT')
            """,
            (ts, transaction_id),
        )

        audit_event(
            conn,
            entity_type="Transaction",
            entity_id=transaction_id,
            action="OPERATIONAL_UNIT_RESOLVED",
            summary=(
                f"{current_user['display_name']} resolved missing Fleet/Unit on transaction {transaction_id}. "
                f"Fleet/Unit: {unit['display_name']} ({unit['unit_kind']} {unit['unit_number']}). "
                f"Notes: {review_notes or 'none'}."
            ),
            organisation_id=organisation_id,
        )

        conn.commit()
        conn.close()

        if txn["submitted_by_user_id"]:
            notify_user(
                user_id=txn["submitted_by_user_id"],
                notification_type="OPERATIONAL_UNIT_RESOLVED",
                title="Your transaction is complete",
                body=(
                    f"The missing Fleet/Unit has been resolved and your transaction "
                    f"#{txn['reference_number'] or transaction_id} has been posted."
                ),
                url=f"/transactions/{transaction_id}",
                entity_type="Transaction",
                entity_id=transaction_id,
                organisation_id=organisation_id,
            )

        return jsonify({
            "transaction_id": transaction_id,
            "status": "POSTED",
            "operational_unit_id": unit["operational_unit_id"],
            "operational_unit_kind": unit["unit_kind"],
            "operational_unit_number": unit["unit_number"],
            "operational_unit_display": unit["display_name"],
            "reviewed_by": current_user["display_name"],
            "review_notes": review_notes,
            "message": "Fleet/Unit resolved. Transaction posted to ledger.",
        }), 200


    @app.post("/pending-approval/<pending_entry_id>/approve")
    def approve_pending_entry(pending_entry_id):
        role_error = _require_org_admin_role()
        if role_error:
            return role_error
        conn = get_conn()

        pending = conn.execute(
            "SELECT * FROM pending_approval_entries WHERE pending_entry_id = ?",
            (pending_entry_id,)
        ).fetchone()

        if not pending:
            conn.close()
            return jsonify({"error": "Pending approval entry not found"}), 404

        if not _current_user_can_access_org(pending["organisation_id"]):
            conn.close()
            return jsonify({"error": "ORG_ACCESS_DENIED", "message": "You do not have access to this organisation."}), 403

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
        role_error = _require_org_admin_role()
        if role_error:
            return role_error
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

        if not _current_user_can_access_org(pending["organisation_id"]):
            conn.close()
            return jsonify({"error": "ORG_ACCESS_DENIED", "message": "You do not have access to this organisation."}), 403

        if pending["status"] not in {"PENDING_APPROVAL", "AWAITING_FIX", "READY_TO_APPROVE"}:
            conn.close()
            return jsonify({
                "error": "PENDING_APPROVAL_CLOSED",
                "message": "Resolved or rejected approvals cannot be changed.",
            }), 409

        if pending["entry_type"] == "Transaction":
            source_transaction = conn.execute(
                "SELECT status FROM transactions WHERE transaction_id = ?",
                (pending["source_record_id"],),
            ).fetchone()
            if not source_transaction:
                conn.close()
                return jsonify({"error": "Source transaction not found"}), 404
            if source_transaction["status"] == "POSTED":
                conn.close()
                return jsonify({
                    "error": "POSTED_TRANSACTION_IMMUTABLE",
                    "message": "A posted transaction cannot be rejected or rewritten.",
                }), 409

        updated = conn.execute(
            """
            UPDATE pending_approval_entries
            SET status = ?, rejection_reason_code = ?, rejection_reason_text = ?, updated_at = ?
            WHERE pending_entry_id = ?
              AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
            """,
            ("REJECTED", rejection_reason_code, rejection_reason_text, now_iso(), pending_entry_id)
        )
        if updated.rowcount != 1:
            conn.rollback()
            conn.close()
            return jsonify({
                "error": "PENDING_APPROVAL_CLOSED",
                "message": "This approval was resolved or rejected by another review action.",
            }), 409

        if pending["entry_type"] == "Transaction":
            source_updated = conn.execute(
                """
                UPDATE transactions
                SET status = ?, approval_reason_code = ?, approval_reason_text = ?
                WHERE transaction_id = ? AND status <> 'POSTED'
                """,
                ("REJECTED", rejection_reason_code, rejection_reason_text, pending["source_record_id"])
            )
            if source_updated.rowcount != 1:
                conn.rollback()
                conn.close()
                return jsonify({
                    "error": "POSTED_TRANSACTION_IMMUTABLE",
                    "message": "The transaction was posted by another action and cannot be rejected.",
                }), 409
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
        if g.current_user.get("role") not in GLOBAL_ADMIN_ROLES:
            organisation_id = g.current_user.get("user_org_id")
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
                SUM(bp.current_quantity) AS current_quantity,
                MAX(bp.updated_at) AS updated_at
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

        sql += " GROUP BY bp.organisation_id, o.name, bp.depot_id, d.name, bp.resource_id, r.name, r.resource_type, r.unit_type"
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
        role_error = _require_org_admin_role()
        if role_error:
            return role_error
        organisation_id = request.args.get("organisation_id")
        if g.current_user.get("role") not in GLOBAL_ADMIN_ROLES:
            organisation_id = g.current_user.get("user_org_id")
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
        role_error = _require_org_admin_role()
        if role_error:
            return role_error
        organisation_id = request.args.get("organisation_id")
        if g.current_user.get("role") not in GLOBAL_ADMIN_ROLES:
            organisation_id = g.current_user.get("user_org_id")
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
        role_error = _require_org_admin_role()
        if role_error:
            return role_error
        conn = get_conn()
        row = conn.execute(
            """
            SELECT *
            FROM pending_approval_entries
            WHERE pending_entry_id = ?
            """,
            (pending_entry_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Pending approval entry not found"}), 404

        if not _current_user_can_access_org(row["organisation_id"]):
            conn.close()
            return jsonify({"error": "ORG_ACCESS_DENIED", "message": "You do not have access to this organisation."}), 403

        conn.close()

        return jsonify(dict(row)), 200
