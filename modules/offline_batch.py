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

_SUPPORTED_TYPES = {"transaction", "resource_loss"}
_LOSS_TYPES = {"DAMAGED", "STOLEN", "LOST", "DESTROYED", "OTHER"}
_MAX_BATCH_SIZE = 200


def ensure_offline_batch_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS offline_batch_log (
        log_id          TEXT PRIMARY KEY,
        device_id       TEXT NOT NULL,
        local_id        TEXT NOT NULL,
        item_type       TEXT NOT NULL,
        status          TEXT NOT NULL,
        server_id       TEXT,
        error_message   TEXT,
        processed_at    TEXT NOT NULL,
        UNIQUE (device_id, local_id)
    )
    """)
    conn.commit()


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

    if not organisation_id:
        raise ValueError("organisation_id is required")
    if not depot_id:
        raise ValueError("depot_id is required")
    if not resource_id:
        raise ValueError("resource_id is required")
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

    resource = conn.execute(
        "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ? AND is_active = 1",
        (resource_id, organisation_id),
    ).fetchone()
    if not resource:
        raise ValueError("Resource not found or inactive")

    partner = None
    if partner_id:
        partner = conn.execute(
            "SELECT * FROM partners WHERE partner_id = ? AND organisation_id = ? AND is_active = 1",
            (partner_id, organisation_id),
        ).fetchone()
        if not partner:
            raise ValueError("Partner not found")

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
            transaction_note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    # Ensure transaction_note column exists
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "transaction_note" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN transaction_note TEXT")

    if depot["opening_balance_used"] == 0:
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
            resource_name=resource["name"],
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
        conn.execute(
            insert_sql,
            (
                transaction_id, organisation_id, depot_id, transaction_type,
                resource_id, quantity, direction, "DRAFT",
                None, None,
                partner_id, partner_address_id,
                submitted_by_user_id, submitted_by_display_name,
                created_at, None,
                reference_number, org_sequence_number,
                transaction_note,
            ),
        )

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
        "resource_name": resource["name"],
        "quantity": quantity,
        "direction": direction,
        "status": status,
        "reference_number": reference_number,
        "org_sequence_number": org_sequence_number,
        "partner_id": partner_id,
        "partner_name": partner["name"] if partner else None,
        "submitted_by_display_name": submitted_by_display_name,
        "created_at": created_at,
        "queued_offline": True,
    }


def _process_resource_loss_item(conn, payload, queued_at, current_user):
    from modules.resource_loss import ensure_resource_loss_tables

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

    if not organisation_id:
        raise ValueError("organisation_id is required")
    if not depot_id:
        raise ValueError("depot_id is required")
    if not resource_id:
        raise ValueError("resource_id is required")
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

    ensure_resource_loss_tables(conn)

    depot = conn.execute(
        "SELECT * FROM depots WHERE depot_id = ? AND organisation_id = ?",
        (depot_id, organisation_id),
    ).fetchone()
    if not depot:
        raise ValueError("Depot not found")

    resource = conn.execute(
        "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ? AND is_active = 1",
        (resource_id, organisation_id),
    ).fetchone()
    if not resource:
        raise ValueError("Resource not found or inactive")

    created_at = queued_at or now_iso()
    loss_id = make_id("loss")
    effective_date = loss_date or created_at[:10]

    conn.execute(
        """INSERT INTO resource_losses (
            loss_id, organisation_id, depot_id, resource_id,
            quantity, loss_type, loss_reason, loss_date, status,
            reported_by_user_id, reported_by_display_name,
            partner_id, related_transaction_id,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_REVIEW', ?, ?, ?, ?, ?, ?)""",
        (
            loss_id, organisation_id, depot_id, resource_id,
            quantity, loss_type, loss_reason, effective_date,
            submitted_by_user_id,
            submitted_by_display_name,
            partner_id, related_transaction_id,
            created_at, created_at,
        ),
    )

    audit_event(
        conn, entity_type="ResourceLoss", entity_id=loss_id,
        action="OFFLINE_UPLOAD",
        summary=(
            f"Offline loss report uploaded by {submitted_by_display_name}: "
            f"{quantity} × {resource['name']} ({loss_type}) at {depot['name']}."
        ),
        organisation_id=organisation_id,
    )

    return loss_id, {
        "loss_id": loss_id,
        "organisation_id": organisation_id,
        "depot_id": depot_id,
        "depot_name": depot["name"],
        "resource_id": resource_id,
        "resource_name": resource["name"],
        "quantity": quantity,
        "loss_type": loss_type,
        "loss_reason": loss_reason,
        "loss_date": effective_date,
        "status": "PENDING_REVIEW",
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
            payload = item.get("payload") or {}

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

            # Process the item
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
                        ensure_partner_address_tables=ensure_partner_address_tables,
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
                       (log_id, device_id, local_id, item_type, status, server_id, processed_at)
                       VALUES (?, ?, ?, ?, 'success', ?, ?)""",
                    (make_id("obl"), device_id, local_id, item_type, server_id, ts),
                )
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
                # Roll back only this item — use a savepoint pattern
                conn.execute("ROLLBACK TO SAVEPOINT batch_item")

                error_msg = str(exc)
                conn.execute(
                    """INSERT OR IGNORE INTO offline_batch_log
                       (log_id, device_id, local_id, item_type, status, error_message, processed_at)
                       VALUES (?, ?, ?, ?, 'error', ?, ?)""",
                    (make_id("obl"), device_id, local_id, item_type, error_msg, ts),
                )
                conn.commit()

                results.append({
                    "local_id": local_id,
                    "status": "error",
                    "type": item_type,
                    "error": error_msg,
                })
                failed += 1

            # Set savepoint for next item
            conn.execute("SAVEPOINT batch_item")

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
            """SELECT log_id, local_id, item_type, status, server_id, error_message, processed_at
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
