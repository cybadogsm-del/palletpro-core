import importlib
import os
import sys
import unittest


class PalletProSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = os.path.abspath("test_pallet_pro.db")
        os.environ["PALLET_PRO_DB"] = cls.db_path

        for module_name in (
            "pallet_pro_core",
            "audit",
            "db",
            "modules.system_routes",
            "modules.subscription_access",
            "modules.transaction_reporting",
        ):
            sys.modules.pop(module_name, None)

        cls.core = importlib.import_module("pallet_pro_core")
        cls.client = cls.core.app.test_client()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.db_path):
            os.remove(cls.db_path)

    def create_organisation(self, name):
        response = self.client.post("/organisations", json={"name": name})
        self.assertEqual(response.status_code, 201)
        return response.get_json()["organisation_id"]

    def test_health_endpoint(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def test_root_endpoint_matches_health_contract(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["app"], "Pallet Pro Core")

    def test_create_organisation_records_audit_event(self):
        organisation_id = self.create_organisation("Demo Org")

        conn = self.core.get_conn()
        payload = conn.execute(
            "SELECT * FROM organisations WHERE organisation_id = ?",
            (organisation_id,),
        ).fetchone()
        self.assertIsNotNone(payload)
        self.assertEqual(payload["name"], "Demo Org")
        self.assertTrue(payload["organisation_id"].startswith("org_"))

        audit_row = conn.execute(
            """
            SELECT *
            FROM audit_events
            WHERE entity_type = 'Organisation'
              AND entity_id = ?
              AND action = 'CREATE'
            """,
            (organisation_id,),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(audit_row)

    def test_pricing_table_route_is_registered(self):
        response = self.client.get("/pricing-table")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("settings", payload)
        self.assertIn("items", payload)
        self.assertGreater(len(payload["items"]), 0)

    def test_global_admin_pricing_dashboard_route_is_registered(self):
        response = self.client.get("/global-admin/pricing-dashboard")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["dashboard_type"], "GLOBAL_ADMIN_PRICING_DASHBOARD")
        self.assertIn("pricing_plans", payload)
        self.assertIn("temporary_user_access_summary", payload)

    def test_subscription_dashboard_route_is_registered(self):
        organisation_id = self.create_organisation("Subscription Org")

        response = self.client.get(f"/organisations/{organisation_id}/subscription-dashboard")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["dashboard_type"], "ORG_ADMIN_SUBSCRIPTION_DASHBOARD")
        self.assertEqual(payload["organisation_id"], organisation_id)
        self.assertEqual(payload["access_status"]["access_state"], "ACTIVE")

    def test_temporary_user_list_route_is_registered(self):
        organisation_id = self.create_organisation("Temporary User Org")

        response = self.client.get(f"/organisations/{organisation_id}/temporary-users")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["organisation_id"], organisation_id)
        self.assertEqual(payload["count"], 0)

    def test_org_transaction_summary_route_is_registered(self):
        organisation_id = self.create_organisation("Reporting Org")

        response = self.client.get(f"/organisations/{organisation_id}/transaction-summary")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["summary_type"], "ORG_ADMIN_TRANSACTION_SUMMARY")
        self.assertEqual(payload["organisation_id"], organisation_id)
        self.assertEqual(payload["summary"]["total_transactions"], 0)

    def test_org_transaction_category_routes_are_registered(self):
        organisation_id = self.create_organisation("Category Reporting Org")

        report_response = self.client.get(
            f"/organisations/{organisation_id}/transaction-category-report"
        )
        self.assertEqual(report_response.status_code, 200)
        report_payload = report_response.get_json()
        self.assertEqual(report_payload["report_type"], "ORG_TRANSACTION_CATEGORY_REPORT")
        self.assertEqual(report_payload["total_transactions"], 0)
        self.assertEqual(report_payload["category_report_count"], 0)

        export_response = self.client.get(
            f"/organisations/{organisation_id}/transaction-category-report-export"
        )
        self.assertEqual(export_response.status_code, 200)
        export_payload = export_response.get_json()
        self.assertEqual(
            export_payload["export_type"],
            "ORG_TRANSACTION_CATEGORY_REPORT_EXPORT",
        )
        self.assertEqual(export_payload["totals"]["total_transactions"], 0)
        self.assertEqual(export_payload["totals"]["category_row_count"], 0)

        csv_response = self.client.get(
            f"/organisations/{organisation_id}/transaction-category-report.csv"
        )
        self.assertEqual(csv_response.status_code, 200)
        self.assertEqual(
            csv_response.headers["X-Pallet-Pro-Export-Type"],
            "ORG_TRANSACTION_CATEGORY_REPORT_CSV",
        )
        self.assertEqual(
            csv_response.get_data(as_text=True).strip(),
            "transaction_type,direction,resource_type,resource_name,depot_name,partner_name,submitted_by_user_id,submitted_by_display_name,transaction_count,total_quantity,first_transaction_at,last_transaction_at",
        )


    def test_error_log_routes_are_registered(self):
        response = self.client.get("/global-admin/error-log")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["log_type"], "GLOBAL_ADMIN_ERROR_LOG")
        self.assertEqual(payload["count"], 0)

    def test_error_alerts_route_is_registered(self):
        response = self.client.get("/global-admin/error-alerts")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["alert_type"], "GLOBAL_ADMIN_ERROR_ALERTS")
        self.assertFalse(payload["has_unreviewed_alerts"])


if __name__ == "__main__":
    unittest.main()
