"""
Resource Loss module.

Field users report resources that are damaged, stolen, lost, or destroyed.
The report sits PENDING_REVIEW until an Org Admin confirms or rejects it.
On confirmation a ResourceLoss transaction is posted to the ledger — reducing
the balance at the relevant depot. The original report is never deleted.
"""

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso
from modules.web_push import send_push_to_user

_LOSS_TYPES = {"DAMAGED", "STOLEN", "LOST", "DESTROYED", "OTHER"}
_VALID_STATUSES = {"PENDING_REVIEW", "CONFIRMED", "REJECTED"}
_ORG_ADMIN_ROLES = {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}


def ensure_resource_loss_tables(conn):
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
        -- who reported it
        reported_by_user_id         TEXT,
        reported_by_display_name    TEXT NOT NULL,
        -- optional context
        partner_id                  TEXT,
        related_transaction_id      TEXT,
        -- review
        reviewed_by_display_name    TEXT,
        review_notes                TEXT,
        reviewed_at                 TEXT,
        -- ledger linkage (set on confirmation)
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
    conn.commit()


def register_resource_loss_routes(
    app,
    post_transaction_to_ledger,
    generate_transaction_reference,
    ensure_transaction_numbering_tables,
    ensure_transaction_partner_columns,
    ensure_transaction_user_attribution_columns,
):

    @app.post("/resource-loss")
    def create_resource_loss():
        """
        Field user reports a resource loss. Status is PENDING_REVIEW.
        The ledger is NOT updated until an Org Admin confirms.
        """
        current_user = g.current_user
        body = request.get_json(silent=True) or {}

        organisation_id = body.get("organisation_id")
        depot_id = body.get("depot_id")
        resource_id = body.get("resource_id")
        quantity = body.get("quantity")
        loss_type = (body.get("loss_type") or "").strip().upper()
        loss_reason = (body.get("loss_reason") or "").strip()
        loss_date = (body.get("loss_date") or "").strip() or None
        partner_id = body.get("partner_id") or None
        related_transaction_id = body.get("related_transaction_id") or None
        unresolved_entity_note = (body.get("unresolved_entity_note") or "").strip() or None
        unresolved_entity_type = (body.get("unresolved_entity_type") or "").strip().upper() or None

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400
        if not depot_id:
            return jsonify({"error": "depot_id is required"}), 400
        if not resource_id and not unresolved_entity_note:
            return jsonify({"error": "resource_id is required (or provide unresolved_entity_note if entity is missing)"}), 400
        if quantity is None:
            return jsonify({"error": "quantity is required"}), 400
        if not loss_type:
            return jsonify({"error": "loss_type is required"}), 400
        if not loss_reason:
            return jsonify({"error": "loss_reason is required"}), 400

        try:
            quantity = int(quantity)
            if quantity <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"error": "quantity must be a positive integer"}), 400

        if loss_type not in _LOSS_TYPES:
            return jsonify({
                "error": f"Invalid loss_type. Use: {', '.join(sorted(_LOSS_TYPES))}",
            }), 400

        conn = get_conn()
        ensure_resource_loss_tables(conn)
        ensure_transaction_partner_columns(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?", (organisation_id,)
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        depot = conn.execute(
            "SELECT * FROM depots WHERE depot_id = ? AND organisation_id = ?",
            (depot_id, organisation_id),
        ).fetchone()
        if not depot:
            conn.close()
            return jsonify({"error": "Depot not found"}), 404

        resource = None
        if resource_id:
            resource = conn.execute(
                "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ? AND is_active = 1",
                (resource_id, organisation_id),
            ).fetchone()
            if not resource and not unresolved_entity_note:
                conn.close()
                return jsonify({"error": "Resource not found or inactive"}), 404

        if not resource_id and unresolved_entity_note:
            resource_id = "UNRESOLVED"

        if partner_id:
            partner = conn.execute(
                "SELECT partner_id FROM partners WHERE partner_id = ? AND organisation_id = ? AND is_active = 1",
                (partner_id, organisation_id),
            ).fetchone()
            if not partner:
                conn.close()
                return jsonify({"error": "Partner not found"}), 404

        if related_transaction_id:
            txn = conn.execute(
                "SELECT transaction_id FROM transactions WHERE transaction_id = ? AND organisation_id = ?",
                (related_transaction_id, organisation_id),
            ).fetchone()
            if not txn:
                conn.close()
                return jsonify({"error": "Related transaction not found"}), 404

        ts = now_iso()
        loss_id = make_id("loss")
        effective_date = loss_date or ts[:10]

        resource_name = resource["name"] if resource else None

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
                current_user["user_id"] if current_user["user_id"] != "master" else None,
                current_user["display_name"],
                partner_id, related_transaction_id,
                unresolved_entity_note, unresolved_entity_type,
                ts, ts,
            ),
        )

        if unresolved_entity_note:
            audit_event(
                conn,
                entity_type="ResourceLoss",
                entity_id=loss_id,
                action="REPORT",
                summary=(
                    f"{current_user['display_name']} reported loss of {quantity} items "
                    f"({loss_type}) at depot {depot['name']} — unresolved entity: {unresolved_entity_note}."
                ),
                organisation_id=organisation_id,
            )
        else:
            audit_event(
                conn,
                entity_type="ResourceLoss",
                entity_id=loss_id,
                action="REPORT",
                summary=(
                    f"{current_user['display_name']} reported loss of {quantity} × "
                    f"{resource_name} ({loss_type}) at depot {depot['name']}."
                ),
                organisation_id=organisation_id,
            )

        conn.commit()
        conn.close()

        return jsonify({
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
            "unresolved_entity_type": unresolved_entity_type,
            "reported_by_display_name": current_user["display_name"],
            "created_at": ts,
            "message": (
                "Loss reported. Your Org Admin will resolve the missing entity and confirm the loss."
                if unresolved_entity_note
                else "Loss reported. Awaiting Org Admin review."
            ),
        }), 201


    @app.get("/organisations/<organisation_id>/resource-losses")
    def list_resource_losses(organisation_id):
        """List resource loss reports for an organisation. Filterable by status and loss_type."""
        status_filter = (request.args.get("status") or "").strip().upper() or None
        loss_type_filter = (request.args.get("loss_type") or "").strip().upper() or None

        conn = get_conn()
        ensure_resource_loss_tables(conn)

        sql = """
            SELECT l.*,
                   r.name AS resource_name, r.resource_type, r.unit_type,
                   d.name AS depot_name
            FROM resource_losses l
            LEFT JOIN resources r ON r.resource_id = l.resource_id
            LEFT JOIN depots d ON d.depot_id = l.depot_id
            WHERE l.organisation_id = ?
        """
        params = [organisation_id]

        if status_filter:
            if status_filter not in _VALID_STATUSES:
                conn.close()
                return jsonify({"error": f"Invalid status. Use: {', '.join(sorted(_VALID_STATUSES))}"}), 400
            sql += " AND l.status = ?"
            params.append(status_filter)

        if loss_type_filter:
            if loss_type_filter not in _LOSS_TYPES:
                conn.close()
                return jsonify({"error": f"Invalid loss_type. Use: {', '.join(sorted(_LOSS_TYPES))}"}), 400
            sql += " AND l.loss_type = ?"
            params.append(loss_type_filter)

        sql += " ORDER BY l.created_at DESC"

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "status_filter": status_filter,
            "loss_type_filter": loss_type_filter,
            "count": len(rows),
            "resource_losses": [dict(r) for r in rows],
        }), 200


    @app.get("/resource-losses/<loss_id>")
    def get_resource_loss(loss_id):
        """Get a single resource loss report with full context."""
        conn = get_conn()
        ensure_resource_loss_tables(conn)

        row = conn.execute(
            """SELECT l.*,
                      r.name AS resource_name, r.resource_type, r.unit_type,
                      d.name AS depot_name,
                      p.name AS partner_name
               FROM resource_losses l
               LEFT JOIN resources r ON r.resource_id = l.resource_id
               LEFT JOIN depots d ON d.depot_id = l.depot_id
               LEFT JOIN partners p ON p.partner_id = l.partner_id
               WHERE l.loss_id = ?""",
            (loss_id,),
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Resource loss report not found"}), 404

        conn.close()
        return jsonify(dict(row)), 200


    @app.post("/resource-losses/<loss_id>/confirm")
    def confirm_resource_loss(loss_id):
        """
        Org Admin confirms a resource loss.
        Posts a ResourceLoss transaction (OUT) to the ledger — balance reduced.
        """
        current_user = g.current_user
        if current_user["role"] not in _ORG_ADMIN_ROLES:
            return jsonify({"error": "Only Org Admin or above can confirm resource losses"}), 403

        body = request.get_json(silent=True) or {}
        review_notes = (body.get("review_notes") or "").strip() or None

        conn = get_conn()
        ensure_resource_loss_tables(conn)
        ensure_transaction_partner_columns(conn)
        ensure_transaction_user_attribution_columns(conn)
        ensure_transaction_numbering_tables(conn)

        loss = conn.execute(
            "SELECT * FROM resource_losses WHERE loss_id = ?", (loss_id,)
        ).fetchone()

        if not loss:
            conn.close()
            return jsonify({"error": "Resource loss report not found"}), 404

        if loss["status"] != "PENDING_REVIEW":
            conn.close()
            return jsonify({
                "error": f"Cannot confirm a loss report with status '{loss['status']}'",
            }), 400

        ts = now_iso()
        loss_txn_id = make_id("txn")
        ref_number, org_seq = generate_transaction_reference(conn, loss["organisation_id"])

        # Post an OUT transaction of type ResourceLoss — reduces the depot balance
        conn.execute(
            """INSERT INTO transactions (
                transaction_id, organisation_id, depot_id, transaction_type,
                resource_id, quantity, direction, status,
                approval_reason_code, approval_reason_text,
                partner_id, submitted_by_user_id, submitted_by_display_name,
                created_at, posted_at, reference_number, org_sequence_number
            ) VALUES (?, ?, ?, 'ResourceLoss', ?, ?, 'OUT', 'POSTED',
                      ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                loss_txn_id, loss["organisation_id"], loss["depot_id"],
                loss["resource_id"], loss["quantity"],
                loss["loss_type"], loss["loss_reason"],
                loss["partner_id"],
                current_user["user_id"] if current_user["user_id"] != "master" else None,
                current_user["display_name"],
                ts, ts, ref_number, org_seq,
            ),
        )

        loss_txn = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?", (loss_txn_id,)
        ).fetchone()
        post_transaction_to_ledger(conn, loss_txn)

        conn.execute(
            """UPDATE resource_losses
               SET status = 'CONFIRMED', reviewed_by_display_name = ?,
                   review_notes = ?, reviewed_at = ?,
                   loss_transaction_id = ?, updated_at = ?
               WHERE loss_id = ?""",
            (current_user["display_name"], review_notes, ts, loss_txn_id, ts, loss_id),
        )

        resource = conn.execute(
            "SELECT name FROM resources WHERE resource_id = ?", (loss["resource_id"],)
        ).fetchone()

        audit_event(
            conn,
            entity_type="ResourceLoss",
            entity_id=loss_id,
            action="CONFIRM",
            summary=(
                f"Loss of {loss['quantity']} × {resource['name'] if resource else loss['resource_id']} "
                f"confirmed by {current_user['display_name']}. "
                f"Ledger updated via transaction {loss_txn_id}."
            ),
            organisation_id=loss["organisation_id"],
        )

        conn.commit()
        conn.close()

        if loss["reported_by_user_id"]:
            resource_label = resource["name"] if resource else "equipment"
            send_push_to_user(
                loss["reported_by_user_id"],
                title="Loss report confirmed",
                body=(
                    f"Your loss report for {loss['quantity']} × {resource_label} "
                    f"has been confirmed. Balance updated."
                ),
                url=f"/resource-losses/{loss_id}",
                tag=f"loss-{loss_id}",
            )

        return jsonify({
            "loss_id": loss_id,
            "status": "CONFIRMED",
            "loss_transaction_id": loss_txn_id,
            "reviewed_by": current_user["display_name"],
            "review_notes": review_notes,
            "message": "Loss confirmed. Balance updated.",
        }), 200


    @app.post("/resource-losses/<loss_id>/reject")
    def reject_resource_loss(loss_id):
        """
        Org Admin rejects a resource loss report.
        No ledger change. Reason is required.
        """
        current_user = g.current_user
        if current_user["role"] not in _ORG_ADMIN_ROLES:
            return jsonify({"error": "Only Org Admin or above can reject resource losses"}), 403

        body = request.get_json(silent=True) or {}
        review_notes = (body.get("review_notes") or "").strip()

        if not review_notes:
            return jsonify({"error": "review_notes are required when rejecting a loss report"}), 400

        conn = get_conn()
        ensure_resource_loss_tables(conn)

        loss = conn.execute(
            "SELECT * FROM resource_losses WHERE loss_id = ?", (loss_id,)
        ).fetchone()

        if not loss:
            conn.close()
            return jsonify({"error": "Resource loss report not found"}), 404

        if loss["status"] != "PENDING_REVIEW":
            conn.close()
            return jsonify({
                "error": f"Cannot reject a loss report with status '{loss['status']}'",
            }), 400

        ts = now_iso()
        conn.execute(
            """UPDATE resource_losses
               SET status = 'REJECTED', reviewed_by_display_name = ?,
                   review_notes = ?, reviewed_at = ?, updated_at = ?
               WHERE loss_id = ?""",
            (current_user["display_name"], review_notes, ts, ts, loss_id),
        )

        audit_event(
            conn,
            entity_type="ResourceLoss",
            entity_id=loss_id,
            action="REJECT",
            summary=f"Loss report rejected by {current_user['display_name']}: {review_notes}",
            organisation_id=loss["organisation_id"],
        )

        conn.commit()
        conn.close()

        if loss["reported_by_user_id"]:
            send_push_to_user(
                loss["reported_by_user_id"],
                title="Loss report not approved",
                body=f"Your loss report was not approved. Reason: {review_notes}",
                url=f"/resource-losses/{loss_id}",
                tag=f"loss-{loss_id}",
            )

        return jsonify({
            "loss_id": loss_id,
            "status": "REJECTED",
            "reviewed_by": current_user["display_name"],
            "review_notes": review_notes,
            "message": "Loss report rejected. No ledger change.",
        }), 200


    @app.patch("/resource-losses/<loss_id>/resolve-entity")
    def resolve_resource_loss_entity(loss_id):
        """
        Org Admin resolves a missing entity on a resource loss report.

        Once the missing resource has been created, supply the correct resource_id.
        The loss report is updated so the normal confirm flow can proceed.
        """
        current_user = g.current_user
        if current_user["role"] not in _ORG_ADMIN_ROLES:
            return jsonify({"error": "Only Org Admin or above can resolve entity issues"}), 403

        body = request.get_json(silent=True) or {}
        new_resource_id = (body.get("resource_id") or "").strip() or None
        review_notes = (body.get("review_notes") or "").strip() or None

        conn = get_conn()
        ensure_resource_loss_tables(conn)

        loss = conn.execute(
            "SELECT * FROM resource_losses WHERE loss_id = ?", (loss_id,)
        ).fetchone()

        if not loss:
            conn.close()
            return jsonify({"error": "Resource loss report not found"}), 404

        if not loss["unresolved_entity_note"]:
            conn.close()
            return jsonify({"error": "This loss report does not have an unresolved entity"}), 400

        if loss["status"] != "PENDING_REVIEW":
            conn.close()
            return jsonify({"error": f"Cannot resolve entity on a loss report with status '{loss['status']}'"}), 400

        organisation_id = loss["organisation_id"]

        if loss["resource_id"] == "UNRESOLVED":
            if not new_resource_id:
                conn.close()
                return jsonify({"error": "resource_id is required — the original report had no resource"}), 400
            resource = conn.execute(
                "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ? AND is_active = 1",
                (new_resource_id, organisation_id),
            ).fetchone()
            if not resource:
                conn.close()
                return jsonify({"error": "Resource not found or inactive"}), 404
        else:
            resource = conn.execute(
                "SELECT name FROM resources WHERE resource_id = ?", (loss["resource_id"],)
            ).fetchone()
            new_resource_id = loss["resource_id"]

        ts = now_iso()
        conn.execute(
            """UPDATE resource_losses
               SET resource_id = ?,
                   unresolved_entity_note = NULL,
                   unresolved_entity_type = NULL,
                   updated_at = ?
               WHERE loss_id = ?""",
            (new_resource_id, ts, loss_id),
        )

        audit_event(
            conn,
            entity_type="ResourceLoss",
            entity_id=loss_id,
            action="ENTITY_RESOLVED",
            summary=(
                f"{current_user['display_name']} resolved missing entity on loss report {loss_id}. "
                f"Resource: {resource['name'] if resource else new_resource_id}. "
                f"Notes: {review_notes or 'none'}. Report is now ready to confirm."
            ),
            organisation_id=organisation_id,
        )

        conn.commit()
        conn.close()

        return jsonify({
            "loss_id": loss_id,
            "status": "PENDING_REVIEW",
            "resource_id": new_resource_id,
            "resource_name": resource["name"] if resource else None,
            "reviewed_by": current_user["display_name"],
            "review_notes": review_notes,
            "message": "Entity resolved. Loss report is now ready to confirm.",
        }), 200
