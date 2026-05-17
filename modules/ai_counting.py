"""
AI resource counting module.

POST /ai/count-resources accepts a field photo (base64 data URL) and
resource context. It pulls up to PALLET_PRO_AI_MAX_SAMPLES reference
images from the resource_sample_photos library, then calls the Anthropic
Claude vision API to get a suggested count with a confidence score.

Every request is stored in ai_count_requests for a full audit trail.
Field workers can record their accepted count via PATCH …/accept.

Environment variables
---------------------
  ANTHROPIC_API_KEY         — required; AI counting returns 503 without it
  PALLET_PRO_AI_MODEL       — Claude model (default: claude-sonnet-4-6)
  PALLET_PRO_AI_MAX_SAMPLES — max reference photos per call (default: 3)

Routes
------
  POST  /ai/count-resources              submit field photo, get count
  GET   /ai/count-requests/<id>          retrieve a past request
  PATCH /ai/count-requests/<id>/accept   record the count the user accepted
"""

import base64
import json
import os
import re

from flask import g, jsonify, request

from db import get_conn, make_id, now_iso

_ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_AI_MODEL = os.environ.get("PALLET_PRO_AI_MODEL", "claude-sonnet-4-6")
_MAX_SAMPLE_PHOTOS = int(os.environ.get("PALLET_PRO_AI_MAX_SAMPLES", "3"))
_MAX_IMAGE_BYTES = 5 * 1024 * 1024

_DATA_URL_RE = re.compile(
    r"^data:(image/(?:png|jpeg|webp));base64,(.+)$", re.DOTALL
)
_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)

_SYSTEM_PROMPT = """You are an expert warehouse inventory assistant specialising in
counting pallets and transport handling equipment from photographs.

When given reference photos, use them to calibrate what the target item looks like.
Then count how many of that item are visible in the field photo.

You MUST respond with ONLY valid JSON — no markdown, no explanation outside the JSON:
{"count": <non-negative integer>, "confidence": <number 0.0–1.0>, "notes": "<1–2 sentences>"}

Guidelines:
- count: total visible items of the target type (include partially visible ones)
- confidence: 0.0 = completely uncertain, 1.0 = completely certain
- notes: briefly describe what you see (stack arrangement, obstructions, lighting)
- If the photo is unclear or the item is not visible, return count 0 with low confidence"""

_GLOBAL_ADMIN_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}


def ensure_ai_counting_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS ai_count_requests (
        request_id              TEXT PRIMARY KEY,
        organisation_id         TEXT,
        user_id                 TEXT,
        resource_id             TEXT,
        resource_type           TEXT,
        resource_name           TEXT,
        sample_photos_used      INTEGER NOT NULL DEFAULT 0,
        suggested_count         INTEGER,
        confidence_score        REAL,
        confidence_label        TEXT,
        ai_notes                TEXT,
        model                   TEXT,
        accepted_count          INTEGER,
        accepted_at             TEXT,
        error                   TEXT,
        created_at              TEXT NOT NULL
    )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_count_org "
        "ON ai_count_requests (organisation_id, created_at DESC)"
    )
    conn.commit()


def _confidence_label(score):
    if score is None:
        return "UNKNOWN"
    if score >= 0.75:
        return "HIGH"
    if score >= 0.45:
        return "MEDIUM"
    return "LOW"


def _call_claude(field_b64, field_media_type, sample_photos):
    """
    Build a multimodal message and call the Anthropic API.
    Returns (suggested_count, confidence_score, notes) or raises.
    """
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "anthropic package not installed — run: pip install anthropic"
        )

    client = anthropic.Anthropic(api_key=_ANTHROPIC_API_KEY)

    content = []

    if sample_photos:
        content.append({
            "type": "text",
            "text": (
                f"Here {'is' if len(sample_photos) == 1 else 'are'} "
                f"{len(sample_photos)} reference "
                f"{'photo' if len(sample_photos) == 1 else 'photos'} showing "
                f"what the target item looks like:"
            ),
        })
        for sp in sample_photos:
            raw = bytes(sp["photo_data"])
            b64 = base64.b64encode(raw).decode()
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": sp["photo_content_type"],
                    "data": b64,
                },
            })

    content.append({
        "type": "text",
        "text": (
            "Now count the items in this field photo:"
            if sample_photos
            else "Count the pallets/resources visible in this field photo:"
        ),
    })
    content.append({
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": field_media_type,
            "data": field_b64,
        },
    })

    response = client.messages.create(
        model=_AI_MODEL,
        max_tokens=256,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    raw_text = response.content[0].text.strip()

    # Try direct parse first, then extract first JSON object from the text
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(raw_text)
        if not m:
            raise ValueError(f"No JSON in model response: {raw_text[:200]}")
        parsed = json.loads(m.group(0))

    count = int(parsed.get("count", 0))
    confidence = float(parsed.get("confidence", 0.0))
    confidence = max(0.0, min(1.0, confidence))
    notes = str(parsed.get("notes", "")).strip()

    return count, confidence, notes


def register_ai_counting_routes(app):

    @app.post("/ai/count-resources")
    def count_resources():
        """
        Submit a field photo and get an AI-suggested resource count.

        Body (JSON):
          image_data          — base64 data URL (required)
          resource_id         — optional; used to fetch reference photos
          resource_type_label — optional fallback when no resource_id given
          organisation_id     — optional; scopes org sample photos

        Returns suggested_count, confidence_score (0–1), confidence_label,
        and ai_notes alongside a request_id for the audit record.
        """
        if not _ANTHROPIC_API_KEY:
            return jsonify({
                "error": "AI counting is not configured on this server.",
                "detail": "Set the ANTHROPIC_API_KEY environment variable to enable this feature.",
            }), 503

        current_user = g.current_user
        body = request.get_json(silent=True) or {}

        raw_image = (body.get("image_data") or "").strip()
        if not raw_image:
            return jsonify({"error": "image_data is required"}), 400

        m = _DATA_URL_RE.match(raw_image)
        if not m:
            return jsonify({
                "error": "image_data must be a data URL: data:image/jpeg;base64,<data>"
            }), 400

        field_media_type = m.group(1)
        field_b64 = m.group(2).strip()

        try:
            field_bytes = base64.b64decode(field_b64)
        except Exception:
            return jsonify({"error": "image_data contains invalid base64"}), 400

        if len(field_bytes) > _MAX_IMAGE_BYTES:
            return jsonify({
                "error": f"Image too large ({len(field_bytes)} bytes). Maximum is 5 MB."
            }), 400

        resource_id = (body.get("resource_id") or "").strip() or None
        resource_type_label = (body.get("resource_type_label") or "").strip() or None
        organisation_id = (body.get("organisation_id") or "").strip() or None

        # Resolve resource metadata and fetch sample photos
        resource_name = None
        resource_type = resource_type_label
        sample_photos = []

        conn = get_conn()
        ensure_ai_counting_tables(conn)

        if resource_id:
            resource_row = conn.execute(
                "SELECT resource_id, name, resource_type, organisation_id "
                "FROM resources WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
            if resource_row:
                resource_name = resource_row["name"]
                resource_type = resource_row["resource_type"]
                organisation_id = organisation_id or resource_row["organisation_id"]

                # Global photos for this resource type
                global_rows = conn.execute(
                    """SELECT photo_data, photo_content_type FROM resource_sample_photos
                       WHERE scope = 'GLOBAL' AND resource_type_label = ? AND is_active = 1
                       ORDER BY sort_order ASC, created_at ASC
                       LIMIT ?""",
                    (resource_type, _MAX_SAMPLE_PHOTOS),
                ).fetchall()

                # Org photos for this specific resource (fill remaining slots)
                remaining = _MAX_SAMPLE_PHOTOS - len(global_rows)
                org_rows = []
                if remaining > 0:
                    org_rows = conn.execute(
                        """SELECT photo_data, photo_content_type FROM resource_sample_photos
                           WHERE scope = 'ORG' AND resource_id = ? AND is_active = 1
                           ORDER BY sort_order ASC, created_at ASC
                           LIMIT ?""",
                        (resource_id, remaining),
                    ).fetchall()

                sample_photos = list(global_rows) + list(org_rows)

        elif resource_type_label:
            global_rows = conn.execute(
                """SELECT photo_data, photo_content_type FROM resource_sample_photos
                   WHERE scope = 'GLOBAL' AND resource_type_label = ? AND is_active = 1
                   ORDER BY sort_order ASC, created_at ASC
                   LIMIT ?""",
                (resource_type_label, _MAX_SAMPLE_PHOTOS),
            ).fetchall()
            sample_photos = list(global_rows)

        request_id = make_id("aic")
        user_id = current_user.get("user_id")

        try:
            suggested_count, confidence_score, ai_notes = _call_claude(
                field_b64, field_media_type, sample_photos
            )
            label = _confidence_label(confidence_score)

            conn.execute(
                """INSERT INTO ai_count_requests
                   (request_id, organisation_id, user_id, resource_id,
                    resource_type, resource_name, sample_photos_used,
                    suggested_count, confidence_score, confidence_label,
                    ai_notes, model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, organisation_id, user_id, resource_id,
                 resource_type, resource_name, len(sample_photos),
                 suggested_count, confidence_score, label,
                 ai_notes, _AI_MODEL, now_iso()),
            )
            conn.commit()
            conn.close()

            return jsonify({
                "request_id": request_id,
                "suggested_count": suggested_count,
                "confidence_score": round(confidence_score, 4),
                "confidence_label": label,
                "ai_notes": ai_notes,
                "sample_photos_used": len(sample_photos),
                "resource_id": resource_id,
                "resource_name": resource_name,
                "resource_type": resource_type,
                "model": _AI_MODEL,
            }), 200

        except Exception as exc:
            error_msg = str(exc)
            conn.execute(
                """INSERT INTO ai_count_requests
                   (request_id, organisation_id, user_id, resource_id,
                    resource_type, resource_name, sample_photos_used,
                    error, model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, organisation_id, user_id, resource_id,
                 resource_type, resource_name, len(sample_photos),
                 error_msg, _AI_MODEL, now_iso()),
            )
            conn.commit()
            conn.close()
            return jsonify({
                "error": "AI counting failed.",
                "detail": error_msg,
                "request_id": request_id,
            }), 502


    @app.get("/ai/count-requests/<request_id>")
    def get_count_request(request_id):
        """Retrieve a past AI count request by ID."""
        current_user = g.current_user

        conn = get_conn()
        ensure_ai_counting_tables(conn)

        row = conn.execute(
            "SELECT * FROM ai_count_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        conn.close()

        if not row:
            return jsonify({"error": "Count request not found"}), 404

        if (current_user["role"] not in _GLOBAL_ADMIN_ROLES
                and current_user.get("user_id") != row["user_id"]
                and current_user.get("organisation_id") != row["organisation_id"]):
            return jsonify({"error": "Access denied"}), 403

        return jsonify(dict(row)), 200


    @app.patch("/ai/count-requests/<request_id>/accept")
    def accept_count_request(request_id):
        """
        Record the count the field worker actually accepted after reviewing
        the AI suggestion. Provides an audit link between AI suggestion and
        the transaction that was submitted.
        """
        current_user = g.current_user
        body = request.get_json(silent=True) or {}

        accepted_count = body.get("accepted_count")
        if accepted_count is None:
            return jsonify({"error": "accepted_count is required"}), 400
        try:
            accepted_count = int(accepted_count)
        except (TypeError, ValueError):
            return jsonify({"error": "accepted_count must be an integer"}), 400

        conn = get_conn()
        ensure_ai_counting_tables(conn)

        row = conn.execute(
            "SELECT * FROM ai_count_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({"error": "Count request not found"}), 404

        if (current_user["role"] not in _GLOBAL_ADMIN_ROLES
                and current_user.get("user_id") != row["user_id"]
                and current_user.get("organisation_id") != row["organisation_id"]):
            conn.close()
            return jsonify({"error": "Access denied"}), 403

        ts = now_iso()
        conn.execute(
            "UPDATE ai_count_requests SET accepted_count = ?, accepted_at = ? WHERE request_id = ?",
            (accepted_count, ts, request_id),
        )
        conn.commit()
        conn.close()

        return jsonify({
            "request_id": request_id,
            "suggested_count": row["suggested_count"],
            "accepted_count": accepted_count,
            "accepted_at": ts,
        }), 200


    @app.get("/organisations/<organisation_id>/ai/count-requests")
    def list_org_count_requests(organisation_id):
        """List AI count requests for an organisation (Org Admin+)."""
        current_user = g.current_user

        if current_user["role"] not in _GLOBAL_ADMIN_ROLES:
            if current_user.get("role") not in {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}:
                return jsonify({"error": "Access denied"}), 403
            if current_user.get("organisation_id") != organisation_id:
                return jsonify({"error": "Access denied"}), 403

        try:
            limit = min(int(request.args.get("limit", 50)), 200)
            offset = max(int(request.args.get("offset", 0)), 0)
        except (ValueError, TypeError):
            return jsonify({"error": "limit and offset must be integers"}), 400

        conn = get_conn()
        ensure_ai_counting_tables(conn)

        rows = conn.execute(
            """SELECT * FROM ai_count_requests
               WHERE organisation_id = ?
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (organisation_id, limit, offset),
        ).fetchall()
        conn.close()

        return jsonify({
            "organisation_id": organisation_id,
            "limit": limit,
            "offset": offset,
            "count": len(rows),
            "requests": [dict(r) for r in rows],
        }), 200
