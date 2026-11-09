import os
import unittest
from unittest.mock import patch

# bot.py reads configuration at import time; use disposable test-only values.
os.environ.setdefault("BOT_TOKEN", "radarpay-test-token")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("GITHUB_TOKEN", "test-github-token")
os.environ.setdefault("GITHUB_REPO", "test-owner/test-repo")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "test-encryption-key")

import bot


class RegistrationApprovalFlowTests(unittest.TestCase):
    """Tests the full registration -> pending -> admin approval -> access flow."""

    def setUp(self):
        self.state = {"roles": {}, "registrations": [], "blacklist": {}}
        self.test_user = {"id": 246801357, "first_name": "Test User", "username": "test_user"}
        self.user = dict(self.test_user)
        self.owner = {"id": bot.OWNER, "first_name": "Admin"}
        self.patches = [
            patch.object(bot, "verify", side_effect=lambda _data: dict(self.user)),
            patch.object(bot, "rate_allowed", return_value=True),
            patch.object(bot, "private_load", side_effect=lambda: self.state),
            patch.object(bot, "private_save"),
            patch.object(bot, "is_blacklisted", return_value=False),
            patch.object(bot, "load", return_value=({"photos": []}, None)),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def call_as(self, user, path, **payload):
        self.user = user
        return bot.api(path, {"initData": "test-init-data", **payload})

    def test_complete_registration_pending_approval_access_flow(self):
        # 1. Registration creates a pending request, never a role.
        result = self.call_as(self.test_user, "/api/role", role="spitta", pseudo="Pseudo Test")
        self.assertEqual(result, {"ok": True, "pending": True})
        self.assertNotIn(str(self.test_user["id"]), self.state["roles"])
        self.assertEqual(len(self.state["registrations"]), 1)
        self.assertEqual(self.state["registrations"][0]["status"], "pending")

        # 2. A repeated request remains pending; changing role is rejected.
        self.assertEqual(
            self.call_as(self.user, "/api/role", role="spitta", pseudo="Pseudo Test"),
            {"ok": True, "pending": True},
        )
        self.assertIn("attente", self.call_as(
            self.user, "/api/role", role="lanceur", pseudo="Pseudo Test"
        )["error"])

        # 3. Before approval, /api/me reports pending and protected APIs deny access.
        me = self.call_as(self.test_user, "/api/me")
        self.assertIsNone(me["role"])
        self.assertTrue(me["pending"])
        self.assertIn("attente", self.call_as(self.test_user, "/api/chat/list")["error"])
        self.assertIn("attente", self.call_as(self.test_user, "/api/blacklist")["error"])

        # 4. A non-admin cannot approve the request.
        denied = self.call_as(self.test_user, "/api/admin/approve-registration", user_id=str(self.user["id"]))
        self.assertIn("administrateur", denied["error"])
        self.assertNotIn(str(self.user["id"]), self.state["roles"])

        # 5. Admin sees pending request and approves it.
        admin_list = self.call_as(self.owner, "/api/admin/registrations")
        self.assertTrue(admin_list["ok"])
        self.assertEqual(admin_list["registrations"][0]["status"], "pending")
        approved = self.call_as(
            self.owner, "/api/admin/approve-registration", user_id=str(self.user["id"])
        )
        self.assertEqual(approved, {
            "ok": True, "user_id": str(self.user["id"]), "role": "spitta"
        })
        self.assertEqual(self.state["roles"][str(self.user["id"])], "spitta")
        self.assertEqual(self.state["registrations"][0]["status"], "approved")

        # 6. After approval, /api/me grants the role and clears pending.
        me_after = self.call_as(self.user, "/api/me")
        self.assertEqual(me_after["role"], "spitta")
        self.assertFalse(me_after["pending"])

        # 7. Approval is one-shot; a second approval is rejected.
        again = self.call_as(
            self.owner, "/api/admin/approve-registration", user_id=str(self.user["id"])
        )
        self.assertIn("introuvable", again["error"])

    def test_invalid_registration_does_not_create_pending_request(self):
        bad_role = self.call_as(self.user, "/api/role", role="admin", pseudo="Pseudo Test")
        self.assertIn("Choisis Lanceur ou Spitta", bad_role["error"])
        bad_pseudo = self.call_as(self.user, "/api/role", role="spitta", pseudo="x")
        self.assertIn("pseudo", bad_pseudo["error"])
        self.assertEqual(self.state["registrations"], [])
        self.assertEqual(self.state["roles"], {})

    def test_admin_registration_list_is_owner_only(self):
        result = self.call_as(self.user, "/api/admin/registrations")
        self.assertIn("administrateur", result["error"])

    def test_legacy_account_with_matching_role_is_reported_approved(self):
        self.state["roles"][str(self.user["id"])] = "spitta"
        self.state["registrations"].append({
            "user_id": str(self.user["id"]), "pseudo": "Ancien compte",
            "role": "spitta", "created": 1
        })
        result = self.call_as(self.owner, "/api/admin/registrations")
        self.assertEqual(result["registrations"][0]["status"], "approved")


if __name__ == "__main__":
    unittest.main()
