"""
Offline batch upload module.

Field users queue transactions and resource losses locally when there is no
data connection. When connectivity returns, the app submits the queued items
as a single batch. Each item is processed independently — one failure does
not block the rest.

Idempotency: every item carries a client-generated local_id + device_id.
If the same (device_id, local_id) pair is submitted again (e.g. retry after
a partial upload), the original result is returned without re-processing.

The queued_at timestamp from the client is preserved as the record's
created_at, so history reflects when the field event actually happened.
"""

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso
from modules.transactions import ensure_transaction_operational_unit_columns
from modules.operational_units import user_can_operate_unit

_SUPPORTED_TYPES = {"transaction", "resource_loss"}
_LOSS_TYPES = {"DAMAGED", "STOLEN", "LOST", "DESTROYED", "OTHER"}
_MAX_BATCH_SIZE = 200


def ensure_offline_batch_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS offline_batch_log (
        log_id          TEXT PRIMARY KEY,
        device_id       TEXT NOT NULL,
        local_id        TEXT NOT NULL,
        organisation_id TEXT,
        submitted_by_user_id TEXT,
        item_type       TEXT NOT NULL,
        status          TEXT NOT NULL,
        server_id       TEXT,
        error_message   TEXT,
        processed_at    TEXT NOT NULL,
        UNIQUE (device_id, local_id)
    )
    """)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(offline_batch_log)").fetchall()}
    if "organisation_id" not in cols:
        conn.execute("ALTER TABLE offline_batch_log ADD COLUMN organisation_id TEXT")
    if "submitted_by_user_id" not in cols:
        conn.execute("ALTER TABLE offline_batch_log ADD COLUMN submitted_by_user_id TEXT")
    conn.commit()


def _offline_log_context(current_user, payload):
    if current_user.get("role") in {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}:
        organisation_id = payload.get("organisation_id") or current_user.get("user_org_id")
    else:
        organisation_id = current_user.get("user_org_id") or payload.get("organisation_id")
    return organisation_id, current_user.get("user_id")


def _create_offline_operational_unit_conflict_entry(conn, create_pending_entry, txn, unit_display_name):
    existing = conn.execute(
        """
        SELECT pending_entry_id FROM pending_approval_entries
        WHERE source_record_id = ? AND reason_code = 'OFFLINE_OPERATIONAL_UNIT_CONFLICT'
        LIMIT 1
        """,
        (txn["transaction_id"],),
    ).fetchone()
    if existing:
        return existing["pending_entry_id"]

    reason = (
        f"Offline Fleet/Unit conflict for {unit_display_name or txn['operational_unit_id']}. "
        "Another user submitted offline work against the same Fleet/Unit before sync."
    )
    return create_pending_entry(
        conn=conn,
        organisation_id=txn["organisation_id"],
        entry_type="Transaction",
        source_record_id=txn["transaction_id"],
        source_module="OfflineBatch",
        submitted_by_display_name=txn["submitted_by_display_name"],
        related_entity_type="OperationalUnit",
        related_entity_id=txn["operational_unit_id"],
        related_entity_name=unit_display_name,
        reason_code="OFFLINE_OPERATIONAL_UNIT_CONFLICT",
        reason_text=reason,
        direct_action_type="ResolveOperationalUnitConflict",
        direct_action_target_id=txn["transaction_id"],
        direct_action_label="Review Fleet/Unit Conflict",
        can_approve_now=False,
        can_reject_now=True,
        resource_id=txn["resource_id"],
        resource_name=None,
        status="PENDING_APPROVAL",
    )


def _route_offline_operational_unit_conflict(conn, create_pending_entry, txn, unit_display_name):
    conn.execute(
        """
        UPDATE transactions
        SET status = 'PENDING_APPROVAL',
            approval_reason_code = 'OFFLINE_OPERATIONAL_UNIT_CONFLICT',
            approval_reason_text = ?
        WHERE transaction_id = ?
        """,
        (
            f"Offline Fleet/Unit conflict for {unit_display_name or txn['operational_unit_id']}",
            txn["transaction_id"],
        ),
    )
    refreshed = conn.execute(
        "SELECT * FROM transactions WHERE transaction_id = ?",
        (txn["transaction_id"],),
    ).fetchone()
    _create_offline_operational_unit_conflict_entry(conn, create_pending_entry, refreshed, unit_display_name)
    audit_event(
        conn,
        entity_type="Transaction",
        entity_id=txn["transaction_id"],
        action="OFFLINE_CONFLICT_ROUTED",
        summary=f"Transaction routed to Pending Approval for Fleet/Unit conflict: {unit_display_name or txn['operational_unit_id']}.",
        organisation_id=txn["organisation_id"],
    )


def _find_conflicting_offline_operational_unit_transaction(
    conn, organisation_id, depot_id, operational_unit_id, submitted_by_user_id, queued_at
):
    if not operational_unit_id or not submitted_by_user_id or not queued_at:
        return None
    return conn.execute(
        """
        SELECT t.*
        FROM transactions t
        JOIN offline_batch_log obl
          ON obl.server_id = t.transaction_id
         AND obl.item_type = 'transaction'
         AND obl.status = 'success'
        WHERE t.organisation_id = ?
          AND t.depot_id = ?
          AND t.operational_unit_id = ?
          AND COALESCE(t.submitted_by_user_id, '') <> ?
          AND t.status IN ('DRAFT', 'PENDING_APPROVAL')
          AND (t.approval_reason_code IS NULL OR t.approval_reason_code = 'OFFLINE_OPERATIONAL_UNIT_CONFLICT')
          AND ABS((julianday(t.created_at) - julianday(?)) * 86400) <= 43200
        ORDER BY t.created_at DESC
        LIMIT 1
        """,
        (organisation_id, depot_id, operational_unit_id, submitted_by_user_id, queued_at),
    ).fetchone()


def _ensure_offline_transaction_schema(
    conn,
    ensure_transaction_numbering_tables,
    ensure_transaction_partner_columns,
    ensure_partner_address_tables,
    ensure_transaction_user_attribution_columns,
):
    ensure_transaction_partner_columns(conn)
    ensure_partner_address_tables(conn)
    ensure_transaction_user_attribution_columns(conn)
    ensure_transaction_numbering_tables(conn)
    ensure_transaction_operational_unit_columns(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "transaction_note" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN transaction_note TEXT")
    if "unresolved_entity_note" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN unresolved_entity_note TEXT")
    if "unresolved_entity_type" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN unresolved_entity_type TEXT")


def _ensure_offline_resource_loss_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS resource_losses (
        loss_id                     TEXT PRIMARY KEY,
        organisation_id             TEXT NOT NULL,
        depot_id                    TEXT NOT NULL,
        resource_id                 TEXT NOT NULL,
        quantity                    INTEGER NOT NULL,
        loss_type                   TEXT NOT NULL,
        loss_reason                 TEXT NOT NULL,
        loss_date                   TEXT NOT NULL,
        status                      TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
        reported_by_user_id         TEXT,
        reported_by_display_name    TEXT NOT NULL,
        partner_id                  TEXT,
        related_transaction_id      TEXT,
        reviewed_by_display_name    TEXT,
        review_notes                TEXT,
        reviewed_at                 TEXT,
        loss_transaction_id         TEXT,
        created_at                  TEXT NOT NULL,
        updated_at                  TEXT NOT NULL
    )
    """)
    loss_cols = {r["name"] for r in conn.execute("PRAGMA table_info(resource_losses)").fetchall()}
    if "unresolved_entity_note" not in loss_cols:
        conn.execute("ALTER TABLE resource_losses ADD COLUMN unresolved_entity_note TEXT")
    if "unresolved_entity_type" not in loss_cols:
        conn.execute("ALTER TABLE resource_losses ADD COLUMN unresolved_entity_type TEXT")


def _process_transaction_item(
    conn,
    payload,
    queued_at,
    current_user,
    post_transaction_to_ledger,
    generate_transaction_reference,
    ensure_transaction_numbering_tables,
    ensure_transaction_partner_columns,
    ensure_partner_address_tables,
    ensure_transaction_user_attribution_columns,
    create_pending_entry,
):
    organisation_id = payload.get("organisation_id")
    depot_id = payload.get("depot_id")
    resource_id = payload.get("resource_id")
    quantity = payload.get("quantity")
    direction = (payload.get("direction") or "").strip().upper()
    transaction_type = (payload.get("transaction_type") or "MOVEMENT").strip()
    partner_id = payload.get("partner_id") or None
    partner_address_id = payload.get("partner_address_id") or None
    submitted_by_user_id = payload.get("submitted_by_user_id") or None
    submitted_by_display_name = (
        payload.get("submitted_by_display_name") or current_user["display_name"]
    ).strip()
    transaction_note = (payload.get("transaction_note") or "").strip() or None
    unresolved_entity_note = (payload.get("unresolved_entity_note") or "").strip() or None
    unresolved_entity_type = (payload.get("unresolved_entity_type") or "").strip().upper() or None
    operational_unit_id = (payload.get("operational_unit_id") or "").strip() or None
    operational_unit_missing = bool(payload.get("operational_unit_missing"))
    operational_unit_missing_note = (payload.get("operational_unit_missing_note") or "").strip() or None
    operational_unit = None
    operational_unit_kind_snapshot = None
    operational_unit_number_snapshot = None
    operational_unit_display_snapshot = None

    if operational_unit_missing:
        if resource_id == "UNRESOLVED" and unresolved_entity_type not in (None, "OPERATIONAL_UNIT"):
            raise ValueError("Resolve the missing entity before marking Fleet/Unit missing")
        unresolved_entity_note = operational_unit_missing_note or unresolved_entity_note or "@missing fleet/unit"
        unresolved_entity_type = "OPERATIONAL_UNIT"

    if not organisation_id:
        raise ValueError("organisation_id is required")
    if not depot_id:
        raise ValueError("depot_id is required")
    if not resource_id and not unresolved_entity_note:
        raise ValueError("resource_id is required (or provide unresolved_entity_note if entity is missing)")
    if quantity is None:
        raise ValueError("quantity is required")
    if direction not in ("IN", "OUT"):
        raise ValueError("direction must be IN or OUT")

    try:
        quantity = int(quantity)
        if quantity <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("quantity must be a positive integer")

    ensure_transaction_partner_columns(conn)
    ensure_partner_address_tables(conn)
    ensure_transaction_user_attribution_columns(conn)
    ensure_transaction_numbering_tables(conn)
    ensure_transaction_operational_unit_columns(conn)

    org = conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?", (organisation_id,)
    ).fetchone()
    if not org:
        raise ValueError("Organisation not found")

    depot = conn.execute(
        "SELECT * FROM depots WHERE depot_id = ? AND organisation_id = ?",
        (depot_id, organisation_id),
    ).fetchone()
    if not depot:
        raise ValueError("Depot not found")

    if operational_unit_id:
        operational_unit = conn.execute(
            """
            SELECT * FROM operational_units
            WHERE operational_unit_id = ?
              AND organisation_id = ?
              AND status = 'ACTIVE'
            """,
            (operational_unit_id, organisation_id),
        ).fetchone()
        if not operational_unit:
            raise ValueError("Fleet/Unit not found or inactive")
        if operational_unit["depot_id"] != depot_id:
            raise ValueError("Fleet/Unit is not assigned to the transaction location")
        if current_user.get("role") not in {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"} and not user_can_operate_unit(
            conn, organisation_id, submitted_by_user_id, operational_unit_id
        ):
            raise PermissionError("OPERATE permission is required for this Fleet/Unit")
        operational_unit_kind_snapshot = operational_unit["unit_kind"]
        operational_unit_number_snapshot = operational_unit["unit_number"]
        operational_unit_display_snapshot = operational_unit["display_name"]

    resource = None
    if unresolved_entity_type and not unresolved_entity_note:
        raise ValueError("unresolved_entity_note is required when unresolved_entity_type is set")
    if unresolved_entity_note and unresolved_entity_type not in {"RESOURCE", "PARTNER", "OPERATIONAL_UNIT"}:
        raise ValueError("unresolved_entity_type must be RESOURCE, PARTNER, or OPERATIONAL_UNIT")
    resource_is_missing = bool(unresolved_entity_note and unresolved_entity_type == "RESOURCE")
    if resource_id and not (resource_is_missing and resource_id == "UNRESOLVED"):
        resource = conn.execute(
            "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ? AND is_active = 1",
            (resource_id, organisation_id),
        ).fetchone()
        if not resource:
            raise ValueError("Resource not found or inactive")

    if (not resource_id or resource_id == "UNRESOLVED") and resource_is_missing:
        resource_id = "UNRESOLVED"
    elif not resource_id:
        raise ValueError("resource_id is required")

    partner = None
    if partner_id:
        partner = conn.execute(
            "SELECT * FROM partners WHERE partner_id = ? AND organisation_id = ? AND is_active = 1",
            (partner_id, organisation_id),
        ).fetchone()
        if not partner:
            raise ValueError("Partner not found")

    resource_name = resource["name"] if resource else None

    # Preserve the field timestamp; fall back to now if not provided
    created_at = queued_at or now_iso()
    transaction_id = make_id("txn")
    reference_number, org_sequence_number = generate_transaction_reference(conn, organisation_id)

    insert_sql = """
        INSERT INTO transactions (
            transaction_id, organisation_id, depot_id, transaction_type,
            resource_id, quantity, direction, status,
            approval_reason_code, approval_reason_text,
            partner_id, partner_address_id,
            submitted_by_user_id, submitted_by_display_name,
            created_at, posted_at,
            reference_number, org_sequence_number,
            transaction_note,
            unresolved_entity_note, unresolved_entity_type,
            operational_unit_id, operational_unit_kind_snapshot,
            operational_unit_number_snapshot, operational_unit_display_snapshot,
            operational_unit_missing
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    # Ensure optional columns exist
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "transaction_note" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN transaction_note TEXT")
    if "unresolved_entity_note" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN unresolved_entity_note TEXT")
    if "unresolved_entity_type" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN unresolved_entity_type TEXT")

    if unresolved_entity_note:
        missing_reason_code = "MISSING_OPERATIONAL_UNIT" if operational_unit_missing else "MISSING_ENTITY"
        missing_action_type = "ResolveOperationalUnit" if operational_unit_missing else "ResolveEntity"
        missing_action_label = "Resolve Missing Fleet/Unit" if operational_unit_missing else "Resolve Missing Entity"
        conn.execute(
            insert_sql,
            (
                transaction_id, organisation_id, depot_id, transaction_type,
                resource_id, quantity, direction, "PENDING_APPROVAL",
                missing_reason_code, unresolved_entity_note,
                partner_id, None,
                submitted_by_user_id, submitted_by_display_name,
                created_at, None,
                reference_number, org_sequence_number,
                transaction_note,
                unresolved_entity_note, unresolved_entity_type,
                operational_unit_id, operational_unit_kind_snapshot,
                operational_unit_number_snapshot, operational_unit_display_snapshot,
                1 if operational_unit_missing else 0,
            ),
        )

        create_pending_entry(
            conn=conn,
            organisation_id=organisation_id,
            entry_type="Transaction",
            source_record_id=transaction_id,
            source_module="OfflineBatch",
            submitted_by_display_name=submitted_by_display_name,
            related_entity_type="OperationalUnit" if operational_unit_missing else (unresolved_entity_type or "UNKNOWN"),
            related_entity_id=None,
            related_entity_name=None,
            reason_code=missing_reason_code,
            reason_text=unresolved_entity_note,
            direct_action_type=missing_action_type,
            direct_action_target_id=transaction_id,
            direct_action_label=missing_action_label,
            can_approve_now=False,
            can_reject_now=True,
            resource_id=resource_id if resource_id != "UNRESOLVED" else None,
            resource_name=resource_name,
            status="PENDING_APPROVAL" if operational_unit_missing else "AWAITING_FIX",
        )

        audit_event(
            conn, entity_type="Transaction", entity_id=transaction_id,
            action="OFFLINE_UPLOAD_PENDING",
            summary=(
                f"Offline transaction uploaded by {submitted_by_display_name} — "
                f"pending due to {missing_reason_code.lower()}: {unresolved_entity_note}."
            ),
            organisation_id=organisation_id,
        )

        status = "PENDING_APPROVAL"

    elif depot["opening_balance_used"] == 0:
        conn.execute(
            insert_sql,
            (
                transaction_id, organisation_id, depot_id, transaction_type,
                resource_id, quantity, direction, "PENDING_APPROVAL",
                "NIL_OPENING_BALANCE",
                "Opening balance has not been set for this entity.",
                partner_id, partner_address_id,
                submitted_by_user_id, submitted_by_display_name,
                created_at, None,
                reference_number, org_sequence_number,
                transaction_note,
                None, None,
                operational_unit_id, operational_unit_kind_snapshot,
                operational_unit_number_snapshot, operational_unit_display_snapshot,
                0,
            ),
        )

        create_pending_entry(
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
            status="AWAITING_FIX",
        )

        audit_event(
            conn, entity_type="Transaction", entity_id=transaction_id,
            action="OFFLINE_UPLOAD_PENDING",
            summary=f"Offline transaction uploaded by {submitted_by_display_name} — pending due to nil opening balance.",
            organisation_id=organisation_id,
        )

        status = "PENDING_APPROVAL"
    else:
        conflicting_txn = _find_conflicting_offline_operational_unit_transaction(
            conn,
            organisation_id=organisation_id,
            depot_id=depot_id,
            operational_unit_id=operational_unit_id,
            submitted_by_user_id=submitted_by_user_id,
            queued_at=created_at,
        )
        conflict_reason = (
            f"Offline Fleet/Unit conflict for {operational_unit_display_snapshot or operational_unit_id}"
            if conflicting_txn else None
        )
        conn.execute(
            insert_sql,
            (
                transaction_id, organisation_id, depot_id, transaction_type,
                resource_id, quantity, direction, "PENDING_APPROVAL" if conflicting_txn else "DRAFT",
                "OFFLINE_OPERATIONAL_UNIT_CONFLICT" if conflicting_txn else None, conflict_reason,
                partner_id, partner_address_id,
                submitted_by_user_id, submitted_by_display_name,
                created_at, None,
                reference_number, org_sequence_number,
                transaction_note,
                None, None,
                operational_unit_id, operational_unit_kind_snapshot,
                operational_unit_number_snapshot, operational_unit_display_snapshot,
                0,
            ),
        )

        inserted_txn = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()

        if conflicting_txn:
            _route_offline_operational_unit_conflict(
                conn, create_pending_entry, conflicting_txn, operational_unit_display_snapshot
            )
            _route_offline_operational_unit_conflict(
                conn, create_pending_entry, inserted_txn, operational_unit_display_snapshot
            )
            audit_event(
                conn, entity_type="Transaction", entity_id=transaction_id,
                action="OFFLINE_UPLOAD_CONFLICT",
                summary=(
                    f"Offline transaction uploaded by {submitted_by_display_name} — "
                    f"Fleet/Unit conflict detected for {operational_unit_display_snapshot or operational_unit_id}."
                ),
                organisation_id=organisation_id,
            )
            status = "PENDING_APPROVAL"
        else:
            audit_event(
                conn, entity_type="Transaction", entity_id=transaction_id,
                action="OFFLINE_UPLOAD",
                summary=(
                    f"Offline transaction uploaded by {submitted_by_display_name}: "
                    f"{transaction_type} {quantity} {direction} "
                    f"{'for ' + partner['name'] if partner else ''}."
                ),
                organisation_id=organisation_id,
            )

            status = "DRAFT"

    return transaction_id, {
        "transaction_id": transaction_id,
        "organisation_id": organisation_id,
        "depot_id": depot_id,
        "depot_name": depot["name"],
        "resource_id": resource_id,
        "resource_name": resource_name,
        "quantity": quantity,
        "direction": direction,
        "status": status,
        "reference_number": reference_number,
        "org_sequence_number": org_sequence_number,
        "partner_id": partner_id,
        "partner_name": partner["name"] if partner else None,
        "unresolved_entity_note": unresolved_entity_note,
        "operational_unit_id": operational_unit_id,
        "operational_unit_kind_snapshot": operational_unit_kind_snapshot,
        "operational_unit_number_snapshot": operational_unit_number_snapshot,
        "operational_unit_display_snapshot": operational_unit_display_snapshot,
        "submitted_by_display_name": submitted_by_display_name,
        "created_at": created_at,
        "queued_offline": True,
    }


def _process_resource_loss_item(conn, payload, queued_at, current_user):
    organisation_id = payload.get("organisation_id")
    depot_id = payload.get("depot_id")
    resource_id = payload.get("resource_id")
    quantity = payload.get("quantity")
    loss_type = (payload.get("loss_type") or "").strip().upper()
    loss_reason = (payload.get("loss_reason") or "").strip()
    loss_date = (payload.get("loss_date") or "").strip() or None
    partner_id = payload.get("partner_id") or None
    related_transaction_id = payload.get("related_transaction_id") or None
    submitted_by_display_name = (
        payload.get("submitted_by_display_name") or current_user["display_name"]
    ).strip()
    submitted_by_user_id = payload.get("submitted_by_user_id") or None
    unresolved_entity_note = (payload.get("unresolved_entity_note") or "").strip() or None
    unresolved_entity_type = (payload.get("unresolved_entity_type") or "").strip().upper() or None

    if not organisation_id:
        raise ValueError("organisation_id is required")
    if not depot_id:
        raise ValueError("depot_id is required")
    if not resource_id and not unresolved_entity_note:
        raise ValueError("resource_id is required (or provide unresolved_entity_note if entity is missing)")
    if quantity is None:
        raise ValueError("quantity is required")
    if not loss_type:
        raise ValueError("loss_type is required")
    if not loss_reason:
        raise ValueError("loss_reason is required")
    if loss_type not in _LOSS_TYPES:
        raise ValueError(f"Invalid loss_type. Use: {', '.join(sorted(_LOSS_TYPES))}")

    try:
        quantity = int(quantity)
        if quantity <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("quantity must be a positive integer")

    _ensure_offline_resource_loss_tables(conn)

    depot = conn.execute(
        "SELECT * FROM depots WHERE depot_id = ? AND organisation_id = ?",
        (depot_id, organisation_id),
    ).fetchone()
    if not depot:
        raise ValueError("Depot not found")

    resource = None
    if resource_id:
        resource = conn.execute(
            "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ? AND is_active = 1",
            (resource_id, organisation_id),
        ).fetchone()
        if not resource and not unresolved_entity_note:
            raise ValueError("Resource not found or inactive")

    if not resource_id and unresolved_entity_note:
        resource_id = "UNRESOLVED"

    resource_name = resource["name"] if resource else None

    created_at = queued_at or now_iso()
    loss_id = make_id("loss")
    effective_date = loss_date or created_at[:10]

    # Ensure unresolved columns exist (may not on older DB)
    loss_cols = {r["name"] for r in conn.execute("PRAGMA table_info(resource_losses)")}
    if "unresolved_entity_note" not in loss_cols:
        conn.execute("ALTER TABLE resource_losses ADD COLUMN unresolved_entity_note TEXT")
    if "unresolved_entity_type" not in loss_cols:
        conn.execute("ALTER TABLE resource_losses ADD COLUMN unresolved_entity_type TEXT")

    conn.execute(
        """INSERT INTO resource_losses (
            loss_id, organisation_id, depot_id, resource_id,
            quantity, loss_type, loss_reason, loss_date, status,
            reported_by_user_id, reported_by_display_name,
            partner_id, related_transaction_id,
            unresolved_entity_note, unresolved_entity_type,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_REVIEW', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            loss_id, organisation_id, depot_id, resource_id,
            quantity, loss_type, loss_reason, effective_date,
            submitted_by_user_id,
            submitted_by_display_name,
            partner_id, related_transaction_id,
            unresolved_entity_note, unresolved_entity_type,
            created_at, created_at,
        ),
    )

    if unresolved_entity_note:
        audit_event(
            conn, entity_type="ResourceLoss", entity_id=loss_id,
            action="OFFLINE_UPLOAD",
            summary=(
                f"Offline loss report uploaded by {submitted_by_display_name}: "
                f"{quantity} items ({loss_type}) at {depot['name']} — "
                f"unresolved entity: {unresolved_entity_note}."
            ),
            organisation_id=organisation_id,
        )
    else:
        audit_event(
            conn, entity_type="ResourceLoss", entity_id=loss_id,
            action="OFFLINE_UPLOAD",
            summary=(
                f"Offline loss report uploaded by {submitted_by_display_name}: "
                f"{quantity} × {resource_name} ({loss_type}) at {depot['name']}."
            ),
            organisation_id=organisation_id,
        )

    return loss_id, {
        "loss_id": loss_id,
        "organisation_id": organisation_id,
        "depot_id": depot_id,
        "depot_name": depot["name"],
        "resource_id": resource_id,
        "resource_name": resource_name,
        "quantity": quantity,
        "loss_type": loss_type,
        "loss_reason": loss_reason,
        "loss_date": effective_date,
        "status": "PENDING_REVIEW",
        "unresolved_entity_note": unresolved_entity_note,
        "reported_by_display_name": submitted_by_display_name,
        "created_at": created_at,
        "queued_offline": True,
    }


def register_offline_batch_routes(
    app,
    post_transaction_to_ledger,
    generate_transaction_reference,
    ensure_transaction_numbering_tables,
    ensure_transaction_partner_columns,
    ensure_partner_address_tables,
    ensure_transaction_user_attribution_columns,
    create_pending_entry,
):

    @app.post("/offline-batch")
    def upload_offline_batch():
        """
        Upload a batch of items queued while the device was offline.

        Each item must include:
          local_id   — client-generated ID for idempotency (string)
          type       — "transaction" or "resource_loss"
          queued_at  — ISO timestamp of when the item was queued locally
          payload    — the request body for the item type

        Items are processed independently. One failure does not block others.
        Submitting the same (device_id, local_id) again returns the original
        result without re-processing.
        """
        current_user = g.current_user
        body = request.get_json(silent=True) or {}

        device_id = (body.get("device_id") or "").strip()
        items = body.get("items") or []

        if not device_id:
            return jsonify({"error": "device_id is required"}), 400
        if not isinstance(items, list) or len(items) == 0:
            return jsonify({"error": "items must be a non-empty array"}), 400
        if len(items) > _MAX_BATCH_SIZE:
            return jsonify({
                "error": f"Batch too large. Maximum {_MAX_BATCH_SIZE} items per request.",
            }), 400

        conn = get_conn()
        ensure_offline_batch_tables(conn)

        ts = now_iso()
        results = []
        succeeded = 0
        failed = 0
        duplicates = 0

        for item in items:
            local_id = (str(item.get("local_id") or "")).strip()
            item_type = (item.get("type") or "").strip().lower()
            queued_at = (item.get("queued_at") or "").strip() or None
            payload = dict(item.get("payload") or {})
            if current_user.get("role") not in {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}:
                payload["organisation_id"] = current_user.get("user_org_id")
                payload["submitted_by_user_id"] = current_user.get("user_id")
                payload["submitted_by_display_name"] = current_user.get("display_name")
            organisation_id, submitted_by_user_id = _offline_log_context(current_user, payload)

            if not local_id:
                results.append({
                    "local_id": local_id or "(missing)",
                    "status": "error",
                    "error": "local_id is required for every item",
                })
                failed += 1
                continue

            if item_type not in _SUPPORTED_TYPES:
                results.append({
                    "local_id": local_id,
                    "status": "error",
                    "error": f"Unsupported type '{item_type}'. Use: {', '.join(sorted(_SUPPORTED_TYPES))}",
                })
                failed += 1
                continue

            # Check idempotency log
            existing = conn.execute(
                "SELECT * FROM offline_batch_log WHERE device_id = ? AND local_id = ?",
                (device_id, local_id),
            ).fetchone()

            if existing:
                results.append({
                    "local_id": local_id,
                    "status": "duplicate",
                    "type": item_type,
                    "server_id": existing["server_id"],
                    "original_status": existing["status"],
                    "processed_at": existing["processed_at"],
                })
                duplicates += 1
                continue

            if item_type == "transaction":
                _ensure_offline_transaction_schema(
                    conn,
                    ensure_transaction_numbering_tables=ensure_transaction_numbering_tables,
                    ensure_transaction_partner_columns=ensure_transaction_partner_columns,
                    ensure_partner_address_tables=ensure_partner_address_tables,
                    ensure_transaction_user_attribution_columns=ensure_transaction_user_attribution_columns,
                )
                ensure_partner_address_for_item = lambda _conn: None
            else:
                _ensure_offline_resource_loss_tables(conn)
                ensure_partner_address_for_item = ensure_partner_address_tables

            # Process the item inside its own savepoint so one bad upload
            # cannot crash or roll back the rest of the batch.
            conn.execute("SAVEPOINT batch_item")
            try:
                if item_type == "transaction":
                    server_id, detail = _process_transaction_item(
                        conn=conn,
                        payload=payload,
                        queued_at=queued_at,
                        current_user=current_user,
                        post_transaction_to_ledger=post_transaction_to_ledger,
                        generate_transaction_reference=generate_transaction_reference,
                        ensure_transaction_numbering_tables=ensure_transaction_numbering_tables,
                        ensure_transaction_partner_columns=ensure_transaction_partner_columns,
                        ensure_partner_address_tables=ensure_partner_address_for_item,
                        ensure_transaction_user_attribution_columns=ensure_transaction_user_attribution_columns,
                        create_pending_entry=create_pending_entry,
                    )
                else:
                    server_id, detail = _process_resource_loss_item(
                        conn=conn,
                        payload=payload,
                        queued_at=queued_at,
                        current_user=current_user,
                    )

                conn.execute(
                    """INSERT INTO offline_batch_log
                       (log_id, device_id, local_id, organisation_id, submitted_by_user_id,
                        item_type, status, server_id, processed_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'success', ?, ?)""",
                    (
                        make_id("obl"), device_id, local_id, organisation_id,
                        submitted_by_user_id, item_type, server_id, ts,
                    ),
                )
                conn.execute("RELEASE SAVEPOINT batch_item")
                conn.commit()

                results.append({
                    "local_id": local_id,
                    "status": "success",
                    "type": item_type,
                    "server_id": server_id,
                    "detail": detail,
                })
                succeeded += 1

            except Exception as exc:
                conn.execute("ROLLBACK TO SAVEPOINT batch_item")
                conn.execute("RELEASE SAVEPOINT batch_item")

                error_msg = str(exc)
                conn.execute(
                    """INSERT OR IGNORE INTO offline_batch_log
                       (log_id, device_id, local_id, organisation_id, submitted_by_user_id,
                        item_type, status, error_message, processed_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'error', ?, ?)""",
                    (
                        make_id("obl"), device_id, local_id, organisation_id,
                        submitted_by_user_id, item_type, error_msg, ts,
                    ),
                )
                conn.commit()

                results.append({
                    "local_id": local_id,
                    "status": "error",
                    "type": item_type,
                    "error": error_msg,
                })
                failed += 1

        conn.close()

        return jsonify({
            "device_id": device_id,
            "total": len(items),
            "succeeded": succeeded,
            "failed": failed,
            "duplicates": duplicates,
            "results": results,
        }), 200 if failed == 0 else 207  # 207 Multi-Status when some failed


    @app.get("/offline-batch/log")
    def get_offline_batch_log():
        """
        Get the upload history for a device. Useful for the app to
        verify which queued items were successfully processed.
        """
        device_id = (request.args.get("device_id") or "").strip()
        if not device_id:
            return jsonify({"error": "device_id query parameter is required"}), 400

        conn = get_conn()
        ensure_offline_batch_tables(conn)

        rows = conn.execute(
            """SELECT log_id, local_id, organisation_id, submitted_by_user_id,
                      item_type, status, server_id, error_message, processed_at
               FROM offline_batch_log WHERE device_id = ?
               ORDER BY processed_at DESC LIMIT 500""",
            (device_id,),
        ).fetchall()
        conn.close()

        return jsonify({
            "device_id": device_id,
            "count": len(rows),
            "items": [dict(r) for r in rows],
        }), 200
