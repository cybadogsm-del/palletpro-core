from collections import defaultdict

from flask import jsonify

from db import get_conn as _get_conn, now_iso

_NO_OPERATIONAL_UNIT = "__NO_OPERATIONAL_UNIT__"


def _unit_breakdown_rows(conn, organisation_id, depot_id=None, resource_id=None):
    where = [
        "bp.organisation_id = ?",
        "bp.operational_unit_id IS NOT NULL",
        "bp.operational_unit_id <> ?",
        "bp.current_quantity <> 0",
    ]
    params = [organisation_id, _NO_OPERATIONAL_UNIT]
    if depot_id:
        where.append("bp.depot_id = ?")
        params.append(depot_id)
    if resource_id:
        where.append("bp.resource_id = ?")
        params.append(resource_id)

    return conn.execute(
        f"""
        SELECT bp.depot_id,
               bp.resource_id,
               bp.operational_unit_id,
               bp.current_quantity AS quantity,
               bp.updated_at,
               ou.unit_kind AS operational_unit_kind_snapshot,
               ou.unit_number AS operational_unit_number_snapshot,
               ou.display_name AS operational_unit_display_snapshot
        FROM balance_projection bp
        LEFT JOIN operational_units ou
          ON ou.operational_unit_id = bp.operational_unit_id
        WHERE {' AND '.join(where)}
        ORDER BY ou.unit_kind ASC, ou.display_name ASC, ou.unit_number ASC, bp.operational_unit_id ASC
        """,
        params,
    ).fetchall()


def _build_unit_breakdown_map(rows):
    breakdown = defaultdict(list)
    for row in rows:
        key = (row["depot_id"], row["resource_id"])
        breakdown[key].append({
            "operational_unit_id": row["operational_unit_id"],
            "operational_unit_kind_snapshot": row["operational_unit_kind_snapshot"],
            "operational_unit_number_snapshot": row["operational_unit_number_snapshot"],
            "operational_unit_display_snapshot": row["operational_unit_display_snapshot"] or row["operational_unit_id"],
            "quantity": row["quantity"],
            "updated_at": row["updated_at"],
        })
    return breakdown


def _resource_depot_totals(rows):
    totals = defaultdict(int)
    for row in rows:
        totals[(row["depot_id"], row["resource_id"])] += row["quantity"]
    return totals


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
            SELECT bp.resource_id, bp.depot_id, bp.operational_unit_id, bp.current_quantity,
                   r.name AS resource_name, r.resource_type, r.unit_type
            FROM balance_projection bp
            JOIN resources r ON r.resource_id = bp.resource_id
            WHERE bp.organisation_id = ? AND r.is_active = 1
            ORDER BY r.resource_type ASC, r.name ASC, bp.depot_id ASC
            """,
            (organisation_id,)
        ).fetchall()
        unit_rows = _unit_breakdown_rows(conn, organisation_id)
        unit_breakdown = _build_unit_breakdown_map(unit_rows)
        conn.close()

        resource_index = {}
        depot_index = {}
        for row in rows:
            key = (row["depot_id"], row["resource_id"])
            quantity = row["current_quantity"]

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
            depot_key = (rid, row["depot_id"])
            if depot_key not in depot_index:
                depot_index[depot_key] = {
                    "depot_id": row["depot_id"],
                    "depot_name": depot_map.get(row["depot_id"], row["depot_id"]),
                    "quantity": 0,
                    "operational_units": unit_breakdown.get(key, []),
                }
                resource_index[rid]["depots"].append(depot_index[depot_key])
            depot_index[depot_key]["quantity"] += quantity
            resource_index[rid]["total_quantity"] += quantity

        items = sorted(
            (item for item in resource_index.values() if item["total_quantity"] > 0),
            key=lambda r: (r["resource_type"], r["resource_name"]),
        )
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
                "total_quantity": sum(item["total_quantity"] for item in items),
                "total_operational_unit_lines": sum(
                    len(depot.get("operational_units", []))
                    for item in items
                    for depot in item["depots"]
                ),
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
                   bp.operational_unit_id, bp.current_quantity AS quantity, bp.updated_at
            FROM balance_projection bp
            JOIN resources r ON r.resource_id = bp.resource_id
            WHERE bp.organisation_id = ? AND bp.depot_id = ?
              AND r.is_active = 1
            ORDER BY r.resource_type ASC, r.name ASC
            """,
            (organisation_id, depot_id)
        ).fetchall()
        unit_rows = _unit_breakdown_rows(conn, organisation_id, depot_id=depot_id)
        unit_breakdown = _build_unit_breakdown_map(unit_rows)
        conn.close()

        item_index = {}
        for row in rows:
            quantity = row["quantity"]
            rid = row["resource_id"]
            if rid not in item_index:
                item_index[rid] = {
                    "resource_id": row["resource_id"],
                    "resource_name": row["resource_name"],
                    "resource_type": row["resource_type"],
                    "unit_type": row["unit_type"],
                    "current_quantity": 0,
                    "quantity": 0,
                    "updated_at": row["updated_at"],
                    "operational_units": unit_breakdown.get((depot_id, row["resource_id"]), []),
                }
            item_index[rid]["current_quantity"] += quantity
            item_index[rid]["quantity"] += quantity
            if row["updated_at"] and row["updated_at"] > (item_index[rid]["updated_at"] or ""):
                item_index[rid]["updated_at"] = row["updated_at"]

        items = sorted(
            (item for item in item_index.values() if item["current_quantity"] > 0),
            key=lambda r: (r["resource_type"], r["resource_name"]),
        )
        by_type = defaultdict(list)
        for item in items:
            by_type[item["resource_type"]].append(item)

        return jsonify({
            "report_type": "DEPOT_STOCK_POSITION",
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "depot_name": depot["name"],
            "generated_at": now_iso(),
            "summary": {
                "total_resources_with_stock": len(items),
                "total_quantity": sum(item["current_quantity"] for item in items),
                "total_operational_unit_lines": sum(len(item.get("operational_units", [])) for item in items),
            },
            "by_resource_type": dict(by_type),
            "items": items,
        }), 200

    @app.get("/organisations/<organisation_id>/depots/<depot_id>/resources/<resource_id>/ledger")
    def depot_resource_ledger(organisation_id, depot_id, resource_id):
        conn = _get_conn()
        from modules.transactions import (
            ensure_ledger_balance_operational_unit_columns,
            ensure_transaction_numbering_tables,
            ensure_transaction_operational_unit_columns,
            ensure_transaction_reference_columns,
            ensure_transaction_user_attribution_columns,
        )
        ensure_transaction_user_attribution_columns(conn)
        ensure_transaction_numbering_tables(conn)
        ensure_transaction_reference_columns(conn)
        ensure_transaction_operational_unit_columns(conn)
        ensure_ledger_balance_operational_unit_columns(conn)
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
                   le.operational_unit_id,
                   le.operational_unit_kind_snapshot,
                   le.operational_unit_number_snapshot,
                   le.operational_unit_display_snapshot,
                   t.transaction_type, t.direction, t.quantity,
                   t.reference_number, t.submitted_by_user_id,
                   t.submitted_by_display_name, t.posted_at
            FROM ledger_entries le
            JOIN transactions t ON t.transaction_id = le.transaction_id
            WHERE le.organisation_id = ? AND le.depot_id = ? AND le.resource_id = ?
            ORDER BY le.created_at ASC, le.ledger_entry_id ASC
            """,
            (organisation_id, depot_id, resource_id)
        ).fetchall()

        balances = conn.execute(
            """
            SELECT operational_unit_id, current_quantity
            FROM balance_projection
            WHERE organisation_id = ? AND depot_id = ? AND resource_id = ?
            """,
            (organisation_id, depot_id, resource_id)
        ).fetchall()
        current_balance = sum(row["current_quantity"] for row in balances)
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
                "operational_unit_id": row["operational_unit_id"],
                "operational_unit_kind_snapshot": row["operational_unit_kind_snapshot"],
                "operational_unit_number_snapshot": row["operational_unit_number_snapshot"],
                "operational_unit_display_snapshot": row["operational_unit_display_snapshot"],
                "submitted_by_user_id": row["submitted_by_user_id"],
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
            "current_balance": current_balance,
            "generated_at": now_iso(),
            "entry_count": len(entries),
            "entries": entries,
        }), 200

    @app.get("/organisations/<organisation_id>/resources/<resource_id>/ledger")
    def org_resource_ledger(organisation_id, resource_id):
        conn = _get_conn()
        from modules.transactions import ensure_transaction_numbering_tables, ensure_transaction_reference_columns, ensure_transaction_user_attribution_columns
        ensure_transaction_user_attribution_columns(conn)
        ensure_transaction_numbering_tables(conn)
        ensure_transaction_reference_columns(conn)
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
            """SELECT bp.depot_id, d.name AS depot_name, SUM(bp.current_quantity) AS current_quantity
               FROM balance_projection bp
               JOIN depots d ON d.depot_id = bp.depot_id AND d.organisation_id = bp.organisation_id
               WHERE bp.organisation_id = ? AND bp.resource_id = ?
               GROUP BY bp.depot_id, d.name""",
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

        for balance in balances:
            sec = depot_sections[balance["depot_id"]]
            if not sec["depot_name"]:
                sec["depot_name"] = balance["depot_name"]

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
