import importlib
import os
import sys
import unittest


_TEST_MASTER_KEY = "test-master-key-operational-units-suite"


class OperationalUnitFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = os.path.abspath("test_pallet_pro_operational_units.db")
        os.environ["PALLET_PRO_DB"] = cls.db_path
        os.environ["PALLET_PRO_MASTER_KEY"] = _TEST_MASTER_KEY

        for module_name in list(sys.modules.keys()):
            if module_name.startswith(("pallet_pro_core", "modules.", "db", "audit")):
                sys.modules.pop(module_name, None)

        cls.core = importlib.import_module("pallet_pro_core")
        cls.client = cls.core.app.test_client()
        cls.client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {_TEST_MASTER_KEY}"

    def setUp(self):
        conn = self.core.get_conn()
        existing_tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        for table in (
            "operational_unit_sessions",
            "user_operational_unit_permissions",
            "operational_unit_category_members",
            "operational_unit_categories",
            "operational_units",
            "ledger_entries",
            "balance_projection",
            "transactions",
            "transaction_sequences",
            "pending_approval_entries",
            "audit_events",
            "resources",
            "categories",
            "user_accounts",
            "depots",
            "organisations",
        ):
            if table in existing_tables:
                conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.db_path):
            os.remove(cls.db_path)

    def _create_org(self, name="Operational Org"):
        response = self.client.post("/organisations", json={"name": name})
        self.assertEqual(response.status_code, 201)
        return response.get_json()["organisation_id"]

    def _create_depot(self, organisation_id, name="Main Depot"):
        response = self.client.post("/depots", json={"organisation_id": organisation_id, "name": name})
        self.assertEqual(response.status_code, 201)
        return response.get_json()["depot_id"]

    def _create_user(self, organisation_id, display_name="Field User", role="USER"):
        response = self.client.post("/global-admin/users", json={
            "organisation_id": organisation_id,
            "display_name": display_name,
            "role": role,
            "access_status": "ACTIVE",
            "created_by_display_name": "Test",
            "confirmation_text": "CREATE USER",
        })
        self.assertEqual(response.status_code, 201)
        return response.get_json()["user"]["user_id"]

    def _client_for_user(self, user_id):
        response = self.client.post("/auth/keys", json={"user_id": user_id, "label": "operational-unit-test"})
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        client = self.core.app.test_client()
        client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {response.get_json()['api_key']}"
        return client

    def _create_unit(self, organisation_id, depot_id, unit_kind="UNIT", unit_type="FORKLIFT", unit_number="FL-07", display_name="Forklift 07", created_by_user_id=None):
        response = self.client.post(f"/organisations/{organisation_id}/operational-units", json={
            "depot_id": depot_id,
            "unit_kind": unit_kind,
            "unit_type": unit_type,
            "unit_number": unit_number,
            "display_name": display_name,
            "created_by_user_id": created_by_user_id,
        })
        self.assertEqual(response.status_code, 201)
        return response.get_json()

    def _create_category(self, organisation_id, depot_id, name="Cold Store Forklifts", created_by_user_id=None):
        response = self.client.post(f"/organisations/{organisation_id}/operational-unit-categories", json={
            "name": name,
            "depot_id": depot_id,
            "description": "Operational unit group",
            "created_by_user_id": created_by_user_id,
        })
        self.assertEqual(response.status_code, 201)
        return response.get_json()

    def _create_resource(self, organisation_id, name="CHEP Pallet"):
        conn = self.core.get_conn()
        category_id = self.core.make_id("cat")
        resource_id = self.core.make_id("res")
        ts = self.core.now_iso()
        conn.execute(
            "INSERT INTO categories (category_id, organisation_id, name, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
            (category_id, organisation_id, "Pallets", ts),
        )
        conn.execute(
            """
            INSERT INTO resources (resource_id, organisation_id, category_id, brand_id, name, resource_type, unit_type, is_active, created_at)
            VALUES (?, ?, ?, NULL, ?, 'PALLET', 'UNIT', 1, ?)
            """,
            (resource_id, organisation_id, category_id, name, ts),
        )
        conn.commit()
        conn.close()
        return resource_id

    def _set_opening_balance_ready(self, depot_id):
        conn = self.core.get_conn()
        conn.execute("UPDATE depots SET opening_balance_used = 1 WHERE depot_id = ?", (depot_id,))
        conn.commit()
        conn.close()

    def _create_draft_transaction(self, org_id, depot_id, resource_id, user_id, operational_unit_id, quantity=10, direction="IN"):
        response = self.client.post("/transactions", json={
            "organisation_id": org_id,
            "depot_id": depot_id,
            "transaction_type": "Movement",
            "resource_id": resource_id,
            "quantity": quantity,
            "direction": direction,
            "submitted_by_user_id": user_id,
            "submitted_by_display_name": "Field User",
            "operational_unit_id": operational_unit_id,
        })
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(body["status"], "DRAFT")
        return body["transaction_id"]

    def _post_transaction(self, transaction_id):
        response = self.client.post(f"/transactions/{transaction_id}/post")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["status"], "POSTED")

    def test_operational_unit_tables_created(self):
        conn = self.core.get_conn()
        table_names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        conn.close()

        self.assertIn("operational_units", table_names)
        self.assertIn("operational_unit_categories", table_names)
        self.assertIn("operational_unit_category_members", table_names)
        self.assertIn("user_operational_unit_permissions", table_names)
        self.assertIn("operational_unit_sessions", table_names)

    def test_create_operational_unit_records_location_and_audit(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")

        response = self.client.post(f"/organisations/{org_id}/operational-units", json={
            "depot_id": depot_id,
            "unit_kind": "UNIT",
            "unit_type": "FORKLIFT",
            "unit_number": "FL-07",
            "display_name": "Forklift 07",
            "created_by_user_id": admin_user_id,
        })

        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        unit_id = body["operational_unit_id"]

        conn = self.core.get_conn()
        unit = conn.execute(
            "SELECT * FROM operational_units WHERE operational_unit_id = ?",
            (unit_id,),
        ).fetchone()
        self.assertEqual(unit["organisation_id"], org_id)
        self.assertEqual(unit["depot_id"], depot_id)
        self.assertEqual(unit["unit_kind"], "UNIT")
        self.assertEqual(unit["unit_type"], "FORKLIFT")
        self.assertEqual(unit["unit_number"], "FL-07")
        self.assertEqual(unit["status"], "ACTIVE")

        audit = conn.execute(
            "SELECT * FROM audit_events WHERE entity_type = ? AND entity_id = ? AND action = ?",
            ("OperationalUnit", unit_id, "CREATE"),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(audit)

    def test_duplicate_unit_number_same_org_and_kind_is_rejected(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        payload = {
            "depot_id": depot_id,
            "unit_kind": "FLEET",
            "unit_type": "TRUCK",
            "unit_number": "12",
            "display_name": "Fleet 12",
            "created_by_user_id": admin_user_id,
        }
        first = self.client.post(f"/organisations/{org_id}/operational-units", json=payload)
        self.assertEqual(first.status_code, 201)

        second = self.client.post(f"/organisations/{org_id}/operational-units", json=payload)
        self.assertEqual(second.status_code, 409)
        self.assertIn("already exists", second.get_json()["error"].lower())

    def test_update_operational_unit_renames_and_audits(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        created = self._create_unit(org_id, depot_id, created_by_user_id=admin_user_id)
        unit_id = created["operational_unit_id"]

        response = self.client.patch(f"/organisations/{org_id}/operational-units/{unit_id}", json={
            "display_name": "Forklift 07 - Cold Store",
            "updated_by_user_id": admin_user_id,
        })

        self.assertEqual(response.status_code, 200)
        conn = self.core.get_conn()
        unit = conn.execute("SELECT * FROM operational_units WHERE operational_unit_id = ?", (unit_id,)).fetchone()
        self.assertEqual(unit["display_name"], "Forklift 07 - Cold Store")

        audit = conn.execute(
            "SELECT * FROM audit_events WHERE entity_type = ? AND entity_id = ? AND action = ?",
            ("OperationalUnit", unit_id, "UPDATE"),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(audit)

    def test_category_create_and_add_member(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        unit = self._create_unit(org_id, depot_id, created_by_user_id=admin_user_id)
        category_response = self.client.post(f"/organisations/{org_id}/operational-unit-categories", json={
            "name": "Cold Store Forklifts",
            "depot_id": depot_id,
            "description": "Forklifts used in the cold store",
            "created_by_user_id": admin_user_id,
        })
        self.assertEqual(category_response.status_code, 201)
        category_id = category_response.get_json()["category_id"]

        member_response = self.client.post(
            f"/organisations/{org_id}/operational-unit-categories/{category_id}/members",
            json={"operational_unit_id": unit["operational_unit_id"], "created_by_user_id": admin_user_id},
        )
        self.assertEqual(member_response.status_code, 201)

        conn = self.core.get_conn()
        member = conn.execute(
            "SELECT * FROM operational_unit_category_members WHERE category_id = ? AND operational_unit_id = ?",
            (category_id, unit["operational_unit_id"]),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(member)

    def test_category_permission_makes_units_selectable_for_user(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(org_id, "Field User")
        unit = self._create_unit(org_id, depot_id, created_by_user_id=admin_user_id)
        category = self._create_category(org_id, depot_id, created_by_user_id=admin_user_id)
        self.client.post(f"/organisations/{org_id}/operational-unit-categories/{category['category_id']}/members", json={
            "operational_unit_id": unit["operational_unit_id"],
            "created_by_user_id": admin_user_id,
        })

        permission = self.client.post(f"/organisations/{org_id}/users/{field_user_id}/operational-unit-permissions", json={
            "category_id": category["category_id"],
            "permission_level": "OPERATE",
            "created_by_user_id": admin_user_id,
        })
        self.assertEqual(permission.status_code, 201)

        selectable = self.client.get(
            f"/organisations/{org_id}/users/{field_user_id}/selectable-operational-units?depot_id={depot_id}&unit_kind=UNIT"
        )
        self.assertEqual(selectable.status_code, 200)
        ids = [row["operational_unit_id"] for row in selectable.get_json()["items"]]
        self.assertIn(unit["operational_unit_id"], ids)

    def test_selectable_units_are_filtered_by_location(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id, "Main Depot")
        other_depot_id = self._create_depot(org_id, "Other Depot")
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(org_id, "Field User")
        allowed = self._create_unit(org_id, depot_id, unit_number="FL-07", display_name="Forklift 07", created_by_user_id=admin_user_id)
        blocked = self._create_unit(org_id, other_depot_id, unit_number="FL-09", display_name="Forklift 09", created_by_user_id=admin_user_id)

        permission = self.client.post(f"/organisations/{org_id}/users/{field_user_id}/operational-unit-permissions", json={
            "depot_id": depot_id,
            "permission_level": "OPERATE",
            "created_by_user_id": admin_user_id,
        })
        self.assertEqual(permission.status_code, 201)

        selectable = self.client.get(
            f"/organisations/{org_id}/users/{field_user_id}/selectable-operational-units?depot_id={depot_id}&unit_kind=UNIT"
        )
        ids = [row["operational_unit_id"] for row in selectable.get_json()["items"]]

        self.assertIn(allowed["operational_unit_id"], ids)
        self.assertNotIn(blocked["operational_unit_id"], ids)

    def test_only_one_active_allocator_session_per_unit(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(org_id, "Field User")
        second_field_user_id = self._create_user(org_id, "Second Field User")
        unit = self._create_unit(
            org_id,
            depot_id,
            unit_kind="FLEET",
            unit_type="TRUCK",
            unit_number="12",
            display_name="Fleet 12",
            created_by_user_id=admin_user_id,
        )

        first = self.client.post(f"/organisations/{org_id}/operational-units/{unit['operational_unit_id']}/sessions", json={
            "user_id": field_user_id,
        })
        self.assertEqual(first.status_code, 201)

        second = self.client.post(f"/organisations/{org_id}/operational-units/{unit['operational_unit_id']}/sessions", json={
            "user_id": second_field_user_id,
        })
        self.assertEqual(second.status_code, 409)
        self.assertIn("already active", second.get_json()["error"].lower())

    def test_releasing_active_session_allows_next_allocator(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(org_id, "Field User")
        second_field_user_id = self._create_user(org_id, "Second Field User")
        unit = self._create_unit(
            org_id,
            depot_id,
            unit_kind="FLEET",
            unit_type="TRUCK",
            unit_number="12",
            display_name="Fleet 12",
            created_by_user_id=admin_user_id,
        )

        first = self.client.post(f"/organisations/{org_id}/operational-units/{unit['operational_unit_id']}/sessions", json={
            "user_id": field_user_id,
        }).get_json()

        release = self.client.post(
            f"/organisations/{org_id}/operational-units/{unit['operational_unit_id']}/sessions/{first['unit_session_id']}/release",
            json={"release_reason": "Shift ended", "released_by_user_id": field_user_id},
        )
        self.assertEqual(release.status_code, 200)

        second = self.client.post(f"/organisations/{org_id}/operational-units/{unit['operational_unit_id']}/sessions", json={
            "user_id": second_field_user_id,
        })
        self.assertEqual(second.status_code, 201)

    def test_cross_org_unit_access_is_blocked(self):
        org_id = self._create_org("Org A")
        other_org_id = self._create_org("Org B")
        depot_id = self._create_depot(org_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        unit = self._create_unit(org_id, depot_id, created_by_user_id=admin_user_id)

        response = self.client.patch(f"/organisations/{other_org_id}/operational-units/{unit['operational_unit_id']}", json={
            "display_name": "Cross org rename attempt",
            "updated_by_user_id": admin_user_id,
        })

        self.assertIn(response.status_code, (403, 404))

    def test_posting_transaction_writes_operational_unit_to_ledger(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        self._set_opening_balance_ready(depot_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        resource_id = self._create_resource(org_id)
        unit = self._create_unit(
            org_id,
            depot_id,
            unit_kind="FLEET",
            unit_type="TRUCK",
            unit_number="12",
            display_name="Fleet 12",
            created_by_user_id=admin_user_id,
        )
        transaction_id = self._create_draft_transaction(
            org_id, depot_id, resource_id, admin_user_id, unit["operational_unit_id"], quantity=10, direction="IN"
        )

        self._post_transaction(transaction_id)

        conn = self.core.get_conn()
        ledger = conn.execute(
            "SELECT * FROM ledger_entries WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(ledger)
        self.assertEqual(ledger["operational_unit_id"], unit["operational_unit_id"])
        self.assertEqual(ledger["operational_unit_kind_snapshot"], "FLEET")
        self.assertEqual(ledger["operational_unit_number_snapshot"], "12")
        self.assertEqual(ledger["operational_unit_display_snapshot"], "Fleet 12")

    def test_balance_projection_is_separated_by_operational_unit(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        self._set_opening_balance_ready(depot_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        resource_id = self._create_resource(org_id)
        fleet_12 = self._create_unit(
            org_id,
            depot_id,
            unit_kind="FLEET",
            unit_type="TRUCK",
            unit_number="12",
            display_name="Fleet 12",
            created_by_user_id=admin_user_id,
        )
        fleet_18 = self._create_unit(
            org_id,
            depot_id,
            unit_kind="FLEET",
            unit_type="TRUCK",
            unit_number="18",
            display_name="Fleet 18",
            created_by_user_id=admin_user_id,
        )

        txn_12 = self._create_draft_transaction(
            org_id, depot_id, resource_id, admin_user_id, fleet_12["operational_unit_id"], quantity=10, direction="IN"
        )
        txn_18 = self._create_draft_transaction(
            org_id, depot_id, resource_id, admin_user_id, fleet_18["operational_unit_id"], quantity=7, direction="IN"
        )
        self._post_transaction(txn_12)
        self._post_transaction(txn_18)

        conn = self.core.get_conn()
        balances = conn.execute(
            """
            SELECT operational_unit_id, current_quantity
            FROM balance_projection
            WHERE organisation_id = ? AND depot_id = ? AND resource_id = ?
            ORDER BY operational_unit_id
            """,
            (org_id, depot_id, resource_id),
        ).fetchall()
        conn.close()

        by_unit = {row["operational_unit_id"]: row["current_quantity"] for row in balances}
        self.assertEqual(by_unit[fleet_12["operational_unit_id"]], 10)
        self.assertEqual(by_unit[fleet_18["operational_unit_id"]], 7)
        self.assertEqual(len(by_unit), 2)

    def test_resolving_missing_operational_unit_posts_transaction_with_unit_context(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        self._set_opening_balance_ready(depot_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(org_id, "Field User")
        resource_id = self._create_resource(org_id)
        unit = self._create_unit(
            org_id,
            depot_id,
            unit_kind="FLEET",
            unit_type="TRUCK",
            unit_number="12",
            display_name="Fleet 12",
            created_by_user_id=admin_user_id,
        )

        create_response = self.client.post("/transactions", json={
            "organisation_id": org_id,
            "depot_id": depot_id,
            "transaction_type": "Movement",
            "resource_id": resource_id,
            "quantity": 6,
            "direction": "IN",
            "submitted_by_user_id": field_user_id,
            "submitted_by_display_name": "Field User",
            "operational_unit_missing": True,
            "operational_unit_missing_note": "Fleet not in list at gate",
        })
        self.assertEqual(create_response.status_code, 201, create_response.get_data(as_text=True))
        created = create_response.get_json()
        self.assertEqual(created["status"], "PENDING_APPROVAL")
        self.assertEqual(created["approval_reason_code"], "MISSING_OPERATIONAL_UNIT")

        resolve = self.client.patch(f"/transactions/{created['transaction_id']}/resolve-operational-unit", json={
            "operational_unit_id": unit["operational_unit_id"],
            "review_notes": "Matched driver paperwork to Fleet 12",
        })

        self.assertEqual(resolve.status_code, 200, resolve.get_data(as_text=True))
        body = resolve.get_json()
        self.assertEqual(body["status"], "POSTED")
        self.assertEqual(body["operational_unit_id"], unit["operational_unit_id"])

        conn = self.core.get_conn()
        txn = conn.execute("SELECT * FROM transactions WHERE transaction_id = ?", (created["transaction_id"],)).fetchone()
        ledger = conn.execute("SELECT * FROM ledger_entries WHERE transaction_id = ?", (created["transaction_id"],)).fetchone()
        balance = conn.execute(
            """
            SELECT current_quantity FROM balance_projection
            WHERE organisation_id = ? AND depot_id = ? AND resource_id = ? AND operational_unit_id = ?
            """,
            (org_id, depot_id, resource_id, unit["operational_unit_id"]),
        ).fetchone()
        pending = conn.execute(
            "SELECT * FROM pending_approval_entries WHERE source_record_id = ? AND reason_code = 'MISSING_OPERATIONAL_UNIT'",
            (created["transaction_id"],),
        ).fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_events WHERE entity_type = 'Transaction' AND entity_id = ? AND action = 'OPERATIONAL_UNIT_RESOLVED'",
            (created["transaction_id"],),
        ).fetchone()
        conn.close()

        self.assertEqual(txn["status"], "POSTED")
        self.assertEqual(txn["operational_unit_id"], unit["operational_unit_id"])
        self.assertEqual(txn["operational_unit_missing"], 0)
        self.assertIsNone(txn["approval_reason_code"])
        self.assertEqual(ledger["operational_unit_id"], unit["operational_unit_id"])
        self.assertEqual(ledger["operational_unit_kind_snapshot"], "FLEET")
        self.assertEqual(balance["current_quantity"], 6)
        self.assertEqual(pending["status"], "RESOLVED")
        self.assertIsNotNone(audit)

    def test_missing_operational_unit_resolution_rejects_unit_from_wrong_location(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id, "Main Depot")
        other_depot_id = self._create_depot(org_id, "Other Depot")
        self._set_opening_balance_ready(depot_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        field_user_id = self._create_user(org_id, "Field User")
        resource_id = self._create_resource(org_id)
        wrong_location_unit = self._create_unit(
            org_id,
            other_depot_id,
            unit_kind="FLEET",
            unit_type="TRUCK",
            unit_number="18",
            display_name="Fleet 18",
            created_by_user_id=admin_user_id,
        )
        create_response = self.client.post("/transactions", json={
            "organisation_id": org_id,
            "depot_id": depot_id,
            "transaction_type": "Movement",
            "resource_id": resource_id,
            "quantity": 4,
            "direction": "IN",
            "submitted_by_user_id": field_user_id,
            "submitted_by_display_name": "Field User",
            "operational_unit_missing": True,
            "operational_unit_missing_note": "Truck not listed",
        })
        self.assertEqual(create_response.status_code, 201, create_response.get_data(as_text=True))
        transaction_id = create_response.get_json()["transaction_id"]

        resolve = self.client.patch(f"/transactions/{transaction_id}/resolve-operational-unit", json={
            "operational_unit_id": wrong_location_unit["operational_unit_id"],
            "review_notes": "Wrong depot attempt",
        })

        self.assertEqual(resolve.status_code, 400)
        self.assertIn("location", resolve.get_json()["error"].lower())

        conn = self.core.get_conn()
        txn = conn.execute("SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,)).fetchone()
        ledger_count = conn.execute("SELECT COUNT(*) AS count FROM ledger_entries WHERE transaction_id = ?", (transaction_id,)).fetchone()["count"]
        conn.close()
        self.assertEqual(txn["status"], "PENDING_APPROVAL")
        self.assertEqual(txn["approval_reason_code"], "MISSING_OPERATIONAL_UNIT")
        self.assertEqual(ledger_count, 0)

    def test_offline_batch_same_fleet_unit_conflict_routes_affected_transactions_to_pending_approval(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        self._set_opening_balance_ready(depot_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        first_user_id = self._create_user(org_id, "Driver One")
        second_user_id = self._create_user(org_id, "Driver Two")
        resource_id = self._create_resource(org_id)
        unit = self._create_unit(
            org_id,
            depot_id,
            unit_kind="FLEET",
            unit_type="TRUCK",
            unit_number="12",
            display_name="Fleet 12",
            created_by_user_id=admin_user_id,
        )

        payload_base = {
            "organisation_id": org_id,
            "depot_id": depot_id,
            "transaction_type": "Movement",
            "resource_id": resource_id,
            "quantity": 5,
            "direction": "IN",
            "operational_unit_id": unit["operational_unit_id"],
        }
        upload = self.client.post("/offline-batch", json={
            "device_id": "device-offline-conflict",
            "items": [
                {
                    "local_id": "offline-1",
                    "type": "transaction",
                    "queued_at": "2026-06-30T08:00:00",
                    "payload": {
                        **payload_base,
                        "submitted_by_user_id": first_user_id,
                        "submitted_by_display_name": "Driver One",
                    },
                },
                {
                    "local_id": "offline-2",
                    "type": "transaction",
                    "queued_at": "2026-06-30T08:03:00",
                    "payload": {
                        **payload_base,
                        "submitted_by_user_id": second_user_id,
                        "submitted_by_display_name": "Driver Two",
                    },
                },
            ],
        })

        self.assertEqual(upload.status_code, 200, upload.get_data(as_text=True))
        body = upload.get_json()
        self.assertEqual(body["succeeded"], 2)
        transaction_ids = [result["server_id"] for result in body["results"]]

        conn = self.core.get_conn()
        transactions = conn.execute(
            f"SELECT * FROM transactions WHERE transaction_id IN ({','.join('?' for _ in transaction_ids)}) ORDER BY created_at",
            transaction_ids,
        ).fetchall()
        pending = conn.execute(
            """
            SELECT * FROM pending_approval_entries
            WHERE reason_code = 'OFFLINE_OPERATIONAL_UNIT_CONFLICT'
              AND source_record_id IN (?, ?)
            """,
            transaction_ids,
        ).fetchall()
        ledger_count = conn.execute(
            f"SELECT COUNT(*) AS count FROM ledger_entries WHERE transaction_id IN ({','.join('?' for _ in transaction_ids)})",
            transaction_ids,
        ).fetchone()["count"]
        conflict_audit_ids = {
            row["entity_id"] for row in conn.execute(
                "SELECT entity_id FROM audit_events WHERE action = 'OFFLINE_CONFLICT_ROUTED' AND entity_id IN (?, ?)",
                transaction_ids,
            ).fetchall()
        }
        conn.close()

        self.assertEqual(len(transactions), 2)
        self.assertTrue(all(txn["status"] == "PENDING_APPROVAL" for txn in transactions))
        self.assertTrue(all(txn["approval_reason_code"] == "OFFLINE_OPERATIONAL_UNIT_CONFLICT" for txn in transactions))
        self.assertTrue(all(txn["operational_unit_id"] == unit["operational_unit_id"] for txn in transactions))
        self.assertEqual(len(pending), 2)
        self.assertEqual(ledger_count, 0)
        self.assertEqual(conflict_audit_ids, set(transaction_ids))

        resolved = self.client.patch(
            f"/transactions/{transaction_ids[0]}/resolve-operational-unit",
            json={"operational_unit_id": unit["operational_unit_id"], "review_notes": "Verified offline docket"},
        )
        self.assertEqual(resolved.status_code, 200, resolved.get_data(as_text=True))
        self.assertEqual(resolved.get_json()["status"], "POSTED")
        resolved_pending_id = next(
            row["pending_entry_id"] for row in pending if row["source_record_id"] == transaction_ids[0]
        )
        reject_resolved = self.client.post(
            f"/pending-approval/{resolved_pending_id}/reject",
            json={"rejection_reason_text": "Must not rewrite posted truth"},
        )
        self.assertEqual(reject_resolved.status_code, 409)
        conn = self.core.get_conn()
        posted_status = conn.execute(
            "SELECT status FROM transactions WHERE transaction_id = ?", (transaction_ids[0],)
        ).fetchone()["status"]
        conn.close()
        self.assertEqual(posted_status, "POSTED")

        conn = self.core.get_conn()
        conn.execute(
            "UPDATE pending_approval_entries SET status = 'AWAITING_FIX' WHERE pending_entry_id = ?",
            (resolved_pending_id,),
        )
        conn.commit()
        conn.close()
        reject_reopened = self.client.post(
            f"/pending-approval/{resolved_pending_id}/reject",
            json={"rejection_reason_text": "Corrupt reopened approval must not rewrite posted truth"},
        )
        self.assertEqual(reject_reopened.status_code, 409)
        conn = self.core.get_conn()
        immutable_status = conn.execute(
            "SELECT status FROM transactions WHERE transaction_id = ?", (transaction_ids[0],)
        ).fetchone()["status"]
        immutable_ledger_count = conn.execute(
            "SELECT COUNT(*) AS count FROM ledger_entries WHERE transaction_id = ?", (transaction_ids[0],)
        ).fetchone()["count"]
        conn.close()
        self.assertEqual(immutable_status, "POSTED")
        self.assertEqual(immutable_ledger_count, 1)

    def test_offline_batch_same_user_same_fleet_unit_does_not_create_conflict(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        self._set_opening_balance_ready(depot_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        driver_id = self._create_user(org_id, "Driver One")
        resource_id = self._create_resource(org_id)
        unit = self._create_unit(
            org_id,
            depot_id,
            unit_kind="FLEET",
            unit_type="TRUCK",
            unit_number="12",
            display_name="Fleet 12",
            created_by_user_id=admin_user_id,
        )
        payload_base = {
            "organisation_id": org_id,
            "depot_id": depot_id,
            "transaction_type": "Movement",
            "resource_id": resource_id,
            "quantity": 5,
            "direction": "IN",
            "operational_unit_id": unit["operational_unit_id"],
            "submitted_by_user_id": driver_id,
            "submitted_by_display_name": "Driver One",
        }

        upload = self.client.post("/offline-batch", json={
            "device_id": "device-offline-same-user",
            "items": [
                {"local_id": "same-1", "type": "transaction", "queued_at": "2026-06-30T08:00:00", "payload": payload_base},
                {"local_id": "same-2", "type": "transaction", "queued_at": "2026-06-30T08:03:00", "payload": {**payload_base, "quantity": 2}},
            ],
        })

        self.assertEqual(upload.status_code, 200, upload.get_data(as_text=True))
        transaction_ids = [result["server_id"] for result in upload.get_json()["results"]]
        conn = self.core.get_conn()
        rows = conn.execute(
            f"SELECT status, approval_reason_code, operational_unit_id FROM transactions WHERE transaction_id IN ({','.join('?' for _ in transaction_ids)})",
            transaction_ids,
        ).fetchall()
        pending_count = conn.execute(
            "SELECT COUNT(*) AS count FROM pending_approval_entries WHERE reason_code = 'OFFLINE_OPERATIONAL_UNIT_CONFLICT'",
        ).fetchone()["count"]
        conn.close()

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "DRAFT" for row in rows))
        self.assertTrue(all(row["approval_reason_code"] is None for row in rows))
        self.assertTrue(all(row["operational_unit_id"] == unit["operational_unit_id"] for row in rows))
        self.assertEqual(pending_count, 0)

    def test_stock_position_reports_operational_unit_breakdown_without_double_counting(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        self._set_opening_balance_ready(depot_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        driver_id = self._create_user(org_id, "Driver One")
        resource_id = self._create_resource(org_id)
        fleet_12 = self._create_unit(
            org_id,
            depot_id,
            unit_kind="FLEET",
            unit_type="TRUCK",
            unit_number="12",
            display_name="Fleet 12",
            created_by_user_id=admin_user_id,
        )
        fleet_18 = self._create_unit(
            org_id,
            depot_id,
            unit_kind="FLEET",
            unit_type="TRUCK",
            unit_number="18",
            display_name="Fleet 18",
            created_by_user_id=admin_user_id,
        )
        tx1 = self._create_draft_transaction(org_id, depot_id, resource_id, driver_id, fleet_12["operational_unit_id"], quantity=10)
        tx2 = self._create_draft_transaction(org_id, depot_id, resource_id, driver_id, fleet_18["operational_unit_id"], quantity=7)
        self._post_transaction(tx1)
        self._post_transaction(tx2)

        org_response = self.client.get(f"/organisations/{org_id}/stock-position")
        self.assertEqual(org_response.status_code, 200, org_response.get_data(as_text=True))
        org_body = org_response.get_json()
        self.assertEqual(org_body["summary"]["total_quantity"], 17)
        self.assertEqual(org_body["summary"]["total_operational_unit_lines"], 2)
        self.assertEqual(len(org_body["items"]), 1)
        resource_row = org_body["items"][0]
        self.assertEqual(resource_row["total_quantity"], 17)
        self.assertEqual(resource_row["depots"][0]["quantity"], 17)
        unit_rows = resource_row["depots"][0]["operational_units"]
        self.assertEqual(
            {row["operational_unit_display_snapshot"]: row["quantity"] for row in unit_rows},
            {"Fleet 12": 10, "Fleet 18": 7},
        )

        depot_response = self.client.get(f"/organisations/{org_id}/depots/{depot_id}/stock-position")
        self.assertEqual(depot_response.status_code, 200, depot_response.get_data(as_text=True))
        depot_body = depot_response.get_json()
        self.assertEqual(depot_body["summary"]["total_quantity"], 17)
        self.assertEqual(depot_body["summary"]["total_operational_unit_lines"], 2)
        self.assertEqual(depot_body["items"][0]["current_quantity"], 17)
        self.assertEqual(
            {row["operational_unit_display_snapshot"]: row["quantity"] for row in depot_body["items"][0]["operational_units"]},
            {"Fleet 12": 10, "Fleet 18": 7},
        )

    def test_resource_ledger_reports_operational_unit_and_user_actor_history(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        self._set_opening_balance_ready(depot_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        driver_id = self._create_user(org_id, "Driver One")
        resource_id = self._create_resource(org_id)
        fleet_12 = self._create_unit(
            org_id,
            depot_id,
            unit_kind="FLEET",
            unit_type="TRUCK",
            unit_number="12",
            display_name="Fleet 12",
            created_by_user_id=admin_user_id,
        )
        tx1 = self._create_draft_transaction(org_id, depot_id, resource_id, driver_id, fleet_12["operational_unit_id"], quantity=10)
        self._post_transaction(tx1)

        response = self.client.get(f"/organisations/{org_id}/depots/{depot_id}/resources/{resource_id}/ledger")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(body["current_balance"], 10)
        self.assertEqual(body["entry_count"], 1)
        entry = body["entries"][0]
        self.assertEqual(entry["operational_unit_id"], fleet_12["operational_unit_id"])
        self.assertEqual(entry["operational_unit_kind_snapshot"], "FLEET")
        self.assertEqual(entry["operational_unit_number_snapshot"], "12")
        self.assertEqual(entry["operational_unit_display_snapshot"], "Fleet 12")
        self.assertEqual(entry["submitted_by_user_id"], driver_id)
        self.assertEqual(entry["submitted_by_display_name"], "Driver One")

    def test_operational_unit_detail_reports_control_room_context(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        self._set_opening_balance_ready(depot_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        driver_id = self._create_user(org_id, "Driver One")
        resource_id = self._create_resource(org_id)
        unit = self._create_unit(
            org_id,
            depot_id,
            unit_kind="FLEET",
            unit_type="TRUCK",
            unit_number="12",
            display_name="Fleet 12",
            created_by_user_id=admin_user_id,
        )
        category = self._create_category(org_id, depot_id, name="Linehaul")
        add_member = self.client.post(
            f"/organisations/{org_id}/operational-unit-categories/{category['category_id']}/members",
            json={"operational_unit_id": unit["operational_unit_id"], "created_by_user_id": admin_user_id},
        )
        self.assertEqual(add_member.status_code, 201, add_member.get_data(as_text=True))
        grant = self.client.post(
            f"/organisations/{org_id}/users/{driver_id}/operational-unit-permissions",
            json={"operational_unit_id": unit["operational_unit_id"], "permission_level": "OPERATE", "created_by_user_id": admin_user_id},
        )
        self.assertEqual(grant.status_code, 201, grant.get_data(as_text=True))
        session = self.client.post(
            f"/organisations/{org_id}/operational-units/{unit['operational_unit_id']}/sessions",
            json={"user_id": driver_id},
        )
        self.assertEqual(session.status_code, 201, session.get_data(as_text=True))
        tx1 = self._create_draft_transaction(org_id, depot_id, resource_id, driver_id, unit["operational_unit_id"], quantity=12)
        self._post_transaction(tx1)

        conn = self.core.get_conn()
        now = self.core.now_iso()
        pending_id = self.core.make_id("pend")
        conn.execute(
            """
            INSERT INTO pending_approval_entries (
                pending_entry_id, organisation_id, entry_type, source_record_id, source_module,
                submitted_by_display_name, related_entity_type, related_entity_id, related_entity_name,
                resource_id, resource_name, status, reason_code, reason_text,
                direct_action_type, direct_action_target_id, direct_action_label,
                can_approve_now, can_reject_now, created_at, updated_at
            ) VALUES (?, ?, 'TRANSACTION', ?, 'offline_batch', 'Driver Two', 'OperationalUnit', ?, 'Fleet 12',
                      ?, 'CHEP Pallet', 'PENDING_APPROVAL', 'OFFLINE_OPERATIONAL_UNIT_CONFLICT',
                      'Offline conflict for Fleet 12', 'REVIEW_TRANSACTION', ?, 'Review conflict', 0, 1, ?, ?)
            """,
            (pending_id, org_id, tx1, unit["operational_unit_id"], resource_id, tx1, now, now),
        )
        conn.commit()
        conn.close()

        response = self.client.get(f"/organisations/{org_id}/operational-units/{unit['operational_unit_id']}/detail")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertEqual(body["report_type"], "OPERATIONAL_UNIT_DETAIL")
        self.assertEqual(body["unit"]["operational_unit_id"], unit["operational_unit_id"])
        self.assertEqual(body["location"]["depot_id"], depot_id)
        self.assertEqual(body["summary"]["current_stock_quantity"], 12)
        self.assertEqual(body["summary"]["open_pending_count"], 1)
        self.assertEqual(body["summary"]["active_session_count"], 1)
        self.assertEqual(body["stock"][0]["resource_id"], resource_id)
        self.assertEqual(body["stock"][0]["current_quantity"], 12)
        self.assertEqual(body["recent_ledger_entries"][0]["operational_unit_display_snapshot"], "Fleet 12")
        self.assertEqual(body["recent_ledger_entries"][0]["submitted_by_user_id"], driver_id)
        self.assertEqual(body["active_sessions"][0]["user_id"], driver_id)
        self.assertEqual(body["permissions"][0]["user_id"], driver_id)
        self.assertEqual(body["categories"][0]["category_id"], category["category_id"])
        self.assertEqual(body["pending_approval_entries"][0]["reason_code"], "OFFLINE_OPERATIONAL_UNIT_CONFLICT")

    def test_list_routes_return_items_envelope(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        admin_user_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        unit = self._create_unit(org_id, depot_id, created_by_user_id=admin_user_id)
        self._create_category(org_id, depot_id, created_by_user_id=admin_user_id)
        self.client.post(f"/organisations/{org_id}/operational-units/{unit['operational_unit_id']}/sessions", json={
            "user_id": admin_user_id,
        })

        unit_list = self.client.get(f"/organisations/{org_id}/operational-units")
        category_list = self.client.get(f"/organisations/{org_id}/operational-unit-categories")
        session_list = self.client.get(f"/organisations/{org_id}/operational-unit-sessions/active")

        self.assertEqual(unit_list.status_code, 200)
        self.assertEqual(category_list.status_code, 200)
        self.assertEqual(session_list.status_code, 200)
        self.assertIn("items", unit_list.get_json())
        self.assertIn("items", category_list.get_json())
        self.assertIn("items", session_list.get_json())

    def test_only_org_admin_can_manage_units_and_authenticated_actor_is_recorded(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        admin_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        user_id = self._create_user(org_id, "Worker")
        denied = self._client_for_user(user_id).post(
            f"/organisations/{org_id}/operational-units",
            json={"depot_id": depot_id, "unit_kind": "UNIT", "unit_number": "DENIED-1", "display_name": "Denied Unit"},
        )
        self.assertEqual(denied.status_code, 403)
        created = self._client_for_user(admin_id).post(
            f"/organisations/{org_id}/operational-units",
            json={"depot_id": depot_id, "unit_kind": "UNIT", "unit_number": "ADMIN-1", "display_name": "Admin Unit", "created_by_user_id": user_id},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        self.assertEqual(created.get_json()["created_by_user_id"], admin_id)

    def test_field_user_must_have_operate_permission_and_cannot_spoof_submitter(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        self._set_opening_balance_ready(depot_id)
        admin_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        worker_id = self._create_user(org_id, "Worker")
        other_id = self._create_user(org_id, "Other Worker")
        resource_id = self._create_resource(org_id)
        unit = self._create_unit(org_id, depot_id, created_by_user_id=admin_id)
        worker_client = self._client_for_user(worker_id)
        payload = {
            "organisation_id": org_id, "depot_id": depot_id, "transaction_type": "Movement",
            "resource_id": resource_id, "quantity": 3, "direction": "IN",
            "submitted_by_user_id": other_id, "submitted_by_display_name": "Spoofed Worker",
            "operational_unit_id": unit["operational_unit_id"],
        }
        self.assertEqual(worker_client.post("/transactions", json=payload).status_code, 403)
        grant = self.client.post(
            f"/organisations/{org_id}/users/{worker_id}/operational-unit-permissions",
            json={"operational_unit_id": unit["operational_unit_id"], "permission_level": "OPERATE"},
        )
        self.assertEqual(grant.status_code, 201, grant.get_data(as_text=True))
        allowed = worker_client.post("/transactions", json=payload)
        self.assertEqual(allowed.status_code, 201, allowed.get_data(as_text=True))
        self.assertEqual(allowed.get_json()["submitted_by_user_id"], worker_id)
        self.assertEqual(allowed.get_json()["submitted_by_display_name"], "Worker")
        admin_detail = worker_client.get(
            f"/organisations/{org_id}/operational-units/{unit['operational_unit_id']}/detail"
        )
        self.assertEqual(admin_detail.status_code, 403)

    def test_org_admin_cannot_resolve_another_organisations_transaction(self):
        org_a = self._create_org("Org A")
        org_b = self._create_org("Org B")
        admin_a = self._create_user(org_a, "Admin A", role="ORG_ADMIN")
        admin_b = self._create_user(org_b, "Admin B", role="ORG_ADMIN")
        worker_b = self._create_user(org_b, "Worker B")
        depot_b = self._create_depot(org_b)
        self._set_opening_balance_ready(depot_b)
        resource_b = self._create_resource(org_b)
        unit_b = self._create_unit(org_b, depot_b, created_by_user_id=admin_b)
        created = self.client.post("/transactions", json={
            "organisation_id": org_b, "depot_id": depot_b, "transaction_type": "Movement",
            "resource_id": resource_b, "quantity": 2, "direction": "IN",
            "submitted_by_user_id": worker_b, "operational_unit_missing": True,
            "operational_unit_missing_note": "Unit not listed",
        }).get_json()
        admin_a_client = self._client_for_user(admin_a)
        get_response = admin_a_client.get(f"/transactions/{created['transaction_id']}")
        post_response = admin_a_client.post(f"/transactions/{created['transaction_id']}/post")
        depot_detail = admin_a_client.get(f"/depots/{depot_b}")
        resource_detail = admin_a_client.get(f"/resources/{resource_b}")
        response = admin_a_client.patch(
            f"/transactions/{created['transaction_id']}/resolve-operational-unit",
            json={"operational_unit_id": unit_b["operational_unit_id"]},
        )
        conn = self.core.get_conn()
        pending_id = conn.execute(
            "SELECT pending_entry_id FROM pending_approval_entries WHERE source_record_id = ?",
            (created["transaction_id"],),
        ).fetchone()["pending_entry_id"]
        conn.close()
        pending_list = admin_a_client.get(f"/pending-approval?organisation_id={org_b}")
        pending_detail = admin_a_client.get(f"/pending-approval/{pending_id}")
        pending_reject = admin_a_client.post(f"/pending-approval/{pending_id}/reject", json={"rejection_reason_text": "wrong org"})
        pending_approve = admin_a_client.post(f"/pending-approval/{pending_id}/approve")
        user_list = self._client_for_user(worker_b).get(f"/pending-approval?organisation_id={org_b}")
        self.assertEqual(get_response.status_code, 403)
        self.assertEqual(post_response.status_code, 403)
        self.assertEqual(depot_detail.status_code, 403)
        self.assertEqual(resource_detail.status_code, 403)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(pending_list.status_code, 200)
        self.assertEqual(pending_list.get_json()["items"], [])
        self.assertEqual(pending_detail.status_code, 403)
        self.assertEqual(pending_reject.status_code, 403)
        self.assertEqual(pending_approve.status_code, 403)
        self.assertEqual(user_list.status_code, 403)

    def test_missing_partner_requires_org_scoped_admin_resolution(self):
        org_a = self._create_org("Entity Org A")
        org_b = self._create_org("Entity Org B")
        admin_a = self._create_user(org_a, "Entity Admin A", role="ORG_ADMIN")
        admin_b = self._create_user(org_b, "Entity Admin B", role="ORG_ADMIN")
        worker_b = self._create_user(org_b, "Entity Worker B")
        depot_b = self._create_depot(org_b)
        self._set_opening_balance_ready(depot_b)
        resource_b = self._create_resource(org_b)
        unit_b = self._create_unit(org_b, depot_b, created_by_user_id=admin_b)
        invalid_resource = self.client.post("/transactions", json={
            "organisation_id": org_b, "depot_id": depot_b, "transaction_type": "Movement",
            "resource_id": "resource_other_org", "quantity": 3, "direction": "IN",
            "submitted_by_user_id": worker_b, "operational_unit_id": unit_b["operational_unit_id"],
            "unresolved_entity_note": "Partner not listed", "unresolved_entity_type": "PARTNER",
        })
        self.assertEqual(invalid_resource.status_code, 404)
        invalid_partner = self.client.post("/transactions", json={
            "organisation_id": org_b, "depot_id": depot_b, "transaction_type": "Movement",
            "resource_id": "UNRESOLVED", "partner_id": "partner_other_org",
            "quantity": 3, "direction": "IN", "submitted_by_user_id": worker_b,
            "operational_unit_id": unit_b["operational_unit_id"],
            "unresolved_entity_note": "Resource not listed", "unresolved_entity_type": "RESOURCE",
        })
        self.assertEqual(invalid_partner.status_code, 404)
        created_response = self.client.post("/transactions", json={
            "organisation_id": org_b, "depot_id": depot_b, "transaction_type": "Movement",
            "resource_id": resource_b, "quantity": 3, "direction": "IN",
            "submitted_by_user_id": worker_b, "operational_unit_id": unit_b["operational_unit_id"],
            "unresolved_entity_note": "Partner not listed", "unresolved_entity_type": "PARTNER",
        })
        self.assertEqual(created_response.status_code, 201, created_response.get_data(as_text=True))
        transaction_id = created_response.get_json()["transaction_id"]
        cross_org = self._client_for_user(admin_a).patch(
            f"/transactions/{transaction_id}/resolve-entity", json={"partner_id": "partner_unknown"},
        )
        self.assertEqual(cross_org.status_code, 403)
        admin_b_client = self._client_for_user(admin_b)
        missing_partner = admin_b_client.patch(f"/transactions/{transaction_id}/resolve-entity", json={})
        self.assertEqual(missing_partner.status_code, 400)
        partner_response = self.client.post("/partners", json={
            "organisation_id": org_b, "name": "Resolved Partner", "is_customer": True,
        })
        self.assertEqual(partner_response.status_code, 201, partner_response.get_data(as_text=True))
        partner_id = partner_response.get_json()["partner_id"]
        resolved = admin_b_client.patch(
            f"/transactions/{transaction_id}/resolve-entity",
            json={"partner_id": partner_id, "review_notes": "Matched customer paperwork"},
        )
        self.assertEqual(resolved.status_code, 200, resolved.get_data(as_text=True))
        self.assertEqual(resolved.get_json()["status"], "POSTED")

    def test_stock_totals_include_opening_balance_and_operational_unit_rows(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        resource_id = self._create_resource(org_id)
        unit = self._create_unit(org_id, depot_id)
        now = self.core.now_iso()
        conn = self.core.get_conn()
        conn.execute(
            "INSERT INTO balance_projection (balance_projection_id, organisation_id, depot_id, operational_unit_id, resource_id, current_quantity, updated_at) VALUES (?, ?, ?, '__NO_OPERATIONAL_UNIT__', ?, 100, ?)",
            (self.core.make_id("bal"), org_id, depot_id, resource_id, now),
        )
        conn.execute(
            "INSERT INTO balance_projection (balance_projection_id, organisation_id, depot_id, operational_unit_id, resource_id, current_quantity, updated_at) VALUES (?, ?, ?, ?, ?, -10, ?)",
            (self.core.make_id("bal"), org_id, depot_id, unit["operational_unit_id"], resource_id, now),
        )
        conn.commit()
        conn.close()
        org_body = self.client.get(f"/organisations/{org_id}/stock-position").get_json()
        depot_body = self.client.get(f"/organisations/{org_id}/depots/{depot_id}/stock-position").get_json()
        ledger_response = self.client.get(f"/organisations/{org_id}/depots/{depot_id}/resources/{resource_id}/ledger")
        org_ledger_response = self.client.get(f"/organisations/{org_id}/resources/{resource_id}/ledger")
        self.assertEqual(ledger_response.status_code, 200, ledger_response.get_data(as_text=True))
        self.assertEqual(org_ledger_response.status_code, 200, org_ledger_response.get_data(as_text=True))
        ledger_body = ledger_response.get_json()
        org_ledger_body = org_ledger_response.get_json()
        depot_profile = self.client.get(f"/depots/{depot_id}").get_json()
        resource_profile = self.client.get(f"/resources/{resource_id}").get_json()
        legacy_stock = self.client.get(f"/stock?organisation_id={org_id}").get_json()
        stocktake = self.client.post(f"/organisations/{org_id}/stocktake", json={
            "depot_id": depot_id, "initiated_by_display_name": "Stocktake Admin",
        })
        self.assertEqual(org_body["summary"]["total_quantity"], 90)
        self.assertEqual(depot_body["summary"]["total_quantity"], 90)
        self.assertEqual(ledger_body["current_balance"], 90)
        self.assertEqual(org_ledger_body["total_balance_across_depots"], 90)
        self.assertEqual(org_ledger_body["by_depot"][0]["current_balance"], 90)
        self.assertEqual(len(depot_profile["stock_items"]), 1)
        self.assertEqual(depot_profile["stock_items"][0]["current_quantity"], 90)
        self.assertEqual(len(resource_profile["stock_items"]), 1)
        self.assertEqual(resource_profile["stock_items"][0]["current_quantity"], 90)
        self.assertEqual(len(legacy_stock["items"]), 1)
        self.assertEqual(legacy_stock["items"][0]["current_quantity"], 90)
        self.assertEqual(stocktake.status_code, 201, stocktake.get_data(as_text=True))
        self.assertEqual(stocktake.get_json()["total_lines"], 1)
        conn = self.core.get_conn()
        stocktake_line = conn.execute(
            "SELECT expected_quantity FROM stocktake_lines WHERE stocktake_id = ?",
            (stocktake.get_json()["stocktake_id"],),
        ).fetchone()
        conn.close()
        self.assertEqual(stocktake_line["expected_quantity"], 90)

    def test_offline_missing_unit_uses_resolvable_pending_reason(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        self._set_opening_balance_ready(depot_id)
        worker_id = self._create_user(org_id, "Worker")
        resource_id = self._create_resource(org_id)
        response = self.client.post("/offline-batch", json={
            "device_id": "missing-unit-device",
            "items": [{"local_id": "missing-unit-1", "type": "transaction", "queued_at": "2026-07-31T08:00:00", "payload": {
                "organisation_id": org_id, "depot_id": depot_id, "transaction_type": "Movement",
                "resource_id": resource_id, "quantity": 2, "direction": "IN",
                "submitted_by_user_id": worker_id, "operational_unit_missing": True,
                "operational_unit_missing_note": "Truck not listed", "unresolved_entity_note": "Truck not listed",
                "unresolved_entity_type": "OPERATIONAL_UNIT",
            }}],
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        transaction_id = response.get_json()["results"][0]["server_id"]
        conn = self.core.get_conn()
        txn = conn.execute("SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,)).fetchone()
        pending = conn.execute("SELECT * FROM pending_approval_entries WHERE source_record_id = ?", (transaction_id,)).fetchone()
        conn.close()
        self.assertEqual(txn["approval_reason_code"], "MISSING_OPERATIONAL_UNIT")
        self.assertEqual(txn["operational_unit_missing"], 1)
        self.assertEqual(pending["reason_code"], "MISSING_OPERATIONAL_UNIT")

    def test_online_draft_does_not_trigger_offline_fleet_unit_conflict(self):
        org_id = self._create_org()
        depot_id = self._create_depot(org_id)
        self._set_opening_balance_ready(depot_id)
        admin_id = self._create_user(org_id, "Admin", role="ORG_ADMIN")
        online_user = self._create_user(org_id, "Online Worker")
        offline_user = self._create_user(org_id, "Offline Worker")
        resource_id = self._create_resource(org_id)
        unit = self._create_unit(org_id, depot_id, created_by_user_id=admin_id)
        self._create_draft_transaction(org_id, depot_id, resource_id, online_user, unit["operational_unit_id"])
        response = self.client.post("/offline-batch", json={
            "device_id": "offline-only-device",
            "items": [{"local_id": "offline-only-1", "type": "transaction", "queued_at": "2026-07-31T09:00:00", "payload": {
                "organisation_id": org_id, "depot_id": depot_id, "transaction_type": "Movement",
                "resource_id": resource_id, "quantity": 2, "direction": "IN",
                "submitted_by_user_id": offline_user, "operational_unit_id": unit["operational_unit_id"],
            }}],
        })
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["results"][0]["detail"]["status"], "DRAFT")
