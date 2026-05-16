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


    def _create_user(self, organisation_id, display_name, role="USER"):
        resp = self.client.post("/global-admin/users", json={
            "organisation_id": organisation_id,
            "display_name": display_name,
            "role": role,
            "access_status": "ACTIVE",
            "created_by_display_name": "Test",
            "confirmation_text": "CREATE USER",
        })
        self.assertEqual(resp.status_code, 201)
        return resp.get_json()["user"]["user_id"]

    def test_admin_handover_full_lifecycle(self):
        organisation_id = self.create_organisation("Handover Org")

        self._create_user(organisation_id, "Alice Admin", role="ORG_ADMIN")
        incoming_user_id = self._create_user(organisation_id, "Bob Incoming", role="USER")

        initiate = self.client.post(
            f"/organisations/{organisation_id}/admin-handover",
            json={
                "incoming_admin_user_id": incoming_user_id,
                "overlap_days": 7,
                "initiated_by_display_name": "Alice Admin",
            },
        )
        self.assertEqual(initiate.status_code, 201)
        payload = initiate.get_json()
        self.assertEqual(payload["status"], "ACTIVE")
        self.assertEqual(payload["overlap_days"], 7)
        handover_id = payload["handover_id"]

        view = self.client.get(f"/organisations/{organisation_id}/admin-handover")
        self.assertEqual(view.status_code, 200)
        self.assertIsNotNone(view.get_json()["active_handover"])

        complete = self.client.post(
            f"/organisations/{organisation_id}/admin-handover/{handover_id}/complete",
            json={"completed_by_display_name": "Bob Incoming"},
        )
        self.assertEqual(complete.status_code, 200)
        self.assertEqual(complete.get_json()["status"], "COMPLETED")

    def test_admin_handover_cancel(self):
        organisation_id = self.create_organisation("Cancel Handover Org")

        self._create_user(organisation_id, "Carol Admin", role="ORG_ADMIN")
        incoming_user_id = self._create_user(organisation_id, "Dave Incoming", role="USER")

        initiate = self.client.post(
            f"/organisations/{organisation_id}/admin-handover",
            json={"incoming_admin_user_id": incoming_user_id, "initiated_by_display_name": "Carol Admin"},
        )
        self.assertEqual(initiate.status_code, 201)
        handover_id = initiate.get_json()["handover_id"]

        cancel = self.client.post(
            f"/organisations/{organisation_id}/admin-handover/{handover_id}/cancel",
            json={"cancelled_by_display_name": "Carol Admin"},
        )
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(cancel.get_json()["status"], "CANCELLED")

    def test_admin_handover_expiry_sweep(self):
        response = self.client.post("/global-admin/admin-handover-expiry-sweep")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["sweep_type"], "ADMIN_HANDOVER_EXPIRY_SWEEP")
        self.assertIn("completed_count", payload)

    def test_emergency_appoint_admin(self):
        organisation_id = self.create_organisation("Emergency Appoint Org")
        user_id = self._create_user(organisation_id, "Eve Emergency", role="USER")

        response = self.client.post(
            f"/global-admin/organisations/{organisation_id}/appoint-admin",
            json={"user_id": user_id, "appointed_by_display_name": "Global Admin"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["new_role"], "ORG_ADMIN")

    def _create_depot(self, organisation_id, name):
        resp = self.client.post(
            "/depots",
            json={"organisation_id": organisation_id, "name": name},
        )
        self.assertEqual(resp.status_code, 201)
        return resp.get_json()["depot_id"]

    def test_stocktake_initiate_with_no_balances(self):
        organisation_id = self.create_organisation("Stocktake Empty Org")
        depot_id = self._create_depot(organisation_id, "Depot A")

        resp = self.client.post(
            f"/organisations/{organisation_id}/stocktake",
            json={"depot_id": depot_id, "initiated_by_display_name": "Alice"},
        )
        self.assertEqual(resp.status_code, 201)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "IN_PROGRESS")
        self.assertEqual(payload["total_lines"], 0)

    def _create_category(self, organisation_id, name):
        req = self.client.post("/category-requests", json={
            "organisation_id": organisation_id,
            "requested_name": name,
            "submitted_by_display_name": "Test",
        })
        self.assertEqual(req.status_code, 201)
        cat_req_id = req.get_json()["category_request_id"]
        approve = self.client.post(f"/category-requests/{cat_req_id}/approve")
        self.assertEqual(approve.status_code, 200)
        return approve.get_json()["category_id"]

    def _create_resource(self, organisation_id, category_id, name):
        resp = self.client.post("/resources", json={
            "organisation_id": organisation_id,
            "category_id": category_id,
            "name": name,
            "resource_type": "PALLET",
            "unit_type": "UNIT",
        })
        self.assertEqual(resp.status_code, 201)
        return resp.get_json()["resource_id"]

    def test_stocktake_full_lifecycle_with_variance(self):
        organisation_id = self.create_organisation("Stocktake Full Org")
        depot_id = self._create_depot(organisation_id, "Main Depot")
        category_id = self._create_category(organisation_id, "Pallets")
        resource_id = self._create_resource(organisation_id, category_id, "Test Pallet")

        ob_resp = self.client.post("/opening-balances", json={
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "resource_id": resource_id,
            "quantity": 50,
        })
        self.assertIn(ob_resp.status_code, (200, 201))

        resp = self.client.post(
            f"/organisations/{organisation_id}/stocktake",
            json={"depot_id": depot_id, "initiated_by_display_name": "Alice"},
        )
        self.assertEqual(resp.status_code, 201)
        stocktake_id = resp.get_json()["stocktake_id"]
        self.assertEqual(resp.get_json()["total_lines"], 1)

        detail = self.client.get(f"/organisations/{organisation_id}/stocktake/{stocktake_id}")
        self.assertEqual(detail.status_code, 200)
        lines = detail.get_json()["lines"]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["expected_quantity"], 50)
        line_id = lines[0]["stocktake_line_id"]

        count_resp = self.client.patch(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/lines/{line_id}",
            json={"counted_quantity": 45, "counted_by_display_name": "Bob"},
        )
        self.assertEqual(count_resp.status_code, 200)
        self.assertEqual(count_resp.get_json()["variance"], -5)

        report = self.client.get(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/variance-report"
        )
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.get_json()["summary"]["variance_line_count"], 1)

        submit = self.client.post(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/submit",
            json={"submitted_by_display_name": "Bob"},
        )
        self.assertEqual(submit.status_code, 200)
        self.assertEqual(submit.get_json()["status"], "PENDING_REVIEW")
        self.assertEqual(submit.get_json()["variance_lines"], 1)

        # Posting without review should be blocked
        blocked = self.client.post(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/post",
            json={"posted_by_display_name": "Alice"},
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(blocked.get_json()["error"], "REVIEW_INCOMPLETE")

        # Bulk-accept all variance lines
        bulk = self.client.post(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/bulk-accept",
            json={"reviewed_by_display_name": "Alice"},
        )
        self.assertEqual(bulk.status_code, 200)
        self.assertEqual(bulk.get_json()["accepted_count"], 1)

        # Check lines are grouped by resource type in detail view
        detail = self.client.get(f"/organisations/{organisation_id}/stocktake/{stocktake_id}")
        self.assertIn("lines_by_resource_type", detail.get_json())
        self.assertIn("PALLET", detail.get_json()["lines_by_resource_type"])

        post_resp = self.client.post(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/post",
            json={"posted_by_display_name": "Alice"},
        )
        self.assertEqual(post_resp.status_code, 200)
        payload = post_resp.get_json()
        self.assertEqual(payload["status"], "POSTED")
        self.assertEqual(payload["adjustments_posted"], 1)
        self.assertEqual(payload["adjustment_transactions"][0]["direction"], "OUT")
        self.assertEqual(payload["adjustment_transactions"][0]["quantity"], 5)

    def test_stocktake_individual_accept_and_reject(self):
        organisation_id = self.create_organisation("Stocktake Review Org")
        depot_id = self._create_depot(organisation_id, "Review Depot")
        category_id = self._create_category(organisation_id, "Review Cat")
        resource_id = self._create_resource(organisation_id, category_id, "Review Pallet")

        self.client.post("/opening-balances", json={
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "resource_id": resource_id,
            "quantity": 100,
        })

        resp = self.client.post(
            f"/organisations/{organisation_id}/stocktake",
            json={"depot_id": depot_id, "initiated_by_display_name": "Alice"},
        )
        stocktake_id = resp.get_json()["stocktake_id"]

        lines = self.client.get(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}"
        ).get_json()["lines"]
        line_id = lines[0]["stocktake_line_id"]

        self.client.patch(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/lines/{line_id}",
            json={"counted_quantity": 90, "counted_by_display_name": "Bob"},
        )

        self.client.post(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/submit",
            json={"submitted_by_display_name": "Bob"},
        )

        # Reject the individual line
        reject = self.client.post(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/lines/{line_id}/reject",
            json={"reviewed_by_display_name": "Alice", "rejection_reason": "Counting error"},
        )
        self.assertEqual(reject.status_code, 200)
        self.assertEqual(reject.get_json()["review_status"], "REJECTED")

        # Post — no adjustments since the only variance was rejected
        post_resp = self.client.post(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/post",
            json={"posted_by_display_name": "Alice"},
        )
        self.assertEqual(post_resp.status_code, 200)
        self.assertEqual(post_resp.get_json()["adjustments_posted"], 0)
        self.assertEqual(post_resp.get_json()["rejected_lines"], 1)

    def test_stocktake_cancel(self):
        organisation_id = self.create_organisation("Stocktake Cancel Org")
        depot_id = self._create_depot(organisation_id, "Depot Cancel")

        resp = self.client.post(
            f"/organisations/{organisation_id}/stocktake",
            json={"depot_id": depot_id, "initiated_by_display_name": "Alice"},
        )
        stocktake_id = resp.get_json()["stocktake_id"]

        cancel = self.client.post(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/cancel",
            json={"cancelled_by_display_name": "Alice"},
        )
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(cancel.get_json()["status"], "CANCELLED")

    def test_stock_position_org_and_depot(self):
        organisation_id = self.create_organisation("Stock Position Org")
        depot_id = self._create_depot(organisation_id, "Stock Depot")
        category_id = self._create_category(organisation_id, "Stock Cat")

        chep_id = self._create_resource(organisation_id, category_id, "CHEP Pallet")
        loscam_id = self._create_resource(organisation_id, category_id, "LOSCAM Pallet")
        self._create_resource(organisation_id, category_id, "Plain Pallet")  # no balance

        self.client.post("/opening-balances", json={
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "resource_id": chep_id,
            "quantity": 120,
        })
        self.client.post("/opening-balances", json={
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "resource_id": loscam_id,
            "quantity": 80,
        })

        # Org-wide: only CHEP and LOSCAM appear (Plain has zero balance)
        org_resp = self.client.get(f"/organisations/{organisation_id}/stock-position")
        self.assertEqual(org_resp.status_code, 200)
        payload = org_resp.get_json()
        self.assertEqual(payload["report_type"], "ORG_STOCK_POSITION")
        self.assertEqual(payload["summary"]["total_resources_with_stock"], 2)
        self.assertIn("PALLET", payload["by_resource_type"])
        names = [i["resource_name"] for i in payload["by_resource_type"]["PALLET"]]
        self.assertIn("CHEP Pallet", names)
        self.assertIn("LOSCAM Pallet", names)
        self.assertNotIn("Plain Pallet", names)

        # Depot-level
        depot_resp = self.client.get(
            f"/organisations/{organisation_id}/depots/{depot_id}/stock-position"
        )
        self.assertEqual(depot_resp.status_code, 200)
        dp = depot_resp.get_json()
        self.assertEqual(dp["report_type"], "DEPOT_STOCK_POSITION")
        self.assertEqual(dp["summary"]["total_resources_with_stock"], 2)
        pallet_items = dp["by_resource_type"]["PALLET"]
        quantities = {i["resource_name"]: i["quantity"] for i in pallet_items}
        self.assertEqual(quantities["CHEP Pallet"], 120)
        self.assertEqual(quantities["LOSCAM Pallet"], 80)
        self.assertNotIn("Plain Pallet", quantities)

    def test_stocktake_list_and_global_summary(self):
        resp = self.client.get("/global-admin/stocktake-summary")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["summary_type"], "GLOBAL_ADMIN_STOCKTAKE_SUMMARY")
        self.assertIn("total_sessions", payload)

    def test_module_flags_list_route_is_registered(self):
        response = self.client.get("/global-admin/module-flags")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["dashboard_type"], "GLOBAL_ADMIN_MODULE_FLAGS")
        self.assertIn("items", payload)
        self.assertGreater(payload["total_modules"], 0)

    def test_module_flags_disable_and_enable(self):
        response = self.client.post(
            "/global-admin/module-flags/partners/disable",
            json={"reason": "smoke test", "disabled_by_display_name": "Test Admin"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["is_enabled"])

        response = self.client.post(
            "/global-admin/module-flags/partners/enable",
            json={"enabled_by_display_name": "Test Admin"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["is_enabled"])

    def test_module_flags_always_on_cannot_be_disabled(self):
        response = self.client.post("/global-admin/module-flags/system/disable")
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

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
