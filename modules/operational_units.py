"""
modules/operational_units.py — Fleet/Unit operational context foundation.

This module owns the backend foundation for Pallet Pro's Pallet Management
System Fleet/Unit pivot. User-facing language is Fleet/Unit; backend language
uses operational_unit so the model can cover trucks, forklifts, trailers, yard
units, warehouse units, and future operational contexts.
"""

from sqlite3 import IntegrityError

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso


VALID_UNIT_KINDS = {"FLEET", "UNIT"}
VALID_STATUSES = {"ACTIVE", "INACTIVE", "MAINTENANCE", "RETIRED"}
VALID_PERMISSION_LEVELS = {"OPERATE", "VIEW", "ADMIN"}
ACTIVE_SESSION_STATUSES = {"ACTIVE"}
ORG_ADMIN_ROLES = {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}


def ensure_operational_unit_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS operational_units (
        operational_unit_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        depot_id TEXT NOT NULL,
        unit_kind TEXT NOT NULL CHECK (unit_kind IN ('FLEET', 'UNIT')),
        unit_type TEXT NOT NULL DEFAULT 'OTHER',
        unit_number TEXT NOT NULL,
        display_name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'INACTIVE', 'MAINTENANCE', 'RETIRED')),
        created_by_user_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (organisation_id, unit_kind, unit_number)
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS operational_unit_categories (
        category_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        depot_id TEXT,
        name TEXT NOT NULL,
        description TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_by_user_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (organisation_id, name)
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS operational_unit_category_members (
        category_id TEXT NOT NULL,
        operational_unit_id TEXT NOT NULL,
        organisation_id TEXT NOT NULL,
        created_by_user_id TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (category_id, operational_unit_id)
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_operational_unit_permissions (
        permission_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        operational_unit_id TEXT,
        category_id TEXT,
        depot_id TEXT,
        permission_level TEXT NOT NULL DEFAULT 'OPERATE' CHECK (permission_level IN ('OPERATE', 'VIEW', 'ADMIN')),
        is_active INTEGER NOT NULL DEFAULT 1,
        created_by_user_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (operational_unit_id IS NOT NULL OR category_id IS NOT NULL OR depot_id IS NOT NULL)
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS operational_unit_sessions (
        unit_session_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        operational_unit_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'RELEASED', 'EXPIRED', 'FORCE_RELEASED')),
        started_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        ended_at TEXT,
        release_reason TEXT,
        force_released_by_user_id TEXT
    )
    """)

    conn.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_operational_unit_one_active_session
    ON operational_unit_sessions (organisation_id, operational_unit_id)
    WHERE status = 'ACTIVE'
    """)


def _json_error(message, status_code=400):
    return jsonify({"error": message}), status_code


def _get_json():
    payload = dict(request.get_json(silent=True) or {})
    payload["created_by_user_id"] = g.current_user.get("user_id")
    return payload


def _require_org_admin():
    if g.current_user.get("role") not in ORG_ADMIN_ROLES:
        return _json_error("Only Org Admin or above can manage Fleet/Units", 403)
    return None


def _user_belongs_to_org(conn, organisation_id, user_id):
    return conn.execute(
        "SELECT 1 FROM user_accounts WHERE user_id = ? AND organisation_id = ? AND access_status = 'ACTIVE'",
        (user_id, organisation_id),
    ).fetchone() is not None


def user_can_operate_unit(conn, organisation_id, user_id, operational_unit_id):
    unit = _unit_belongs_to_org(conn, organisation_id, operational_unit_id)
    if not unit or unit["status"] != "ACTIVE":
        return False
    return conn.execute(
        """
        SELECT 1
        FROM user_operational_unit_permissions p
        WHERE p.organisation_id = ?
          AND p.user_id = ?
          AND p.is_active = 1
          AND p.permission_level IN ('OPERATE', 'ADMIN')
          AND (
              p.operational_unit_id = ?
              OR p.depot_id = ?
              OR p.category_id IN (
                  SELECT category_id FROM operational_unit_category_members
                  WHERE organisation_id = ? AND operational_unit_id = ?
              )
          )
        LIMIT 1
        """,
        (organisation_id, user_id, operational_unit_id, unit["depot_id"], organisation_id, operational_unit_id),
    ).fetchone() is not None


def _normalise_unit_kind(value):
    value = (value or "").strip().upper()
    return value


def _normalise_status(value):
    value = (value or "ACTIVE").strip().upper()
    return value


def _depot_belongs_to_org(conn, organisation_id, depot_id):
    if not depot_id:
        return False
    row = conn.execute(
        "SELECT depot_id FROM depots WHERE depot_id = ? AND organisation_id = ?",
        (depot_id, organisation_id),
    ).fetchone()
    return row is not None


def _unit_belongs_to_org(conn, organisation_id, operational_unit_id):
    return conn.execute(
        "SELECT * FROM operational_units WHERE operational_unit_id = ? AND organisation_id = ?",
        (operational_unit_id, organisation_id),
    ).fetchone()


def _category_belongs_to_org(conn, organisation_id, category_id):
    return conn.execute(
        "SELECT * FROM operational_unit_categories WHERE category_id = ? AND organisation_id = ?",
        (category_id, organisation_id),
    ).fetchone()


def _permission_payload_is_valid(payload):
    return any(payload.get(key) for key in ("operational_unit_id", "category_id", "depot_id"))


def _row_to_dict(row):
    return dict(row) if row else None


def _create_operational_unit(conn, organisation_id, payload):
    depot_id = payload.get("depot_id")
    unit_kind = _normalise_unit_kind(payload.get("unit_kind"))
    unit_type = (payload.get("unit_type") or "OTHER").strip().upper()
    unit_number = (payload.get("unit_number") or "").strip()
    display_name = (payload.get("display_name") or unit_number).strip()
    created_by_user_id = payload.get("created_by_user_id")

    if not _depot_belongs_to_org(conn, organisation_id, depot_id):
        return None, _json_error("Depot/location does not belong to this organisation", 400)
    if unit_kind not in VALID_UNIT_KINDS:
        return None, _json_error("unit_kind must be FLEET or UNIT", 400)
    if not unit_number:
        return None, _json_error("unit_number is required", 400)
    if not display_name:
        return None, _json_error("display_name is required", 400)

    operational_unit_id = make_id("opu")
    now = now_iso()
    try:
        conn.execute(
            """
            INSERT INTO operational_units (
                operational_unit_id,
                organisation_id,
                depot_id,
                unit_kind,
                unit_type,
                unit_number,
                display_name,
                status,
                created_by_user_id,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
            """,
            (
                operational_unit_id,
                organisation_id,
                depot_id,
                unit_kind,
                unit_type,
                unit_number,
                display_name,
                created_by_user_id,
                now,
                now,
            ),
        )
    except IntegrityError:
        return None, _json_error("Fleet/Unit already exists for this organisation and kind", 409)

    audit_event(
        conn,
        entity_type="OperationalUnit",
        entity_id=operational_unit_id,
        action="CREATE",
        summary=f"Created {unit_kind} {unit_number} at location {depot_id}",
        organisation_id=organisation_id,
    )
    return operational_unit_id, None


def register_operational_unit_routes(app):
    @app.post("/organisations/<organisation_id>/operational-units")
    def create_operational_unit(organisation_id):
        denied = _require_org_admin()
        if denied:
            return denied
        payload = _get_json()
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            operational_unit_id, error = _create_operational_unit(conn, organisation_id, payload)
            if error:
                conn.rollback()
                return error
            conn.commit()
            unit = _unit_belongs_to_org(conn, organisation_id, operational_unit_id)
            return jsonify(_row_to_dict(unit)), 201
        finally:
            conn.close()

    @app.get("/organisations/<organisation_id>/operational-units")
    def list_operational_units(organisation_id):
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            filters = ["organisation_id = ?"]
            params = [organisation_id]
            depot_id = request.args.get("depot_id")
            unit_kind = request.args.get("unit_kind")
            status = request.args.get("status")
            if depot_id:
                filters.append("depot_id = ?")
                params.append(depot_id)
            if unit_kind:
                filters.append("unit_kind = ?")
                params.append(_normalise_unit_kind(unit_kind))
            if status:
                filters.append("status = ?")
                params.append(_normalise_status(status))
            rows = conn.execute(
                f"""
                SELECT * FROM operational_units
                WHERE {' AND '.join(filters)}
                ORDER BY unit_kind, unit_number, display_name
                """,
                params,
            ).fetchall()
            return jsonify({"items": [_row_to_dict(row) for row in rows]})
        finally:
            conn.close()

    @app.get("/organisations/<organisation_id>/operational-units/<operational_unit_id>/detail")
    def get_operational_unit_detail(organisation_id, operational_unit_id):
        admin_error = _require_org_admin()
        if admin_error:
            return admin_error
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            unit = conn.execute(
                """
                SELECT ou.*, d.name AS depot_name
                FROM operational_units ou
                JOIN depots d
                  ON d.depot_id = ou.depot_id
                 AND d.organisation_id = ou.organisation_id
                WHERE ou.organisation_id = ? AND ou.operational_unit_id = ?
                """,
                (organisation_id, operational_unit_id),
            ).fetchone()
            if not unit:
                return _json_error("Fleet/Unit not found", 404)

            stock_rows = conn.execute(
                """
                SELECT bp.resource_id,
                       r.name AS resource_name,
                       r.resource_type,
                       r.unit_type,
                       bp.current_quantity,
                       bp.updated_at
                FROM balance_projection bp
                JOIN resources r
                  ON r.resource_id = bp.resource_id
                 AND r.organisation_id = bp.organisation_id
                WHERE bp.organisation_id = ?
                  AND bp.depot_id = ?
                  AND bp.operational_unit_id = ?
                  AND bp.current_quantity <> 0
                  AND r.is_active = 1
                ORDER BY r.resource_type ASC, r.name ASC
                """,
                (organisation_id, unit["depot_id"], operational_unit_id),
            ).fetchall()

            ledger_rows = conn.execute(
                """
                SELECT le.ledger_entry_id, le.transaction_id, le.resource_id,
                       r.name AS resource_name, r.resource_type, r.unit_type,
                       le.quantity_delta, le.created_at AS ledger_at,
                       le.operational_unit_id,
                       le.operational_unit_kind_snapshot,
                       le.operational_unit_number_snapshot,
                       le.operational_unit_display_snapshot,
                       t.transaction_type, t.direction, t.quantity,
                       t.reference_number, t.submitted_by_user_id,
                       t.submitted_by_display_name, t.posted_at
                FROM ledger_entries le
                JOIN transactions t ON t.transaction_id = le.transaction_id
                JOIN resources r
                  ON r.resource_id = le.resource_id
                 AND r.organisation_id = le.organisation_id
                WHERE le.organisation_id = ?
                  AND le.depot_id = ?
                  AND le.operational_unit_id = ?
                ORDER BY le.created_at DESC, le.ledger_entry_id DESC
                LIMIT 50
                """,
                (organisation_id, unit["depot_id"], operational_unit_id),
            ).fetchall()

            session_rows = conn.execute(
                """
                SELECT s.*, u.display_name AS user_display_name
                FROM operational_unit_sessions s
                LEFT JOIN user_accounts u ON u.user_id = s.user_id
                WHERE s.organisation_id = ?
                  AND s.operational_unit_id = ?
                  AND s.status = 'ACTIVE'
                ORDER BY s.started_at DESC
                """,
                (organisation_id, operational_unit_id),
            ).fetchall()

            permission_rows = conn.execute(
                """
                SELECT p.*, u.display_name AS user_display_name, c.name AS category_name, d.name AS depot_name
                FROM user_operational_unit_permissions p
                LEFT JOIN user_accounts u ON u.user_id = p.user_id
                LEFT JOIN operational_unit_categories c ON c.category_id = p.category_id
                LEFT JOIN depots d ON d.depot_id = p.depot_id
                WHERE p.organisation_id = ?
                  AND p.is_active = 1
                  AND (
                    p.operational_unit_id = ?
                    OR p.depot_id = ?
                    OR p.category_id IN (
                        SELECT category_id
                        FROM operational_unit_category_members
                        WHERE organisation_id = ? AND operational_unit_id = ?
                    )
                  )
                ORDER BY u.display_name ASC, p.created_at ASC
                """,
                (organisation_id, operational_unit_id, unit["depot_id"], organisation_id, operational_unit_id),
            ).fetchall()

            category_rows = conn.execute(
                """
                SELECT c.*
                FROM operational_unit_categories c
                JOIN operational_unit_category_members m
                  ON m.category_id = c.category_id
                 AND m.organisation_id = c.organisation_id
                WHERE c.organisation_id = ?
                  AND m.operational_unit_id = ?
                  AND c.is_active = 1
                ORDER BY c.name ASC
                """,
                (organisation_id, operational_unit_id),
            ).fetchall()

            pending_rows = conn.execute(
                """
                SELECT *
                FROM pending_approval_entries
                WHERE organisation_id = ?
                  AND status = 'PENDING_APPROVAL'
                  AND (
                    related_entity_id = ?
                    OR reason_text LIKE ?
                    OR related_entity_name = ?
                  )
                ORDER BY created_at DESC
                LIMIT 50
                """,
                (organisation_id, operational_unit_id, f"%{unit['display_name']}%", unit["display_name"]),
            ).fetchall()

            stock = [_row_to_dict(row) for row in stock_rows]
            ledger = [_row_to_dict(row) for row in ledger_rows]
            active_sessions = [_row_to_dict(row) for row in session_rows]
            pending = [_row_to_dict(row) for row in pending_rows]

            return jsonify({
                "report_type": "OPERATIONAL_UNIT_DETAIL",
                "organisation_id": organisation_id,
                "generated_at": now_iso(),
                "unit": _row_to_dict(unit),
                "location": {
                    "depot_id": unit["depot_id"],
                    "depot_name": unit["depot_name"],
                },
                "summary": {
                    "current_stock_quantity": sum(row["current_quantity"] for row in stock),
                    "resource_count": len(stock),
                    "recent_ledger_entry_count": len(ledger),
                    "active_session_count": len(active_sessions),
                    "permission_count": len(permission_rows),
                    "category_count": len(category_rows),
                    "open_pending_count": len(pending),
                },
                "stock": stock,
                "recent_ledger_entries": ledger,
                "active_sessions": active_sessions,
                "permissions": [_row_to_dict(row) for row in permission_rows],
                "categories": [_row_to_dict(row) for row in category_rows],
                "pending_approval_entries": pending,
            }), 200
        finally:
            conn.close()

    @app.patch("/organisations/<organisation_id>/operational-units/<operational_unit_id>")
    def update_operational_unit(organisation_id, operational_unit_id):
        denied = _require_org_admin()
        if denied:
            return denied
        payload = _get_json()
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            existing = _unit_belongs_to_org(conn, organisation_id, operational_unit_id)
            if not existing:
                return _json_error("Fleet/Unit not found", 404)

            updates = {}
            if "depot_id" in payload:
                if not _depot_belongs_to_org(conn, organisation_id, payload.get("depot_id")):
                    return _json_error("Depot/location does not belong to this organisation", 400)
                updates["depot_id"] = payload.get("depot_id")
            if "unit_type" in payload:
                updates["unit_type"] = (payload.get("unit_type") or "OTHER").strip().upper()
            if "unit_number" in payload:
                unit_number = (payload.get("unit_number") or "").strip()
                if not unit_number:
                    return _json_error("unit_number cannot be blank", 400)
                updates["unit_number"] = unit_number
            if "display_name" in payload:
                display_name = (payload.get("display_name") or "").strip()
                if not display_name:
                    return _json_error("display_name cannot be blank", 400)
                updates["display_name"] = display_name
            if "status" in payload:
                status = _normalise_status(payload.get("status"))
                if status not in VALID_STATUSES:
                    return _json_error("Invalid Fleet/Unit status", 400)
                updates["status"] = status

            if updates:
                updates["updated_at"] = now_iso()
                set_clause = ", ".join(f"{key} = ?" for key in updates)
                values = list(updates.values()) + [operational_unit_id, organisation_id]
                try:
                    conn.execute(
                        f"UPDATE operational_units SET {set_clause} WHERE operational_unit_id = ? AND organisation_id = ?",
                        values,
                    )
                except IntegrityError:
                    return _json_error("Fleet/Unit already exists for this organisation and kind", 409)

            audit_event(
                conn,
                entity_type="OperationalUnit",
                entity_id=operational_unit_id,
                action="UPDATE",
                summary="Updated Fleet/Unit operational context",
                organisation_id=organisation_id,
            )
            conn.commit()
            row = _unit_belongs_to_org(conn, organisation_id, operational_unit_id)
            return jsonify(_row_to_dict(row))
        finally:
            conn.close()

    @app.post("/organisations/<organisation_id>/operational-units/<operational_unit_id>/deactivate")
    def deactivate_operational_unit(organisation_id, operational_unit_id):
        denied = _require_org_admin()
        if denied:
            return denied
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            existing = _unit_belongs_to_org(conn, organisation_id, operational_unit_id)
            if not existing:
                return _json_error("Fleet/Unit not found", 404)
            conn.execute(
                "UPDATE operational_units SET status = 'INACTIVE', updated_at = ? WHERE operational_unit_id = ? AND organisation_id = ?",
                (now_iso(), operational_unit_id, organisation_id),
            )
            audit_event(
                conn,
                entity_type="OperationalUnit",
                entity_id=operational_unit_id,
                action="DEACTIVATE",
                summary="Deactivated Fleet/Unit operational context",
                organisation_id=organisation_id,
            )
            conn.commit()
            return jsonify({"status": "INACTIVE", "operational_unit_id": operational_unit_id})
        finally:
            conn.close()

    @app.post("/organisations/<organisation_id>/operational-unit-categories")
    def create_operational_unit_category(organisation_id):
        denied = _require_org_admin()
        if denied:
            return denied
        payload = _get_json()
        name = (payload.get("name") or "").strip()
        depot_id = payload.get("depot_id")
        description = payload.get("description")
        created_by_user_id = payload.get("created_by_user_id")
        if not name:
            return _json_error("Category name is required", 400)
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            if depot_id and not _depot_belongs_to_org(conn, organisation_id, depot_id):
                return _json_error("Depot/location does not belong to this organisation", 400)
            category_id = make_id("ouc")
            now = now_iso()
            try:
                conn.execute(
                    """
                    INSERT INTO operational_unit_categories (
                        category_id, organisation_id, depot_id, name, description,
                        is_active, created_by_user_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (category_id, organisation_id, depot_id, name, description, created_by_user_id, now, now),
                )
            except IntegrityError:
                return _json_error("Fleet/Unit category already exists for this organisation", 409)
            audit_event(
                conn,
                entity_type="OperationalUnitCategory",
                entity_id=category_id,
                action="CREATE",
                summary=f"Created Fleet/Unit category {name}",
                organisation_id=organisation_id,
            )
            conn.commit()
            row = _category_belongs_to_org(conn, organisation_id, category_id)
            return jsonify(_row_to_dict(row)), 201
        finally:
            conn.close()

    @app.get("/organisations/<organisation_id>/operational-unit-categories")
    def list_operational_unit_categories(organisation_id):
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            rows = conn.execute(
                """
                SELECT * FROM operational_unit_categories
                WHERE organisation_id = ?
                ORDER BY is_active DESC, name
                """,
                (organisation_id,),
            ).fetchall()
            return jsonify({"items": [_row_to_dict(row) for row in rows]})
        finally:
            conn.close()

    @app.patch("/organisations/<organisation_id>/operational-unit-categories/<category_id>")
    def update_operational_unit_category(organisation_id, category_id):
        denied = _require_org_admin()
        if denied:
            return denied
        payload = _get_json()
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            existing = _category_belongs_to_org(conn, organisation_id, category_id)
            if not existing:
                return _json_error("Fleet/Unit category not found", 404)
            updates = {}
            if "name" in payload:
                name = (payload.get("name") or "").strip()
                if not name:
                    return _json_error("Category name cannot be blank", 400)
                updates["name"] = name
            if "description" in payload:
                updates["description"] = payload.get("description")
            if "depot_id" in payload:
                depot_id = payload.get("depot_id")
                if depot_id and not _depot_belongs_to_org(conn, organisation_id, depot_id):
                    return _json_error("Depot/location does not belong to this organisation", 400)
                updates["depot_id"] = depot_id
            if "is_active" in payload:
                updates["is_active"] = 1 if payload.get("is_active") else 0
            if updates:
                updates["updated_at"] = now_iso()
                set_clause = ", ".join(f"{key} = ?" for key in updates)
                values = list(updates.values()) + [category_id, organisation_id]
                try:
                    conn.execute(
                        f"UPDATE operational_unit_categories SET {set_clause} WHERE category_id = ? AND organisation_id = ?",
                        values,
                    )
                except IntegrityError:
                    return _json_error("Fleet/Unit category already exists for this organisation", 409)
            audit_event(
                conn,
                entity_type="OperationalUnitCategory",
                entity_id=category_id,
                action="UPDATE",
                summary="Updated Fleet/Unit category",
                organisation_id=organisation_id,
            )
            conn.commit()
            row = _category_belongs_to_org(conn, organisation_id, category_id)
            return jsonify(_row_to_dict(row))
        finally:
            conn.close()

    @app.post("/organisations/<organisation_id>/operational-unit-categories/<category_id>/deactivate")
    def deactivate_operational_unit_category(organisation_id, category_id):
        denied = _require_org_admin()
        if denied:
            return denied
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            existing = _category_belongs_to_org(conn, organisation_id, category_id)
            if not existing:
                return _json_error("Fleet/Unit category not found", 404)
            conn.execute(
                "UPDATE operational_unit_categories SET is_active = 0, updated_at = ? WHERE category_id = ? AND organisation_id = ?",
                (now_iso(), category_id, organisation_id),
            )
            audit_event(
                conn,
                entity_type="OperationalUnitCategory",
                entity_id=category_id,
                action="DEACTIVATE",
                summary="Deactivated Fleet/Unit category",
                organisation_id=organisation_id,
            )
            conn.commit()
            return jsonify({"category_id": category_id, "is_active": 0})
        finally:
            conn.close()

    @app.post("/organisations/<organisation_id>/operational-unit-categories/<category_id>/members")
    def add_operational_unit_category_member(organisation_id, category_id):
        denied = _require_org_admin()
        if denied:
            return denied
        payload = _get_json()
        operational_unit_id = payload.get("operational_unit_id")
        created_by_user_id = payload.get("created_by_user_id")
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            if not _category_belongs_to_org(conn, organisation_id, category_id):
                return _json_error("Fleet/Unit category not found", 404)
            if not _unit_belongs_to_org(conn, organisation_id, operational_unit_id):
                return _json_error("Fleet/Unit not found", 404)
            try:
                conn.execute(
                    """
                    INSERT INTO operational_unit_category_members (
                        category_id, operational_unit_id, organisation_id, created_by_user_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (category_id, operational_unit_id, organisation_id, created_by_user_id, now_iso()),
                )
            except IntegrityError:
                return _json_error("Fleet/Unit is already in this category", 409)
            audit_event(
                conn,
                entity_type="OperationalUnitCategoryMember",
                entity_id=f"{category_id}:{operational_unit_id}",
                action="CREATE",
                summary="Added Fleet/Unit to category",
                organisation_id=organisation_id,
            )
            conn.commit()
            return jsonify({"category_id": category_id, "operational_unit_id": operational_unit_id}), 201
        finally:
            conn.close()

    @app.delete("/organisations/<organisation_id>/operational-unit-categories/<category_id>/members/<operational_unit_id>")
    def remove_operational_unit_category_member(organisation_id, category_id, operational_unit_id):
        denied = _require_org_admin()
        if denied:
            return denied
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            conn.execute(
                """
                DELETE FROM operational_unit_category_members
                WHERE organisation_id = ? AND category_id = ? AND operational_unit_id = ?
                """,
                (organisation_id, category_id, operational_unit_id),
            )
            audit_event(
                conn,
                entity_type="OperationalUnitCategoryMember",
                entity_id=f"{category_id}:{operational_unit_id}",
                action="DELETE",
                summary="Removed Fleet/Unit from category",
                organisation_id=organisation_id,
            )
            conn.commit()
            return jsonify({"removed": True})
        finally:
            conn.close()

    @app.post("/organisations/<organisation_id>/users/<user_id>/operational-unit-permissions")
    def create_operational_unit_permission(organisation_id, user_id):
        denied = _require_org_admin()
        if denied:
            return denied
        payload = _get_json()
        permission_level = (payload.get("permission_level") or "OPERATE").strip().upper()
        created_by_user_id = payload.get("created_by_user_id")
        if permission_level not in VALID_PERMISSION_LEVELS:
            return _json_error("Invalid permission level", 400)
        if not _permission_payload_is_valid(payload):
            return _json_error("Permission requires a Fleet/Unit, category, or location", 400)

        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            if not _user_belongs_to_org(conn, organisation_id, user_id):
                return _json_error("User does not belong to this organisation", 400)
            operational_unit_id = payload.get("operational_unit_id")
            category_id = payload.get("category_id")
            depot_id = payload.get("depot_id")
            if operational_unit_id and not _unit_belongs_to_org(conn, organisation_id, operational_unit_id):
                return _json_error("Fleet/Unit not found", 404)
            if category_id and not _category_belongs_to_org(conn, organisation_id, category_id):
                return _json_error("Fleet/Unit category not found", 404)
            if depot_id and not _depot_belongs_to_org(conn, organisation_id, depot_id):
                return _json_error("Depot/location does not belong to this organisation", 400)
            permission_id = make_id("oup")
            now = now_iso()
            conn.execute(
                """
                INSERT INTO user_operational_unit_permissions (
                    permission_id, organisation_id, user_id, operational_unit_id,
                    category_id, depot_id, permission_level, is_active,
                    created_by_user_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    permission_id,
                    organisation_id,
                    user_id,
                    operational_unit_id,
                    category_id,
                    depot_id,
                    permission_level,
                    created_by_user_id,
                    now,
                    now,
                ),
            )
            audit_event(
                conn,
                entity_type="OperationalUnitPermission",
                entity_id=permission_id,
                action="CREATE",
                summary="Granted Fleet/Unit permission",
                organisation_id=organisation_id,
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM user_operational_unit_permissions WHERE permission_id = ?",
                (permission_id,),
            ).fetchone()
            return jsonify(_row_to_dict(row)), 201
        finally:
            conn.close()

    @app.get("/organisations/<organisation_id>/users/<user_id>/operational-unit-permissions")
    def list_operational_unit_permissions(organisation_id, user_id):
        if g.current_user.get("role") not in ORG_ADMIN_ROLES and g.current_user.get("user_id") != user_id:
            return _json_error("You can only view your own Fleet/Unit permissions", 403)
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            rows = conn.execute(
                """
                SELECT * FROM user_operational_unit_permissions
                WHERE organisation_id = ? AND user_id = ? AND is_active = 1
                ORDER BY created_at
                """,
                (organisation_id, user_id),
            ).fetchall()
            return jsonify({"items": [_row_to_dict(row) for row in rows]})
        finally:
            conn.close()

    @app.delete("/organisations/<organisation_id>/users/<user_id>/operational-unit-permissions/<permission_id>")
    def delete_operational_unit_permission(organisation_id, user_id, permission_id):
        denied = _require_org_admin()
        if denied:
            return denied
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            conn.execute(
                """
                UPDATE user_operational_unit_permissions
                SET is_active = 0, updated_at = ?
                WHERE organisation_id = ? AND user_id = ? AND permission_id = ?
                """,
                (now_iso(), organisation_id, user_id, permission_id),
            )
            audit_event(
                conn,
                entity_type="OperationalUnitPermission",
                entity_id=permission_id,
                action="DELETE",
                summary="Removed Fleet/Unit permission",
                organisation_id=organisation_id,
            )
            conn.commit()
            return jsonify({"removed": True})
        finally:
            conn.close()

    @app.get("/organisations/<organisation_id>/users/<user_id>/selectable-operational-units")
    def list_selectable_operational_units(organisation_id, user_id):
        if g.current_user.get("role") not in ORG_ADMIN_ROLES and g.current_user.get("user_id") != user_id:
            return _json_error("You can only view your own selectable Fleet/Units", 403)
        depot_id = request.args.get("depot_id")
        unit_kind = request.args.get("unit_kind")
        params = [organisation_id, user_id]
        filters = [
            "ou.organisation_id = ?", "p.user_id = ?", "ou.status = 'ACTIVE'",
            "p.is_active = 1", "p.permission_level IN ('OPERATE', 'ADMIN')",
        ]
        if depot_id:
            filters.append("ou.depot_id = ?")
            params.append(depot_id)
        if unit_kind:
            filters.append("ou.unit_kind = ?")
            params.append(_normalise_unit_kind(unit_kind))

        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            rows = conn.execute(
                f"""
                SELECT DISTINCT ou.*
                FROM operational_units ou
                JOIN user_operational_unit_permissions p
                  ON p.organisation_id = ou.organisation_id
                 AND (
                    p.operational_unit_id = ou.operational_unit_id
                    OR p.depot_id = ou.depot_id
                    OR p.category_id IN (
                        SELECT m.category_id
                        FROM operational_unit_category_members m
                        JOIN operational_unit_categories c
                          ON c.category_id = m.category_id
                         AND c.organisation_id = m.organisation_id
                         AND c.is_active = 1
                        WHERE m.organisation_id = ou.organisation_id
                          AND m.operational_unit_id = ou.operational_unit_id
                    )
                 )
                WHERE {' AND '.join(filters)}
                ORDER BY ou.unit_kind, ou.unit_number, ou.display_name
                """,
                params,
            ).fetchall()
            return jsonify({"items": [_row_to_dict(row) for row in rows]})
        finally:
            conn.close()

    @app.post("/organisations/<organisation_id>/operational-units/<operational_unit_id>/sessions")
    def create_operational_unit_session(organisation_id, operational_unit_id):
        payload = _get_json()
        current_user = g.current_user
        user_id = payload.get("user_id") if current_user.get("role") in ORG_ADMIN_ROLES else current_user.get("user_id")
        if not user_id:
            return _json_error("user_id is required", 400)
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            if not _unit_belongs_to_org(conn, organisation_id, operational_unit_id):
                return _json_error("Fleet/Unit not found", 404)
            if not _user_belongs_to_org(conn, organisation_id, user_id):
                return _json_error("User does not belong to this organisation", 400)
            if current_user.get("role") not in ORG_ADMIN_ROLES and not user_can_operate_unit(
                conn, organisation_id, user_id, operational_unit_id
            ):
                return _json_error("OPERATE permission is required for this Fleet/Unit", 403)
            unit_session_id = make_id("ous")
            now = now_iso()
            try:
                conn.execute(
                    """
                    INSERT INTO operational_unit_sessions (
                        unit_session_id, organisation_id, operational_unit_id,
                        user_id, status, started_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)
                    """,
                    (unit_session_id, organisation_id, operational_unit_id, user_id, now, now),
                )
            except IntegrityError:
                return _json_error("Fleet/Unit already active with another allocator", 409)
            audit_event(
                conn,
                entity_type="OperationalUnitSession",
                entity_id=unit_session_id,
                action="CREATE",
                summary="Started active Fleet/Unit allocator session",
                organisation_id=organisation_id,
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM operational_unit_sessions WHERE unit_session_id = ?",
                (unit_session_id,),
            ).fetchone()
            return jsonify(_row_to_dict(row)), 201
        finally:
            conn.close()

    @app.post("/organisations/<organisation_id>/operational-units/<operational_unit_id>/sessions/<unit_session_id>/release")
    def release_operational_unit_session(organisation_id, operational_unit_id, unit_session_id):
        payload = _get_json()
        release_reason = payload.get("release_reason")
        released_by_user_id = g.current_user.get("user_id")
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            existing = conn.execute(
                """
                SELECT * FROM operational_unit_sessions
                WHERE unit_session_id = ? AND organisation_id = ? AND operational_unit_id = ? AND status = 'ACTIVE'
                """,
                (unit_session_id, organisation_id, operational_unit_id),
            ).fetchone()
            if not existing:
                return _json_error("Active Fleet/Unit session not found", 404)
            if g.current_user.get("role") not in ORG_ADMIN_ROLES and existing["user_id"] != released_by_user_id:
                return _json_error("You can only release your own Fleet/Unit session", 403)
            conn.execute(
                """
                UPDATE operational_unit_sessions
                SET status = 'RELEASED', ended_at = ?, release_reason = ?, force_released_by_user_id = ?
                WHERE unit_session_id = ? AND organisation_id = ? AND operational_unit_id = ?
                """,
                (now_iso(), release_reason, released_by_user_id, unit_session_id, organisation_id, operational_unit_id),
            )
            audit_event(
                conn,
                entity_type="OperationalUnitSession",
                entity_id=unit_session_id,
                action="RELEASE",
                summary="Released active Fleet/Unit allocator session",
                organisation_id=organisation_id,
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM operational_unit_sessions WHERE unit_session_id = ?",
                (unit_session_id,),
            ).fetchone()
            return jsonify(_row_to_dict(row))
        finally:
            conn.close()

    @app.get("/organisations/<organisation_id>/operational-unit-sessions/active")
    def list_active_operational_unit_sessions(organisation_id):
        conn = get_conn()
        try:
            ensure_operational_unit_tables(conn)
            rows = conn.execute(
                """
                SELECT s.*, ou.unit_kind, ou.unit_number, ou.display_name, ou.depot_id
                FROM operational_unit_sessions s
                JOIN operational_units ou
                  ON ou.operational_unit_id = s.operational_unit_id
                 AND ou.organisation_id = s.organisation_id
                WHERE s.organisation_id = ? AND s.status = 'ACTIVE'
                ORDER BY s.started_at
                """,
                (organisation_id,),
            ).fetchall()
            return jsonify({"items": [_row_to_dict(row) for row in rows]})
        finally:
            conn.close()
