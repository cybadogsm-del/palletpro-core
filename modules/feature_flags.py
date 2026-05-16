from flask import jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso


# Modules that can never be disabled — core platform integrity
MODULE_ALWAYS_ON = {
    "system",
    "error_logging",
    "feature_flags",
}

# Map URL path prefixes to module keys (longest match wins)
URL_MODULE_MAP = [
    ("/global-admin/error-log",            "error_logging"),
    ("/global-admin/error-alerts",         "error_logging"),
    ("/global-admin/module-flags",         "feature_flags"),
    ("/global-admin/billing-export",       "billing"),
    ("/global-admin/data-retention",       "data_retention"),
    ("/global-admin/login-integrity",      "login_integrity"),
    ("/global-admin/login-integrity-actions", "login_integrity"),
    ("/global-admin/session-expiry-sweep", "user_management"),
    ("/global-admin/temporary-user-expiry-sweep", "subscription"),
    ("/global-admin/access-operations",   "user_management"),
    ("/global-admin/system-control-panel","system"),
    ("/global-admin/pricing",             "subscription"),
    ("/global-admin/organisations",        "subscription"),
    ("/global-admin/users",               "user_management"),
    ("/global-admin/transaction-summary", "reporting"),
    ("/global-admin",                      "global_admin"),
    ("/sessions",                          "user_management"),
    ("/users",                             "user_management"),
    ("/qr-handoff",                        "qr_handoff"),
    ("/shared-transactions",               "shared_transactions"),
    ("/opening-balances",                  "transactions"),
    ("/transactions",                      "transactions"),
    ("/partner-addresses",                 "partners"),
    ("/partners",                          "partners"),
    ("/depots",                            "depots"),
    ("/location-update-requests",          "partners"),
    ("/org-connection-requests",           "shared_transactions"),
    ("/pending-approval-entries",          "pending_approval"),
    ("/pricing-table",                     "subscription"),
    ("/organisations",                     "organisations"),
    ("/health",                            "system"),
    ("/api/health",                        "system"),
    ("/api/system",                        "system"),
    ("/constitution",                      "system"),
    ("/api/constitution",                  "system"),
    ("/",                                  "system"),
]

ALL_MODULE_KEYS = sorted({m for _, m in URL_MODULE_MAP} | MODULE_ALWAYS_ON)


def ensure_feature_flag_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS module_flags (
        module_key TEXT PRIMARY KEY,
        is_enabled INTEGER NOT NULL DEFAULT 1,
        disabled_reason TEXT,
        disabled_by_display_name TEXT,
        enabled_by_display_name TEXT,
        updated_at TEXT NOT NULL
    )
    """)


def is_module_enabled(conn, module_key):
    if module_key in MODULE_ALWAYS_ON:
        return True
    row = conn.execute(
        "SELECT is_enabled FROM module_flags WHERE module_key = ?",
        (module_key,)
    ).fetchone()
    # No row = never explicitly set = enabled by default
    return row is None or bool(row["is_enabled"])


def resolve_module_for_path(path):
    for prefix, module_key in URL_MODULE_MAP:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            return module_key
    return None


def register_feature_flag_routes(app):

    @app.before_request
    def check_module_flag():
        module_key = resolve_module_for_path(request.path)
        if not module_key:
            return None

        if module_key in MODULE_ALWAYS_ON:
            return None

        conn = get_conn()
        try:
            ensure_feature_flag_tables(conn)
            enabled = is_module_enabled(conn, module_key)
            if not enabled:
                row = conn.execute(
                    "SELECT disabled_reason, disabled_by_display_name, updated_at FROM module_flags WHERE module_key = ?",
                    (module_key,)
                ).fetchone()
                reason = row["disabled_reason"] if row else None
                disabled_by = row["disabled_by_display_name"] if row else None
                return jsonify({
                    "error": "MODULE_OFFLINE",
                    "module": module_key,
                    "dialog": {
                        "title": "This feature is temporarily offline",
                        "message": (
                            f"The '{module_key}' module has been taken offline by a Pallet Pro administrator."
                            + (f" Reason: {reason}" if reason else "")
                            + " Please try again later or contact your administrator."
                        ),
                        "disabled_by": disabled_by,
                        "primary_action": {
                            "label": "Go to Dashboard",
                            "route": "/",
                            "action_type": "NAVIGATE",
                        },
                    },
                }), 503
        finally:
            conn.close()

        return None

    @app.get("/global-admin/module-flags")
    def list_module_flags():
        conn = get_conn()
        ensure_feature_flag_tables(conn)

        rows = conn.execute("SELECT * FROM module_flags").fetchall()
        flag_map = {row["module_key"]: dict(row) for row in rows}

        items = []
        for key in ALL_MODULE_KEYS:
            if key in flag_map:
                items.append(flag_map[key])
            else:
                items.append({
                    "module_key": key,
                    "is_enabled": 1,
                    "disabled_reason": None,
                    "disabled_by_display_name": None,
                    "enabled_by_display_name": None,
                    "updated_at": None,
                    "always_on": key in MODULE_ALWAYS_ON,
                })

        for item in items:
            item.setdefault("always_on", item["module_key"] in MODULE_ALWAYS_ON)

        conn.close()

        return jsonify({
            "dashboard_type": "GLOBAL_ADMIN_MODULE_FLAGS",
            "total_modules": len(items),
            "online": sum(1 for i in items if i["is_enabled"]),
            "offline": sum(1 for i in items if not i["is_enabled"]),
            "items": items,
        }), 200

    @app.post("/global-admin/module-flags/<module_key>/disable")
    def disable_module(module_key):
        if module_key in MODULE_ALWAYS_ON:
            return jsonify({
                "error": "Cannot disable this module",
                "message": f"'{module_key}' is a core system module and cannot be taken offline.",
                "always_on_modules": sorted(MODULE_ALWAYS_ON),
            }), 400

        if module_key not in ALL_MODULE_KEYS:
            return jsonify({
                "error": "Unknown module",
                "known_modules": ALL_MODULE_KEYS,
            }), 404

        body = request.get_json(silent=True) or {}
        disabled_by = (body.get("disabled_by_display_name") or "Global Admin").strip()
        reason = (body.get("reason") or "").strip() or None

        conn = get_conn()
        ensure_feature_flag_tables(conn)

        conn.execute("""
            INSERT INTO module_flags (module_key, is_enabled, disabled_reason, disabled_by_display_name, updated_at)
            VALUES (?, 0, ?, ?, ?)
            ON CONFLICT(module_key) DO UPDATE SET
                is_enabled = 0,
                disabled_reason = excluded.disabled_reason,
                disabled_by_display_name = excluded.disabled_by_display_name,
                updated_at = excluded.updated_at
        """, (module_key, reason, disabled_by, now_iso()))

        audit_event(
            conn,
            entity_type="ModuleFlag",
            entity_id=module_key,
            action="DISABLE",
            summary=f"Module '{module_key}' taken offline by {disabled_by}. Reason: {reason or 'not specified'}.",
        )

        conn.commit()
        conn.close()

        return jsonify({
            "module_key": module_key,
            "is_enabled": False,
            "disabled_by_display_name": disabled_by,
            "disabled_reason": reason,
            "message": f"Module '{module_key}' is now offline.",
        }), 200

    @app.post("/global-admin/module-flags/<module_key>/enable")
    def enable_module(module_key):
        if module_key not in ALL_MODULE_KEYS:
            return jsonify({
                "error": "Unknown module",
                "known_modules": ALL_MODULE_KEYS,
            }), 404

        body = request.get_json(silent=True) or {}
        enabled_by = (body.get("enabled_by_display_name") or "Global Admin").strip()

        conn = get_conn()
        ensure_feature_flag_tables(conn)

        conn.execute("""
            INSERT INTO module_flags (module_key, is_enabled, disabled_reason, enabled_by_display_name, updated_at)
            VALUES (?, 1, NULL, ?, ?)
            ON CONFLICT(module_key) DO UPDATE SET
                is_enabled = 1,
                disabled_reason = NULL,
                enabled_by_display_name = excluded.enabled_by_display_name,
                updated_at = excluded.updated_at
        """, (module_key, enabled_by, now_iso()))

        audit_event(
            conn,
            entity_type="ModuleFlag",
            entity_id=module_key,
            action="ENABLE",
            summary=f"Module '{module_key}' brought back online by {enabled_by}.",
        )

        conn.commit()
        conn.close()

        return jsonify({
            "module_key": module_key,
            "is_enabled": True,
            "enabled_by_display_name": enabled_by,
            "message": f"Module '{module_key}' is back online.",
        }), 200
