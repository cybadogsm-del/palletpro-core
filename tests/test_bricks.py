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

    def setUp(self):
        if hasattr(self.core, "_rate_store"):
            with self.core._rate_lock:
                self.core._rate_store.clear()

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

    def _create_login_user(self, role="USER", organisation_id=None):
        self.__class__._email_counter += 1
        email = f"login-user-{self.__class__._email_counter}@example.test"
        password = "Password123!"
        access_method = "DESKTOP" if role in ("ORG_ADMIN", "GLOBAL_ADMIN", "SUPER_GLOBAL_ADMIN") else "TABLET"

        user_create = self.client.post("/global-admin/users", json={
            "organisation_id": organisation_id,
            "display_name": f"Login User {self.__class__._email_counter}",
            "email": email,
            "role": role,
            "access_status": "ACTIVE",
            "access_method": access_method,
            "created_by_display_name": "Test",
            "confirmation_text": "CREATE USER",
        })
        self.assertEqual(user_create.status_code, 201)
        payload = user_create.get_json()

        set_pw = self.client.post("/auth/set-password", json={
            "setup_token": payload["setup_token"],
            "password": password,
        })
        self.assertEqual(set_pw.status_code, 200)

        return payload["user"], email, password

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

    def _ensure_org_subscription(self, organisation_id):
        conn = self.core.get_conn()
        conn.execute(
            """
            INSERT OR IGNORE INTO organisation_subscriptions (
                organisation_id,
                subscription_mode,
                subscription_status,
                billing_status,
                do_not_bill,
                created_at,
                updated_at
            ) VALUES (?, 'STANDARD', 'ACTIVE', 'BILLABLE', 0, ?, ?)
            """,
            (organisation_id, self.core.now_iso(), self.core.now_iso()),
        )
        conn.commit()
        conn.close()

    def _set_org_commercial_settings(
        self,
        organisation_id,
        free_period_days=None,
        beta_tester=None,
        discount_percent=None,
        custom_price_cents=None,
    ):
        self._ensure_org_subscription(organisation_id)

        updates = {}
        if free_period_days is not None:
            updates["commercial_free_period_days"] = free_period_days
        if beta_tester is not None:
            updates["commercial_beta_tester"] = 1 if beta_tester else 0
        if discount_percent is not None:
            updates["commercial_discount_percent"] = discount_percent
        if custom_price_cents is not None:
            updates["commercial_custom_price_cents"] = custom_price_cents

        if not updates:
            return

        values = [updates[key] for key in sorted(updates.keys())]
        set_clause = ", ".join([f"{key} = ?" for key in sorted(updates.keys())])
        values.append(organisation_id)

        conn = self.core.get_conn()
        conn.execute(
            f"UPDATE organisation_subscriptions SET {set_clause} WHERE organisation_id = ?",
            values,
        )
        conn.commit()
        conn.close()

    def _patch_org_commercial_settings(
        self,
        client,
        organisation_id,
        payload,
    ):
        return client.patch(f"/global-admin/organisations/{organisation_id}/commercial-settings", json=payload)

    def _run_billing_export(self, organisation_id, endpoint):
        response = self.client.post(endpoint, json={
            "billing_period_start": "2026-01-01",
            "billing_period_end": "2026-01-31",
            "created_by_display_name": "Test",
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        item = next(
            (
                row
                for row in payload["items"]
                if row["organisation_id"] == organisation_id
            ),
            None,
        )
        self.assertIsNotNone(item, f"Organisation {organisation_id} not found in {endpoint}")
        return payload, item

    def _create_billing_org(self, name="Billing Org"):
        org_id = self._create_org(name)

        self._create_user(org_id, "Included Admin", role="ORG_ADMIN")
        self._create_user(org_id, "Billable Admin", role="ORG_ADMIN")

        self._ensure_org_subscription(org_id)
        return org_id

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

    def test_org_admin_can_invite_user_to_own_org(self):
        """ORG_ADMIN can create a new user in their own organisation."""
        org_id = self._create_org("Org Admin Invite Own Org")
        client, _ = self._create_login_client("ORG_ADMIN", org_id)

        r = client.post(f"/organisations/{org_id}/users", json={
            "display_name": "Invite Field User",
            "role": "USER",
            "access_status": "ACTIVE",
            "access_method": "MOBILE",
            "mobile_number": "0400000004",
            "created_by_display_name": "Org Admin",
            "confirmation_text": "CREATE USER",
        })
        self.assertEqual(r.status_code, 201)

        payload = r.get_json()
        self.assertIn("user", payload)
        self.assertIn("user_id", payload["user"])
        self.assertEqual(payload["user"]["organisation_id"], org_id)
        self.assertNotIn("setup_token", payload)

    def test_org_admin_cannot_invite_user_to_other_org(self):
        """ORG_ADMIN cannot create users outside their own organisation."""
        own_org_id = self._create_org("Org Admin Invite Own Org 2")
        other_org_id = self._create_org("Org Admin Invite Other Org")
        client, _ = self._create_login_client("ORG_ADMIN", own_org_id)

        r = client.post(f"/organisations/{other_org_id}/users", json={
            "display_name": "Blocked User",
            "role": "USER",
            "access_status": "ACTIVE",
            "access_method": "MOBILE",
            "mobile_number": "0400000005",
            "created_by_display_name": "Org Admin",
            "confirmation_text": "CREATE USER",
        })
        self.assertEqual(r.status_code, 403)

    def test_user_cannot_invite_user(self):
        """Basic USER cannot create users."""
        org_id = self._create_org("Field User Invite Block Org")
        client, _ = self._create_login_client("USER", org_id)

        r = client.post(f"/organisations/{org_id}/users", json={
            "display_name": "Field Invite Attempt",
            "role": "USER",
            "access_status": "ACTIVE",
            "access_method": "MOBILE",
            "mobile_number": "0400000006",
            "created_by_display_name": "Field User",
            "confirmation_text": "CREATE USER",
        })
        self.assertEqual(r.status_code, 403)

    def test_org_operational_insights_empty_org_is_clear(self):
        """GET /organisations/<id>/operational-insights returns clear structured insight."""
        org_id = self._create_org("Clear Insight Org")

        r = self.client.get(f"/organisations/{org_id}/operational-insights")

        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["insight_type"], "ORG_OPERATIONAL_INSIGHTS")
        self.assertEqual(payload["organisation_id"], org_id)
        self.assertEqual(payload["summary"]["status"], "CLEAR")
        self.assertEqual(payload["summary"]["total_attention_items"], 0)
        self.assertEqual(payload["attention_items"], [])
        self.assertGreaterEqual(len(payload["items"]), 1)
        first_item = payload["items"][0]
        self.assertIn("allowed_user_actions", first_item)
        self.assertIn("audit_context", first_item)
        self.assertIn("recommended_action", first_item)

    def test_field_user_cannot_view_org_operational_insights(self):
        """Field users cannot view org-wide operational insights."""
        org_id = self._create_org("Field Insight Block Org")
        client, _ = self._create_login_client("USER", org_id)

        r = client.get(f"/organisations/{org_id}/operational-insights")

        self.assertEqual(r.status_code, 403)

    def test_org_admin_can_view_own_org_operational_insights(self):
        """ORG_ADMIN can view deterministic insights for their own organisation."""
        org_id = self._create_org("Org Admin Own Insight Org")
        client, _ = self._create_login_client("ORG_ADMIN", org_id)

        r = client.get(f"/organisations/{org_id}/operational-insights")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["organisation_id"], org_id)

    def test_org_admin_cannot_view_other_org_operational_insights(self):
        """ORG_ADMIN cannot view operational insight data for another org."""
        own_org_id = self._create_org("Org Admin Own Insight Scope Org")
        other_org_id = self._create_org("Org Admin Other Insight Scope Org")
        client, _ = self._create_login_client("ORG_ADMIN", own_org_id)

        r = client.get(f"/organisations/{other_org_id}/operational-insights")

        self.assertEqual(r.status_code, 403)

    def test_org_operational_insights_reports_attention_items(self):
        """Operational insights expose deterministic backend issues for the frontend."""
        from modules.offline_batch import ensure_offline_batch_tables

        org_id = self._create_org("Attention Insight Org")
        conn = self.core.get_conn()
        ensure_offline_batch_tables(conn)
        ts = self.core.now_iso()

        conn.execute(
            """
            INSERT INTO pending_approval_entries (
                pending_entry_id, organisation_id, entry_type, source_record_id,
                source_module, submitted_by_display_name, status, reason_code,
                reason_text, direct_action_type, direct_action_label,
                can_approve_now, can_reject_now, created_at, updated_at
            ) VALUES (?, ?, 'TRANSACTION', ?, 'transactions', 'Tester',
                      'PENDING_APPROVAL', 'TEST', 'Needs review',
                      'REVIEW', 'Review', 1, 1, ?, ?)
            """,
            (self.core.make_id("pend"), org_id, self.core.make_id("txn"), ts, ts),
        )
        conn.execute(
            """
            INSERT INTO offline_batch_log (
                log_id, device_id, local_id, organisation_id, submitted_by_user_id,
                item_type, status, error_message, processed_at
            ) VALUES (?, 'device-1', 'local-1', ?, 'usr_test',
                      'transaction', 'error', 'depot_id is required', ?)
            """,
            (self.core.make_id("obl"), org_id, ts),
        )
        conn.commit()
        conn.close()

        r = self.client.get(f"/organisations/{org_id}/operational-insights")

        self.assertEqual(r.status_code, 200)
        payload = r.get_json()
        self.assertEqual(payload["summary"]["status"], "ATTENTION_REQUIRED")
        self.assertEqual(payload["summary"]["total_attention_items"], 2)
        by_id = {item["insight_id"]: item for item in payload["items"]}
        self.assertEqual(by_id["pending_approvals"]["count"], 1)
        self.assertEqual(by_id["offline_upload_failures"]["count"], 1)
        self.assertEqual(by_id["offline_upload_failures"]["severity"], "medium")
        self.assertIn("recommended_action", by_id["pending_approvals"])
        self.assertEqual(
            by_id["pending_approvals"]["allowed_user_actions"][0]["can_act_now"],
            True,
        )
        self.assertEqual(
            by_id["offline_upload_failures"]["audit_context"]["entity_type"],
            "OfflineBatchLog",
        )

    def test_post_transaction_to_ledger_is_idempotent(self):
        """Ledger posting cannot apply the same transaction to balance twice."""
        org_id = self._create_org("Idempotent Ledger Org")
        depot_id = self._create_depot(org_id, "Main Depot")
        category_id = self._create_category(org_id, "Pallets")
        resource_id = self._create_resource(org_id, category_id, "CHEP")
        transaction_id = self.core.make_id("txn")
        ts = self.core.now_iso()

        conn = self.core.get_conn()
        conn.execute(
            """
            INSERT INTO transactions (
                transaction_id, organisation_id, depot_id, transaction_type,
                resource_id, quantity, direction, status, created_at
            ) VALUES (?, ?, ?, 'MOVEMENT', ?, 5, 'IN', 'DRAFT', ?)
            """,
            (transaction_id, org_id, depot_id, resource_id, ts),
        )
        txn = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()

        first_posted = self.core.post_transaction_to_ledger(conn, txn)
        after_first = conn.execute(
            "SELECT status, posted_at FROM transactions WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        second_posted = self.core.post_transaction_to_ledger(conn, txn)
        after_second = conn.execute(
            "SELECT status, posted_at FROM transactions WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()

        ledger_count = conn.execute(
            "SELECT COUNT(*) AS count FROM ledger_entries WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()["count"]
        balance = conn.execute(
            """
            SELECT current_quantity
            FROM balance_projection
            WHERE organisation_id = ? AND depot_id = ? AND resource_id = ?
            """,
            (org_id, depot_id, resource_id),
        ).fetchone()
        post_audit_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM audit_events
            WHERE entity_type = 'Transaction'
              AND entity_id = ?
              AND action = 'POST'
            """,
            (transaction_id,),
        ).fetchone()["count"]

        conn.rollback()
        conn.close()

        self.assertTrue(first_posted)
        self.assertFalse(second_posted)
        self.assertEqual(after_first["status"], "POSTED")
        self.assertIsNotNone(after_first["posted_at"])
        self.assertEqual(after_second["status"], "POSTED")
        self.assertEqual(after_second["posted_at"], after_first["posted_at"])
        self.assertEqual(ledger_count, 1)
        self.assertEqual(balance["current_quantity"], 5)
        self.assertEqual(post_audit_count, 1)

    def test_post_transaction_to_ledger_rejects_missing_transaction(self):
        """Ledger posting never creates ledger or balance rows for missing transactions."""
        org_id = self._create_org("Missing Ledger Org")
        depot_id = self._create_depot(org_id, "Missing Depot")
        category_id = self._create_category(org_id, "Missing Category")
        resource_id = self._create_resource(org_id, category_id, "Missing Pallet")
        missing_transaction_id = self.core.make_id("txn")
        txn = {
            "transaction_id": missing_transaction_id,
            "organisation_id": org_id,
            "depot_id": depot_id,
            "resource_id": resource_id,
            "quantity": 9,
            "direction": "IN",
        }

        conn = self.core.get_conn()
        ledger_count_before = conn.execute(
            "SELECT COUNT(*) AS count FROM ledger_entries"
        ).fetchone()["count"]
        balance_count_before = conn.execute(
            "SELECT COUNT(*) AS count FROM balance_projection"
        ).fetchone()["count"]

        with self.assertRaisesRegex(ValueError, "Transaction not found"):
            self.core.post_transaction_to_ledger(conn, txn)

        missing_ledger_count = conn.execute(
            "SELECT COUNT(*) AS count FROM ledger_entries WHERE transaction_id = ?",
            (missing_transaction_id,),
        ).fetchone()["count"]
        missing_balance = conn.execute(
            """
            SELECT *
            FROM balance_projection
            WHERE organisation_id = ? AND depot_id = ? AND resource_id = ?
            """,
            (org_id, depot_id, resource_id),
        ).fetchone()
        ledger_count_after = conn.execute(
            "SELECT COUNT(*) AS count FROM ledger_entries"
        ).fetchone()["count"]
        balance_count_after = conn.execute(
            "SELECT COUNT(*) AS count FROM balance_projection"
        ).fetchone()["count"]

        conn.rollback()
        conn.close()

        self.assertEqual(missing_ledger_count, 0)
        self.assertIsNone(missing_balance)
        self.assertEqual(ledger_count_after, ledger_count_before)
        self.assertEqual(balance_count_after, balance_count_before)

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

    def test_sessions_login_options_includes_local_origin(self):
        """OPTIONS /sessions/login returns Access-Control-Allow-Origin for local frontend."""
        origin = "http://127.0.0.1:3001"
        r = self.client.options("/sessions/login", headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, Authorization",
        })
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), origin)

    def test_sessions_login_post_includes_local_origin(self):
        """Successful login responses should not include password hashes or tokens."""
        organisation_id = self._create_org("Login CORS & Safe Payload Org")
        _, email, password = self._create_login_user(organisation_id=organisation_id)
        origin = "http://127.0.0.1:3001"
        r = self.client.post("/sessions/login", headers={"Origin": origin}, environ_overrides={
            "REMOTE_ADDR": "127.0.0.2",
        }, json={
            "email": email,
            "password": password,
            "device_id": "test-device-cors-safe",
            "device_label": "Local Browser",
        })
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), origin)

        payload = r.get_json()
        user = payload["user"]
        forbidden_fields = [
            "password_hash",
            "setup_token",
            "password_reset_token",
            "password_reset_expires_at",
            "reset_token",
            "credential_material",
            "raw_auth_secret",
            "webauthn_credential_id",
            "webauthn_public_key",
            "webauthn_counter",
            "webauthn_device_type",
            "raw_encrypted_secret",
            "stripe_customer_id",
            "stripe_subscription_id",
            "billing_customer_id",
            "internal_control_flag",
            "admin_control_level",
            "env",
        ]
        for key in forbidden_fields:
            self.assertNotIn(key, user)

        for key in [
            "user_id",
            "organisation_id",
            "display_name",
            "email",
            "mobile_number",
            "role",
            "access_status",
            "default_nav_app",
            "default_depot_id",
        ]:
            self.assertIn(key, user)
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

    def test_org_subscription_has_commercial_settings_storage_columns(self):
        """organisation_subscriptions has commercial settings storage columns."""
        conn = self.core.get_conn()
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(organisation_subscriptions)").fetchall()}
        conn.close()

        required_columns = {
            "commercial_free_period_days",
            "commercial_beta_tester",
            "commercial_discount_percent",
            "commercial_custom_price_cents",
        }
        for col in required_columns:
            self.assertIn(col, cols)

    def test_billing_export_preview_respects_free_period(self):
        """Free Period commercial setting changes billing totals."""
        org_id = self._create_billing_org("Billing Org Free Period")

        _, base = self._run_billing_export(org_id, "/global-admin/billing-export-preview")

        self._set_org_commercial_settings(org_id, free_period_days=30)
        _, discounted = self._run_billing_export(org_id, "/global-admin/billing-export-preview")

        self.assertGreater(base["total_cents"], 0)
        self.assertEqual(discounted["total_cents"], 0)

    def test_billing_export_preview_respects_beta_tester(self):
        """Beta Tester commercial setting changes billing totals."""
        org_id = self._create_billing_org("Billing Org Beta Tester")

        _, base = self._run_billing_export(org_id, "/global-admin/billing-export-preview")

        self._set_org_commercial_settings(org_id, beta_tester=True)
        _, discounted = self._run_billing_export(org_id, "/global-admin/billing-export-preview")

        self.assertGreater(base["total_cents"], discounted["total_cents"])
        self.assertGreater(discounted["total_cents"], 0)

    def test_billing_export_preview_respects_discount(self):
        """Discount commercial setting changes billing totals."""
        org_id = self._create_billing_org("Billing Org Discount")

        _, base = self._run_billing_export(org_id, "/global-admin/billing-export-preview")

        self._set_org_commercial_settings(org_id, discount_percent=50)
        _, discounted = self._run_billing_export(org_id, "/global-admin/billing-export-preview")

        self.assertGreater(base["total_cents"], discounted["total_cents"])
        self.assertEqual(discounted["commercial_discount_percent"], 50)

    def test_billing_export_preview_respects_custom_price(self):
        """Custom Price commercial setting changes billing totals."""
        org_id = self._create_billing_org("Billing Org Custom Price")

        _, base = self._run_billing_export(org_id, "/global-admin/billing-export-preview")

        self._set_org_commercial_settings(org_id, custom_price_cents=2500)
        _, updated = self._run_billing_export(org_id, "/global-admin/billing-export-preview")

        self.assertGreater(updated["total_cents"], base["total_cents"])
        self.assertEqual(updated["commercial_custom_price_cents"], 2500)

    def test_sga_can_update_org_commercial_settings(self):
        """PATCH /global-admin/organisations/<id>/commercial-settings updates all commercial fields."""
        org_id = self._create_billing_org("Billing Org Commercial Settings Update")
        client, _ = self._create_login_client("SUPER_GLOBAL_ADMIN")
        response = self._patch_org_commercial_settings(client, org_id, {
            "commercial_free_period_days": 30,
            "commercial_beta_tester": True,
            "commercial_discount_percent": 25,
            "commercial_custom_price_cents": 3000,
            "audit_reason": "Quarterly commercial adjustment",
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["organisation_id"], org_id)
        self.assertEqual(payload["commercial_settings"]["commercial_free_period_days"], 30)
        self.assertEqual(payload["commercial_settings"]["commercial_beta_tester"], 1)
        self.assertEqual(payload["commercial_settings"]["commercial_discount_percent"], 25)
        self.assertEqual(payload["commercial_settings"]["commercial_custom_price_cents"], 3000)

    def test_non_sga_cannot_update_org_commercial_settings(self):
        """PATCH /global-admin/organisations/<id>/commercial-settings is SGA-only."""
        org_id = self._create_billing_org("Billing Org Commercial Settings Forbidden")
        client, _ = self._create_login_client("GLOBAL_ADMIN")
        response = self._patch_org_commercial_settings(client, org_id, {
            "commercial_beta_tester": True,
            "audit_reason": "Attempted global admin change",
        })
        self.assertEqual(response.status_code, 403)

    def test_org_commercial_settings_update_rejects_missing_audit_reason(self):
        """PATCH /global-admin/organisations/<id>/commercial-settings requires audit_reason."""
        org_id = self._create_billing_org("Billing Org Missing Audit Reason")
        client, _ = self._create_login_client("SUPER_GLOBAL_ADMIN")
        response = self._patch_org_commercial_settings(client, org_id, {
            "commercial_beta_tester": False,
        })
        self.assertEqual(response.status_code, 400)
        response = self._patch_org_commercial_settings(client, org_id, {
            "commercial_beta_tester": False,
            "audit_reason": "   ",
        })
        self.assertEqual(response.status_code, 400)

    def test_org_commercial_settings_update_rejects_invalid_values(self):
        """PATCH /global-admin/organisations/<id>/commercial-settings validates fields."""
        org_id = self._create_billing_org("Billing Org Invalid Settings")
        client, _ = self._create_login_client("SUPER_GLOBAL_ADMIN")

        response = self._patch_org_commercial_settings(client, org_id, {
            "commercial_free_period_days": -1,
            "audit_reason": "Invalid free period",
        })
        self.assertEqual(response.status_code, 400)

        response = self._patch_org_commercial_settings(client, org_id, {
            "commercial_beta_tester": "true",
            "audit_reason": "Invalid beta flag",
        })
        self.assertEqual(response.status_code, 400)

        response = self._patch_org_commercial_settings(client, org_id, {
            "commercial_discount_percent": 120,
            "audit_reason": "Invalid discount",
        })
        self.assertEqual(response.status_code, 400)

        response = self._patch_org_commercial_settings(client, org_id, {
            "commercial_discount_percent": 12.5,
            "audit_reason": "Non-integer discount",
        })
        self.assertEqual(response.status_code, 400)

        response = self._patch_org_commercial_settings(client, org_id, {
            "commercial_custom_price_cents": -1,
            "audit_reason": "Invalid custom price",
        })
        self.assertEqual(response.status_code, 400)

    def test_org_commercial_settings_update_reflected_in_sga_preview(self):
        """Updated commercial settings appear in Org Admin subscription preview for SGA."""
        org_id = self._create_billing_org("Billing Org Preview Reflect")
        client, _ = self._create_login_client("SUPER_GLOBAL_ADMIN")

        patch = self._patch_org_commercial_settings(client, org_id, {
            "commercial_free_period_days": 7,
            "commercial_beta_tester": True,
            "commercial_discount_percent": 10,
            "commercial_custom_price_cents": 4000,
            "audit_reason": "Preview validation",
        })
        self.assertEqual(patch.status_code, 200)

        preview = self.client.get(f"/global-admin/organisations/{org_id}/org-admin-subscription-preview")
        self.assertEqual(preview.status_code, 200)
        payload = preview.get_json()
        subscription = payload["org_admin_dashboard"]["subscription"]
        self.assertEqual(subscription["commercial_free_period_days"], 7)
        self.assertEqual(subscription["commercial_beta_tester"], 1)
        self.assertEqual(subscription["commercial_discount_percent"], 10)
        self.assertEqual(subscription["commercial_custom_price_cents"], 4000)

    def test_org_commercial_settings_update_affects_billing_export_preview_totals(self):
        """PATCH changes affect billing export preview totals using shared resolver."""
        org_id = self._create_billing_org("Billing Org Preview Resolver")
        client, _ = self._create_login_client("SUPER_GLOBAL_ADMIN")

        _, base = self._run_billing_export(org_id, "/global-admin/billing-export-preview")
        patch = self._patch_org_commercial_settings(client, org_id, {
            "commercial_free_period_days": 30,
            "audit_reason": "Free period override",
        })
        self.assertEqual(patch.status_code, 200)

        _, updated = self._run_billing_export(org_id, "/global-admin/billing-export-preview")
        self.assertGreater(base["total_cents"], 0)
        self.assertEqual(updated["total_cents"], 0)
        self.assertEqual(updated["commercial_free_period_days"], 30)

    def test_org_commercial_settings_update_affects_billing_export_finalise_totals(self):
        """PATCH changes affect billing export finalise totals using shared resolver."""
        org_id = self._create_billing_org("Billing Org Finalise Resolver")
        client, _ = self._create_login_client("SUPER_GLOBAL_ADMIN")

        _, base = self._run_billing_export(org_id, "/global-admin/billing-export-preview")
        patch = self._patch_org_commercial_settings(client, org_id, {
            "commercial_discount_percent": 33,
            "audit_reason": "Discount rollout",
        })
        self.assertEqual(patch.status_code, 200)

        _, preview_item = self._run_billing_export(org_id, "/global-admin/billing-export-preview")
        _, final_item = self._run_billing_export(org_id, "/global-admin/billing-export-finalise")

        self.assertLess(preview_item["total_cents"], base["total_cents"])
        self.assertEqual(preview_item["subtotal_cents"], final_item["subtotal_cents"])
        self.assertEqual(preview_item["gst_cents"], final_item["gst_cents"])
        self.assertEqual(preview_item["total_cents"], final_item["total_cents"])

    def test_billing_export_preview_and_finalise_are_consistent(self):
        """Preview and finalise produce the same line-item total for the same period."""
        org_id = self._create_billing_org("Billing Org Export Parity")
        self._set_org_commercial_settings(org_id, discount_percent=25)

        _, preview_item = self._run_billing_export(org_id, "/global-admin/billing-export-preview")
        _, finalise_item = self._run_billing_export(org_id, "/global-admin/billing-export-finalise")

        self.assertEqual(preview_item["subtotal_cents"], finalise_item["subtotal_cents"])
        self.assertEqual(preview_item["gst_cents"], finalise_item["gst_cents"])
        self.assertEqual(preview_item["total_cents"], finalise_item["total_cents"])

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
