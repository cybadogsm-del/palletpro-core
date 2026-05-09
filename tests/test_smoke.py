import importlib
import os
import sys
import unittest


class PalletProSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = os.path.abspath("test_pallet_pro.db")
        os.environ["PALLET_PRO_DB"] = cls.db_path

        for module_name in ("pallet_pro_core", "audit", "db"):
            sys.modules.pop(module_name, None)

        cls.core = importlib.import_module("pallet_pro_core")
        cls.client = cls.core.app.test_client()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.db_path):
            os.remove(cls.db_path)

    def test_health_endpoint(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def test_create_organisation_records_audit_event(self):
        response = self.client.post("/organisations", json={"name": "Demo Org"})

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["name"], "Demo Org")
        self.assertTrue(payload["organisation_id"].startswith("org_"))

        conn = self.core.get_conn()
        audit_row = conn.execute(
            """
            SELECT *
            FROM audit_events
            WHERE entity_type = 'Organisation'
              AND entity_id = ?
              AND action = 'CREATE'
            """,
            (payload["organisation_id"],),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(audit_row)


if __name__ == "__main__":
    unittest.main()
