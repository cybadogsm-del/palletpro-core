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
from modules.subscription_access import (
    ensure_subscription_guard_tables,
    get_org_access_status_payload,
)
from modules.stocktake import ensure_stocktake_tables
from modules.tcr import ensure_tcr_tables

_ORG_ADMIN_ROLES = {"ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}
_ACTIVE_PENDING_STATUSES = ("PENDING_APPROVAL", "AWAITING_FIX", "READY_TO_APPROVE")
_GLOBAL_ROLES = {"GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN"}


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


def _table_has_column(conn, table_name, column_name):
    return column_name in {
        row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


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
    action_type="NAVIGATE",
    can_act=True,
    unavailable_reason=None,
    audit_entity_type=None,
    severity=None,
):
    action = {
        "label": action_label,
        "action_type": action_type,
        "route": action_route,
        "can_act_now": bool(can_act),
        "unavailable_reason": unavailable_reason,
    } if action_label and action_route else None

    return {
        "insight_id": insight_id,
        "title": title,
        "severity": severity or _severity(count),
        "count": count,
        "reason": reason,
        "recommended_action": recommended_action,
        "allowed_user_actions": [action] if action else [],
        "audit_context": {
            "entity_type": audit_entity_type or "OperationalInsight",
            "entity_id": insight_id,
            "action": "VIEW_ATTENTION",
            "outcome": "ACTIONABLE" if count > 0 and can_act else "VIEW_ONLY",
            "reason": reason,
        },
    }


def build_operational_insights(conn, organisation_id):
    ensure_offline_batch_tables(conn)
    ensure_stocktake_tables(conn)
    ensure_tcr_tables(conn)
    ensure_resource_loss_tables(conn)
    ensure_subscription_guard_tables(conn)

    current_user = getattr(g, "current_user", {})
    role = current_user.get("role")
    can_administer_org = role in _ORG_ADMIN_ROLES
    if role not in _GLOBAL_ROLES:
        can_administer_org = can_administer_org and current_user.get("user_org_id") == organisation_id

    unavailable_reason = None if can_administer_org else "Your account can view this issue but cannot change it."

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

    missing_reference_reviews = _count(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM pending_approval_entries
        WHERE organisation_id = ?
          AND reason_code = 'MISSING_ENTITY'
          AND status IN (?, ?, ?)
        """,
        (organisation_id, *_ACTIVE_PENDING_STATUSES),
    )

    unresolved_transactions = _count(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM transactions
        WHERE organisation_id = ?
          AND unresolved_entity_note IS NOT NULL
          AND status IN ('PENDING_APPROVAL', 'AWAITING_FIX')
        """,
        (organisation_id,),
    )

    unresolved_resource_losses = 0
    if _table_has_column(conn, "resource_losses", "unresolved_entity_note"):
        unresolved_resource_losses = _count(
            conn,
            """
            SELECT COUNT(*) AS count
            FROM resource_losses
            WHERE organisation_id = ?
              AND unresolved_entity_note IS NOT NULL
              AND status = 'PENDING_REVIEW'
            """,
            (organisation_id,),
        )

    duplicate_submitter_column = (
        ", submitted_by_user_id"
        if _table_has_column(conn, "transactions", "submitted_by_user_id")
        else ""
    )
    suspicious_duplicate_transactions = _count(
        conn,
        f"""
        SELECT COUNT(*) AS count
        FROM (
            SELECT depot_id, resource_id, direction, quantity{duplicate_submitter_column}, COUNT(*) AS duplicate_count
            FROM transactions
            WHERE organisation_id = ?
              AND status IN ('POSTED', 'PENDING_APPROVAL', 'AWAITING_FIX')
            GROUP BY depot_id, resource_id, direction, quantity{duplicate_submitter_column}
            HAVING COUNT(*) > 1
        )
        """,
        (organisation_id,),
    )

    unusual_stock_movements = _count(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM transactions
        WHERE organisation_id = ?
          AND quantity >= 1000
          AND status IN ('POSTED', 'PENDING_APPROVAL')
        """,
        (organisation_id,),
    )

    access_payload = get_org_access_status_payload(conn, organisation_id)
    subscription_access_blockers = 0 if access_payload["normal_access_allowed"] else 1

    insights = [
        _insight(
            "pending_approvals",
            "Pending approvals",
            pending_approvals,
            "Transactions or resource requests are waiting for admin review.",
            "Review each pending item and approve, reject, or send it back for correction.",
            "Review approvals",
            "/org/resources",
            can_act=can_administer_org,
            unavailable_reason=unavailable_reason,
            audit_entity_type="PendingApprovalEntry",
        ),
        _insight(
            "active_stocktakes",
            "Active stocktakes",
            active_stocktakes,
            "One or more stocktakes are still in progress.",
            "Complete counts or cancel stale stocktakes before starting duplicate counts.",
            "View stocktakes",
            "/stocktake/new",
            can_act=can_administer_org,
            unavailable_reason=unavailable_reason,
            audit_entity_type="StocktakeSession",
        ),
        _insight(
            "stocktake_variance_reviews",
            "Stocktake variance reviews",
            stocktake_variance_reviews,
            "Submitted stocktakes contain counted quantities that differ from expected stock.",
            "Review variance lines, then accept, reject, or post the stocktake.",
            "Review stocktakes",
            "/stocktake/new",
            can_act=can_administer_org,
            unavailable_reason=unavailable_reason,
            audit_entity_type="StocktakeSession",
        ),
        _insight(
            "correction_requests",
            "Correction requests",
            correction_requests,
            "Users have requested changes to posted transactions.",
            "Review each correction request and preserve the audit trail by approving or rejecting it.",
            "Review corrections",
            "/admin/tcr",
            can_act=can_administer_org,
            unavailable_reason=unavailable_reason,
            audit_entity_type="TransactionCorrectionRequest",
        ),
        _insight(
            "resource_loss_reviews",
            "Resource loss reviews",
            resource_loss_reviews,
            "Reported losses are waiting for admin confirmation before ledger impact.",
            "Confirm genuine losses or reject reports that should not affect stock.",
            "Review losses",
            "/admin/losses",
            can_act=can_administer_org,
            unavailable_reason=unavailable_reason,
            audit_entity_type="ResourceLoss",
        ),
        _insight(
            "offline_upload_failures",
            "Offline upload failures",
            offline_failures,
            "Queued offline work failed backend validation during upload.",
            "Open the queue, fix the failed item data, and retry upload.",
            "Open upload queue",
            "/queue",
            can_act=can_administer_org,
            unavailable_reason=unavailable_reason,
            audit_entity_type="OfflineBatchLog",
        ),
        _insight(
            "missing_references",
            "Missing references",
            missing_reference_reviews + unresolved_transactions + unresolved_resource_losses,
            "Transactions or loss reports refer to a partner, resource, or depot that the backend could not match.",
            "Open the source workflow, resolve the missing reference, then approve or reject the item.",
            "Resolve references",
            "/org/resources",
            can_act=can_administer_org,
            unavailable_reason=unavailable_reason,
            audit_entity_type="PendingApprovalEntry",
        ),
        _insight(
            "suspicious_duplicate_transactions",
            "Possible duplicate transactions",
            suspicious_duplicate_transactions,
            "Multiple recent transactions have the same depot, resource, direction, quantity, and submitter.",
            "Review the matching transactions before posting or correcting stock.",
            "Review transactions",
            "/transactions/search",
            can_act=can_administer_org,
            unavailable_reason=unavailable_reason,
            audit_entity_type="Transaction",
        ),
        _insight(
            "unusual_stock_movement",
            "Unusual stock movement",
            unusual_stock_movements,
            "One or more transactions have unusually large quantities for manual review.",
            "Check the transaction evidence before relying on the stock position.",
            "Review transactions",
            "/transactions/search",
            can_act=can_administer_org,
            unavailable_reason=unavailable_reason,
            audit_entity_type="Transaction",
        ),
        _insight(
            "subscription_access_blockers",
            "Subscription access blockers",
            subscription_access_blockers,
            access_payload["reason"],
            "Review subscription state and restore access or export operating data as appropriate.",
            "Open organisation settings",
            "/org/manage",
            can_act=can_administer_org,
            unavailable_reason=unavailable_reason,
            audit_entity_type="OrganisationSubscription",
            severity="high" if subscription_access_blockers else "clear",
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
