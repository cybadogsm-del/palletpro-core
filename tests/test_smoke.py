import importlib
import os
import sys
import unittest


_TEST_MASTER_KEY = "test-master-key-smoke-suite-abc123"


class PalletProSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = os.path.abspath("test_pallet_pro.db")
        os.environ["PALLET_PRO_DB"] = cls.db_path
        os.environ["PALLET_PRO_MASTER_KEY"] = _TEST_MASTER_KEY

        for module_name in list(sys.modules.keys()):
            if module_name.startswith(("pallet_pro_core", "modules.", "db", "audit")):
                sys.modules.pop(module_name, None)

        cls.core = importlib.import_module("pallet_pro_core")
        cls.client = cls.core.app.test_client()
        cls.client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {_TEST_MASTER_KEY}"

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

    def _set_opening_balance(self, organisation_id, depot_id, resource_id, quantity):
        resp = self.client.post("/opening-balances", json={
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "resource_id": resource_id,
            "quantity": quantity,
        })
        self.assertIn(resp.status_code, (200, 201))

    def _create_and_post_transaction(
        self,
        organisation_id,
        depot_id,
        resource_id,
        quantity,
        direction="OUT",
    ):
        create = self.client.post("/transactions", json={
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "transaction_type": "Movement",
            "resource_id": resource_id,
            "quantity": quantity,
            "direction": direction,
            "submitted_by_display_name": "Field User",
        })
        self.assertEqual(create.status_code, 201)
        self.assertEqual(create.get_json()["status"], "DRAFT")

        transaction_id = create.get_json()["transaction_id"]
        post = self.client.post(f"/transactions/{transaction_id}/post")
        self.assertEqual(post.status_code, 200)
        self.assertEqual(post.get_json()["status"], "POSTED")
        return transaction_id

    def _client_for_user(self, user_id, label="test user key"):
        resp = self.client.post("/auth/keys", json={"user_id": user_id, "label": label})
        self.assertEqual(resp.status_code, 201)
        user_client = self.core.app.test_client()
        user_client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {resp.get_json()['api_key']}"
        return user_client

    def _get_transaction_row(self, transaction_id):
        conn = self.core.get_conn()
        row = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        return dict(row)

    def _get_resource_balance(self, organisation_id, depot_id, resource_id):
        conn = self.core.get_conn()
        row = conn.execute(
            """
            SELECT current_quantity
            FROM balance_projection
            WHERE organisation_id = ? AND depot_id = ? AND resource_id = ?
            """,
            (organisation_id, depot_id, resource_id),
        ).fetchone()
        conn.close()
        return row["current_quantity"] if row else None

    def _get_transaction_ledger_count(self, transaction_id):
        conn = self.core.get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM ledger_entries WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        conn.close()
        return row["count"]

    def _get_table_count(self, table_name):
        conn = self.core.get_conn()
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if not exists:
            conn.close()
            return 0
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
        conn.close()
        return row["count"]

    def _assert_no_sensitive_keys(self, value):
        forbidden_keys = {
            "api_key",
            "key_hash",
            "password",
            "password_hash",
            "setup_token",
            "token",
            "token_hash",
            "raw_token",
            "session_token",
            "credential_private_key",
            "private_key",
            "secret",
            "env",
            "environment",
            "card_number",
            "cvv",
            "bank_account",
            "bsb",
        }

        def walk(node):
            if isinstance(node, dict):
                self.assertTrue(
                    forbidden_keys.isdisjoint(node.keys()),
                    f"Sensitive key exposed: {forbidden_keys.intersection(node.keys())}",
                )
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(value)

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

    def test_stocktake_lifecycle_requires_review_before_ledger_adjustment(self):
        organisation_id = self.create_organisation("Stocktake Proof Org")
        depot_id = self._create_depot(organisation_id, "Proof Depot")
        category_id = self._create_category(organisation_id, "Proof Cat")
        counted_resource_id = self._create_resource(
            organisation_id,
            category_id,
            "Counted Pallet",
        )
        zero_balance_resource_id = self._create_resource(
            organisation_id,
            category_id,
            "Zero Balance Pallet",
        )
        org_admin_id = self._create_user(organisation_id, "Stocktake Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(organisation_id, "Stocktake Field", role="USER")
        org_admin_client = self._client_for_user(org_admin_id, "stocktake admin key")
        field_client = self._client_for_user(field_user_id, "stocktake field key")
        self._set_opening_balance(organisation_id, depot_id, counted_resource_id, 50)

        ledger_count_before = self._get_table_count("ledger_entries")
        transaction_count_before = self._get_table_count("transactions")

        initiate = org_admin_client.post(
            f"/organisations/{organisation_id}/stocktake",
            json={
                "depot_id": depot_id,
                "initiated_by_display_name": "Stocktake Admin",
                "notes": "Proof test",
            },
        )
        self.assertEqual(initiate.status_code, 201)
        self.assertEqual(initiate.get_json()["status"], "IN_PROGRESS")
        self.assertEqual(initiate.get_json()["total_lines"], 2)
        stocktake_id = initiate.get_json()["stocktake_id"]

        detail = org_admin_client.get(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}"
        )
        self.assertEqual(detail.status_code, 200)
        lines = detail.get_json()["lines"]
        self.assertEqual(len(lines), 2)
        expected_by_resource = {line["resource_id"]: line["expected_quantity"] for line in lines}
        self.assertEqual(expected_by_resource[counted_resource_id], 50)
        self.assertEqual(expected_by_resource[zero_balance_resource_id], 0)

        lines_by_resource = {line["resource_id"]: line for line in lines}
        counted_line_id = lines_by_resource[counted_resource_id]["stocktake_line_id"]
        zero_line_id = lines_by_resource[zero_balance_resource_id]["stocktake_line_id"]

        count_variance = field_client.patch(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/lines/{counted_line_id}",
            json={"counted_quantity": 45, "counted_by_display_name": "Stocktake Field"},
        )
        self.assertEqual(count_variance.status_code, 200)
        self.assertEqual(count_variance.get_json()["variance"], -5)

        count_zero = field_client.patch(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/lines/{zero_line_id}",
            json={"counted_quantity": 0, "counted_by_display_name": "Stocktake Field"},
        )
        self.assertEqual(count_zero.status_code, 200)
        self.assertEqual(count_zero.get_json()["variance"], 0)

        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)
        self.assertEqual(self._get_table_count("transactions"), transaction_count_before)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, counted_resource_id), 50)

        submit = field_client.post(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/submit",
            json={"submitted_by_display_name": "Stocktake Field"},
        )
        self.assertEqual(submit.status_code, 200)
        self.assertEqual(submit.get_json()["status"], "PENDING_REVIEW")
        self.assertEqual(submit.get_json()["variance_lines"], 1)

        field_accept = field_client.post(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/lines/{counted_line_id}/accept",
            json={"reviewed_by_display_name": "Stocktake Field"},
        )
        self.assertEqual(field_accept.status_code, 403)

        accept = org_admin_client.post(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/lines/{counted_line_id}/accept",
            json={"reviewed_by_display_name": "Stocktake Admin"},
        )
        self.assertEqual(accept.status_code, 200)
        self.assertEqual(accept.get_json()["review_status"], "ACCEPTED")

        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)
        self.assertEqual(self._get_table_count("transactions"), transaction_count_before)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, counted_resource_id), 50)

        field_post = field_client.post(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/post",
            json={"posted_by_display_name": "Stocktake Field"},
        )
        self.assertEqual(field_post.status_code, 403)

        post = org_admin_client.post(
            f"/organisations/{organisation_id}/stocktake/{stocktake_id}/post",
            json={"posted_by_display_name": "Stocktake Admin"},
        )
        self.assertEqual(post.status_code, 200)
        self.assertEqual(post.get_json()["status"], "POSTED")
        self.assertEqual(post.get_json()["adjustments_posted"], 1)
        adjustment = post.get_json()["adjustment_transactions"][0]
        self.assertEqual(adjustment["direction"], "OUT")
        self.assertEqual(adjustment["quantity"], 5)
        adjustment_transaction_id = adjustment["transaction_id"]

        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before + 1)
        self.assertEqual(self._get_table_count("transactions"), transaction_count_before + 1)
        self.assertEqual(self._get_transaction_ledger_count(adjustment_transaction_id), 1)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, counted_resource_id), 45)

    def test_stocktake_cross_org_access_is_blocked_for_basic_user(self):
        org_a = self.create_organisation("Stocktake Boundary Org A")
        org_b = self.create_organisation("Stocktake Boundary Org B")
        depot_b = self._create_depot(org_b, "Boundary Depot B")
        category_b = self._create_category(org_b, "Boundary Cat B")
        resource_b = self._create_resource(org_b, category_b, "Boundary Pallet B")
        self._set_opening_balance(org_b, depot_b, resource_b, 25)

        stocktake = self.client.post(
            f"/organisations/{org_b}/stocktake",
            json={"depot_id": depot_b, "initiated_by_display_name": "Boundary Admin"},
        )
        self.assertEqual(stocktake.status_code, 201)
        stocktake_id = stocktake.get_json()["stocktake_id"]

        user_id = self._create_user(org_a, "Stocktake Boundary User", role="USER")
        user_client = self._client_for_user(user_id, "stocktake boundary key")

        detail = user_client.get(f"/organisations/{org_b}/stocktake/{stocktake_id}")
        self.assertEqual(detail.status_code, 403)
        self.assertEqual(detail.get_json()["error"], "ORG_ACCESS_DENIED")

    def test_org_user_listing_permissions_and_sanitized_response(self):
        org_a = self.create_organisation("Org Users A")
        org_b = self.create_organisation("Org Users B")
        org_admin_id = self._create_user(org_a, "Org Users Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(org_a, "Org Users Field", role="USER")
        org_b_user_id = self._create_user(org_b, "Other Org User", role="USER")
        org_admin_client = self._client_for_user(org_admin_id, "org users admin key")
        field_client = self._client_for_user(field_user_id, "org users field key")

        field_list = field_client.get(f"/organisations/{org_a}/users")
        self.assertEqual(field_list.status_code, 403)

        own_list = org_admin_client.get(f"/organisations/{org_a}/users")
        self.assertEqual(own_list.status_code, 200)
        payload = own_list.get_json()
        self.assertEqual(payload["organisation_id"], org_a)
        self.assertGreaterEqual(payload["count"], 2)

        forbidden_keys = {
            "api_key",
            "key_hash",
            "password_hash",
            "setup_token",
            "token_hash",
            "raw_token",
            "credential_private_key",
        }
        for item in payload["items"]:
            user_payload = item["user"]
            self.assertTrue(forbidden_keys.isdisjoint(user_payload.keys()))

        cross_list = org_admin_client.get(f"/organisations/{org_b}/users")
        self.assertEqual(cross_list.status_code, 403)

        sga_list = self.client.get(f"/organisations/{org_b}/users")
        self.assertEqual(sga_list.status_code, 200)
        self.assertIn(org_b_user_id, [item["user"]["user_id"] for item in sga_list.get_json()["items"]])

    def test_route_intelligence_access_boundaries_and_no_business_mutation(self):
        org_a = self.create_organisation("Route Intel Org A")
        org_b = self.create_organisation("Route Intel Org B")
        org_admin_id = self._create_user(org_a, "Route Intel Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(org_a, "Route Intel Field", role="USER")
        same_org_user_id = self._create_user(org_a, "Route Intel Peer", role="USER")
        other_org_user_id = self._create_user(org_b, "Route Intel Other Org", role="USER")
        global_admin_id = self._create_user(org_a, "Route Intel GA", role="GLOBAL_ADMIN")

        org_admin_client = self._client_for_user(org_admin_id, "route intel org admin key")
        field_client = self._client_for_user(field_user_id, "route intel field key")
        same_org_client = self._client_for_user(same_org_user_id, "route intel peer key")
        other_org_client = self._client_for_user(other_org_user_id, "route intel other key")
        global_admin_client = self._client_for_user(global_admin_id, "route intel ga key")
        unauth_client = self.core.app.test_client()

        readonly_paths = [
            f"/users/{field_user_id}/morning-sync",
            f"/users/{field_user_id}/route-history",
            f"/users/{field_user_id}/dropdown-preferences",
        ]

        for path in readonly_paths:
            with self.subTest(path=path, actor="unauthenticated"):
                self.assertEqual(unauth_client.get(path).status_code, 401)

            with self.subTest(path=path, actor="own-user"):
                response = field_client.get(path)
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["user_id"], field_user_id)
                self.assertEqual(payload["organisation_id"], org_a)
                self._assert_no_sensitive_keys(payload)

            with self.subTest(path=path, actor="same-org-basic-user"):
                self.assertEqual(same_org_client.get(path).status_code, 403)

            with self.subTest(path=path, actor="other-org-basic-user"):
                self.assertEqual(other_org_client.get(path).status_code, 403)

            with self.subTest(path=path, actor="same-org-admin"):
                self.assertEqual(org_admin_client.get(path).status_code, 200)

            with self.subTest(path=path, actor="global-admin"):
                self.assertEqual(global_admin_client.get(path).status_code, 200)

            with self.subTest(path=path, actor="super-global-admin"):
                self.assertEqual(self.client.get(path).status_code, 200)

        cross_org_admin_read = org_admin_client.get(f"/users/{other_org_user_id}/morning-sync")
        self.assertEqual(cross_org_admin_read.status_code, 403)

        mutation_counts_before = {
            "transactions": self._get_table_count("transactions"),
            "ledger_entries": self._get_table_count("ledger_entries"),
            "balance_projection": self._get_table_count("balance_projection"),
        }

        preference_body = {
            "entity_type": "resource",
            "ordered_ids": ["res_preferred"],
            "default_id": "res_preferred",
        }

        own_patch = field_client.patch(
            f"/users/{field_user_id}/dropdown-preferences",
            json=preference_body,
        )
        self.assertEqual(own_patch.status_code, 200)

        peer_patch = same_org_client.patch(
            f"/users/{field_user_id}/dropdown-preferences",
            json=preference_body,
        )
        self.assertEqual(peer_patch.status_code, 403)

        cross_org_patch = org_admin_client.patch(
            f"/users/{other_org_user_id}/dropdown-preferences",
            json=preference_body,
        )
        self.assertEqual(cross_org_patch.status_code, 403)

        admin_patch = org_admin_client.patch(
            f"/users/{field_user_id}/dropdown-preferences",
            json=preference_body,
        )
        self.assertEqual(admin_patch.status_code, 200)

        mutation_counts_after = {
            "transactions": self._get_table_count("transactions"),
            "ledger_entries": self._get_table_count("ledger_entries"),
            "balance_projection": self._get_table_count("balance_projection"),
        }
        self.assertEqual(mutation_counts_after, mutation_counts_before)

    def test_route_intelligence_learns_posted_partner_address_patterns(self):
        organisation_id = self.create_organisation("Route Learning Org")
        depot_id = self._create_depot(organisation_id, "Route Learning Depot")
        category_id = self._create_category(organisation_id, "Route Learning Cat")
        resource_id = self._create_resource(organisation_id, category_id, "Route Learning Pallet")
        field_user_id = self._create_user(organisation_id, "Route Learning Driver", role="USER")
        field_client = self._client_for_user(field_user_id, "route learning field key")
        self._set_opening_balance(organisation_id, depot_id, resource_id, 100)

        partner = self.client.post("/partners", json={
            "organisation_id": organisation_id,
            "name": "Route Learning Customer",
            "is_customer": True,
        })
        self.assertEqual(partner.status_code, 201)
        partner_id = partner.get_json()["partner_id"]

        address = self.client.post(f"/partners/{partner_id}/addresses", json={
            "organisation_id": organisation_id,
            "label": "Front Gate",
            "category": "WORK_SITE",
            "address_line_1": "10 Test Road",
            "suburb": "Melbourne",
            "state": "VIC",
            "entry_heading": 270,
            "gate_number": "A",
            "entry_instructions": "Use the western gate",
            "is_default_dispatch_site": True,
        })
        self.assertEqual(address.status_code, 201)
        partner_address_id = address.get_json()["partner_address_id"]

        ledger_count_before = self._get_table_count("ledger_entries")
        transaction_count_before = self._get_table_count("transactions")

        first = field_client.post("/transactions", json={
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "transaction_type": "Movement",
            "resource_id": resource_id,
            "quantity": 7,
            "direction": "OUT",
            "partner_id": partner_id,
            "partner_address_id": partner_address_id,
            "submitted_by_user_id": field_user_id,
            "submitted_by_display_name": "Route Learning Driver",
        })
        self.assertEqual(first.status_code, 201)
        first_transaction_id = first.get_json()["transaction_id"]
        first_post = field_client.post(f"/transactions/{first_transaction_id}/post")
        self.assertEqual(first_post.status_code, 200)

        conn = self.core.get_conn()
        first_txn = conn.execute(
            "SELECT created_at FROM transactions WHERE transaction_id = ?",
            (first_transaction_id,),
        ).fetchone()
        learned = conn.execute(
            """
            SELECT *
            FROM user_route_defaults
            WHERE user_id = ? AND organisation_id = ? AND partner_address_id = ?
            """,
            (field_user_id, organisation_id, partner_address_id),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(learned)
        self.assertEqual(learned["visit_count"], 1)
        self.assertEqual(learned["preferred_resource_id"], resource_id)
        self.assertEqual(learned["preferred_action"], "Dropoff")
        self.assertEqual(learned["last_visited_at"], first_txn["created_at"])

        second = field_client.post("/transactions", json={
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "transaction_type": "Movement",
            "resource_id": resource_id,
            "quantity": 3,
            "direction": "IN",
            "partner_id": partner_id,
            "partner_address_id": partner_address_id,
            "submitted_by_user_id": field_user_id,
            "submitted_by_display_name": "Route Learning Driver",
        })
        self.assertEqual(second.status_code, 201)
        second_transaction_id = second.get_json()["transaction_id"]
        second_post = field_client.post(f"/transactions/{second_transaction_id}/post")
        self.assertEqual(second_post.status_code, 200)

        conn = self.core.get_conn()
        second_txn = conn.execute(
            "SELECT created_at FROM transactions WHERE transaction_id = ?",
            (second_transaction_id,),
        ).fetchone()
        learned = conn.execute(
            """
            SELECT *
            FROM user_route_defaults
            WHERE user_id = ? AND organisation_id = ? AND partner_address_id = ?
            """,
            (field_user_id, organisation_id, partner_address_id),
        ).fetchone()
        conn.close()

        self.assertEqual(learned["visit_count"], 2)
        self.assertEqual(learned["preferred_resource_id"], resource_id)
        self.assertEqual(learned["preferred_action"], "Pickup")
        self.assertEqual(learned["last_visited_at"], second_txn["created_at"])

        expected_counts_after_transactions = {
            "transactions": transaction_count_before + 2,
            "ledger_entries": ledger_count_before + 2,
            "balance_projection": self._get_table_count("balance_projection"),
        }
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), 96)

        sync = field_client.get(f"/users/{field_user_id}/morning-sync")
        self.assertEqual(sync.status_code, 200)
        sync_payload = sync.get_json()
        self.assertEqual(sync_payload["user_id"], field_user_id)
        self.assertEqual(sync_payload["organisation_id"], organisation_id)
        self.assertEqual(sync_payload["route_pool_count"], 1)
        route = sync_payload["predicted_route_pool"][0]
        self.assertEqual(route["partner_address_id"], partner_address_id)
        self.assertEqual(route["partner_id"], partner_id)
        self.assertEqual(route["partner_name"], "Route Learning Customer")
        self.assertEqual(route["label"], "Front Gate")
        self.assertEqual(route["entry_heading"], 270)
        self.assertEqual(route["gate_number"], "A")
        self.assertEqual(route["entry_instructions"], "Use the western gate")
        self.assertEqual(route["visit_count"], 2)
        self.assertEqual(route["last_visited_at"], second_txn["created_at"])
        self.assertEqual(route["prefill"]["resource_id"], resource_id)
        self.assertEqual(route["prefill"]["action"], "Pickup")
        self.assertEqual(route["prefill"]["direction"], "IN")

        history = field_client.get(f"/users/{field_user_id}/route-history")
        self.assertEqual(history.status_code, 200)
        history_payload = history.get_json()
        self.assertEqual(history_payload["count"], 1)
        self.assertEqual(history_payload["items"][0]["visit_count"], 2)

        self.assertEqual(self._get_table_count("transactions"), expected_counts_after_transactions["transactions"])
        self.assertEqual(self._get_table_count("ledger_entries"), expected_counts_after_transactions["ledger_entries"])
        self.assertEqual(self._get_table_count("balance_projection"), expected_counts_after_transactions["balance_projection"])
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), 96)

    def test_partner_management_requires_org_admin_and_preserves_org_boundary(self):
        org_a = self.create_organisation("Partner Mgmt A")
        org_b = self.create_organisation("Partner Mgmt B")
        org_admin_id = self._create_user(org_a, "Partner Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(org_a, "Partner Field", role="USER")
        org_admin_client = self._client_for_user(org_admin_id, "partner admin key")
        field_client = self._client_for_user(field_user_id, "partner field key")

        field_create = field_client.post("/partners", json={
            "organisation_id": org_a,
            "name": "Field Created Partner",
            "is_customer": True,
        })
        self.assertEqual(field_create.status_code, 403)

        create = org_admin_client.post("/partners", json={
            "organisation_id": org_b,
            "name": "Admin Partner",
            "is_customer": True,
        })
        self.assertEqual(create.status_code, 201)
        self.assertEqual(create.get_json()["organisation_id"], org_a)
        partner_id = create.get_json()["partner_id"]

        field_deactivate = field_client.post(f"/partners/{partner_id}/deactivate")
        self.assertEqual(field_deactivate.status_code, 403)

        deactivate = org_admin_client.post(f"/partners/{partner_id}/deactivate")
        self.assertEqual(deactivate.status_code, 200)
        self.assertFalse(deactivate.get_json()["is_active"])

        org_b_partner = self.client.post("/partners", json={
            "organisation_id": org_b,
            "name": "Other Org Partner",
            "is_supplier": True,
        })
        self.assertEqual(org_b_partner.status_code, 201)
        cross_deactivate = org_admin_client.post(
            f"/partners/{org_b_partner.get_json()['partner_id']}/deactivate"
        )
        self.assertEqual(cross_deactivate.status_code, 403)

    def test_partner_address_management_requires_org_admin_and_preserves_org_boundary(self):
        org_a = self.create_organisation("Partner Address Mgmt A")
        org_b = self.create_organisation("Partner Address Mgmt B")
        org_admin_id = self._create_user(org_a, "Partner Address Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(org_a, "Partner Address Field", role="USER")
        org_admin_client = self._client_for_user(org_admin_id, "paddr admin key")
        field_client = self._client_for_user(field_user_id, "paddr field key")
        cross_org_field_id = self._create_user(org_b, "Cross Org Field", role="USER")
        cross_org_field_client = self._client_for_user(cross_org_field_id, "cross org field key")

        partner = org_admin_client.post("/partners", json={
            "organisation_id": org_a,
            "name": "Preferred Supplier",
            "is_supplier": True,
        })
        self.assertEqual(partner.status_code, 201)
        partner_id = partner.get_json()["partner_id"]

        mutation_counts_before = {
            "transactions": self._get_table_count("transactions"),
            "ledger_entries": self._get_table_count("ledger_entries"),
            "balance_projection": self._get_table_count("balance_projection"),
            "resource_losses": self._get_table_count("resource_losses"),
            "organisation_subscriptions": self._get_table_count("organisation_subscriptions"),
            "billing_export_runs": self._get_table_count("billing_export_runs"),
        }

        field_create = field_client.post(f"/partners/{partner_id}/addresses", json={
            "organisation_id": org_a,
            "label": "Field Site",
            "category": "WORK_SITE",
        })
        self.assertEqual(field_create.status_code, 403)

        own_list = field_client.get(f"/partners/{partner_id}/addresses")
        self.assertEqual(own_list.status_code, 403)

        partner_address = org_admin_client.post(f"/partners/{partner_id}/addresses", json={
            "organisation_id": org_a,
            "label": "Admin Site",
            "category": "WAREHOUSE",
            "address_line_1": "10 Admin Road",
            "state": "VIC",
            "is_primary": True,
            "is_default_dispatch_site": True,
            "is_default_receiving_site": True,
            "entry_heading": 180,
            "gate_number": "7",
            "door_number": "2",
        })
        self.assertEqual(partner_address.status_code, 201)
        partner_address_payload = partner_address.get_json()
        partner_address_id = partner_address_payload["partner_address_id"]
        self._assert_no_sensitive_keys(partner_address_payload)

        # Cross-org basic user is also blocked
        cross_org_field_list = cross_org_field_client.get(f"/partners/{partner_id}/addresses")
        self.assertEqual(cross_org_field_list.status_code, 403)

        org_admin_list = org_admin_client.get(f"/partners/{partner_id}/addresses")
        self.assertEqual(org_admin_list.status_code, 200)
        list_payload = org_admin_list.get_json()
        self.assertEqual(list_payload["partner_id"], partner_id)
        self.assertEqual(list_payload["count"], 1)
        self._assert_no_sensitive_keys(list_payload)

        update = org_admin_client.patch(f"/partner-addresses/{partner_address_id}", json={
            "organisation_id": org_a,
            "label": "Updated Admin Site",
            "entry_instructions": "Use back gate",
        })
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.get_json()["label"], "Updated Admin Site")
        self._assert_no_sensitive_keys(update.get_json())

        field_update = field_client.patch(f"/partner-addresses/{partner_address_id}", json={
            "organisation_id": org_a,
            "label": "Should Never Happen",
        })
        self.assertEqual(field_update.status_code, 403)

        deactivate = org_admin_client.post(f"/partner-addresses/{partner_address_id}/deactivate", json={
            "organisation_id": org_a,
            "deactivated_by_display_name": "Partner Address Admin",
        })
        self.assertEqual(deactivate.status_code, 200)
        self.assertFalse(deactivate.get_json()["is_active"])
        self._assert_no_sensitive_keys(deactivate.get_json())

        field_deactivate = field_client.post(f"/partner-addresses/{partner_address_id}/deactivate", json={
            "organisation_id": org_a,
            "deactivated_by_display_name": "Partner Address Field",
        })
        self.assertEqual(field_deactivate.status_code, 403)

        mutation_counts_after = {
            "transactions": self._get_table_count("transactions"),
            "ledger_entries": self._get_table_count("ledger_entries"),
            "balance_projection": self._get_table_count("balance_projection"),
            "resource_losses": self._get_table_count("resource_losses"),
            "organisation_subscriptions": self._get_table_count("organisation_subscriptions"),
            "billing_export_runs": self._get_table_count("billing_export_runs"),
        }
        self.assertEqual(mutation_counts_before, mutation_counts_after)

    def test_partner_address_org_admin_cannot_manage_other_orgs_and_field_basic_is_cross_org_blocked(self):
        org_a = self.create_organisation("Partner Address Boundary A")
        org_b = self.create_organisation("Partner Address Boundary B")
        org_admin_a_id = self._create_user(org_a, "Address Boundary Admin A", role="ORG_ADMIN")
        org_admin_b_id = self._create_user(org_b, "Address Boundary Admin B", role="ORG_ADMIN")
        field_a_id = self._create_user(org_a, "Address Boundary Field A", role="USER")

        org_admin_a_client = self._client_for_user(org_admin_a_id, "address boundary admin a key")
        org_admin_b_client = self._client_for_user(org_admin_b_id, "address boundary admin b key")
        field_a_client = self._client_for_user(field_a_id, "address boundary field a key")

        partner_b = org_admin_b_client.post("/partners", json={
            "organisation_id": org_b,
            "name": "Boundary Partner",
            "is_customer": True,
        })
        self.assertEqual(partner_b.status_code, 201)
        partner_b_id = partner_b.get_json()["partner_id"]

        partner_b_address = org_admin_b_client.post(f"/partners/{partner_b_id}/addresses", json={
            "organisation_id": org_b,
            "label": "Boundary Address",
            "category": "WORK_SITE",
            "address_line_1": "99 Boundary Road",
        })
        self.assertEqual(partner_b_address.status_code, 201)
        partner_b_address_id = partner_b_address.get_json()["partner_address_id"]

        cross_admin_list = org_admin_a_client.get(f"/partners/{partner_b_id}/addresses")
        self.assertEqual(cross_admin_list.status_code, 403)

        cross_admin_create = org_admin_a_client.post(f"/partners/{partner_b_id}/addresses", json={
            "organisation_id": org_b,
            "label": "Cross Admin Add",
            "category": "WORK_SITE",
            "address_line_1": "1 Foreign Road",
        })
        self.assertEqual(cross_admin_create.status_code, 403)

        cross_admin_update = org_admin_a_client.patch(f"/partner-addresses/{partner_b_address_id}", json={
            "organisation_id": org_b,
            "label": "Cross Admin Update",
        })
        self.assertEqual(cross_admin_update.status_code, 403)

        cross_admin_deactivate = org_admin_a_client.post(
            f"/partner-addresses/{partner_b_address_id}/deactivate",
            json={"organisation_id": org_b, "deactivated_by_display_name": "Address Boundary Admin A"},
        )
        self.assertEqual(cross_admin_deactivate.status_code, 403)

        cross_field_list = field_a_client.get(f"/partners/{partner_b_id}/addresses")
        self.assertEqual(cross_field_list.status_code, 403)

        cross_field_create = field_a_client.post(f"/partners/{partner_b_id}/addresses", json={
            "organisation_id": org_b,
            "label": "Cross Field Add",
            "category": "WORK_SITE",
            "address_line_1": "1 Foreign Road",
        })
        self.assertEqual(cross_field_create.status_code, 403)

        cross_field_update = field_a_client.patch(f"/partner-addresses/{partner_b_address_id}", json={
            "organisation_id": org_b,
            "label": "Cross Field Update",
        })
        self.assertEqual(cross_field_update.status_code, 403)

        cross_field_deactivate = field_a_client.post(
            f"/partner-addresses/{partner_b_address_id}/deactivate",
            json={"organisation_id": org_b, "deactivated_by_display_name": "Address Boundary Field A"},
        )
        self.assertEqual(cross_field_deactivate.status_code, 403)

    def test_inactive_partner_addresses_are_filtered_out_of_morning_sync_and_defaults(self):
        organisation_id = self.create_organisation("Partner Address Defaults Org")
        category_id = self._create_category(organisation_id, "Route Default Cat")
        resource_id = self._create_resource(organisation_id, category_id, "Route Default Pallet")
        depot_id = self._create_depot(organisation_id, "Route Default Depot")
        self._set_opening_balance(organisation_id, depot_id, resource_id, 50)

        org_admin_id = self._create_user(organisation_id, "Address Default Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(organisation_id, "Address Default Field", role="USER")

        org_admin_client = self._client_for_user(org_admin_id, "address defaults admin key")
        field_client = self._client_for_user(field_user_id, "address defaults field key")

        partner = org_admin_client.post("/partners", json={
            "organisation_id": organisation_id,
            "name": "Default Partner",
            "is_customer": True,
        })
        self.assertEqual(partner.status_code, 201)
        partner_id = partner.get_json()["partner_id"]

        address = org_admin_client.post(f"/partners/{partner_id}/addresses", json={
            "organisation_id": organisation_id,
            "label": "Primary Dispatch",
            "category": "WORK_SITE",
            "address_line_1": "123 Route Road",
            "state": "VIC",
            "is_primary": True,
            "is_default_dispatch_site": True,
        })
        self.assertEqual(address.status_code, 201)
        address_id = address.get_json()["partner_address_id"]

        transaction = field_client.post("/transactions", json={
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "transaction_type": "Movement",
            "resource_id": resource_id,
            "quantity": 4,
            "direction": "OUT",
            "partner_id": partner_id,
            "partner_address_id": address_id,
            "submitted_by_user_id": field_user_id,
            "submitted_by_display_name": "Address Default Field",
        })
        self.assertEqual(transaction.status_code, 201)
        transaction_id = transaction.get_json()["transaction_id"]
        posted = field_client.post(f"/transactions/{transaction_id}/post")
        self.assertEqual(posted.status_code, 200)

        sync_before = field_client.get(f"/users/{field_user_id}/morning-sync")
        self.assertEqual(sync_before.status_code, 200)
        sync_before_payload = sync_before.get_json()
        self.assertTrue(any(
            route["partner_address_id"] == address_id
            for route in sync_before_payload["predicted_route_pool"]
        ))

        deactivate = org_admin_client.post(f"/partner-addresses/{address_id}/deactivate", json={
            "organisation_id": organisation_id,
            "deactivated_by_display_name": "Address Default Admin",
        })
        self.assertEqual(deactivate.status_code, 200)

        sync_after = field_client.get(f"/users/{field_user_id}/morning-sync")
        self.assertEqual(sync_after.status_code, 200)
        self.assertEqual(sync_after.get_json()["route_pool_count"], 0)

        defaulted_txn = field_client.post("/transactions", json={
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "transaction_type": "Movement",
            "resource_id": resource_id,
            "quantity": 2,
            "direction": "OUT",
            "partner_id": partner_id,
            "submitted_by_user_id": field_user_id,
            "submitted_by_display_name": "Address Default Field",
        })
        self.assertEqual(defaulted_txn.status_code, 201)
        self.assertIsNone(defaulted_txn.get_json()["partner_address_id"])

        explicit_inactive = field_client.post("/transactions", json={
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "transaction_type": "Movement",
            "resource_id": resource_id,
            "quantity": 2,
            "direction": "OUT",
            "partner_id": partner_id,
            "partner_address_id": address_id,
            "submitted_by_user_id": field_user_id,
            "submitted_by_display_name": "Address Default Field",
        })
        self.assertEqual(explicit_inactive.status_code, 404)

    def test_resource_management_requires_org_admin_and_does_not_mutate_ledger(self):
        org_a = self.create_organisation("Resource Mgmt A")
        org_b = self.create_organisation("Resource Mgmt B")
        category_a = self._create_category(org_a, "Resource Mgmt Cat A")
        category_b = self._create_category(org_b, "Resource Mgmt Cat B")
        org_admin_id = self._create_user(org_a, "Resource Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(org_a, "Resource Field", role="USER")
        org_admin_client = self._client_for_user(org_admin_id, "resource admin key")
        field_client = self._client_for_user(field_user_id, "resource field key")
        resource_id = self._create_resource(org_a, category_a, "Managed Resource")

        ledger_count_before = self._get_table_count("ledger_entries")

        field_deactivate = field_client.post(f"/resources/{resource_id}/deactivate")
        self.assertEqual(field_deactivate.status_code, 403)

        deactivate = org_admin_client.post(f"/resources/{resource_id}/deactivate")
        self.assertEqual(deactivate.status_code, 200)
        self.assertFalse(deactivate.get_json()["is_active"])
        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)

        reactivate = org_admin_client.post(f"/resources/{resource_id}/reactivate")
        self.assertEqual(reactivate.status_code, 200)
        self.assertTrue(reactivate.get_json()["is_active"])
        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)

        org_b_resource = self._create_resource(org_b, category_b, "Other Org Resource")
        cross_deactivate = org_admin_client.post(f"/resources/{org_b_resource}/deactivate")
        self.assertEqual(cross_deactivate.status_code, 403)

    def test_depot_management_requires_org_admin_and_does_not_mutate_ledger(self):
        org_a = self.create_organisation("Depot Mgmt A")
        org_b = self.create_organisation("Depot Mgmt B")
        org_admin_id = self._create_user(org_a, "Depot Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(org_a, "Depot Field", role="USER")
        org_admin_client = self._client_for_user(org_admin_id, "depot admin key")
        field_client = self._client_for_user(field_user_id, "depot field key")

        field_create = field_client.post("/depots", json={
            "organisation_id": org_a,
            "name": "Field Depot",
        })
        self.assertEqual(field_create.status_code, 403)

        create = org_admin_client.post("/depots", json={
            "organisation_id": org_b,
            "name": "Admin Depot",
        })
        self.assertEqual(create.status_code, 201)
        self.assertEqual(create.get_json()["organisation_id"], org_a)
        depot_id = create.get_json()["depot_id"]

        ledger_count_before = self._get_table_count("ledger_entries")

        field_deactivate = field_client.post(f"/depots/{depot_id}/deactivate")
        self.assertEqual(field_deactivate.status_code, 403)

        deactivate = org_admin_client.post(f"/depots/{depot_id}/deactivate")
        self.assertEqual(deactivate.status_code, 200)
        self.assertFalse(deactivate.get_json()["is_active"])
        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)

        org_b_depot = self._create_depot(org_b, "Other Org Depot")
        cross_deactivate = org_admin_client.post(f"/depots/{org_b_depot}/deactivate")
        self.assertEqual(cross_deactivate.status_code, 403)

    def test_global_admin_console_routes_require_global_admin_or_sga(self):
        org_id = self.create_organisation("Global Console Org")
        field_user_id = self._create_user(org_id, "Global Console Field", role="USER")
        org_admin_id = self._create_user(org_id, "Global Console Org Admin", role="ORG_ADMIN")
        global_admin_id = self._create_user(org_id, "Global Console GA", role="GLOBAL_ADMIN")
        field_client = self._client_for_user(field_user_id, "global console field key")
        org_admin_client = self._client_for_user(org_admin_id, "global console org admin key")
        global_admin_client = self._client_for_user(global_admin_id, "global console ga key")
        unauth_client = self.core.app.test_client()

        routes = [
            ("GET", "/global-admin/system-control-panel", None),
            ("GET", "/global-admin/access-operations-dashboard", None),
            ("GET", "/global-admin/billing-export-runs", None),
            ("POST", "/global-admin/billing-export-preview", {}),
            ("GET", "/global-admin/pricing-dashboard", None),
        ]

        for method, path, body in routes:
            unauth = getattr(unauth_client, method.lower())(path, json=body) if body is not None else getattr(unauth_client, method.lower())(path)
            self.assertEqual(unauth.status_code, 401, path)

            field = getattr(field_client, method.lower())(path, json=body) if body is not None else getattr(field_client, method.lower())(path)
            self.assertEqual(field.status_code, 403, path)

            org_admin = getattr(org_admin_client, method.lower())(path, json=body) if body is not None else getattr(org_admin_client, method.lower())(path)
            self.assertEqual(org_admin.status_code, 403, path)

            ga = getattr(global_admin_client, method.lower())(path, json=body) if body is not None else getattr(global_admin_client, method.lower())(path)
            self.assertEqual(ga.status_code, 200, path)
            self._assert_no_sensitive_keys(ga.get_json())

            sga = getattr(self.client, method.lower())(path, json=body) if body is not None else getattr(self.client, method.lower())(path)
            self.assertEqual(sga.status_code, 200, path)
            self._assert_no_sensitive_keys(sga.get_json())

    def test_sga_only_global_org_and_user_access_boundaries(self):
        org_a = self.create_organisation("SGA Boundary Org A")
        org_b = self.create_organisation("SGA Boundary Org B")
        org_admin_id = self._create_user(org_a, "SGA Boundary Org Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(org_a, "SGA Boundary Field", role="USER")
        global_admin_id = self._create_user(org_a, "SGA Boundary GA", role="GLOBAL_ADMIN")
        target_user_id = self._create_user(org_b, "SGA Boundary Target", role="USER")
        org_admin_client = self._client_for_user(org_admin_id, "sga boundary org admin key")
        field_client = self._client_for_user(field_user_id, "sga boundary field key")
        global_admin_client = self._client_for_user(global_admin_id, "sga boundary ga key")
        unauth_client = self.core.app.test_client()

        unauth_orgs = unauth_client.get("/global-admin/organisations")
        self.assertEqual(unauth_orgs.status_code, 401)
        field_orgs = field_client.get("/global-admin/organisations")
        self.assertEqual(field_orgs.status_code, 403)
        org_admin_orgs = org_admin_client.get("/global-admin/organisations")
        self.assertEqual(org_admin_orgs.status_code, 403)
        ga_orgs = global_admin_client.get("/global-admin/organisations")
        self.assertEqual(ga_orgs.status_code, 403)
        sga_orgs = self.client.get("/global-admin/organisations")
        self.assertEqual(sga_orgs.status_code, 200)
        self._assert_no_sensitive_keys(sga_orgs.get_json())

        access_body = {
            "access_status": "SUSPENDED",
            "changed_by_display_name": "Boundary Admin",
            "confirmation_text": "CHANGE USER ACCESS",
        }
        unauth_access = unauth_client.post(
            f"/global-admin/users/{target_user_id}/access",
            json=access_body,
        )
        self.assertEqual(unauth_access.status_code, 401)
        field_access = field_client.post(
            f"/global-admin/users/{target_user_id}/access",
            json=access_body,
        )
        self.assertEqual(field_access.status_code, 403)
        org_admin_access = org_admin_client.post(
            f"/global-admin/users/{target_user_id}/access",
            json=access_body,
        )
        self.assertEqual(org_admin_access.status_code, 403)
        ga_access = global_admin_client.post(
            f"/global-admin/users/{target_user_id}/access",
            json=access_body,
        )
        self.assertEqual(ga_access.status_code, 403)
        sga_access = self.client.post(
            f"/global-admin/users/{target_user_id}/access",
            json=access_body,
        )
        self.assertEqual(sga_access.status_code, 200)
        self._assert_no_sensitive_keys(sga_access.get_json())

        ga_cross_users = global_admin_client.get(f"/organisations/{org_b}/users")
        self.assertEqual(ga_cross_users.status_code, 403)
        sga_cross_users = self.client.get(f"/organisations/{org_b}/users")
        self.assertEqual(sga_cross_users.status_code, 200)
        self._assert_no_sensitive_keys(sga_cross_users.get_json())

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

    def test_resource_ledger_routes(self):
        organisation_id = self.create_organisation("Ledger Org")
        depot_id = self._create_depot(organisation_id, "Ledger Depot")
        category_id = self._create_category(organisation_id, "Ledger Cat")
        resource_id = self._create_resource(organisation_id, category_id, "CHEP Pallet")

        self.client.post("/opening-balances", json={
            "organisation_id": organisation_id,
            "depot_id": depot_id,
            "resource_id": resource_id,
            "quantity": 100,
        })

        depot_ledger = self.client.get(
            f"/organisations/{organisation_id}/depots/{depot_id}/resources/{resource_id}/ledger"
        )
        self.assertEqual(depot_ledger.status_code, 200)
        dl = depot_ledger.get_json()
        self.assertEqual(dl["report_type"], "RESOURCE_DEPOT_LEDGER")
        self.assertEqual(dl["current_balance"], 100)
        self.assertEqual(dl["entry_count"], 1)
        self.assertEqual(dl["entries"][0]["running_balance"], 100)
        self.assertEqual(dl["entries"][0]["transaction_type"], "OpeningBalance")

        org_ledger = self.client.get(
            f"/organisations/{organisation_id}/resources/{resource_id}/ledger"
        )
        self.assertEqual(org_ledger.status_code, 200)
        ol = org_ledger.get_json()
        self.assertEqual(ol["report_type"], "RESOURCE_ORG_LEDGER")
        self.assertEqual(ol["total_balance_across_depots"], 100)
        self.assertEqual(ol["by_depot"][0]["current_balance"], 100)

    def test_tcr_approval_posts_correction_without_editing_original_transaction(self):
        organisation_id = self.create_organisation("TCR Approval Org")
        depot_id = self._create_depot(organisation_id, "TCR Depot")
        category_id = self._create_category(organisation_id, "TCR Cat")
        resource_id = self._create_resource(organisation_id, category_id, "TCR Pallet")
        self._set_opening_balance(organisation_id, depot_id, resource_id, 100)
        transaction_id = self._create_and_post_transaction(
            organisation_id,
            depot_id,
            resource_id,
            quantity=10,
            direction="OUT",
        )

        original_before = self._get_transaction_row(transaction_id)
        self.assertEqual(original_before["status"], "POSTED")
        self.assertEqual(self._get_transaction_ledger_count(transaction_id), 1)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), 90)

        request_resp = self.client.post(
            f"/transactions/{transaction_id}/correction-request",
            json={
                "correction_reason": "Quantity was overstated",
                "proposed_quantity": 6,
            },
        )
        self.assertEqual(request_resp.status_code, 201)
        tcr_id = request_resp.get_json()["tcr_id"]

        approve = self.client.post(
            f"/correction-requests/{tcr_id}/approve",
            json={"review_notes": "Approved after docket check"},
        )
        self.assertEqual(approve.status_code, 200)
        approve_payload = approve.get_json()
        self.assertEqual(approve_payload["status"], "APPROVED")
        self.assertTrue(approve_payload["ledger_updated"])
        self.assertEqual(len(approve_payload["correction_transactions"]), 1)

        original_after = self._get_transaction_row(transaction_id)
        for field in (
            "transaction_id",
            "organisation_id",
            "depot_id",
            "transaction_type",
            "resource_id",
            "quantity",
            "direction",
            "status",
            "posted_at",
            "reference_number",
            "org_sequence_number",
        ):
            self.assertEqual(original_after[field], original_before[field])
        self.assertEqual(self._get_transaction_ledger_count(transaction_id), 1)

        conn = self.core.get_conn()
        corrections = conn.execute(
            """
            SELECT *
            FROM transactions
            WHERE correction_of_transaction_id = ?
            """,
            (transaction_id,),
        ).fetchall()
        correction_ledger = conn.execute(
            """
            SELECT le.*
            FROM ledger_entries le
            JOIN transactions t ON t.transaction_id = le.transaction_id
            WHERE t.correction_of_transaction_id = ?
            """,
            (transaction_id,),
        ).fetchall()
        tcr = conn.execute(
            "SELECT * FROM transaction_correction_requests WHERE tcr_id = ?",
            (tcr_id,),
        ).fetchone()
        conn.close()

        self.assertEqual(len(corrections), 1)
        correction = dict(corrections[0])
        self.assertEqual(correction["transaction_type"], "Correction")
        self.assertEqual(correction["status"], "POSTED")
        self.assertEqual(correction["quantity"], 4)
        self.assertEqual(correction["direction"], "IN")
        self.assertEqual(correction["correction_of_transaction_id"], transaction_id)
        self.assertEqual(len(correction_ledger), 1)
        self.assertEqual(correction_ledger[0]["quantity_delta"], 4)
        self.assertEqual(tcr["status"], "APPROVED")
        self.assertEqual(tcr["correction_transaction_id"], correction["transaction_id"])
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), 94)

    def test_tcr_rejection_does_not_mutate_original_transaction_or_ledger(self):
        organisation_id = self.create_organisation("TCR Rejection Org")
        depot_id = self._create_depot(organisation_id, "Reject Depot")
        category_id = self._create_category(organisation_id, "Reject Cat")
        resource_id = self._create_resource(organisation_id, category_id, "Reject Pallet")
        self._set_opening_balance(organisation_id, depot_id, resource_id, 100)
        transaction_id = self._create_and_post_transaction(
            organisation_id,
            depot_id,
            resource_id,
            quantity=8,
            direction="OUT",
        )

        original_before = self._get_transaction_row(transaction_id)
        balance_before = self._get_resource_balance(organisation_id, depot_id, resource_id)
        conn = self.core.get_conn()
        ledger_count_before = conn.execute(
            "SELECT COUNT(*) AS count FROM ledger_entries"
        ).fetchone()["count"]
        transaction_count_before = conn.execute(
            "SELECT COUNT(*) AS count FROM transactions"
        ).fetchone()["count"]
        conn.close()

        request_resp = self.client.post(
            f"/transactions/{transaction_id}/correction-request",
            json={
                "correction_reason": "Requested change was not supported",
                "proposed_quantity": 4,
            },
        )
        self.assertEqual(request_resp.status_code, 201)
        tcr_id = request_resp.get_json()["tcr_id"]

        reject = self.client.post(
            f"/correction-requests/{tcr_id}/reject",
            json={"review_notes": "Original docket is correct"},
        )
        self.assertEqual(reject.status_code, 200)
        self.assertEqual(reject.get_json()["status"], "REJECTED")

        original_after = self._get_transaction_row(transaction_id)
        for field in (
            "transaction_id",
            "organisation_id",
            "depot_id",
            "transaction_type",
            "resource_id",
            "quantity",
            "direction",
            "status",
            "posted_at",
            "reference_number",
            "org_sequence_number",
        ):
            self.assertEqual(original_after[field], original_before[field])

        conn = self.core.get_conn()
        ledger_count_after = conn.execute(
            "SELECT COUNT(*) AS count FROM ledger_entries"
        ).fetchone()["count"]
        transaction_count_after = conn.execute(
            "SELECT COUNT(*) AS count FROM transactions"
        ).fetchone()["count"]
        corrections = conn.execute(
            """
            SELECT *
            FROM transactions
            WHERE correction_of_transaction_id = ?
            """,
            (transaction_id,),
        ).fetchall()
        tcr = conn.execute(
            "SELECT * FROM transaction_correction_requests WHERE tcr_id = ?",
            (tcr_id,),
        ).fetchone()
        conn.close()

        self.assertEqual(ledger_count_after, ledger_count_before)
        self.assertEqual(transaction_count_after, transaction_count_before)
        self.assertEqual(len(corrections), 0)
        self.assertEqual(tcr["status"], "REJECTED")
        self.assertIsNone(tcr["correction_transaction_id"])
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), balance_before)

    def test_resource_loss_rejection_does_not_mutate_transaction_or_ledger_state(self):
        organisation_id = self.create_organisation("Loss Rejection Org")
        depot_id = self._create_depot(organisation_id, "Loss Reject Depot")
        category_id = self._create_category(organisation_id, "Loss Reject Cat")
        resource_id = self._create_resource(organisation_id, category_id, "Loss Reject Pallet")
        user_id = self._create_user(organisation_id, "Loss Field User", role="USER")
        field_client = self._client_for_user(user_id, "loss rejection field key")
        self._set_opening_balance(organisation_id, depot_id, resource_id, 100)

        ledger_count_before = self._get_table_count("ledger_entries")
        transaction_count_before = self._get_table_count("transactions")
        balance_before = self._get_resource_balance(organisation_id, depot_id, resource_id)

        create = field_client.post("/resource-loss", json={
            "depot_id": depot_id,
            "resource_id": resource_id,
            "quantity": 5,
            "loss_type": "DAMAGED",
            "loss_reason": "Broken during unloading",
            "loss_date": "2026-06-06",
        })
        self.assertEqual(create.status_code, 201)
        create_payload = create.get_json()
        self.assertEqual(create_payload["organisation_id"], organisation_id)
        self.assertEqual(create_payload["status"], "PENDING_REVIEW")
        loss_id = create_payload["loss_id"]

        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)
        self.assertEqual(self._get_table_count("transactions"), transaction_count_before)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), balance_before)

        field_reject = field_client.post(
            f"/resource-losses/{loss_id}/reject",
            json={"review_notes": "Trying to self-review"},
        )
        self.assertEqual(field_reject.status_code, 403)

        reject = self.client.post(
            f"/resource-losses/{loss_id}/reject",
            json={"review_notes": "Damage report was duplicated"},
        )
        self.assertEqual(reject.status_code, 200)
        self.assertEqual(reject.get_json()["status"], "REJECTED")

        conn = self.core.get_conn()
        loss = conn.execute(
            "SELECT * FROM resource_losses WHERE loss_id = ?",
            (loss_id,),
        ).fetchone()
        resource_loss_transactions = conn.execute(
            """
            SELECT *
            FROM transactions
            WHERE transaction_type = 'ResourceLoss'
              AND organisation_id = ?
              AND depot_id = ?
              AND resource_id = ?
            """,
            (organisation_id, depot_id, resource_id),
        ).fetchall()
        conn.close()

        self.assertEqual(loss["status"], "REJECTED")
        self.assertIsNone(loss["loss_transaction_id"])
        self.assertEqual(len(resource_loss_transactions), 0)
        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)
        self.assertEqual(self._get_table_count("transactions"), transaction_count_before)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), balance_before)

    def test_resource_loss_confirmation_posts_resource_loss_transaction(self):
        organisation_id = self.create_organisation("Loss Confirm Org")
        depot_id = self._create_depot(organisation_id, "Loss Confirm Depot")
        category_id = self._create_category(organisation_id, "Loss Confirm Cat")
        resource_id = self._create_resource(organisation_id, category_id, "Loss Confirm Pallet")
        user_id = self._create_user(organisation_id, "Loss Reporter", role="USER")
        field_client = self._client_for_user(user_id, "loss confirm field key")
        self._set_opening_balance(organisation_id, depot_id, resource_id, 100)

        ledger_count_before = self._get_table_count("ledger_entries")
        transaction_count_before = self._get_table_count("transactions")

        create = field_client.post("/resource-loss", json={
            "depot_id": depot_id,
            "resource_id": resource_id,
            "quantity": 7,
            "loss_type": "LOST",
            "loss_reason": "Missing after site reconciliation",
        })
        self.assertEqual(create.status_code, 201)
        self.assertEqual(create.get_json()["status"], "PENDING_REVIEW")
        loss_id = create.get_json()["loss_id"]

        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)
        self.assertEqual(self._get_table_count("transactions"), transaction_count_before)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), 100)

        field_confirm = field_client.post(
            f"/resource-losses/{loss_id}/confirm",
            json={"review_notes": "Trying to self-confirm"},
        )
        self.assertEqual(field_confirm.status_code, 403)

        confirm = self.client.post(
            f"/resource-losses/{loss_id}/confirm",
            json={"review_notes": "Confirmed against depot count"},
        )
        self.assertEqual(confirm.status_code, 200)
        confirm_payload = confirm.get_json()
        self.assertEqual(confirm_payload["status"], "CONFIRMED")
        loss_transaction_id = confirm_payload["loss_transaction_id"]

        conn = self.core.get_conn()
        loss = conn.execute(
            "SELECT * FROM resource_losses WHERE loss_id = ?",
            (loss_id,),
        ).fetchone()
        loss_txn = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?",
            (loss_transaction_id,),
        ).fetchone()
        loss_ledger = conn.execute(
            "SELECT * FROM ledger_entries WHERE transaction_id = ?",
            (loss_transaction_id,),
        ).fetchone()
        conn.close()

        self.assertEqual(loss["status"], "CONFIRMED")
        self.assertEqual(loss["loss_transaction_id"], loss_transaction_id)
        self.assertEqual(loss_txn["transaction_type"], "ResourceLoss")
        self.assertEqual(loss_txn["status"], "POSTED")
        self.assertEqual(loss_txn["direction"], "OUT")
        self.assertEqual(loss_txn["quantity"], 7)
        self.assertEqual(loss_ledger["quantity_delta"], -7)
        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before + 1)
        self.assertEqual(self._get_table_count("transactions"), transaction_count_before + 1)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), 93)

    def test_resource_loss_field_user_cannot_report_against_another_org(self):
        org_a = self.create_organisation("Loss Boundary Org A")
        org_b = self.create_organisation("Loss Boundary Org B")
        depot_b = self._create_depot(org_b, "Boundary Depot B")
        category_b = self._create_category(org_b, "Boundary Cat B")
        resource_b = self._create_resource(org_b, category_b, "Boundary Pallet B")
        user_id = self._create_user(org_a, "Boundary Field User", role="USER")
        field_client = self._client_for_user(user_id, "loss boundary field key")

        create = field_client.post("/resource-loss", json={
            "organisation_id": org_b,
            "depot_id": depot_b,
            "resource_id": resource_b,
            "quantity": 3,
            "loss_type": "STOLEN",
            "loss_reason": "Attempted cross-org report",
        })
        self.assertEqual(create.status_code, 404)
        self.assertEqual(create.get_json()["error"], "Depot not found")

        conn = self.core.get_conn()
        cross_org_losses = conn.execute(
            """
            SELECT *
            FROM resource_losses
            WHERE organisation_id = ? AND depot_id = ? AND resource_id = ?
            """,
            (org_b, depot_b, resource_b),
        ).fetchall()
        conn.close()
        self.assertEqual(len(cross_org_losses), 0)

    def test_offline_batch_transaction_idempotency_does_not_duplicate_records_or_ledger(self):
        organisation_id = self.create_organisation("Offline Txn Org")
        depot_id = self._create_depot(organisation_id, "Offline Txn Depot")
        category_id = self._create_category(organisation_id, "Offline Txn Cat")
        resource_id = self._create_resource(organisation_id, category_id, "Offline Txn Pallet")
        self._set_opening_balance(organisation_id, depot_id, resource_id, 100)

        ledger_count_before = self._get_table_count("ledger_entries")
        transaction_count_before = self._get_table_count("transactions")
        balance_before = self._get_resource_balance(organisation_id, depot_id, resource_id)
        body = {
            "device_id": "device-offline-txn-1",
            "items": [{
                "local_id": "local-txn-1",
                "type": "transaction",
                "queued_at": "2026-06-06T01:00:00Z",
                "payload": {
                    "organisation_id": organisation_id,
                    "depot_id": depot_id,
                    "resource_id": resource_id,
                    "quantity": 12,
                    "direction": "OUT",
                    "transaction_type": "Movement",
                    "submitted_by_display_name": "Offline User",
                },
            }],
        }

        first = self.client.post("/offline-batch", json=body)
        self.assertEqual(first.status_code, 200)
        first_payload = first.get_json()
        self.assertEqual(first_payload["succeeded"], 1)
        self.assertEqual(first_payload["failed"], 0)
        self.assertEqual(first_payload["duplicates"], 0)
        result = first_payload["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["detail"]["status"], "DRAFT")
        transaction_id = result["server_id"]

        self.assertEqual(self._get_table_count("transactions"), transaction_count_before + 1)
        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), balance_before)
        self.assertEqual(self._get_transaction_ledger_count(transaction_id), 0)

        repeat = self.client.post("/offline-batch", json=body)
        self.assertEqual(repeat.status_code, 200)
        repeat_payload = repeat.get_json()
        self.assertEqual(repeat_payload["succeeded"], 0)
        self.assertEqual(repeat_payload["failed"], 0)
        self.assertEqual(repeat_payload["duplicates"], 1)
        duplicate = repeat_payload["results"][0]
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["server_id"], transaction_id)
        self.assertEqual(duplicate["original_status"], "success")
        self.assertEqual(self._get_table_count("transactions"), transaction_count_before + 1)
        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), balance_before)

    def test_offline_batch_resource_loss_idempotency_creates_pending_report_once(self):
        organisation_id = self.create_organisation("Offline Loss Org")
        depot_id = self._create_depot(organisation_id, "Offline Loss Depot")
        category_id = self._create_category(organisation_id, "Offline Loss Cat")
        resource_id = self._create_resource(organisation_id, category_id, "Offline Loss Pallet")
        self._set_opening_balance(organisation_id, depot_id, resource_id, 100)

        ledger_count_before = self._get_table_count("ledger_entries")
        loss_count_before = self._get_table_count("resource_losses")
        balance_before = self._get_resource_balance(organisation_id, depot_id, resource_id)
        body = {
            "device_id": "device-offline-loss-1",
            "items": [{
                "local_id": "local-loss-1",
                "type": "resource_loss",
                "queued_at": "2026-06-06T02:00:00Z",
                "payload": {
                    "organisation_id": organisation_id,
                    "depot_id": depot_id,
                    "resource_id": resource_id,
                    "quantity": 3,
                    "loss_type": "DAMAGED",
                    "loss_reason": "Found damaged while offline",
                    "submitted_by_display_name": "Offline User",
                },
            }],
        }

        first = self.client.post("/offline-batch", json=body)
        self.assertEqual(first.status_code, 200)
        first_payload = first.get_json()
        self.assertEqual(first_payload["succeeded"], 1)
        result = first_payload["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["detail"]["status"], "PENDING_REVIEW")
        loss_id = result["server_id"]

        conn = self.core.get_conn()
        loss = conn.execute(
            "SELECT * FROM resource_losses WHERE loss_id = ?",
            (loss_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(loss["status"], "PENDING_REVIEW")
        self.assertIsNone(loss["loss_transaction_id"])
        self.assertEqual(self._get_table_count("resource_losses"), loss_count_before + 1)
        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), balance_before)

        repeat = self.client.post("/offline-batch", json=body)
        self.assertEqual(repeat.status_code, 200)
        repeat_payload = repeat.get_json()
        self.assertEqual(repeat_payload["succeeded"], 0)
        self.assertEqual(repeat_payload["failed"], 0)
        self.assertEqual(repeat_payload["duplicates"], 1)
        duplicate = repeat_payload["results"][0]
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["server_id"], loss_id)
        self.assertEqual(duplicate["original_status"], "success")
        self.assertEqual(self._get_table_count("resource_losses"), loss_count_before + 1)
        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), balance_before)

    def test_offline_batch_invalid_payload_reports_failure_without_mutating_stock(self):
        organisation_id = self.create_organisation("Offline Invalid Org")
        depot_id = self._create_depot(organisation_id, "Offline Invalid Depot")
        category_id = self._create_category(organisation_id, "Offline Invalid Cat")
        resource_id = self._create_resource(organisation_id, category_id, "Offline Invalid Pallet")
        self._set_opening_balance(organisation_id, depot_id, resource_id, 100)

        ledger_count_before = self._get_table_count("ledger_entries")
        transaction_count_before = self._get_table_count("transactions")
        loss_count_before = self._get_table_count("resource_losses")
        balance_before = self._get_resource_balance(organisation_id, depot_id, resource_id)

        response = self.client.post("/offline-batch", json={
            "device_id": "device-offline-invalid-1",
            "items": [{
                "local_id": "local-invalid-1",
                "type": "transaction",
                "queued_at": "2026-06-06T03:00:00Z",
                "payload": {
                    "organisation_id": organisation_id,
                    "depot_id": depot_id,
                    "resource_id": resource_id,
                    "quantity": 4,
                    "direction": "SIDEWAYS",
                    "transaction_type": "Movement",
                },
            }],
        })
        self.assertEqual(response.status_code, 207)
        payload = response.get_json()
        self.assertEqual(payload["succeeded"], 0)
        self.assertEqual(payload["failed"], 1)
        result = payload["results"][0]
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["type"], "transaction")
        self.assertIn("direction must be IN or OUT", result["error"])
        self.assertEqual(self._get_table_count("transactions"), transaction_count_before)
        self.assertEqual(self._get_table_count("resource_losses"), loss_count_before)
        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), balance_before)

    def test_offline_batch_mixed_results_are_reported_per_item(self):
        organisation_id = self.create_organisation("Offline Mixed Org")
        depot_id = self._create_depot(organisation_id, "Offline Mixed Depot")
        category_id = self._create_category(organisation_id, "Offline Mixed Cat")
        resource_id = self._create_resource(organisation_id, category_id, "Offline Mixed Pallet")
        self._set_opening_balance(organisation_id, depot_id, resource_id, 100)

        ledger_count_before = self._get_table_count("ledger_entries")
        transaction_count_before = self._get_table_count("transactions")
        loss_count_before = self._get_table_count("resource_losses")
        balance_before = self._get_resource_balance(organisation_id, depot_id, resource_id)

        response = self.client.post("/offline-batch", json={
            "device_id": "device-offline-mixed-1",
            "items": [
                {
                    "local_id": "local-mixed-txn-1",
                    "type": "transaction",
                    "queued_at": "2026-06-06T04:00:00Z",
                    "payload": {
                        "organisation_id": organisation_id,
                        "depot_id": depot_id,
                        "resource_id": resource_id,
                        "quantity": 9,
                        "direction": "IN",
                        "transaction_type": "Movement",
                    },
                },
                {
                    "local_id": "local-mixed-loss-bad-1",
                    "type": "resource_loss",
                    "queued_at": "2026-06-06T04:05:00Z",
                    "payload": {
                        "organisation_id": organisation_id,
                        "depot_id": depot_id,
                        "resource_id": resource_id,
                        "quantity": 2,
                        "loss_type": "UNKNOWN",
                        "loss_reason": "Invalid loss type",
                    },
                },
            ],
        })
        self.assertEqual(response.status_code, 207)
        payload = response.get_json()
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["succeeded"], 1)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["duplicates"], 0)
        results_by_local_id = {item["local_id"]: item for item in payload["results"]}
        self.assertEqual(results_by_local_id["local-mixed-txn-1"]["status"], "success")
        self.assertEqual(results_by_local_id["local-mixed-loss-bad-1"]["status"], "error")
        self.assertIn("Invalid loss_type", results_by_local_id["local-mixed-loss-bad-1"]["error"])
        self.assertEqual(self._get_table_count("transactions"), transaction_count_before + 1)
        self.assertEqual(self._get_table_count("resource_losses"), loss_count_before)
        self.assertEqual(self._get_table_count("ledger_entries"), ledger_count_before)
        self.assertEqual(self._get_resource_balance(organisation_id, depot_id, resource_id), balance_before)

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

    def test_user_cap_self_serve_and_enforcement(self):
        org_id = self.create_organisation("CapTestOrg")

        # 26+ must be rejected for self-serve
        r = self.client.post(
            f"/organisations/{org_id}/subscription/select-users",
            json={"selected_user_count": 26, "changed_by_display_name": "Admin"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("self_serve_limit", r.get_json())

        # Set cap to 2 via self-serve
        r = self.client.post(
            f"/organisations/{org_id}/subscription/select-users",
            json={"selected_user_count": 2, "changed_by_display_name": "Admin"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["selected_user_count"], 2)

        # Create user 1 — should succeed
        r = self.client.post(
            "/global-admin/users",
            json={
                "organisation_id": org_id,
                "display_name": "Cap User 1",
                "role": "USER",
                "confirmation_text": "CREATE USER",
            },
        )
        self.assertEqual(r.status_code, 201)

        # Create user 2 — should succeed (hits cap exactly)
        r = self.client.post(
            "/global-admin/users",
            json={
                "organisation_id": org_id,
                "display_name": "Cap User 2",
                "role": "USER",
                "confirmation_text": "CREATE USER",
            },
        )
        self.assertEqual(r.status_code, 201)

        # Create user 3 — should be blocked
        r = self.client.post(
            "/global-admin/users",
            json={
                "organisation_id": org_id,
                "display_name": "Cap User 3",
                "role": "USER",
                "confirmation_text": "CREATE USER",
            },
        )
        self.assertEqual(r.status_code, 403)
        payload = r.get_json()
        self.assertEqual(payload["error"], "USER_CAP_REACHED")
        self.assertIn("dialog", payload)

    def test_global_admin_can_set_user_count_above_25(self):
        org_id = self.create_organisation("LargeCapOrg")

        r = self.client.post(
            f"/global-admin/organisations/{org_id}/set-user-count",
            json={"selected_user_count": 120, "changed_by_display_name": "Global Admin"},
        )
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["selected_user_count"], 120)
        self.assertTrue(payload["is_custom_plan"])

    def test_cannot_set_user_count_below_active_users(self):
        org_id = self.create_organisation("ActiveCapOrg")

        # Create one user
        self.client.post(
            "/global-admin/users",
            json={
                "organisation_id": org_id,
                "display_name": "Active User",
                "role": "USER",
                "confirmation_text": "CREATE USER",
            },
        )

        # Try to cap below existing active count
        r = self.client.post(
            f"/organisations/{org_id}/subscription/select-users",
            json={"selected_user_count": 0, "changed_by_display_name": "Admin"},
        )
        self.assertEqual(r.status_code, 400)

    def test_unauthenticated_request_is_rejected(self):
        # Make a request with no auth header
        r = self.core.app.test_client().get("/organisations/anything")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["error"], "AUTHENTICATION_REQUIRED")

    def test_invalid_api_key_is_rejected(self):
        client = self.core.app.test_client()
        client.environ_base["HTTP_AUTHORIZATION"] = "Bearer ppk_thisisnotavalidkey00000000000000000000000000000000000000000000000"
        r = client.get("/health")
        # /health is exempt — should pass even with garbage key
        self.assertEqual(r.status_code, 200)
        # but a real route should reject it
        r = client.get("/organisations/anything")
        self.assertEqual(r.status_code, 401)

    def test_org_user_cannot_access_global_admin_routes(self):
        org_id = self.create_organisation("AuthTestOrg")

        # Create a regular user
        r = self.client.post(
            "/global-admin/users",
            json={
                "organisation_id": org_id,
                "display_name": "Regular User",
                "role": "USER",
                "confirmation_text": "CREATE USER",
            },
        )
        self.assertEqual(r.status_code, 201)
        user_id = r.get_json()["user"]["user_id"]

        # Issue an API key for that user
        r = self.client.post("/auth/keys", json={"user_id": user_id, "label": "test"})
        self.assertEqual(r.status_code, 201)
        user_key = r.get_json()["api_key"]

        # Use the user key to hit a global-admin route
        user_client = self.core.app.test_client()
        user_client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {user_key}"
        r = user_client.get("/global-admin/pricing-dashboard")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"], "INSUFFICIENT_ROLE")

    def test_org_user_cannot_access_other_org(self):
        org_a = self.create_organisation("OrgIsoA")
        org_b = self.create_organisation("OrgIsoB")

        r = self.client.post(
            "/global-admin/users",
            json={
                "organisation_id": org_a,
                "display_name": "Org A User",
                "role": "USER",
                "confirmation_text": "CREATE USER",
            },
        )
        self.assertEqual(r.status_code, 201)
        user_id = r.get_json()["user"]["user_id"]

        r = self.client.post("/auth/keys", json={"user_id": user_id, "label": "test"})
        self.assertEqual(r.status_code, 201)
        user_key = r.get_json()["api_key"]

        user_client = self.core.app.test_client()
        user_client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {user_key}"

        # Can access own org
        r = user_client.get(f"/organisations/{org_a}/stock-position")
        self.assertEqual(r.status_code, 200)

        # Cannot access another org
        r = user_client.get(f"/organisations/{org_b}/stock-position")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"], "ORG_ACCESS_DENIED")

    def test_api_key_issue_list_revoke(self):
        org_id = self.create_organisation("KeyLifecycleOrg")
        user_id = self._create_user(org_id, "Key Test User", "USER")

        # Issue a key for a real user via the master key
        r = self.client.post("/auth/keys", json={"user_id": user_id, "label": "smoke test key"})
        self.assertEqual(r.status_code, 201)
        payload = r.get_json()
        self.assertIn("api_key", payload)
        self.assertTrue(payload["api_key"].startswith("ppk_"))
        key_id = payload["api_key_id"]

        # List keys for that user
        r = self.client.get(f"/auth/keys?user_id={user_id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["count"], 1)

        # Revoke it
        r = self.client.post(f"/auth/keys/{key_id}/revoke")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["revoked"])

    def test_bootstrap_blocked_when_users_exist(self):
        # The test DB already has users created in earlier tests
        r = self.client.post(
            "/auth/bootstrap",
            json={"display_name": "Late Bootstrap Attempt"},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"], "BOOTSTRAP_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
