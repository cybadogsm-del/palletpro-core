"""
Transaction Correction Request (TCR) module.

Field users raise a TCR when a posted transaction is wrong. It sits PENDING until
an Org Admin approves or rejects it. On approval the ledger is corrected by posting
a new CORRECTION transaction that applies the exact delta — the original is never
edited (LAW-001/LAW-002).
"""

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso

_VALID_TCR_STATUSES = {"PENDING", "APPROVED", "REJECTED"}
_ORG_ADMIN_ROLES = {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}


def ensure_tcr_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS transaction_correction_requests (
        tcr_id                      TEXT PRIMARY KEY,
        organisation_id             TEXT NOT NULL,
        transaction_id              TEXT NOT NULL,
        status                      TEXT NOT NULL DEFAULT 'PENDING',
        -- who raised it
        requested_by_user_id        TEXT,
        requested_by_display_name   TEXT NOT NULL,
        correction_reason           TEXT NOT NULL,
        -- proposed new values (NULL = keep original)
        proposed_quantity           INTEGER,
        proposed_direction          TEXT,
        proposed_partner_id         TEXT,
        proposed_depot_id           TEXT,
        proposed_resource_id        TEXT,
        -- review
        reviewed_by_display_name    TEXT,
        review_notes                TEXT,
        reviewed_at                 TEXT,
        -- correction transaction created on approval
        correction_transaction_id   TEXT,
        created_at                  TEXT NOT NULL,
        updated_at                  TEXT NOT NULL
    )
    """)
    # Link correction transactions back to their original
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "correction_of_transaction_id" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN correction_of_transaction_id TEXT")
    if "transaction_note" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN transaction_note TEXT")
    conn.commit()


def register_tcr_routes(
    app,
    post_transaction_to_ledger,
    generate_transaction_reference,
    ensure_transaction_numbering_tables,
    ensure_transaction_partner_columns,
    ensure_transaction_user_attribution_columns,
):

    @app.post("/transactions/<transaction_id>/correction-request")
    def create_correction_request(transaction_id):
        """
        Field user raises a TCR against a posted transaction.
        Does NOT touch the ledger — status is PENDING until Org Admin acts.
        """
        current_user = g.current_user
        body = request.get_json(silent=True) or {}

        correction_reason = (body.get("correction_reason") or "").strip()
        if not correction_reason:
            return jsonify({"error": "correction_reason is required"}), 400

        proposed_quantity = body.get("proposed_quantity")
        proposed_direction = (body.get("proposed_direction") or "").strip().upper() or None
        proposed_partner_id = body.get("proposed_partner_id") or None
        proposed_depot_id = body.get("proposed_depot_id") or None
        proposed_resource_id = body.get("proposed_resource_id") or None

        if proposed_quantity is not None:
            try:
                proposed_quantity = int(proposed_quantity)
                if proposed_quantity <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                return jsonify({"error": "proposed_quantity must be a positive integer"}), 400

        if proposed_direction and proposed_direction not in ("IN", "OUT"):
            return jsonify({"error": "proposed_direction must be IN or OUT"}), 400

        conn = get_conn()
        ensure_tcr_tables(conn)
        ensure_transaction_partner_columns(conn)
        ensure_transaction_user_attribution_columns(conn)

        txn = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,)
        ).fetchone()

        if not txn:
            conn.close()
            return jsonify({"error": "Transaction not found"}), 404

        if txn["status"] != "POSTED":
            conn.close()
            return jsonify({
                "error": "Only POSTED transactions can have a correction request",
                "status": txn["status"],
            }), 400

        if txn["transaction_type"] == "Correction":
            conn.close()
            return jsonify({"error": "Cannot raise a TCR against a correction transaction"}), 400

        # Prevent duplicate open TCRs on the same transaction
        existing = conn.execute(
            """SELECT tcr_id FROM transaction_correction_requests
               WHERE transaction_id = ? AND status = 'PENDING'""",
            (transaction_id,),
        ).fetchone()
        if existing:
            conn.close()
            return jsonify({
                "error": "A correction request for this transaction is already pending",
                "existing_tcr_id": existing["tcr_id"],
            }), 409

        # Validate proposed references belong to same org
        if proposed_partner_id:
            partner = conn.execute(
                "SELECT partner_id FROM partners WHERE partner_id = ? AND organisation_id = ?",
                (proposed_partner_id, txn["organisation_id"]),
            ).fetchone()
            if not partner:
                conn.close()
                return jsonify({"error": "Proposed partner not found in this organisation"}), 404

        if proposed_depot_id:
            depot = conn.execute(
                "SELECT depot_id FROM depots WHERE depot_id = ? AND organisation_id = ?",
                (proposed_depot_id, txn["organisation_id"]),
            ).fetchone()
            if not depot:
                conn.close()
                return jsonify({"error": "Proposed depot not found in this organisation"}), 404

        if proposed_resource_id:
            resource = conn.execute(
                "SELECT resource_id FROM resources WHERE resource_id = ? AND organisation_id = ? AND is_active = 1",
                (proposed_resource_id, txn["organisation_id"]),
            ).fetchone()
            if not resource:
                conn.close()
                return jsonify({"error": "Proposed resource not found in this organisation"}), 404

        ts = now_iso()
        tcr_id = make_id("tcr")

        conn.execute(
            """INSERT INTO transaction_correction_requests (
                tcr_id, organisation_id, transaction_id, status,
                requested_by_user_id, requested_by_display_name, correction_reason,
                proposed_quantity, proposed_direction, proposed_partner_id,
                proposed_depot_id, proposed_resource_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tcr_id,
                txn["organisation_id"],
                transaction_id,
                current_user["user_id"] if current_user["user_id"] != "master" else None,
                current_user["display_name"],
                correction_reason,
                proposed_quantity,
                proposed_direction,
                proposed_partner_id,
                proposed_depot_id,
                proposed_resource_id,
                ts,
                ts,
            ),
        )

        audit_event(
            conn,
            entity_type="TransactionCorrectionRequest",
            entity_id=tcr_id,
            action="CREATE",
            summary=(
                f"TCR raised by {current_user['display_name']} for transaction "
                f"{transaction_id}: {correction_reason}"
            ),
            organisation_id=txn["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({
            "tcr_id": tcr_id,
            "transaction_id": transaction_id,
            "status": "PENDING",
            "correction_reason": correction_reason,
            "proposed_quantity": proposed_quantity,
            "proposed_direction": proposed_direction,
            "proposed_partner_id": proposed_partner_id,
            "proposed_depot_id": proposed_depot_id,
            "proposed_resource_id": proposed_resource_id,
            "requested_by_display_name": current_user["display_name"],
            "created_at": ts,
            "message": "Correction request submitted. Awaiting Org Admin review.",
        }), 201


    @app.get("/organisations/<organisation_id>/correction-requests")
    def list_correction_requests(organisation_id):
        """List TCRs for an organisation. Filterable by status."""
        status_filter = (request.args.get("status") or "").strip().upper() or None

        conn = get_conn()
        ensure_tcr_tables(conn)

        sql = """
            SELECT tcr.*, t.transaction_type, t.quantity AS original_quantity,
                   t.direction AS original_direction, t.resource_id AS original_resource_id,
                   t.depot_id AS original_depot_id,
                   r.name AS original_resource_name, d.name AS original_depot_name
            FROM transaction_correction_requests tcr
            JOIN transactions t ON t.transaction_id = tcr.transaction_id
            LEFT JOIN resources r ON r.resource_id = t.resource_id
            LEFT JOIN depots d ON d.depot_id = t.depot_id
            WHERE tcr.organisation_id = ?
        """
        params = [organisation_id]

        if status_filter:
            if status_filter not in _VALID_TCR_STATUSES:
                conn.close()
                return jsonify({"error": f"Invalid status. Use: {', '.join(sorted(_VALID_TCR_STATUSES))}"}), 400
            sql += " AND tcr.status = ?"
            params.append(status_filter)

        sql += " ORDER BY tcr.created_at DESC"

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "status_filter": status_filter,
            "count": len(rows),
            "correction_requests": [dict(r) for r in rows],
        }), 200


    @app.get("/correction-requests/<tcr_id>")
    def get_correction_request(tcr_id):
        """Get a single TCR with full original transaction detail."""
        conn = get_conn()
        ensure_tcr_tables(conn)
        ensure_transaction_partner_columns(conn)
        ensure_transaction_user_attribution_columns(conn)
        ensure_transaction_numbering_tables(conn)

        tcr = conn.execute(
            "SELECT * FROM transaction_correction_requests WHERE tcr_id = ?", (tcr_id,)
        ).fetchone()

        if not tcr:
            conn.close()
            return jsonify({"error": "Correction request not found"}), 404

        txn = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?",
            (tcr["transaction_id"],),
        ).fetchone()

        resource = None
        if txn:
            resource = conn.execute(
                "SELECT resource_id, name, resource_type, unit_type FROM resources WHERE resource_id = ?",
                (txn["resource_id"],),
            ).fetchone()

        conn.close()

        return jsonify({
            "tcr": dict(tcr),
            "original_transaction": dict(txn) if txn else None,
            "original_resource": dict(resource) if resource else None,
        }), 200


    @app.post("/correction-requests/<tcr_id>/approve")
    def approve_correction_request(tcr_id):
        """
        Org Admin approves a TCR.

        Creates a CORRECTION transaction that posts the exact ledger delta needed to
        move from the original (wrong) state to the proposed (correct) state.
        The original transaction is never modified (LAW-001/LAW-002).
        """
        current_user = g.current_user
        if current_user["role"] not in _ORG_ADMIN_ROLES:
            return jsonify({"error": "Only Org Admin or above can approve correction requests"}), 403

        body = request.get_json(silent=True) or {}
        review_notes = (body.get("review_notes") or "").strip() or None

        conn = get_conn()
        ensure_tcr_tables(conn)
        ensure_transaction_partner_columns(conn)
        ensure_transaction_user_attribution_columns(conn)
        ensure_transaction_numbering_tables(conn)

        tcr = conn.execute(
            "SELECT * FROM transaction_correction_requests WHERE tcr_id = ?", (tcr_id,)
        ).fetchone()

        if not tcr:
            conn.close()
            return jsonify({"error": "Correction request not found"}), 404

        if tcr["status"] != "PENDING":
            conn.close()
            return jsonify({
                "error": f"Cannot approve a TCR with status '{tcr['status']}'",
            }), 400

        txn = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?",
            (tcr["transaction_id"],),
        ).fetchone()

        if not txn:
            conn.close()
            return jsonify({"error": "Original transaction not found"}), 404

        # Resolve final values: proposed overrides original where specified
        final_quantity = tcr["proposed_quantity"] if tcr["proposed_quantity"] is not None else txn["quantity"]
        final_direction = tcr["proposed_direction"] if tcr["proposed_direction"] else txn["direction"]
        final_resource_id = tcr["proposed_resource_id"] if tcr["proposed_resource_id"] else txn["resource_id"]
        final_depot_id = tcr["proposed_depot_id"] if tcr["proposed_depot_id"] else txn["depot_id"]
        final_partner_id = tcr["proposed_partner_id"] if tcr["proposed_partner_id"] else txn["partner_id"]

        # Calculate the delta: what was posted vs what should have been posted
        original_delta = txn["quantity"] if txn["direction"] == "IN" else -txn["quantity"]
        correct_delta = final_quantity if final_direction == "IN" else -final_quantity

        # If resource or depot changed, we must fully reverse the original and post the new
        resource_changed = final_resource_id != txn["resource_id"]
        depot_changed = final_depot_id != txn["depot_id"]
        needs_full_reversal = resource_changed or depot_changed

        ts = now_iso()
        correction_transaction_id = make_id("txn")
        ref_number, org_seq = generate_transaction_reference(conn, txn["organisation_id"])
        correction_transactions = []

        if needs_full_reversal:
            # Step 1: reverse the original completely
            reversal_id = make_id("txn")
            reversal_ref, reversal_seq = generate_transaction_reference(conn, txn["organisation_id"])
            reversal_direction = "OUT" if txn["direction"] == "IN" else "IN"

            conn.execute(
                """INSERT INTO transactions (
                    transaction_id, organisation_id, depot_id, transaction_type,
                    resource_id, quantity, direction, status, created_at, posted_at,
                    correction_of_transaction_id, reference_number, org_sequence_number,
                    partner_id, submitted_by_display_name
                ) VALUES (?, ?, ?, 'Correction', ?, ?, ?, 'POSTED', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    reversal_id, txn["organisation_id"], txn["depot_id"],
                    txn["resource_id"], txn["quantity"], reversal_direction,
                    ts, ts, txn["transaction_id"],
                    reversal_ref, reversal_seq,
                    final_partner_id, current_user["display_name"],
                ),
            )
            reversal_txn = conn.execute(
                "SELECT * FROM transactions WHERE transaction_id = ?", (reversal_id,)
            ).fetchone()
            post_transaction_to_ledger(conn, reversal_txn)
            correction_transactions.append(reversal_id)

            # Step 2: post the correct transaction
            conn.execute(
                """INSERT INTO transactions (
                    transaction_id, organisation_id, depot_id, transaction_type,
                    resource_id, quantity, direction, status, created_at, posted_at,
                    correction_of_transaction_id, reference_number, org_sequence_number,
                    partner_id, submitted_by_display_name
                ) VALUES (?, ?, ?, 'Correction', ?, ?, ?, 'POSTED', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    correction_transaction_id, txn["organisation_id"], final_depot_id,
                    final_resource_id, final_quantity, final_direction,
                    ts, ts, txn["transaction_id"],
                    ref_number, org_seq,
                    final_partner_id, current_user["display_name"],
                ),
            )
            correction_txn = conn.execute(
                "SELECT * FROM transactions WHERE transaction_id = ?",
                (correction_transaction_id,),
            ).fetchone()
            post_transaction_to_ledger(conn, correction_txn)
            correction_transactions.append(correction_transaction_id)

        else:
            # Same resource and depot: post a single delta correction
            net_delta = correct_delta - original_delta
            if net_delta == 0:
                # Only metadata changed (partner) — no ledger correction needed
                correction_transaction_id = None
            else:
                delta_direction = "IN" if net_delta > 0 else "OUT"
                delta_quantity = abs(net_delta)

                conn.execute(
                    """INSERT INTO transactions (
                        transaction_id, organisation_id, depot_id, transaction_type,
                        resource_id, quantity, direction, status, created_at, posted_at,
                        correction_of_transaction_id, reference_number, org_sequence_number,
                        partner_id, submitted_by_display_name
                    ) VALUES (?, ?, ?, 'Correction', ?, ?, ?, 'POSTED', ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        correction_transaction_id, txn["organisation_id"], final_depot_id,
                        final_resource_id, delta_quantity, delta_direction,
                        ts, ts, txn["transaction_id"],
                        ref_number, org_seq,
                        final_partner_id, current_user["display_name"],
                    ),
                )
                correction_txn = conn.execute(
                    "SELECT * FROM transactions WHERE transaction_id = ?",
                    (correction_transaction_id,),
                ).fetchone()
                post_transaction_to_ledger(conn, correction_txn)
                correction_transactions.append(correction_transaction_id)

        # Update TCR status
        conn.execute(
            """UPDATE transaction_correction_requests
               SET status = 'APPROVED', reviewed_by_display_name = ?, review_notes = ?,
                   reviewed_at = ?, correction_transaction_id = ?, updated_at = ?
               WHERE tcr_id = ?""",
            (
                current_user["display_name"],
                review_notes,
                ts,
                correction_transaction_id,
                ts,
                tcr_id,
            ),
        )

        audit_event(
            conn,
            entity_type="TransactionCorrectionRequest",
            entity_id=tcr_id,
            action="APPROVE",
            summary=(
                f"TCR approved by {current_user['display_name']}. "
                f"Correction transaction(s): {correction_transactions or 'metadata only'}."
            ),
            organisation_id=txn["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({
            "tcr_id": tcr_id,
            "status": "APPROVED",
            "reviewed_by": current_user["display_name"],
            "review_notes": review_notes,
            "correction_transactions": correction_transactions,
            "ledger_updated": len(correction_transactions) > 0,
            "message": "Correction approved and ledger updated." if correction_transactions else "Correction approved. No ledger change was required.",
        }), 200


    @app.post("/correction-requests/<tcr_id>/reject")
    def reject_correction_request(tcr_id):
        """Org Admin rejects a TCR. No ledger change. Reason is recorded."""
        current_user = g.current_user
        if current_user["role"] not in _ORG_ADMIN_ROLES:
            return jsonify({"error": "Only Org Admin or above can reject correction requests"}), 403

        body = request.get_json(silent=True) or {}
        review_notes = (body.get("review_notes") or "").strip()

        if not review_notes:
            return jsonify({"error": "review_notes are required when rejecting a TCR"}), 400

        conn = get_conn()
        ensure_tcr_tables(conn)

        tcr = conn.execute(
            "SELECT * FROM transaction_correction_requests WHERE tcr_id = ?", (tcr_id,)
        ).fetchone()

        if not tcr:
            conn.close()
            return jsonify({"error": "Correction request not found"}), 404

        if tcr["status"] != "PENDING":
            conn.close()
            return jsonify({
                "error": f"Cannot reject a TCR with status '{tcr['status']}'",
            }), 400

        ts = now_iso()
        conn.execute(
            """UPDATE transaction_correction_requests
               SET status = 'REJECTED', reviewed_by_display_name = ?, review_notes = ?,
                   reviewed_at = ?, updated_at = ?
               WHERE tcr_id = ?""",
            (current_user["display_name"], review_notes, ts, ts, tcr_id),
        )

        audit_event(
            conn,
            entity_type="TransactionCorrectionRequest",
            entity_id=tcr_id,
            action="REJECT",
            summary=(
                f"TCR rejected by {current_user['display_name']}: {review_notes}"
            ),
            organisation_id=tcr["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({
            "tcr_id": tcr_id,
            "status": "REJECTED",
            "reviewed_by": current_user["display_name"],
            "review_notes": review_notes,
            "message": "Correction request rejected. Original transaction unchanged.",
        }), 200
