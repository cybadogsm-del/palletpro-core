"""
modules/route_intelligence.py — Route Intelligence brick

Covers:
  - DB setup: user_route_defaults, user_dropdown_preferences tables
  - Helpers: upsert_route_learning (called by transaction POST after ledger commit)
  - Routes:
      GET  /users/<id>/morning-sync
      GET  /users/<id>/route-history
      GET  /users/<id>/dropdown-preferences
      PATCH /users/<id>/dropdown-preferences
"""

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso


# ── Valid action values ────────────────────────────────────────────────────────

VALID_ACTIONS = {"Pickup", "Dropoff", "Exchange"}

ACTION_TO_DIRECTION = {
    "Pickup": "IN",
    "Dropoff": "OUT",
    "Exchange": "EXCHANGE",
}

_ROUTE_ADMIN_ROLES = {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
_GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}


# ── Schema ─────────────────────────────────────────────────────────────────────

def ensure_route_intelligence_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_route_defaults (
        route_default_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        organisation_id TEXT NOT NULL,
        partner_address_id TEXT NOT NULL,
        day_of_week INTEGER NOT NULL,
        preferred_resource_id TEXT,
        preferred_action TEXT,
        visit_count INTEGER NOT NULL DEFAULT 1,
        last_visited_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (user_id, partner_address_id, day_of_week)
    )
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_dropdown_preferences (
        dropdown_preference_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        organisation_id TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 0,
        display_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (user_id, entity_type, target_id)
    )
    """)


# ── Learning hook (called after every successful transaction post) ─────────────

def upsert_route_learning(conn, user_id, organisation_id, partner_address_id,
                          resource_id, action, transaction_created_at):
    """
    Increments visit_count for the (user, site, day_of_week) combination.
    Updates preferred_resource_id and preferred_action to the most recently used values.
    Inserts a new row if this combination has never been seen before.
    Called inside the transaction POST after ledger commit — same connection, before close.
    """
    if not partner_address_id:
        return

    ensure_route_intelligence_tables(conn)

    from datetime import datetime
    try:
        dt = datetime.fromisoformat(transaction_created_at)
        day_of_week = dt.weekday()  # 0=Monday … 6=Sunday
    except Exception:
        return

    now = now_iso()

    existing = conn.execute(
        """
        SELECT route_default_id, visit_count
        FROM user_route_defaults
        WHERE user_id = ? AND partner_address_id = ? AND day_of_week = ?
        """,
        (user_id, partner_address_id, day_of_week)
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE user_route_defaults
            SET visit_count = visit_count + 1,
                preferred_resource_id = COALESCE(?, preferred_resource_id),
                preferred_action = COALESCE(?, preferred_action),
                last_visited_at = ?,
                updated_at = ?
            WHERE route_default_id = ?
            """,
            (resource_id, action, transaction_created_at, now, existing["route_default_id"])
        )
    else:
        conn.execute(
            """
            INSERT INTO user_route_defaults (
                route_default_id,
                user_id,
                organisation_id,
                partner_address_id,
                day_of_week,
                preferred_resource_id,
                preferred_action,
                visit_count,
                last_visited_at,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                make_id("urd"),
                user_id,
                organisation_id,
                partner_address_id,
                day_of_week,
                resource_id,
                action,
                transaction_created_at,
                now,
                now,
            )
        )


# ── Access policy ─────────────────────────────────────────────────────────────

def _get_route_target_user(conn, user_id):
    from modules.users import ensure_user_access_tables
    ensure_user_access_tables(conn)

    return conn.execute(
        "SELECT user_id, organisation_id FROM user_accounts WHERE user_id = ?",
        (user_id,)
    ).fetchone()


def _route_access_error(target_user):
    if not target_user:
        return jsonify({"error": "User not found"}), 404

    current_user = g.current_user
    current_user_id = current_user.get("user_id")
    current_role = current_user.get("role")
    current_org_id = current_user.get("user_org_id")
    target_org_id = target_user["organisation_id"]

    if current_user_id == target_user["user_id"]:
        return None

    if current_role in _GLOBAL_ADMIN_ROLES:
        return None

    if current_role in _ROUTE_ADMIN_ROLES and current_org_id == target_org_id:
        return None

    return jsonify({
        "error": "INSUFFICIENT_ROLE",
        "message": "Route intelligence access requires the target user, same-organisation Org Admin, Global Admin, or Super Global Admin.",
        "your_role": current_role,
    }), 403


# ── Route registration ─────────────────────────────────────────────────────────

def register_route_intelligence_routes(app):

    @app.get("/users/<user_id>/morning-sync")
    def get_morning_sync(user_id):
        """
        Called by the device at morning authentication check-in.
        Returns the user's predicted route pool for today (based on historical
        visit patterns for this day of week), plus dropdown configurations.

        No parameters needed — everything is resolved from the user's profile.
        The pool starts empty for new users and grows with every submitted transaction.
        """
        from datetime import datetime

        conn = get_conn()
        ensure_route_intelligence_tables(conn)

        user = _get_route_target_user(conn, user_id)

        access_error = _route_access_error(user)
        if access_error:
            conn.close()
            return access_error

        organisation_id = user["organisation_id"]
        user_profile = conn.execute(
            "SELECT default_depot_id FROM user_accounts WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        default_depot_id = user_profile["default_depot_id"] if user_profile else None

        now_dt = datetime.fromisoformat(now_iso())
        today_dow = now_dt.weekday()  # 0=Monday … 6=Sunday
        sync_date = now_dt.date().isoformat()

        # ── Predicted route pool ──────────────────────────────────────────────
        # Sites this user has visited on this day of the week, ordered by
        # visit frequency (most frequent first). Starts empty, grows over time.
        route_rows = conn.execute(
            """
            SELECT
                urd.route_default_id,
                urd.partner_address_id,
                urd.preferred_resource_id,
                urd.preferred_action,
                urd.visit_count,
                urd.last_visited_at,
                pa.label,
                pa.address_line_1,
                pa.suburb,
                pa.state,
                pa.latitude,
                pa.longitude,
                pa.entry_heading,
                pa.gate_number,
                pa.entry_instructions,
                pa.partner_id,
                p.name AS partner_name,
                r.name AS resource_name
            FROM user_route_defaults urd
            LEFT JOIN partner_addresses pa
                ON pa.partner_address_id = urd.partner_address_id
            LEFT JOIN partners p
                ON p.partner_id = pa.partner_id
            LEFT JOIN resources r
                ON r.resource_id = urd.preferred_resource_id
            WHERE urd.user_id = ?
              AND urd.organisation_id = ?
              AND urd.day_of_week = ?
              AND pa.is_active = 1
            ORDER BY urd.visit_count DESC, urd.last_visited_at DESC
            """,
            (user_id, organisation_id, today_dow)
        ).fetchall()

        # ── Org-level resource defaults (fallback when user has no preference) ─
        org_resources = conn.execute(
            """
            SELECT resource_id, name
            FROM resources
            WHERE organisation_id = ? AND is_active = 1
            ORDER BY name ASC
            """,
            (organisation_id,)
        ).fetchall()

        # ── User dropdown preferences ─────────────────────────────────────────
        user_prefs = conn.execute(
            """
            SELECT entity_type, target_id, is_default, display_order
            FROM user_dropdown_preferences
            WHERE user_id = ? AND organisation_id = ?
            ORDER BY entity_type, display_order ASC
            """,
            (user_id, organisation_id)
        ).fetchall()

        conn.close()

        # Build resource preference map
        pref_map = {}
        for pref in user_prefs:
            pref_map.setdefault(pref["entity_type"], []).append(dict(pref))

        # Resolve ordered resource list (user preference order → fallback to alpha)
        resource_prefs = pref_map.get("resource", [])
        pref_resource_ids = [p["target_id"] for p in resource_prefs]
        default_resource_id = next(
            (p["target_id"] for p in resource_prefs if p["is_default"]),
            org_resources[0]["resource_id"] if org_resources else None
        )

        # Resources ordered: user-preferred first, then remaining alphabetically
        preferred_resources = [r for r in org_resources if r["resource_id"] in pref_resource_ids]
        preferred_resources.sort(key=lambda r: pref_resource_ids.index(r["resource_id"]))
        remaining_resources = [r for r in org_resources if r["resource_id"] not in pref_resource_ids]
        ordered_resources = preferred_resources + remaining_resources

        # Action ordering: user preference → default alphabetical
        action_prefs = pref_map.get("action", [])
        pref_actions = [p["target_id"] for p in action_prefs]
        default_action = next(
            (p["target_id"] for p in action_prefs if p["is_default"]),
            "Dropoff"
        )
        all_actions = list(VALID_ACTIONS)
        ordered_actions = pref_actions + [a for a in all_actions if a not in pref_actions]

        # Build route pool
        route_pool = []
        for row in route_rows:
            # Per-site prefill: user learned preference → org site default → system default
            prefill_resource_id = row["preferred_resource_id"] or default_resource_id
            prefill_action = row["preferred_action"] or default_action

            route_pool.append({
                "partner_address_id": row["partner_address_id"],
                "partner_id": row["partner_id"],
                "partner_name": row["partner_name"],
                "label": row["label"],
                "address_line_1": row["address_line_1"],
                "suburb": row["suburb"],
                "state": row["state"],
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "entry_heading": row["entry_heading"],
                "gate_number": row["gate_number"],
                "entry_instructions": row["entry_instructions"],
                "visit_count": row["visit_count"],
                "last_visited_at": row["last_visited_at"],
                "prefill": {
                    "resource_id": prefill_resource_id,
                    "resource_name": row["resource_name"],
                    "action": prefill_action,
                    "direction": ACTION_TO_DIRECTION.get(prefill_action, "OUT"),
                },
            })

        return jsonify({
            "sync_date": sync_date,
            "user_id": user_id,
            "organisation_id": organisation_id,
            "default_depot_id": default_depot_id,
            "day_of_week": today_dow,
            "day_name": ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"][today_dow],
            "predicted_route_pool": route_pool,
            "route_pool_count": len(route_pool),
            "dropdown_config": {
                "resources": {
                    "default_id": default_resource_id,
                    "ordered_options": [
                        {"resource_id": r["resource_id"], "name": r["name"]}
                        for r in ordered_resources
                    ],
                },
                "actions": {
                    "default": default_action,
                    "ordered_options": ordered_actions,
                },
            },
        }), 200


    @app.get("/users/<user_id>/route-history")
    def get_user_route_history(user_id):
        """
        Returns the full learned route history for a user — all sites,
        all days of week, all visit counts. Useful for Org Admin to review
        what patterns the system has learned.
        """
        conn = get_conn()
        ensure_route_intelligence_tables(conn)

        user = _get_route_target_user(conn, user_id)

        access_error = _route_access_error(user)
        if access_error:
            conn.close()
            return access_error

        rows = conn.execute(
            """
            SELECT
                urd.route_default_id,
                urd.partner_address_id,
                urd.day_of_week,
                urd.preferred_resource_id,
                urd.preferred_action,
                urd.visit_count,
                urd.last_visited_at,
                urd.created_at,
                pa.label,
                pa.suburb,
                pa.state,
                p.name AS partner_name,
                r.name AS resource_name
            FROM user_route_defaults urd
            LEFT JOIN partner_addresses pa
                ON pa.partner_address_id = urd.partner_address_id
            LEFT JOIN partners p
                ON p.partner_id = pa.partner_id
            LEFT JOIN resources r
                ON r.resource_id = urd.preferred_resource_id
            WHERE urd.user_id = ?
              AND urd.organisation_id = ?
            ORDER BY urd.visit_count DESC, urd.day_of_week ASC
            """,
            (user_id, user["organisation_id"])
        ).fetchall()

        conn.close()

        day_names = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        items = []
        for row in rows:
            d = dict(row)
            d["day_name"] = day_names[d["day_of_week"]]
            items.append(d)

        return jsonify({
            "user_id": user_id,
            "organisation_id": user["organisation_id"],
            "count": len(items),
            "items": items,
        }), 200


    @app.get("/users/<user_id>/dropdown-preferences")
    def get_user_dropdown_preferences(user_id):
        conn = get_conn()
        ensure_route_intelligence_tables(conn)

        user = _get_route_target_user(conn, user_id)

        access_error = _route_access_error(user)
        if access_error:
            conn.close()
            return access_error

        rows = conn.execute(
            """
            SELECT dropdown_preference_id, entity_type, target_id, is_default, display_order
            FROM user_dropdown_preferences
            WHERE user_id = ? AND organisation_id = ?
            ORDER BY entity_type, display_order ASC
            """,
            (user_id, user["organisation_id"])
        ).fetchall()

        conn.close()

        return jsonify({
            "user_id": user_id,
            "organisation_id": user["organisation_id"],
            "count": len(rows),
            "items": [dict(r) for r in rows],
        }), 200


    @app.patch("/users/<user_id>/dropdown-preferences")
    def update_user_dropdown_preferences(user_id):
        """
        Replaces the user's dropdown preference ordering for a given entity_type.
        Body: { "entity_type": "resource", "ordered_ids": ["res_aaa", "res_bbb"], "default_id": "res_aaa" }
        """
        body = request.get_json(silent=True) or {}
        entity_type = (body.get("entity_type") or "").strip().lower()
        ordered_ids = body.get("ordered_ids")
        default_id = body.get("default_id")

        if not entity_type:
            return jsonify({"error": "entity_type is required"}), 400
        if not isinstance(ordered_ids, list) or not ordered_ids:
            return jsonify({"error": "ordered_ids must be a non-empty list"}), 400

        conn = get_conn()
        ensure_route_intelligence_tables(conn)

        user = _get_route_target_user(conn, user_id)

        access_error = _route_access_error(user)
        if access_error:
            conn.close()
            return access_error

        organisation_id = user["organisation_id"]
        now = now_iso()

        # Delete existing preferences for this entity_type and replace
        conn.execute(
            "DELETE FROM user_dropdown_preferences WHERE user_id = ? AND organisation_id = ? AND entity_type = ?",
            (user_id, organisation_id, entity_type)
        )

        for idx, target_id in enumerate(ordered_ids):
            conn.execute(
                """
                INSERT INTO user_dropdown_preferences (
                    dropdown_preference_id,
                    user_id,
                    organisation_id,
                    entity_type,
                    target_id,
                    is_default,
                    display_order,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    make_id("udp"),
                    user_id,
                    organisation_id,
                    entity_type,
                    target_id,
                    1 if target_id == default_id else 0,
                    idx,
                    now,
                    now,
                )
            )

        audit_event(
            conn,
            entity_type="UserAccount",
            entity_id=user_id,
            action="DROPDOWN_PREFERENCES_UPDATED",
            summary=f"Dropdown preferences updated for entity type: {entity_type}",
            organisation_id=organisation_id,
        )

        conn.commit()
        conn.close()

        return jsonify({
            "user_id": user_id,
            "entity_type": entity_type,
            "ordered_ids": ordered_ids,
            "default_id": default_id,
            "count": len(ordered_ids),
        }), 200
