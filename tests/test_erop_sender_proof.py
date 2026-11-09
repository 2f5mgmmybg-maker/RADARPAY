import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "radarpay-test-token")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("GITHUB_TOKEN", "test-github-token")
os.environ.setdefault("GITHUB_REPO", "test-owner/test-repo")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "test-encryption-key")

import bot


class EropSenderProofTests(unittest.TestCase):
    def setUp(self):
        self.client = {"id": 246801357, "first_name": "Lanceur"}
        self.vendor = {"id": 975318642, "first_name": "Spitta"}
        self.chat = {
            "id": "erop-test-chat", "client": self.client["id"],
            "vendor": self.vendor["id"], "status": "erop_pending",
            "erop_deadline": 0, "erop_sender_proof_submitted": False,
            "messages": []
        }
        self.state = {"chats": [self.chat]}
        self.user = dict(self.client)
        self.patches = [
            patch.object(bot, "verify", side_effect=lambda _data: dict(self.user)),
            patch.object(bot, "rate_allowed", return_value=True),
            patch.object(bot, "is_blacklisted", return_value=False),
            patch.object(bot, "role", side_effect=lambda user: "lanceur" if user["id"] == self.client["id"] else "spitta"),
            patch.object(bot, "_has_valid_app_session", return_value=True),
            patch.object(bot, "private_load", return_value={"roles": {}, "registrations": [], "blacklist": {}}),
            patch.object(bot, "private_save"),
            patch.object(bot, "chats_load", side_effect=lambda: ({"chats": [self.chat]}, None)),
            patch.object(bot, "chats_save", side_effect=self.save_chats),
            patch.object(bot, "say"),
            patch.object(bot, "gh_put"),
            patch.object(bot, "gh_del"),
            patch.object(bot, "decode_image_data_uri", return_value=b"image-bytes"),
            patch.object(bot, "FERNET", SimpleNamespace(encrypt=lambda value: b"encrypted-image")),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def save_chats(self, data, _sha=None):
        self.state["chats"] = data["chats"]
        self.chat = self.state["chats"][0]

    def call_path(self, path, user, **payload):
        self.user = dict(user)
        return bot.api(path, {
            "initData": "test-init-data", "id": self.chat["id"], **payload,
            "sessionToken": "valid-test-session"
        })

    def call(self, user, **payload):
        return self.call_path("/api/chat/erop-sender-proof", user, **payload)

    def test_launcher_can_declare_photo_unavailable_and_starts_vendor_timer(self):
        before = int(time.time())
        result = self.call(self.client, mode="unavailable")
        self.assertTrue(result["ok"])
        self.assertTrue(self.chat["erop_sender_proof_submitted"])
        self.assertTrue(self.chat["erop_sender_proof_unavailable"])
        self.assertGreaterEqual(self.chat["erop_deadline"], before + 200)
        self.assertIn("ne constitue pas une preuve photographique", self.chat["messages"][-1]["text"])

    def test_launcher_photo_is_encrypted_and_saved_as_chat_proof(self):
        result = self.call(self.client, mode="photo", image="data:image/jpeg;base64,dGVzdA==")
        self.assertTrue(result["ok"])
        self.assertTrue(self.chat["erop_sender_proof_submitted"])
        self.assertFalse(self.chat["erop_sender_proof_unavailable"])
        self.assertTrue(self.chat["messages"][-1]["proof_file"].startswith("private_proofs/"))
        self.assertIn("Preuve d’envoi EROP transmise", self.chat["messages"][-1]["text"])

    def test_spitta_cannot_submit_lanceur_proof(self):
        result = self.call(self.vendor, mode="unavailable")
        self.assertIn("Seul le Lanceur", result["error"])
        self.assertFalse(self.chat["erop_sender_proof_submitted"])

    def test_photo_or_unavailable_choice_is_mandatory(self):
        result = self.call(self.client, mode="other")
        self.assertIn("Choisis une photo", result["error"])
        self.assertFalse(self.chat["erop_sender_proof_submitted"])

    def test_vendor_cannot_send_second_capture_before_launcher_evidence(self):
        result = self.call_path(
            "/api/chat/proof", self.vendor, image="data:image/jpeg;base64,dGVzdA=="
        )
        self.assertIn("Lanceur doit d’abord envoyer sa preuve", result["error"])
        self.assertEqual(self.chat["status"], "erop_pending")

    def test_background_timeout_does_not_blacklist_vendor_before_launcher_evidence(self):
        self.chat["erop_deadline"] = 0
        bot.process_pending_requests()
        self.assertEqual(self.chat["status"], "erop_pending")
        self.assertFalse(self.chat.get("erop_blacklist_until"))

    def test_duplicate_sender_proof_is_rejected(self):
        self.call(self.client, mode="unavailable")
        result = self.call(self.client, mode="unavailable")
        self.assertIn("déjà été enregistrée", result["error"])
        self.assertEqual(len(self.chat["messages"]), 1)


if __name__ == "__main__":
    unittest.main()
