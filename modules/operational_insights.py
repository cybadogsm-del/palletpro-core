"""
Deterministic operational insights.

This module turns backend state into structured "what needs attention" records.
It does not call an LLM; the goal is predictable, auditable product intelligence
that the frontend can render directly.
"""

from flask import g, jsonify

from db import get_conn, now_iso
from modules.offline_batch import ensure_offline_batch_tables
from modules.resource_loss import ensure_resource_loss_tables
from modules.stocktake import ensure_stocktake_tables
from modules.tcr import ensure_tcr_tables

_ORG_ADMIN_ROLES = {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
_ACTIVE_PENDING_STATUSES = ("PENDING_APPROVAL", "AWAITING_FIX", "READY_TO_APPROVE")


def _require_org_admin_or_above():
    current_user = g.current_user
    if current_user["role"] not in _ORG_ADMIN_ROLES:
        return jsonify({
            "error": "INSUFFICIENT_ROLE",
            "message": "Only Org Admin or above can view operational insights.",
            "your_role": current_user["role"],
        }), 403
    return None


def _count(conn, sql, params):
    return conn.execute(sql, params).fetchone()["count"]


def _severity(count, high_at=10):
    if count >= high_at:
        return "high"
    if count > 0:
        return "medium"
    return "clear"


def _insight(
    insight_id,
    title,
    count,
    reason,
    recommended_action,
    action_label,
    action_route,
    severity=None,
):
    return {
        "insight_id": insight_id,
        "title": title,
        "severity": severity or _severity(count),
        "count": count,
        "reason": reason,
        "recommended_action": recommended_action,
        "allowed_user_actions": [
            {
                "label": action_label,
                "action_type": "NAVIGATE",
                "route": action_route,
            }
        ] if action_label and action_route else [],
    }


def build_operational_insights(conn, organisation_id):
    ensure_offline_batch_tables(conn)
    ensure_stocktake_tables(conn)
    ensure_tcr_tables(conn)
    ensure_resource_loss_tables(conn)

    pending_approvals = _count(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM pending_approval_entries
        WHERE organisation_id = ?
          AND status IN (?, ?, ?)
        """,
        (organisation_id, *_ACTIVE_PENDING_STATUSES),
    )

    active_stocktakes = _count(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM stocktake_sessions
        WHERE organisation_id = ?
          AND status = 'IN_PROGRESS'
        """,
        (organisation_id,),
    )

    stocktake_variance_reviews = _count(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM stocktake_sessions
        WHERE organisation_id = ?
          AND status = 'PENDING_REVIEW'
          AND variance_lines > 0
        """,
        (organisation_id,),
    )

    correction_requests = _count(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM transaction_correction_requests
        WHERE organisation_id = ?
          AND status = 'PENDING'
        """,
        (organisation_id,),
    )

    resource_loss_reviews = _count(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM resource_losses
        WHERE organisation_id = ?
          AND status = 'PENDING_REVIEW'
        """,
        (organisation_id,),
    )

    offline_failures = _count(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM offline_batch_log
        WHERE organisation_id = ?
          AND status = 'error'
        """,
        (organisation_id,),
    )

    insights = [
        _insight(
            "pending_approvals",
            "Pending approvals",
            pending_approvals,
            "Transactions or resource requests are waiting for admin review.",
            "Review each pending item and approve, reject, or send it back for correction.",
            "Review approvals",
            "/pending-approval",
        ),
        _insight(
            "active_stocktakes",
            "Active stocktakes",
            active_stocktakes,
            "One or more stocktakes are still in progress.",
            "Complete counts or cancel stale stocktakes before starting duplicate counts.",
            "View stocktakes",
            f"/organisations/{organisation_id}/stocktake",
        ),
        _insight(
            "stocktake_variance_reviews",
            "Stocktake variance reviews",
            stocktake_variance_reviews,
            "Submitted stocktakes contain counted quantities that differ from expected stock.",
            "Review variance lines, then accept, reject, or post the stocktake.",
            "Review stocktakes",
            f"/organisations/{organisation_id}/stocktake",
        ),
        _insight(
            "correction_requests",
            "Correction requests",
            correction_requests,
            "Users have requested changes to posted transactions.",
            "Review each correction request and preserve the audit trail by approving or rejecting it.",
            "Review corrections",
            f"/organisations/{organisation_id}/correction-requests",
        ),
        _insight(
            "resource_loss_reviews",
            "Resource loss reviews",
            resource_loss_reviews,
            "Reported losses are waiting for admin confirmation before ledger impact.",
            "Confirm genuine losses or reject reports that should not affect stock.",
            "Review losses",
            f"/organisations/{organisation_id}/resource-losses",
        ),
        _insight(
            "offline_upload_failures",
            "Offline upload failures",
            offline_failures,
            "Queued offline work failed backend validation during upload.",
            "Open the queue, fix the failed item data, and retry upload.",
            "Open upload queue",
            "/queue",
        ),
    ]

    attention_items = [item for item in insights if item["count"] > 0]

    return {
        "insight_type": "ORG_OPERATIONAL_INSIGHTS",
        "organisation_id": organisation_id,
        "generated_at": now_iso(),
        "summary": {
            "total_attention_items": sum(item["count"] for item in attention_items),
            "categories_with_attention": len(attention_items),
            "status": "ATTENTION_REQUIRED" if attention_items else "CLEAR",
        },
        "items": insights,
        "attention_items": attention_items,
    }


def register_operational_insight_routes(app):
    @app.get("/organisations/<organisation_id>/operational-insights")
    def get_org_operational_insights(organisation_id):
        denied = _require_org_admin_or_above()
        if denied:
            return denied

        conn = get_conn()
        org = conn.execute(
            "SELECT organisation_id FROM organisations WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()
        if not org:
            conn.close()
            return jsonify({"error": "Organisation not found"}), 404

        payload = build_operational_insights(conn, organisation_id)
        conn.close()
        return jsonify(payload), 200
