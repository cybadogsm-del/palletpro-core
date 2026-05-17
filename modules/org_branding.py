"""
Org branding module.

Each organisation can upload a logo and set a primary colour.
The logo is stored as raw bytes in SQLite (BLOB). When cloud storage
is wired up (item 8), the storage layer can be swapped here without
changing the API contract.

Upload accepts base64 — either a raw base64 string or a data URL
(data:image/png;base64,...). The GET /logo route returns the raw image
so the frontend can use it directly in an <img src="..."> tag.
"""

import base64
import re

from flask import g, jsonify, make_response, request

from audit import audit_event
from db import get_conn, make_id, now_iso

_MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2 MB
_ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_ORG_ADMIN_ROLES = {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}

_DATA_URL_RE = re.compile(
    r"^data:(image/(?:png|jpeg|webp|gif));base64,(.+)$", re.DOTALL
)


def ensure_org_branding_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS org_branding (
        branding_id             TEXT PRIMARY KEY,
        organisation_id         TEXT UNIQUE NOT NULL,
        logo_data               BLOB,
        logo_content_type       TEXT,
        logo_filename           TEXT,
        logo_size_bytes         INTEGER,
        primary_colour          TEXT,
        updated_by_display_name TEXT,
        created_at              TEXT NOT NULL,
        updated_at              TEXT NOT NULL
    )
    """)
    conn.commit()


def _decode_logo(raw):
    """
    Accept either a data URL (data:image/png;base64,...) or a plain
    base64 string with a separately supplied content_type.
    Returns (bytes, content_type) or raises ValueError.
    """
    m = _DATA_URL_RE.match(raw.strip())
    if m:
        content_type = m.group(1)
        b64_data = m.group(2)
    else:
        # Caller must supply content_type separately
        content_type = None
        b64_data = raw.strip()

    try:
        image_bytes = base64.b64decode(b64_data, validate=True)
    except Exception:
        raise ValueError("logo_base64 is not valid base64")

    return image_bytes, content_type


def register_org_branding_routes(app):

    @app.post("/organisations/<organisation_id>/branding/logo")
    def upload_org_logo(organisation_id):
        """
        Upload or replace the organisation logo.

        Body (JSON):
          logo_base64   — base64 string or data URL  (required)
          content_type  — e.g. "image/png"           (required if logo_base64 is not a data URL)
          filename      — original filename           (optional)
        """
        current_user = g.current_user
        if current_user["role"] not in _ORG_ADMIN_ROLES:
            return jsonify({"error": "Only Org Admin or above can manage org branding"}), 403

        body = request.get_json(silent=True) or {}
        logo_base64 = (body.get("logo_base64") or "").strip()
        supplied_type = (body.get("content_type") or "").strip().lower() or None
        filename = (body.get("filename") or "").strip() or None

        if not logo_base64:
            return jsonify({"error": "logo_base64 is required"}), 400

        try:
            image_bytes, detected_type = _decode_logo(logo_base64)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        content_type = detected_type or supplied_type
        if not content_type:
            return jsonify({"error": "content_type is required when logo_base64 is not a data URL"}), 400
        if content_type not in _ALLOWED_TYPES:
            return jsonify({"error": f"Unsupported image type. Use: {', '.join(sorted(_ALLOWED_TYPES))}"}), 400

        if len(image_bytes) > _MAX_LOGO_BYTES:
            return jsonify({"error": f"Logo exceeds maximum size of {_MAX_LOGO_BYTES // 1024 // 1024} MB"}), 400

        conn = get_conn()
        ensure_org_branding_tables(conn)

        org = conn.execute(
            "SELECT organisation_id, name FROM organisations WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        ts = now_iso()
        existing = conn.execute(
            "SELECT branding_id FROM org_branding WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE org_branding
                   SET logo_data = ?, logo_content_type = ?, logo_filename = ?,
                       logo_size_bytes = ?, updated_by_display_name = ?, updated_at = ?
                   WHERE organisation_id = ?""",
                (image_bytes, content_type, filename,
                 len(image_bytes), current_user["display_name"], ts,
                 organisation_id),
            )
            branding_id = existing["branding_id"]
            action = "LOGO_REPLACE"
        else:
            branding_id = make_id("brand")
            conn.execute(
                """INSERT INTO org_branding
                   (branding_id, organisation_id, logo_data, logo_content_type,
                    logo_filename, logo_size_bytes, updated_by_display_name,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (branding_id, organisation_id, image_bytes, content_type,
                 filename, len(image_bytes), current_user["display_name"],
                 ts, ts),
            )
            action = "LOGO_UPLOAD"

        audit_event(
            conn,
            entity_type="OrgBranding",
            entity_id=branding_id,
            action=action,
            summary=(
                f"{current_user['display_name']} uploaded logo for {org['name']} "
                f"({content_type}, {len(image_bytes):,} bytes)."
            ),
            organisation_id=organisation_id,
        )

        conn.commit()
        conn.close()

        return jsonify({
            "branding_id": branding_id,
            "organisation_id": organisation_id,
            "logo_content_type": content_type,
            "logo_filename": filename,
            "logo_size_bytes": len(image_bytes),
            "logo_url": f"/organisations/{organisation_id}/branding/logo",
            "updated_by": current_user["display_name"],
            "updated_at": ts,
            "message": "Logo uploaded successfully.",
        }), 200


    @app.get("/organisations/<organisation_id>/branding/logo")
    def get_org_logo(organisation_id):
        """
        Return the organisation logo as a raw image response.
        Use directly in <img src="..."> — no base64 needed on the client.
        """
        conn = get_conn()
        ensure_org_branding_tables(conn)

        row = conn.execute(
            "SELECT logo_data, logo_content_type FROM org_branding WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()
        conn.close()

        if not row or not row["logo_data"]:
            return jsonify({"error": "No logo found for this organisation"}), 404

        response = make_response(bytes(row["logo_data"]))
        response.headers["Content-Type"] = row["logo_content_type"] or "image/png"
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response


    @app.delete("/organisations/<organisation_id>/branding/logo")
    def delete_org_logo(organisation_id):
        """Remove the organisation logo. Primary colour is preserved."""
        current_user = g.current_user
        if current_user["role"] not in _ORG_ADMIN_ROLES:
            return jsonify({"error": "Only Org Admin or above can manage org branding"}), 403

        conn = get_conn()
        ensure_org_branding_tables(conn)

        row = conn.execute(
            "SELECT branding_id, logo_data FROM org_branding WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()

        if not row or not row["logo_data"]:
            conn.close()
            return jsonify({"error": "No logo found for this organisation"}), 404

        ts = now_iso()
        conn.execute(
            """UPDATE org_branding
               SET logo_data = NULL, logo_content_type = NULL,
                   logo_filename = NULL, logo_size_bytes = NULL,
                   updated_by_display_name = ?, updated_at = ?
               WHERE organisation_id = ?""",
            (current_user["display_name"], ts, organisation_id),
        )

        audit_event(
            conn,
            entity_type="OrgBranding",
            entity_id=row["branding_id"],
            action="LOGO_DELETE",
            summary=f"{current_user['display_name']} removed logo for organisation {organisation_id}.",
            organisation_id=organisation_id,
        )

        conn.commit()
        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "logo_removed": True,
            "message": "Logo removed.",
        }), 200


    @app.get("/organisations/<organisation_id>/branding")
    def get_org_branding(organisation_id):
        """
        Return branding metadata — logo presence, colour, and logo URL.
        Does NOT return the logo bytes; use GET /branding/logo for the image.
        """
        conn = get_conn()
        ensure_org_branding_tables(conn)

        row = conn.execute(
            """SELECT branding_id, organisation_id,
                      logo_content_type, logo_filename, logo_size_bytes,
                      primary_colour, updated_by_display_name,
                      created_at, updated_at
               FROM org_branding WHERE organisation_id = ?""",
            (organisation_id,),
        ).fetchone()
        conn.close()

        has_logo = bool(row and row["logo_size_bytes"])

        return jsonify({
            "organisation_id": organisation_id,
            "has_logo": has_logo,
            "logo_url": f"/organisations/{organisation_id}/branding/logo" if has_logo else None,
            "logo_content_type": row["logo_content_type"] if row else None,
            "logo_filename": row["logo_filename"] if row else None,
            "logo_size_bytes": row["logo_size_bytes"] if row else None,
            "primary_colour": row["primary_colour"] if row else None,
            "updated_by": row["updated_by_display_name"] if row else None,
            "updated_at": row["updated_at"] if row else None,
        }), 200


    @app.patch("/organisations/<organisation_id>/branding")
    def update_org_branding(organisation_id):
        """
        Update branding settings that are not the logo.
        Currently: primary_colour (hex string, e.g. '#1A2B3C').
        """
        current_user = g.current_user
        if current_user["role"] not in _ORG_ADMIN_ROLES:
            return jsonify({"error": "Only Org Admin or above can manage org branding"}), 403

        body = request.get_json(silent=True) or {}
        primary_colour = body.get("primary_colour")

        if primary_colour is not None:
            primary_colour = str(primary_colour).strip()
            if primary_colour and not re.match(r"^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$", primary_colour):
                return jsonify({"error": "primary_colour must be a valid hex colour e.g. '#1A2B3C' or '#FFF'"}), 400
            if not primary_colour:
                primary_colour = None

        conn = get_conn()
        ensure_org_branding_tables(conn)

        org = conn.execute(
            "SELECT organisation_id FROM organisations WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        ts = now_iso()
        existing = conn.execute(
            "SELECT branding_id FROM org_branding WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE org_branding
                   SET primary_colour = ?, updated_by_display_name = ?, updated_at = ?
                   WHERE organisation_id = ?""",
                (primary_colour, current_user["display_name"], ts, organisation_id),
            )
            branding_id = existing["branding_id"]
        else:
            branding_id = make_id("brand")
            conn.execute(
                """INSERT INTO org_branding
                   (branding_id, organisation_id, primary_colour,
                    updated_by_display_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (branding_id, organisation_id, primary_colour,
                 current_user["display_name"], ts, ts),
            )

        audit_event(
            conn,
            entity_type="OrgBranding",
            entity_id=branding_id,
            action="UPDATE",
            summary=(
                f"{current_user['display_name']} updated branding for organisation {organisation_id}. "
                f"primary_colour={primary_colour!r}."
            ),
            organisation_id=organisation_id,
        )

        conn.commit()
        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "primary_colour": primary_colour,
            "updated_by": current_user["display_name"],
            "updated_at": ts,
            "message": "Branding updated.",
        }), 200
