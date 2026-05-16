from collections import defaultdict

from flask import jsonify

from db import get_conn as _get_conn, now_iso


def register_stock_position_routes(app):

    @app.get("/organisations/<organisation_id>/stock-position")
    def org_stock_position(organisation_id):
        conn = _get_conn()
        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?", (organisation_id,)
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        depots = conn.execute(
            "SELECT depot_id, name FROM depots WHERE organisation_id = ? ORDER BY name ASC",
            (organisation_id,)
        ).fetchall()
        depot_map = {d["depot_id"]: d["name"] for d in depots}

        rows = conn.execute(
            """
            SELECT bp.resource_id, bp.depot_id, bp.current_quantity,
                   r.name AS resource_name, r.resource_type, r.unit_type
            FROM balance_projection bp
            JOIN resources r ON r.resource_id = bp.resource_id
            WHERE bp.organisation_id = ? AND bp.current_quantity > 0 AND r.is_active = 1
            ORDER BY r.resource_type ASC, r.name ASC, bp.depot_id ASC
            """,
            (organisation_id,)
        ).fetchall()
        conn.close()

        resource_index = {}
        for row in rows:
            rid = row["resource_id"]
            if rid not in resource_index:
                resource_index[rid] = {
                    "resource_id": rid,
                    "resource_name": row["resource_name"],
                    "resource_type": row["resource_type"],
                    "unit_type": row["unit_type"],
                    "total_quantity": 0,
                    "depots": [],
                }
            resource_index[rid]["total_quantity"] += row["current_quantity"]
            resource_index[rid]["depots"].append({
                "depot_id": row["depot_id"],
                "depot_name": depot_map.get(row["depot_id"], row["depot_id"]),
                "quantity": row["current_quantity"],
            })

        items = sorted(resource_index.values(), key=lambda r: (r["resource_type"], r["resource_name"]))
        by_type = defaultdict(list)
        for item in items:
            by_type[item["resource_type"]].append(item)

        return jsonify({
            "report_type": "ORG_STOCK_POSITION",
            "organisation_id": organisation_id,
            "organisation_name": org["name"],
            "generated_at": now_iso(),
            "summary": {
                "total_resources_with_stock": len(items),
                "total_depots": len(depots),
            },
            "by_resource_type": dict(by_type),
            "items": items,
        }), 200

    @app.get("/organisations/<organisation_id>/depots/<depot_id>/stock-position")
    def depot_stock_position(organisation_id, depot_id):
        conn = _get_conn()
        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?", (organisation_id,)
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

        rows = conn.execute(
            """
            SELECT r.resource_id, r.name AS resource_name, r.resource_type, r.unit_type,
                   bp.current_quantity AS quantity, bp.updated_at
            FROM balance_projection bp
            JOIN resources r ON r.resource_id = bp.resource_id
            WHERE bp.organisation_id = ? AND bp.depot_id = ?
              AND bp.current_quantity > 0 AND r.is_active = 1
            ORDER BY r.resource_type ASC, r.name ASC
            """,
            (organisation_id, depot_id)
        ).fetchall()
        conn.close()

        items = [dict(r) for r in rows]
        by_type = defaultdict(list)
        for item in items:
            by_type[item["resource_type"]].append(item)

        return jsonify({
            "report_type": "DEPOT_STOCK_POSITION",
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "depot_name": depot["name"],
            "generated_at": now_iso(),
            "summary": {"total_resources_with_stock": len(items)},
            "by_resource_type": dict(by_type),
            "items": items,
        }), 200

    @app.get("/organisations/<organisation_id>/depots/<depot_id>/resources/<resource_id>/ledger")
    def depot_resource_ledger(organisation_id, depot_id, resource_id):
        conn = _get_conn()
        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?", (organisation_id,)
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

        resource = conn.execute(
            "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ?",
            (resource_id, organisation_id)
        ).fetchone()
        if not resource:
            conn.close()
            return jsonify({"error": "Resource not found"}), 404

        rows = conn.execute(
            """
            SELECT le.ledger_entry_id, le.transaction_id, le.quantity_delta,
                   le.created_at AS ledger_at,
                   t.transaction_type, t.direction, t.quantity,
                   t.reference_number, t.submitted_by_display_name, t.posted_at
            FROM ledger_entries le
            JOIN transactions t ON t.transaction_id = le.transaction_id
            WHERE le.organisation_id = ? AND le.depot_id = ? AND le.resource_id = ?
            ORDER BY le.created_at ASC, le.ledger_entry_id ASC
            """,
            (organisation_id, depot_id, resource_id)
        ).fetchall()

        bal = conn.execute(
            "SELECT current_quantity FROM balance_projection WHERE organisation_id = ? AND depot_id = ? AND resource_id = ?",
            (organisation_id, depot_id, resource_id)
        ).fetchone()
        conn.close()

        running = 0
        entries = []
        for row in rows:
            running += row["quantity_delta"]
            entries.append({
                "ledger_entry_id": row["ledger_entry_id"],
                "transaction_id": row["transaction_id"],
                "reference_number": row["reference_number"],
                "transaction_type": row["transaction_type"],
                "direction": row["direction"],
                "quantity": row["quantity"],
                "quantity_delta": row["quantity_delta"],
                "running_balance": running,
                "submitted_by_display_name": row["submitted_by_display_name"],
                "posted_at": row["posted_at"],
                "ledger_at": row["ledger_at"],
            })

        return jsonify({
            "report_type": "RESOURCE_DEPOT_LEDGER",
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "depot_name": depot["name"],
            "resource_id": resource_id,
            "resource_name": resource["name"],
            "resource_type": resource["resource_type"],
            "unit_type": resource["unit_type"],
            "current_balance": bal["current_quantity"] if bal else 0,
            "generated_at": now_iso(),
            "entry_count": len(entries),
            "entries": entries,
        }), 200

    @app.get("/organisations/<organisation_id>/resources/<resource_id>/ledger")
    def org_resource_ledger(organisation_id, resource_id):
        conn = _get_conn()
        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?", (organisation_id,)
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        resource = conn.execute(
            "SELECT * FROM resources WHERE resource_id = ? AND organisation_id = ?",
            (resource_id, organisation_id)
        ).fetchone()
        if not resource:
            conn.close()
            return jsonify({"error": "Resource not found"}), 404

        rows = conn.execute(
            """
            SELECT le.ledger_entry_id, le.transaction_id, le.depot_id, le.quantity_delta,
                   le.created_at AS ledger_at,
                   t.transaction_type, t.direction, t.quantity,
                   t.reference_number, t.submitted_by_display_name, t.posted_at,
                   d.name AS depot_name
            FROM ledger_entries le
            JOIN transactions t ON t.transaction_id = le.transaction_id
            JOIN depots d       ON d.depot_id        = le.depot_id
            WHERE le.organisation_id = ? AND le.resource_id = ?
            ORDER BY d.name ASC, le.created_at ASC, le.ledger_entry_id ASC
            """,
            (organisation_id, resource_id)
        ).fetchall()

        balances = conn.execute(
            "SELECT depot_id, current_quantity FROM balance_projection WHERE organisation_id = ? AND resource_id = ?",
            (organisation_id, resource_id)
        ).fetchall()
        balance_map = {b["depot_id"]: b["current_quantity"] for b in balances}
        conn.close()

        depot_sections = defaultdict(lambda: {"entries": [], "running": 0, "depot_name": ""})
        for row in rows:
            sec = depot_sections[row["depot_id"]]
            sec["depot_name"] = row["depot_name"]
            sec["running"] += row["quantity_delta"]
            sec["entries"].append({
                "ledger_entry_id": row["ledger_entry_id"],
                "transaction_id": row["transaction_id"],
                "reference_number": row["reference_number"],
                "transaction_type": row["transaction_type"],
                "direction": row["direction"],
                "quantity": row["quantity"],
                "quantity_delta": row["quantity_delta"],
                "running_balance": sec["running"],
                "submitted_by_display_name": row["submitted_by_display_name"],
                "posted_at": row["posted_at"],
                "ledger_at": row["ledger_at"],
            })

        by_depot = [
            {
                "depot_id": did,
                "depot_name": sec["depot_name"],
                "current_balance": balance_map.get(did, 0),
                "entry_count": len(sec["entries"]),
                "entries": sec["entries"],
            }
            for did, sec in sorted(depot_sections.items(), key=lambda x: x[1]["depot_name"])
        ]

        return jsonify({
            "report_type": "RESOURCE_ORG_LEDGER",
            "organisation_id": organisation_id,
            "resource_id": resource_id,
            "resource_name": resource["name"],
            "resource_type": resource["resource_type"],
            "unit_type": resource["unit_type"],
            "total_balance_across_depots": sum(balance_map.values()),
            "generated_at": now_iso(),
            "total_entry_count": sum(d["entry_count"] for d in by_depot),
            "by_depot": by_depot,
        }), 200
