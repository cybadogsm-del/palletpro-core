"""
modules/resources.py — Resources brick

Covers:
  - Schema migration helpers (ensure_resource_cleanup_columns)
  - Pending-entry helper for opening balance (update_pending_entries_ready_for_opening_balance)
  - Resource module overview dashboard
  - Brand requests  (create, list, get, approve)
  - Category requests (create, list, get, approve)
  - Resource requests (create, list, get, approve)
  - Resources CRUD (list, get, create, update, classify, deactivate)
  - Opening balance (create)
"""

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso


# ── Schema migration helpers ───────────────────────────────────────────────────

def ensure_resource_cleanup_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(resources)").fetchall()}
    if "merged_into_resource_id" not in cols:
        conn.execute("ALTER TABLE resources ADD COLUMN merged_into_resource_id TEXT")
    if "inactive_reason_code" not in cols:
        conn.execute("ALTER TABLE resources ADD COLUMN inactive_reason_code TEXT")
    if "inactive_reason_text" not in cols:
        conn.execute("ALTER TABLE resources ADD COLUMN inactive_reason_text TEXT")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE resources ADD COLUMN updated_at TEXT")


# ── Opening-balance pending-entry helper ──────────────────────────────────────

def update_pending_entries_ready_for_opening_balance(conn, organisation_id, depot_id):
    rows = conn.execute(
        """
        SELECT pending_entry_id
        FROM pending_approval_entries
        WHERE organisation_id = ?
          AND related_entity_id = ?
          AND reason_code = 'NIL_OPENING_BALANCE'
          AND status = 'AWAITING_FIX'
        """,
        (organisation_id, depot_id)
    ).fetchall()

    for row in rows:
        conn.execute(
            """
            UPDATE pending_approval_entries
            SET status = ?, can_approve_now = ?, updated_at = ?
            WHERE pending_entry_id = ?
            """,
            ("READY_TO_APPROVE", 1, now_iso(), row["pending_entry_id"])
        )

        audit_event(
            conn,
            entity_type="PendingApprovalEntry",
            entity_id=row["pending_entry_id"],
            action="READY_TO_APPROVE",
            summary="Pending entry is now ready to approve after opening balance was set",
            organisation_id=organisation_id
        )


# ── Route registration ─────────────────────────────────────────────────────────

def register_resource_routes(app, create_pending_entry, generate_transaction_reference, ensure_transaction_numbering_tables):

    @app.get("/organisations/<organisation_id>/resource-module")
    def get_resource_module_overview(organisation_id):
        conn = get_conn()

        org = conn.execute(
            """
            SELECT organisation_id, name, created_at
            FROM organisations
            WHERE organisation_id = ?
            """,
            (organisation_id,)
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        resource_count_total = conn.execute(
            "SELECT COUNT(*) AS c FROM resources WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()["c"]

        resource_count_active = conn.execute(
            "SELECT COUNT(*) AS c FROM resources WHERE organisation_id = ? AND is_active = 1",
            (organisation_id,)
        ).fetchone()["c"]

        resource_count_inactive = conn.execute(
            "SELECT COUNT(*) AS c FROM resources WHERE organisation_id = ? AND is_active = 0",
            (organisation_id,)
        ).fetchone()["c"]

        brand_count = conn.execute(
            "SELECT COUNT(*) AS c FROM brands WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()["c"]

        category_count = conn.execute(
            "SELECT COUNT(*) AS c FROM categories WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()["c"]

        status_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS item_count
            FROM pending_approval_entries
            WHERE organisation_id = ?
              AND entry_type IN ('ResourceRequest', 'BrandRequest', 'CategoryRequest')
            GROUP BY status
            """,
            (organisation_id,)
        ).fetchall()

        counts = {
            "PENDING_APPROVAL": 0,
            "AWAITING_FIX": 0,
            "READY_TO_APPROVE": 0,
            "RESOLVED": 0,
            "REJECTED": 0,
        }

        for row in status_rows:
            counts[row["status"]] = row["item_count"]

        recent_open_rows = conn.execute(
            """
            SELECT
                pending_entry_id,
                entry_type,
                status,
                reason_code,
                reason_text,
                related_entity_type,
                related_entity_id,
                related_entity_name,
                resource_id,
                resource_name,
                direct_action_label,
                direct_action_target_id,
                source_record_id,
                submitted_by_display_name,
                created_at,
                updated_at
            FROM pending_approval_entries
            WHERE organisation_id = ?
              AND entry_type IN ('ResourceRequest', 'BrandRequest', 'CategoryRequest')
              AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (organisation_id,)
        ).fetchall()

        recent_history_rows = conn.execute(
            """
            SELECT
                pending_entry_id,
                entry_type,
                status,
                reason_code,
                reason_text,
                rejection_reason_code,
                rejection_reason_text,
                related_entity_type,
                related_entity_id,
                related_entity_name,
                resource_id,
                resource_name,
                direct_action_label,
                direct_action_target_id,
                source_record_id,
                submitted_by_display_name,
                created_at,
                updated_at
            FROM pending_approval_entries
            WHERE organisation_id = ?
              AND entry_type IN ('ResourceRequest', 'BrandRequest', 'CategoryRequest')
              AND status IN ('RESOLVED', 'REJECTED')
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 10
            """,
            (organisation_id,)
        ).fetchall()

        recent_resources = conn.execute(
            """
            SELECT
                r.resource_id,
                r.name,
                r.resource_type,
                r.unit_type,
                r.is_active,
                r.created_at,
                r.updated_at,
                r.category_id,
                c.name AS category_name,
                r.brand_id,
                b.name AS brand_name,
                r.merged_into_resource_id,
                r.inactive_reason_code,
                r.inactive_reason_text
            FROM resources r
            LEFT JOIN categories c ON c.category_id = r.category_id
            LEFT JOIN brands b ON b.brand_id = r.brand_id
            WHERE r.organisation_id = ?
            ORDER BY r.is_active DESC, r.created_at DESC
            LIMIT 10
            """,
            (organisation_id,)
        ).fetchall()

        recent_brands = conn.execute(
            """
            SELECT
                brand_id,
                name,
                is_active,
                created_at
            FROM brands
            WHERE organisation_id = ?
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (organisation_id,)
        ).fetchall()

        recent_categories = conn.execute(
            """
            SELECT
                category_id,
                name,
                is_active,
                created_at
            FROM categories
            WHERE organisation_id = ?
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (organisation_id,)
        ).fetchall()

        conn.close()

        def decorate_request_item(row_dict):
            mapping = {
                "ResourceRequest": {
                    "entry_target_section": "requests/resources",
                    "entry_target_entity_type": "ResourceRequest",
                    "entry_target_action": "review_create_resource",
                    "highlight_key": row_dict.get("source_record_id"),
                },
                "BrandRequest": {
                    "entry_target_section": "requests/brands",
                    "entry_target_entity_type": "BrandRequest",
                    "entry_target_action": "review_create_brand",
                    "highlight_key": row_dict.get("source_record_id"),
                },
                "CategoryRequest": {
                    "entry_target_section": "requests/categories",
                    "entry_target_entity_type": "CategoryRequest",
                    "entry_target_action": "review_create_category",
                    "highlight_key": row_dict.get("source_record_id"),
                },
            }
            row_dict.update(mapping.get(row_dict.get("entry_type"), {}))
            return row_dict

        recent_open_items = [decorate_request_item(dict(r)) for r in recent_open_rows]
        recent_history_items = [decorate_request_item(dict(r)) for r in recent_history_rows]

        recent_resource_items = []
        for row in recent_resources:
            d = dict(row)
            d["is_active"] = bool(d["is_active"])
            d["entry_target_action"] = "open_resource_profile"
            d["entry_target_entity_type"] = "Resource"
            d["entry_target_section"] = "resources"
            d["highlight_key"] = d["resource_id"]
            recent_resource_items.append(d)

        recent_brand_items = []
        for row in recent_brands:
            d = dict(row)
            d["is_active"] = bool(d["is_active"])
            d["entry_target_action"] = "open_brand_profile"
            d["entry_target_entity_type"] = "Brand"
            d["entry_target_section"] = "brands"
            d["highlight_key"] = d["brand_id"]
            recent_brand_items.append(d)

        recent_category_items = []
        for row in recent_categories:
            d = dict(row)
            d["is_active"] = bool(d["is_active"])
            d["entry_target_action"] = "open_category_profile"
            d["entry_target_entity_type"] = "Category"
            d["entry_target_section"] = "categories"
            d["highlight_key"] = d["category_id"]
            recent_category_items.append(d)

        open_count = (
            counts["PENDING_APPROVAL"]
            + counts["AWAITING_FIX"]
            + counts["READY_TO_APPROVE"]
        )

        return jsonify({
            "module_key": "resource-module",
            "organisation_id": org["organisation_id"],
            "organisation_name": org["name"],
            "created_at": org["created_at"],
            "default_section": "open_requests",
            "navigation": {
                "open_requests": {
                    "label": "Open Requests",
                    "count": open_count
                },
                "history": {
                    "label": "History",
                    "count": counts["RESOLVED"] + counts["REJECTED"]
                },
                "resources": {
                    "label": "Resources",
                    "count": resource_count_active
                },
                "brands": {
                    "label": "Brands",
                    "count": brand_count
                },
                "categories": {
                    "label": "Categories",
                    "count": category_count
                }
            },
            "summary": {
                "resource_count": resource_count_active,
                "resource_count_active": resource_count_active,
                "resource_count_inactive": resource_count_inactive,
                "resource_count_total": resource_count_total,
                "brand_count": brand_count,
                "category_count": category_count,
                "open_count": open_count,
                "pending_approval_count": counts["PENDING_APPROVAL"],
                "awaiting_fix_count": counts["AWAITING_FIX"],
                "ready_to_approve_count": counts["READY_TO_APPROVE"],
                "resolved_count": counts["RESOLVED"],
                "rejected_count": counts["REJECTED"]
            },
            "recent_open_items": recent_open_items,
            "recent_history_items": recent_history_items,
            "recent_resources": recent_resource_items,
            "recent_brands": recent_brand_items,
            "recent_categories": recent_category_items
        }), 200


    @app.post("/brand-requests")
    def create_brand_request():
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        requested_name = (body.get("requested_name") or "").strip()
        note = (body.get("note") or "").strip() or None
        submitted_by_display_name = (body.get("submitted_by_display_name") or "Unknown User").strip()

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400
        if not requested_name:
            return jsonify({"error": "requested_name is required"}), 400

        conn = get_conn()
        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        existing_brand = conn.execute(
            """
            SELECT brand_id, name
            FROM brands
            WHERE organisation_id = ?
              AND LOWER(TRIM(name)) = LOWER(TRIM(?))
            LIMIT 1
            """,
            (organisation_id, requested_name)
        ).fetchone()

        if existing_brand:
            conn.close()
            return jsonify({
                "error": "Brand already exists",
                "brand_id": existing_brand["brand_id"],
                "name": existing_brand["name"]
            }), 409

        existing_request = conn.execute(
            """
            SELECT brand_request_id, status, requested_name
            FROM brand_requests
            WHERE organisation_id = ?
              AND LOWER(TRIM(requested_name)) = LOWER(TRIM(?))
              AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
            LIMIT 1
            """,
            (organisation_id, requested_name)
        ).fetchone()

        if existing_request:
            conn.close()
            return jsonify({
                "error": "Duplicate open brand request already exists",
                "brand_request_id": existing_request["brand_request_id"],
                "status": existing_request["status"],
                "requested_name": existing_request["requested_name"]
            }), 409

        brand_request_id = make_id("breq")

        conn.execute(
            """
            INSERT INTO brand_requests (
                brand_request_id,
                organisation_id,
                requested_name,
                note,
                submitted_by_display_name,
                status,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                brand_request_id,
                organisation_id,
                requested_name,
                note,
                submitted_by_display_name,
                "PENDING_APPROVAL",
                now_iso(),
                now_iso()
            )
        )

        audit_event(
            conn,
            entity_type="BrandRequest",
            entity_id=brand_request_id,
            action="CREATE",
            summary=f"Created brand request: {requested_name}",
            organisation_id=organisation_id
        )

        pending_entry_id = create_pending_entry(
            conn=conn,
            organisation_id=organisation_id,
            entry_type="BrandRequest",
            source_record_id=brand_request_id,
            source_module="BrandRequests",
            submitted_by_display_name=submitted_by_display_name,
            related_entity_type="ResourceModule",
            related_entity_id="resource-module",
            related_entity_name="Resource Module",
            reason_code="MISSING_DROPDOWN_OPTION",
            reason_text="User requested a new brand because the required dropdown option was not available.",
            direct_action_type="GoToResourceModule",
            direct_action_target_id="resource-module",
            direct_action_label="Review / Create Brand",
            can_approve_now=True,
            can_reject_now=True,
            resource_id=None,
            resource_name=requested_name,
            status="PENDING_APPROVAL"
        )

        conn.commit()
        conn.close()

        return jsonify({
            "brand_request_id": brand_request_id,
            "status": "PENDING_APPROVAL",
            "pending_entry_id": pending_entry_id,
            "requested_name": requested_name,
            "note": note,
            "submitted_by_display_name": submitted_by_display_name,
            "message": "Your request has been saved and sent to your Org Admin for approval. You can continue with your entry."
        }), 201

    @app.get("/brand-requests")
    def list_brand_requests():
        organisation_id = request.args.get("organisation_id")
        status = request.args.get("status")

        conn = get_conn()

        sql = """
            SELECT
                br.brand_request_id,
                br.organisation_id,
                o.name AS organisation_name,
                br.requested_name,
                br.note,
                br.submitted_by_display_name,
                br.status,
                br.rejection_reason_code,
                br.rejection_reason_text,
                br.created_at,
                br.updated_at
            FROM brand_requests br
            LEFT JOIN organisations o ON o.organisation_id = br.organisation_id
            WHERE 1=1
        """
        params = []

        if organisation_id:
            sql += " AND br.organisation_id = ?"
            params.append(organisation_id)

        if status:
            sql += " AND br.status = ?"
            params.append(status)

        sql += " ORDER BY br.created_at DESC"

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        return jsonify({
            "count": len(rows),
            "items": [dict(r) for r in rows]
        }), 200

    @app.get("/brand-requests/<brand_request_id>")
    def get_brand_request(brand_request_id):
        conn = get_conn()

        row = conn.execute(
            """
            SELECT
                br.brand_request_id,
                br.organisation_id,
                o.name AS organisation_name,
                br.requested_name,
                br.note,
                br.submitted_by_display_name,
                br.status,
                br.rejection_reason_code,
                br.rejection_reason_text,
                br.created_at,
                br.updated_at
            FROM brand_requests br
            LEFT JOIN organisations o ON o.organisation_id = br.organisation_id
            WHERE br.brand_request_id = ?
            """,
            (brand_request_id,)
        ).fetchone()

        conn.close()

        if not row:
            return jsonify({"error": "Brand request not found"}), 404

        return jsonify(dict(row)), 200

    @app.post("/brand-requests/<brand_request_id>/approve")
    def approve_brand_request(brand_request_id):
        conn = get_conn()

        req = conn.execute(
            "SELECT * FROM brand_requests WHERE brand_request_id = ?",
            (brand_request_id,)
        ).fetchone()

        if not req:
            conn.close()
            return jsonify({"error": "Brand request not found"}), 404

        if req["status"] != "PENDING_APPROVAL":
            conn.close()
            return jsonify({"error": "Brand request is not pending approval"}), 400

        brand_id = make_id("brand")

        conn.execute(
            """
            INSERT INTO brands (
                brand_id,
                organisation_id,
                name,
                is_active,
                created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                brand_id,
                req["organisation_id"],
                req["requested_name"],
                1,
                now_iso()
            )
        )

        conn.execute(
            """
            UPDATE brand_requests
            SET status = ?, updated_at = ?
            WHERE brand_request_id = ?
            """,
            ("RESOLVED", now_iso(), brand_request_id)
        )

        conn.execute(
            """
            UPDATE pending_approval_entries
            SET status = ?, updated_at = ?
            WHERE source_record_id = ?
              AND entry_type = 'BrandRequest'
              AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
            """,
            ("RESOLVED", now_iso(), brand_request_id)
        )

        audit_event(
            conn,
            entity_type="Brand",
            entity_id=brand_id,
            action="CREATE",
            summary=f"Created brand from request: {req['requested_name']}",
            organisation_id=req["organisation_id"]
        )

        audit_event(
            conn,
            entity_type="BrandRequest",
            entity_id=brand_request_id,
            action="APPROVE",
            summary=f"Approved brand request: {req['requested_name']}",
            organisation_id=req["organisation_id"]
        )

        pending_rows = conn.execute(
            """
            SELECT pending_entry_id
            FROM pending_approval_entries
            WHERE source_record_id = ?
              AND entry_type = 'BrandRequest'
            """,
            (brand_request_id,)
        ).fetchall()

        for row in pending_rows:
            audit_event(
                conn,
                entity_type="PendingApprovalEntry",
                entity_id=row["pending_entry_id"],
                action="APPROVE",
                summary="Pending approval entry approved and resolved for brand request",
                organisation_id=req["organisation_id"]
            )

        conn.commit()
        conn.close()

        return jsonify({
            "brand_request_id": brand_request_id,
            "brand_id": brand_id,
            "requested_name": req["requested_name"],
            "status": "RESOLVED",
            "brand_status": "CREATED"
        }), 200


    @app.post("/category-requests")
    def create_category_request():
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        requested_name = (body.get("requested_name") or "").strip()
        note = (body.get("note") or "").strip() or None
        submitted_by_display_name = (body.get("submitted_by_display_name") or "Unknown User").strip()

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400
        if not requested_name:
            return jsonify({"error": "requested_name is required"}), 400

        conn = get_conn()
        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        existing_category = conn.execute(
            """
            SELECT category_id, name
            FROM categories
            WHERE organisation_id = ?
              AND LOWER(TRIM(name)) = LOWER(TRIM(?))
            LIMIT 1
            """,
            (organisation_id, requested_name)
        ).fetchone()

        if existing_category:
            conn.close()
            return jsonify({
                "error": "Category already exists",
                "category_id": existing_category["category_id"],
                "name": existing_category["name"]
            }), 409

        existing_request = conn.execute(
            """
            SELECT category_request_id, status, requested_name
            FROM category_requests
            WHERE organisation_id = ?
              AND LOWER(TRIM(requested_name)) = LOWER(TRIM(?))
              AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
            LIMIT 1
            """,
            (organisation_id, requested_name)
        ).fetchone()

        if existing_request:
            conn.close()
            return jsonify({
                "error": "Duplicate open category request already exists",
                "category_request_id": existing_request["category_request_id"],
                "status": existing_request["status"],
                "requested_name": existing_request["requested_name"]
            }), 409

        category_request_id = make_id("creq")

        conn.execute(
            """
            INSERT INTO category_requests (
                category_request_id,
                organisation_id,
                requested_name,
                note,
                submitted_by_display_name,
                status,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                category_request_id,
                organisation_id,
                requested_name,
                note,
                submitted_by_display_name,
                "PENDING_APPROVAL",
                now_iso(),
                now_iso()
            )
        )

        audit_event(
            conn,
            entity_type="CategoryRequest",
            entity_id=category_request_id,
            action="CREATE",
            summary=f"Created category request: {requested_name}",
            organisation_id=organisation_id
        )

        pending_entry_id = create_pending_entry(
            conn=conn,
            organisation_id=organisation_id,
            entry_type="CategoryRequest",
            source_record_id=category_request_id,
            source_module="CategoryRequests",
            submitted_by_display_name=submitted_by_display_name,
            related_entity_type="ResourceModule",
            related_entity_id="resource-module",
            related_entity_name="Resource Module",
            reason_code="MISSING_DROPDOWN_OPTION",
            reason_text="User requested a new category because the required dropdown option was not available.",
            direct_action_type="GoToResourceModule",
            direct_action_target_id="resource-module",
            direct_action_label="Review / Create Category",
            can_approve_now=True,
            can_reject_now=True,
            resource_id=None,
            resource_name=requested_name,
            status="PENDING_APPROVAL"
        )

        conn.commit()
        conn.close()

        return jsonify({
            "category_request_id": category_request_id,
            "status": "PENDING_APPROVAL",
            "pending_entry_id": pending_entry_id,
            "requested_name": requested_name,
            "note": note,
            "submitted_by_display_name": submitted_by_display_name,
            "message": "Your request has been saved and sent to your Org Admin for approval. You can continue with your entry."
        }), 201

    @app.get("/category-requests")
    def list_category_requests():
        organisation_id = request.args.get("organisation_id")
        status = request.args.get("status")

        conn = get_conn()

        sql = """
            SELECT
                cr.category_request_id,
                cr.organisation_id,
                o.name AS organisation_name,
                cr.requested_name,
                cr.note,
                cr.submitted_by_display_name,
                cr.status,
                cr.rejection_reason_code,
                cr.rejection_reason_text,
                cr.created_at,
                cr.updated_at
            FROM category_requests cr
            LEFT JOIN organisations o ON o.organisation_id = cr.organisation_id
            WHERE 1=1
        """
        params = []

        if organisation_id:
            sql += " AND cr.organisation_id = ?"
            params.append(organisation_id)

        if status:
            sql += " AND cr.status = ?"
            params.append(status)

        sql += " ORDER BY cr.created_at DESC"

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        return jsonify({
            "count": len(rows),
            "items": [dict(r) for r in rows]
        }), 200

    @app.get("/category-requests/<category_request_id>")
    def get_category_request(category_request_id):
        conn = get_conn()

        row = conn.execute(
            """
            SELECT
                cr.category_request_id,
                cr.organisation_id,
                o.name AS organisation_name,
                cr.requested_name,
                cr.note,
                cr.submitted_by_display_name,
                cr.status,
                cr.rejection_reason_code,
                cr.rejection_reason_text,
                cr.created_at,
                cr.updated_at
            FROM category_requests cr
            LEFT JOIN organisations o ON o.organisation_id = cr.organisation_id
            WHERE cr.category_request_id = ?
            """,
            (category_request_id,)
        ).fetchone()

        conn.close()

        if not row:
            return jsonify({"error": "Category request not found"}), 404

        return jsonify(dict(row)), 200

    @app.post("/category-requests/<category_request_id>/approve")
    def approve_category_request(category_request_id):
        conn = get_conn()

        req = conn.execute(
            "SELECT * FROM category_requests WHERE category_request_id = ?",
            (category_request_id,)
        ).fetchone()

        if not req:
            conn.close()
            return jsonify({"error": "Category request not found"}), 404

        if req["status"] != "PENDING_APPROVAL":
            conn.close()
            return jsonify({"error": "Category request is not pending approval"}), 400

        category_id = make_id("cat")

        conn.execute(
            """
            INSERT INTO categories (
                category_id,
                organisation_id,
                name,
                is_active,
                created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                category_id,
                req["organisation_id"],
                req["requested_name"],
                1,
                now_iso()
            )
        )

        conn.execute(
            """
            UPDATE category_requests
            SET status = ?, updated_at = ?
            WHERE category_request_id = ?
            """,
            ("RESOLVED", now_iso(), category_request_id)
        )

        conn.execute(
            """
            UPDATE pending_approval_entries
            SET status = ?, updated_at = ?
            WHERE source_record_id = ?
              AND entry_type = 'CategoryRequest'
              AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
            """,
            ("RESOLVED", now_iso(), category_request_id)
        )

        audit_event(
            conn,
            entity_type="Category",
            entity_id=category_id,
            action="CREATE",
            summary=f"Created category from request: {req['requested_name']}",
            organisation_id=req["organisation_id"]
        )

        audit_event(
            conn,
            entity_type="CategoryRequest",
            entity_id=category_request_id,
            action="APPROVE",
            summary=f"Approved category request: {req['requested_name']}",
            organisation_id=req["organisation_id"]
        )

        pending_rows = conn.execute(
            """
            SELECT pending_entry_id
            FROM pending_approval_entries
            WHERE source_record_id = ?
              AND entry_type = 'CategoryRequest'
            """,
            (category_request_id,)
        ).fetchall()

        for row in pending_rows:
            audit_event(
                conn,
                entity_type="PendingApprovalEntry",
                entity_id=row["pending_entry_id"],
                action="APPROVE",
                summary="Pending approval entry approved and resolved for category request",
                organisation_id=req["organisation_id"]
            )

        conn.commit()
        conn.close()

        return jsonify({
            "category_request_id": category_request_id,
            "category_id": category_id,
            "requested_name": req["requested_name"],
            "status": "RESOLVED",
            "category_status": "CREATED"
        }), 200


    @app.post("/resource-requests")
    def create_resource_request():
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        category_id = body.get("category_id")
        brand_id = body.get("brand_id")
        requested_name = (body.get("requested_name") or "").strip()
        resource_type = (body.get("resource_type") or "pallet").strip()
        unit_type = (body.get("unit_type") or "each").strip()
        note = (body.get("note") or "").strip() or None
        submitted_by_display_name = (body.get("submitted_by_display_name") or "Unknown User").strip()

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400
        if not category_id:
            return jsonify({"error": "category_id is required"}), 400
        if not requested_name:
            return jsonify({"error": "requested_name is required"}), 400

        conn = get_conn()
        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        category = conn.execute(
            "SELECT * FROM categories WHERE category_id = ? AND organisation_id = ?",
            (category_id, organisation_id)
        ).fetchone()
        if not category:
            conn.close()
            return jsonify({"error": "Category not found"}), 404

        brand = None
        if brand_id:
            brand = conn.execute(
                "SELECT * FROM brands WHERE brand_id = ? AND organisation_id = ?",
                (brand_id, organisation_id)
            ).fetchone()
            if not brand:
                conn.close()
                return jsonify({"error": "Brand not found"}), 404

        existing_resource = conn.execute(
            """
            SELECT resource_id, name
            FROM resources
            WHERE organisation_id = ?
              AND category_id = ?
              AND COALESCE(brand_id, '') = COALESCE(?, '')
              AND LOWER(TRIM(name)) = LOWER(TRIM(?))
              AND resource_type = ?
              AND unit_type = ?
            LIMIT 1
            """,
            (organisation_id, category_id, brand_id, requested_name, resource_type, unit_type)
        ).fetchone()

        if existing_resource:
            conn.close()
            return jsonify({
                "error": "Resource already exists",
                "resource_id": existing_resource["resource_id"],
                "name": existing_resource["name"]
            }), 409

        existing_request = conn.execute(
            """
            SELECT resource_request_id, status, requested_name
            FROM resource_requests
            WHERE organisation_id = ?
              AND category_id = ?
              AND COALESCE(brand_id, '') = COALESCE(?, '')
              AND LOWER(TRIM(requested_name)) = LOWER(TRIM(?))
              AND resource_type = ?
              AND unit_type = ?
              AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
            LIMIT 1
            """,
            (organisation_id, category_id, brand_id, requested_name, resource_type, unit_type)
        ).fetchone()

        if existing_request:
            conn.close()
            return jsonify({
                "error": "Duplicate open resource request already exists",
                "resource_request_id": existing_request["resource_request_id"],
                "status": existing_request["status"],
                "requested_name": existing_request["requested_name"]
            }), 409

        resource_request_id = make_id("rreq")

        conn.execute(
            """
            INSERT INTO resource_requests (
                resource_request_id,
                organisation_id,
                category_id,
                brand_id,
                requested_name,
                resource_type,
                unit_type,
                note,
                submitted_by_display_name,
                status,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resource_request_id,
                organisation_id,
                category_id,
                brand_id,
                requested_name,
                resource_type,
                unit_type,
                note,
                submitted_by_display_name,
                "PENDING_APPROVAL",
                now_iso(),
                now_iso()
            )
        )

        audit_event(
            conn,
            entity_type="ResourceRequest",
            entity_id=resource_request_id,
            action="CREATE",
            summary=f"Created resource request: {requested_name}",
            organisation_id=organisation_id
        )

        pending_entry_id = create_pending_entry(
            conn=conn,
            organisation_id=organisation_id,
            entry_type="ResourceRequest",
            source_record_id=resource_request_id,
            source_module="ResourceRequests",
            submitted_by_display_name=submitted_by_display_name,
            related_entity_type="ResourceModule",
            related_entity_id="resource-module",
            related_entity_name="Resource Module",
            reason_code="MISSING_DROPDOWN_OPTION",
            reason_text="User requested a new resource because the required dropdown option was not available.",
            direct_action_type="GoToResourceModule",
            direct_action_target_id="resource-module",
            direct_action_label="Review / Create Resource",
            can_approve_now=True,
            can_reject_now=True,
            resource_id=None,
            resource_name=requested_name,
            status="PENDING_APPROVAL"
        )

        conn.commit()
        conn.close()

        return jsonify({
            "resource_request_id": resource_request_id,
            "status": "PENDING_APPROVAL",
            "pending_entry_id": pending_entry_id,
            "category_id": category_id,
            "category_name": category["name"],
            "brand_id": brand_id,
            "brand_name": brand["name"] if brand else None,
            "requested_name": requested_name,
            "resource_type": resource_type,
            "unit_type": unit_type,
            "note": note,
            "submitted_by_display_name": submitted_by_display_name,
            "message": "Your request has been saved and sent to your Org Admin for approval. You can continue with your entry."
        }), 201

    @app.get("/resource-requests")
    def list_resource_requests():
        organisation_id = request.args.get("organisation_id")
        status = request.args.get("status")

        conn = get_conn()

        sql = """
            SELECT
                rr.resource_request_id,
                rr.organisation_id,
                o.name AS organisation_name,
                rr.category_id,
                c.name AS category_name,
                rr.brand_id,
                b.name AS brand_name,
                rr.requested_name,
                rr.resource_type,
                rr.unit_type,
                rr.note,
                rr.submitted_by_display_name,
                rr.status,
                rr.rejection_reason_code,
                rr.rejection_reason_text,
                rr.created_at,
                rr.updated_at
            FROM resource_requests rr
            LEFT JOIN organisations o ON o.organisation_id = rr.organisation_id
            LEFT JOIN categories c ON c.category_id = rr.category_id
            LEFT JOIN brands b ON b.brand_id = rr.brand_id
            WHERE 1=1
        """
        params = []

        if organisation_id:
            sql += " AND rr.organisation_id = ?"
            params.append(organisation_id)

        if status:
            sql += " AND rr.status = ?"
            params.append(status)

        sql += " ORDER BY rr.created_at DESC"

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        return jsonify({
            "count": len(rows),
            "items": [dict(r) for r in rows]
        }), 200

    @app.get("/resource-requests/<resource_request_id>")
    def get_resource_request(resource_request_id):
        conn = get_conn()

        row = conn.execute(
            """
            SELECT
                rr.resource_request_id,
                rr.organisation_id,
                o.name AS organisation_name,
                rr.category_id,
                c.name AS category_name,
                rr.brand_id,
                b.name AS brand_name,
                rr.requested_name,
                rr.resource_type,
                rr.unit_type,
                rr.note,
                rr.submitted_by_display_name,
                rr.status,
                rr.rejection_reason_code,
                rr.rejection_reason_text,
                rr.created_at,
                rr.updated_at
            FROM resource_requests rr
            LEFT JOIN organisations o ON o.organisation_id = rr.organisation_id
            LEFT JOIN categories c ON c.category_id = rr.category_id
            LEFT JOIN brands b ON b.brand_id = rr.brand_id
            WHERE rr.resource_request_id = ?
            """,
            (resource_request_id,)
        ).fetchone()

        conn.close()

        if not row:
            return jsonify({"error": "Resource request not found"}), 404

        return jsonify(dict(row)), 200

    @app.post("/resource-requests/<resource_request_id>/approve")
    def approve_resource_request(resource_request_id):
        conn = get_conn()

        req = conn.execute(
            """
            SELECT *
            FROM resource_requests
            WHERE resource_request_id = ?
            """,
            (resource_request_id,)
        ).fetchone()

        if not req:
            conn.close()
            return jsonify({"error": "Resource request not found"}), 404

        if req["status"] != "PENDING_APPROVAL":
            conn.close()
            return jsonify({"error": "Resource request is not pending approval"}), 400

        resource_id = make_id("res")

        conn.execute(
            """
            INSERT INTO resources (
                resource_id,
                organisation_id,
                category_id,
                brand_id,
                name,
                resource_type,
                unit_type,
                is_active,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resource_id,
                req["organisation_id"],
                req["category_id"],
                req["brand_id"],
                req["requested_name"],
                req["resource_type"],
                req["unit_type"],
                1,
                now_iso()
            )
        )

        conn.execute(
            """
            UPDATE resource_requests
            SET status = ?, updated_at = ?
            WHERE resource_request_id = ?
            """,
            ("RESOLVED", now_iso(), resource_request_id)
        )

        conn.execute(
            """
            UPDATE pending_approval_entries
            SET status = ?, updated_at = ?
            WHERE source_record_id = ?
              AND entry_type = 'ResourceRequest'
              AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX', 'READY_TO_APPROVE')
            """,
            ("RESOLVED", now_iso(), resource_request_id)
        )

        audit_event(
            conn,
            entity_type="Resource",
            entity_id=resource_id,
            action="CREATE",
            summary=f"Created resource from request: {req['requested_name']}",
            organisation_id=req["organisation_id"]
        )

        audit_event(
            conn,
            entity_type="ResourceRequest",
            entity_id=resource_request_id,
            action="APPROVE",
            summary=f"Approved resource request: {req['requested_name']}",
            organisation_id=req["organisation_id"]
        )

        pending_rows = conn.execute(
            """
            SELECT pending_entry_id
            FROM pending_approval_entries
            WHERE source_record_id = ?
              AND entry_type = 'ResourceRequest'
            """,
            (resource_request_id,)
        ).fetchall()

        for row in pending_rows:
            audit_event(
                conn,
                entity_type="PendingApprovalEntry",
                entity_id=row["pending_entry_id"],
                action="APPROVE",
                summary="Pending approval entry approved and resolved for resource request",
                organisation_id=req["organisation_id"]
            )

        conn.commit()
        conn.close()

        return jsonify({
            "resource_request_id": resource_request_id,
            "resource_id": resource_id,
            "requested_name": req["requested_name"],
            "status": "RESOLVED",
            "resource_status": "CREATED"
        }), 200

    @app.get("/resources")
    def list_resources():
        organisation_id = request.args.get("organisation_id")
        resource_type = request.args.get("resource_type")
        is_active = request.args.get("is_active")
        category_id = request.args.get("category_id")
        brand_id = request.args.get("brand_id")

        conn = get_conn()
        ensure_resource_cleanup_columns(conn)

        sql = """
            SELECT
                r.resource_id,
                r.organisation_id,
                o.name AS organisation_name,
                r.category_id,
                c.name AS category_name,
                r.brand_id,
                b.name AS brand_name,
                r.name,
                r.resource_type,
                r.unit_type,
                r.is_active,
                r.merged_into_resource_id,
                r.inactive_reason_code,
                r.inactive_reason_text,
                r.created_at,
                r.updated_at
            FROM resources r
            LEFT JOIN organisations o ON o.organisation_id = r.organisation_id
            LEFT JOIN categories c ON c.category_id = r.category_id
            LEFT JOIN brands b ON b.brand_id = r.brand_id
            WHERE 1=1
        """
        params = []

        if organisation_id:
            sql += " AND r.organisation_id = ?"
            params.append(organisation_id)

        if resource_type:
            sql += " AND r.resource_type = ?"
            params.append(resource_type)

        if category_id:
            sql += " AND r.category_id = ?"
            params.append(category_id)

        if brand_id:
            sql += " AND r.brand_id = ?"
            params.append(brand_id)

        if is_active == "true":
            sql += " AND r.is_active = 1"
        elif is_active == "false":
            sql += " AND r.is_active = 0"

        sql += " ORDER BY r.name"

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        items = []
        for row in rows:
            d = dict(row)
            d["is_active"] = bool(d["is_active"])
            items.append(d)

        return jsonify({
            "count": len(items),
            "items": items
        }), 200

    @app.get("/resources/<resource_id>")
    def get_resource_profile(resource_id):
        conn = get_conn()
        ensure_resource_cleanup_columns(conn)

        row = conn.execute(
            """
            SELECT
                r.resource_id,
                r.organisation_id,
                o.name AS organisation_name,
                r.category_id,
                c.name AS category_name,
                r.brand_id,
                b.name AS brand_name,
                r.name,
                r.resource_type,
                r.unit_type,
                r.is_active,
                r.merged_into_resource_id,
                r.inactive_reason_code,
                r.inactive_reason_text,
                r.created_at,
                r.updated_at
            FROM resources r
            LEFT JOIN organisations o ON o.organisation_id = r.organisation_id
            LEFT JOIN categories c ON c.category_id = r.category_id
            LEFT JOIN brands b ON b.brand_id = r.brand_id
            WHERE r.resource_id = ?
            """,
            (resource_id,)
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Resource not found"}), 404

        stock_rows = conn.execute(
            """
            SELECT
                bp.depot_id,
                d.name AS depot_name,
                bp.current_quantity,
                bp.updated_at
            FROM balance_projection bp
            LEFT JOIN depots d ON d.depot_id = bp.depot_id
            WHERE bp.resource_id = ?
            ORDER BY d.name
            """,
            (resource_id,)
        ).fetchall()

        conn.close()

        d = dict(row)
        d["is_active"] = bool(d["is_active"])
        d["stock_count"] = len(stock_rows)
        d["stock_items"] = [dict(r) for r in stock_rows]

        return jsonify(d), 200

    @app.post("/resources")
    def create_resource():
        _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
        body = request.get_json(silent=True) or {}
        # Enforce org isolation: non-global-admins always write to their own org
        if g.current_user.get("role") not in _GLOBAL_ADMIN_ROLES:
            organisation_id = g.current_user.get("user_org_id")
        else:
            organisation_id = body.get("organisation_id")
        category_id = body.get("category_id")
        brand_id = body.get("brand_id")
        name = (body.get("name") or "").strip()[:255]
        resource_type = (body.get("resource_type") or "pallet").strip()[:100]
        unit_type = (body.get("unit_type") or "each").strip()[:100]

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400
        if not category_id:
            return jsonify({"error": "category_id is required"}), 400
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

        category = conn.execute(
            "SELECT * FROM categories WHERE category_id = ? AND organisation_id = ?",
            (category_id, organisation_id)
        ).fetchone()
        if not category:
            conn.close()
            return jsonify({"error": "Category not found"}), 404

        brand = None
        if brand_id:
            brand = conn.execute(
                "SELECT * FROM brands WHERE brand_id = ? AND organisation_id = ?",
                (brand_id, organisation_id)
            ).fetchone()
            if not brand:
                conn.close()
                return jsonify({"error": "Brand not found"}), 404

        existing_resource = conn.execute(
            """
            SELECT resource_id, name
            FROM resources
            WHERE organisation_id = ?
              AND category_id = ?
              AND COALESCE(brand_id, '') = COALESCE(?, '')
              AND LOWER(TRIM(name)) = LOWER(TRIM(?))
              AND resource_type = ?
              AND unit_type = ?
            LIMIT 1
            """,
            (organisation_id, category_id, brand_id, name, resource_type, unit_type)
        ).fetchone()

        if existing_resource:
            conn.close()
            return jsonify({
                "error": "Resource already exists",
                "resource_id": existing_resource["resource_id"],
                "name": existing_resource["name"]
            }), 409

        resource_id = make_id("res")

        conn.execute(
            """
            INSERT INTO resources (
                resource_id,
                organisation_id,
                category_id,
                brand_id,
                name,
                resource_type,
                unit_type,
                is_active,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resource_id,
                organisation_id,
                category_id,
                brand_id,
                name,
                resource_type,
                unit_type,
                1,
                now_iso()
            )
        )

        audit_event(
            conn,
            entity_type="Resource",
            entity_id=resource_id,
            action="CREATE",
            summary=f"Created resource: {name}",
            organisation_id=organisation_id
        )

        conn.commit()
        conn.close()

        return jsonify({
            "resource_id": resource_id,
            "organisation_id": organisation_id,
            "category_id": category_id,
            "category_name": category["name"],
            "brand_id": brand_id,
            "brand_name": brand["name"] if brand else None,
            "name": name,
            "resource_type": resource_type,
            "unit_type": unit_type,
            "is_active": True
        }), 201


    @app.patch("/resources/<resource_id>")
    def update_resource(resource_id):
        body = request.get_json(silent=True) or {}

        conn = get_conn()
        ensure_resource_cleanup_columns(conn)

        resource = conn.execute(
            "SELECT * FROM resources WHERE resource_id = ?", (resource_id,)
        ).fetchone()

        if not resource:
            conn.close()
            return jsonify({"error": "Resource not found"}), 404

        new_name = (body.get("name") or "").strip() or resource["name"]
        new_resource_type = (body.get("resource_type") or "").strip() or resource["resource_type"]
        new_unit_type = (body.get("unit_type") or "").strip() or resource["unit_type"]
        new_category_id = body.get("category_id") or resource["category_id"]
        new_brand_id = body.get("brand_id") if "brand_id" in body else resource["brand_id"]

        if new_category_id != resource["category_id"]:
            category = conn.execute(
                "SELECT * FROM categories WHERE category_id = ? AND organisation_id = ?",
                (new_category_id, resource["organisation_id"]),
            ).fetchone()
            if not category:
                conn.close()
                return jsonify({"error": "Category not found"}), 404

        if new_brand_id and new_brand_id != resource["brand_id"]:
            brand = conn.execute(
                "SELECT * FROM brands WHERE brand_id = ? AND organisation_id = ?",
                (new_brand_id, resource["organisation_id"]),
            ).fetchone()
            if not brand:
                conn.close()
                return jsonify({"error": "Brand not found"}), 404

        changes = []
        if new_name != resource["name"]:
            changes.append(f"name '{resource['name']}' → '{new_name}'")
        if new_resource_type != resource["resource_type"]:
            changes.append(f"resource_type '{resource['resource_type']}' → '{new_resource_type}'")
        if new_unit_type != resource["unit_type"]:
            changes.append(f"unit_type '{resource['unit_type']}' → '{new_unit_type}'")
        if new_category_id != resource["category_id"]:
            changes.append(f"category_id → '{new_category_id}'")
        if new_brand_id != resource["brand_id"]:
            changes.append(f"brand_id → '{new_brand_id}'")

        if not changes:
            conn.close()
            return jsonify({"message": "No changes made", "resource_id": resource_id}), 200

        ts = now_iso()
        conn.execute(
            """UPDATE resources
               SET name = ?, resource_type = ?, unit_type = ?, category_id = ?, brand_id = ?, updated_at = ?
               WHERE resource_id = ?""",
            (new_name, new_resource_type, new_unit_type, new_category_id, new_brand_id, ts, resource_id),
        )

        audit_event(
            conn,
            entity_type="Resource",
            entity_id=resource_id,
            action="UPDATE",
            summary=f"Resource updated: {'; '.join(changes)}.",
            organisation_id=resource["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({
            "resource_id": resource_id,
            "organisation_id": resource["organisation_id"],
            "name": new_name,
            "resource_type": new_resource_type,
            "unit_type": new_unit_type,
            "category_id": new_category_id,
            "brand_id": new_brand_id,
            "updated_at": ts,
        }), 200


    @app.post("/resources/<resource_id>/classify")
    def classify_resource(resource_id):
        body = request.get_json(silent=True) or {}
        category_id = body.get("category_id")
        brand_id = body.get("brand_id")

        if not category_id:
            return jsonify({"error": "category_id is required"}), 400

        conn = get_conn()
        ensure_resource_cleanup_columns(conn)

        resource = conn.execute(
            "SELECT * FROM resources WHERE resource_id = ?",
            (resource_id,)
        ).fetchone()

        if not resource:
            conn.close()
            return jsonify({"error": "Resource not found"}), 404

        category = conn.execute(
            "SELECT * FROM categories WHERE category_id = ? AND organisation_id = ?",
            (category_id, resource["organisation_id"])
        ).fetchone()

        if not category:
            conn.close()
            return jsonify({"error": "Category not found"}), 404

        brand = None
        if brand_id:
            brand = conn.execute(
                "SELECT * FROM brands WHERE brand_id = ? AND organisation_id = ?",
                (brand_id, resource["organisation_id"])
            ).fetchone()
            if not brand:
                conn.close()
                return jsonify({"error": "Brand not found"}), 404

        conn.execute(
            """
            UPDATE resources
            SET category_id = ?, brand_id = ?, updated_at = ?
            WHERE resource_id = ?
            """,
            (category_id, brand_id, now_iso(), resource_id)
        )

        audit_event(
            conn,
            entity_type="Resource",
            entity_id=resource_id,
            action="CLASSIFY",
            summary=f"Updated resource classification for {resource['name']}",
            organisation_id=resource["organisation_id"]
        )

        conn.commit()
        conn.close()

        return jsonify({
            "resource_id": resource_id,
            "status": "CLASSIFIED",
            "category_id": category_id,
            "category_name": category["name"],
            "brand_id": brand_id,
            "brand_name": brand["name"] if brand else None
        }), 200


    @app.post("/resources/<resource_id>/deactivate")
    def deactivate_resource(resource_id):
        body = request.get_json(silent=True) or {}
        merged_into_resource_id = body.get("merged_into_resource_id")
        inactive_reason_code = (body.get("inactive_reason_code") or "INACTIVE_BY_ADMIN").strip()
        inactive_reason_text = (body.get("inactive_reason_text") or "Marked inactive by Org Admin").strip()

        conn = get_conn()
        ensure_resource_cleanup_columns(conn)

        resource = conn.execute(
            "SELECT * FROM resources WHERE resource_id = ?",
            (resource_id,)
        ).fetchone()

        if not resource:
            conn.close()
            return jsonify({"error": "Resource not found"}), 404

        if merged_into_resource_id == resource_id:
            conn.close()
            return jsonify({"error": "Resource cannot merge into itself"}), 400

        if merged_into_resource_id:
            merge_target = conn.execute(
                """
                SELECT *
                FROM resources
                WHERE resource_id = ? AND organisation_id = ?
                """,
                (merged_into_resource_id, resource["organisation_id"])
            ).fetchone()

            if not merge_target:
                conn.close()
                return jsonify({"error": "Merge target resource not found"}), 404

        conn.execute(
            """
            UPDATE resources
            SET is_active = 0,
                merged_into_resource_id = ?,
                inactive_reason_code = ?,
                inactive_reason_text = ?,
                updated_at = ?
            WHERE resource_id = ?
            """,
            (
                merged_into_resource_id,
                inactive_reason_code,
                inactive_reason_text,
                now_iso(),
                resource_id
            )
        )

        audit_event(
            conn,
            entity_type="Resource",
            entity_id=resource_id,
            action="DEACTIVATE",
            summary=f"Deactivated resource: {resource['name']} ({inactive_reason_code})",
            organisation_id=resource["organisation_id"]
        )

        conn.commit()
        conn.close()

        return jsonify({
            "resource_id": resource_id,
            "status": "INACTIVE",
            "merged_into_resource_id": merged_into_resource_id,
            "inactive_reason_code": inactive_reason_code,
            "inactive_reason_text": inactive_reason_text
        }), 200


    @app.post("/opening-balances")
    def create_opening_balance():
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        depot_id = body.get("depot_id")
        resource_id = body.get("resource_id")
        quantity = body.get("quantity")

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400
        if not depot_id:
            return jsonify({"error": "depot_id is required"}), 400
        if not resource_id:
            return jsonify({"error": "resource_id is required"}), 400
        if not isinstance(quantity, int) or quantity < 0:
            return jsonify({"error": "quantity must be an integer greater than or equal to zero"}), 400

        conn = get_conn()
        ensure_transaction_numbering_tables(conn)

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

        existing_ob = conn.execute(
            """
            SELECT transaction_id FROM transactions
            WHERE depot_id = ? AND resource_id = ? AND transaction_type = 'OpeningBalance'
            LIMIT 1
            """,
            (depot_id, resource_id)
        ).fetchone()
        if existing_ob:
            conn.close()
            return jsonify({
                "error": "OPENING_BALANCE_ALREADY_SET",
                "message": (
                    f"An opening balance has already been entered for "
                    f"'{resource['name']}' at this depot. "
                    "Use a regular transaction to adjust the balance."
                ),
                "resource_id": resource_id,
                "resource_name": resource["name"],
                "existing_transaction_id": existing_ob["transaction_id"],
            }), 400

        transaction_id = make_id("txn")
        ledger_entry_id = make_id("led")
        created_at = now_iso()
        ob_reference_number, ob_org_seq = generate_transaction_reference(conn, organisation_id)

        conn.execute(
            """
            INSERT INTO transactions (
                transaction_id,
                organisation_id,
                depot_id,
                transaction_type,
                resource_id,
                quantity,
                direction,
                status,
                approval_reason_code,
                approval_reason_text,
                created_at,
                posted_at,
                reference_number,
                org_sequence_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transaction_id,
                organisation_id,
                depot_id,
                "OpeningBalance",
                resource_id,
                quantity,
                "IN",
                "POSTED",
                None,
                None,
                created_at,
                created_at,
                ob_reference_number,
                ob_org_seq
            )
        )

        conn.execute(
            """
            INSERT INTO ledger_entries (
                ledger_entry_id,
                transaction_id,
                organisation_id,
                depot_id,
                resource_id,
                quantity_delta,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ledger_entry_id,
                transaction_id,
                organisation_id,
                depot_id,
                resource_id,
                quantity,
                created_at
            )
        )

        conn.execute(
            """
            INSERT INTO balance_projection (
                balance_projection_id,
                organisation_id,
                depot_id,
                resource_id,
                current_quantity,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(organisation_id, depot_id, resource_id)
            DO UPDATE SET current_quantity = excluded.current_quantity, updated_at = excluded.updated_at
            """,
            (
                make_id("bal"),
                organisation_id,
                depot_id,
                resource_id,
                quantity,
                created_at
            )
        )

        conn.execute(
            "UPDATE depots SET opening_balance_used = 1 WHERE depot_id = ?",
            (depot_id,)
        )

        update_pending_entries_ready_for_opening_balance(conn, organisation_id, depot_id)

        audit_event(
            conn,
            entity_type="Depot",
            entity_id=depot_id,
            action="OPENING_BALANCE_SET",
            summary=f"Opening balance set for depot with quantity {quantity}",
            organisation_id=organisation_id
        )

        conn.commit()
        conn.close()

        return jsonify({
            "transaction_id": transaction_id,
            "status": "POSTED",
            "opening_balance_used": True
        }), 201
