from collections import defaultdict

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn as _get_conn, make_id, now_iso

_ORG_ADMIN_ROLES = {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}


def ensure_stocktake_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS stocktake_sessions (
        stocktake_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        depot_id TEXT NOT NULL,
        depot_name TEXT NOT NULL,
        status TEXT NOT NULL,
        snapshot_taken_at TEXT NOT NULL,
        initiated_by_display_name TEXT NOT NULL,
        submitted_by_display_name TEXT,
        submitted_at TEXT,
        posted_by_display_name TEXT,
        posted_at TEXT,
        cancelled_at TEXT,
        cancelled_by_display_name TEXT,
        notes TEXT,
        total_lines INTEGER NOT NULL DEFAULT 0,
        lines_counted INTEGER NOT NULL DEFAULT 0,
        variance_lines INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS stocktake_lines (
        stocktake_line_id TEXT PRIMARY KEY,
        stocktake_id TEXT NOT NULL,
        organisation_id TEXT NOT NULL,
        depot_id TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        resource_name TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        unit_type TEXT NOT NULL,
        expected_quantity INTEGER NOT NULL,
        counted_quantity INTEGER,
        variance INTEGER,
        count_notes TEXT,
        counted_by_display_name TEXT,
        counted_at TEXT,
        adjustment_transaction_id TEXT,
        review_status TEXT,
        reviewed_by_display_name TEXT,
        reviewed_at TEXT,
        rejection_reason TEXT
    )
    """)

    # Migration: add review columns to existing tables
    line_cols = {r["name"] for r in conn.execute("PRAGMA table_info(stocktake_lines)").fetchall()}
    for col in ("review_status", "reviewed_by_display_name", "reviewed_at", "rejection_reason"):
        if col not in line_cols:
            conn.execute(f"ALTER TABLE stocktake_lines ADD COLUMN {col} TEXT")


def _get_depot(conn, depot_id, organisation_id):
    return conn.execute(
        "SELECT * FROM depots WHERE depot_id = ? AND organisation_id = ?",
        (depot_id, organisation_id)
    ).fetchone()


def _get_stocktake(conn, stocktake_id, organisation_id):
    return conn.execute(
        "SELECT * FROM stocktake_sessions WHERE stocktake_id = ? AND organisation_id = ?",
        (stocktake_id, organisation_id)
    ).fetchone()


def _get_lines(conn, stocktake_id):
    return conn.execute(
        """
        SELECT * FROM stocktake_lines
        WHERE stocktake_id = ?
        ORDER BY resource_type ASC, resource_name ASC
        """,
        (stocktake_id,)
    ).fetchall()


def _group_lines_by_resource_type(lines):
    groups = defaultdict(list)
    for line in lines:
        groups[line["resource_type"]].append(dict(line))
    return dict(groups)


def _update_session_counts(conn, stocktake_id):
    lines = _get_lines(conn, stocktake_id)
    total = len(lines)
    counted = sum(1 for l in lines if l["counted_quantity"] is not None)
    variances = sum(1 for l in lines if l["variance"] is not None and l["variance"] != 0)
    conn.execute(
        """
        UPDATE stocktake_sessions
        SET total_lines = ?, lines_counted = ?, variance_lines = ?
        WHERE stocktake_id = ?
        """,
        (total, counted, variances, stocktake_id),
    )


def _require_org_admin_or_above(action_label):
    current_user = g.current_user
    if current_user["role"] not in _ORG_ADMIN_ROLES:
        return jsonify({
            "error": "INSUFFICIENT_ROLE",
            "message": f"Only Org Admin or above can {action_label}.",
            "your_role": current_user["role"],
        }), 403
    return None


def register_stocktake_routes(
    app,
    post_transaction_to_ledger,
    generate_transaction_reference,
    ensure_transaction_numbering_tables,
    ensure_transaction_partner_columns,
    ensure_transaction_user_attribution_columns,
):

    @app.post("/organisations/<organisation_id>/stocktake")
    def initiate_stocktake(organisation_id):
        body = request.get_json(silent=True) or {}
        depot_id = (body.get("depot_id") or "").strip()
        initiated_by = (body.get("initiated_by_display_name") or "").strip()
        notes = (body.get("notes") or "").strip() or None

        if not depot_id:
            return jsonify({"error": "depot_id is required"}), 400
        if not initiated_by:
            return jsonify({"error": "initiated_by_display_name is required"}), 400

        conn = _get_conn()
        ensure_stocktake_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        depot = _get_depot(conn, depot_id, organisation_id)
        if not depot:
            conn.close()
            return jsonify({"error": "Depot not found"}), 404

        active = conn.execute(
            """
            SELECT stocktake_id FROM stocktake_sessions
            WHERE organisation_id = ? AND depot_id = ?
              AND status IN ('IN_PROGRESS', 'PENDING_REVIEW')
            """,
            (organisation_id, depot_id),
        ).fetchone()
        if active:
            conn.close()
            return jsonify({
                "error": "STOCKTAKE_ALREADY_ACTIVE",
                "dialog": {
                    "title": "Stocktake already in progress",
                    "message": (
                        f"There is already an active stocktake for depot '{depot['name']}'. "
                        "Complete or cancel it before starting a new one."
                    ),
                    "primary_action": {
                        "label": "View Stocktake",
                        "route": f"/organisations/{organisation_id}/stocktake/{active['stocktake_id']}",
                        "action_type": "NAVIGATE",
                    },
                },
                "active_stocktake_id": active["stocktake_id"],
            }), 409

        # Snapshot every active resource the org uses, with balance at this depot (0 if none)
        balances = conn.execute(
            """
            SELECT r.resource_id,
                   r.name AS resource_name,
                   r.resource_type,
                   r.unit_type,
                   COALESCE(SUM(bp.current_quantity), 0) AS current_quantity
            FROM resources r
            LEFT JOIN balance_projection bp
                ON bp.resource_id = r.resource_id
               AND bp.organisation_id = ?
               AND bp.depot_id = ?
            WHERE r.organisation_id = ?
              AND r.is_active = 1
            GROUP BY r.resource_id, r.name, r.resource_type, r.unit_type
            ORDER BY r.resource_type ASC, r.name ASC
            """,
            (organisation_id, depot_id, organisation_id),
        ).fetchall()

        stocktake_id = make_id("stk")
        snapshot_at = now_iso()

        conn.execute(
            """
            INSERT INTO stocktake_sessions (
                stocktake_id, organisation_id, depot_id, depot_name, status,
                snapshot_taken_at, initiated_by_display_name, notes,
                total_lines, lines_counted, variance_lines, created_at
            ) VALUES (?, ?, ?, ?, 'IN_PROGRESS', ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                stocktake_id, organisation_id, depot_id, depot["name"],
                snapshot_at, initiated_by, notes, len(balances), snapshot_at,
            ),
        )

        for bal in balances:
            conn.execute(
                """
                INSERT INTO stocktake_lines (
                    stocktake_line_id, stocktake_id, organisation_id, depot_id,
                    resource_id, resource_name, resource_type, unit_type, expected_quantity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    make_id("stkl"), stocktake_id, organisation_id, depot_id,
                    bal["resource_id"], bal["resource_name"],
                    bal["resource_type"], bal["unit_type"],
                    bal["current_quantity"],
                ),
            )

        resource_types = sorted({bal["resource_type"] for bal in balances})

        audit_event(
            conn,
            entity_type="StocktakeSession",
            entity_id=stocktake_id,
            action="INITIATE",
            summary=(
                f"Stocktake initiated for depot '{depot['name']}' in org {organisation_id} "
                f"by {initiated_by}. {len(balances)} line(s) across "
                f"{len(resource_types)} resource type(s): {', '.join(resource_types) or 'none'}."
            ),
        )

        conn.commit()
        conn.close()

        return jsonify({
            "stocktake_id": stocktake_id,
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "depot_name": depot["name"],
            "status": "IN_PROGRESS",
            "snapshot_taken_at": snapshot_at,
            "initiated_by_display_name": initiated_by,
            "total_lines": len(balances),
            "resource_types": resource_types,
            "notes": notes,
            "message": (
                f"Stocktake started for depot '{depot['name']}' with {len(balances)} "
                f"line(s) across {len(resource_types)} resource type(s). "
                "Record a physical count for each line, then submit for review."
            ),
        }), 201

    @app.get("/organisations/<organisation_id>/stocktake")
    def list_stocktakes(organisation_id):
        status_filter = request.args.get("status", "").strip().upper() or None
        depot_filter = request.args.get("depot_id", "").strip() or None

        conn = _get_conn()
        ensure_stocktake_tables(conn)

        sql = "SELECT * FROM stocktake_sessions WHERE organisation_id = ?"
        params = [organisation_id]
        if status_filter:
            sql += " AND status = ?"
            params.append(status_filter)
        if depot_filter:
            sql += " AND depot_id = ?"
            params.append(depot_filter)
        sql += " ORDER BY created_at DESC"

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "count": len(rows),
            "filter_status": status_filter,
            "filter_depot_id": depot_filter,
            "items": [dict(r) for r in rows],
        }), 200

    @app.get("/organisations/<organisation_id>/stocktake/<stocktake_id>")
    def get_stocktake(organisation_id, stocktake_id):
        conn = _get_conn()
        ensure_stocktake_tables(conn)

        session = _get_stocktake(conn, stocktake_id, organisation_id)
        if not session:
            conn.close()
            return jsonify({"error": "Stocktake not found"}), 404

        lines = _get_lines(conn, stocktake_id)
        conn.close()

        line_dicts = [dict(l) for l in lines]
        pending_review = [l for l in line_dicts if l.get("review_status") == "PENDING_REVIEW"]

        return jsonify({
            **dict(session),
            "lines": line_dicts,
            "lines_by_resource_type": _group_lines_by_resource_type(lines),
            "lines_pending_review": len(pending_review),
            "review_complete": len(pending_review) == 0 and session["status"] == "PENDING_REVIEW",
        }), 200

    @app.patch("/organisations/<organisation_id>/stocktake/<stocktake_id>/lines/<stocktake_line_id>")
    def record_stocktake_count(organisation_id, stocktake_id, stocktake_line_id):
        body = request.get_json(silent=True) or {}
        counted_quantity = body.get("counted_quantity")
        counted_by = (body.get("counted_by_display_name") or "").strip()
        count_notes = (body.get("count_notes") or "").strip() or None

        if counted_quantity is None:
            return jsonify({"error": "counted_quantity is required"}), 400
        if not isinstance(counted_quantity, int) or counted_quantity < 0:
            return jsonify({"error": "counted_quantity must be a non-negative integer"}), 400
        if not counted_by:
            return jsonify({"error": "counted_by_display_name is required"}), 400

        conn = _get_conn()
        ensure_stocktake_tables(conn)

        session = _get_stocktake(conn, stocktake_id, organisation_id)
        if not session:
            conn.close()
            return jsonify({"error": "Stocktake not found"}), 404

        if session["status"] != "IN_PROGRESS":
            conn.close()
            return jsonify({
                "error": "STOCKTAKE_NOT_EDITABLE",
                "dialog": {
                    "title": "Stocktake is not editable",
                    "message": (
                        f"This stocktake is '{session['status']}' and can no longer be edited. "
                        "Counts can only be recorded while the stocktake is IN_PROGRESS."
                    ),
                    "primary_action": {
                        "label": "View Stocktake",
                        "route": f"/organisations/{organisation_id}/stocktake/{stocktake_id}",
                        "action_type": "NAVIGATE",
                    },
                },
            }), 409

        line = conn.execute(
            "SELECT * FROM stocktake_lines WHERE stocktake_line_id = ? AND stocktake_id = ?",
            (stocktake_line_id, stocktake_id),
        ).fetchone()
        if not line:
            conn.close()
            return jsonify({"error": "Stocktake line not found"}), 404

        variance = counted_quantity - line["expected_quantity"]
        counted_at = now_iso()

        conn.execute(
            """
            UPDATE stocktake_lines
            SET counted_quantity = ?, variance = ?, count_notes = ?,
                counted_by_display_name = ?, counted_at = ?,
                review_status = NULL
            WHERE stocktake_line_id = ?
            """,
            (counted_quantity, variance, count_notes, counted_by, counted_at, stocktake_line_id),
        )

        _update_session_counts(conn, stocktake_id)

        audit_event(
            conn,
            entity_type="StocktakeLine",
            entity_id=stocktake_line_id,
            action="COUNT_RECORDED",
            summary=(
                f"Count recorded for '{line['resource_name']}' ({line['resource_type']}) "
                f"in stocktake {stocktake_id}: "
                f"expected={line['expected_quantity']}, counted={counted_quantity}, "
                f"variance={variance:+d}. By {counted_by}."
            ),
        )

        conn.commit()
        conn.close()

        return jsonify({
            "stocktake_line_id": stocktake_line_id,
            "stocktake_id": stocktake_id,
            "resource_id": line["resource_id"],
            "resource_name": line["resource_name"],
            "resource_type": line["resource_type"],
            "expected_quantity": line["expected_quantity"],
            "counted_quantity": counted_quantity,
            "variance": variance,
            "counted_by_display_name": counted_by,
            "counted_at": counted_at,
            "count_notes": count_notes,
        }), 200

    @app.post("/organisations/<organisation_id>/stocktake/<stocktake_id>/submit")
    def submit_stocktake(organisation_id, stocktake_id):
        body = request.get_json(silent=True) or {}
        submitted_by = (body.get("submitted_by_display_name") or "").strip()

        if not submitted_by:
            return jsonify({"error": "submitted_by_display_name is required"}), 400

        conn = _get_conn()
        ensure_stocktake_tables(conn)

        session = _get_stocktake(conn, stocktake_id, organisation_id)
        if not session:
            conn.close()
            return jsonify({"error": "Stocktake not found"}), 404

        if session["status"] != "IN_PROGRESS":
            conn.close()
            return jsonify({
                "error": "Stocktake is not IN_PROGRESS",
                "current_status": session["status"],
            }), 400

        lines = _get_lines(conn, stocktake_id)
        uncounted = [l for l in lines if l["counted_quantity"] is None]
        if uncounted:
            conn.close()
            return jsonify({
                "error": "UNCOUNTED_LINES",
                "dialog": {
                    "title": "Not all lines have been counted",
                    "message": (
                        f"{len(uncounted)} of {len(lines)} resource line(s) still need a count. "
                        "Record a physical count for every line before submitting for review."
                    ),
                    "primary_action": {
                        "label": "Continue Counting",
                        "route": f"/organisations/{organisation_id}/stocktake/{stocktake_id}",
                        "action_type": "NAVIGATE",
                    },
                },
                "uncounted_line_ids": [l["stocktake_line_id"] for l in uncounted],
                "uncounted_resources": [
                    {"name": l["resource_name"], "type": l["resource_type"]}
                    for l in uncounted
                ],
            }), 400

        submitted_at = now_iso()

        # Auto-accept zero-variance lines; flag variance lines for admin review
        for line in lines:
            if line["variance"] == 0:
                conn.execute(
                    "UPDATE stocktake_lines SET review_status = 'ACCEPTED' WHERE stocktake_line_id = ?",
                    (line["stocktake_line_id"],),
                )
            else:
                conn.execute(
                    "UPDATE stocktake_lines SET review_status = 'PENDING_REVIEW' WHERE stocktake_line_id = ?",
                    (line["stocktake_line_id"],),
                )

        conn.execute(
            """
            UPDATE stocktake_sessions
            SET status = 'PENDING_REVIEW', submitted_by_display_name = ?, submitted_at = ?
            WHERE stocktake_id = ?
            """,
            (submitted_by, submitted_at, stocktake_id),
        )

        variance_lines = [l for l in lines if l["variance"] != 0]
        by_type = defaultdict(int)
        for l in variance_lines:
            by_type[l["resource_type"]] += 1

        audit_event(
            conn,
            entity_type="StocktakeSession",
            entity_id=stocktake_id,
            action="SUBMIT",
            summary=(
                f"Stocktake {stocktake_id} submitted for review by {submitted_by}. "
                f"{len(lines)} line(s), {len(variance_lines)} variance(s) pending admin decision."
            ),
        )

        conn.commit()
        conn.close()

        return jsonify({
            "stocktake_id": stocktake_id,
            "status": "PENDING_REVIEW",
            "submitted_by_display_name": submitted_by,
            "submitted_at": submitted_at,
            "total_lines": len(lines),
            "variance_lines": len(variance_lines),
            "variance_by_resource_type": dict(by_type),
            "message": (
                f"Stocktake submitted. {len(variance_lines)} variance(s) are awaiting your decision. "
                "Accept or reject each counted quantity, or use bulk-accept to approve all at once."
            ),
        }), 200

    @app.post("/organisations/<organisation_id>/stocktake/<stocktake_id>/lines/<stocktake_line_id>/accept")
    def accept_stocktake_line(organisation_id, stocktake_id, stocktake_line_id):
        role_error = _require_org_admin_or_above("accept stocktake variance lines")
        if role_error:
            return role_error

        body = request.get_json(silent=True) or {}
        reviewed_by = (body.get("reviewed_by_display_name") or "").strip()

        if not reviewed_by:
            return jsonify({"error": "reviewed_by_display_name is required"}), 400

        conn = _get_conn()
        ensure_stocktake_tables(conn)

        session = _get_stocktake(conn, stocktake_id, organisation_id)
        if not session:
            conn.close()
            return jsonify({"error": "Stocktake not found"}), 404

        if session["status"] != "PENDING_REVIEW":
            conn.close()
            return jsonify({
                "error": "Line review is only available when stocktake is PENDING_REVIEW",
                "current_status": session["status"],
            }), 400

        line = conn.execute(
            "SELECT * FROM stocktake_lines WHERE stocktake_line_id = ? AND stocktake_id = ?",
            (stocktake_line_id, stocktake_id),
        ).fetchone()
        if not line:
            conn.close()
            return jsonify({"error": "Stocktake line not found"}), 404

        if line["review_status"] != "PENDING_REVIEW":
            conn.close()
            return jsonify({
                "error": "Line is not pending review",
                "current_review_status": line["review_status"],
            }), 400

        reviewed_at = now_iso()
        conn.execute(
            """
            UPDATE stocktake_lines
            SET review_status = 'ACCEPTED', reviewed_by_display_name = ?,
                reviewed_at = ?, rejection_reason = NULL
            WHERE stocktake_line_id = ?
            """,
            (reviewed_by, reviewed_at, stocktake_line_id),
        )

        audit_event(
            conn,
            entity_type="StocktakeLine",
            entity_id=stocktake_line_id,
            action="ACCEPT",
            summary=(
                f"Counted qty {line['counted_quantity']} accepted for "
                f"'{line['resource_name']}' ({line['resource_type']}) "
                f"(variance {line['variance']:+d}) by {reviewed_by}."
            ),
        )

        conn.commit()
        conn.close()

        return jsonify({
            "stocktake_line_id": stocktake_line_id,
            "resource_name": line["resource_name"],
            "resource_type": line["resource_type"],
            "expected_quantity": line["expected_quantity"],
            "counted_quantity": line["counted_quantity"],
            "variance": line["variance"],
            "review_status": "ACCEPTED",
            "reviewed_by_display_name": reviewed_by,
            "reviewed_at": reviewed_at,
            "message": (
                f"Accepted. A {abs(line['variance'])}-unit "
                f"{'increase' if line['variance'] > 0 else 'decrease'} adjustment will be "
                "applied to the balance when this stocktake is posted."
            ),
        }), 200

    @app.post("/organisations/<organisation_id>/stocktake/<stocktake_id>/lines/<stocktake_line_id>/reject")
    def reject_stocktake_line(organisation_id, stocktake_id, stocktake_line_id):
        role_error = _require_org_admin_or_above("reject stocktake variance lines")
        if role_error:
            return role_error

        body = request.get_json(silent=True) or {}
        reviewed_by = (body.get("reviewed_by_display_name") or "").strip()
        rejection_reason = (body.get("rejection_reason") or "").strip() or None

        if not reviewed_by:
            return jsonify({"error": "reviewed_by_display_name is required"}), 400

        conn = _get_conn()
        ensure_stocktake_tables(conn)

        session = _get_stocktake(conn, stocktake_id, organisation_id)
        if not session:
            conn.close()
            return jsonify({"error": "Stocktake not found"}), 404

        if session["status"] != "PENDING_REVIEW":
            conn.close()
            return jsonify({
                "error": "Line review is only available when stocktake is PENDING_REVIEW",
                "current_status": session["status"],
            }), 400

        line = conn.execute(
            "SELECT * FROM stocktake_lines WHERE stocktake_line_id = ? AND stocktake_id = ?",
            (stocktake_line_id, stocktake_id),
        ).fetchone()
        if not line:
            conn.close()
            return jsonify({"error": "Stocktake line not found"}), 404

        if line["review_status"] != "PENDING_REVIEW":
            conn.close()
            return jsonify({
                "error": "Line is not pending review",
                "current_review_status": line["review_status"],
            }), 400

        reviewed_at = now_iso()
        conn.execute(
            """
            UPDATE stocktake_lines
            SET review_status = 'REJECTED', reviewed_by_display_name = ?,
                reviewed_at = ?, rejection_reason = ?
            WHERE stocktake_line_id = ?
            """,
            (reviewed_by, reviewed_at, rejection_reason, stocktake_line_id),
        )

        audit_event(
            conn,
            entity_type="StocktakeLine",
            entity_id=stocktake_line_id,
            action="REJECT",
            summary=(
                f"Counted qty {line['counted_quantity']} rejected for "
                f"'{line['resource_name']}' ({line['resource_type']}) "
                f"(variance {line['variance']:+d}) by {reviewed_by}. "
                f"Reason: {rejection_reason or 'not specified'}."
            ),
        )

        conn.commit()
        conn.close()

        return jsonify({
            "stocktake_line_id": stocktake_line_id,
            "resource_name": line["resource_name"],
            "resource_type": line["resource_type"],
            "expected_quantity": line["expected_quantity"],
            "counted_quantity": line["counted_quantity"],
            "variance": line["variance"],
            "review_status": "REJECTED",
            "reviewed_by_display_name": reviewed_by,
            "reviewed_at": reviewed_at,
            "rejection_reason": rejection_reason,
            "message": "Rejected. The balance for this resource will not be adjusted.",
        }), 200

    @app.post("/organisations/<organisation_id>/stocktake/<stocktake_id>/bulk-accept")
    def bulk_accept_stocktake_lines(organisation_id, stocktake_id):
        role_error = _require_org_admin_or_above("bulk accept stocktake lines")
        if role_error:
            return role_error

        body = request.get_json(silent=True) or {}
        reviewed_by = (body.get("reviewed_by_display_name") or "").strip()

        if not reviewed_by:
            return jsonify({"error": "reviewed_by_display_name is required"}), 400

        conn = _get_conn()
        ensure_stocktake_tables(conn)

        session = _get_stocktake(conn, stocktake_id, organisation_id)
        if not session:
            conn.close()
            return jsonify({"error": "Stocktake not found"}), 404

        if session["status"] != "PENDING_REVIEW":
            conn.close()
            return jsonify({
                "error": "Bulk accept is only available when stocktake is PENDING_REVIEW",
                "current_status": session["status"],
            }), 400

        pending_lines = conn.execute(
            """
            SELECT * FROM stocktake_lines
            WHERE stocktake_id = ? AND review_status = 'PENDING_REVIEW'
            """,
            (stocktake_id,),
        ).fetchall()

        if not pending_lines:
            conn.close()
            return jsonify({
                "error": "No lines are pending review",
                "message": "All variance lines have already been reviewed.",
            }), 400

        reviewed_at = now_iso()
        conn.execute(
            """
            UPDATE stocktake_lines
            SET review_status = 'ACCEPTED', reviewed_by_display_name = ?, reviewed_at = ?
            WHERE stocktake_id = ? AND review_status = 'PENDING_REVIEW'
            """,
            (reviewed_by, reviewed_at, stocktake_id),
        )

        by_type = defaultdict(int)
        for l in pending_lines:
            by_type[l["resource_type"]] += 1

        audit_event(
            conn,
            entity_type="StocktakeSession",
            entity_id=stocktake_id,
            action="BULK_ACCEPT",
            summary=(
                f"Bulk-accepted {len(pending_lines)} variance line(s) in stocktake {stocktake_id} "
                f"by {reviewed_by}."
            ),
        )

        conn.commit()
        conn.close()

        return jsonify({
            "stocktake_id": stocktake_id,
            "accepted_count": len(pending_lines),
            "accepted_by_resource_type": dict(by_type),
            "reviewed_by_display_name": reviewed_by,
            "reviewed_at": reviewed_at,
            "message": (
                f"{len(pending_lines)} variance line(s) accepted. "
                "You can now post the stocktake to apply all adjustments."
            ),
        }), 200

    @app.get("/organisations/<organisation_id>/stocktake/<stocktake_id>/variance-report")
    def get_variance_report(organisation_id, stocktake_id):
        conn = _get_conn()
        ensure_stocktake_tables(conn)

        session = _get_stocktake(conn, stocktake_id, organisation_id)
        if not session:
            conn.close()
            return jsonify({"error": "Stocktake not found"}), 404

        lines = _get_lines(conn, stocktake_id)
        conn.close()

        all_lines = [dict(l) for l in lines]
        variance_lines = [l for l in all_lines if l.get("variance") is not None and l["variance"] != 0]
        zero_variance = sum(1 for l in all_lines if l.get("variance") == 0)
        uncounted = sum(1 for l in all_lines if l["counted_quantity"] is None)

        pending_review = [l for l in variance_lines if l.get("review_status") == "PENDING_REVIEW"]
        accepted = [l for l in variance_lines if l.get("review_status") == "ACCEPTED"]
        rejected = [l for l in variance_lines if l.get("review_status") == "REJECTED"]

        # Group variance lines by resource type
        variance_by_type = defaultdict(list)
        for l in variance_lines:
            variance_by_type[l["resource_type"]].append(l)

        return jsonify({
            "stocktake_id": stocktake_id,
            "organisation_id": organisation_id,
            "depot_id": session["depot_id"],
            "depot_name": session["depot_name"],
            "status": session["status"],
            "report_type": "STOCKTAKE_VARIANCE_REPORT",
            "summary": {
                "total_lines": len(all_lines),
                "counted_lines": len(all_lines) - uncounted,
                "uncounted_lines": uncounted,
                "zero_variance_lines": zero_variance,
                "variance_line_count": len(variance_lines),
                "pending_review_count": len(pending_review),
                "accepted_count": len(accepted),
                "rejected_count": len(rejected),
            },
            "variance_lines": variance_lines,
            "variance_by_resource_type": {k: v for k, v in variance_by_type.items()},
        }), 200

    @app.post("/organisations/<organisation_id>/stocktake/<stocktake_id>/post")
    def post_stocktake(organisation_id, stocktake_id):
        role_error = _require_org_admin_or_above("post stocktakes")
        if role_error:
            return role_error

        body = request.get_json(silent=True) or {}
        posted_by = (body.get("posted_by_display_name") or "").strip()

        if not posted_by:
            return jsonify({"error": "posted_by_display_name is required"}), 400

        conn = _get_conn()
        ensure_stocktake_tables(conn)
        ensure_transaction_numbering_tables(conn)
        ensure_transaction_partner_columns(conn)
        ensure_transaction_user_attribution_columns(conn)

        session = _get_stocktake(conn, stocktake_id, organisation_id)
        if not session:
            conn.close()
            return jsonify({"error": "Stocktake not found"}), 404

        if session["status"] != "PENDING_REVIEW":
            conn.close()
            return jsonify({
                "error": "Stocktake must be PENDING_REVIEW before posting",
                "current_status": session["status"],
            }), 400

        lines = [dict(l) for l in _get_lines(conn, stocktake_id)]

        uncounted = [l for l in lines if l["counted_quantity"] is None]
        if uncounted:
            conn.close()
            return jsonify({
                "error": "UNCOUNTED_LINES",
                "dialog": {
                    "title": "Not all lines have been counted",
                    "message": (
                        f"{len(uncounted)} line(s) have no count recorded. "
                        "All lines must be counted before posting."
                    ),
                    "primary_action": {
                        "label": "View Stocktake",
                        "route": f"/organisations/{organisation_id}/stocktake/{stocktake_id}",
                        "action_type": "NAVIGATE",
                    },
                },
                "uncounted_line_ids": [l["stocktake_line_id"] for l in uncounted],
            }), 400

        still_pending = [l for l in lines if l.get("review_status") == "PENDING_REVIEW"]
        if still_pending:
            conn.close()
            return jsonify({
                "error": "REVIEW_INCOMPLETE",
                "dialog": {
                    "title": "Review not complete",
                    "message": (
                        f"{len(still_pending)} variance line(s) still need your decision. "
                        "Accept or reject each counted quantity, or use bulk-accept to approve all at once."
                    ),
                    "primary_action": {
                        "label": "Review Variances",
                        "route": f"/organisations/{organisation_id}/stocktake/{stocktake_id}/variance-report",
                        "action_type": "NAVIGATE",
                    },
                },
                "pending_review_count": len(still_pending),
                "pending_line_ids": [l["stocktake_line_id"] for l in still_pending],
            }), 400

        posted_at = now_iso()
        adjustment_txns = []

        for line in lines:
            # Only post adjustments for accepted non-zero variances
            if line.get("review_status") != "ACCEPTED" or line["variance"] == 0:
                continue

            direction = "IN" if line["variance"] > 0 else "OUT"
            qty = abs(line["variance"])
            txn_id = make_id("txn")
            ref_number, org_seq = generate_transaction_reference(conn, organisation_id)

            conn.execute(
                """
                INSERT INTO transactions (
                    transaction_id, organisation_id, depot_id,
                    transaction_type, resource_id, quantity, direction, status,
                    submitted_by_display_name, reference_number, org_sequence_number,
                    created_at
                ) VALUES (?, ?, ?, 'STOCKTAKE_ADJUSTMENT', ?, ?, ?, 'PENDING',
                          ?, ?, ?, ?)
                """,
                (
                    txn_id, organisation_id, session["depot_id"],
                    line["resource_id"], qty, direction,
                    posted_by, ref_number, org_seq, posted_at,
                ),
            )

            post_transaction_to_ledger(conn, {
                "transaction_id": txn_id,
                "organisation_id": organisation_id,
                "depot_id": session["depot_id"],
                "resource_id": line["resource_id"],
                "quantity": qty,
                "direction": direction,
            })

            conn.execute(
                "UPDATE stocktake_lines SET adjustment_transaction_id = ? WHERE stocktake_line_id = ?",
                (txn_id, line["stocktake_line_id"]),
            )

            adjustment_txns.append({
                "transaction_id": txn_id,
                "reference_number": ref_number,
                "resource_name": line["resource_name"],
                "resource_type": line["resource_type"],
                "direction": direction,
                "quantity": qty,
                "variance": line["variance"],
            })

        rejected_lines = [l for l in lines if l.get("review_status") == "REJECTED"]

        conn.execute(
            """
            UPDATE stocktake_sessions
            SET status = 'POSTED', posted_by_display_name = ?, posted_at = ?
            WHERE stocktake_id = ?
            """,
            (posted_by, posted_at, stocktake_id),
        )

        _update_session_counts(conn, stocktake_id)

        audit_event(
            conn,
            entity_type="StocktakeSession",
            entity_id=stocktake_id,
            action="POST",
            summary=(
                f"Stocktake {stocktake_id} posted by {posted_by}. "
                f"{len(adjustment_txns)} adjustment(s) applied, "
                f"{len(rejected_lines)} variance(s) rejected (no adjustment)."
            ),
        )

        conn.commit()
        conn.close()

        return jsonify({
            "stocktake_id": stocktake_id,
            "status": "POSTED",
            "posted_by_display_name": posted_by,
            "posted_at": posted_at,
            "total_lines": len(lines),
            "adjustments_posted": len(adjustment_txns),
            "rejected_lines": len(rejected_lines),
            "adjustment_transactions": adjustment_txns,
            "message": (
                f"Stocktake posted. {len(adjustment_txns)} adjustment(s) applied to balance. "
                + (f"{len(rejected_lines)} variance(s) were rejected and not adjusted." if rejected_lines else "")
            ),
        }), 200

    @app.post("/organisations/<organisation_id>/stocktake/<stocktake_id>/cancel")
    def cancel_stocktake(organisation_id, stocktake_id):
        body = request.get_json(silent=True) or {}
        cancelled_by = (body.get("cancelled_by_display_name") or "").strip()

        if not cancelled_by:
            return jsonify({"error": "cancelled_by_display_name is required"}), 400

        conn = _get_conn()
        ensure_stocktake_tables(conn)

        session = _get_stocktake(conn, stocktake_id, organisation_id)
        if not session:
            conn.close()
            return jsonify({"error": "Stocktake not found"}), 404

        if session["status"] == "POSTED":
            conn.close()
            return jsonify({
                "error": "STOCKTAKE_ALREADY_POSTED",
                "dialog": {
                    "title": "Cannot cancel a posted stocktake",
                    "message": (
                        "This stocktake has already been posted and adjustments have been applied. "
                        "To correct the balance, start a new stocktake for this depot."
                    ),
                    "primary_action": {
                        "label": "Start New Stocktake",
                        "route": f"/organisations/{organisation_id}/stocktake",
                        "action_type": "NAVIGATE",
                    },
                },
            }), 400

        if session["status"] == "CANCELLED":
            conn.close()
            return jsonify({"error": "Stocktake is already cancelled"}), 400

        cancelled_at = now_iso()
        conn.execute(
            """
            UPDATE stocktake_sessions
            SET status = 'CANCELLED', cancelled_at = ?, cancelled_by_display_name = ?
            WHERE stocktake_id = ?
            """,
            (cancelled_at, cancelled_by, stocktake_id),
        )

        audit_event(
            conn,
            entity_type="StocktakeSession",
            entity_id=stocktake_id,
            action="CANCEL",
            summary=f"Stocktake {stocktake_id} cancelled by {cancelled_by}.",
        )

        conn.commit()
        conn.close()

        return jsonify({
            "stocktake_id": stocktake_id,
            "status": "CANCELLED",
            "cancelled_at": cancelled_at,
            "cancelled_by_display_name": cancelled_by,
            "message": "Stocktake cancelled. No adjustments have been applied.",
        }), 200

    @app.get("/global-admin/stocktake-summary")
    def global_stocktake_summary():
        limit = min(int(request.args.get("limit", 50)), 200)
        status_filter = request.args.get("status", "").strip().upper() or None

        conn = _get_conn()
        ensure_stocktake_tables(conn)

        sql = "SELECT * FROM stocktake_sessions"
        params = []
        if status_filter:
            sql += " WHERE status = ?"
            params.append(status_filter)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()

        total_row = conn.execute("SELECT COUNT(*) AS cnt FROM stocktake_sessions").fetchone()
        active_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM stocktake_sessions WHERE status = 'IN_PROGRESS'"
        ).fetchone()
        pending_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM stocktake_sessions WHERE status = 'PENDING_REVIEW'"
        ).fetchone()

        conn.close()

        return jsonify({
            "summary_type": "GLOBAL_ADMIN_STOCKTAKE_SUMMARY",
            "total_sessions": total_row["cnt"] if total_row else 0,
            "active_sessions": active_row["cnt"] if active_row else 0,
            "pending_review_sessions": pending_row["cnt"] if pending_row else 0,
            "filter_status": status_filter,
            "count": len(rows),
            "items": [dict(r) for r in rows],
        }), 200
