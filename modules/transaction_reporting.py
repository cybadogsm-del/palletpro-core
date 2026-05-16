import csv
import io

from flask import Response, jsonify, request


TRANSACTION_CATEGORY_REPORT_COLUMNS = [
    "transaction_type",
    "direction",
    "resource_type",
    "resource_name",
    "depot_name",
    "partner_name",
    "submitted_by_user_id",
    "submitted_by_display_name",
    "transaction_count",
    "total_quantity",
    "first_transaction_at",
    "last_transaction_at",
]


def build_transaction_where_clause(
    organisation_id=None,
    date_from=None,
    date_to=None,
):
    where = ["1 = 1"]
    params = []

    if organisation_id:
        where.append("t.organisation_id = ?")
        params.append(organisation_id)

    if date_from:
        where.append("t.created_at >= ?")
        params.append(date_from)

    if date_to:
        where.append("t.created_at <= ?")
        params.append(date_to)

    return " AND ".join(where), params


def build_transaction_summary(conn, organisation_id=None, date_from=None, date_to=None):
    where_sql, params = build_transaction_where_clause(
        organisation_id=organisation_id,
        date_from=date_from,
        date_to=date_to,
    )

    total_row = conn.execute(
        f"""
        SELECT COUNT(*) AS total_transactions
        FROM transactions t
        WHERE {where_sql}
        """,
        params,
    ).fetchone()

    status_rows = conn.execute(
        f"""
        SELECT
            CASE
                WHEN t.posted_at IS NULL THEN 'PENDING'
                ELSE 'POSTED'
            END AS transaction_status,
            COUNT(*) AS count
        FROM transactions t
        WHERE {where_sql}
        GROUP BY transaction_status
        ORDER BY transaction_status ASC
        """,
        params,
    ).fetchall()

    type_rows = conn.execute(
        f"""
        SELECT
            COALESCE(t.transaction_type, 'UNKNOWN') AS transaction_type,
            COUNT(*) AS count,
            COALESCE(SUM(t.quantity), 0) AS total_quantity
        FROM transactions t
        WHERE {where_sql}
        GROUP BY t.transaction_type
        ORDER BY count DESC, transaction_type ASC
        """,
        params,
    ).fetchall()

    direction_rows = conn.execute(
        f"""
        SELECT
            COALESCE(t.direction, 'UNKNOWN') AS direction,
            COUNT(*) AS count,
            COALESCE(SUM(t.quantity), 0) AS total_quantity
        FROM transactions t
        WHERE {where_sql}
        GROUP BY t.direction
        ORDER BY count DESC, direction ASC
        """,
        params,
    ).fetchall()

    org_rows = conn.execute(
        f"""
        SELECT
            t.organisation_id,
            o.name AS organisation_name,
            COUNT(*) AS transaction_count,
            COALESCE(SUM(t.quantity), 0) AS total_quantity
        FROM transactions t
        LEFT JOIN organisations o ON o.organisation_id = t.organisation_id
        WHERE {where_sql}
        GROUP BY t.organisation_id, o.name
        ORDER BY transaction_count DESC, organisation_name ASC
        """,
        params,
    ).fetchall()

    depot_rows = conn.execute(
        f"""
        SELECT
            t.depot_id,
            d.name AS depot_name,
            COUNT(*) AS transaction_count,
            COALESCE(SUM(t.quantity), 0) AS total_quantity
        FROM transactions t
        LEFT JOIN depots d ON d.depot_id = t.depot_id
        WHERE {where_sql}
        GROUP BY t.depot_id, d.name
        ORDER BY transaction_count DESC, depot_name ASC
        """,
        params,
    ).fetchall()

    resource_rows = conn.execute(
        f"""
        SELECT
            t.resource_id,
            r.name AS resource_name,
            r.resource_type,
            COUNT(*) AS transaction_count,
            COALESCE(SUM(t.quantity), 0) AS total_quantity
        FROM transactions t
        LEFT JOIN resources r ON r.resource_id = t.resource_id
        WHERE {where_sql}
        GROUP BY t.resource_id, r.name, r.resource_type
        ORDER BY transaction_count DESC, resource_name ASC
        """,
        params,
    ).fetchall()

    partner_rows = conn.execute(
        f"""
        SELECT
            t.partner_id,
            p.name AS partner_name,
            COUNT(*) AS transaction_count,
            COALESCE(SUM(t.quantity), 0) AS total_quantity
        FROM transactions t
        LEFT JOIN partners p ON p.partner_id = t.partner_id
        WHERE {where_sql}
        GROUP BY t.partner_id, p.name
        ORDER BY transaction_count DESC, partner_name ASC
        """,
        params,
    ).fetchall()

    date_rows = conn.execute(
        f"""
        SELECT
            substr(t.created_at, 1, 10) AS transaction_date,
            COUNT(*) AS transaction_count,
            COALESCE(SUM(t.quantity), 0) AS total_quantity
        FROM transactions t
        WHERE {where_sql}
        GROUP BY substr(t.created_at, 1, 10)
        ORDER BY transaction_date DESC
        LIMIT 60
        """,
        params,
    ).fetchall()

    return {
        "filters": {
            "organisation_id": organisation_id,
            "date_from": date_from,
            "date_to": date_to,
        },
        "total_transactions": total_row["total_transactions"] if total_row else 0,
        "status_counts": [dict(row) for row in status_rows],
        "transaction_type_counts": [dict(row) for row in type_rows],
        "direction_counts": [dict(row) for row in direction_rows],
        "organisation_counts": [dict(row) for row in org_rows],
        "depot_counts": [dict(row) for row in depot_rows],
        "resource_counts": [dict(row) for row in resource_rows],
        "partner_counts": [dict(row) for row in partner_rows],
        "daily_counts": [dict(row) for row in date_rows],
    }


def get_organisation(conn, organisation_id):
    return conn.execute(
        "SELECT * FROM organisations WHERE organisation_id = ?",
        (organisation_id,),
    ).fetchone()


def fetch_transaction_category_rows(conn, organisation_id, date_from=None, date_to=None):
    if not organisation_id:
        raise ValueError("organisation_id is required for category reporting")

    where_sql, params = build_transaction_where_clause(
        organisation_id=organisation_id,
        date_from=date_from,
        date_to=date_to,
    )

    rows = conn.execute(
        f"""
        SELECT
            COALESCE(t.transaction_type, 'UNKNOWN') AS transaction_type,
            COALESCE(t.direction, 'UNKNOWN') AS direction,
            COALESCE(r.resource_type, 'UNKNOWN') AS resource_type,
            COALESCE(r.name, 'UNKNOWN') AS resource_name,
            COALESCE(d.name, 'UNKNOWN') AS depot_name,
            COALESCE(p.name, 'UNKNOWN') AS partner_name,
            COALESCE(t.submitted_by_user_id, 'UNKNOWN') AS submitted_by_user_id,
            COALESCE(t.submitted_by_display_name, 'Unknown User') AS submitted_by_display_name,
            COUNT(*) AS transaction_count,
            COALESCE(SUM(t.quantity), 0) AS total_quantity,
            MIN(t.created_at) AS first_transaction_at,
            MAX(t.created_at) AS last_transaction_at
        FROM transactions t
        LEFT JOIN resources r ON r.resource_id = t.resource_id
        LEFT JOIN depots d ON d.depot_id = t.depot_id
        LEFT JOIN partners p ON p.partner_id = t.partner_id
        WHERE {where_sql}
        GROUP BY
            t.transaction_type,
            t.direction,
            r.resource_type,
            r.name,
            d.name,
            p.name,
            t.submitted_by_user_id,
            t.submitted_by_display_name
        ORDER BY
            transaction_count DESC,
            total_quantity DESC,
            resource_name ASC
        """,
        params,
    ).fetchall()

    return [dict(row) for row in rows]


def get_transaction_total_count(conn, organisation_id, date_from=None, date_to=None):
    if not organisation_id:
        raise ValueError("organisation_id is required for category reporting")

    where_sql, params = build_transaction_where_clause(
        organisation_id=organisation_id,
        date_from=date_from,
        date_to=date_to,
    )

    row = conn.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM transactions t
        WHERE {where_sql}
        """,
        params,
    ).fetchone()

    return row["c"] if row else 0


def build_transaction_category_csv(items, organisation_id, date_from=None, date_to=None):
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(TRANSACTION_CATEGORY_REPORT_COLUMNS)

    for row in items:
        writer.writerow([row[column] for column in TRANSACTION_CATEGORY_REPORT_COLUMNS])

    filename_parts = ["pallet_pro_transaction_category_report", organisation_id]
    if date_from:
        filename_parts.append(f"from_{date_from}")
    if date_to:
        filename_parts.append(f"to_{date_to}")

    filename = "_".join(filename_parts).replace("/", "-").replace(":", "-") + ".csv"

    return output.getvalue(), filename


def register_transaction_reporting_routes(
    app,
    *,
    get_conn,
    ensure_transaction_partner_columns,
    ensure_partner_address_tables,
    ensure_transaction_user_attribution_columns,
):
    def prepare_transaction_reporting(conn, include_user_attribution=False):
        ensure_transaction_partner_columns(conn)
        ensure_partner_address_tables(conn)
        if include_user_attribution:
            ensure_transaction_user_attribution_columns(conn)

    @app.get("/global-admin/transaction-summary")
    def get_global_admin_transaction_summary():
        organisation_id = request.args.get("organisation_id")
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")

        conn = get_conn()
        try:
            prepare_transaction_reporting(conn)
            summary = build_transaction_summary(
                conn,
                organisation_id=organisation_id,
                date_from=date_from,
                date_to=date_to,
            )
        finally:
            conn.close()

        return jsonify({
            "summary_type": "GLOBAL_ADMIN_TRANSACTION_SUMMARY",
            "visibility": "GLOBAL_ADMIN_PLATFORM_SUMMARY",
            "summary": summary,
            "rules": [
                "Global Admin transaction reporting is for platform activity, support, audit, growth, and pricing intelligence.",
                "Global Admin sees summary reporting first, not deep operational interference.",
                "Use organisation_id and date range filters to narrow the report.",
            ],
        }), 200

    @app.get("/organisations/<organisation_id>/transaction-summary")
    def get_org_admin_transaction_summary(organisation_id):
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")

        conn = get_conn()
        try:
            prepare_transaction_reporting(conn)
            org = get_organisation(conn, organisation_id)

            if not org:
                return jsonify({"error": "Organisation not found"}), 404

            summary = build_transaction_summary(
                conn,
                organisation_id=organisation_id,
                date_from=date_from,
                date_to=date_to,
            )
        finally:
            conn.close()

        return jsonify({
            "summary_type": "ORG_ADMIN_TRANSACTION_SUMMARY",
            "visibility": "ORG_SCOPED_OPERATIONAL_SUMMARY",
            "organisation_id": organisation_id,
            "organisation_name": org["name"],
            "summary": summary,
            "rules": [
                "Org Admin transaction reporting is operational and organisation-scoped.",
                "Org Admins can review transaction totals by date, depot, partner, resource, type, and status.",
                "This summary supports daily operation review: what happened, where, with whom, and how much.",
            ],
        }), 200

    @app.get("/organisations/<organisation_id>/transaction-category-report")
    def org_transaction_category_report(organisation_id):
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")

        conn = get_conn()
        try:
            prepare_transaction_reporting(conn, include_user_attribution=True)
            org = get_organisation(conn, organisation_id)

            if not org:
                return jsonify({"error": "Organisation not found"}), 404

            items = fetch_transaction_category_rows(
                conn,
                organisation_id=organisation_id,
                date_from=date_from,
                date_to=date_to,
            )
            total_transactions = get_transaction_total_count(
                conn,
                organisation_id=organisation_id,
                date_from=date_from,
                date_to=date_to,
            )
        finally:
            conn.close()

        return jsonify({
            "report_type": "ORG_TRANSACTION_CATEGORY_REPORT",
            "organisation_id": organisation_id,
            "organisation_name": org["name"],
            "filters": {
                "date_from": date_from,
                "date_to": date_to,
            },
            "total_transactions": total_transactions,
            "category_report_count": len(items),
            "items": items,
            "rules": [
                "Org Admin reporting is organisation-scoped only.",
                "This report exists to provide operational truth by category, type, resource, depot, partner, and user attribution.",
                "Shared logins weaken accountability.",
                "Who, Where, When remains core truth.",
            ],
        }), 200

    @app.get("/organisations/<organisation_id>/transaction-category-report-export")
    def org_transaction_category_report_export(organisation_id):
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        export_format = (request.args.get("format") or "JSON").strip().upper()

        conn = get_conn()
        try:
            prepare_transaction_reporting(conn, include_user_attribution=True)
            org = get_organisation(conn, organisation_id)

            if not org:
                return jsonify({"error": "Organisation not found"}), 404

            items = fetch_transaction_category_rows(
                conn,
                organisation_id=organisation_id,
                date_from=date_from,
                date_to=date_to,
            )
        finally:
            conn.close()

        total_transactions = sum(int(row["transaction_count"]) for row in items)
        total_quantity = sum(int(row["total_quantity"]) for row in items)

        return jsonify({
            "export_type": "ORG_TRANSACTION_CATEGORY_REPORT_EXPORT",
            "export_format": export_format,
            "organisation_id": organisation_id,
            "organisation_name": org["name"],
            "filters": {
                "date_from": date_from,
                "date_to": date_to,
            },
            "totals": {
                "category_row_count": len(items),
                "total_transactions": total_transactions,
                "total_quantity": total_quantity,
            },
            "columns": TRANSACTION_CATEGORY_REPORT_COLUMNS,
            "items": items,
            "rules": [
                "Org transaction category exports are organisation-scoped only.",
                "Exported reporting supports the organisation's own operational records.",
                "The report preserves who, where, when through user attribution, depot, partner, and timestamp ranges.",
            ],
        }), 200

    @app.get("/organisations/<organisation_id>/transaction-category-report.csv")
    def org_transaction_category_report_csv_export(organisation_id):
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")

        conn = get_conn()
        try:
            prepare_transaction_reporting(conn, include_user_attribution=True)
            org = get_organisation(conn, organisation_id)

            if not org:
                return jsonify({"error": "Organisation not found"}), 404

            items = fetch_transaction_category_rows(
                conn,
                organisation_id=organisation_id,
                date_from=date_from,
                date_to=date_to,
            )
        finally:
            conn.close()

        csv_text, filename = build_transaction_category_csv(
            items,
            organisation_id=organisation_id,
            date_from=date_from,
            date_to=date_to,
        )

        return Response(
            csv_text,
            mimetype="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename={filename}",
                "X-Pallet-Pro-Export-Type": "ORG_TRANSACTION_CATEGORY_REPORT_CSV",
                "X-Pallet-Pro-Organisation-Id": organisation_id,
            },
        )
