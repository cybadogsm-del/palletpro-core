"""
Resource sample photos module.

Maintains a two-tier library of reference photos for resources:

  GLOBAL — managed by Global Admin, keyed by resource_type (e.g. "euro_pallet").
            Visible to all orgs that have resources of that type.

  ORG    — managed by Org Admin, linked to a specific resource_id within
            that org's catalogue.

The combined GET /resources/<resource_id>/sample-photos endpoint merges both
tiers and is the feed used by the AI counting endpoint (Item 12).

Photos are stored as BLOBs (max 5 MB). PNG, JPEG, and WebP are accepted.
Upload is via base64-encoded JSON body (same pattern as org_branding.py).

Routes
------
  POST   /global-admin/resource-sample-photos          upload global photo
  GET    /global-admin/resource-sample-photos          list global photos
  DELETE /global-admin/resource-sample-photos/<id>     delete global photo
  PATCH  /global-admin/resource-sample-photos/<id>     update caption/order/active

  POST   /organisations/<org_id>/resources/<res_id>/sample-photos   org upload
  GET    /organisations/<org_id>/resources/<res_id>/sample-photos   list org photos
  DELETE /resource-sample-photos/<id>                  delete (own org or global admin)
  PATCH  /resource-sample-photos/<id>                  update caption/order/active

  GET    /resources/<resource_id>/sample-photos        COMBINED global + org
  GET    /resource-sample-photos/<id>/image            raw image bytes
"""

import base64
import re

from flask import g, jsonify, make_response, request

from db import get_conn, make_id, now_iso

_MAX_PHOTO_BYTES = 5 * 1024 * 1024  # 5 MB
_ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp"}
_DATA_URL_RE = re.compile(
    r"^data:(image/(?:png|jpeg|webp));base64,(.+)$", re.DOTALL
)

_GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
_ORG_ADMIN_ROLES = {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}


def ensure_resource_sample_photo_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS resource_sample_photos (
        photo_id                TEXT PRIMARY KEY,
        scope                   TEXT NOT NULL,          -- 'GLOBAL' or 'ORG'
        organisation_id         TEXT,                   -- NULL for GLOBAL
        resource_id             TEXT,                   -- NULL for GLOBAL
        resource_type_label     TEXT,                   -- NULL for ORG
        photo_data              BLOB NOT NULL,
        photo_content_type      TEXT NOT NULL,
        photo_filename          TEXT,
        photo_size_bytes        INTEGER,
        caption                 TEXT,
        is_active               INTEGER NOT NULL DEFAULT 1,
        sort_order              INTEGER NOT NULL DEFAULT 0,
        uploaded_by_display_name TEXT,
        created_at              TEXT NOT NULL
    )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rsp_scope_type "
        "ON resource_sample_photos (scope, resource_type_label)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rsp_resource "
        "ON resource_sample_photos (resource_id)"
    )
    conn.commit()


def _decode_photo(body):
    """
    Parse and validate a base64 photo from a JSON request body.
    Returns (bytes, content_type, filename) or raises ValueError with a message.
    """
    raw = (body.get("photo_data") or "").strip()
    if not raw:
        raise ValueError("photo_data is required")

    m = _DATA_URL_RE.match(raw)
    if not m:
        raise ValueError(
            "photo_data must be a data URL: data:image/png;base64,<data>"
        )

    content_type = m.group(1)
    try:
        photo_bytes = base64.b64decode(m.group(2))
    except Exception:
        raise ValueError("photo_data contains invalid base64")

    if len(photo_bytes) > _MAX_PHOTO_BYTES:
        raise ValueError(
            f"Photo too large ({len(photo_bytes)} bytes). Maximum is {_MAX_PHOTO_BYTES} bytes (5 MB)."
        )

    filename = (body.get("filename") or "").strip() or None
    return photo_bytes, content_type, filename


def _photo_row_to_dict(row, include_image=False):
    d = {
        "photo_id": row["photo_id"],
        "scope": row["scope"],
        "organisation_id": row["organisation_id"],
        "resource_id": row["resource_id"],
        "resource_type_label": row["resource_type_label"],
        "photo_content_type": row["photo_content_type"],
        "photo_filename": row["photo_filename"],
        "photo_size_bytes": row["photo_size_bytes"],
        "caption": row["caption"],
        "is_active": bool(row["is_active"]),
        "sort_order": row["sort_order"],
        "uploaded_by_display_name": row["uploaded_by_display_name"],
        "created_at": row["created_at"],
        "image_url": f"/resource-sample-photos/{row['photo_id']}/image",
    }
    if include_image:
        d["photo_data"] = (
            "data:" + row["photo_content_type"] + ";base64,"
            + base64.b64encode(bytes(row["photo_data"])).decode()
        )
    return d


def register_resource_sample_photo_routes(app):

    # ------------------------------------------------------------------ #
    # Global Admin — manage global library                               #
    # ------------------------------------------------------------------ #

    @app.post("/global-admin/resource-sample-photos")
    def upload_global_sample_photo():
        current_user = g.current_user
        if current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            return jsonify({"error": "Global admin access required"}), 403

        body = request.get_json(silent=True) or {}
        resource_type_label = (body.get("resource_type_label") or "").strip()
        if not resource_type_label:
            return jsonify({"error": "resource_type_label is required"}), 400

        try:
            photo_bytes, content_type, filename = _decode_photo(body)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        caption = (body.get("caption") or "").strip() or None
        sort_order = int(body.get("sort_order") or 0)
        uploader = current_user.get("display_name") or current_user.get("email") or "Global Admin"

        conn = get_conn()
        ensure_resource_sample_photo_tables(conn)

        photo_id = make_id("rsp")
        conn.execute(
            """INSERT INTO resource_sample_photos
               (photo_id, scope, organisation_id, resource_id,
                resource_type_label, photo_data, photo_content_type,
                photo_filename, photo_size_bytes, caption,
                is_active, sort_order, uploaded_by_display_name, created_at)
               VALUES (?, 'GLOBAL', NULL, NULL, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (photo_id, resource_type_label, photo_bytes, content_type,
             filename, len(photo_bytes), caption, sort_order, uploader, now_iso()),
        )
        conn.commit()
        conn.close()

        return jsonify({
            "photo_id": photo_id,
            "scope": "GLOBAL",
            "resource_type_label": resource_type_label,
            "photo_content_type": content_type,
            "photo_size_bytes": len(photo_bytes),
            "caption": caption,
            "sort_order": sort_order,
            "image_url": f"/resource-sample-photos/{photo_id}/image",
        }), 201


    @app.get("/global-admin/resource-sample-photos")
    def list_global_sample_photos():
        current_user = g.current_user
        if current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            return jsonify({"error": "Global admin access required"}), 403

        resource_type_label = request.args.get("resource_type_label", "").strip() or None
        active_only = request.args.get("active_only", "").lower() in ("1", "true", "yes")

        conn = get_conn()
        ensure_resource_sample_photo_tables(conn)

        sql = "SELECT * FROM resource_sample_photos WHERE scope = 'GLOBAL'"
        params = []
        if resource_type_label:
            sql += " AND resource_type_label = ?"
            params.append(resource_type_label)
        if active_only:
            sql += " AND is_active = 1"
        sql += " ORDER BY resource_type_label ASC, sort_order ASC, created_at ASC"

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        return jsonify({
            "count": len(rows),
            "photos": [_photo_row_to_dict(r) for r in rows],
        }), 200


    @app.delete("/global-admin/resource-sample-photos/<photo_id>")
    def delete_global_sample_photo(photo_id):
        current_user = g.current_user
        if current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            return jsonify({"error": "Global admin access required"}), 403

        conn = get_conn()
        ensure_resource_sample_photo_tables(conn)

        row = conn.execute(
            "SELECT photo_id, scope FROM resource_sample_photos WHERE photo_id = ?",
            (photo_id,),
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Photo not found"}), 404

        if row["scope"] != "GLOBAL":
            conn.close()
            return jsonify({"error": "Use the org delete endpoint for ORG-scoped photos"}), 400

        conn.execute(
            "DELETE FROM resource_sample_photos WHERE photo_id = ?", (photo_id,)
        )
        conn.commit()
        conn.close()

        return jsonify({"photo_id": photo_id, "deleted": True}), 200


    @app.patch("/global-admin/resource-sample-photos/<photo_id>")
    def update_global_sample_photo(photo_id):
        current_user = g.current_user
        if current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            return jsonify({"error": "Global admin access required"}), 403

        conn = get_conn()
        ensure_resource_sample_photo_tables(conn)

        row = conn.execute(
            "SELECT * FROM resource_sample_photos WHERE photo_id = ?",
            (photo_id,),
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Photo not found"}), 404

        if row["scope"] != "GLOBAL":
            conn.close()
            return jsonify({"error": "Use the org update endpoint for ORG-scoped photos"}), 400

        body = request.get_json(silent=True) or {}
        updates = {}
        if "caption" in body:
            updates["caption"] = (body["caption"] or "").strip() or None
        if "sort_order" in body:
            updates["sort_order"] = int(body["sort_order"])
        if "is_active" in body:
            updates["is_active"] = 1 if body["is_active"] else 0

        if not updates:
            conn.close()
            return jsonify({"error": "Nothing to update"}), 400

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE resource_sample_photos SET {set_clause} WHERE photo_id = ?",
            list(updates.values()) + [photo_id],
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM resource_sample_photos WHERE photo_id = ?", (photo_id,)
        ).fetchone()
        conn.close()

        return jsonify(_photo_row_to_dict(row)), 200


    # ------------------------------------------------------------------ #
    # Org Admin — manage org-level photos                                #
    # ------------------------------------------------------------------ #

    @app.post("/organisations/<organisation_id>/resources/<resource_id>/sample-photos")
    def upload_org_sample_photo(organisation_id, resource_id):
        current_user = g.current_user
        if current_user["role"] not in _ORG_ADMIN_ROLES:
            return jsonify({"error": "Org admin access required"}), 403
        if (current_user["role"] not in _GLOBAL_ADMIN_ROLES
                and current_user.get("organisation_id") != organisation_id):
            return jsonify({"error": "Access denied"}), 403

        conn = get_conn()
        ensure_resource_sample_photo_tables(conn)

        resource = conn.execute(
            "SELECT resource_id, name, resource_type FROM resources "
            "WHERE resource_id = ? AND organisation_id = ?",
            (resource_id, organisation_id),
        ).fetchone()
        if not resource:
            conn.close()
            return jsonify({"error": "Resource not found"}), 404

        body = request.get_json(silent=True) or {}
        try:
            photo_bytes, content_type, filename = _decode_photo(body)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        caption = (body.get("caption") or "").strip() or None
        sort_order = int(body.get("sort_order") or 0)
        uploader = current_user.get("display_name") or current_user.get("email") or "Org Admin"

        photo_id = make_id("rsp")
        conn.execute(
            """INSERT INTO resource_sample_photos
               (photo_id, scope, organisation_id, resource_id,
                resource_type_label, photo_data, photo_content_type,
                photo_filename, photo_size_bytes, caption,
                is_active, sort_order, uploaded_by_display_name, created_at)
               VALUES (?, 'ORG', ?, ?, NULL, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (photo_id, organisation_id, resource_id,
             photo_bytes, content_type, filename, len(photo_bytes),
             caption, sort_order, uploader, now_iso()),
        )
        conn.commit()
        conn.close()

        return jsonify({
            "photo_id": photo_id,
            "scope": "ORG",
            "organisation_id": organisation_id,
            "resource_id": resource_id,
            "resource_name": resource["name"],
            "photo_content_type": content_type,
            "photo_size_bytes": len(photo_bytes),
            "caption": caption,
            "sort_order": sort_order,
            "image_url": f"/resource-sample-photos/{photo_id}/image",
        }), 201


    @app.get("/organisations/<organisation_id>/resources/<resource_id>/sample-photos")
    def list_org_sample_photos(organisation_id, resource_id):
        current_user = g.current_user
        if current_user["role"] not in _ORG_ADMIN_ROLES:
            return jsonify({"error": "Org admin access required"}), 403
        if (current_user["role"] not in _GLOBAL_ADMIN_ROLES
                and current_user.get("organisation_id") != organisation_id):
            return jsonify({"error": "Access denied"}), 403

        conn = get_conn()
        ensure_resource_sample_photo_tables(conn)

        rows = conn.execute(
            """SELECT * FROM resource_sample_photos
               WHERE scope = 'ORG' AND resource_id = ? AND organisation_id = ?
               ORDER BY sort_order ASC, created_at ASC""",
            (resource_id, organisation_id),
        ).fetchall()
        conn.close()

        return jsonify({
            "resource_id": resource_id,
            "organisation_id": organisation_id,
            "count": len(rows),
            "photos": [_photo_row_to_dict(r) for r in rows],
        }), 200


    @app.delete("/resource-sample-photos/<photo_id>")
    def delete_sample_photo(photo_id):
        current_user = g.current_user

        conn = get_conn()
        ensure_resource_sample_photo_tables(conn)

        row = conn.execute(
            "SELECT * FROM resource_sample_photos WHERE photo_id = ?",
            (photo_id,),
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Photo not found"}), 404

        if row["scope"] == "GLOBAL" and current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            conn.close()
            return jsonify({"error": "Global admin access required to delete global photos"}), 403

        if row["scope"] == "ORG":
            if current_user["role"] not in _GLOBAL_ADMIN_ROLES:
                if current_user["role"] not in _ORG_ADMIN_ROLES:
                    conn.close()
                    return jsonify({"error": "Org admin access required"}), 403
                if current_user.get("organisation_id") != row["organisation_id"]:
                    conn.close()
                    return jsonify({"error": "Access denied"}), 403

        conn.execute(
            "DELETE FROM resource_sample_photos WHERE photo_id = ?", (photo_id,)
        )
        conn.commit()
        conn.close()

        return jsonify({"photo_id": photo_id, "deleted": True}), 200


    @app.patch("/resource-sample-photos/<photo_id>")
    def update_sample_photo(photo_id):
        current_user = g.current_user

        conn = get_conn()
        ensure_resource_sample_photo_tables(conn)

        row = conn.execute(
            "SELECT * FROM resource_sample_photos WHERE photo_id = ?",
            (photo_id,),
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Photo not found"}), 404

        if row["scope"] == "GLOBAL" and current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            conn.close()
            return jsonify({"error": "Global admin access required to update global photos"}), 403

        if row["scope"] == "ORG":
            if current_user["role"] not in _GLOBAL_ADMIN_ROLES:
                if current_user["role"] not in _ORG_ADMIN_ROLES:
                    conn.close()
                    return jsonify({"error": "Org admin access required"}), 403
                if current_user.get("organisation_id") != row["organisation_id"]:
                    conn.close()
                    return jsonify({"error": "Access denied"}), 403

        body = request.get_json(silent=True) or {}
        updates = {}
        if "caption" in body:
            updates["caption"] = (body["caption"] or "").strip() or None
        if "sort_order" in body:
            updates["sort_order"] = int(body["sort_order"])
        if "is_active" in body:
            updates["is_active"] = 1 if body["is_active"] else 0

        if not updates:
            conn.close()
            return jsonify({"error": "Nothing to update"}), 400

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE resource_sample_photos SET {set_clause} WHERE photo_id = ?",
            list(updates.values()) + [photo_id],
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM resource_sample_photos WHERE photo_id = ?", (photo_id,)
        ).fetchone()
        conn.close()

        return jsonify(_photo_row_to_dict(row)), 200


    # ------------------------------------------------------------------ #
    # Combined feed — global + org for a specific resource               #
    # ------------------------------------------------------------------ #

    @app.get("/resources/<resource_id>/sample-photos")
    def get_combined_sample_photos(resource_id):
        """
        Returns global photos matching the resource's type PLUS org-specific
        photos for this resource. Sorted: GLOBAL first (by sort_order), then ORG.
        Only active photos are returned. Used by the AI counting endpoint.
        """
        current_user = g.current_user

        conn = get_conn()
        ensure_resource_sample_photo_tables(conn)

        resource = conn.execute(
            "SELECT resource_id, name, resource_type, organisation_id "
            "FROM resources WHERE resource_id = ?",
            (resource_id,),
        ).fetchone()

        if not resource:
            conn.close()
            return jsonify({"error": "Resource not found"}), 404

        if (current_user["role"] not in _GLOBAL_ADMIN_ROLES
                and current_user.get("organisation_id") != resource["organisation_id"]):
            conn.close()
            return jsonify({"error": "Access denied"}), 403

        global_photos = conn.execute(
            """SELECT * FROM resource_sample_photos
               WHERE scope = 'GLOBAL'
                 AND resource_type_label = ?
                 AND is_active = 1
               ORDER BY sort_order ASC, created_at ASC""",
            (resource["resource_type"],),
        ).fetchall()

        org_photos = conn.execute(
            """SELECT * FROM resource_sample_photos
               WHERE scope = 'ORG'
                 AND resource_id = ?
                 AND is_active = 1
               ORDER BY sort_order ASC, created_at ASC""",
            (resource_id,),
        ).fetchall()

        conn.close()

        return jsonify({
            "resource_id": resource_id,
            "resource_name": resource["name"],
            "resource_type": resource["resource_type"],
            "organisation_id": resource["organisation_id"],
            "global_count": len(global_photos),
            "org_count": len(org_photos),
            "photos": (
                [_photo_row_to_dict(r) for r in global_photos]
                + [_photo_row_to_dict(r) for r in org_photos]
            ),
        }), 200


    # ------------------------------------------------------------------ #
    # Raw image endpoint                                                  #
    # ------------------------------------------------------------------ #

    @app.get("/resource-sample-photos/<photo_id>/image")
    def get_sample_photo_image(photo_id):
        """Serve the raw image bytes — suitable for use in <img src=>."""
        current_user = g.current_user

        conn = get_conn()
        ensure_resource_sample_photo_tables(conn)

        row = conn.execute(
            "SELECT photo_data, photo_content_type, scope, organisation_id "
            "FROM resource_sample_photos WHERE photo_id = ?",
            (photo_id,),
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Photo not found"}), 404

        if row["scope"] == "ORG":
            if (current_user["role"] not in _GLOBAL_ADMIN_ROLES
                    and current_user.get("organisation_id") != row["organisation_id"]):
                conn.close()
                return jsonify({"error": "Access denied"}), 403

        data = bytes(row["photo_data"])
        content_type = row["photo_content_type"]
        conn.close()

        resp = make_response(data)
        resp.headers["Content-Type"] = content_type
        resp.headers["Cache-Control"] = "public, max-age=604800"  # 7 days
        return resp
