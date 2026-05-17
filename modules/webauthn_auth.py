import base64
import json
import os
from datetime import datetime, timedelta, timezone

from flask import g, jsonify, request

from audit import audit_event
from db import get_conn, make_id, now_iso

try:
    import webauthn
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json
    from webauthn.helpers.cose import COSEAlgorithmIdentifier
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )
    _WEBAUTHN_AVAILABLE = True
except ImportError:
    _WEBAUTHN_AVAILABLE = False


RP_ID = os.environ.get("PALLET_PRO_RP_ID", "localhost")
RP_NAME = "Pallet Pro"
RP_ORIGIN = os.environ.get("PALLET_PRO_RP_ORIGIN", "http://localhost:3000")

_CHALLENGE_TTL_SECONDS = 300  # 5 minutes


def ensure_webauthn_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS webauthn_credentials (
        credential_id          TEXT PRIMARY KEY,
        user_id                TEXT NOT NULL,
        credential_id_b64      TEXT NOT NULL UNIQUE,
        public_key_b64         TEXT NOT NULL,
        sign_count             INTEGER NOT NULL DEFAULT 0,
        aaguid                 TEXT,
        device_label           TEXT,
        created_at             TEXT NOT NULL,
        last_used_at           TEXT
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS webauthn_challenges (
        challenge_id    TEXT PRIMARY KEY,
        user_id         TEXT NOT NULL,
        challenge_b64   TEXT NOT NULL,
        purpose         TEXT NOT NULL,
        expires_at      TEXT NOT NULL,
        created_at      TEXT NOT NULL
    )
    """)
    conn.commit()


def _unavailable():
    return jsonify({
        "error": "WEBAUTHN_UNAVAILABLE",
        "message": "Install the 'webauthn' package to enable biometric login.",
    }), 503


def register_webauthn_routes(app):

    @app.post("/auth/webauthn/register/begin")
    def webauthn_register_begin():
        """
        Start passkey/biometric registration. Requires an active session.
        Returns PublicKeyCredentialCreationOptions for the browser WebAuthn API.
        """
        if not _WEBAUTHN_AVAILABLE:
            return _unavailable()

        current_user = g.current_user
        if current_user["user_id"] == "master":
            return jsonify({"error": "Master key user cannot register passkeys"}), 400

        body = request.get_json(silent=True) or {}
        device_label = (body.get("device_label") or "").strip() or None

        conn = get_conn()
        ensure_webauthn_tables(conn)

        user = conn.execute(
            "SELECT * FROM user_accounts WHERE user_id = ?",
            (current_user["user_id"],),
        ).fetchone()
        if not user:
            conn.close()
            return jsonify({"error": "User not found"}), 404

        # Collect already-registered credential IDs to exclude them
        existing_creds = conn.execute(
            "SELECT credential_id_b64 FROM webauthn_credentials WHERE user_id = ?",
            (current_user["user_id"],),
        ).fetchall()
        exclude_credentials = [
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(row["credential_id_b64"]))
            for row in existing_creds
        ]

        mobile_number = user["mobile_number"] or user["email"] or current_user["user_id"]

        options = webauthn.generate_registration_options(
            rp_id=RP_ID,
            rp_name=RP_NAME,
            user_id=current_user["user_id"].encode(),
            user_name=mobile_number,
            user_display_name=user["display_name"],
            exclude_credentials=exclude_credentials,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
        )

        # Store challenge
        ts = now_iso()
        challenge_id = make_id("wac")
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=_CHALLENGE_TTL_SECONDS)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        conn.execute(
            """INSERT INTO webauthn_challenges (challenge_id, user_id, challenge_b64, purpose, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                challenge_id,
                current_user["user_id"],
                bytes_to_base64url(options.challenge),
                "register",
                expires_at,
                ts,
            ),
        )
        conn.commit()
        conn.close()

        options_dict = json.loads(options_to_json(options))
        options_dict["_challenge_id"] = challenge_id
        if device_label:
            options_dict["_device_label"] = device_label

        return jsonify(options_dict), 200


    @app.post("/auth/webauthn/register/complete")
    def webauthn_register_complete():
        """
        Complete passkey registration. Verifies the browser response and stores the credential.
        """
        if not _WEBAUTHN_AVAILABLE:
            return _unavailable()

        current_user = g.current_user
        body = request.get_json(silent=True) or {}
        challenge_id = (body.get("challenge_id") or "").strip()
        device_label = (body.get("device_label") or "").strip() or None

        if not challenge_id:
            return jsonify({"error": "challenge_id is required"}), 400

        conn = get_conn()
        ensure_webauthn_tables(conn)

        ts = now_iso()
        challenge_row = conn.execute(
            """SELECT * FROM webauthn_challenges
               WHERE challenge_id = ? AND user_id = ? AND purpose = 'register' AND expires_at > ?""",
            (challenge_id, current_user["user_id"], ts),
        ).fetchone()

        if not challenge_row:
            conn.close()
            return jsonify({"error": "Challenge not found or expired"}), 400

        try:
            from webauthn.helpers.structs import RegistrationCredential
            credential = RegistrationCredential.parse_raw(json.dumps({
                k: v for k, v in body.items() if k not in ("challenge_id", "device_label")
            }))
            verified = webauthn.verify_registration_response(
                credential=credential,
                expected_challenge=base64url_to_bytes(challenge_row["challenge_b64"]),
                expected_rp_id=RP_ID,
                expected_origin=RP_ORIGIN,
            )
        except Exception as exc:
            conn.close()
            return jsonify({"error": "Registration verification failed", "detail": str(exc)}), 400

        cred_id = make_id("wcr")
        conn.execute(
            """INSERT INTO webauthn_credentials
               (credential_id, user_id, credential_id_b64, public_key_b64,
                sign_count, aaguid, device_label, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cred_id,
                current_user["user_id"],
                bytes_to_base64url(verified.credential_id),
                bytes_to_base64url(verified.credential_public_key),
                verified.sign_count,
                str(verified.aaguid) if verified.aaguid else None,
                device_label,
                ts,
            ),
        )
        # Consume challenge
        conn.execute("DELETE FROM webauthn_challenges WHERE challenge_id = ?", (challenge_id,))

        user = conn.execute(
            "SELECT organisation_id, display_name FROM user_accounts WHERE user_id = ?",
            (current_user["user_id"],),
        ).fetchone()

        audit_event(
            conn,
            entity_type="WebAuthnCredential",
            entity_id=cred_id,
            action="REGISTER",
            summary=f"Passkey registered for user {user['display_name'] if user else current_user['user_id']} — device: {device_label or 'unlabelled'}.",
            organisation_id=user["organisation_id"] if user else None,
        )

        conn.commit()
        conn.close()

        return jsonify({
            "credential_id": cred_id,
            "device_label": device_label,
            "message": "Passkey registered. You can now use biometrics to log in.",
        }), 201


    @app.post("/auth/webauthn/authenticate/begin")
    def webauthn_authenticate_begin():
        """
        Start passkey authentication. No auth required.
        Client passes mobile_number (from remember-me) to scope credentials.
        """
        if not _WEBAUTHN_AVAILABLE:
            return _unavailable()

        body = request.get_json(silent=True) or {}
        mobile_number = (body.get("mobile_number") or "").strip() or None
        email = (body.get("email") or "").strip() or None

        conn = get_conn()
        ensure_webauthn_tables(conn)

        user = None
        if mobile_number:
            user = conn.execute(
                "SELECT * FROM user_accounts WHERE mobile_number = ? AND access_status = 'ACTIVE'",
                (mobile_number,),
            ).fetchone()
        if not user and email:
            user = conn.execute(
                "SELECT * FROM user_accounts WHERE email = ? AND access_status = 'ACTIVE'",
                (email,),
            ).fetchone()

        allow_credentials = []
        user_id_for_challenge = None

        if user:
            creds = conn.execute(
                "SELECT credential_id_b64 FROM webauthn_credentials WHERE user_id = ?",
                (user["user_id"],),
            ).fetchall()
            allow_credentials = [
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(row["credential_id_b64"]))
                for row in creds
            ]
            user_id_for_challenge = user["user_id"]

        options = webauthn.generate_authentication_options(
            rp_id=RP_ID,
            allow_credentials=allow_credentials,
            user_verification=UserVerificationRequirement.PREFERRED,
        )

        ts = now_iso()
        challenge_id = make_id("wac")
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=_CHALLENGE_TTL_SECONDS)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        conn.execute(
            """INSERT INTO webauthn_challenges (challenge_id, user_id, challenge_b64, purpose, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                challenge_id,
                user_id_for_challenge or "unknown",
                bytes_to_base64url(options.challenge),
                "authenticate",
                expires_at,
                ts,
            ),
        )
        conn.commit()
        conn.close()

        options_dict = json.loads(options_to_json(options))
        options_dict["_challenge_id"] = challenge_id

        return jsonify(options_dict), 200


    @app.post("/auth/webauthn/authenticate/complete")
    def webauthn_authenticate_complete():
        """
        Complete passkey authentication. Verifies response, creates a session.
        No auth required — this IS the auth step.
        """
        if not _WEBAUTHN_AVAILABLE:
            return _unavailable()

        body = request.get_json(silent=True) or {}
        challenge_id = (body.get("challenge_id") or "").strip()
        device_id = (body.get("device_id") or "").strip()
        device_label = (body.get("device_label") or "").strip() or None
        ip_address_hash = (body.get("ip_address_hash") or "").strip() or None
        user_agent_hash = (body.get("user_agent_hash") or "").strip() or None

        if not challenge_id or not device_id:
            return jsonify({"error": "challenge_id and device_id are required"}), 400

        conn = get_conn()
        ensure_webauthn_tables(conn)

        ts = now_iso()
        challenge_row = conn.execute(
            """SELECT * FROM webauthn_challenges
               WHERE challenge_id = ? AND purpose = 'authenticate' AND expires_at > ?""",
            (challenge_id, ts),
        ).fetchone()

        if not challenge_row:
            conn.close()
            return jsonify({"error": "Challenge not found or expired"}), 400

        # Find credential by the ID in the response
        raw_id_b64 = body.get("rawId") or body.get("id") or ""
        cred_row = conn.execute(
            "SELECT * FROM webauthn_credentials WHERE credential_id_b64 = ?",
            (raw_id_b64,),
        ).fetchone()

        if not cred_row:
            conn.close()
            return jsonify({"error": "Credential not found"}), 400

        try:
            from webauthn.helpers.structs import AuthenticationCredential
            credential = AuthenticationCredential.parse_raw(json.dumps({
                k: v for k, v in body.items() if k not in ("challenge_id", "device_id", "device_label", "ip_address_hash", "user_agent_hash")
            }))
            verified = webauthn.verify_authentication_response(
                credential=credential,
                expected_challenge=base64url_to_bytes(challenge_row["challenge_b64"]),
                expected_rp_id=RP_ID,
                expected_origin=RP_ORIGIN,
                credential_public_key=base64url_to_bytes(cred_row["public_key_b64"]),
                credential_current_sign_count=cred_row["sign_count"],
            )
        except Exception as exc:
            conn.close()
            return jsonify({"error": "Authentication verification failed", "detail": str(exc)}), 400

        # Update sign count and last used
        conn.execute(
            "UPDATE webauthn_credentials SET sign_count = ?, last_used_at = ? WHERE credential_id = ?",
            (verified.new_sign_count, ts, cred_row["credential_id"]),
        )
        conn.execute("DELETE FROM webauthn_challenges WHERE challenge_id = ?", (challenge_id,))

        user_id = cred_row["user_id"]
        user = conn.execute(
            "SELECT * FROM user_accounts WHERE user_id = ? AND access_status = 'ACTIVE'",
            (user_id,),
        ).fetchone()

        if not user:
            conn.close()
            return jsonify({"error": "User account not active"}), 403

        # Reuse or create session (mirrors the password login logic)
        session_id = make_id("sess")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, organisation_id TEXT,
            role TEXT NOT NULL, device_id TEXT NOT NULL, device_label TEXT,
            ip_address_hash TEXT, user_agent_hash TEXT, session_status TEXT NOT NULL,
            login_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, logout_at TEXT,
            ended_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")

        existing_session = conn.execute(
            """SELECT session_id FROM user_sessions
               WHERE user_id = ? AND device_id = ? AND session_status = 'ACTIVE'
               ORDER BY login_at DESC LIMIT 1""",
            (user_id, device_id),
        ).fetchone()

        if existing_session:
            session_id = existing_session["session_id"]
            conn.execute(
                "UPDATE user_sessions SET last_seen_at = ?, updated_at = ? WHERE session_id = ?",
                (ts, ts, session_id),
            )
        else:
            conn.execute(
                """INSERT INTO user_sessions
                   (session_id, user_id, organisation_id, role, device_id, device_label,
                    ip_address_hash, user_agent_hash, session_status, login_at,
                    last_seen_at, logout_at, ended_reason, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, NULL, NULL, ?, ?)""",
                (session_id, user_id, user["organisation_id"], user["role"],
                 device_id, device_label, ip_address_hash, user_agent_hash,
                 ts, ts, ts, ts),
            )

        audit_event(
            conn,
            entity_type="UserSession",
            entity_id=session_id,
            action="LOGIN_WEBAUTHN",
            summary=f"Passkey login for user {user['display_name']}.",
            organisation_id=user["organisation_id"],
        )

        conn.commit()

        session = conn.execute(
            "SELECT * FROM user_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        conn.close()

        return jsonify({
            "session_id": session_id,
            "session": dict(session),
            "user": {
                "user_id": user["user_id"],
                "display_name": user["display_name"],
                "role": user["role"],
                "organisation_id": user["organisation_id"],
            },
        }), 201


    @app.get("/users/<user_id>/webauthn-credentials")
    def list_webauthn_credentials(user_id):
        """List registered passkeys for a user."""
        current_user = g.current_user
        _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}

        if user_id != current_user["user_id"] and current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            return jsonify({"error": "You can only view your own passkeys"}), 403

        conn = get_conn()
        ensure_webauthn_tables(conn)

        rows = conn.execute(
            """SELECT credential_id, device_label, aaguid, created_at, last_used_at
               FROM webauthn_credentials WHERE user_id = ? ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()
        conn.close()

        return jsonify({
            "user_id": user_id,
            "count": len(rows),
            "credentials": [dict(r) for r in rows],
        }), 200


    @app.delete("/webauthn-credentials/<credential_id>")
    def delete_webauthn_credential(credential_id):
        """Remove a registered passkey."""
        current_user = g.current_user
        _GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}

        conn = get_conn()
        ensure_webauthn_tables(conn)

        cred = conn.execute(
            "SELECT * FROM webauthn_credentials WHERE credential_id = ?", (credential_id,)
        ).fetchone()

        if not cred:
            conn.close()
            return jsonify({"error": "Credential not found"}), 404

        if cred["user_id"] != current_user["user_id"] and current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            conn.close()
            return jsonify({"error": "You can only remove your own passkeys"}), 403

        user = conn.execute(
            "SELECT organisation_id, display_name FROM user_accounts WHERE user_id = ?",
            (cred["user_id"],),
        ).fetchone()

        conn.execute(
            "DELETE FROM webauthn_credentials WHERE credential_id = ?", (credential_id,)
        )

        audit_event(
            conn,
            entity_type="WebAuthnCredential",
            entity_id=credential_id,
            action="DELETE",
            summary=f"Passkey '{cred['device_label'] or 'unlabelled'}' removed by {current_user['display_name']}.",
            organisation_id=user["organisation_id"] if user else None,
        )

        conn.commit()
        conn.close()

        return jsonify({"credential_id": credential_id, "deleted": True}), 200
