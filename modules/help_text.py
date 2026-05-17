"""
Help text module.

Global Admin maintains a library of context-keyed help snippets.
The frontend fetches a snippet by its context_key at runtime and shows
it as a tooltip, drawer, or guidance panel — no app release needed to
update help content.

Naming convention for context_key:
  <module>.<element>   e.g.  "transaction.partner_field"
                              "stocktake.quantity_hint"
                              "resource_loss.loss_type"
                              "depot.opening_balance"

Any authenticated user can read help text.
Only Global Admin can create, update, or delete entries.
"""

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso

_GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}


def ensure_help_text_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS help_texts (
        help_text_id            TEXT PRIMARY KEY,
        context_key             TEXT UNIQUE NOT NULL,
        title                   TEXT NOT NULL,
        body                    TEXT NOT NULL,
        is_active               INTEGER NOT NULL DEFAULT 1,
        created_by_display_name TEXT NOT NULL,
        updated_by_display_name TEXT,
        created_at              TEXT NOT NULL,
        updated_at              TEXT NOT NULL
    )
    """)
    conn.commit()


def register_help_text_routes(app):

    # ------------------------------------------------------------------ #
    # Read — any authenticated user                                       #
    # ------------------------------------------------------------------ #

    @app.get("/help-text/<context_key>")
    def get_help_text(context_key):
        """
        Fetch a single help snippet by its context key.
        Returns 404 if the key doesn't exist or is inactive.
        The frontend calls this on-demand when a user taps a help icon.
        """
        conn = get_conn()
        ensure_help_text_tables(conn)

        row = conn.execute(
            """SELECT help_text_id, context_key, title, body, updated_at
               FROM help_texts
               WHERE context_key = ? AND is_active = 1""",
            (context_key,),
        ).fetchone()
        conn.close()

        if not row:
            return jsonify({"error": "Help text not found for this context"}), 404

        return jsonify(dict(row)), 200


    @app.post("/help-text/batch")
    def get_help_text_batch():
        """
        Fetch multiple help snippets in one call.
        Body: { "context_keys": ["transaction.partner_field", "stocktake.quantity_hint"] }
        Returns a map of context_key → snippet (missing/inactive keys are omitted).
        The frontend can pre-fetch an entire screen's help content in one request.
        """
        body = request.get_json(silent=True) or {}
        context_keys = body.get("context_keys") or []

        if not isinstance(context_keys, list):
            return jsonify({"error": "context_keys must be a list"}), 400
        if len(context_keys) > 100:
            return jsonify({"error": "Maximum 100 context_keys per batch"}), 400

        # Deduplicate and validate
        context_keys = [str(k).strip() for k in context_keys if k]
        context_keys = list(dict.fromkeys(context_keys))  # preserves order, deduplicates

        if not context_keys:
            return jsonify({"help_texts": {}}), 200

        conn = get_conn()
        ensure_help_text_tables(conn)

        placeholders = ",".join("?" * len(context_keys))
        rows = conn.execute(
            f"""SELECT context_key, title, body, updated_at
                FROM help_texts
                WHERE context_key IN ({placeholders}) AND is_active = 1""",
            context_keys,
        ).fetchall()
        conn.close()

        result = {row["context_key"]: dict(row) for row in rows}
        return jsonify({"help_texts": result, "count": len(result)}), 200


    # ------------------------------------------------------------------ #
    # Manage — Global Admin only                                          #
    # ------------------------------------------------------------------ #

    @app.post("/global-admin/help-text")
    def create_help_text():
        """
        Create a new help text entry.
        context_key must be unique — use <module>.<element> convention.
        """
        current_user = g.current_user
        body = request.get_json(silent=True) or {}

        context_key = (body.get("context_key") or "").strip()
        title = (body.get("title") or "").strip()
        help_body = (body.get("body") or "").strip()

        if not context_key:
            return jsonify({"error": "context_key is required"}), 400
        if not title:
            return jsonify({"error": "title is required"}), 400
        if not help_body:
            return jsonify({"error": "body is required"}), 400

        conn = get_conn()
        ensure_help_text_tables(conn)

        existing = conn.execute(
            "SELECT help_text_id FROM help_texts WHERE context_key = ?",
            (context_key,),
        ).fetchone()
        if existing:
            conn.close()
            return jsonify({
                "error": f"A help text entry already exists for context_key '{context_key}'",
                "help_text_id": existing["help_text_id"],
            }), 409

        ts = now_iso()
        help_text_id = make_id("help")

        conn.execute(
            """INSERT INTO help_texts
               (help_text_id, context_key, title, body, is_active,
                created_by_display_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
            (help_text_id, context_key, title, help_body,
             current_user["display_name"], ts, ts),
        )

        audit_event(
            conn,
            entity_type="HelpText",
            entity_id=help_text_id,
            action="CREATE",
            summary=f"{current_user['display_name']} created help text '{context_key}'.",
            organisation_id=None,
        )

        conn.commit()
        conn.close()

        return jsonify({
            "help_text_id": help_text_id,
            "context_key": context_key,
            "title": title,
            "body": help_body,
            "is_active": True,
            "created_by": current_user["display_name"],
            "created_at": ts,
        }), 201


    @app.get("/global-admin/help-text")
    def list_help_texts():
        """List all help text entries, including inactive ones."""
        is_active_filter = request.args.get("is_active")

        conn = get_conn()
        ensure_help_text_tables(conn)

        sql = "SELECT * FROM help_texts"
        params = []

        if is_active_filter is not None:
            sql += " WHERE is_active = ?"
            params.append(1 if is_active_filter.lower() in ("1", "true", "yes") else 0)

        sql += " ORDER BY context_key ASC"

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        return jsonify({
            "count": len(rows),
            "help_texts": [dict(r) for r in rows],
        }), 200


    @app.get("/global-admin/help-text/<help_text_id>")
    def get_help_text_by_id(help_text_id):
        """Get a single help text entry by ID (includes inactive)."""
        conn = get_conn()
        ensure_help_text_tables(conn)

        row = conn.execute(
            "SELECT * FROM help_texts WHERE help_text_id = ?", (help_text_id,)
        ).fetchone()
        conn.close()

        if not row:
            return jsonify({"error": "Help text not found"}), 404

        return jsonify(dict(row)), 200


    @app.patch("/global-admin/help-text/<help_text_id>")
    def update_help_text(help_text_id):
        """
        Update a help text entry.
        All fields are optional — only supplied fields are changed.
        context_key cannot be changed (it is the stable reference used
        by frontend code; rename by deleting and recreating instead).
        """
        current_user = g.current_user
        body = request.get_json(silent=True) or {}

        conn = get_conn()
        ensure_help_text_tables(conn)

        row = conn.execute(
            "SELECT * FROM help_texts WHERE help_text_id = ?", (help_text_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Help text not found"}), 404

        title = (body.get("title") or "").strip() or row["title"]
        help_body = (body.get("body") or "").strip() or row["body"]
        is_active = body.get("is_active")
        if is_active is None:
            is_active = bool(row["is_active"])
        else:
            is_active = bool(is_active)

        ts = now_iso()
        conn.execute(
            """UPDATE help_texts
               SET title = ?, body = ?, is_active = ?,
                   updated_by_display_name = ?, updated_at = ?
               WHERE help_text_id = ?""",
            (title, help_body, 1 if is_active else 0,
             current_user["display_name"], ts, help_text_id),
        )

        audit_event(
            conn,
            entity_type="HelpText",
            entity_id=help_text_id,
            action="UPDATE",
            summary=(
                f"{current_user['display_name']} updated help text "
                f"'{row['context_key']}' (is_active={is_active})."
            ),
            organisation_id=None,
        )

        conn.commit()
        conn.close()

        return jsonify({
            "help_text_id": help_text_id,
            "context_key": row["context_key"],
            "title": title,
            "body": help_body,
            "is_active": is_active,
            "updated_by": current_user["display_name"],
            "updated_at": ts,
        }), 200


    @app.delete("/global-admin/help-text/<help_text_id>")
    def delete_help_text(help_text_id):
        """
        Permanently delete a help text entry.
        Prefer setting is_active=false to preserve audit history.
        """
        current_user = g.current_user

        conn = get_conn()
        ensure_help_text_tables(conn)

        row = conn.execute(
            "SELECT * FROM help_texts WHERE help_text_id = ?", (help_text_id,)
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Help text not found"}), 404

        audit_event(
            conn,
            entity_type="HelpText",
            entity_id=help_text_id,
            action="DELETE",
            summary=(
                f"{current_user['display_name']} permanently deleted help text "
                f"'{row['context_key']}'."
            ),
            organisation_id=None,
        )

        conn.execute(
            "DELETE FROM help_texts WHERE help_text_id = ?", (help_text_id,)
        )

        conn.commit()
        conn.close()

        return jsonify({
            "help_text_id": help_text_id,
            "context_key": row["context_key"],
            "deleted": True,
            "message": "Help text permanently deleted.",
        }), 200
