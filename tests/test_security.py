import base64
import hashlib
import hmac
import json
import os
import time
import unittest
from urllib.parse import urlencode

# bot.py reads its configuration at import time. Use disposable test-only values.
os.environ["BOT_TOKEN"] = "radarpay-test-token"
os.environ["OWNER_ID"] = "123456789"
os.environ["GITHUB_TOKEN"] = "test-github-token"
os.environ["GITHUB_REPO"] = "test-owner/test-repo"
os.environ["DATA_ENCRYPTION_KEY"] = "test-encryption-key"
os.environ["NOWPAYMENTS_IPN_SECRET"] = "test-nowpayments-ipn-secret"

import bot
bot.NOWPAYMENTS_IPN_SECRET = os.environ["NOWPAYMENTS_IPN_SECRET"]


class SecurityTests(unittest.TestCase):
    def test_audio_signatures_are_checked(self):
        samples = {
            "audio/webm": b"\x1a\x45\xdf\xa3payload",
            "audio/ogg": b"OggSpayload",
            "audio/mp4": b"\x00\x00\x00\x18ftypisomdata",
            "audio/mpeg": b"ID3payload",
            "audio/aac": b"\xff\xf1payload",
            "audio/3gpp": b"#!AMR\npayload",
        }
        for mime, raw in samples.items():
            with self.subTest(mime=mime):
                uri = f"data:{mime};base64," + base64.b64encode(raw).decode()
                self.assertEqual(bot.decode_audio_data_uri(uri, mime), raw)

    def test_audio_rejects_fake_or_mismatched_content(self):
        fake = "data:audio/webm;base64," + base64.b64encode(b"not audio").decode()
        with self.assertRaises(ValueError):
            bot.decode_audio_data_uri(fake, "audio/webm")
        valid_ogg = "data:audio/ogg;base64," + base64.b64encode(b"OggSpayload").decode()
        with self.assertRaises(ValueError):
            bot.decode_audio_data_uri(valid_ogg, "audio/webm")

    def test_audio_rejects_oversized_payload(self):
        raw = b"\x1a\x45\xdf\xa3" + b"x" * 650000
        uri = "data:audio/webm;base64," + base64.b64encode(raw).decode()
        with self.assertRaises(ValueError):
            bot.decode_audio_data_uri(uri, "audio/webm")

    def test_image_signature_is_checked(self):
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\ncontent").decode()
        self.assertTrue(bot.decode_image_data_uri(png).startswith(b"\x89PNG"))
        fake = "data:image/png;base64," + base64.b64encode(b"not an image").decode()
        with self.assertRaises(ValueError):
            bot.decode_image_data_uri(fake)

    def test_jpeg_storage_rejects_other_image_formats(self):
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\ncontent").decode()
        jpeg = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffcontent").decode()
        with self.assertRaises(ValueError):
            bot.decode_jpeg_data_uri(png)
        self.assertTrue(bot.decode_jpeg_data_uri(jpeg).startswith(b"\xff\xd8\xff"))

    def test_owner_metadata_fails_closed(self):
        self.assertIsNone(bot.owner_of({"id": "listing-without-owner"}))
        pid = "test-listing"
        listing = {"id": pid, "o": bot.enc(987654321, pid)}
        self.assertEqual(bot.owner_of(listing), 987654321)

    def test_telegram_init_data_signature_and_age(self):
        now = int(time.time())
        data = {
            "auth_date": str(now),
            "user": json.dumps({"id": 987654321, "first_name": "Test"}, separators=(",", ":")),
        }
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret = hmac.new(b"WebAppData", os.environ["BOT_TOKEN"].encode(), hashlib.sha256).digest()
        data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        signed = urlencode(data)
        self.assertEqual(bot.verify(signed)["id"], 987654321)
        self.assertIsNone(bot.verify(signed + "&auth_date=" + str(now)))
        data["auth_date"] = str(now - 3601)
        check = "\n".join(f"{key}={value}" for key, value in sorted({k: v for k, v in data.items() if k != "hash"}.items()))
        data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        self.assertIsNone(bot.verify(urlencode(data)))

    def test_owner_can_access_admin_without_a_pseudo(self):
        now = int(time.time())
        user = {"id": int(os.environ["OWNER_ID"]), "first_name": "Owner"}
        data = {
            "auth_date": str(now),
            "user": json.dumps(user, separators=(",", ":")),
        }
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret = hmac.new(b"WebAppData", os.environ["BOT_TOKEN"].encode(), hashlib.sha256).digest()
        data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        response = bot.api("/api/role", {"initData": urlencode(data), "role": "lanceur"})
        self.assertEqual(response, {"ok": True, "role": "owner"})

    def test_erop_cannot_restart_while_eraf_is_pending(self):
        pending = {"erop_photo_sent": True, "erop_confirmed": False}
        confirmed = {"erop_photo_sent": False, "erop_confirmed": True}
        self.assertTrue(bot.eraf_validation_pending(pending))
        self.assertFalse(bot.eraf_validation_pending(confirmed))
        self.assertFalse(bot.eraf_validation_pending({"erop_photo_sent": False}))

    def test_erop_sender_timeout_reopens_chat_without_penalty(self):
        chat = {
            "status": "erop_pending",
            "erop_previous_status": "proof_received",
            "erop_sender_deadline": 100,
            "erop_sender_proof_submitted": False,
            "messages": [],
        }
        self.assertTrue(bot.expire_erop_sender_step(chat, 100))
        self.assertEqual(chat["status"], "proof_received")
        self.assertNotIn("erop_previous_status", chat)
        self.assertNotIn("erop_sender_deadline", chat)
        self.assertIn("sans sanction pour le SPITTA", chat["messages"][-1]["text"])
        self.assertFalse(bot.expire_erop_sender_step(chat, 101))

    def test_payment_report_accepts_late_followup_for_admin_review(self):
        awaiting = {"status": "awaiting", "started_at": 100, "deadline": 200}
        overdue = {"status": "overdue", "started_at": 100, "deadline": 200}
        submitted = {"status": "submitted", "started_at": 100, "deadline": 200}
        self.assertTrue(bot.payment_report_followup_valid(awaiting, 100))
        self.assertTrue(bot.payment_report_followup_valid(overdue, 100))
        self.assertFalse(bot.payment_report_followup_valid(submitted, 100))
        self.assertFalse(bot.payment_report_followup_valid(overdue, 0))
        self.assertFalse(bot.payment_report_is_late(awaiting, 199))
        self.assertTrue(bot.payment_report_is_late(awaiting, 200))
        self.assertTrue(bot.payment_report_is_late(overdue, 250))

    def test_chat_retention_covers_full_erop_eraf_flow(self):
        self.assertGreaterEqual(bot.CHAT_TTL, 24 * 60 * 60)
        self.assertEqual(bot.EROP_BLACKLIST_SECONDS, 7 * 24 * 60 * 60)
        self.assertEqual(bot.ERAF_BLACKLIST_SECONDS, 10 * 24 * 60 * 60)

    def test_chat_retention_uses_last_message_activity(self):
        now = int(time.time())
        chat = {"created": now - 3 * 24 * 60 * 60, "messages": [{"time": now - 60}]}
        self.assertEqual(bot.chat_last_activity(chat), now - 60)


    def test_nowpayments_ipn_signature_uses_sorted_compact_json(self):
        payload = {
            "z": "fin",
            "nested": {"b": 2, "a": "été"},
            "a": [{"y": 1, "x": True}],
        }
        canonical = json.dumps(bot._nowpayments_sort(payload), separators=(",", ":"))
        expected = hmac.new(
            os.environ["NOWPAYMENTS_IPN_SECRET"].encode(),
            canonical.encode(),
            hashlib.sha512,
        ).hexdigest()
        self.assertTrue(bot.nowpayments_signature_valid(payload, expected))
        self.assertFalse(bot.nowpayments_signature_valid(payload, "0" * 128))
        self.assertFalse(bot.nowpayments_signature_valid(payload, ""))

    def test_nowpayments_ipn_confirms_only_matching_order_and_is_idempotent(self):
        import unittest.mock
        bot.PRIVATE_CACHE = {
            "digital_targets": [],
            "digital_orders": [{
                "id": "order-test-1",
                "caller_id": "987654321",
                "caller_name": "Caller Test",
                "title": "Target test",
                "price": "25 €",
                "status": "awaiting_payment",
                "payment_provider": "nowpayments",
                "nowpayments_invoice_id": "invoice-test-1",
            }],
        }
        bot.STORAGE_READY = True
        payload = {
            "order_id": "order-test-1",
            "invoice_id": "invoice-test-1",
            "payment_id": "payment-test-1",
            "payment_status": "finished",
            "price_amount": 25,
            "price_currency": "eur",
            "pay_currency": "btc",
            "actually_paid": "0.0001",
        }
        with unittest.mock.patch.object(bot, "private_save"), unittest.mock.patch.object(bot, "say_async") as notify, unittest.mock.patch.object(bot.threading, "Thread"):
            result = bot.apply_nowpayments_ipn(payload)
            self.assertTrue(result["ok"])
            self.assertEqual(bot.PRIVATE_CACHE["digital_orders"][0]["status"], "paid")
            self.assertEqual(bot.PRIVATE_CACHE["digital_orders"][0]["payment_status"], "finished")
            self.assertEqual(notify.call_count, 2)
            duplicate = bot.apply_nowpayments_ipn(payload)
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(notify.call_count, 2)

    def test_nowpayments_ipn_rejects_wrong_invoice_or_amount(self):
        import unittest.mock
        bot.PRIVATE_CACHE = {
            "digital_targets": [],
            "digital_orders": [{
                "id": "order-test-2",
                "caller_id": "987654321",
                "status": "awaiting_payment",
                "payment_provider": "nowpayments",
                "nowpayments_invoice_id": "invoice-correct",
            }],
        }
        bot.STORAGE_READY = True
        with unittest.mock.patch.object(bot, "private_save"):
            with self.assertRaises(ValueError):
                bot.apply_nowpayments_ipn({
                    "order_id": "order-test-2", "invoice_id": "invoice-wrong",
                    "payment_status": "finished", "price_amount": 25, "price_currency": "eur",
                })
            with self.assertRaises(ValueError):
                bot.apply_nowpayments_ipn({
                    "order_id": "order-test-2", "invoice_id": "invoice-correct",
                    "payment_status": "finished", "price_amount": 24, "price_currency": "eur",
                })
            self.assertEqual(bot.PRIVATE_CACHE["digital_orders"][0]["status"], "awaiting_payment")


    def test_nowpayments_ipn_requires_exact_invoice_id(self):
        bot.PRIVATE_CACHE = {
            "digital_targets": [],
            "digital_orders": [{
                "id": "order-test-invoice",
                "caller_id": "987654321",
                "status": "awaiting_payment",
                "payment_provider": "nowpayments",
                "nowpayments_invoice_id": "invoice-required",
            }],
        }
        bot.STORAGE_READY = True
        base = {
            "order_id": "order-test-invoice",
            "payment_status": "finished",
            "price_amount": 25,
            "price_currency": "eur",
        }
        with unittest.mock.patch.object(bot, "private_save"):
            with self.assertRaisesRegex(ValueError, "Identifiant de facture"):
                bot.apply_nowpayments_ipn(dict(base))
            with self.assertRaisesRegex(ValueError, "Identifiant de facture"):
                bot.apply_nowpayments_ipn(dict(base, invoice_id=""))
            with self.assertRaisesRegex(ValueError, "Identifiant de facture"):
                bot.apply_nowpayments_ipn(dict(base, invoice_id="invoice-other"))
        self.assertEqual(bot.PRIVATE_CACHE["digital_orders"][0]["status"], "awaiting_payment")

    def test_nowpayments_orders_cannot_use_manual_payment_confirmation(self):
        import unittest.mock
        now = int(time.time())
        user_id = 987654321
        session_token = "test-session-token"
        salt = "00112233445566778899aabbccddeeff"
        bot.PRIVATE_CACHE = {
            "roles": {str(user_id): "lanceur"},
            "blacklist": {},
            "registrations": [{
                "user_id": str(user_id), "role": "lanceur", "status": "approved",
                "pin_salt": salt, "pin_hash": "not-used",
                "session_hash": hashlib.sha256(session_token.encode()).hexdigest(),
                "session_expires": now + 3600,
            }],
            "digital_orders": [{
                "id": "order-locked",
                "caller_id": str(user_id),
                "status": "awaiting_payment",
                "payment_provider": "nowpayments",
                "nowpayments_invoice_id": "invoice-locked",
                "title": "TARGET test", "price": "25 €",
            }],
        }
        bot.STORAGE_READY = True
        user = {"id": user_id, "first_name": "Caller Test"}
        data = {
            "auth_date": str(now),
            "user": json.dumps(user, separators=(",", ":")),
        }
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret = hmac.new(b"WebAppData", os.environ["BOT_TOKEN"].encode(), hashlib.sha256).digest()
        data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        init_data = urlencode(data)
        with unittest.mock.patch.object(bot, "private_save"), unittest.mock.patch.object(bot, "say_async"):
            caller_result = bot.api("/api/digital/paid", {
                "initData": init_data, "sessionToken": session_token, "id": "order-locked"
            })
            self.assertIn("automatiquement", caller_result.get("error", ""))

            owner_data = {
                "auth_date": str(now),
                "user": json.dumps({"id": int(os.environ["OWNER_ID"]), "first_name": "Owner"}, separators=(",", ":")),
            }
            owner_check = "\n".join(f"{key}={value}" for key, value in sorted(owner_data.items()))
            owner_data["hash"] = hmac.new(secret, owner_check.encode(), hashlib.sha256).hexdigest()
            owner_init = urlencode(owner_data)
            os.environ["ADMIN_ACCESS_CODE"] = "134679"
            bot.OWNER_ADMIN_SESSIONS.clear()
            admin_auth = bot.api("/api/admin/access", {"initData": owner_init, "code": "134679"})
            admin_token = admin_auth.get("session_token", "")
            self.assertTrue(admin_token)
            instructions_result = bot.api("/api/digital/instructions", {
                "initData": owner_init, "sessionToken": admin_token,
                "id": "order-locked", "instructions": "wallet"
            })
            self.assertIn("NOWPayments", instructions_result.get("error", ""))

            bot.PRIVATE_CACHE["digital_orders"][0]["status"] = "payment_reported"
            admin_result = bot.api("/api/digital/order-status", {
                "initData": owner_init, "sessionToken": admin_token,
                "id": "order-locked", "action": "received"
            })
            self.assertIn("IPN signée", admin_result.get("error", ""))
        self.assertEqual(bot.PRIVATE_CACHE["digital_orders"][0]["status"], "payment_reported")



    def test_owner_admin_access_requires_code_and_server_session(self):
        now = int(time.time())
        owner_id = int(os.environ["OWNER_ID"])
        user = {"id": owner_id, "first_name": "Owner"}
        data = {
            "auth_date": str(now),
            "user": json.dumps(user, separators=(",", ":")),
        }
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret = hmac.new(b"WebAppData", os.environ["BOT_TOKEN"].encode(), hashlib.sha256).digest()
        data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        init_data = urlencode(data)
        bot.PRIVATE_CACHE = {"digital_targets": [], "digital_orders": [], "blacklist": {}}
        bot.STORAGE_READY = True
        bot.OWNER_ADMIN_SESSIONS.clear()
        os.environ["ADMIN_ACCESS_CODE"] = "134679"

        with unittest.mock.patch.object(bot, "private_save"):
            blocked = bot.api("/api/digital/admin", {"initData": init_data})
            self.assertIn("Session administrateur fermée", blocked.get("error", ""))

            wrong = bot.api("/api/admin/access", {"initData": init_data, "code": "000000"})
            self.assertIn("Code incorrect", wrong.get("error", ""))

            opened = bot.api("/api/admin/access", {"initData": init_data, "code": "134679"})
            self.assertTrue(opened.get("session_token"))
            allowed = bot.api("/api/digital/admin", {
                "initData": init_data, "sessionToken": opened["session_token"]
            })
            self.assertTrue(allowed.get("ok"))

            bot.OWNER_ADMIN_SESSIONS[owner_id]["expires"] = now - 1
            expired = bot.api("/api/digital/admin", {
                "initData": init_data, "sessionToken": opened["session_token"]
            })
            self.assertIn("Session administrateur fermée", expired.get("error", ""))

        self.assertIn("ADMIN", bot.api("/api/login", {"initData": init_data, "pin": "1234"}).get("error", ""))
        self.assertIn("ADMIN", bot.api("/api/register", {"initData": init_data, "role": "lanceur"}).get("error", ""))
        self.assertIn("ADMIN", bot.api("/api/pin/setup", {"initData": init_data, "pin": "1234"}).get("error", ""))



if __name__ == "__main__":
    unittest.main()
