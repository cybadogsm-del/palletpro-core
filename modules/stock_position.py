from collections import defaultdict

from flask import jsonify, request

from db import get_conn as _get_conn, now_iso


def register_stock_position_routes(app):

    @app.get("/organisations/<organisation_id>/stock-position")
    def org_stock_position(organisation_id):
        """
        Org-wide stock position. Only resources with a positive balance are shown
        (resources at zero are omitted — use the stocktake module for a full count).
        Grouped by resource type, then individual resource, with a per-depot split.
        """
        conn = _get_conn()

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        # All depots for the org
        depots = conn.execute(
            "SELECT depot_id, name FROM depots WHERE organisation_id = ? ORDER BY name ASC",
            (organisation_id,)
        ).fetchall()
        depot_map = {d["depot_id"]: d["name"] for d in depots}

        # Only positive balances for this org
        rows = conn.execute(
            """
            SELECT
                bp.resource_id,
                bp.depot_id,
                bp.current_quantity,
                r.name          AS resource_name,
                r.resource_type,
                r.unit_type
            FROM balance_projection bp
            JOIN resources r ON r.resource_id = bp.resource_id
            WHERE bp.organisation_id = ?
              AND bp.current_quantity > 0
              AND r.is_active = 1
            ORDER BY r.resource_type ASC, r.name ASC, bp.depot_id ASC
            """,
            (organisation_id,)
        ).fetchall()

        conn.close()

        # Group by resource, accumulate depot breakdown
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

        items = sorted(
            resource_index.values(),
            key=lambda r: (r["resource_type"], r["resource_name"])
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
            },
            "by_resource_type": dict(by_type),
            "items": items,
        }), 200

    @app.get("/organisations/<organisation_id>/depots/<depot_id>/stock-position")
    def depot_stock_position(organisation_id, depot_id):
        """
        Stock position for one depot. Only resources with a positive balance
        at this depot are shown. Grouped by resource type.
        """
        conn = _get_conn()

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

        rows = conn.execute(
            """
            SELECT
                r.resource_id,
                r.name          AS resource_name,
                r.resource_type,
                r.unit_type,
                bp.current_quantity AS quantity,
                bp.updated_at
            FROM balance_projection bp
            JOIN resources r ON r.resource_id = bp.resource_id
            WHERE bp.organisation_id = ?
              AND bp.depot_id        = ?
              AND bp.current_quantity > 0
              AND r.is_active = 1
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
            "summary": {
                "total_resources_with_stock": len(items),
            },
            "by_resource_type": dict(by_type),
            "items": items,
        }), 200
