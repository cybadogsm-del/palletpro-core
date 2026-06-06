"""
Brick-specific smoke tests — one test per newly-extracted module,
confirming each brick's routes are wired up and return sane responses.

Bricks under test:
  - modules/organisations.py
  - modules/users.py
  - modules/sessions.py
  - modules/billing.py
  - modules/access_operations.py
  - modules/org_subscription.py
  - modules/shared_transactions.py
"""

import importlib
import os
import sys
import unittest


_TEST_MASTER_KEY = "test-master-key-bricks-suite-xyz789"


class BrickSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = os.path.abspath("test_pallet_pro_bricks.db")
        os.environ["PALLET_PRO_DB"] = cls.db_path
        os.environ["PALLET_PRO_MASTER_KEY"] = _TEST_MASTER_KEY

        for module_name in list(sys.modules.keys()):
            if module_name.startswith(("pallet_pro_core", "modules.", "db", "audit")):
                sys.modules.pop(module_name, None)

        cls.core = importlib.import_module("pallet_pro_core")
        cls.client = cls.core.app.test_client()
        cls.client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {_TEST_MASTER_KEY}"
        cls._email_counter = 0

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.db_path):
            os.remove(cls.db_path)

    # ─── helpers ─────────────────────────────────────────────────────────────

    def _create_org(self, name):
        r = self.client.post("/organisations", json={"name": name})
        self.assertEqual(r.status_code, 201)
        return r.get_json()["organisation_id"]

    def _create_user(self, organisation_id, display_name, role="USER"):
        r = self.client.post("/global-admin/users", json={
            "organisation_id": organisation_id,
            "display_name": display_name,
            "role": role,
            "access_status": "ACTIVE",
            "created_by_display_name": "Test",
            "confirmation_text": "CREATE USER",
        })
        self.assertEqual(r.status_code, 201)
        return r.get_json()["user"]["user_id"]

    def _create_login_client(self, role, organisation_id=None):
        self.__class__._email_counter += 1
        email = f"{role.lower()}-{self.__class__._email_counter}@example.test"
        password = "Password123!"
        access_method = "DESKTOP" if role in ("ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN") else "TABLET"

        r = self.client.post("/global-admin/users", json={
            "organisation_id": organisation_id,
            "display_name": f"{role} Test User {self.__class__._email_counter}",
            "email": email,
            "role": role,
            "access_status": "ACTIVE",
            "access_method": access_method,
            "created_by_display_name": "Test",
            "confirmation_text": "CREATE USER",
        })
        self.assertEqual(r.status_code, 201)
        payload = r.get_json()

        set_pw = self.client.post("/auth/set-password", json={
            "setup_token": payload["setup_token"],
            "password": password,
        })
        self.assertEqual(set_pw.status_code, 200)

        login = self.client.post("/sessions/login", json={
            "email": email,
            "password": password,
            "device_id": f"test-device-{self.__class__._email_counter}",
            "device_label": "Test Client",
        })
        self.assertIn(login.status_code, (200, 201))

        session_id = login.get_json()["session"]["session_id"]
        client = self.core.app.test_client()
        client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {session_id}"
        return client, payload["user"]["user_id"]

    def _create_depot(self, organisation_id, name):
        r = self.client.post("/depots", json={"organisation_id": organisation_id, "name": name})
        self.assertEqual(r.status_code, 201)
        return r.get_json()["depot_id"]

    def _create_resource(self, organisation_id, category_id, name):
        r = self.client.post("/resources", json={
            "organisation_id": organisation_id,
            "category_id": category_id,
            "name": name,
            "resource_type": "PALLET",
            "unit_type": "UNIT",
        })
        self.assertEqual(r.status_code, 201)
        return r.get_json()["resource_id"]

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

    # ─── organisations brick ──────────────────────────────────────────────────

    def test_organisations_global_admin_list(self):
        """GET /global-admin/organisations returns a list payload."""
        self._create_org("List Test Org")
        r = self.client.get("/global-admin/organisations")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertIn("organisations", payload)
        self.assertIsInstance(payload["organisations"], list)
        self.assertGreater(len(payload["organisations"]), 0)

    def test_unauthenticated_global_organisations_blocked(self):
        """GET /global-admin/organisations requires authentication."""
        client = self.core.app.test_client()
        r = client.get("/global-admin/organisations")
        self.assertEqual(r.status_code, 401)

    def test_global_admin_cannot_list_global_organisations(self):
        """GET /global-admin/organisations is SGA-only."""
        client, _ = self._create_login_client("GLOBAL_ADMIN")
        r = client.get("/global-admin/organisations")
        self.assertEqual(r.status_code, 403)

    def test_super_global_admin_can_list_global_organisations(self):
        """SUPER_GLOBAL_ADMIN can list all organisations."""
        self._create_org("SGA List Org")
        client, _ = self._create_login_client("SUPER_GLOBAL_ADMIN")
        r = client.get("/global-admin/organisations")
        self.assertEqual(r.status_code, 200)
        self.assertIn("organisations", r.get_json())

    def test_org_admin_dashboard(self):
        """GET /organisations/<id>/admin-dashboard returns dashboard shape."""
        org_id = self._create_org("Dashboard Org")
        r = self.client.get(f"/organisations/{org_id}/admin-dashboard")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertIn("organisation_id", payload)
        self.assertEqual(payload["organisation_id"], org_id)

    def test_org_admin_dashboard_not_found(self):
        """GET /organisations/<bad-id>/admin-dashboard returns 404."""
        r = self.client.get("/organisations/org_doesnotexist/admin-dashboard")
        self.assertEqual(r.status_code, 404)

    # ─── users brick ─────────────────────────────────────────────────────────

    def test_list_org_users(self):
        """GET /organisations/<id>/users returns an empty users list for a new org."""
        org_id = self._create_org("User List Org")
        r = self.client.get(f"/organisations/{org_id}/users")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertIn("items", payload)
        self.assertIsInstance(payload["items"], list)

    def test_list_org_users_with_members(self):
        """GET /organisations/<id>/users includes created users."""
        org_id = self._create_org("User Members Org")
        self._create_user(org_id, "Alice")
        self._create_user(org_id, "Bob")
        r = self.client.get(f"/organisations/{org_id}/users")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        names = [u["user"]["display_name"] for u in payload["items"]]
        self.assertIn("Alice", names)
        self.assertIn("Bob", names)

    def test_unauthenticated_org_users_blocked(self):
        """GET /organisations/<id>/users requires authentication."""
        org_id = self._create_org("Unauth Users Org")
        client = self.core.app.test_client()
        r = client.get(f"/organisations/{org_id}/users")
        self.assertEqual(r.status_code, 401)

    def test_field_user_cannot_list_org_users(self):
        """Field users cannot list organisation users."""
        org_id = self._create_org("Field Block Org")
        client, _ = self._create_login_client("USER", org_id)
        r = client.get(f"/organisations/{org_id}/users")
        self.assertEqual(r.status_code, 403)

    def test_org_admin_can_list_own_org_users(self):
        """ORG_ADMIN can list users in their own organisation."""
        org_id = self._create_org("Org Admin Own Users Org")
        client, _ = self._create_login_client("ORG_ADMIN", org_id)
        r = client.get(f"/organisations/{org_id}/users")
        self.assertEqual(r.status_code, 200)
        self.assertIn("items", r.get_json())

    def test_org_admin_cannot_list_other_org_users(self):
        """ORG_ADMIN cannot list users in another organisation."""
        own_org_id = self._create_org("Org Admin Own Org")
        other_org_id = self._create_org("Org Admin Other Org")
        client, _ = self._create_login_client("ORG_ADMIN", own_org_id)
        r = client.get(f"/organisations/{other_org_id}/users")
        self.assertEqual(r.status_code, 403)

    def test_user_access_policy(self):
        """GET /users/<id>/access-policy returns a structured policy."""
        org_id = self._create_org("Policy Org")
        user_id = self._create_user(org_id, "Policy User")
        r = self.client.get(f"/users/{user_id}/access-policy")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertIn("user_id", payload)
        self.assertEqual(payload["user_id"], user_id)

    def test_global_admin_update_user_access(self):
        """POST /global-admin/users/<id>/access can suspend a user."""
        org_id = self._create_org("Access Update Org")
        user_id = self._create_user(org_id, "Suspend Me")
        r = self.client.post(f"/global-admin/users/{user_id}/access", json={
            "access_status": "SUSPENDED",
            "changed_by_display_name": "Global Admin",
            "confirmation_text": "CHANGE USER ACCESS",
        })
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["user"]["access_status"], "SUSPENDED")

    def test_global_admin_cannot_update_user_access(self):
        """GLOBAL_ADMIN cannot use the SGA-only user access update endpoint."""
        org_id = self._create_org("GA Access Block Org")
        user_id = self._create_user(org_id, "GA Cannot Suspend")
        client, _ = self._create_login_client("GLOBAL_ADMIN")
        r = client.post(f"/global-admin/users/{user_id}/access", json={
            "access_status": "SUSPENDED",
            "changed_by_display_name": "Global Admin",
            "confirmation_text": "CHANGE USER ACCESS",
        })
        self.assertEqual(r.status_code, 403)

    def test_super_global_admin_can_update_user_access(self):
        """SUPER_GLOBAL_ADMIN can use the user access update endpoint."""
        org_id = self._create_org("SGA Access Update Org")
        user_id = self._create_user(org_id, "SGA Can Suspend")
        client, _ = self._create_login_client("SUPER_GLOBAL_ADMIN")
        r = client.post(f"/global-admin/users/{user_id}/access", json={
            "access_status": "SUSPENDED",
            "changed_by_display_name": "Super Global Admin",
            "confirmation_text": "CHANGE USER ACCESS",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["user"]["access_status"], "SUSPENDED")

    # ─── sessions brick ───────────────────────────────────────────────────────

    def test_session_expiry_sweep(self):
        """POST /global-admin/session-expiry-sweep runs without error."""
        r = self.client.post("/global-admin/session-expiry-sweep", json={
            "confirmation_text": "SWEEP EXPIRED SESSIONS",
        })
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertIn("expired_session_count", payload)

    def test_temporary_user_expiry_sweep(self):
        """POST /global-admin/temporary-user-expiry-sweep runs without error."""
        r = self.client.post("/global-admin/temporary-user-expiry-sweep", json={
            "confirmation_text": "SWEEP EXPIRED TEMPORARY USERS",
        })
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertIn("expired_temporary_access_count", payload)

    def test_login_integrity_dashboard(self):
        """GET /global-admin/login-integrity-dashboard returns dashboard shape."""
        r = self.client.get("/global-admin/login-integrity-dashboard")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertIn("dashboard_type", payload)
        self.assertEqual(payload["dashboard_type"], "GLOBAL_ADMIN_LOGIN_INTEGRITY_DASHBOARD")

    def test_login_integrity_actions_list(self):
        """GET /global-admin/login-integrity-actions returns an action list."""
        r = self.client.get("/global-admin/login-integrity-actions")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertIn("items", payload)
        self.assertIsInstance(payload["items"], list)

    def test_login_integrity_actions_summary(self):
        """GET /global-admin/login-integrity-actions-summary returns summary shape."""
        r = self.client.get("/global-admin/login-integrity-actions-summary")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertIn("open_action_count", payload)

    def test_user_sessions_list(self):
        """GET /users/<id>/sessions returns a sessions list for a user."""
        org_id = self._create_org("Sessions List Org")
        user_id = self._create_user(org_id, "Session List User")
        r = self.client.get(f"/users/{user_id}/sessions")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertIn("items", payload)
        self.assertIsInstance(payload["items"], list)

    # ─── billing brick ───────────────────────────────────────────────────────

    def test_billing_export_preview_empty(self):
        """POST /global-admin/billing-export-preview returns a preview payload."""
        r = self.client.post("/global-admin/billing-export-preview")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["export_type"], "THIRD_PARTY_BILLER")
        self.assertEqual(payload["export_status"], "PREVIEW")
        self.assertIn("items", payload)

    def test_billing_subscription_mode_update(self):
        """POST /global-admin/organisations/<id>/subscription-mode sets mode."""
        org_id = self._create_org("Billing Mode Org")
        r = self.client.post(
            f"/global-admin/organisations/{org_id}/subscription-mode",
            json={
                "subscription_mode": "FREE",
                "changed_by_display_name": "Global Admin",
                "confirmation_text": "CHANGE SUBSCRIPTION MODE",
            },
        )
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["subscription"]["subscription_mode"], "FREE")

    # ─── access_operations brick ──────────────────────────────────────────────

    def test_access_operations_dashboard(self):
        """GET /global-admin/access-operations-dashboard returns dashboard shape."""
        r = self.client.get("/global-admin/access-operations-dashboard")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["dashboard_type"], "GLOBAL_ADMIN_ACCESS_OPERATIONS_DASHBOARD")
        self.assertIn("active_sessions", payload)
        self.assertIn("user_status_summary", payload)

    def test_access_operations_health_is_green_on_empty_db(self):
        """GET /global-admin/access-operations-health returns GREEN with no problems."""
        r = self.client.get("/global-admin/access-operations-health")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["health_type"], "GLOBAL_ADMIN_ACCESS_OPERATIONS_HEALTH")
        self.assertIn(payload["status"], ("GREEN", "AMBER", "RED"))
        self.assertIn("metrics", payload)

    def test_system_control_panel(self):
        """GET /global-admin/system-control-panel returns panel shape."""
        r = self.client.get("/global-admin/system-control-panel")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["panel_type"], "GLOBAL_ADMIN_SYSTEM_CONTROL_PANEL")
        self.assertIn("status", payload)
        self.assertIn("admin_surfaces", payload)
        self.assertGreater(len(payload["admin_surfaces"]), 0)

    def test_access_operations_snapshot_requires_confirmation(self):
        """POST snapshot without confirmation text returns 400."""
        r = self.client.post("/global-admin/access-operations-metrics-snapshot", json={
            "captured_by_display_name": "Test Admin",
            "confirmation_text": "WRONG TEXT",
        })
        self.assertEqual(r.status_code, 400)
        payload = r.get_json()
        self.assertIn("required_confirmation_text", payload)

    def test_access_operations_snapshot_capture_and_list(self):
        """Full lifecycle: capture snapshot, then list it."""
        # Capture
        r = self.client.post("/global-admin/access-operations-metrics-snapshot", json={
            "captured_by_display_name": "Global Admin",
            "confirmation_text": "CAPTURE ACCESS OPERATIONS SNAPSHOT",
        })
        self.assertEqual(r.status_code, 201)
        payload = r.get_json()
        self.assertIn("snapshot", payload)
        self.assertEqual(payload["snapshot"]["snapshot_status"], "CAPTURED")
        self.assertIn("health_status", payload["metrics"])

        # List
        r = self.client.get("/global-admin/access-operations-metrics-snapshots")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["snapshot_list_type"], "ACCESS_OPERATIONS_METRICS_SNAPSHOTS")
        self.assertGreaterEqual(payload["count"], 1)

    # ─── org_subscription brick ──────────────────────────────────────────────

    def test_org_access_status_active(self):
        """GET /organisations/<id>/access-status returns ACTIVE for a new org."""
        org_id = self._create_org("Access Status Org")
        r = self.client.get(f"/organisations/{org_id}/access-status")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertIn("access_state", payload)
        self.assertEqual(payload["access_state"], "ACTIVE")
        self.assertEqual(payload["organisation_name"], "Access Status Org")

    def test_org_exit_dashboard_active_org_returns_early(self):
        """GET /organisations/<id>/exit-dashboard for active org returns ACTIVE message."""
        org_id = self._create_org("Exit Dashboard Active Org")
        r = self.client.get(f"/organisations/{org_id}/exit-dashboard")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["access_state"], "ACTIVE")
        self.assertIn("not required", payload["message"])

    def test_org_unsubscribe_lifecycle(self):
        """POST unsubscribe marks org as CANCELLED and schedules retention jobs."""
        org_id = self._create_org("Unsubscribe Org")

        r = self.client.post(f"/organisations/{org_id}/unsubscribe", json={
            "unsubscribed_by_display_name": "Org Admin",
            "reason_text": "No longer needed",
        })
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertIn("unsubscribe_event_id", payload)
        self.assertTrue(payload["unsubscribe_event_id"].startswith("unsub_"))
        self.assertEqual(payload["subscription"]["subscription_status"], "CANCELLED")
        self.assertEqual(payload["subscription"]["billing_status"], "DO_NOT_BILL")
        self.assertIsNotNone(payload["subscription"]["operating_data_delete_after"])
        self.assertIsNotNone(payload["subscription"]["historical_data_delete_after"])

        # Access status is now no longer ACTIVE
        r = self.client.get(f"/organisations/{org_id}/access-status")
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.get_json()["access_state"], "ACTIVE")

        # Retention jobs were scheduled — verify via DB
        conn = self.core.get_conn()
        jobs = conn.execute(
            "SELECT * FROM data_retention_jobs WHERE organisation_id = ?",
            (org_id,)
        ).fetchall()
        conn.close()
        self.assertEqual(len(jobs), 2)
        job_types = {j["job_type"] for j in jobs}
        self.assertIn("DELETE_OPERATING_DATA", job_types)
        self.assertIn("DELETE_HISTORICAL_ACCOUNT_DATA", job_types)

    # ─── shared_transactions brick ────────────────────────────────────────────

    def test_shared_transactions_list_empty(self):
        """GET /organisations/<id>/shared-transactions returns empty list for new org."""
        org_id = self._create_org("Shared Txn Org")
        r = self.client.get(f"/organisations/{org_id}/shared-transactions")
        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertIn("items", payload)
        self.assertIsInstance(payload["items"], list)
        self.assertEqual(payload["count"], 0)

    def test_shared_transaction_missing_fields_returns_400(self):
        """POST /shared-transactions with missing required fields returns 400."""
        # Missing origin_partner_id — route should validate and reject
        org_a = self._create_org("SharedTxn Validate A")
        org_b = self._create_org("SharedTxn Validate B")
        r = self.client.post("/shared-transactions", json={
            "origin_org_id": org_a,
            "counterparty_org_id": org_b,
            # deliberately omitting origin_partner_id and origin_resource_id
            "quantity": 10,
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.get_json())

    def test_shared_transaction_same_org_rejected(self):
        """POST /shared-transactions where origin == counterparty returns 400."""
        org_id = self._create_org("SharedTxn Same Org")
        cat = self._create_category(org_id, "Cat")
        resource_id = self._create_resource(org_id, cat, "Pallet")
        r = self.client.post("/shared-transactions", json={
            "origin_org_id": org_id,
            "counterparty_org_id": org_id,
            "origin_partner_id": "partner_fake",
            "origin_resource_id": resource_id,
            "quantity": 5,
        })
        self.assertEqual(r.status_code, 400)
        payload = r.get_json()
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
