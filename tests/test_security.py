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
        with unittest.mock.patch.object(bot, "private_save"), \\
             unittest.mock.patch.object(bot, "say_async") as notify, \\
             unittest.mock.patch.object(bot.threading, "Thread"):
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


if __name__ == "__main__":
    unittest.main()
