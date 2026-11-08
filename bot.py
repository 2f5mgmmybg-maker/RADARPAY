import os, time, json, base64, hashlib, hmac, threading, requests
from cryptography.fernet import Fernet
from urllib.parse import parse_qsl
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])
GH_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPO"]            # exemple : monnom/radar-site
BRANCH = os.environ.get("GITHUB_BRANCH", "main")
SITE = os.environ.get("SITE_URL", "")
LINK = os.environ.get("MINIAPP_LINK") or SITE
ACCESS_CODE = os.environ.get("ACCESS_CODE", "")
# URL publique du Web Service Render. API_BASE_URL permet une valeur explicite
# si RENDER_EXTERNAL_URL n'est pas disponible.
API_URL = (os.environ.get("API_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).strip().rstrip("/")
LOCK = threading.Lock()
FAILS = {}
WANTED = {}

API = f"https://api.telegram.org/bot{TOKEN}"
FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256((TOKEN + "|RADARPAY_PRIVATE_V1").encode()).digest()))
PROOF_CACHE = {}
GH = f"https://api.github.com/repos/{REPO}/contents"
H = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]

AIDE = (
    "Panneau admin RADAR PAYMENT 💡\n\n"
    "📸 Envoie une photo avec le prix en légende (ex : 120 €) : elle est ajoutée.\n"
    "/liste : voir les photos et les prix\n"
    "/prix 2 150 € : changer le prix de la photo n°2\n"
    "/supprimer 2 : enlever la photo n°2\n"
    "/vider oui : tout enlever\n"
    "/menu : afficher le bouton pour ouvrir la mini app"
)


def tg(method, **p):
    r = requests.post(f"{API}/{method}", json=p, timeout=40)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram a refusé l'envoi."))
    return data


def say(chat, text, **k):
    tg("sendMessage", chat_id=chat, text=text, **k)


def today():
    d = datetime.now(ZoneInfo("Europe/Brussels"))
    return f"{d.day} {MOIS[d.month - 1]}"


def gh_get(path):
    r = requests.get(f"{GH}/{path}", headers=H, params={"ref": BRANCH}, timeout=30)
    r.raise_for_status()
    return r.json()


def gh_put(path, data, msg, sha=None):
    body = {"message": msg, "content": base64.b64encode(data).decode(), "branch": BRANCH}
    if sha:
        body["sha"] = sha
    requests.put(f"{GH}/{path}", headers=H, json=body, timeout=60).raise_for_status()


def gh_del(path, msg):
    try:
        sha = gh_get(path)["sha"]
        requests.delete(f"{GH}/{path}", headers=H, timeout=30,
                        json={"message": msg, "sha": sha, "branch": BRANCH})
    except Exception:
        pass


def load():
    f = gh_get("photos.json")
    raw = base64.b64decode(f["content"]).decode() or "{}"
    data = json.loads(raw)
    data.setdefault("photos", [])
    return data, f["sha"]


def save(data, sha):
    data["maj"] = today()
    gh_put("photos.json", json.dumps(data, ensure_ascii=False, indent=2).encode(),
           "Mise à jour des photos", sha)


CHAT_TTL = 600
PRIVATE_CACHE = None


def private_load():
    global PRIVATE_CACHE
    if PRIVATE_CACHE is not None:
        return PRIVATE_CACHE
    try:
        f = gh_get("radar_private.enc")
        PRIVATE_CACHE = json.loads(FERNET.decrypt(base64.b64decode(f.get("content", ""))).decode())
    except Exception:
        PRIVATE_CACHE = {"roles": {}, "chats": []}
        try:
            old = gh_get("user_roles.json")
            PRIVATE_CACHE["roles"] = json.loads(base64.b64decode(old.get("content", "")).decode() or "{}")
        except Exception:
            pass
        try:
            private_save()
            gh_del("user_roles.json", "Migration vers les données privées chiffrées")
        except Exception as e:
            print("migration données privées:", repr(e))
    PRIVATE_CACHE.setdefault("roles", {})
    PRIVATE_CACHE.setdefault("chats", [])
    return PRIVATE_CACHE


def private_save():
    encrypted = FERNET.encrypt(json.dumps(private_load(), ensure_ascii=False, separators=(",", ":")).encode())
    try:
        sha = gh_get("radar_private.enc").get("sha")
    except Exception:
        sha = None
    gh_put("radar_private.enc", encrypted, "Sauvegarde chiffrée RADAR PAYMENT", sha)


def chats_load():
    data = private_load()
    now = int(time.time())
    before = len(data.get("chats", []))
    data["chats"] = [c for c in data.get("chats", [])
                     if now - int(c.get("created", now)) < CHAT_TTL]
    if len(data["chats"]) != before:
        private_save()
    return data, None


def cleanup_chats():
    while True:
        try:
            with LOCK:
                chats_load()
        except Exception as e:
            print("nettoyage discussions:", repr(e))
        time.sleep(15)


def chats_save(data, sha=None):
    private_load()["chats"] = data.get("chats", [])
    private_save()


def chat_update(rid, **changes):
    with LOCK:
        data, sha = chats_load()
        chat = next((x for x in data.get("chats", []) if x.get("id") == rid), None)
        if chat:
            chat.update(changes)
            chats_save(data, sha)
        return chat


def send_menu(chat, private=False):
    btn = {"text": "Ouvrir RADAR PAYMENT 💡"}
    if private and SITE:
        btn["web_app"] = {"url": SITE}
    elif LINK:
        btn["url"] = LINK
    else:
        say(chat, "Le lien de la mini app n'est pas encore réglé.")
        return
    say(chat, "Bienvenue 💡 Touche le bouton pour voir mes produits.",
        reply_markup={"inline_keyboard": [[btn]]})


def add_photo(msg, chat, caption):
    p = msg["photo"][-1]
    info = tg("getFile", file_id=p["file_id"])["result"]
    img = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}", timeout=60).content
    name = f"{int(time.time() * 1000)}.jpg"
    gh_put(f"photos/{name}", img, "Ajout d'une photo")
    pid = name[:-4]
    with LOCK:
        data, sha = load()
        data["photos"].insert(0, {"id": pid, "image": name, "prix": caption, "o": enc(OWNER, pid)})
        save(data, sha)
    if caption:
        say(chat, f"✅ Photo ajoutée ({caption}). Visible dans 1 à 2 minutes.")
    else:
        say(chat, "✅ Photo ajoutée, mais sans prix. Écris /prix 1 120 € pour en mettre un.")


def handle(msg):
    chat = msg["chat"]["id"]
    user = msg.get("from", {}).get("id")
    private = msg["chat"]["type"] == "private"
    is_owner = user == OWNER
    text = (msg.get("text") or msg.get("caption") or "").strip()

    if is_owner and private and msg.get("photo"):
        add_photo(msg, chat, text)
        return

    if not text.startswith("/"):
        return
    parts = text.split(None, 1)
    cmd = parts[0][1:].split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("start", "menu", "radar"):
        send_menu(chat, private)
        if cmd == "start" and is_owner and private:
            say(chat, AIDE)
        return

    if not (is_owner and private):
        return

    if cmd in ("aide", "help"):
        say(chat, AIDE)
    elif cmd == "liste":
        data, _ = load()
        if not data["photos"]:
            say(chat, "Aucune photo pour le moment.")
        else:
            say(chat, "\n".join(f"{i + 1}. {p.get('prix') or '(sans prix)'}"
                                for i, p in enumerate(data["photos"])))
    elif cmd == "supprimer":
        data, sha = load()
        try:
            n = int(arg) - 1
            p = data["photos"].pop(n)
        except Exception:
            say(chat, "Écris par exemple : /supprimer 2")
            return
        save(data, sha)
        gh_del(f"photos/{p['image']}", "Suppression d'une photo")
        say(chat, "🗑️ Photo supprimée.")
    elif cmd == "prix":
        bits = arg.split(None, 1)
        data, sha = load()
        try:
            n = int(bits[0]) - 1
            data["photos"][n]["prix"] = bits[1] if len(bits) > 1 else ""
        except Exception:
            say(chat, "Écris par exemple : /prix 2 150 €")
            return
        save(data, sha)
        say(chat, "✅ Prix changé.")
    elif cmd == "vider":
        if arg.lower() != "oui":
            say(chat, "Pour tout enlever, écris : /vider oui")
            return
        data, sha = load()
        old = data["photos"]
        data["photos"] = []
        save(data, sha)
        for p in old:
            gh_del(f"photos/{p['image']}", "Suppression d'une photo")
        say(chat, "🗑️ Tout est enlevé.")


def verify(init):
    try:
        d = dict(parse_qsl(init, keep_blank_values=True))
        h = d.pop("hash")
        chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), h):
            return None
        if time.time() - int(d.get("auth_date", "0")) > 86400:
            return None
        return json.loads(d["user"])
    except Exception:
        return None


def _ks(pid):
    return int.from_bytes(hashlib.sha256((TOKEN + pid).encode()).digest()[:8], "big")


def enc(uid, pid):
    return base64.urlsafe_b64encode((uid ^ _ks(pid)).to_bytes(8, "big")).decode()


def dec(tok, pid):
    return int.from_bytes(base64.urlsafe_b64decode(tok), "big") ^ _ks(pid)


def load_roles():
    return private_load()["roles"]


def save_roles():
    private_save()

def role(user, code=None):
    if user["id"] == OWNER:
        return "owner"
    return load_roles().get(str(user["id"]))


def owner_of(p):
    try:
        return dec(p["o"], p["id"])
    except Exception:
        return OWNER


def process_pending_requests():
    actions = []
    with LOCK:
        data, _ = chats_load()
        changed = False
        now = int(time.time())
        for c in data.get("chats", []):
            if c.get("status") != "pending" or not c.get("request_deadline"):
                continue
            if now >= int(c["request_deadline"]):
                c["status"] = "expired"
                c.setdefault("messages", []).append({"from": 0, "name": "RADAR PAYMENT",
                    "text": "⏱️ Délai de réponse de 50 secondes dépassé. La discussion reste ouverte.", "time": now})
                actions.append(("expired", c.get("client"), c.get("vendor"), c.get("price")))
                changed = True
            elif now >= int(c.get("remind_at", 0)) and not c.get("reminded"):
                c["reminded"] = True
                actions.append(("remind", c.get("vendor"), None, c.get("price")))
                changed = True
        if changed:
            chats_save(data, None)
    for kind, first, second, price in actions:
        try:
            if kind == "remind":
                say(first, f"⏱️ Plus que 20 secondes pour répondre à la demande ({price or 'sans prix'}) !")
            else:
                say(first, "⏱️ Le délai de 50 secondes est dépassé. Tu peux poursuivre la discussion dans RADAR PAYMENT.")
                say(second, "⌛ La demande a expiré. Le Lanceur peut encore t'écrire dans la discussion.")
        except Exception as e:
            print("notification délai:", repr(e))


def handle_cb(cb):
    kind, _, rid = (cb.get("data") or "").partition(":")
    with LOCK:
        data, _ = chats_load()
        c = next((x for x in data.get("chats", []) if x.get("id") == rid), None)
        if (not c or c.get("status") != "pending" or
                cb.get("from", {}).get("id") != c.get("vendor") or
                time.time() > int(c.get("request_deadline", 0))):
            if c and c.get("status") == "pending" and time.time() > int(c.get("request_deadline", 0)):
                c["status"] = "expired"
                chats_save(data, None)
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Délai dépassé ou demande déjà traitée.")
            return
        if kind not in ("y", "n"):
            tg("answerCallbackQuery", callback_query_id=cb["id"], text="Action inconnue.")
            return
        c["status"] = "vendor_confirmed" if kind == "y" else "declined"
        c.setdefault("messages", []).append({"from": 0, "name": "RADAR PAYMENT",
            "text": "✅ Le Spitta confirme la disponibilité." if kind == "y" else "❌ Le Spitta indique que le produit n'est plus disponible.",
            "time": int(time.time())})
        chats_save(data, None)
    tg("answerCallbackQuery", callback_query_id=cb["id"],
       text="Disponibilité confirmée." if kind == "y" else "Indisponibilité envoyée.")
    m = cb.get("message") or {}
    if kind == "y" and SITE:
        target = {"text": "💬 Ouvrir la discussion", "web_app": {"url": SITE + "?chat=" + rid}}
        tg("editMessageReplyMarkup", chat_id=m.get("chat", {}).get("id"), message_id=m.get("message_id"),
           reply_markup={"inline_keyboard": [[target]]})
        say(c["client"], "✅ Le Spitta a confirmé la disponibilité. Ouvre RADAR PAYMENT et appuie sur « Accepter la vente » pour lancer les 2 minutes.")
    else:
        tg("editMessageReplyMarkup", chat_id=m.get("chat", {}).get("id"), message_id=m.get("message_id"),
           reply_markup={"inline_keyboard": []})
        if kind == "n":
            say(c["client"], "❌ Le Spitta indique que ce produit n'est plus disponible. Tu peux refaire une demande si tu le souhaites.")


def api(path, b):
    user = verify(b.get("initData", ""))
    if not user:
        return {"error": "Ouvre l'app depuis Telegram."}
    uid = user["id"]
    if path == "/api/role":
        if uid == OWNER:
            return {"ok": True, "role": "owner"}
        chosen = str(b.get("role", ""))
        if chosen not in ("lanceur", "spitta"):
            return {"error": "Choisis Lanceur ou Spitta."}
        with LOCK:
            roles = load_roles()
            existing = roles.get(str(uid))
            if existing and existing != chosen:
                return {"error": "Ton profil est déjà fixé. Contacte l’administrateur pour le modifier."}
            roles[str(uid)] = chosen
            save_roles()
        return {"ok": True, "role": chosen}
    if path == "/api/want":
        if role(user) != "lanceur":
            return {"error": "Seuls les comptes Lanceur peuvent contacter un spitta. Choisis le rôle Lanceur."}
        product_id = str(b.get("id", ""))
        data, _ = load()
        p = next((x for x in data["photos"] if x.get("id") == product_id), None)
        if not p:
            return {"error": "Produit introuvable."}
        vendor = owner_of(p)
        prix = p.get("prix") or "sans prix"
        if user.get("username"):
            who, url = "@" + user["username"], "https://t.me/" + user["username"]
        else:
            who, url = user.get("first_name", "Un client"), f"tg://user?id={uid}"
        now = int(time.time())
        with LOCK:
            data_ch, _ = chats_load()
            previous = next((c for c in reversed(data_ch.get("chats", []))
                if c.get("client") == uid and c.get("product") == product_id and
                c.get("status") in ("pending", "notification_failed", "vendor_confirmed", "proof_pending", "proof_received") and
                now - int(c.get("created", now)) < CHAT_TTL), None)
            if previous and previous.get("status") != "notification_failed":
                return {"ok": True, "chat": previous["id"], "notification": True, "existing": True}
            if previous:
                chat = previous
                rid = chat["id"]
                chat.update({"status": "pending", "request_deadline": now + 50,
                             "remind_at": now + 30, "reminded": False})
            else:
                rid = f"{uid}-{int(time.time() * 1000)}"
                chat = {"id": rid, "client": uid, "vendor": vendor, "product": product_id,
                        "price": prix, "client_name": who, "client_url": url, "created": now,
                        "status": "pending", "request_deadline": now + 50, "remind_at": now + 30,
                        "reminded": False, "messages": []}
                data_ch.setdefault("chats", []).append(chat)
            chats_save(data_ch, None)
        try:
            markup = {"inline_keyboard": [
                [{"text": "💬 Ouvrir la discussion", "web_app": {"url": SITE + "?chat=" + rid}}] if SITE else [{"text": "💬 Parler au client", "url": url}],
                [{"text": "✅ Je confirme", "callback_data": "y:" + rid},
                 {"text": "❌ Pas disponible", "callback_data": "n:" + rid}]]}
            say(vendor, f"🚨🚨 NOUVELLE DEMANDE 🚨🚨\nProduit : {prix}.\n"
                        f"Réponse attendue sous 50 secondes.\nClient : {who}", reply_markup=markup)
        except Exception as e:
            print("notification spitta impossible:", repr(e))
            chat_update(rid, status="notification_failed")
            return {"ok": True, "chat": rid, "notification": False,
                    "error": "Demande enregistrée, mais Telegram n'a pas prévenu le Spitta. Demande réessayable."}
        return {"ok": True, "chat": rid, "notification": True}
    r = role(user)
    if path == "/api/me":
        data, _ = load()
        mine = [p["id"] for p in data["photos"] if p.get("id") and
                (r == "owner" or (r == "spitta" and owner_of(p) == uid))]
        return {"role": r, "mine": mine}
    if not r and path not in ("/api/chat/list", "/api/chat/messages", "/api/chat/send", "/api/chat/accept", "/api/chat/proof"):
        return {"role": None, "error": "Choisis d’abord ton rôle sur l’écran de bienvenue."}
    if path == "/api/chat/list":
        chats_data, _ = chats_load()
        if r == "owner":
            visible = chats_data.get("chats", [])
        else:
            visible = [c for c in chats_data.get("chats", []) if uid in (c.get("client"), c.get("vendor"))]
        return {"ok": True, "chats": [{k: c.get(k) for k in
                ("id", "client", "vendor", "product", "price", "client_name", "created", "status")}
                | {"last": (c.get("messages") or [{}])[-1].get("text", "")}
                for c in sorted(visible, key=lambda x: x.get("created", 0), reverse=True)]}
    if path == "/api/chat/messages":
        rid = str(b.get("id", ""))
        chats_data, _ = chats_load()
        c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
        if not c or (uid not in (c.get("client"), c.get("vendor")) and r != "owner"):
            return {"error": "Discussion introuvable ou accès refusé."}
        if c.get("status") == "proof_pending" and time.time() > c.get("proof_deadline", 0):
            c["status"] = "proof_expired"
            c.setdefault("messages", []).append({"from": 0, "name": "RADAR PAYMENT", "text": "⏰ Délai de 2 minutes dépassé. La vente est bloquée.", "time": int(time.time())})
            chats_save(chats_data, None)
        public_chat = {k: c.get(k) for k in
                ("id", "client", "vendor", "price", "client_name", "status", "proof_deadline", "messages")}
        for msg in public_chat.get("messages") or []:
            proof_file = msg.get("proof_file")
            if proof_file:
                try:
                    if proof_file not in PROOF_CACHE:
                        encrypted = base64.b64decode(gh_get(proof_file).get("content", ""))
                        PROOF_CACHE[proof_file] = base64.b64encode(FERNET.decrypt(encrypted)).decode()
                    msg["image"] = "data:image/jpeg;base64," + PROOF_CACHE[proof_file]
                except Exception as e:
                    print("lecture capture chiffrée:", repr(e))
        return {"ok": True, "chat": public_chat}
    if path == "/api/chat/accept":
        rid = str(b.get("id", ""))
        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if not c or uid != c.get("client"):
                return {"error": "Seul le lanceur peut accepter cette vente."}
            if c.get("status") != "vendor_confirmed":
                return {"error": "Cette vente n'attend pas ton acceptation."}
            c["status"] = "proof_pending"
            c["proof_deadline"] = int(time.time()) + 120
            chats_save(chats_data, None)
        try:
            say(c["vendor"], "⏱️ Le lanceur a accepté la vente. Tu as 2 minutes pour envoyer la capture d’écran dans la discussion RADAR PAYMENT.")
        except Exception as e:
            print("notification délai preuve impossible:", repr(e))
        return {"ok": True}
    if path == "/api/chat/proof":
        rid, image = str(b.get("id", "")), str(b.get("image", ""))
        if not image.startswith("data:image/") or len(image) > 4_200_000:
            return {"error": "Capture invalide ou trop lourde. Réessaie avec une image plus légère."}
        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if not c or uid != c.get("vendor"):
                return {"error": "Seul le spitta peut envoyer la capture."}
            if c.get("status") != "proof_pending" or time.time() > c.get("proof_deadline", 0):
                if c.get("status") == "proof_pending":
                    c["status"] = "proof_expired"
                chats_save(chats_data, None)
                return {"error": "Le délai de 2 minutes est terminé. La vente est bloquée."}
            try:
                raw_image = base64.b64decode(image.split(",", 1)[1])
                if len(raw_image) > 650_000:
                    return {"error": "Capture trop lourde après compression. Réessaie avec une image plus légère."}
                proof_file = "private_proofs/" + rid + ".enc"
                gh_put(proof_file, FERNET.encrypt(raw_image), "Sauvegarde chiffrée d'une capture")
                PROOF_CACHE[proof_file] = base64.b64encode(raw_image).decode()
            except Exception as e:
                print("sauvegarde capture chiffrée:", repr(e))
                return {"error": "Impossible de sauvegarder la capture. Réessaie."}
            c["status"] = "proof_received"
            c.setdefault("messages", []).append({"from": uid, "name": user.get("first_name", "Spitta"),
                "text": "📸 Capture d’écran envoyée — preuve reçue.", "proof_file": proof_file, "time": int(time.time())})
            chats_save(chats_data, None)
        try:
            say(c["client"], "📸 Le spitta a envoyé sa capture. La discussion est maintenant ouverte.")
        except Exception as e:
            print("notification capture impossible:", repr(e))
        return {"ok": True}
    if path == "/api/chat/send":
        rid, msg = str(b.get("id", "")), str(b.get("text", "")).strip()[:1500]
        if not msg:
            return {"error": "Écris un message."}
        with LOCK:
            chats_data, sha_ch = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if not c or uid not in (c.get("client"), c.get("vendor")):
                return {"error": "Discussion introuvable ou accès refusé."}
            if c.get("status") not in ("proof_received", "expired"):
                return {"error": "La discussion est verrouillée jusqu’à la capture du spitta ou à l’expiration du délai de réponse."}
            c.setdefault("messages", []).append({"from": uid, "name": user.get("first_name", "Utilisateur"),
                                                  "text": msg, "time": int(time.time())})
            chats_save(chats_data, sha_ch)
        other = c.get("vendor") if uid == c.get("client") else c.get("client")
        def notify():
            try:
                say(other, "💬 Nouveau message dans RADAR PAYMENT. Ouvre la mini-app pour répondre.")
            except Exception as e:
                print("alerte nouveau message non envoyée:", repr(e))
        threading.Thread(target=notify, daemon=True).start()
        return {"ok": True}
    if path in ("/api/add", "/api/update", "/api/delete") and r not in ("owner", "spitta"):
        return {"error": "Accès réservé."}
    if path == "/api/add":
        raw = base64.b64decode(b["image"].split(",", 1)[1])
        if len(raw) > 3_000_000:
            return {"error": "Photo trop lourde."}
        pid = str(int(time.time() * 1000))
        with LOCK:
            gh_put(f"photos/{pid}.jpg", raw, "Ajout d'une photo")
            data, sha = load()
            data["photos"].insert(0, {"id": pid, "image": pid + ".jpg", "prix": str(b.get("prix", ""))[:40], "pays": str(b.get("pays", ""))[:30], "o": enc(uid, pid)})
            save(data, sha)
        return {"ok": True, "photo": {"id": pid, "image": pid + ".jpg", "prix": str(b.get("prix", ""))[:40], "pays": str(b.get("pays", ""))[:30]}}
    if path == "/api/update":
        with LOCK:
            data, sha = load()
            p = next((x for x in data["photos"] if x.get("id") == b.get("id")), None)
            if not p or not (r == "owner" or (r == "spitta" and owner_of(p) == uid)):
                return {"error": "Impossible."}
            p["pays"] = str(b.get("pays", ""))[:30]
            p["prix"] = str(b.get("prix", p.get("prix", "")))[:40]
            save(data, sha)
        return {"ok": True}
    if path == "/api/delete":
        with LOCK:
            data, sha = load()
            p = next((x for x in data["photos"] if x.get("id") == b.get("id")), None)
            if not p or not (r == "owner" or (r == "spitta" and owner_of(p) == uid)):
                return {"error": "Impossible."}
            data["photos"].remove(p)
            save(data, sha)
            gh_del(f"photos/{p['image']}", "Suppression d'une photo")
        return {"ok": True}
    return {"error": "?"}


class Ping(BaseHTTPRequestHandler):
    def _s(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        for k, v in (("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*"),
                     ("Access-Control-Allow-Headers", "Content-Type"), ("Access-Control-Allow-Methods", "POST, GET, OPTIONS")):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._s({"ok": True})

    def do_OPTIONS(self):
        self._s({})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 6_000_000:
                return self._s({"error": "Trop gros."})
            self._s(api(self.path, json.loads(self.rfile.read(n) or b"{}")))
        except Exception as e:
            print("api:", e)
            self._s({"error": "Erreur, réessaie."})

    def log_message(self, *a):
        pass


def serve():
    port = int(os.environ.get("PORT", "10000"))
    ThreadingHTTPServer(("0.0.0.0", port), Ping).serve_forever()


def startup():
    # GITHUB_REPO doit pointer vers le dépôt de la mini-app (RADARPAY),
    # car c'est là que se trouvent photos.json et le dossier photos/.
    print(f"RADAR PAYMENT config: repo={REPO}, branch={BRANCH}, api_url={'définie' if API_URL else 'ABSENTE'}, site_url={'définie' if SITE else 'ABSENTE'}")
    try:
        if SITE:
            tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Ouvrir", "web_app": {"url": SITE}})
        if not API_URL:
            print("ERREUR CONFIG: API_BASE_URL/RENDER_EXTERNAL_URL est vide; l'Admin ne pourra pas joindre l'API.")
            return
        data, sha = load()
        if data.get("api") != API_URL:
            data["api"] = API_URL
            save(data, sha)
            print("API publiée dans photos.json.")
        else:
            print("API déjà correctement publiée dans photos.json.")
    except Exception as e:
        print("ERREUR startup/GitHub:", repr(e))


if __name__ == "__main__":
    threading.Thread(target=serve, daemon=True).start()
    threading.Thread(target=cleanup_chats, daemon=True).start()
    def request_scheduler():
        while True:
            try:
                process_pending_requests()
            except Exception as e:
                print("scheduler demandes:", repr(e))
            time.sleep(2)
    threading.Thread(target=request_scheduler, daemon=True).start()
    startup()
    offset = 0
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=45).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                if "callback_query" in u:
                    try:
                        handle_cb(u["callback_query"])
                    except Exception as e:
                        print("cb:", e)
                if "message" in u:
                    try:
                        handle(u["message"])
                    except Exception as e:
                        print("erreur:", e)
                        try:
                            say(u["message"]["chat"]["id"], "⚠️ Une erreur est survenue. Réessaie.")
                        except Exception:
                            pass
        except Exception as e:
            print("réseau:", e)
            time.sleep(5)
