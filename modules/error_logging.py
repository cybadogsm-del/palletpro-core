import traceback

from flask import jsonify, request

from db import get_conn, make_id, now_iso


def ensure_error_logging_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS error_events (
        error_event_id TEXT PRIMARY KEY,
        occurred_at TEXT NOT NULL,
        route TEXT,
        http_method TEXT,
        error_type TEXT NOT NULL,
        error_message TEXT NOT NULL,
        traceback_text TEXT,
        organisation_id TEXT,
        user_id TEXT,
        alert_status TEXT NOT NULL DEFAULT 'UNREVIEWED',
        reviewed_by_display_name TEXT,
        reviewed_at TEXT,
        review_notes TEXT
    )
    """)


def log_error_event(
    conn,
    error,
    route=None,
    http_method=None,
    organisation_id=None,
    user_id=None,
):
    error_event_id = make_id("err")
    tb = traceback.format_exc()

    conn.execute(
        """
        INSERT INTO error_events (
            error_event_id,
            occurred_at,
            route,
            http_method,
            error_type,
            error_message,
            traceback_text,
            organisation_id,
            user_id,
            alert_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            error_event_id,
            now_iso(),
            route,
            http_method,
            type(error).__name__,
            str(error),
            tb if tb.strip() != "NoneType: None" else None,
            organisation_id,
            user_id,
            "UNREVIEWED",
        )
    )
    conn.commit()
    return error_event_id


def register_error_logging_routes(app):

    @app.errorhandler(Exception)
    def handle_unhandled_exception(error):
        conn = get_conn()
        try:
            ensure_error_logging_tables(conn)
            error_event_id = log_error_event(
                conn,
                error=error,
                route=request.path,
                http_method=request.method,
            )
        except Exception:
            pass
        finally:
            conn.close()

        return jsonify({
            "error": "INTERNAL_SERVER_ERROR",
            "message": "An unexpected error occurred. It has been logged and the Pallet Pro team has been alerted.",
            "error_event_id": error_event_id if "error_event_id" in dir() else None,
        }), 500

    @app.get("/global-admin/error-log")
    def list_error_log():
        alert_status = request.args.get("alert_status")
        limit = min(int(request.args.get("limit", 50)), 200)

        conn = get_conn()
        ensure_error_logging_tables(conn)

        sql = "SELECT * FROM error_events"
        params = []

        if alert_status:
            sql += " WHERE alert_status = ?"
            params.append(alert_status.upper())

        sql += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        conn.close()

        return jsonify({
            "log_type": "GLOBAL_ADMIN_ERROR_LOG",
            "count": len(rows),
            "filter_alert_status": alert_status,
            "items": [dict(row) for row in rows],
        }), 200

    @app.get("/global-admin/error-alerts")
    def list_error_alerts():
        conn = get_conn()
        ensure_error_logging_tables(conn)

        rows = conn.execute(
            "SELECT * FROM error_events WHERE alert_status = 'UNREVIEWED' ORDER BY occurred_at DESC"
        ).fetchall()

        unreviewed_count = len(rows)
        conn.close()

        return jsonify({
            "alert_type": "GLOBAL_ADMIN_ERROR_ALERTS",
            "unreviewed_count": unreviewed_count,
            "has_unreviewed_alerts": unreviewed_count > 0,
            "items": [dict(row) for row in rows],
        }), 200

    @app.get("/global-admin/error-log/<error_event_id>")
    def get_error_event(error_event_id):
        conn = get_conn()
        ensure_error_logging_tables(conn)

        row = conn.execute(
            "SELECT * FROM error_events WHERE error_event_id = ?",
            (error_event_id,)
        ).fetchone()
        conn.close()

        if not row:
            return jsonify({"error": "Error event not found"}), 404

        return jsonify(dict(row)), 200

    @app.post("/global-admin/error-log/<error_event_id>/review")
    def review_error_event(error_event_id):
        body = request.get_json(silent=True) or {}
        reviewed_by_display_name = (body.get("reviewed_by_display_name") or "Global Admin").strip()
        review_notes = (body.get("review_notes") or "").strip() or None

        conn = get_conn()
        ensure_error_logging_tables(conn)

        row = conn.execute(
            "SELECT * FROM error_events WHERE error_event_id = ?",
            (error_event_id,)
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Error event not found"}), 404

        conn.execute(
            """
            UPDATE error_events
            SET alert_status = 'REVIEWED',
                reviewed_by_display_name = ?,
                reviewed_at = ?,
                review_notes = ?
            WHERE error_event_id = ?
            """,
            (reviewed_by_display_name, now_iso(), review_notes, error_event_id)
        )
        conn.commit()
        conn.close()

        return jsonify({
            "error_event_id": error_event_id,
            "alert_status": "REVIEWED",
            "reviewed_by_display_name": reviewed_by_display_name,
            "review_notes": review_notes,
            "message": "Error event marked as reviewed.",
        }), 200
