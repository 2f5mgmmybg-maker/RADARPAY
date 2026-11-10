import os
import unittest
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "radarpay-test-token")
os.environ.setdefault("OWNER_ID", "123456789")
os.environ.setdefault("GITHUB_TOKEN", "test-github-token")
os.environ.setdefault("GITHUB_REPO", "test-owner/test-repo")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "test-encryption-key")

import bot


class PublicationOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.spitta_id = 975318642
        self.lanceur_id = 246801357
        self.user = {"id": self.lanceur_id, "first_name": "Lanceur", "username": "client_test"}
        self.private = {"roles": {str(self.spitta_id): "spitta", str(self.lanceur_id): "lanceur"},
                        "registrations": [
                            {"user_id": str(self.spitta_id), "pseudo": "Vendeur Test", "role": "spitta",
                             "status": "approved", "telegram_username": "vendeur_test"},
                            {"user_id": str(self.lanceur_id), "pseudo": "Client Test", "role": "lanceur",
                             "status": "approved", "telegram_username": "client_test"}
                        ], "blacklist": {}}
        self.photo = {"id": "target-1", "image": "target-1.jpg", "prix": "2500 €",
                      "categorie": "2500", "o": bot.enc(self.spitta_id, "target-1")}
        self.photos = {"photos": [dict(self.photo)]}
        self.patches = [
            patch.object(bot, "verify", side_effect=lambda _data: dict(self.user)),
            patch.object(bot, "rate_allowed", return_value=True),
            patch.object(bot, "is_blacklisted", return_value=False),
            patch.object(bot, "role", side_effect=lambda u: "owner" if u["id"] == bot.OWNER else self.private["roles"].get(str(u["id"]))),
            patch.object(bot, "_has_valid_app_session", return_value=True),
            patch.object(bot, "private_load", side_effect=lambda: self.private),
            patch.object(bot, "private_save"),
            patch.object(bot, "load", side_effect=lambda: (self.photos, "sha")),
            patch.object(bot, "save"),
            patch.object(bot, "gh_put"),
            patch.object(bot, "decode_image_data_uri", return_value=b"image-bytes"),
            patch.object(bot, "say"),
            patch.object(bot, "chats_load", return_value=({"chats": []}, None)),
            patch.object(bot, "chats_save")
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def call_as(self, uid, role, path, **payload):
        self.user = {"id": uid, "first_name": "Client", "username": "client_test"}
        self.private["roles"][str(uid)] = role
        return bot.api(path, {"initData": "test-init", "sessionToken": "valid-session", **payload})

    def test_lanceur_cannot_make_offer_on_own_listing(self):
        self.photos["photos"][0]["o"] = bot.enc(self.lanceur_id, "target-1")
        result = self.call_as(self.lanceur_id, "lanceur", "/api/want",
                              id="target-1", client_offer="2000 €")
        self.assertIn("propre publication", result["error"])
        bot.say.assert_not_called()

    def test_lanceur_can_offer_on_another_users_listing(self):
        result = self.call_as(self.lanceur_id, "lanceur", "/api/want",
                              id="target-1", client_offer="2300 €")
        self.assertTrue(result["ok"])
        self.assertEqual(self.photos["photos"][0]["id"], "target-1")
        bot.say.assert_called_once()

    def test_publication_stores_signup_pseudo_and_notifies_admin(self):
        result = self.call_as(self.spitta_id, "spitta", "/api/add",
                              image="data:image/jpeg;base64,dGVzdA==",
                              prix="2500 €", categorie="2500", pourcentage="10",
                              pays="Belgique")
        self.assertTrue(result["ok"])
        saved_photo = self.photos["photos"][0]
        self.assertEqual(saved_photo["auteur"], "Vendeur Test")
        self.assertEqual(result["photo"]["auteur"], "Vendeur Test")
        bot.say.assert_called_once()
        self.assertIn("Vendeur Test", bot.say.call_args.args[1])
        self.assertIn(str(self.spitta_id), bot.say.call_args.args[1])

    def test_spitta_cannot_change_another_users_price(self):
        self.user = {"id": self.spitta_id, "first_name": "Vendeur"}
        self.photos["photos"][0]["o"] = bot.enc(self.lanceur_id, "target-1")
        result = bot.api("/api/update", {"initData": "test-init", "sessionToken": "valid-session",
                                        "id": "target-1", "prix": "5000 €"})
        self.assertIn("tes propres publications", result["error"])

    def test_owner_can_only_set_one_of_the_allowed_prices(self):
        self.user = {"id": bot.OWNER, "first_name": "Admin"}
        result = bot.api("/api/update", {"initData": "test-init", "id": "target-1", "prix": "123 €"})
        self.assertIn("Choisis uniquement", result["error"])

    def test_publisher_can_change_only_own_price_and_category_updates(self):
        result = self.call_as(self.spitta_id, "spitta", "/api/update",
                              id="target-1", prix="5000 €")
        self.assertTrue(result["ok"])
        self.assertEqual(self.photos["photos"][0]["prix"], "5000 €")
        self.assertEqual(self.photos["photos"][0]["categorie"], "5000")

    def test_app_access_refreshes_verified_telegram_identity_and_last_seen(self):
        result = self.call_as(self.spitta_id, "spitta", "/api/me")
        self.assertEqual(result["telegram_username"], "client_test")
        profile = next(x for x in self.private["registrations"]
                       if x["user_id"] == str(self.spitta_id))
        self.assertTrue(profile["last_seen"])
        self.assertEqual(profile["telegram_username"], "client_test")

    def test_only_spitta_can_publish_target(self):
        result = self.call_as(self.lanceur_id, "lanceur", "/api/add",
                              image="data:image/jpeg;base64,dGVzdA==",
                              prix="2500 €", categorie="2500", pourcentage="10", pays="Belgique")
        self.assertIn("Seuls les comptes SPITTA", result["error"])
        bot.gh_put.assert_not_called()
        bot.save.assert_not_called()

    def test_owner_cannot_publish_target_from_app(self):
        self.user = {"id": bot.OWNER, "first_name": "Admin"}
        result = bot.api("/api/add", {"initData": "test-init", "image": "data:image/jpeg;base64,dGVzdA==",
                                      "prix": "2500 €", "categorie": "2500", "pourcentage": "10"})
        self.assertIn("Seuls les comptes SPITTA", result["error"])
        bot.gh_put.assert_not_called()
        bot.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
