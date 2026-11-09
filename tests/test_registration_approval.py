import os
import unittest
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "radarpay-test-token")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("GITHUB_TOKEN", "test-github-token")
os.environ.setdefault("GITHUB_REPO", "test-owner/test-repo")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "test-encryption-key")

import bot


class RegistrationAndPinLoginTests(unittest.TestCase):
    def setUp(self):
        self.state = {"roles": {}, "registrations": [], "blacklist": {}, "chats": []}
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
            patch.object(bot, "chats_load", return_value=({"chats": []}, None)),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def call_as(self, user, path, **payload):
        self.user = user
        return bot.api(path, {"initData": "test-init-data", **payload})

    def register(self, role="spitta", pin="4826"):
        return self.call_as(self.test_user, "/api/register", role=role, pseudo="Pseudo Test", pin=pin)

    def approve(self):
        self.call_as(self.owner, "/api/admin/approve-registration", user_id=str(self.test_user["id"]))

    def login(self, pin="4826"):
        return self.call_as(self.test_user, "/api/login", pin=pin)

    def test_registration_waits_for_approval_and_stores_only_pin_hash(self):
        result = self.register()
        self.assertEqual(result, {"ok": True, "pending": True})
        self.assertNotIn(str(self.test_user["id"]), self.state["roles"])
        profile = self.state["registrations"][0]
        self.assertEqual(profile["status"], "pending")
        self.assertNotEqual(profile["pin_hash"], "4826")
        self.assertNotIn("4826", profile["pin_hash"])
        self.assertEqual(len(profile["pin_salt"]), 32)

    def test_duplicate_registration_does_not_create_second_account_or_change_role(self):
        self.assertTrue(self.register()["pending"])
        repeated = self.register(role="spitta", pin="9999")
        self.assertTrue(repeated["pending"])
        changed = self.register(role="lanceur")
        self.assertIn("autre profil", changed["error"])
        self.assertEqual(len(self.state["registrations"]), 1)
        self.assertEqual(self.state["registrations"][0]["role"], "spitta")

    def test_pending_user_cannot_login_or_access_protected_routes(self):
        self.register()
        me = self.call_as(self.test_user, "/api/me")
        self.assertIsNone(me["role"])
        self.assertTrue(me["pending"])
        self.assertTrue(me["has_pin"])
        self.assertTrue(self.login()["pending"])
        self.assertIn("attente", self.call_as(self.test_user, "/api/chat/list")["error"])
        self.assertIn("attente", self.call_as(self.test_user, "/api/blacklist")["error"])

    def test_owner_is_recognized_as_admin_without_registration_or_pin(self):
        me = self.call_as(self.owner, "/api/me")
        self.assertEqual(me["role"], "owner")
        self.assertFalse(me["pending"])

    def test_admin_can_approve_then_user_logs_in_with_pin_and_gets_session(self):
        self.register()
        admin_list = self.call_as(self.owner, "/api/admin/registrations")
        self.assertTrue(admin_list["ok"])
        safe_profile = admin_list["registrations"][0]
        self.assertNotIn("pin_hash", safe_profile)
        self.assertNotIn("pin_salt", safe_profile)
        approved = self.call_as(self.owner, "/api/admin/approve-registration", user_id=str(self.test_user["id"]))
        self.assertEqual(approved["role"], "spitta")
        self.assertEqual(self.state["roles"][str(self.test_user["id"])], "spitta")
        self.assertEqual(self.state["registrations"][0]["status"], "approved")

        self.assertIn("incorrect", self.login("0000")["error"])
        result = self.login("4826")
        self.assertTrue(result["ok"])
        self.assertEqual(result["role"], "spitta")
        self.assertTrue(result["session_token"])
        self.assertIn("Session fermée", self.call_as(self.test_user, "/api/chat/list")["error"])
        chat_list = self.call_as(self.test_user, "/api/chat/list", sessionToken=result["session_token"])
        self.assertTrue(chat_list["ok"])

    def test_approved_legacy_account_can_create_pin_without_registering_again(self):
        uid = str(self.test_user["id"])
        self.state["roles"][uid] = "lanceur"
        me = self.call_as(self.test_user, "/api/me")
        self.assertEqual(me["role"], "lanceur")
        self.assertFalse(me["has_pin"])
        setup = self.call_as(self.test_user, "/api/pin/setup", pin="1357")
        self.assertTrue(setup["ok"])
        self.assertEqual(setup["role"], "lanceur")
        login = self.login("1357")
        self.assertTrue(login["ok"])

    def test_non_admin_cannot_approve_even_after_login(self):
        uid = str(self.test_user["id"])
        self.state["roles"][uid] = "lanceur"
        setup = self.call_as(self.test_user, "/api/pin/setup", pin="2468")
        denied = self.call_as(self.test_user, "/api/admin/approve-registration",
                              user_id=uid, sessionToken=setup["session_token"])
        self.assertIn("administrateur", denied["error"])

    def test_invalid_role_pseudo_or_pin_does_not_create_registration(self):
        self.assertIn("Choisis Lanceur", self.call_as(self.test_user, "/api/register",
                    role="admin", pseudo="Pseudo Test", pin="1234")["error"])
        self.assertIn("pseudo", self.call_as(self.test_user, "/api/register",
                    role="spitta", pseudo="x", pin="1234")["error"])
        self.assertIn("4 chiffres", self.call_as(self.test_user, "/api/register",
                    role="spitta", pseudo="Pseudo Test", pin="12ab")["error"])
        self.assertEqual(self.state["registrations"], [])

    def test_five_wrong_pin_attempts_temporarily_lock_login(self):
        self.register()
        self.approve()
        for _ in range(5):
            self.assertIn("incorrect", self.login("0000")["error"])
        self.assertIn("Trop de codes incorrects", self.login("4826")["error"])


if __name__ == "__main__":
    unittest.main()
