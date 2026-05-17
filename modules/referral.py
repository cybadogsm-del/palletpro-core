"""
Referral QR + install tracking module.

Any authenticated user can generate a referral token. Sharing the resulting
URL lets Pallet Pro track where new installs come from.

Flow:
  1. User taps "Recommend Pallet Pro" → POST /referral-qr
     Returns a URL: <INSTALL_BASE>?ref=<token>
     Frontend renders the QR code from that URL.

  2. Recipient scans QR → browser opens GET /install?ref=<token>
     Backend logs a SCAN event. Returns JSON so the Next.js frontend can
     render the install/download page.

  3. Browser fires the PWA beforeinstallprompt or appinstalled event
     → frontend beacons POST /install-events
     Backend logs INSTALL_PROMPT or INSTALLED.

  4. Global Admin reviews GET /global-admin/referral-stats.

GET /install and POST /install-events are public (no auth required).
Their paths must be added to auth._EXEMPT_PATHS.
"""

import os
import secrets

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso

_INSTALL_BASE = os.environ.get("PALLET_PRO_INSTALL_URL", "https://palletpro.app/install")
_VALID_EVENT_TYPES = {"SCAN", "INSTALL_PROMPT", "INSTALLED", "ADD_TO_HOME_SCREEN"}
_GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}


def ensure_referral_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS referral_qr_codes (
        referral_id             TEXT PRIMARY KEY,
        referral_token          TEXT UNIQUE NOT NULL,
        organisation_id         TEXT,
        generated_by_user_id    TEXT,
        generated_by_display_name TEXT NOT NULL,
        label                   TEXT,
        is_active               INTEGER NOT NULL DEFAULT 1,
        created_at              TEXT NOT NULL
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS referral_events (
        event_id        TEXT PRIMARY KEY,
        referral_id     TEXT NOT NULL,
        event_type      TEXT NOT NULL,
        user_agent      TEXT,
        created_at      TEXT NOT NULL
    )
    """)
    conn.commit()


def _build_qr_payload(row, scan_count=None, install_count=None):
    d = dict(row)
    d["install_url"] = f"{_INSTALL_BASE}?ref={row['referral_token']}"
    if scan_count is not None:
        d["scan_count"] = scan_count
    if install_count is not None:
        d["install_count"] = install_count
    return d


def register_referral_routes(app):

    # ------------------------------------------------------------------ #
    # Public routes — no auth token required                              #
    # ------------------------------------------------------------------ #

    @app.get("/install")
    def install_landing():
        """
        Called when a recipient scans a referral QR code.
        Logs a SCAN event and returns JSON so the frontend can render
        the install page. Invalid/inactive tokens still return 200 so
        the landing page always loads — tracking is best-effort.
        """
        token = (request.args.get("ref") or "").strip()

        conn = get_conn()
        ensure_referral_tables(conn)

        referral = None
        if token:
            referral = conn.execute(
                "SELECT * FROM referral_qr_codes WHERE referral_token = ? AND is_active = 1",
                (token,),
            ).fetchone()

        if referral:
            event_id = make_id("revt")
            user_agent = request.headers.get("User-Agent", "")[:512]
            conn.execute(
                """INSERT INTO referral_events (event_id, referral_id, event_type, user_agent, created_at)
                   VALUES (?, ?, 'SCAN', ?, ?)""",
                (event_id, referral["referral_id"], user_agent, now_iso()),
            )
            conn.commit()

        conn.close()

        return jsonify({
            "valid_referral": referral is not None,
            "referral_id": referral["referral_id"] if referral else None,
            "generated_by": referral["generated_by_display_name"] if referral else None,
            "install_base_url": _INSTALL_BASE,
        }), 200


    @app.post("/install-events")
    def log_install_event():
        """
        Client-side beacon. Called by the frontend when the PWA
        beforeinstallprompt or appinstalled event fires.
        Always returns 200 — tracking is best-effort, never blocks the user.
        """
        body = request.get_json(silent=True) or {}
        token = (body.get("ref") or "").strip()
        event_type = (body.get("event_type") or "").strip().upper()

        if event_type not in _VALID_EVENT_TYPES:
            return jsonify({"logged": False, "reason": "Unknown event_type"}), 200

        conn = get_conn()
        ensure_referral_tables(conn)

        referral = None
        if token:
            referral = conn.execute(
                "SELECT * FROM referral_qr_codes WHERE referral_token = ? AND is_active = 1",
                (token,),
            ).fetchone()

        if referral:
            event_id = make_id("revt")
            user_agent = request.headers.get("User-Agent", "")[:512]
            conn.execute(
                """INSERT INTO referral_events (event_id, referral_id, event_type, user_agent, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_id, referral["referral_id"], event_type, user_agent, now_iso()),
            )
            conn.commit()

        conn.close()

        return jsonify({"logged": referral is not None}), 200


    # ------------------------------------------------------------------ #
    # Authenticated routes                                                #
    # ------------------------------------------------------------------ #

    @app.post("/referral-qr")
    def create_referral_qr():
        """
        Generate a referral token. Any authenticated user can call this.
        Returns the full install URL to encode into a QR code client-side.
        """
        current_user = g.current_user
        body = request.get_json(silent=True) or {}
        label = (body.get("label") or "").strip() or None
        organisation_id = body.get("organisation_id") or current_user.get("user_org_id") or None

        conn = get_conn()
        ensure_referral_tables(conn)

        referral_id = make_id("ref")
        referral_token = secrets.token_urlsafe(16)
        ts = now_iso()
        user_id = current_user["user_id"] if current_user["user_id"] != "master" else None

        conn.execute(
            """INSERT INTO referral_qr_codes
               (referral_id, referral_token, organisation_id,
                generated_by_user_id, generated_by_display_name,
                label, is_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (referral_id, referral_token, organisation_id,
             user_id, current_user["display_name"],
             label, ts),
        )

        audit_event(
            conn,
            entity_type="ReferralQR",
            entity_id=referral_id,
            action="CREATE",
            summary=f"{current_user['display_name']} generated referral QR code{' (' + label + ')' if label else ''}.",
            organisation_id=organisation_id,
        )

        conn.commit()
        conn.close()

        return jsonify({
            "referral_id": referral_id,
            "referral_token": referral_token,
            "install_url": f"{_INSTALL_BASE}?ref={referral_token}",
            "organisation_id": organisation_id,
            "generated_by_display_name": current_user["display_name"],
            "label": label,
            "is_active": True,
            "created_at": ts,
            "message": "Referral QR generated. Encode install_url into a QR code to share.",
        }), 201


    @app.get("/referral-qr/<referral_id>")
    def get_referral_qr(referral_id):
        """Get a single referral QR with its scan and install counts."""
        conn = get_conn()
        ensure_referral_tables(conn)

        row = conn.execute(
            "SELECT * FROM referral_qr_codes WHERE referral_id = ?", (referral_id,)
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Referral QR not found"}), 404

        counts = conn.execute(
            """SELECT
                 SUM(CASE WHEN event_type = 'SCAN' THEN 1 ELSE 0 END) AS scan_count,
                 SUM(CASE WHEN event_type IN ('INSTALLED', 'ADD_TO_HOME_SCREEN') THEN 1 ELSE 0 END) AS install_count
               FROM referral_events WHERE referral_id = ?""",
            (referral_id,),
        ).fetchone()

        conn.close()

        return jsonify({
            **_build_qr_payload(row, counts["scan_count"] or 0, counts["install_count"] or 0)
        }), 200


    @app.get("/organisations/<organisation_id>/referral-qrs")
    def list_org_referral_qrs(organisation_id):
        """List all referral QRs generated by users in this organisation."""
        conn = get_conn()
        ensure_referral_tables(conn)

        rows = conn.execute(
            """SELECT q.*,
                 COUNT(CASE WHEN e.event_type = 'SCAN' THEN 1 END) AS scan_count,
                 COUNT(CASE WHEN e.event_type IN ('INSTALLED', 'ADD_TO_HOME_SCREEN') THEN 1 END) AS install_count
               FROM referral_qr_codes q
               LEFT JOIN referral_events e ON e.referral_id = q.referral_id
               WHERE q.organisation_id = ?
               GROUP BY q.referral_id
               ORDER BY q.created_at DESC""",
            (organisation_id,),
        ).fetchall()

        conn.close()

        items = []
        for row in rows:
            d = dict(row)
            d["install_url"] = f"{_INSTALL_BASE}?ref={row['referral_token']}"
            items.append(d)

        return jsonify({
            "organisation_id": organisation_id,
            "count": len(items),
            "referral_qrs": items,
        }), 200


    @app.post("/referral-qr/<referral_id>/deactivate")
    def deactivate_referral_qr(referral_id):
        """Deactivate a referral QR so new scans are no longer tracked."""
        current_user = g.current_user
        conn = get_conn()
        ensure_referral_tables(conn)

        row = conn.execute(
            "SELECT * FROM referral_qr_codes WHERE referral_id = ?", (referral_id,)
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Referral QR not found"}), 404

        if not row["is_active"]:
            conn.close()
            return jsonify({"error": "Referral QR is already inactive"}), 400

        # Allow the generating user, their Org Admin, or Global Admin
        _ORG_ADMIN_ROLES = {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
        is_owner = current_user["user_id"] == row["generated_by_user_id"]
        is_admin = current_user["role"] in _ORG_ADMIN_ROLES
        if not is_owner and not is_admin:
            conn.close()
            return jsonify({"error": "You can only deactivate your own referral QR codes"}), 403

        ts = now_iso()
        conn.execute(
            "UPDATE referral_qr_codes SET is_active = 0 WHERE referral_id = ?", (referral_id,)
        )

        audit_event(
            conn,
            entity_type="ReferralQR",
            entity_id=referral_id,
            action="DEACTIVATE",
            summary=f"Referral QR {referral_id} deactivated by {current_user['display_name']}.",
            organisation_id=row["organisation_id"],
        )

        conn.commit()
        conn.close()

        return jsonify({
            "referral_id": referral_id,
            "is_active": False,
            "deactivated_by": current_user["display_name"],
            "message": "Referral QR deactivated. Existing scans are still visible in stats.",
        }), 200


    @app.get("/global-admin/referral-stats")
    def global_referral_stats():
        """
        Global Admin view of all referral QR codes across all orgs.
        Shows who generated each, scan counts, install counts, and last activity.
        """
        conn = get_conn()
        ensure_referral_tables(conn)

        rows = conn.execute(
            """SELECT
                 q.referral_id,
                 q.referral_token,
                 q.organisation_id,
                 o.name AS organisation_name,
                 q.generated_by_user_id,
                 q.generated_by_display_name,
                 q.label,
                 q.is_active,
                 q.created_at,
                 COUNT(CASE WHEN e.event_type = 'SCAN' THEN 1 END) AS scan_count,
                 COUNT(CASE WHEN e.event_type = 'INSTALL_PROMPT' THEN 1 END) AS install_prompt_count,
                 COUNT(CASE WHEN e.event_type IN ('INSTALLED', 'ADD_TO_HOME_SCREEN') THEN 1 END) AS install_count,
                 MAX(e.created_at) AS last_event_at
               FROM referral_qr_codes q
               LEFT JOIN organisations o ON o.organisation_id = q.organisation_id
               LEFT JOIN referral_events e ON e.referral_id = q.referral_id
               GROUP BY q.referral_id
               ORDER BY q.created_at DESC"""
        ).fetchall()

        total_scans = sum(r["scan_count"] for r in rows)
        total_installs = sum(r["install_count"] for r in rows)

        items = []
        for row in rows:
            d = dict(row)
            d["install_url"] = f"{_INSTALL_BASE}?ref={row['referral_token']}"
            items.append(d)

        conn.close()

        return jsonify({
            "total_referral_qrs": len(items),
            "total_scans": total_scans,
            "total_installs": total_installs,
            "referral_qrs": items,
        }), 200
