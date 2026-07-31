"""
modules/depots.py — Depots brick

Covers:
  - Depot CRUD (list, create, get profile, update name,
    deactivate, reactivate)
  - Schema migration helper (_ensure_depot_update_columns)
"""

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso

_ORG_ADMIN_ROLES = {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
_GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}


# ── Schema migration helper ────────────────────────────────────────────────────

def _ensure_depot_update_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(depots)")}
    if "is_active" not in cols:
        conn.execute("ALTER TABLE depots ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE depots ADD COLUMN updated_at TEXT")
    conn.commit()


def _require_org_admin_role(action_label):
    role = g.current_user.get("role")
    if role not in _ORG_ADMIN_ROLES:
        return jsonify({
            "error": "INSUFFICIENT_ROLE",
            "message": f"Only Org Admin or above can {action_label}.",
            "your_role": role,
        }), 403
    return None


def _require_depot_org_access(depot):
    role = g.current_user.get("role")
    if role in _GLOBAL_ADMIN_ROLES:
        return None
    if g.current_user.get("user_org_id") == depot["organisation_id"]:
        return None
    return jsonify({
        "error": "ORG_ACCESS_DENIED",
        "message": "You do not have access to manage this depot.",
    }), 403


# ── Route registration ─────────────────────────────────────────────────────────

def register_depot_routes(app):

    @app.get("/depots")
    def list_depots():
        organisation_id = request.args.get("organisation_id")
        is_active = request.args.get("is_active")

        conn = get_conn()

        sql = "SELECT depot_id, organisation_id, name, opening_balance_used, created_at FROM depots WHERE 1=1"
        params = []

        if organisation_id:
            sql += " AND organisation_id = ?"
            params.append(organisation_id)

        if is_active is not None:
            active_val = 1 if is_active.lower() in ("1", "true", "yes") else 0
            sql += " AND is_active = ?"
            params.append(active_val)

        sql += " ORDER BY name ASC"

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        return jsonify({
            "count": len(rows),
            "depots": [dict(r) for r in rows],
        }), 200


    @app.post("/depots")
    def create_depot():
        role_error = _require_org_admin_role("create depots")
        if role_error:
            return role_error

        body = request.get_json(silent=True) or {}
        # Enforce org isolation: non-global-admins always write to their own org
        if g.current_user.get("role") not in _GLOBAL_ADMIN_ROLES:
            organisation_id = g.current_user.get("user_org_id")
        else:
            organisation_id = body.get("organisation_id")
        name = (body.get("name") or "").strip()[:255]

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400
        if not name:
            return jsonify({"error": "name is required"}), 400

        conn = get_conn()
        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        depot_id = make_id("depot")

        conn.execute(
            "INSERT INTO depots (depot_id, organisation_id, name, opening_balance_used, created_at) VALUES (?, ?, ?, ?, ?)",
            (depot_id, organisation_id, name, 0, now_iso())
        )

        audit_event(
            conn,
            entity_type="Depot",
            entity_id=depot_id,
            action="CREATE",
            summary=f"Created depot: {name}",
            organisation_id=organisation_id
        )

        conn.commit()
        conn.close()

        return jsonify({
            "depot_id": depot_id,
            "organisation_id": organisation_id,
            "name": name,
            "opening_balance_used": False
        }), 201


    @app.get("/depots/<depot_id>")
    def get_depot_profile(depot_id):
        conn = get_conn()

        depot = conn.execute(
            """
            SELECT
                d.depot_id,
                d.organisation_id,
                d.name,
                d.opening_balance_used,
                d.created_at,
                o.name AS organisation_name
            FROM depots d
            LEFT JOIN organisations o ON o.organisation_id = d.organisation_id
            WHERE d.depot_id = ?
            """,
            (depot_id,)
        ).fetchone()

        if not depot:
            conn.close()
            return jsonify({"error": "Depot not found"}), 404

        if g.current_user.get("role") not in {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"} and g.current_user.get("user_org_id") != depot["organisation_id"]:
            conn.close()
            return jsonify({"error": "ORG_ACCESS_DENIED", "message": "You do not have access to this organisation."}), 403

        stock_rows = conn.execute(
            """
            SELECT
                bp.resource_id,
                r.name AS resource_name,
                r.resource_type,
                r.unit_type,
                SUM(bp.current_quantity) AS current_quantity,
                MAX(bp.updated_at) AS updated_at
            FROM balance_projection bp
            LEFT JOIN resources r ON r.resource_id = bp.resource_id
            WHERE bp.depot_id = ?
            GROUP BY bp.resource_id, r.name, r.resource_type, r.unit_type
            ORDER BY r.name
            """,
            (depot_id,)
        ).fetchall()

        pending_rows = conn.execute(
            """
            SELECT
                pending_entry_id,
                entry_type,
                status,
                reason_code,
                reason_text,
                direct_action_label,
                direct_action_target_id,
                resource_id,
                resource_name,
                source_record_id,
                created_at
            FROM pending_approval_entries
            WHERE related_entity_id = ?
              AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
            ORDER BY created_at DESC
            """,
            (depot_id,)
        ).fetchall()

        history_rows = conn.execute(
            """
            SELECT
                pending_entry_id,
                entry_type,
                status,
                reason_code,
                reason_text,
                direct_action_label,
                direct_action_target_id,
                resource_id,
                resource_name,
                source_record_id,
                created_at
            FROM pending_approval_entries
            WHERE related_entity_id = ?
              AND status IN ('RESOLVED', 'REJECTED')
            ORDER BY created_at DESC
            """,
            (depot_id,)
        ).fetchall()

        conn.close()

        opening_balance_used = bool(depot["opening_balance_used"])

        return jsonify({
            "depot_id": depot["depot_id"],
            "organisation_id": depot["organisation_id"],
            "organisation_name": depot["organisation_name"],
            "name": depot["name"],
            "created_at": depot["created_at"],
            "opening_balance_used": opening_balance_used,
            "opening_balance_available": not opening_balance_used,
            "opening_balance_action_label": "Enter Opening Balance" if not opening_balance_used else None,
            "stock_count": len(stock_rows),
            "stock_items": [dict(r) for r in stock_rows],
            "pending_count": len(pending_rows),
            "pending_items": [dict(r) for r in pending_rows],
            "history_count": len(history_rows),
            "history_items": [dict(r) for r in history_rows]
        }), 200


    @app.patch("/depots/<depot_id>")
    def update_depot(depot_id):
        body = request.get_json(silent=True) or {}
        new_name = (body.get("name") or "").strip() or None

        if not new_name:
            return jsonify({"error": "name is required"}), 400

        conn = get_conn()
        _ensure_depot_update_columns(conn)

        depot = conn.execute(
            "SELECT * FROM depots WHERE depot_id = ?", (depot_id,)
        ).fetchone()

        if not depot:
            conn.close()
            return jsonify({"error": "Depot not found"}), 404

        old_name = depot["name"]
        ts = now_iso()

        conn.execute(
            "UPDATE depots SET name = ?, updated_at = ? WHERE depot_id = ?",
            (new_name, ts, depot_id),
        )

        audit_event(
            conn,
            entity_type="Depot",
            entity_id=depot_id,
            action="UPDATE",
            summary=f"Depot name updated from '{old_name}' to '{new_name}'.",
            organisation_id=depot["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({
            "depot_id": depot_id,
            "organisation_id": depot["organisation_id"],
            "name": new_name,
            "updated_at": ts,
        }), 200


    @app.post("/depots/<depot_id>/deactivate")
    def deactivate_depot(depot_id):
        role_error = _require_org_admin_role("deactivate depots")
        if role_error:
            return role_error

        conn = get_conn()
        _ensure_depot_update_columns(conn)

        depot = conn.execute(
            "SELECT * FROM depots WHERE depot_id = ?", (depot_id,)
        ).fetchone()

        if not depot:
            conn.close()
            return jsonify({"error": "Depot not found"}), 404

        access_error = _require_depot_org_access(depot)
        if access_error:
            conn.close()
            return access_error

        depot_cols = {row["name"] for row in conn.execute("PRAGMA table_info(depots)")}
        if "is_active" in depot_cols and depot["is_active"] == 0:
            conn.close()
            return jsonify({"error": "Depot is already inactive"}), 409

        ts = now_iso()
        conn.execute(
            "UPDATE depots SET is_active = 0, updated_at = ? WHERE depot_id = ?",
            (ts, depot_id),
        )

        audit_event(
            conn,
            entity_type="Depot",
            entity_id=depot_id,
            action="DEACTIVATE",
            summary=f"Depot '{depot['name']}' deactivated.",
            organisation_id=depot["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({
            "depot_id": depot_id,
            "name": depot["name"],
            "is_active": False,
        }), 200


    @app.post("/depots/<depot_id>/reactivate")
    def reactivate_depot(depot_id):
        role_error = _require_org_admin_role("reactivate depots")
        if role_error:
            return role_error

        conn = get_conn()
        _ensure_depot_update_columns(conn)

        depot = conn.execute(
            "SELECT * FROM depots WHERE depot_id = ?", (depot_id,)
        ).fetchone()

        if not depot:
            conn.close()
            return jsonify({"error": "Depot not found"}), 404

        access_error = _require_depot_org_access(depot)
        if access_error:
            conn.close()
            return access_error

        ts = now_iso()
        conn.execute(
            "UPDATE depots SET is_active = 1, updated_at = ? WHERE depot_id = ?",
            (ts, depot_id),
        )

        audit_event(
            conn,
            entity_type="Depot",
            entity_id=depot_id,
            action="REACTIVATE",
            summary=f"Depot '{depot['name']}' reactivated.",
            organisation_id=depot["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({
            "depot_id": depot_id,
            "name": depot["name"],
            "is_active": True,
        }), 200
