"""
modules/partners.py — Partners brick

Covers:
  - DB setup: partners, partner_addresses, org_connection_requests,
              location_update_requests tables
  - Navigation contract helper
  - Partner CRUD (create, list, get, update, deactivate, reactivate)
  - Partner addresses (full CRUD + set-primary / set-default-dispatch /
    set-default-receiving + audit + history + navigation-options)
  - Location update requests (submit, list, approve, reject)
  - Org connection requests (request, list, approve, reject)
  - Partner module overview dashboard
  - Shared-transaction disputes list + admin resolve
"""

from urllib.parse import quote

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso


# ── DB setup ──────────────────────────────────────────────────────────────────

def ensure_partner_connection_tables(conn):
    changed = False

    partner_cols = {row["name"] for row in conn.execute("PRAGMA table_info(partners)").fetchall()}
    if "linked_org_id" not in partner_cols:
        conn.execute("ALTER TABLE partners ADD COLUMN linked_org_id TEXT")
        changed = True
    if "connection_status" not in partner_cols:
        conn.execute("ALTER TABLE partners ADD COLUMN connection_status TEXT")
        changed = True
    if "updated_at" not in partner_cols:
        conn.execute("ALTER TABLE partners ADD COLUMN updated_at TEXT")
        changed = True

    conn.execute("""
    CREATE TABLE IF NOT EXISTS org_connection_requests (
        connection_request_id TEXT PRIMARY KEY,
        requesting_org_id TEXT NOT NULL,
        requesting_partner_id TEXT NOT NULL,
        target_org_id TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    if changed:
        conn.commit()


def ensure_partner_address_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS partner_addresses (
        partner_address_id TEXT PRIMARY KEY,
        partner_id TEXT NOT NULL,
        organisation_id TEXT NOT NULL,
        label TEXT NOT NULL,
        category TEXT NOT NULL,
        custom_category_label TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        is_primary INTEGER NOT NULL DEFAULT 0,
        is_default_dispatch_site INTEGER NOT NULL DEFAULT 0,
        is_default_receiving_site INTEGER NOT NULL DEFAULT 0,
        address_line_1 TEXT,
        address_line_2 TEXT,
        suburb TEXT,
        state TEXT,
        postcode TEXT,
        country TEXT,
        gate_number TEXT,
        door_number TEXT,
        entry_instructions TEXT,
        truck_access_notes TEXT,
        latitude REAL,
        longitude REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS location_update_requests (
        location_update_request_id TEXT PRIMARY KEY,
        organisation_id TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        current_latitude REAL,
        current_longitude REAL,
        proposed_latitude REAL NOT NULL,
        proposed_longitude REAL NOT NULL,
        reason_text TEXT,
        status TEXT NOT NULL,
        submitted_by_display_name TEXT NOT NULL,
        reviewed_by_display_name TEXT,
        review_notes TEXT,
        reviewed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    conn.commit()


# ── Navigation contract helper ─────────────────────────────────────────────────

def build_partner_address_navigation_contract(addr_like, default_nav_app="google_maps"):
    addr = dict(addr_like)

    def clean(value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    label = clean(addr.get("label")) or "Partner Address"
    gate_number = clean(addr.get("gate_number"))
    door_number = clean(addr.get("door_number"))

    address_parts = [
        clean(addr.get("address_line_1")),
        clean(addr.get("address_line_2")),
        clean(addr.get("suburb")),
        clean(addr.get("state")),
        clean(addr.get("postcode")),
        clean(addr.get("country")),
    ]
    formatted_address = ", ".join([x for x in address_parts if x]) or None

    latitude = addr.get("latitude")
    longitude = addr.get("longitude")

    has_gps = latitude is not None and longitude is not None
    destination_type = "gps" if has_gps else ("address" if formatted_address else None)
    can_navigate = destination_type is not None

    arrival_bits = []
    if gate_number:
        arrival_bits.append(f"Gate {gate_number}")
    if door_number:
        arrival_bits.append(f"Door {door_number}")
    arrival_hint = ", ".join(arrival_bits) or None

    apps = []
    if can_navigate:
        if has_gps:
            gps_pair = f"{latitude},{longitude}"
            google_uri = f"google.navigation:q={gps_pair}"
            waze_uri = f"waze://?ll={gps_pair}&navigate=yes"
            apple_uri = f"http://maps.apple.com/?ll={gps_pair}&q={quote(label)}"
            geo_uri = f"geo:{gps_pair}?q={gps_pair}({quote(label)})"
        else:
            encoded_address = quote(formatted_address)
            google_uri = f"google.navigation:q={encoded_address}"
            waze_uri = f"waze://?q={encoded_address}&navigate=yes"
            apple_uri = f"http://maps.apple.com/?address={encoded_address}"
            geo_uri = f"geo:0,0?q={encoded_address}"

        apps = [
            {
                "app_key": "google_maps",
                "app_label": "Google Maps",
                "is_default": False,
                "launch_uri": google_uri,
            },
            {
                "app_key": "waze",
                "app_label": "Waze",
                "is_default": False,
                "launch_uri": waze_uri,
            },
            {
                "app_key": "apple_maps",
                "app_label": "Apple Maps",
                "is_default": False,
                "launch_uri": apple_uri,
            },
            {
                "app_key": "generic_geo",
                "app_label": "Default Navigation App",
                "is_default": False,
                "launch_uri": geo_uri,
            },
        ]

        wanted = (default_nav_app or "google_maps").strip().lower()
        order = [wanted] + [a["app_key"] for a in apps if a["app_key"] != wanted]
        app_map = {a["app_key"]: a for a in apps}
        ordered = []
        for key in order:
            if key in app_map:
                item = dict(app_map[key])
                item["is_default"] = (key == wanted)
                ordered.append(item)
        apps = ordered

    navigation = {
        "can_navigate": can_navigate,
        "destination_type": destination_type,
        "latitude": latitude,
        "longitude": longitude,
        "formatted_address": formatted_address,
        "gate_number": gate_number,
        "door_number": door_number,
        "arrival_hint": arrival_hint,
        "preferred_label": label,
        "default_nav_app": (default_nav_app or "google_maps").strip().lower() or "google_maps",
    }

    return {
        "navigation": navigation,
        "navigation_apps": apps,
    }


# ── Route registration ─────────────────────────────────────────────────────────

def register_partner_routes(app, record_shared_transaction_event):

    # ── Partner addresses ──────────────────────────────────────────────────────

    @app.post("/partners/<partner_id>/addresses")
    def create_partner_address(partner_id):
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        label = (body.get("label") or "").strip()
        category = (body.get("category") or "").strip().upper()
        custom_category_label = (body.get("custom_category_label") or "").strip() or None

        address_line_1 = (body.get("address_line_1") or "").strip() or None
        address_line_2 = (body.get("address_line_2") or "").strip() or None
        suburb = (body.get("suburb") or "").strip() or None
        state = (body.get("state") or "").strip() or None
        postcode = (body.get("postcode") or "").strip() or None
        country = (body.get("country") or "").strip() or None
        gate_number = (body.get("gate_number") or "").strip() or None
        door_number = (body.get("door_number") or "").strip() or None
        entry_instructions = (body.get("entry_instructions") or "").strip() or None
        truck_access_notes = (body.get("truck_access_notes") or "").strip() or None

        latitude = body.get("latitude")
        longitude = body.get("longitude")

        is_primary = 1 if bool(body.get("is_primary")) else 0
        is_default_dispatch_site = 1 if bool(body.get("is_default_dispatch_site")) else 0
        is_default_receiving_site = 1 if bool(body.get("is_default_receiving_site")) else 0

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400
        if not label:
            return jsonify({"error": "label is required"}), 400

        allowed_categories = {
            "HEAD_OFFICE",
            "WORK_SITE",
            "WAREHOUSE",
            "FACTORY",
            "YARD",
            "DEPOT",
            "DISTRIBUTION_CENTRE",
            "OFFICE",
            "RETURN_SITE",
            "OTHER",
            "CUSTOM",
        }
        if category not in allowed_categories:
            return jsonify({"error": "Invalid category"}), 400
        if category == "CUSTOM" and not custom_category_label:
            return jsonify({"error": "custom_category_label is required when category is CUSTOM"}), 400

        if latitude is not None:
            try:
                latitude = float(latitude)
            except Exception:
                return jsonify({"error": "latitude must be a number"}), 400
        if longitude is not None:
            try:
                longitude = float(longitude)
            except Exception:
                return jsonify({"error": "longitude must be a number"}), 400

        conn = get_conn()
        ensure_partner_address_tables(conn)

        partner = conn.execute(
            "SELECT partner_id, organisation_id, name FROM partners WHERE partner_id = ? AND organisation_id = ?",
            (partner_id, organisation_id)
        ).fetchone()

        if not partner:
            conn.close()
            return jsonify({"error": "Partner not found"}), 404

        if is_primary:
            conn.execute(
                "UPDATE partner_addresses SET is_primary = 0 WHERE partner_id = ? AND organisation_id = ?",
                (partner_id, organisation_id)
            )
        if is_default_dispatch_site:
            conn.execute(
                "UPDATE partner_addresses SET is_default_dispatch_site = 0 WHERE partner_id = ? AND organisation_id = ?",
                (partner_id, organisation_id)
            )
        if is_default_receiving_site:
            conn.execute(
                "UPDATE partner_addresses SET is_default_receiving_site = 0 WHERE partner_id = ? AND organisation_id = ?",
                (partner_id, organisation_id)
            )

        partner_address_id = make_id("paddr")
        now = now_iso()

        conn.execute(
            """
            INSERT INTO partner_addresses (
                partner_address_id,
                partner_id,
                organisation_id,
                label,
                category,
                custom_category_label,
                is_active,
                is_primary,
                is_default_dispatch_site,
                is_default_receiving_site,
                address_line_1,
                address_line_2,
                suburb,
                state,
                postcode,
                country,
                gate_number,
                door_number,
                entry_instructions,
                truck_access_notes,
                latitude,
                longitude,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                partner_address_id,
                partner_id,
                organisation_id,
                label,
                category,
                custom_category_label,
                1,
                is_primary,
                is_default_dispatch_site,
                is_default_receiving_site,
                address_line_1,
                address_line_2,
                suburb,
                state,
                postcode,
                country,
                gate_number,
                door_number,
                entry_instructions,
                truck_access_notes,
                latitude,
                longitude,
                now,
                now
            )
        )

        audit_event(
            conn,
            entity_type="Partner",
            entity_id=partner_id,
            action="PARTNER_ADDRESS_CREATED",
            summary=f"Created partner address: {label}",
            organisation_id=organisation_id
        )

        conn.commit()
        conn.close()

        return jsonify({
            "partner_address_id": partner_address_id,
            "partner_id": partner_id,
            "organisation_id": organisation_id,
            "partner_name": partner["name"],
            "label": label,
            "category": category,
            "custom_category_label": custom_category_label,
            "is_active": True,
            "is_primary": bool(is_primary),
            "is_default_dispatch_site": bool(is_default_dispatch_site),
            "is_default_receiving_site": bool(is_default_receiving_site),
            "address_line_1": address_line_1,
            "address_line_2": address_line_2,
            "suburb": suburb,
            "state": state,
            "postcode": postcode,
            "country": country,
            "gate_number": gate_number,
            "door_number": door_number,
            "entry_instructions": entry_instructions,
            "truck_access_notes": truck_access_notes,
            "latitude": latitude,
            "longitude": longitude,
            "created_at": now,
            "updated_at": now
        }), 201


    @app.get("/partners/<partner_id>/addresses")
    def list_partner_addresses(partner_id):
        conn = get_conn()
        ensure_partner_address_tables(conn)

        partner = conn.execute(
            "SELECT partner_id, organisation_id, name FROM partners WHERE partner_id = ?",
            (partner_id,)
        ).fetchone()

        if not partner:
            conn.close()
            return jsonify({"error": "Partner not found"}), 404

        rows = conn.execute(
            """
            SELECT
                partner_address_id,
                partner_id,
                organisation_id,
                label,
                category,
                custom_category_label,
                is_active,
                is_primary,
                is_default_dispatch_site,
                is_default_receiving_site,
                address_line_1,
                address_line_2,
                suburb,
                state,
                postcode,
                country,
                gate_number,
                door_number,
                entry_instructions,
                truck_access_notes,
                latitude,
                longitude,
                created_at,
                updated_at
            FROM partner_addresses
            WHERE partner_id = ?
            ORDER BY is_primary DESC, label ASC, created_at DESC
            """,
            (partner_id,)
        ).fetchall()

        conn.close()

        items = []
        for row in rows:
            d = dict(row)
            d["is_active"] = bool(d["is_active"])
            d["is_primary"] = bool(d["is_primary"])
            d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
            d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
            nav_contract = build_partner_address_navigation_contract(d)
            d["navigation"] = nav_contract["navigation"]
            d["navigation_apps"] = nav_contract["navigation_apps"]
            items.append(d)

        return jsonify({
            "partner_id": partner_id,
            "organisation_id": partner["organisation_id"],
            "partner_name": partner["name"],
            "count": len(items),
            "items": items
        }), 200


    @app.get("/partner-addresses/<partner_address_id>")
    def get_partner_address(partner_address_id):
        conn = get_conn()
        ensure_partner_address_tables(conn)

        row = conn.execute(
            """
            SELECT
                pa.partner_address_id,
                pa.partner_id,
                pa.organisation_id,
                p.name AS partner_name,
                pa.label,
                pa.category,
                pa.custom_category_label,
                pa.is_active,
                pa.is_primary,
                pa.is_default_dispatch_site,
                pa.is_default_receiving_site,
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
                pa.created_at,
                pa.updated_at
            FROM partner_addresses pa
            LEFT JOIN partners p ON p.partner_id = pa.partner_id
            WHERE pa.partner_address_id = ?
            """,
            (partner_address_id,)
        ).fetchone()

        conn.close()

        if not row:
            return jsonify({"error": "Partner address not found"}), 404

        d = dict(row)
        d["is_active"] = bool(d["is_active"])
        d["is_primary"] = bool(d["is_primary"])
        d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
        d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
        nav_contract = build_partner_address_navigation_contract(d)
        d["navigation"] = nav_contract["navigation"]
        d["navigation_apps"] = nav_contract["navigation_apps"]

        return jsonify(d), 200


    @app.patch("/partner-addresses/<partner_address_id>")
    def update_partner_address(partner_address_id):
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        updated_by_display_name = (body.get("updated_by_display_name") or "Unknown Admin").strip()

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400

        conn = get_conn()
        ensure_partner_address_tables(conn)

        addr = conn.execute(
            """
            SELECT pa.*, p.name AS partner_name
            FROM partner_addresses pa
            LEFT JOIN partners p ON p.partner_id = pa.partner_id
            WHERE pa.partner_address_id = ? AND pa.organisation_id = ?
            """,
            (partner_address_id, organisation_id)
        ).fetchone()

        if not addr:
            conn.close()
            return jsonify({"error": "Partner address not found"}), 404

        allowed_categories = {
            "HEAD_OFFICE",
            "WORK_SITE",
            "WAREHOUSE",
            "FACTORY",
            "YARD",
            "DEPOT",
            "DISTRIBUTION_CENTRE",
            "OFFICE",
            "RETURN_SITE",
            "OTHER",
            "CUSTOM",
        }

        new_category = body.get("category", addr["category"])
        if isinstance(new_category, str):
            new_category = new_category.strip().upper()

        new_custom_category_label = body.get("custom_category_label", addr["custom_category_label"])
        if isinstance(new_custom_category_label, str):
            new_custom_category_label = new_custom_category_label.strip() or None

        if new_category not in allowed_categories:
            conn.close()
            return jsonify({"error": "Invalid category"}), 400

        if new_category == "CUSTOM" and not new_custom_category_label:
            conn.close()
            return jsonify({"error": "custom_category_label is required when category is CUSTOM"}), 400

        if "label" in body:
            label_check = (body.get("label") or "").strip()
            if not label_check:
                conn.close()
                return jsonify({"error": "label cannot be blank"}), 400

        updates = []
        params = []

        text_fields = [
            "label",
            "category",
            "custom_category_label",
            "address_line_1",
            "address_line_2",
            "suburb",
            "state",
            "postcode",
            "country",
            "gate_number",
            "door_number",
            "entry_instructions",
            "truck_access_notes",
        ]

        for field in text_fields:
            if field in body:
                value = body.get(field)
                if isinstance(value, str):
                    value = value.strip()
                if field == "category" and value is not None:
                    value = value.upper()
                if field not in ("label", "category") and value == "":
                    value = None
                updates.append(f"{field} = ?")
                params.append(value)

        for field in ("latitude", "longitude"):
            if field in body:
                value = body.get(field)
                if value in ("", None):
                    value = None
                else:
                    try:
                        value = float(value)
                    except Exception:
                        conn.close()
                        return jsonify({"error": f"{field} must be a number"}), 400
                updates.append(f"{field} = ?")
                params.append(value)

        if not updates:
            conn.close()
            return jsonify({"error": "No editable fields were provided"}), 400

        updates.append("updated_at = ?")
        params.append(now_iso())
        params.append(partner_address_id)

        conn.execute(
            f"UPDATE partner_addresses SET {', '.join(updates)} WHERE partner_address_id = ?",
            params
        )

        audit_event(
            conn,
            entity_type="PartnerAddress",
            entity_id=partner_address_id,
            action="PARTNER_ADDRESS_UPDATED",
            summary=f"Partner address updated by {updated_by_display_name}",
            organisation_id=organisation_id
        )

        updated = conn.execute(
            """
            SELECT pa.*, p.name AS partner_name
            FROM partner_addresses pa
            LEFT JOIN partners p ON p.partner_id = pa.partner_id
            WHERE pa.partner_address_id = ?
            """,
            (partner_address_id,)
        ).fetchone()

        conn.commit()
        conn.close()

        d = dict(updated)
        d["is_active"] = bool(d["is_active"])
        d["is_primary"] = bool(d["is_primary"])
        d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
        d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
        return jsonify(d), 200


    @app.post("/partner-addresses/<partner_address_id>/deactivate")
    def deactivate_partner_address(partner_address_id):
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        deactivated_by_display_name = (body.get("deactivated_by_display_name") or "Unknown Admin").strip()

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400

        conn = get_conn()
        ensure_partner_address_tables(conn)

        addr = conn.execute(
            """
            SELECT pa.*, p.name AS partner_name
            FROM partner_addresses pa
            LEFT JOIN partners p ON p.partner_id = pa.partner_id
            WHERE pa.partner_address_id = ? AND pa.organisation_id = ?
            """,
            (partner_address_id, organisation_id)
        ).fetchone()

        if not addr:
            conn.close()
            return jsonify({"error": "Partner address not found"}), 404

        conn.execute(
            """
            UPDATE partner_addresses
            SET is_active = 0,
                is_primary = 0,
                is_default_dispatch_site = 0,
                is_default_receiving_site = 0,
                updated_at = ?
            WHERE partner_address_id = ?
            """,
            (now_iso(), partner_address_id)
        )

        audit_event(
            conn,
            entity_type="PartnerAddress",
            entity_id=partner_address_id,
            action="PARTNER_ADDRESS_DEACTIVATED",
            summary=f"Partner address deactivated by {deactivated_by_display_name}",
            organisation_id=organisation_id
        )

        updated = conn.execute(
            """
            SELECT pa.*, p.name AS partner_name
            FROM partner_addresses pa
            LEFT JOIN partners p ON p.partner_id = pa.partner_id
            WHERE pa.partner_address_id = ?
            """,
            (partner_address_id,)
        ).fetchone()

        conn.commit()
        conn.close()

        d = dict(updated)
        d["is_active"] = bool(d["is_active"])
        d["is_primary"] = bool(d["is_primary"])
        d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
        d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
        return jsonify(d), 200


    @app.post("/partner-addresses/<partner_address_id>/set-primary")
    def set_primary_partner_address(partner_address_id):
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        updated_by_display_name = (body.get("updated_by_display_name") or "Unknown Admin").strip()

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400

        conn = get_conn()
        ensure_partner_address_tables(conn)

        addr = conn.execute(
            "SELECT * FROM partner_addresses WHERE partner_address_id = ? AND organisation_id = ?",
            (partner_address_id, organisation_id)
        ).fetchone()

        if not addr:
            conn.close()
            return jsonify({"error": "Partner address not found"}), 404
        if not addr["is_active"]:
            conn.close()
            return jsonify({"error": "Inactive address cannot be set as primary"}), 400

        conn.execute(
            "UPDATE partner_addresses SET is_primary = 0 WHERE partner_id = ? AND organisation_id = ?",
            (addr["partner_id"], organisation_id)
        )
        conn.execute(
            "UPDATE partner_addresses SET is_primary = 1, updated_at = ? WHERE partner_address_id = ?",
            (now_iso(), partner_address_id)
        )

        audit_event(
            conn,
            entity_type="PartnerAddress",
            entity_id=partner_address_id,
            action="PARTNER_ADDRESS_SET_PRIMARY",
            summary=f"Primary partner address set by {updated_by_display_name}",
            organisation_id=organisation_id
        )

        updated = conn.execute(
            """
            SELECT pa.*, p.name AS partner_name
            FROM partner_addresses pa
            LEFT JOIN partners p ON p.partner_id = pa.partner_id
            WHERE pa.partner_address_id = ?
            """,
            (partner_address_id,)
        ).fetchone()

        conn.commit()
        conn.close()

        d = dict(updated)
        d["is_active"] = bool(d["is_active"])
        d["is_primary"] = bool(d["is_primary"])
        d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
        d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
        return jsonify(d), 200


    @app.post("/partner-addresses/<partner_address_id>/set-default-dispatch")
    def set_default_dispatch_partner_address(partner_address_id):
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        updated_by_display_name = (body.get("updated_by_display_name") or "Unknown Admin").strip()

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400

        conn = get_conn()
        ensure_partner_address_tables(conn)

        addr = conn.execute(
            "SELECT * FROM partner_addresses WHERE partner_address_id = ? AND organisation_id = ?",
            (partner_address_id, organisation_id)
        ).fetchone()

        if not addr:
            conn.close()
            return jsonify({"error": "Partner address not found"}), 404
        if not addr["is_active"]:
            conn.close()
            return jsonify({"error": "Inactive address cannot be default dispatch"}), 400

        conn.execute(
            "UPDATE partner_addresses SET is_default_dispatch_site = 0 WHERE partner_id = ? AND organisation_id = ?",
            (addr["partner_id"], organisation_id)
        )
        conn.execute(
            "UPDATE partner_addresses SET is_default_dispatch_site = 1, updated_at = ? WHERE partner_address_id = ?",
            (now_iso(), partner_address_id)
        )

        audit_event(
            conn,
            entity_type="PartnerAddress",
            entity_id=partner_address_id,
            action="PARTNER_ADDRESS_SET_DEFAULT_DISPATCH",
            summary=f"Default dispatch partner address set by {updated_by_display_name}",
            organisation_id=organisation_id
        )

        updated = conn.execute(
            """
            SELECT pa.*, p.name AS partner_name
            FROM partner_addresses pa
            LEFT JOIN partners p ON p.partner_id = pa.partner_id
            WHERE pa.partner_address_id = ?
            """,
            (partner_address_id,)
        ).fetchone()

        conn.commit()
        conn.close()

        d = dict(updated)
        d["is_active"] = bool(d["is_active"])
        d["is_primary"] = bool(d["is_primary"])
        d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
        d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
        return jsonify(d), 200


    @app.post("/partner-addresses/<partner_address_id>/set-default-receiving")
    def set_default_receiving_partner_address(partner_address_id):
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        updated_by_display_name = (body.get("updated_by_display_name") or "Unknown Admin").strip()

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400

        conn = get_conn()
        ensure_partner_address_tables(conn)

        addr = conn.execute(
            "SELECT * FROM partner_addresses WHERE partner_address_id = ? AND organisation_id = ?",
            (partner_address_id, organisation_id)
        ).fetchone()

        if not addr:
            conn.close()
            return jsonify({"error": "Partner address not found"}), 404
        if not addr["is_active"]:
            conn.close()
            return jsonify({"error": "Inactive address cannot be default receiving"}), 400

        conn.execute(
            "UPDATE partner_addresses SET is_default_receiving_site = 0 WHERE partner_id = ? AND organisation_id = ?",
            (addr["partner_id"], organisation_id)
        )
        conn.execute(
            "UPDATE partner_addresses SET is_default_receiving_site = 1, updated_at = ? WHERE partner_address_id = ?",
            (now_iso(), partner_address_id)
        )

        audit_event(
            conn,
            entity_type="PartnerAddress",
            entity_id=partner_address_id,
            action="PARTNER_ADDRESS_SET_DEFAULT_RECEIVING",
            summary=f"Default receiving partner address set by {updated_by_display_name}",
            organisation_id=organisation_id
        )

        updated = conn.execute(
            """
            SELECT pa.*, p.name AS partner_name
            FROM partner_addresses pa
            LEFT JOIN partners p ON p.partner_id = pa.partner_id
            WHERE pa.partner_address_id = ?
            """,
            (partner_address_id,)
        ).fetchone()

        conn.commit()
        conn.close()

        d = dict(updated)
        d["is_active"] = bool(d["is_active"])
        d["is_primary"] = bool(d["is_primary"])
        d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
        d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
        return jsonify(d), 200


    @app.get("/partner-addresses/<partner_address_id>/audit")
    def get_partner_address_audit(partner_address_id):
        conn = get_conn()
        ensure_partner_address_tables(conn)

        addr = conn.execute(
            """
            SELECT
                pa.partner_address_id,
                pa.partner_id,
                pa.organisation_id,
                p.name AS partner_name,
                pa.label,
                pa.category,
                pa.custom_category_label,
                pa.is_active,
                pa.is_primary,
                pa.is_default_dispatch_site,
                pa.is_default_receiving_site,
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
                pa.created_at,
                pa.updated_at
            FROM partner_addresses pa
            LEFT JOIN partners p ON p.partner_id = pa.partner_id
            WHERE pa.partner_address_id = ?
            """,
            (partner_address_id,)
        ).fetchone()

        if not addr:
            conn.close()
            return jsonify({"error": "Partner address not found"}), 404

        audit_rows = conn.execute(
            """
            SELECT *
            FROM audit_events
            WHERE entity_type = 'PartnerAddress'
              AND entity_id = ?
            ORDER BY created_at DESC
            """,
            (partner_address_id,)
        ).fetchall()

        location_rows = conn.execute(
            """
            SELECT *
            FROM location_update_requests
            WHERE entity_type = 'PartnerAddress'
              AND entity_id = ?
            ORDER BY created_at DESC
            """,
            (partner_address_id,)
        ).fetchall()

        conn.close()

        address = dict(addr)
        address["is_active"] = bool(address["is_active"])
        address["is_primary"] = bool(address["is_primary"])
        address["is_default_dispatch_site"] = bool(address["is_default_dispatch_site"])
        address["is_default_receiving_site"] = bool(address["is_default_receiving_site"])

        nav_contract = build_partner_address_navigation_contract(address)
        address["navigation"] = nav_contract["navigation"]
        address["navigation_apps"] = nav_contract["navigation_apps"]

        audit_events = [dict(r) for r in audit_rows]
        location_update_requests = [dict(r) for r in location_rows]

        return jsonify({
            "partner_address_id": address["partner_address_id"],
            "partner_id": address["partner_id"],
            "organisation_id": address["organisation_id"],
            "partner_name": address["partner_name"],
            "label": address["label"],
            "address": address,
            "audit_event_count": len(audit_events),
            "location_update_request_count": len(location_update_requests),
            "audit_events": audit_events,
            "location_update_requests": location_update_requests,
        }), 200


    @app.get("/partner-addresses/<partner_address_id>/history")
    def get_partner_address_history(partner_address_id):
        conn = get_conn()
        ensure_partner_address_tables(conn)

        addr = conn.execute(
            """
            SELECT
                pa.partner_address_id,
                pa.partner_id,
                pa.organisation_id,
                p.name AS partner_name,
                pa.label,
                pa.category
            FROM partner_addresses pa
            LEFT JOIN partners p ON p.partner_id = pa.partner_id
            WHERE pa.partner_address_id = ?
            """,
            (partner_address_id,)
        ).fetchone()

        if not addr:
            conn.close()
            return jsonify({"error": "Partner address not found"}), 404

        audit_rows = conn.execute(
            """
            SELECT *
            FROM audit_events
            WHERE entity_type = 'PartnerAddress'
              AND entity_id = ?
            """,
            (partner_address_id,)
        ).fetchall()

        location_rows = conn.execute(
            """
            SELECT *
            FROM location_update_requests
            WHERE entity_type = 'PartnerAddress'
              AND entity_id = ?
            """,
            (partner_address_id,)
        ).fetchall()

        conn.close()

        history = []

        for row in audit_rows:
            d = dict(row)
            history.append({
                "history_type": "AUDIT_EVENT",
                "sort_at": d.get("created_at"),
                "partner_address_id": partner_address_id,
                "partner_address_label": addr["label"],
                "partner_id": addr["partner_id"],
                "partner_name": addr["partner_name"],
                "payload": d,
            })

        for row in location_rows:
            d = dict(row)
            history.append({
                "history_type": "LOCATION_UPDATE_REQUEST",
                "sort_at": d.get("reviewed_at") or d.get("updated_at") or d.get("created_at"),
                "partner_address_id": partner_address_id,
                "partner_address_label": addr["label"],
                "partner_id": addr["partner_id"],
                "partner_name": addr["partner_name"],
                "payload": d,
            })

        history.sort(key=lambda x: x.get("sort_at") or "", reverse=True)

        return jsonify({
            "partner_address_id": addr["partner_address_id"],
            "partner_id": addr["partner_id"],
            "organisation_id": addr["organisation_id"],
            "partner_name": addr["partner_name"],
            "label": addr["label"],
            "category": addr["category"],
            "history_count": len(history),
            "history": history,
        }), 200


    @app.get("/partners/<partner_id>/address-history")
    def get_partner_address_history_for_partner(partner_id):
        conn = get_conn()
        ensure_partner_address_tables(conn)

        partner = conn.execute(
            "SELECT partner_id, organisation_id, name FROM partners WHERE partner_id = ?",
            (partner_id,)
        ).fetchone()

        if not partner:
            conn.close()
            return jsonify({"error": "Partner not found"}), 404

        address_rows = conn.execute(
            """
            SELECT
                partner_address_id,
                label,
                category,
                is_active,
                is_primary,
                is_default_dispatch_site,
                is_default_receiving_site
            FROM partner_addresses
            WHERE partner_id = ?
            ORDER BY created_at DESC
            """,
            (partner_id,)
        ).fetchall()

        address_map = {}
        for row in address_rows:
            d = dict(row)
            d["is_active"] = bool(d["is_active"])
            d["is_primary"] = bool(d["is_primary"])
            d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
            d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])
            address_map[d["partner_address_id"]] = d

        history = []

        for partner_address_id, address_info in address_map.items():
            audit_rows = conn.execute(
                """
                SELECT *
                FROM audit_events
                WHERE entity_type = 'PartnerAddress'
                  AND entity_id = ?
                """,
                (partner_address_id,)
            ).fetchall()

            location_rows = conn.execute(
                """
                SELECT *
                FROM location_update_requests
                WHERE entity_type = 'PartnerAddress'
                  AND entity_id = ?
                """,
                (partner_address_id,)
            ).fetchall()

            for row in audit_rows:
                d = dict(row)
                history.append({
                    "history_type": "AUDIT_EVENT",
                    "sort_at": d.get("created_at"),
                    "partner_address_id": partner_address_id,
                    "partner_address_label": address_info["label"],
                    "partner_address_category": address_info["category"],
                    "partner_address_flags": {
                        "is_active": address_info["is_active"],
                        "is_primary": address_info["is_primary"],
                        "is_default_dispatch_site": address_info["is_default_dispatch_site"],
                        "is_default_receiving_site": address_info["is_default_receiving_site"],
                    },
                    "payload": d,
                })

            for row in location_rows:
                d = dict(row)
                history.append({
                    "history_type": "LOCATION_UPDATE_REQUEST",
                    "sort_at": d.get("reviewed_at") or d.get("updated_at") or d.get("created_at"),
                    "partner_address_id": partner_address_id,
                    "partner_address_label": address_info["label"],
                    "partner_address_category": address_info["category"],
                    "partner_address_flags": {
                        "is_active": address_info["is_active"],
                        "is_primary": address_info["is_primary"],
                        "is_default_dispatch_site": address_info["is_default_dispatch_site"],
                        "is_default_receiving_site": address_info["is_default_receiving_site"],
                    },
                    "payload": d,
                })

        conn.close()

        history.sort(key=lambda x: x.get("sort_at") or "", reverse=True)

        return jsonify({
            "partner_id": partner["partner_id"],
            "organisation_id": partner["organisation_id"],
            "partner_name": partner["name"],
            "address_count": len(address_map),
            "address_history_count": len(history),
            "items": history,
        }), 200


    @app.get("/partner-addresses/<partner_address_id>/navigation-options")
    def get_partner_address_navigation_options(partner_address_id):
        default_nav_app = (request.args.get("default_nav_app") or "google_maps").strip().lower() or "google_maps"

        conn = get_conn()
        ensure_partner_address_tables(conn)

        row = conn.execute(
            """
            SELECT
                pa.partner_address_id,
                pa.partner_id,
                pa.organisation_id,
                p.name AS partner_name,
                pa.label,
                pa.category,
                pa.custom_category_label,
                pa.is_active,
                pa.is_primary,
                pa.is_default_dispatch_site,
                pa.is_default_receiving_site,
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
                pa.created_at,
                pa.updated_at
            FROM partner_addresses pa
            LEFT JOIN partners p ON p.partner_id = pa.partner_id
            WHERE pa.partner_address_id = ?
            """,
            (partner_address_id,)
        ).fetchone()

        conn.close()

        if not row:
            return jsonify({"error": "Partner address not found"}), 404

        d = dict(row)
        d["is_active"] = bool(d["is_active"])
        d["is_primary"] = bool(d["is_primary"])
        d["is_default_dispatch_site"] = bool(d["is_default_dispatch_site"])
        d["is_default_receiving_site"] = bool(d["is_default_receiving_site"])

        nav_contract = build_partner_address_navigation_contract(d, default_nav_app=default_nav_app)

        return jsonify({
            "partner_address_id": d["partner_address_id"],
            "partner_id": d["partner_id"],
            "organisation_id": d["organisation_id"],
            "partner_name": d["partner_name"],
            "label": d["label"],
            "navigation": nav_contract["navigation"],
            "navigation_apps": nav_contract["navigation_apps"],
        }), 200


    # ── Location update requests ───────────────────────────────────────────────

    @app.post("/partner-addresses/<partner_address_id>/location-update-request")
    def submit_partner_address_location_update_request(partner_address_id):
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        proposed_latitude = body.get("proposed_latitude")
        proposed_longitude = body.get("proposed_longitude")
        reason_text = (body.get("reason_text") or "Field user suggested GPS update").strip()
        submitted_by_display_name = (body.get("submitted_by_display_name") or "Unknown User").strip()

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400
        if proposed_latitude is None or proposed_longitude is None:
            return jsonify({"error": "proposed_latitude and proposed_longitude are required"}), 400

        try:
            proposed_latitude = float(proposed_latitude)
            proposed_longitude = float(proposed_longitude)
        except Exception:
            return jsonify({"error": "proposed_latitude and proposed_longitude must be numbers"}), 400

        conn = get_conn()
        ensure_partner_address_tables(conn)

        addr = conn.execute(
            """
            SELECT pa.*, p.name AS partner_name
            FROM partner_addresses pa
            LEFT JOIN partners p ON p.partner_id = pa.partner_id
            WHERE pa.partner_address_id = ? AND pa.organisation_id = ?
            """,
            (partner_address_id, organisation_id)
        ).fetchone()

        if not addr:
            conn.close()
            return jsonify({"error": "Partner address not found"}), 404

        req_id = make_id("lreq")
        now = now_iso()

        conn.execute(
            """
            INSERT INTO location_update_requests (
                location_update_request_id,
                organisation_id,
                entity_type,
                entity_id,
                current_latitude,
                current_longitude,
                proposed_latitude,
                proposed_longitude,
                reason_text,
                status,
                submitted_by_display_name,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                req_id,
                organisation_id,
                "PartnerAddress",
                partner_address_id,
                addr["latitude"],
                addr["longitude"],
                proposed_latitude,
                proposed_longitude,
                reason_text,
                "PENDING_APPROVAL",
                submitted_by_display_name,
                now,
                now
            )
        )

        audit_event(
            conn,
            entity_type="PartnerAddress",
            entity_id=partner_address_id,
            action="LOCATION_UPDATE_REQUESTED",
            summary=f"GPS update requested for partner address by {submitted_by_display_name}",
            organisation_id=organisation_id
        )

        conn.commit()
        conn.close()

        return jsonify({
            "location_update_request_id": req_id,
            "organisation_id": organisation_id,
            "entity_type": "PartnerAddress",
            "entity_id": partner_address_id,
            "partner_id": addr["partner_id"],
            "partner_name": addr["partner_name"],
            "label": addr["label"],
            "status": "PENDING_APPROVAL",
            "current_latitude": addr["latitude"],
            "current_longitude": addr["longitude"],
            "proposed_latitude": proposed_latitude,
            "proposed_longitude": proposed_longitude,
            "reason_text": reason_text,
            "submitted_by_display_name": submitted_by_display_name
        }), 201


    @app.get("/organisations/<organisation_id>/location-update-requests")
    def list_location_update_requests(organisation_id):
        status = (request.args.get("status") or "PENDING_APPROVAL").strip().upper()
        entity_type = (request.args.get("entity_type") or "").strip()

        conn = get_conn()
        ensure_partner_address_tables(conn)

        sql = """
            SELECT
                lur.location_update_request_id,
                lur.organisation_id,
                lur.entity_type,
                lur.entity_id,
                pa.partner_id,
                p.name AS partner_name,
                pa.label AS entity_name,
                lur.current_latitude,
                lur.current_longitude,
                lur.proposed_latitude,
                lur.proposed_longitude,
                lur.reason_text,
                lur.status,
                lur.submitted_by_display_name,
                lur.reviewed_by_display_name,
                lur.review_notes,
                lur.reviewed_at,
                lur.created_at,
                lur.updated_at
            FROM location_update_requests lur
            LEFT JOIN partner_addresses pa
                ON lur.entity_type = 'PartnerAddress' AND pa.partner_address_id = lur.entity_id
            LEFT JOIN partners p
                ON p.partner_id = pa.partner_id
            WHERE lur.organisation_id = ?
              AND lur.status = ?
        """
        params = [organisation_id, status]

        if entity_type:
            sql += " AND lur.entity_type = ?"
            params.append(entity_type)

        sql += " ORDER BY lur.created_at DESC"

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "count": len(rows),
            "items": [dict(r) for r in rows]
        }), 200


    @app.post("/location-update-requests/<location_update_request_id>/approve")
    def approve_location_update_request(location_update_request_id):
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        reviewed_by_display_name = (body.get("reviewed_by_display_name") or "Unknown Admin").strip()
        review_notes = (body.get("review_notes") or "").strip() or None

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400

        conn = get_conn()
        ensure_partner_address_tables(conn)

        req = conn.execute(
            "SELECT * FROM location_update_requests WHERE location_update_request_id = ? AND organisation_id = ?",
            (location_update_request_id, organisation_id)
        ).fetchone()

        if not req:
            conn.close()
            return jsonify({"error": "Location update request not found"}), 404

        if req["status"] != "PENDING_APPROVAL":
            conn.close()
            return jsonify({"error": "Location update request is not pending approval"}), 400

        if req["entity_type"] != "PartnerAddress":
            conn.close()
            return jsonify({"error": "Unsupported entity_type for this approval route"}), 400

        conn.execute(
            "UPDATE partner_addresses SET latitude = ?, longitude = ?, updated_at = ? WHERE partner_address_id = ?",
            (req["proposed_latitude"], req["proposed_longitude"], now_iso(), req["entity_id"])
        )

        conn.execute(
            """
            UPDATE location_update_requests
            SET status = 'APPROVED',
                reviewed_by_display_name = ?,
                review_notes = ?,
                reviewed_at = ?,
                updated_at = ?
            WHERE location_update_request_id = ?
            """,
            (reviewed_by_display_name, review_notes, now_iso(), now_iso(), location_update_request_id)
        )

        audit_event(
            conn,
            entity_type="PartnerAddress",
            entity_id=req["entity_id"],
            action="LOCATION_UPDATE_APPROVED",
            summary=f"GPS update approved by {reviewed_by_display_name}",
            organisation_id=organisation_id
        )

        conn.commit()
        conn.close()

        return jsonify({
            "location_update_request_id": location_update_request_id,
            "status": "APPROVED",
            "entity_type": "PartnerAddress",
            "entity_id": req["entity_id"],
            "latitude": req["proposed_latitude"],
            "longitude": req["proposed_longitude"],
            "reviewed_by_display_name": reviewed_by_display_name,
            "review_notes": review_notes
        }), 200


    @app.post("/location-update-requests/<location_update_request_id>/reject")
    def reject_location_update_request(location_update_request_id):
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        reviewed_by_display_name = (body.get("reviewed_by_display_name") or "Unknown Admin").strip()
        review_notes = (body.get("review_notes") or "").strip() or None

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400

        conn = get_conn()
        ensure_partner_address_tables(conn)

        req = conn.execute(
            "SELECT * FROM location_update_requests WHERE location_update_request_id = ? AND organisation_id = ?",
            (location_update_request_id, organisation_id)
        ).fetchone()

        if not req:
            conn.close()
            return jsonify({"error": "Location update request not found"}), 404

        if req["status"] != "PENDING_APPROVAL":
            conn.close()
            return jsonify({"error": "Location update request is not pending approval"}), 400

        conn.execute(
            """
            UPDATE location_update_requests
            SET status = 'REJECTED',
                reviewed_by_display_name = ?,
                review_notes = ?,
                reviewed_at = ?,
                updated_at = ?
            WHERE location_update_request_id = ?
            """,
            (reviewed_by_display_name, review_notes, now_iso(), now_iso(), location_update_request_id)
        )

        audit_event(
            conn,
            entity_type=req["entity_type"],
            entity_id=req["entity_id"],
            action="LOCATION_UPDATE_REJECTED",
            summary=f"GPS update rejected by {reviewed_by_display_name}",
            organisation_id=organisation_id
        )

        conn.commit()
        conn.close()

        return jsonify({
            "location_update_request_id": location_update_request_id,
            "status": "REJECTED",
            "entity_type": req["entity_type"],
            "entity_id": req["entity_id"],
            "reviewed_by_display_name": reviewed_by_display_name,
            "review_notes": review_notes
        }), 200


    # ── Partner module overview ────────────────────────────────────────────────

    @app.get("/organisations/<organisation_id>/partner-module")
    def get_partner_module_overview(organisation_id):
        conn = get_conn()
        ensure_partner_connection_tables(conn)

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

        partner_count_total = conn.execute(
            "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()["c"]

        partner_count_active = conn.execute(
            "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1",
            (organisation_id,)
        ).fetchone()["c"]

        partner_count_inactive = conn.execute(
            "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 0",
            (organisation_id,)
        ).fetchone()["c"]

        customer_count = conn.execute(
            "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1 AND is_customer = 1",
            (organisation_id,)
        ).fetchone()["c"]

        supplier_count = conn.execute(
            "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1 AND is_supplier = 1",
            (organisation_id,)
        ).fetchone()["c"]

        customer_only_count = conn.execute(
            "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1 AND is_customer = 1 AND is_supplier = 0",
            (organisation_id,)
        ).fetchone()["c"]

        supplier_only_count = conn.execute(
            "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1 AND is_supplier = 1 AND is_customer = 0",
            (organisation_id,)
        ).fetchone()["c"]

        both_count = conn.execute(
            "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1 AND is_customer = 1 AND is_supplier = 1",
            (organisation_id,)
        ).fetchone()["c"]

        connected_partner_count = conn.execute(
            "SELECT COUNT(*) AS c FROM partners WHERE organisation_id = ? AND is_active = 1 AND COALESCE(connection_status, 'LOCAL_ONLY') = 'CONNECTED'",
            (organisation_id,)
        ).fetchone()["c"]

        pending_incoming_count = conn.execute(
            "SELECT COUNT(*) AS c FROM org_connection_requests WHERE target_org_id = ? AND status = 'PENDING_APPROVAL'",
            (organisation_id,)
        ).fetchone()["c"]

        pending_outgoing_count = conn.execute(
            "SELECT COUNT(*) AS c FROM org_connection_requests WHERE requesting_org_id = ? AND status = 'PENDING_APPROVAL'",
            (organisation_id,)
        ).fetchone()["c"]

        all_rows = conn.execute(
            """
            SELECT
                p.partner_id,
                p.organisation_id,
                p.name,
                p.is_active,
                p.is_customer,
                p.is_supplier,
                p.created_at,
                p.updated_at,
                p.linked_org_id,
                lo.name AS linked_org_name,
                COALESCE(p.connection_status, 'LOCAL_ONLY') AS connection_status
            FROM partners p
            LEFT JOIN organisations lo ON lo.organisation_id = p.linked_org_id
            WHERE p.organisation_id = ?
            ORDER BY p.is_active DESC, p.created_at DESC
            LIMIT 50
            """,
            (organisation_id,)
        ).fetchall()

        customer_rows = conn.execute(
            """
            SELECT
                p.partner_id,
                p.organisation_id,
                p.name,
                p.is_active,
                p.is_customer,
                p.is_supplier,
                p.created_at,
                p.updated_at,
                p.linked_org_id,
                lo.name AS linked_org_name,
                COALESCE(p.connection_status, 'LOCAL_ONLY') AS connection_status
            FROM partners p
            LEFT JOIN organisations lo ON lo.organisation_id = p.linked_org_id
            WHERE p.organisation_id = ?
              AND p.is_customer = 1
            ORDER BY p.is_active DESC, p.created_at DESC
            LIMIT 50
            """,
            (organisation_id,)
        ).fetchall()

        supplier_rows = conn.execute(
            """
            SELECT
                p.partner_id,
                p.organisation_id,
                p.name,
                p.is_active,
                p.is_customer,
                p.is_supplier,
                p.created_at,
                p.updated_at,
                p.linked_org_id,
                lo.name AS linked_org_name,
                COALESCE(p.connection_status, 'LOCAL_ONLY') AS connection_status
            FROM partners p
            LEFT JOIN organisations lo ON lo.organisation_id = p.linked_org_id
            WHERE p.organisation_id = ?
              AND p.is_supplier = 1
            ORDER BY p.is_active DESC, p.created_at DESC
            LIMIT 50
            """,
            (organisation_id,)
        ).fetchall()

        connection_rows = conn.execute(
            """
            SELECT
                ocr.connection_request_id,
                ocr.requesting_org_id,
                ro.name AS requesting_org_name,
                ocr.requesting_partner_id,
                p.name AS partner_name,
                ocr.target_org_id,
                to2.name AS target_org_name,
                ocr.status,
                ocr.created_at,
                ocr.updated_at
            FROM org_connection_requests ocr
            LEFT JOIN organisations ro ON ro.organisation_id = ocr.requesting_org_id
            LEFT JOIN organisations to2 ON to2.organisation_id = ocr.target_org_id
            LEFT JOIN partners p ON p.partner_id = ocr.requesting_partner_id
            WHERE (ocr.requesting_org_id = ? OR ocr.target_org_id = ?)
              AND ocr.status = 'PENDING_APPROVAL'
            ORDER BY ocr.created_at DESC
            LIMIT 20
            """,
            (organisation_id, organisation_id)
        ).fetchall()

        conn.close()

        def decorate_partner(row):
            d = dict(row)
            d["is_active"] = bool(d["is_active"])
            d["is_customer"] = bool(d["is_customer"])
            d["is_supplier"] = bool(d["is_supplier"])

            if d["is_customer"] and d["is_supplier"]:
                d["role_label"] = "Customer + Supplier"
            elif d["is_customer"]:
                d["role_label"] = "Customer"
            elif d["is_supplier"]:
                d["role_label"] = "Supplier"
            else:
                d["role_label"] = "Partner"

            if d["connection_status"] == "CONNECTED":
                d["connection_label"] = "Connected Pallet Pro Org"
            elif d["connection_status"] == "REQUESTED":
                d["connection_label"] = "Connection Requested"
            else:
                d["connection_label"] = "Local Partner Only"

            d["entry_target_section"] = "all_partners"
            d["entry_target_entity_type"] = "Partner"
            d["entry_target_action"] = "open_partner_profile"
            d["highlight_key"] = d["partner_id"]
            return d

        recent_all_partners = [decorate_partner(r) for r in all_rows]
        recent_customers = [decorate_partner(r) for r in customer_rows]
        recent_suppliers = [decorate_partner(r) for r in supplier_rows]

        recent_connection_requests = []
        for row in connection_rows:
            d = dict(row)
            d["request_direction"] = "INCOMING" if d["target_org_id"] == organisation_id else "OUTGOING"
            d["entry_target_section"] = "connection_requests"
            d["entry_target_entity_type"] = "OrgConnectionRequest"
            d["entry_target_action"] = "review_connection_request"
            d["highlight_key"] = d["connection_request_id"]
            recent_connection_requests.append(d)

        return jsonify({
            "module_key": "partner-module",
            "organisation_id": org["organisation_id"],
            "organisation_name": org["name"],
            "created_at": org["created_at"],
            "default_section": "all_partners",
            "navigation": {
                "customers": {
                    "label": "Customers",
                    "count": customer_count
                },
                "suppliers": {
                    "label": "Suppliers",
                    "count": supplier_count
                },
                "all_partners": {
                    "label": "All Partners",
                    "count": partner_count_active
                }
            },
            "summary": {
                "partner_count": partner_count_active,
                "partner_count_active": partner_count_active,
                "partner_count_inactive": partner_count_inactive,
                "partner_count_total": partner_count_total,
                "customer_count": customer_count,
                "supplier_count": supplier_count,
                "customer_only_count": customer_only_count,
                "supplier_only_count": supplier_only_count,
                "customer_supplier_both_count": both_count,
                "connected_partner_count": connected_partner_count,
                "incoming_connection_request_count": pending_incoming_count,
                "outgoing_connection_request_count": pending_outgoing_count
            },
            "recent_customers": recent_customers,
            "recent_suppliers": recent_suppliers,
            "recent_all_partners": recent_all_partners,
            "recent_connection_requests": recent_connection_requests
        }), 200


    # ── Partner CRUD ───────────────────────────────────────────────────────────

    @app.post("/partners")
    def create_partner():
        _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
        body = request.get_json(silent=True) or {}
        # Enforce org isolation: non-global-admins always write to their own org
        if g.current_user.get("role") not in _GLOBAL_ADMIN_ROLES:
            organisation_id = g.current_user.get("user_org_id")
        else:
            organisation_id = body.get("organisation_id")
        name = (body.get("name") or "").strip()[:255]
        is_active = bool(body.get("is_active", True))
        is_customer = bool(body.get("is_customer", False))
        is_supplier = bool(body.get("is_supplier", False))

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400
        if not name:
            return jsonify({"error": "name is required"}), 400

        conn = get_conn()
        ensure_partner_connection_tables(conn)

        org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,)
        ).fetchone()

        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        partner_id = make_id("partner")

        conn.execute(
            """
            INSERT INTO partners (
                partner_id,
                organisation_id,
                name,
                is_active,
                is_customer,
                is_supplier,
                created_at,
                linked_org_id,
                connection_status,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                partner_id,
                organisation_id,
                name,
                1 if is_active else 0,
                1 if is_customer else 0,
                1 if is_supplier else 0,
                now_iso(),
                None,
                "LOCAL_ONLY",
                now_iso()
            )
        )

        audit_event(
            conn,
            entity_type="Partner",
            entity_id=partner_id,
            action="CREATE",
            summary=f"Created partner: {name}",
            organisation_id=organisation_id
        )

        conn.commit()
        conn.close()

        return jsonify({
            "partner_id": partner_id,
            "organisation_id": organisation_id,
            "name": name,
            "is_active": is_active,
            "is_customer": is_customer,
            "is_supplier": is_supplier,
            "linked_org_id": None,
            "connection_status": "LOCAL_ONLY"
        }), 201


    @app.get("/partners")
    def list_partners():
        organisation_id = request.args.get("organisation_id")
        is_active = request.args.get("is_active")
        role = request.args.get("role")
        connection_status = request.args.get("connection_status")

        conn = get_conn()
        ensure_partner_connection_tables(conn)

        sql = """
            SELECT
                p.partner_id,
                p.organisation_id,
                o.name AS organisation_name,
                p.name,
                p.is_active,
                p.is_customer,
                p.is_supplier,
                p.created_at,
                p.updated_at,
                p.linked_org_id,
                lo.name AS linked_org_name,
                COALESCE(p.connection_status, 'LOCAL_ONLY') AS connection_status
            FROM partners p
            LEFT JOIN organisations o ON o.organisation_id = p.organisation_id
            LEFT JOIN organisations lo ON lo.organisation_id = p.linked_org_id
            WHERE 1=1
        """
        params = []

        if organisation_id:
            sql += " AND p.organisation_id = ?"
            params.append(organisation_id)

        if is_active == "true":
            sql += " AND p.is_active = 1"
        elif is_active == "false":
            sql += " AND p.is_active = 0"

        if role == "customer":
            sql += " AND p.is_customer = 1"
        elif role == "supplier":
            sql += " AND p.is_supplier = 1"

        if connection_status:
            sql += " AND COALESCE(p.connection_status, 'LOCAL_ONLY') = ?"
            params.append(connection_status)

        sql += " ORDER BY p.is_active DESC, p.name"

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        items = []
        for row in rows:
            d = dict(row)
            d["is_active"] = bool(d["is_active"])
            d["is_customer"] = bool(d["is_customer"])
            d["is_supplier"] = bool(d["is_supplier"])

            if d["is_customer"] and d["is_supplier"]:
                d["role_label"] = "Customer + Supplier"
            elif d["is_customer"]:
                d["role_label"] = "Customer"
            elif d["is_supplier"]:
                d["role_label"] = "Supplier"
            else:
                d["role_label"] = "Partner"

            if d["connection_status"] == "CONNECTED":
                d["connection_label"] = "Connected Pallet Pro Org"
            elif d["connection_status"] == "REQUESTED":
                d["connection_label"] = "Connection Requested"
            else:
                d["connection_label"] = "Local Partner Only"

            items.append(d)

        return jsonify({
            "count": len(items),
            "items": items
        }), 200


    @app.get("/partners/<partner_id>")
    def get_partner_profile(partner_id):
        conn = get_conn()
        ensure_partner_connection_tables(conn)
        ensure_partner_address_tables(conn)

        row = conn.execute(
            """
            SELECT
                p.partner_id,
                p.organisation_id,
                o.name AS organisation_name,
                p.name,
                p.is_active,
                p.is_customer,
                p.is_supplier,
                p.created_at,
                p.updated_at,
                p.linked_org_id,
                lo.name AS linked_org_name,
                COALESCE(p.connection_status, 'LOCAL_ONLY') AS connection_status
            FROM partners p
            LEFT JOIN organisations o ON o.organisation_id = p.organisation_id
            LEFT JOIN organisations lo ON lo.organisation_id = p.linked_org_id
            WHERE p.partner_id = ?
            """,
            (partner_id,)
        ).fetchone()

        address_rows = conn.execute(
            """
            SELECT
                partner_address_id,
                partner_id,
                organisation_id,
                label,
                category,
                custom_category_label,
                is_active,
                is_primary,
                is_default_dispatch_site,
                is_default_receiving_site,
                address_line_1,
                address_line_2,
                suburb,
                state,
                postcode,
                country,
                gate_number,
                door_number,
                entry_instructions,
                truck_access_notes,
                latitude,
                longitude,
                created_at,
                updated_at
            FROM partner_addresses
            WHERE partner_id = ?
            ORDER BY
                is_primary DESC,
                is_default_dispatch_site DESC,
                is_default_receiving_site DESC,
                label ASC,
                created_at DESC
            """,
            (partner_id,)
        ).fetchall()

        conn.close()

        if not row:
            return jsonify({"error": "Partner not found"}), 404

        d = dict(row)
        d["is_active"] = bool(d["is_active"])
        d["is_customer"] = bool(d["is_customer"])
        d["is_supplier"] = bool(d["is_supplier"])

        if d["is_customer"] and d["is_supplier"]:
            d["role_label"] = "Customer + Supplier"
        elif d["is_customer"]:
            d["role_label"] = "Customer"
        elif d["is_supplier"]:
            d["role_label"] = "Supplier"
        else:
            d["role_label"] = "Partner"

        if d["connection_status"] == "CONNECTED":
            d["connection_label"] = "Connected Pallet Pro Org"
        elif d["connection_status"] == "REQUESTED":
            d["connection_label"] = "Connection Requested"
        else:
            d["connection_label"] = "Local Partner Only"

        partner_addresses = []
        for addr in address_rows:
            a = dict(addr)
            a["is_active"] = bool(a["is_active"])
            a["is_primary"] = bool(a["is_primary"])
            a["is_default_dispatch_site"] = bool(a["is_default_dispatch_site"])
            a["is_default_receiving_site"] = bool(a["is_default_receiving_site"])
            nav_contract = build_partner_address_navigation_contract(a)
            a["navigation"] = nav_contract["navigation"]
            a["navigation_apps"] = nav_contract["navigation_apps"]
            partner_addresses.append(a)

        d["partner_address_count"] = len(partner_addresses)
        d["partner_addresses"] = partner_addresses
        d["primary_partner_address"] = next((a for a in partner_addresses if a["is_primary"]), None)
        d["default_dispatch_partner_address"] = next((a for a in partner_addresses if a["is_default_dispatch_site"]), None)
        d["default_receiving_partner_address"] = next((a for a in partner_addresses if a["is_default_receiving_site"]), None)

        return jsonify(d), 200


    @app.patch("/partners/<partner_id>")
    def update_partner(partner_id):
        body = request.get_json(silent=True) or {}

        conn = get_conn()
        ensure_partner_connection_tables(conn)

        partner = conn.execute(
            "SELECT * FROM partners WHERE partner_id = ?", (partner_id,)
        ).fetchone()

        if not partner:
            conn.close()
            return jsonify({"error": "Partner not found"}), 404

        new_name = (body.get("name") or "").strip() or partner["name"]
        new_is_customer = body.get("is_customer")
        new_is_supplier = body.get("is_supplier")

        if new_is_customer is None:
            new_is_customer = bool(partner["is_customer"])
        else:
            new_is_customer = bool(new_is_customer)

        if new_is_supplier is None:
            new_is_supplier = bool(partner["is_supplier"])
        else:
            new_is_supplier = bool(new_is_supplier)

        changes = []
        if new_name != partner["name"]:
            changes.append(f"name '{partner['name']}' → '{new_name}'")
        if new_is_customer != bool(partner["is_customer"]):
            changes.append(f"is_customer → {new_is_customer}")
        if new_is_supplier != bool(partner["is_supplier"]):
            changes.append(f"is_supplier → {new_is_supplier}")

        if not changes:
            conn.close()
            return jsonify({"message": "No changes made", "partner_id": partner_id}), 200

        ts = now_iso()
        conn.execute(
            """UPDATE partners SET name = ?, is_customer = ?, is_supplier = ?, updated_at = ?
               WHERE partner_id = ?""",
            (new_name, 1 if new_is_customer else 0, 1 if new_is_supplier else 0, ts, partner_id),
        )

        audit_event(
            conn,
            entity_type="Partner",
            entity_id=partner_id,
            action="UPDATE",
            summary=f"Partner updated: {'; '.join(changes)}.",
            organisation_id=partner["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({
            "partner_id": partner_id,
            "organisation_id": partner["organisation_id"],
            "name": new_name,
            "is_customer": new_is_customer,
            "is_supplier": new_is_supplier,
            "updated_at": ts,
        }), 200


    @app.post("/partners/<partner_id>/deactivate")
    def deactivate_partner(partner_id):
        conn = get_conn()
        ensure_partner_connection_tables(conn)

        partner = conn.execute(
            "SELECT * FROM partners WHERE partner_id = ?", (partner_id,)
        ).fetchone()

        if not partner:
            conn.close()
            return jsonify({"error": "Partner not found"}), 404

        if not partner["is_active"]:
            conn.close()
            return jsonify({"error": "Partner is already inactive"}), 409

        ts = now_iso()
        conn.execute(
            "UPDATE partners SET is_active = 0, updated_at = ? WHERE partner_id = ?",
            (ts, partner_id),
        )

        audit_event(
            conn,
            entity_type="Partner",
            entity_id=partner_id,
            action="DEACTIVATE",
            summary=f"Partner '{partner['name']}' deactivated.",
            organisation_id=partner["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({
            "partner_id": partner_id,
            "name": partner["name"],
            "is_active": False,
        }), 200


    @app.post("/partners/<partner_id>/reactivate")
    def reactivate_partner(partner_id):
        conn = get_conn()
        ensure_partner_connection_tables(conn)

        partner = conn.execute(
            "SELECT * FROM partners WHERE partner_id = ?", (partner_id,)
        ).fetchone()

        if not partner:
            conn.close()
            return jsonify({"error": "Partner not found"}), 404

        if partner["is_active"]:
            conn.close()
            return jsonify({"error": "Partner is already active"}), 409

        ts = now_iso()
        conn.execute(
            "UPDATE partners SET is_active = 1, updated_at = ? WHERE partner_id = ?",
            (ts, partner_id),
        )

        audit_event(
            conn,
            entity_type="Partner",
            entity_id=partner_id,
            action="REACTIVATE",
            summary=f"Partner '{partner['name']}' reactivated.",
            organisation_id=partner["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({
            "partner_id": partner_id,
            "name": partner["name"],
            "is_active": True,
        }), 200


    # ── Org connection requests ────────────────────────────────────────────────

    @app.post("/partners/<partner_id>/request-org-connection")
    def request_org_connection(partner_id):
        conn = get_conn()
        ensure_partner_connection_tables(conn)

        partner = conn.execute(
            "SELECT * FROM partners WHERE partner_id = ?",
            (partner_id,)
        ).fetchone()

        if not partner:
            conn.close()
            return jsonify({"error": "Partner not found"}), 404

        matched_org = conn.execute(
            """
            SELECT organisation_id, name
            FROM organisations
            WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))
              AND organisation_id != ?
            LIMIT 1
            """,
            (partner["name"], partner["organisation_id"])
        ).fetchone()

        if not matched_org:
            conn.close()
            return jsonify({
                "error": "No matching Pallet Pro org found for this partner",
                "partner_id": partner_id,
                "partner_name": partner["name"]
            }), 404

        existing_connected = conn.execute(
            """
            SELECT partner_id
            FROM partners
            WHERE partner_id = ?
              AND linked_org_id = ?
              AND COALESCE(connection_status, 'LOCAL_ONLY') = 'CONNECTED'
            LIMIT 1
            """,
            (partner_id, matched_org["organisation_id"])
        ).fetchone()

        if existing_connected:
            conn.close()
            return jsonify({
                "error": "Partner is already connected to this Pallet Pro org",
                "partner_id": partner_id,
                "linked_org_id": matched_org["organisation_id"],
                "linked_org_name": matched_org["name"]
            }), 409

        existing_request = conn.execute(
            """
            SELECT connection_request_id, status
            FROM org_connection_requests
            WHERE requesting_partner_id = ?
              AND target_org_id = ?
              AND status IN ('PENDING_APPROVAL', 'CONNECTED')
            LIMIT 1
            """,
            (partner_id, matched_org["organisation_id"])
        ).fetchone()

        if existing_request:
            conn.close()
            return jsonify({
                "error": "Connection request already exists",
                "connection_request_id": existing_request["connection_request_id"],
                "status": existing_request["status"]
            }), 409

        connection_request_id = make_id("ocon")

        conn.execute(
            """
            INSERT INTO org_connection_requests (
                connection_request_id,
                requesting_org_id,
                requesting_partner_id,
                target_org_id,
                status,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                connection_request_id,
                partner["organisation_id"],
                partner_id,
                matched_org["organisation_id"],
                "PENDING_APPROVAL",
                now_iso(),
                now_iso()
            )
        )

        conn.execute(
            """
            UPDATE partners
            SET linked_org_id = ?, connection_status = ?, updated_at = ?
            WHERE partner_id = ?
            """,
            (matched_org["organisation_id"], "REQUESTED", now_iso(), partner_id)
        )

        audit_event(
            conn,
            entity_type="OrgConnectionRequest",
            entity_id=connection_request_id,
            action="CREATE",
            summary=f"Requested Pallet Pro org connection for partner {partner['name']}",
            organisation_id=partner["organisation_id"]
        )

        conn.commit()
        conn.close()

        return jsonify({
            "connection_request_id": connection_request_id,
            "partner_id": partner_id,
            "partner_name": partner["name"],
            "matched_org_id": matched_org["organisation_id"],
            "matched_org_name": matched_org["name"],
            "status": "PENDING_APPROVAL",
            "message": "Matched Pallet Pro partner found. Connection request created."
        }), 201


    @app.get("/organisations/<organisation_id>/org-connection-requests")
    def list_org_connection_requests(organisation_id):
        conn = get_conn()
        ensure_partner_connection_tables(conn)

        incoming_rows = conn.execute(
            """
            SELECT
                ocr.connection_request_id,
                ocr.requesting_org_id,
                ro.name AS requesting_org_name,
                ocr.requesting_partner_id,
                p.name AS partner_name,
                ocr.target_org_id,
                to2.name AS target_org_name,
                ocr.status,
                ocr.created_at,
                ocr.updated_at
            FROM org_connection_requests ocr
            LEFT JOIN organisations ro ON ro.organisation_id = ocr.requesting_org_id
            LEFT JOIN organisations to2 ON to2.organisation_id = ocr.target_org_id
            LEFT JOIN partners p ON p.partner_id = ocr.requesting_partner_id
            WHERE ocr.target_org_id = ?
            ORDER BY ocr.created_at DESC
            """,
            (organisation_id,)
        ).fetchall()

        outgoing_rows = conn.execute(
            """
            SELECT
                ocr.connection_request_id,
                ocr.requesting_org_id,
                ro.name AS requesting_org_name,
                ocr.requesting_partner_id,
                p.name AS partner_name,
                ocr.target_org_id,
                to2.name AS target_org_name,
                ocr.status,
                ocr.created_at,
                ocr.updated_at
            FROM org_connection_requests ocr
            LEFT JOIN organisations ro ON ro.organisation_id = ocr.requesting_org_id
            LEFT JOIN organisations to2 ON to2.organisation_id = ocr.target_org_id
            LEFT JOIN partners p ON p.partner_id = ocr.requesting_partner_id
            WHERE ocr.requesting_org_id = ?
            ORDER BY ocr.created_at DESC
            """,
            (organisation_id,)
        ).fetchall()

        conn.close()

        incoming = []
        for row in incoming_rows:
            d = dict(row)
            d["request_direction"] = "INCOMING"
            d["entry_target_section"] = "connection_requests"
            d["entry_target_entity_type"] = "OrgConnectionRequest"
            d["entry_target_action"] = "review_incoming_connection_request"
            d["highlight_key"] = d["connection_request_id"]
            incoming.append(d)

        outgoing = []
        for row in outgoing_rows:
            d = dict(row)
            d["request_direction"] = "OUTGOING"
            d["entry_target_section"] = "connection_requests"
            d["entry_target_entity_type"] = "OrgConnectionRequest"
            d["entry_target_action"] = "review_outgoing_connection_request"
            d["highlight_key"] = d["connection_request_id"]
            outgoing.append(d)

        return jsonify({
            "organisation_id": organisation_id,
            "incoming_count": len(incoming),
            "outgoing_count": len(outgoing),
            "incoming_requests": incoming,
            "outgoing_requests": outgoing
        }), 200


    @app.post("/org-connection-requests/<connection_request_id>/approve")
    def approve_org_connection_request(connection_request_id):
        conn = get_conn()
        ensure_partner_connection_tables(conn)

        req = conn.execute(
            "SELECT * FROM org_connection_requests WHERE connection_request_id = ?",
            (connection_request_id,)
        ).fetchone()

        if not req:
            conn.close()
            return jsonify({"error": "Connection request not found"}), 404

        if req["status"] != "PENDING_APPROVAL":
            conn.close()
            return jsonify({"error": "Connection request is not pending approval"}), 400

        requesting_org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (req["requesting_org_id"],)
        ).fetchone()

        target_org = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (req["target_org_id"],)
        ).fetchone()

        requesting_partner = conn.execute(
            "SELECT * FROM partners WHERE partner_id = ?",
            (req["requesting_partner_id"],)
        ).fetchone()

        if not requesting_org or not target_org or not requesting_partner:
            conn.close()
            return jsonify({"error": "Connection request references missing records"}), 404

        conn.execute(
            """
            UPDATE org_connection_requests
            SET status = ?, updated_at = ?
            WHERE connection_request_id = ?
            """,
            ("CONNECTED", now_iso(), connection_request_id)
        )

        conn.execute(
            """
            UPDATE partners
            SET linked_org_id = ?, connection_status = ?, updated_at = ?
            WHERE partner_id = ?
            """,
            (req["target_org_id"], "CONNECTED", now_iso(), req["requesting_partner_id"])
        )

        reciprocal_partner = conn.execute(
            """
            SELECT *
            FROM partners
            WHERE organisation_id = ?
              AND (
                    linked_org_id = ?
                    OR LOWER(TRIM(name)) = LOWER(TRIM(?))
                  )
            ORDER BY CASE WHEN linked_org_id = ? THEN 0 ELSE 1 END, created_at DESC
            LIMIT 1
            """,
            (req["target_org_id"], req["requesting_org_id"], requesting_org["name"], req["requesting_org_id"])
        ).fetchone()

        if reciprocal_partner:
            conn.execute(
                """
                UPDATE partners
                SET linked_org_id = ?,
                    connection_status = ?,
                    is_active = 1,
                    updated_at = ?
                WHERE partner_id = ?
                """,
                (req["requesting_org_id"], "CONNECTED", now_iso(), reciprocal_partner["partner_id"])
            )
            reciprocal_partner_id = reciprocal_partner["partner_id"]
        else:
            reciprocal_partner_id = make_id("partner")
            conn.execute(
                """
                INSERT INTO partners (
                    partner_id,
                    organisation_id,
                    name,
                    is_active,
                    is_customer,
                    is_supplier,
                    created_at,
                    linked_org_id,
                    connection_status,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reciprocal_partner_id,
                    req["target_org_id"],
                    requesting_org["name"],
                    1,
                    1,
                    1,
                    now_iso(),
                    req["requesting_org_id"],
                    "CONNECTED",
                    now_iso()
                )
            )

        audit_event(
            conn,
            entity_type="OrgConnectionRequest",
            entity_id=connection_request_id,
            action="APPROVE",
            summary=f"Approved Pallet Pro org connection between {requesting_org['name']} and {target_org['name']}",
            organisation_id=req["target_org_id"]
        )

        audit_event(
            conn,
            entity_type="Partner",
            entity_id=req["requesting_partner_id"],
            action="CONNECT",
            summary=f"Partner connected to Pallet Pro org {target_org['name']}",
            organisation_id=req["requesting_org_id"]
        )

        audit_event(
            conn,
            entity_type="Partner",
            entity_id=reciprocal_partner_id,
            action="CONNECT",
            summary=f"Partner connected to Pallet Pro org {requesting_org['name']}",
            organisation_id=req["target_org_id"]
        )

        conn.commit()
        conn.close()

        return jsonify({
            "connection_request_id": connection_request_id,
            "status": "CONNECTED",
            "requesting_org_id": req["requesting_org_id"],
            "target_org_id": req["target_org_id"],
            "requesting_partner_id": req["requesting_partner_id"],
            "reciprocal_partner_id": reciprocal_partner_id
        }), 200


    @app.post("/org-connection-requests/<connection_request_id>/reject")
    def reject_org_connection_request(connection_request_id):
        conn = get_conn()
        ensure_partner_connection_tables(conn)

        req = conn.execute(
            "SELECT * FROM org_connection_requests WHERE connection_request_id = ?",
            (connection_request_id,)
        ).fetchone()

        if not req:
            conn.close()
            return jsonify({"error": "Connection request not found"}), 404

        if req["status"] != "PENDING_APPROVAL":
            conn.close()
            return jsonify({"error": "Connection request is not pending approval"}), 400

        conn.execute(
            """
            UPDATE org_connection_requests
            SET status = ?, updated_at = ?
            WHERE connection_request_id = ?
            """,
            ("REJECTED", now_iso(), connection_request_id)
        )

        conn.execute(
            """
            UPDATE partners
            SET linked_org_id = NULL,
                connection_status = 'LOCAL_ONLY',
                updated_at = ?
            WHERE partner_id = ?
              AND linked_org_id = ?
              AND COALESCE(connection_status, 'LOCAL_ONLY') = 'REQUESTED'
            """,
            (now_iso(), req["requesting_partner_id"], req["target_org_id"])
        )

        audit_event(
            conn,
            entity_type="OrgConnectionRequest",
            entity_id=connection_request_id,
            action="REJECT",
            summary="Rejected Pallet Pro org connection request",
            organisation_id=req["target_org_id"]
        )

        conn.commit()
        conn.close()

        return jsonify({
            "connection_request_id": connection_request_id,
            "status": "REJECTED"
        }), 200


    # ── Shared transaction disputes (partner-facing admin routes) ──────────────

    @app.get("/organisations/<organisation_id>/shared-transaction-disputes")
    def list_shared_transaction_disputes(organisation_id):
        conn = get_conn()

        rows = conn.execute(
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
                st.shared_status,
                st.dispute_reason_code,
                st.dispute_reason_text,
                st.disputed_by_display_name,
                st.disputed_at,
                st.created_at,
                st.updated_at
            FROM shared_transactions st
            LEFT JOIN organisations oo ON oo.organisation_id = st.origin_org_id
            LEFT JOIN organisations co ON co.organisation_id = st.counterparty_org_id
            LEFT JOIN partners op ON op.partner_id = st.origin_partner_id
            LEFT JOIN partners cp ON cp.partner_id = st.counterparty_partner_id
            WHERE (st.origin_org_id = ? OR st.counterparty_org_id = ?)
              AND st.shared_status = 'DISPUTED'
            ORDER BY COALESCE(st.disputed_at, st.updated_at) DESC
            """,
            (organisation_id, organisation_id)
        ).fetchall()

        conn.close()

        items = []
        for row in rows:
            d = dict(row)
            if d["origin_org_id"] == organisation_id:
                d["perspective_role"] = "DISPATCHING"
                d["lane"] = "outgoing"
                d["counterparty_label"] = d["counterparty_org_name"]
            else:
                d["perspective_role"] = "RECEIVING"
                d["lane"] = "incoming"
                d["counterparty_label"] = d["origin_org_name"]

            d["next_action"] = "admin_resolve"
            d["entry_target_section"] = "shared_transaction_disputes"
            d["entry_target_entity_type"] = "SharedTransaction"
            d["entry_target_action"] = "open_dispute_review"
            d["highlight_key"] = d["shared_transaction_id"]
            items.append(d)

        return jsonify({
            "organisation_id": organisation_id,
            "count": len(items),
            "items": items
        }), 200


    @app.post("/shared-transactions/<shared_transaction_id>/admin-resolve")
    def admin_resolve_shared_transaction(shared_transaction_id):
        body = request.get_json(silent=True) or {}
        organisation_id = body.get("organisation_id")
        resolution_action = (body.get("resolution_action") or "").strip().upper()
        resolved_by_display_name = (body.get("resolved_by_display_name") or "Unknown Admin").strip()
        resolution_notes = (body.get("resolution_notes") or "").strip() or None

        if not organisation_id:
            return jsonify({"error": "organisation_id is required"}), 400
        if resolution_action not in ("KEEP_ORIGINAL", "ACCEPT_PROPOSED_CORRECTION"):
            return jsonify({"error": "resolution_action must be KEEP_ORIGINAL or ACCEPT_PROPOSED_CORRECTION"}), 400

        conn = get_conn()

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

        if st["shared_status"] != "DISPUTED":
            conn.close()
            return jsonify({"error": "Shared transaction is not currently disputed"}), 400

        actor_org_role = "DISPATCHING" if organisation_id == st["origin_org_id"] else "RECEIVING"
        other_org_id = st["counterparty_org_id"] if organisation_id == st["origin_org_id"] else st["origin_org_id"]
        previous_status = st["shared_status"]

        final_quantity = st["quantity"]
        final_reference = st["reference_number"]

        if resolution_action == "ACCEPT_PROPOSED_CORRECTION":
            if st["proposed_quantity"] is not None:
                final_quantity = st["proposed_quantity"]
            if st["proposed_reference_number"] not in (None, ""):
                final_reference = st["proposed_reference_number"]
            resolution_code = "ADMIN_ACCEPTED_PROPOSED_CORRECTION"
            event_action = "ADMIN_RESOLVED_ACCEPT_PROPOSED_CORRECTION"
            summary = f"Org Admin resolved dispute by accepting proposed correction. Final quantity {final_quantity}, reference {final_reference}"
        else:
            resolution_code = "ADMIN_KEPT_ORIGINAL"
            event_action = "ADMIN_RESOLVED_KEEP_ORIGINAL"
            summary = f"Org Admin resolved dispute by keeping original values. Final quantity {final_quantity}, reference {final_reference}"

        if resolution_notes:
            summary = summary + f". Notes: {resolution_notes}"

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
                dispute_reason_code = NULL,
                dispute_reason_text = NULL,
                disputed_by_display_name = NULL,
                disputed_at = NULL,
                resolution_code = ?,
                resolution_notes = ?,
                resolved_by_display_name = ?,
                resolved_at = ?,
                updated_at = ?
            WHERE shared_transaction_id = ?
            """,
            (
                "CONFIRMED",
                final_quantity,
                final_reference,
                resolved_by_display_name,
                now_iso(),
                resolution_code,
                resolution_notes,
                resolved_by_display_name,
                now_iso(),
                now_iso(),
                shared_transaction_id
            )
        )

        record_shared_transaction_event(
            conn=conn,
            shared_transaction_id=shared_transaction_id,
            organisation_id=organisation_id,
            actor_org_role=actor_org_role,
            action=event_action,
            summary=summary,
            previous_status=previous_status,
            new_status="CONFIRMED",
            created_by_display_name=resolved_by_display_name
        )

        audit_event(
            conn,
            entity_type="SharedTransaction",
            entity_id=shared_transaction_id,
            action="ADMIN_RESOLVED",
            summary=summary,
            organisation_id=organisation_id
        )

        audit_event(
            conn,
            entity_type="SharedTransaction",
            entity_id=shared_transaction_id,
            action="ADMIN_RESOLVED",
            summary=summary,
            organisation_id=other_org_id
        )

        conn.commit()
        conn.close()

        return jsonify({
            "shared_transaction_id": shared_transaction_id,
            "shared_status": "CONFIRMED",
            "resolution_action": resolution_action,
            "resolution_code": resolution_code,
            "resolution_notes": resolution_notes,
            "resolved_by_display_name": resolved_by_display_name,
            "quantity": final_quantity,
            "reference_number": final_reference
        }), 200
