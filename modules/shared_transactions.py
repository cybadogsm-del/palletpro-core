"""
modules/shared_transactions.py — Shared Transactions brick

Covers:
  - Schema helpers
      ensure_shared_transaction_tables
      ensure_qr_token_tables
      ensure_shared_transaction_partner_address_tables
  - Core helpers
      record_shared_transaction_event   (re-exported — used by partners module via main)
      validate_qr_handoff_token_for_action
      consume_qr_handoff_token
      get_partner_address_for_shared_transaction
      get_default_partner_address_for_shared_transaction
      get_shared_transaction_partner_address_payloads
  - Routes
      POST  /shared-transactions                                     (create)
      POST  /shared-transactions/<id>/generate-qr-token
      GET   /qr-handoff/<qr_token_id>
      POST  /qr-handoff/<qr_token_id>/scan
      GET   /organisations/<org_id>/shared-transactions             (list)
      GET   /shared-transactions/<id>                               (get)
      POST  /shared-transactions/<id>/propose-correction
      POST  /shared-transactions/<id>/accept-correction
      POST  /shared-transactions/<id>/reject-correction
      POST  /shared-transactions/<id>/confirm
      POST  /shared-transactions/<id>/dispute
"""

from flask import jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso
from modules.partners import ensure_partner_address_tables, build_partner_address_navigation_contract


# ── Schema helpers ─────────────────────────────────────────────────────────────

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


# ── Core helpers ───────────────────────────────────────────────────────────────

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


# ── Route registration ─────────────────────────────────────────────────────────

def register_shared_transaction_routes(app):

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
