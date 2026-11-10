import os, time, json, base64, hashlib, hmac, threading, requests, re, secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from cryptography.fernet import Fernet, InvalidToken
from urllib.parse import parse_qsl
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])
GH_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPO"]            # exemple : monnom/radar-site
BRANCH = os.environ.get("GITHUB_BRANCH", "main")
SITE = (os.environ.get("SITE_URL") or os.environ.get("SITE") or "").strip().rstrip("/")
LINK = os.environ.get("MINIAPP_LINK") or SITE
# URL publique du Web Service Render. API_BASE_URL permet une valeur explicite
# si RENDER_EXTERNAL_URL n'est pas disponible.
API_URL = (os.environ.get("API_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).strip().rstrip("/")
LOCK = threading.Lock()
ACTIVE_PRESENCE = {}
FAILS = {}
RATE_LOCK = threading.Lock()
RATE_LIMITS = {}
RATE_WINDOW = 60
RATE_MAX = 60
# Seuls les liens définis dans le catalogue de la mini-app peuvent être enregistrés.
CATALOG_SITES = {
    "Cartes cadeaux ON": "https://www.on.com/en-es/products/digital-gift-card-907644gc",
    "Prada": "https://www.prada.com",
    "Farfetch": "https://www.farfetch.com",
    "Zeitauktion": "https://www.zeitauktion.com/fr",
    "Watchdeluxe.it": "https://www.watchdeluxe.it",
    "Chrono24": "https://www.chrono24.fr",
    "Paysafe Card": "https://www.paysafecard.com",
}


API = f"https://api.telegram.org/bot{TOKEN}"
DATA_ENCRYPTION_KEY = os.environ.get("DATA_ENCRYPTION_KEY", "").strip()
LEGACY_FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256((TOKEN + "|RADARPAY_PRIVATE_V1").encode()).digest()))
FERNET = (Fernet(base64.urlsafe_b64encode(hashlib.sha256(DATA_ENCRYPTION_KEY.encode()).digest()))
          if DATA_ENCRYPTION_KEY else LEGACY_FERNET)
PROOF_CACHE = {}
VOICE_CACHE = {}
ATTACHMENT_CACHE = {}
GH = f"https://api.github.com/repos/{REPO}/contents"
H = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]

AIDE = (
    "Panneau admin RADAR PAYMENT 💡\n\n"
    "Les TARGETS sont publiées par les comptes SPITTA depuis la mini-app.\n"
    "/liste : voir les publications et les prix\n"
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


PROCESS_STARTED_AT = int(time.time())


def monitor_event(kind, title, detail="", level="info", notify=False, lock_held=False):
    now = int(time.time())
    event = {"id": secrets.token_hex(8), "time": now, "kind": str(kind)[:40],
             "title": str(title)[:120], "detail": str(detail)[:240],
             "level": level if level in ("info", "warning", "critical") else "info"}
    try:
        def persist_event():
            state = private_load()
            events = state.setdefault("monitor_events", [])
            events.append(event)
            del events[:-500]
            private_save()
        if lock_held:
            persist_event()
        else:
            with LOCK:
                persist_event()
    except Exception as e:
        print("journal supervision:", repr(e))
    if notify:
        try:
            stamp = datetime.fromtimestamp(now, ZoneInfo("Europe/Brussels")).strftime("%d/%m/%Y %H:%M:%S")
            symbol = "🚨" if event["level"] == "critical" else "⚠️" if event["level"] == "warning" else "🔔"
            say(OWNER, symbol + " RADARPAY — " + event["title"] + "\n" + event["detail"] + "\n" + stamp + "\nOuvre Administration > Supervision.")
        except Exception as e:
            print("alerte supervision Telegram:", repr(e))


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
        response = requests.delete(f"{GH}/{path}", headers=H, timeout=30,
                                   json={"message": msg, "sha": sha, "branch": BRANCH})
        if response.status_code == 404:
            return True
        response.raise_for_status()
        return True
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return True
        print("suppression GitHub échouée:", path, repr(e))
        return False
    except Exception as e:
        print("suppression GitHub échouée:", path, repr(e))
        return False


def load():
    f = gh_get("photos.json")
    raw = base64.b64decode(f["content"]).decode() or "{}"
    data = json.loads(raw)
    data.setdefault("photos", [])
    return data, f["sha"]


def save(data, sha):
    stamp = datetime.now(ZoneInfo("Europe/Brussels"))
    data["maj"] = f"{stamp.day} {MOIS[stamp.month - 1]} {stamp.year} à {stamp:%H:%M}"
    gh_put("photos.json", json.dumps(data, ensure_ascii=False, indent=2).encode(),
           "Mise à jour des photos", sha)


CHAT_TTL = 24 * 60 * 60  # Conserve les discussions 24 h pour couvrir EROP + ERAF et laisser le temps de répondre.
EROP_BLACKLIST_SECONDS = 7 * 24 * 60 * 60
ERAF_BLACKLIST_SECONDS = 10 * 24 * 60 * 60
PRIVATE_CACHE = None
STORAGE_READY = False


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_POOL = None
DB_INIT_LOCK = threading.Lock()


def ensure_db():
    """Pool PostgreSQL partagé; l'état privé reste chiffré."""
    global DB_POOL
    if not DATABASE_URL:
        return None
    with DB_INIT_LOCK:
        if DB_POOL is None:
            from psycopg_pool import ConnectionPool
            pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=8, timeout=10)
            try:
                with pool.connection() as conn:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS radarpay_private_state (
                            id SMALLINT PRIMARY KEY CHECK (id = 1),
                            payload BYTEA NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                    """)
            except Exception:
                pool.close()
                raise
            DB_POOL = pool
    return DB_POOL


def _decode_private(encrypted):
    migrated = False
    try:
        decrypted = FERNET.decrypt(encrypted)
    except InvalidToken as primary_error:
        if not DATA_ENCRYPTION_KEY:
            raise RuntimeError("Le fichier privé chiffré est illisible; aucune donnée n'a été écrasée.") from primary_error
        try:
            decrypted = LEGACY_FERNET.decrypt(encrypted)
            migrated = True
        except InvalidToken as legacy_error:
            raise RuntimeError("Clé de chiffrement invalide; aucune donnée n'a été écrasée.") from legacy_error
    try:
        data = json.loads(decrypted.decode())
    except (ValueError, UnicodeDecodeError) as e:
        raise RuntimeError("Le fichier privé déchiffré est invalide; aucune donnée n'a été écrasée.") from e
    data.setdefault("roles", {})
    data.setdefault("chats", [])
    data.setdefault("admin_chats", {})
    data.setdefault("site_clicks", {})
    data.setdefault("payment_reports", [])
    data.setdefault("monitor_events", [])
    return data, migrated


def private_load():
    global PRIVATE_CACHE, STORAGE_READY
    if PRIVATE_CACHE is not None:
        PRIVATE_CACHE.setdefault("blacklist", {})
        PRIVATE_CACHE.setdefault("registrations", [])
        PRIVATE_CACHE.setdefault("monitor_events", [])
        return PRIVATE_CACHE

    pool = ensure_db()
    if pool is not None:
        with pool.connection() as conn:
            row = conn.execute("SELECT payload FROM radarpay_private_state WHERE id = 1").fetchone()
        if row:
            PRIVATE_CACHE, migrated = _decode_private(bytes(row[0]))
            if migrated:
                private_save()
            else:
                STORAGE_READY = True
            return PRIVATE_CACHE

    # Première initialisation PostgreSQL : importer l'état GitHub historique sans le supprimer.
    try:
        f = gh_get("radar_private.enc")
    except requests.HTTPError as e:
        if not e.response or e.response.status_code != 404:
            raise
        PRIVATE_CACHE = {"roles": {}, "chats": []}
        try:
            old = gh_get("user_roles.json")
            PRIVATE_CACHE["roles"] = json.loads(base64.b64decode(old.get("content", "")).decode() or "{}")
        except requests.HTTPError as old_error:
            if not old_error.response or old_error.response.status_code != 404:
                PRIVATE_CACHE = None
                raise
        private_save()
        if pool is None:
            gh_del("user_roles.json", "Migration vers les données privées chiffrées")
        return PRIVATE_CACHE

    encrypted = base64.b64decode(f.get("content", ""))
    PRIVATE_CACHE, migrated = _decode_private(encrypted)
    if pool is not None or migrated:
        private_save()
    else:
        STORAGE_READY = True
    return PRIVATE_CACHE


def private_save():
    global STORAGE_READY
    data = private_load()
    encrypted = FERNET.encrypt(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
    pool = ensure_db()
    if pool is not None:
        with pool.connection() as conn:
            conn.execute("""
                INSERT INTO radarpay_private_state (id, payload, updated_at)
                VALUES (1, %s, NOW())
                ON CONFLICT (id) DO UPDATE
                SET payload = EXCLUDED.payload, updated_at = NOW()
            """, (encrypted,))
        STORAGE_READY = True
        return

    # Compatibilité si DATABASE_URL n'est pas encore configuré.
    try:
        sha = gh_get("radar_private.enc").get("sha")
    except requests.HTTPError as e:
        if not e.response or e.response.status_code != 404:
            raise
        sha = None
    gh_put("radar_private.enc", encrypted, "Sauvegarde chiffrée RADAR PAYMENT", sha)
    STORAGE_READY = True

def parse_target_amount(value):
    """Retourne (frais à 50 %, devise) si le prix est interprétable sans supposition."""
    raw = str(value or "").strip()
    if not raw or len(raw) > 40:
        return None, None
    currency = None
    for token, code in (("EUR", "EUR"), ("USD", "USD"), ("GBP", "GBP"), ("CHF", "CHF"),
                        ("€", "EUR"), ("$", "USD"), ("£", "GBP")):
        found = token in raw.upper() if token.isalpha() else token in raw
        if found:
            currency = code
            raw = re.sub(re.escape(token), "", raw, flags=re.I).strip()
            break
    if re.search(r"[A-Za-z]", raw):
        return None, None
    raw = raw.replace(" ", "").replace("\u00a0", "").replace("'", "").replace("’", "")
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", raw):
        return None, None
    if "," in raw and "." in raw:
        decimal_sep = "," if raw.rfind(",") > raw.rfind(".") else "."
        grouping_sep = "." if decimal_sep == "," else ","
        raw = raw.replace(grouping_sep, "").replace(decimal_sep, ".")
    elif "," in raw or "." in raw:
        sep = "," if "," in raw else "."
        chunks = raw.split(sep)
        if len(chunks) > 2:
            if not all(len(part) == 3 for part in chunks[1:]):
                return None, None
            raw = "".join(chunks)
        elif len(chunks[1]) == 3:
            raw = "".join(chunks)
        elif len(chunks[1]) in (1, 2):
            raw = chunks[0] + "." + chunks[1]
        else:
            return None, None
    try:
        amount = Decimal(raw)
        if not amount.is_finite() or amount <= 0:
            return None, None
        fee = (amount * Decimal("0.50")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return str(fee), currency
    except (InvalidOperation, ValueError):
        return None, None


def record_guarantee_fee(chat, blacklist_entry, now):
    amount, currency = parse_target_amount(chat.get("price"))
    chat["guarantee_fee_due"] = True
    chat["guarantee_fee_rate"] = 50
    chat["guarantee_fee_amount"] = amount
    chat["guarantee_fee_currency"] = currency
    blacklist_entry.update({
        "guarantee_fee_due": True, "guarantee_fee_rate": 50,
        "guarantee_fee_amount": amount, "guarantee_fee_currency": currency,
        "created": now
    })
    return amount, currency


def payment_report_followup_valid(followup, started_at):
    return bool(started_at and followup.get("status") in ("awaiting", "overdue")
                and followup.get("started_at") == started_at)


def payment_report_is_late(followup, now):
    return int(now) >= int(followup.get("deadline", 0))


def eraf_validation_pending(chat):
    return bool(chat.get("erop_photo_sent") and not chat.get("erop_confirmed"))


def expire_erop_sender_step(chat, now):
    """Clôture l'étape EROP du CALLER sans bloquer la discussion ni sanctionner le SPITTA."""
    if (chat.get("status") != "erop_pending" or chat.get("erop_sender_proof_submitted")
            or int(chat.get("erop_sender_deadline", 0)) <= 0
            or now < int(chat.get("erop_sender_deadline", 0))):
        return False
    chat["status"] = chat.get("erop_previous_status") or "proof_received"
    chat.pop("erop_previous_status", None)
    chat.pop("erop_sender_deadline", None)
    chat.setdefault("messages", []).append({"from": 0, "name": "RADAR PAYMENT",
        "text": "⛔ Délai de 2 minutes dépassé : le CALLER n’a pas envoyé sa preuve EROP ni enregistré son indisponibilité. Cette demande EROP est clôturée sans sanction pour le SPITTA. La discussion est de nouveau ouverte ; le CALLER peut relancer EROP si nécessaire.",
        "time": now})
    return True


def chat_last_activity(chat):
    messages = chat.get("messages") or []
    last_message = messages[-1] if messages else {}
    return int(last_message.get("time") or chat.get("updated") or chat.get("created", 0))


def _delete_expired_chat_media(files):
    for private_file, cache in files:
        gh_del(private_file, "Suppression d'un média arrivé à expiration")
        cache.pop(private_file, None)


def chats_load():
    data = private_load()
    now = int(time.time())
    old_chats = data.get("chats", [])
    data["chats"] = [c for c in old_chats
                     if now - chat_last_activity(c) < CHAT_TTL]
    if len(data["chats"]) != len(old_chats):
        expired_files = []
        for expired in old_chats:
            if expired not in data["chats"]:
                for msg in expired.get("messages", []):
                    for file_key, cache in (("proof_file", PROOF_CACHE), ("voice_file", VOICE_CACHE), ("attachment_file", ATTACHMENT_CACHE)):
                        private_file = msg.get(file_key)
                        if private_file:
                            expired_files.append((private_file, cache))
                            cache.pop(private_file, None)
        private_save()
        if expired_files:
            # GitHub peut répondre lentement : ne jamais supprimer les médias sous le verrou global.
            threading.Thread(target=_delete_expired_chat_media, args=(expired_files,), daemon=True).start()
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
        say(chat, "Seuls les comptes SPITTA peuvent publier une TARGET depuis la mini-app. Tu peux toujours gérer les publications existantes dans l’administration.")
        return

    if not text.startswith("/"):
        return
    parts = text.split(None, 1)
    cmd = parts[0][1:].split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("start", "menu", "radar"):
        send_menu(chat, private)
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
        if not isinstance(init, str) or not init or len(init) > 12000:
            return None
        pairs = parse_qsl(init, keep_blank_values=True, strict_parsing=True)
        if len({k for k, _ in pairs}) != len(pairs):
            return None
        d = dict(pairs)
        h = d.pop("hash", "")
        if not re.fullmatch(r"[0-9a-f]{64}", h):
            return None
        chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(key, chk.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, h):
            return None
        auth_date = int(d.get("auth_date", "0"))
        now = int(time.time())
        if auth_date <= 0 or auth_date > now + 30 or now - auth_date > 3600:
            return None
        user = json.loads(d["user"])
        if not isinstance(user, dict) or not isinstance(user.get("id"), int) or user["id"] <= 0:
            return None
        return user
    except Exception:
        return None


def rate_allowed(uid, path):
    now = time.time()
    key = (int(uid), str(path))
    limits = {"/api/chat/messages": 35, "/api/chat/list": 20,
              "/api/adminchat/messages": 35, "/api/adminchat/list": 20}
    maximum = limits.get(path, RATE_MAX)
    with RATE_LOCK:
        hits = [t for t in RATE_LIMITS.get(key, []) if now - t < RATE_WINDOW]
        if len(hits) >= maximum:
            RATE_LIMITS[key] = hits
            return False
        hits.append(now)
        RATE_LIMITS[key] = hits
        if len(RATE_LIMITS) > 5000:
            for old_key in list(RATE_LIMITS)[:1000]:
                if not RATE_LIMITS[old_key] or now - RATE_LIMITS[old_key][-1] >= RATE_WINDOW:
                    RATE_LIMITS.pop(old_key, None)
        return True


def decode_image_data_uri(value, max_bytes=650000):
    if not isinstance(value, str) or len(value) > 4200000:
        raise ValueError("Capture invalide ou trop lourde.")
    match = re.fullmatch(r"data:image/(jpeg|jpg|png|gif|webp);base64,([A-Za-z0-9+/=]+)", value, re.I)
    if not match:
        raise ValueError("Format d’image invalide.")
    raw = base64.b64decode(match.group(2), validate=True)
    if not raw or len(raw) > max_bytes:
        raise ValueError("Image vide ou trop lourde.")
    valid = (raw.startswith(b"\xff\xd8\xff") or
             raw.startswith(b"\x89PNG\r\n\x1a\n") or
             raw.startswith((b"GIF87a", b"GIF89a")) or
             (len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"))
    if not valid:
        raise ValueError("Le fichier reçu n’est pas une image valide.")
    return raw


def decode_jpeg_data_uri(value, max_bytes=650000):
    raw = decode_image_data_uri(value, max_bytes=max_bytes)
    if not raw.startswith(b"\xff\xd8\xff"):
        raise ValueError("La photo doit être préparée au format JPEG.")
    return raw



def decode_audio_data_uri(value, expected_mime, max_bytes=650000):
    if not isinstance(value, str) or len(value) > 900000:
        raise ValueError("Vocal invalide ou trop lourd.")
    match = re.fullmatch(
        r"data:(audio/(?:webm|ogg|mp4|mpeg|aac|3gpp))(?:;[A-Za-z0-9._=+-]+)*;base64,([A-Za-z0-9+/=]+)",
        value, re.I
    )
    if not match or match.group(1).lower() != expected_mime:
        raise ValueError("Type audio incohérent.")
    raw = base64.b64decode(match.group(2), validate=True)
    if not raw or len(raw) > max_bytes:
        raise ValueError("Vocal vide ou trop lourd.")
    starts_mp4 = len(raw) >= 12 and raw[4:8] == b"ftyp"
    starts_mpeg = raw.startswith(b"ID3") or (
        len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0
    )
    valid = {
        "audio/webm": raw.startswith(b"\x1a\x45\xdf\xa3"),
        "audio/ogg": raw.startswith(b"OggS"),
        "audio/mp4": starts_mp4,
        "audio/mpeg": starts_mpeg,
        "audio/aac": len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xF6) == 0xF0,
        "audio/3gpp": starts_mp4 or raw.startswith(b"#!AMR"),
    }.get(expected_mime, False)
    if not valid:
        raise ValueError("Le fichier reçu ne correspond pas à un format audio reconnu.")
    return raw

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

def is_blacklisted(user_id):
    with LOCK:
        blacklist = private_load().setdefault("blacklist", {})
        entry = blacklist.get(str(user_id))
        if not entry:
            return False
        if int(time.time()) >= int(entry.get("until", 0)):
            blacklist.pop(str(user_id), None)
            private_save()
            return False
        return True


def role(user, code=None):
    if user["id"] == OWNER:
        return "owner"
    if is_blacklisted(user["id"]):
        return None
    with LOCK:
        return private_load().setdefault("roles", {}).get(str(user["id"]))

PIN_ITERATIONS = 310000
APP_SESSION_SECONDS = 12 * 60 * 60

def _pin_digest(pin, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), bytes.fromhex(salt_hex), PIN_ITERATIONS).hex()

def _new_app_session(profile):
    token = secrets.token_urlsafe(32)
    profile["session_hash"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
    profile["session_expires"] = int(time.time()) + APP_SESSION_SECONDS
    return token

def _has_valid_app_session(profile, token):
    if not profile or not token or int(profile.get("session_expires", 0)) <= int(time.time()):
        return False
    supplied = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
    return hmac.compare_digest(str(profile.get("session_hash", "")), supplied)


def owner_of(p):
    try:
        if not p.get("o") or not p.get("id"):
            return None
        owner = dec(p["o"], p["id"])
        return owner if owner > 0 else None
    except Exception:
        return None


def process_pending_requests():
    actions = []
    monitor_actions = []
    with LOCK:
        data, _ = chats_load()
        changed = False
        now = int(time.time())
        for c in data.get("chats", []):
            if expire_erop_sender_step(c, now):
                changed = True
            if c.get("status") == "proof_pending" and int(c.get("proof_deadline", 0)) > 0 and now >= int(c.get("proof_deadline", 0)):
                c["status"] = "proof_expired"
                c.setdefault("messages", []).append({"from": 0, "name": "RADAR PAYMENT",
                    "text": "⏰ Délai de 2 minutes dépassé. La vente est bloquée.", "time": now})
                actions.append(("proof_expired", c.get("client"), c.get("vendor")))
                changed = True
            if c.get("status") == "erop_pending" and int(c.get("erop_deadline", 0)) > 0 and now >= int(c.get("erop_deadline", 0)):
                c["status"] = "erop_expired"
                monitor_actions.append(("erop", c.get("id"), c.get("vendor"), c.get("price")))
                vendor_id = str(c.get("vendor"))
                blacklist = private_load().setdefault("blacklist", {})
                blacklist[vendor_id] = {
                    "until": now + EROP_BLACKLIST_SECONDS,
                    "reason": "Délai EROP de 3 min 30 non respecté",
                    "chat_id": c.get("id")
                }
                record_guarantee_fee(c, blacklist[vendor_id], now)
                c["erop_blacklist_until"] = now + EROP_BLACKLIST_SECONDS
                c.setdefault("messages", []).append({"from": 0, "name": "RADAR PAYMENT",
                    "text": "⛔ Délai EROP de 3 min 30 dépassé. Discussion bloquée. Le SPITTA est inscrit sur la liste noire pendant 7 jours. Les frais de garantie, fixés à 50 % du montant de la Target concernée, sont signalés comme dus ; leur règlement doit être confirmé par l’administration.", "time": now})
                changed = True
            if c.get("status") == "erop_expired" and (
                    not c.get("erop_client_notified") or not c.get("erop_vendor_notified")):
                actions.append(("erop_expired", c.get("id"), c.get("client"), c.get("vendor"),
                                c.get("price"), bool(c.get("erop_client_notified")),
                                bool(c.get("erop_vendor_notified"))))
            if (c.get("erop_photo_sent") and not c.get("erop_confirmed")
                    and c.get("eraf_deadline") and now >= int(c.get("eraf_deadline", 0))
                    and c.get("status") != "eraf_expired"):
                c["status"] = "eraf_expired"
                monitor_actions.append(("eraf", c.get("id"), c.get("vendor"), c.get("price")))
                vendor_id = str(c.get("vendor"))
                blacklist = private_load().setdefault("blacklist", {})
                blacklist[vendor_id] = {
                    "until": now + ERAF_BLACKLIST_SECONDS,
                    "reason": "Délai ERAF de 10 minutes non respecté",
                    "chat_id": c.get("id")
                }
                record_guarantee_fee(c, blacklist[vendor_id], now)
                c["eraf_blacklist_until"] = now + ERAF_BLACKLIST_SECONDS
                c["erop_photo_sent"] = False
                c.setdefault("messages", []).append({"from": 0, "name": "RADAR PAYMENT",
                    "text": "⛔ Délai ERAF de 10 minutes dépassé. Discussion bloquée. Le SPITTA est inscrit sur la liste noire pendant 10 jours. Les frais de garantie, fixés à 50 % du montant de la Target concernée, sont signalés comme dus ; leur règlement doit être confirmé par l’administration.", "time": now})
                changed = True
            if c.get("status") == "eraf_expired" and (
                    not c.get("eraf_client_notified") or not c.get("eraf_vendor_notified")):
                actions.append(("eraf_expired", c.get("id"), c.get("client"), c.get("vendor"),
                                bool(c.get("eraf_client_notified")), bool(c.get("eraf_vendor_notified"))))
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
    for item in monitor_actions:
        stage, rid, vendor_id, price = item
        if stage == "erop":
            monitor_event("security_deadline", "Délai EROP dépassé — intervention automatique", "Discussion : " + str(rid) + " · SPITTA : " + str(vendor_id) + " · TARGET : " + str(price), "critical", True)
        else:
            monitor_event("security_deadline", "Délai ERAF dépassé — intervention automatique", "Discussion : " + str(rid) + " · SPITTA : " + str(vendor_id), "critical", True)
    for action in actions:
        kind = action[0]
        if kind == "erop_expired":
            _, rid, client_id, vendor_id, price, client_done, vendor_done = action
            for field, recipient, message, already_sent in (
                ("erop_client_notified", client_id,
                 "⛔ Le délai EROP de 3 min 30 est dépassé. La discussion est bloquée. Le SPITTA est blacklisté pendant 7 jours et les frais de garantie (50 % du montant de la Target concernée) sont signalés comme dus.",
                 client_done),
                ("erop_vendor_notified", vendor_id,
                 "⛔ Tu n'as pas envoyé la deuxième capture dans les 3 min 30. La discussion est bloquée. Ton compte est blacklisté pendant 7 jours. Les frais de garantie (50 % du montant de la Target concernée) sont dus : contacte l’administration pour confirmer le montant et le règlement.",
                 vendor_done),
            ):
                if already_sent:
                    continue
                try:
                    say(recipient, message)
                except Exception as e:
                    print("notification EROP non envoyée; nouvel essai prévu:", repr(e))
                    continue
                try:
                    with LOCK:
                        chats_data, _ = chats_load()
                        chat = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
                        if chat and chat.get("status") == "erop_expired":
                            chat[field] = True
                            chats_save(chats_data, None)
                except Exception as e:
                    print("confirmation notification EROP non sauvegardée:", repr(e))
            continue
        if kind == "eraf_expired":
            _, rid, client_id, vendor_id, client_done, vendor_done = action
            for field, recipient, message, already_sent in (
                ("eraf_client_notified", client_id,
                 "⛔ Le délai ERAF de 10 minutes est dépassé. La discussion est bloquée. Le SPITTA est blacklisté pendant 10 jours et les frais de garantie (50 % du montant de la Target concernée) sont signalés comme dus.",
                 client_done),
                ("eraf_vendor_notified", vendor_id,
                 "⛔ Tu n'as pas validé ERAF dans les 10 minutes après ta deuxième capture. La discussion est bloquée. Ton compte est blacklisté pendant 10 jours. Les frais de garantie (50 % du montant de la Target concernée) sont dus : contacte l’administration pour confirmer le montant et le règlement.",
                 vendor_done),
            ):
                if already_sent:
                    continue
                try:
                    say(recipient, message)
                except Exception as e:
                    print("notification ERAF non envoyée; nouvel essai prévu:", repr(e))
                    continue
                try:
                    with LOCK:
                        chats_data, _ = chats_load()
                        chat = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
                        if chat and chat.get("status") == "eraf_expired":
                            chat[field] = True
                            chats_save(chats_data, None)
                except Exception as e:
                    print("confirmation notification ERAF non sauvegardée:", repr(e))
            continue
        if kind == "proof_expired":
            _, client_id, vendor_id = action
            try:
                say(client_id, "⏰ Le délai de 2 minutes pour la capture est dépassé. La vente est bloquée.")
                say(vendor_id, "⏰ Le délai de 2 minutes pour envoyer la capture est dépassé. La vente est bloquée.")
            except Exception as e:
                print("notification expiration capture:", repr(e))
            continue
        try:
            if kind == "remind":
                say(action[1], f"⏱️ Plus que 20 secondes pour répondre à la demande ({action[3] or 'sans prix'}) !")
            else:
                say(action[1], "⏱️ Le délai de 50 secondes est dépassé. Tu peux poursuivre la discussion dans RADAR PAYMENT.")
                say(action[2], "⌛ La demande a expiré. Le CALLER peut encore t'écrire dans la discussion.")
        except Exception as e:
            print("notification délai:", repr(e))


def handle_cb(cb):
    kind, _, rid = (cb.get("data") or "").partition(":")
    rejection = None
    with LOCK:
        data, _ = chats_load()
        c = next((x for x in data.get("chats", []) if x.get("id") == rid), None)
        if (not c or c.get("status") != "pending" or
                cb.get("from", {}).get("id") != c.get("vendor") or
                time.time() > int(c.get("request_deadline", 0))):
            if c and c.get("status") == "pending" and time.time() > int(c.get("request_deadline", 0)):
                c["status"] = "expired"
                chats_save(data, None)
            rejection = "Délai dépassé ou demande déjà traitée."
        elif kind not in ("y", "n"):
            rejection = "Action inconnue."
        else:
            c["status"] = "vendor_confirmed" if kind == "y" else "declined"
            c.setdefault("messages", []).append({"from": 0, "name": "RADAR PAYMENT",
                "text": "✅ Le SPITTA confirme la disponibilité." if kind == "y" else "❌ Le SPITTA indique que le produit n'est plus disponible.",
                "time": int(time.time())})
            chats_save(data, None)
    if rejection:
        tg("answerCallbackQuery", callback_query_id=cb["id"], text=rejection)
        return
    tg("answerCallbackQuery", callback_query_id=cb["id"],
       text="Disponibilité confirmée." if kind == "y" else "Indisponibilité envoyée.")
    m = cb.get("message") or {}
    if kind == "y":
        if SITE:
            target = {"text": "💬 Ouvrir la discussion", "web_app": {"url": SITE + "?chat=" + rid}}
        else:
            target = {"text": "💬 Contacter le CALLER", "url": c.get("client_url") or "https://t.me/"}
        tg("editMessageReplyMarkup", chat_id=m.get("chat", {}).get("id"), message_id=m.get("message_id"),
           reply_markup={"inline_keyboard": [[target]]})
        say(c["client"], "✅ Le SPITTA a confirmé la disponibilité. Ouvre RADAR PAYMENT et appuie sur « Accepter la vente » pour lancer les 2 minutes.")
    else:
        tg("editMessageReplyMarkup", chat_id=m.get("chat", {}).get("id"), message_id=m.get("message_id"),
           reply_markup={"inline_keyboard": []})
        say(c["client"], "❌ Le SPITTA indique que le produit n'est plus disponible. Tu peux refaire une demande si tu le souhaites.")


def api(path, b):
    user = verify(b.get("initData", ""))
    if not user:
        return {"error": "Ouvre l'app depuis Telegram."}
    uid = user["id"]
    if uid != OWNER and path != "/api/role" and is_blacklisted(uid):
        entry = private_load().get("blacklist", {}).get(str(uid), {})
        remaining = max(0, int(entry.get("until", 0)) - int(time.time()))
        return {"error": "Compte temporairement blacklisté jusqu’au " +
                time.strftime("%d/%m/%Y à %H:%M", time.localtime(entry.get("until", 0))) +
                ". Motif : délai de sécurité non respecté. Les frais de garantie, fixés à 50 % du montant de la Target concernée, sont signalés comme dus. " +
                "Contacte l’administration. Temps restant : " + str(remaining // 86400) + " jour(s)."}
    if not rate_allowed(uid, path):
        return {"error": "Trop de requêtes. Attends quelques secondes puis réessaie."}
    if path == "/api/role":
        if uid == OWNER:
            return {"ok": True, "role": "owner"}
        return {"error": "Mise à jour requise. Utilise Inscription ou Se connecter."}
    if path == "/api/register":
        if uid == OWNER:
            return {"ok": True, "role": "owner"}
        if is_blacklisted(uid):
            return {"error": "Ton compte est temporairement blacklisté. Contacte l’administration."}
        chosen = str(b.get("role", ""))
        pseudo = re.sub(r"\s+", " ", str(b.get("pseudo", "")).strip())
        pin = str(b.get("pin", ""))
        guarantee_acceptance = str(b.get("guarantee_acceptance", "")).strip()
        if guarantee_acceptance != "Garant":
            return {"error": "Lis le règlement puis écris exactement Garant pour l’accepter."}
        if chosen not in ("lanceur", "spitta"):
            return {"error": "Choisis CALLER ou SPITTA."}
        if not pseudo or len(pseudo) < 3 or len(pseudo) > 24 or not re.fullmatch(r"[\w .@-]+", pseudo, re.UNICODE):
            return {"error": "Choisis un pseudo de 3 à 24 caractères (lettres, chiffres, espaces, . _ @ -)." }
        if not re.fullmatch(r"\d{4}", pin):
            return {"error": "Le code secret doit contenir exactement 4 chiffres."}
        with LOCK:
            state = private_load()
            uid_key = str(uid)
            existing_role = state.setdefault("roles", {}).get(uid_key)
            registrations = state.setdefault("registrations", [])
            profile = next((x for x in registrations if str(x.get("user_id")) == uid_key), None)
            if existing_role:
                return {"error": "Ce compte existe déjà. Utilise Se connecter."}
            if profile:
                if profile.get("status", "pending") == "pending":
                    if profile.get("role") != chosen:
                        return {"error": "Ta demande est déjà enregistrée pour un autre profil. Contacte l’administrateur."}
                    profile["guarantee_rules_version"] = "2026-11-09"
                    profile["guarantee_accepted"] = True
                    profile["guarantee_acceptance_word"] = "Garant"
                    profile["guarantee_accepted_at"] = int(time.time())
                    private_save()
                    return {"ok": True, "pending": True}
                return {"error": "Ce compte existe déjà. Utilise Se connecter."}
            salt = secrets.token_hex(16)
            registrations.append({
                "user_id": uid_key, "pseudo": pseudo, "role": chosen,
                "telegram_username": str(user.get("username") or ""),
                "first_name": str(user.get("first_name") or ""),
                "pin_salt": salt, "pin_hash": _pin_digest(pin, salt),
                "created": int(time.time()), "status": "pending",
                "guarantee_rules_version": "2026-11-09",
                "guarantee_accepted": True,
                "guarantee_acceptance_word": "Garant",
                "guarantee_accepted_at": int(time.time())
            })
            registrations[:] = registrations[-1000:]
            private_save()
        monitor_event("registration", "Nouvelle inscription en attente", "Profil : " + chosen + " · Pseudo : " + pseudo, "info", True)
        return {"ok": True, "pending": True}
    if path == "/api/login":
        if uid == OWNER:
            return {"ok": True, "role": "owner"}
        pin = str(b.get("pin", ""))
        if not re.fullmatch(r"\d{4}", pin):
            return {"error": "Entre ton code secret à 4 chiffres."}
        with LOCK:
            state = private_load()
            uid_key = str(uid)
            profile = next((x for x in state.setdefault("registrations", []) if str(x.get("user_id")) == uid_key), None)
            existing_role = state.setdefault("roles", {}).get(uid_key)
            if not existing_role:
                if profile and profile.get("status", "pending") == "pending":
                    return {"pending": True, "error": "Ton inscription attend encore la validation de l’administrateur."}
                return {"error": "Aucun compte trouvé. Choisis Inscription."}
            if not profile or not profile.get("pin_hash") or not profile.get("pin_salt"):
                return {"needs_pin": True, "role": existing_role}
            now = int(time.time())
            if int(profile.get("pin_locked_until", 0)) > now:
                wait = int(profile["pin_locked_until"]) - now
                return {"error": "Trop de codes incorrects. Réessaie dans " + str(max(1, (wait + 59) // 60)) + " minute(s)." }
            if not hmac.compare_digest(str(profile.get("pin_hash", "")), _pin_digest(pin, profile["pin_salt"])):
                failures = [int(x) for x in profile.get("pin_failures", []) if now - int(x) < 900]
                failures.append(now)
                profile["pin_failures"] = failures[-5:]
                if len(failures) >= 5:
                    profile["pin_locked_until"] = now + 900
                    profile["pin_failures"] = []
                    monitor_event("security_login", "Compte verrouillé après 5 codes PIN incorrects", "Compte Telegram : " + uid_key, "critical", False, lock_held=True)
                private_save()
                return {"error": "Code secret incorrect."}
            profile["pin_failures"] = []
            profile.pop("pin_locked_until", None)
            token = _new_app_session(profile)
            private_save()
            return {"ok": True, "role": existing_role, "session_token": token}
    if path == "/api/pin/setup":
        if uid == OWNER:
            return {"ok": True, "role": "owner"}
        existing_role = role(user)
        if existing_role not in ("lanceur", "spitta"):
            return {"error": "Ton compte doit être approuvé avant de créer un code secret."}
        pin = str(b.get("pin", ""))
        if not re.fullmatch(r"\d{4}", pin):
            return {"error": "Le code secret doit contenir exactement 4 chiffres."}
        with LOCK:
            state = private_load()
            registrations = state.setdefault("registrations", [])
            profile = next((x for x in registrations if str(x.get("user_id")) == str(uid)), None)
            if profile and profile.get("pin_hash"):
                return {"error": "Un code secret existe déjà. Utilise Se connecter."}
            if not profile:
                profile = {"user_id": str(uid), "pseudo": str(user.get("first_name") or "Utilisateur"),
                           "role": existing_role, "created": int(time.time()), "status": "approved"}
                registrations.append(profile)
            salt = secrets.token_hex(16)
            profile["pin_salt"] = salt
            profile["pin_hash"] = _pin_digest(pin, salt)
            token = _new_app_session(profile)
            private_save()
        return {"ok": True, "role": existing_role, "session_token": token}
    if path == "/api/admin/access":
        if uid != OWNER:
            return {"error": "Ce compte Telegram n’est pas reconnu comme propriétaire. Vérifie OWNER_ID dans le service Render de l’API RADARPAY."}
        configured_code = os.environ.get("ADMIN_ACCESS_CODE", "").strip().strip(chr(34)).strip(chr(39)).strip()
        supplied_code = str(b.get("code", "")).strip()
        if not configured_code:
            return {"error": "ADMIN_ACCESS_CODE est absent dans Render. Ajoute cette variable puis redémarre le service."}
        if not re.fullmatch(r"\d{6}", configured_code):
            return {"error": "La variable ADMIN_ACCESS_CODE dans Render doit contenir exactement 6 chiffres, sans espace ni guillemets."}
        if not re.fullmatch(r"\d{6}", supplied_code) or not hmac.compare_digest(supplied_code, configured_code):
            return {"error": "Code incorrect. Vérifie la valeur de ADMIN_ACCESS_CODE dans le service Render qui héberge l’API RADARPAY."}
        return {"ok": True, "role": "owner"}

    # Toutes les routes de l’application exigent une session ouverte par PIN.
    # Le propriétaire Telegram est reconnu directement par OWNER_ID.
    if uid != OWNER and path not in ("/api/me", "/api/register", "/api/login", "/api/pin/setup"):
        state = private_load()
        profile = next((x for x in state.setdefault("registrations", []) if str(x.get("user_id")) == str(uid)), None)
        current_role = role(user)
        if current_role not in ("lanceur", "spitta"):
            if profile and profile.get("status", "pending") == "pending":
                return {"error": "Ton inscription est en attente de validation par l’administrateur."}
            return {"error": "Inscris-toi ou connecte-toi avec ton compte RADARPAY."}
        if not _has_valid_app_session(profile, b.get("sessionToken", "")):
            return {"error": "Session fermée. Clique sur Se connecter et entre ton code secret à 4 chiffres."}
    if path == "/api/want":
        if role(user) != "lanceur":
            return {"error": "Seuls les comptes CALLER peuvent contacter un SPITTA. Choisis le rôle CALLER."}
        product_id = str(b.get("id", ""))
        data, _ = load()
        p = next((x for x in data["photos"] if x.get("id") == product_id), None)
        if not p:
            return {"error": "Produit introuvable."}
        vendor = owner_of(p)
        if not vendor:
            return {"error": "Ce produit n’a pas de propriétaire valide. Contacte l’administrateur."}
        if int(vendor) == int(uid):
            return {"error": "Tu ne peux pas faire une offre sur ta propre publication. Modifie plutôt son prix depuis ton espace Mes TARGETS."}
        prix = p.get("prix") or "sans prix"
        client_offer = str(b.get("client_offer", "")).strip()[:40]
        if not client_offer:
            return {"error": "Indique ta proposition de prix avant de contacter le SPITTA."}
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
                if previous.get("client_offer") != client_offer:
                    previous["client_offer"] = client_offer
                    chats_save(data_ch, None)
                return {"ok": True, "chat": previous["id"], "notification": True, "existing": True}
            if previous:
                chat = previous
                rid = chat["id"]
                chat.update({"status": "pending", "client_offer": client_offer, "request_deadline": now + 50,
                             "remind_at": now + 30, "reminded": False})
            else:
                rid = f"{uid}-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
                chat = {"id": rid, "client": uid, "vendor": vendor, "product": product_id,
                        "price": prix, "client_offer": client_offer, "client_name": who, "client_url": url, "created": now,
                        "status": "pending", "request_deadline": now + 50, "remind_at": now + 30,
                        "reminded": False, "messages": []}
                data_ch.setdefault("chats", []).append(chat)
            chats_save(data_ch, None)
        monitor_event("offer", "Nouvelle offre envoyée", "Produit : " + prix + " · Proposition : " + client_offer, "warning", False)
        try:
            markup = {"inline_keyboard": [
                [{"text": "💬 Ouvrir la discussion", "web_app": {"url": SITE + "?chat=" + rid}}] if SITE else [{"text": "💬 Parler au client", "url": url}],
                [{"text": "✅ Je confirme", "callback_data": "y:" + rid},
                 {"text": "❌ Pas disponible", "callback_data": "n:" + rid}]]}
            say(vendor, f"🚨🚨 NOUVELLE DEMANDE 🚨🚨\nProduit : {prix}.\nProposition du CALLER : {client_offer}.\n"
                        f"Réponse attendue sous 50 secondes.\nClient : {who}", reply_markup=markup)
        except Exception as e:
            print("notification SPITTA impossible:", repr(e))
            chat_update(rid, status="notification_failed")
            return {"ok": True, "chat": rid, "notification": False,
                    "error": "Demande enregistrée, mais Telegram n'a pas prévenu le SPITTA. Demande réessayable."}
        return {"ok": True, "chat": rid, "notification": True}
    r = role(user)
    if path == "/api/catalog/click":
        if r != "lanceur":
            return {"error": "Réservé aux comptes CALLER."}
        site = str(b.get("site", "")).strip()[:100]
        url = str(b.get("url", "")).strip()[:500]
        if not site or CATALOG_SITES.get(site) != url:
            return {"error": "Site invalide ou absent du catalogue autorisé."}
        now = int(time.time())
        with LOCK:
            pdata = private_load()
            pdata.setdefault("site_clicks", {})[str(uid)] = {"site": site, "url": url, "time": now}
            thread = pdata.setdefault("admin_chats", {}).setdefault(str(uid), [])
            thread.append({"from": 0, "name": "RADAR PAYMENT", "text": "🌐 Site ouvert par le CALLER : " + site + "\n" + url, "time": now})
            private_save()
        notified = True
        try:
            who = user.get("username") and ("@" + user["username"]) or user.get("first_name", "CALLER")
            say(OWNER, "🌐 CLIC SUR UN SITE — RADAR PAYMENT\\nCALLER : " + who + " (ID " + str(uid) + ")\\nSite : " + site + "\\nLien : " + url + "\\nHeure : " + datetime.fromtimestamp(now, ZoneInfo("Europe/Brussels")).strftime("%d/%m/%Y à %H:%M:%S") + "\\nUne discussion privée a été ouverte dans l’espace Admin.")
        except Exception as e:
            notified = False
            print("notification clic site admin:", repr(e))
        monitor_event("catalog_click", "Ouverture d’un LINK partenaire", "Site : " + site + " · Compte Telegram : " + str(uid), "info", False)
        return {"ok": True, "notification": notified}
    if path == "/api/adminchat/list":
        if r != "owner":
            return {"error": "Accès réservé à l’administrateur."}
        with LOCK:
            roles = dict(load_roles())
            pdata = private_load()
            items = []
            for account_id, account_role in roles.items():
                if account_role in ("lanceur", "spitta"):
                    msgs = list(pdata.setdefault("admin_chats", {}).get(str(account_id), []))
                    last = msgs[-1].get("text", "") if msgs else "Aucun message"
                    profile = next((x for x in pdata.setdefault("registrations", []) if str(x.get("user_id")) == str(account_id)), {})
                    label = profile.get("pseudo") or profile.get("first_name") or ("SPITTA" if account_role == "spitta" else "CALLER")
                    items.append({"id": str(account_id), "role": account_role, "label": str(label)[:40],
                                  "last": last, "time": msgs[-1].get("time", 0) if msgs else 0})
        return {"ok": True, "launchers": sorted([x for x in items if x["role"] == "lanceur"], key=lambda x: x["time"], reverse=True),
                "spittas": sorted([x for x in items if x["role"] == "spitta"], key=lambda x: x["time"], reverse=True)}
    if path == "/api/adminchat/messages":
        target = str(b.get("id", uid))
        if r != "owner" and (r not in ("lanceur", "spitta") or target != str(uid)):
            return {"error": "Accès refusé."}
        with LOCK:
            pdata = private_load()
            msgs = [dict(m) for m in pdata.setdefault("admin_chats", {}).get(target, [])]
        return {"ok": True, "chat": {"id": target, "client": int(target), "vendor": OWNER, "status": "proof_received", "messages": msgs}}
    if path == "/api/adminchat/file":
        target = str(b.get("id", uid))
        file_key = str(b.get("file", ""))
        if r != "owner" and (r not in ("lanceur", "spitta") or target != str(uid)):
            return {"error": "Accès refusé."}
        with LOCK:
            thread = private_load().setdefault("admin_chats", {}).get(target, [])
            msg = next((item for item in thread if item.get("proof_file") == file_key), None)
            if not msg or not file_key.startswith("private_payment_proofs/" + target + "/"):
                return {"error": "Justificatif introuvable ou accès refusé."}
        try:
            if file_key not in PROOF_CACHE:
                encrypted = base64.b64decode(gh_get(file_key).get("content", ""))
                PROOF_CACHE[file_key] = base64.b64encode(FERNET.decrypt(encrypted)).decode()
            return {"ok": True, "data": "data:image/jpeg;base64," + PROOF_CACHE[file_key],
                    "name": "Justificatif de paiement", "mime": "image/jpeg"}
        except Exception as e:
            print("lecture preuve paiement:", repr(e))
            return {"error": "Impossible de charger ce justificatif."}
    if path == "/api/adminchat/send":
        target = str(b.get("id", uid))
        msg = str(b.get("text", "")).strip()[:1500]
        if r != "owner" and (r not in ("lanceur", "spitta") or target != str(uid)):
            return {"error": "Accès refusé."}
        if not msg:
            return {"error": "Écris un message."}
        with LOCK:
            pdata = private_load()
            thread = pdata.setdefault("admin_chats", {}).setdefault(target, [])
            thread.append({"from": uid, "name": user.get("first_name", "Utilisateur"), "text": msg, "time": int(time.time())})
            private_save()
        other = int(target) if r == "owner" else OWNER
        try:
            say(other, "💬 Nouveau message dans ton chat RADAR PAYMENT avec l’administrateur. Ouvre la mini-app.")
        except Exception as e:
            print("notification chat admin:", repr(e))
        with LOCK:
            pdata = private_load()
            admin_messages = [dict(item) for item in pdata.setdefault("admin_chats", {}).get(target, [])]
        return {"ok": True, "chat": {"id": target, "client": int(target), "vendor": OWNER,
                                      "status": "proof_received", "messages": admin_messages}}
    if path == "/api/payment/status":
        if r != "lanceur":
            return {"error": "Réservé aux comptes CALLER."}
        now = int(time.time())
        with LOCK:
            pdata = private_load()
            click = pdata.setdefault("site_clicks", {}).get(str(uid))
            if not click:
                return {"ok": True, "active": False}
            followup = click.get("followup") or {}
            if b.get("start") and not followup.get("started_at") and followup.get("status") not in ("submitted", "decided"):
                followup = {"started_at": now, "deadline": now + 180, "status": "awaiting"}
                click["followup"] = followup
                pdata["site_clicks"][str(uid)] = click
                thread = pdata.setdefault("admin_chats", {}).setdefault(str(uid), [])
                thread.append({"from": 0, "name": "RADAR PAYMENT",
                    "text": "⏱️ Retour du CALLER sur RADARPAY : réponse attendue sous 3 minutes pour le site " + str(click.get("site", "")) + ".",
                    "time": now})
                private_save()
            if followup.get("status") == "awaiting" and now >= int(followup.get("deadline", 0)):
                followup["status"] = "overdue"
                followup["overdue_at"] = now
                pdata["site_clicks"][str(uid)] = click
                pdata.setdefault("payment_reports", []).append({
                    "id": secrets.token_hex(8), "launcher": uid, "site": click.get("site", ""),
                    "url": click.get("url", ""), "time": now, "status": "overdue",
                    "reason": "", "article": "", "amount": "", "currency": ""
                })
                private_save()
                monitor_event("payment_deadline", "Délai de réponse LINK dépassé",
                    "Site : " + str(click.get("site", "")) + " · CALLER : " + str(uid), "warning", False, lock_held=True)
            return {"ok": True, "active": True, "site": click.get("site"),
                    "deadline": followup.get("deadline", 0), "status": followup.get("status", "not_started"),
                    "decision": followup.get("decision", ""), "decision_label": followup.get("decision_label", "")}
    if path == "/api/payment/not-done":
        if r != "lanceur":
            return {"error": "Réservé aux comptes CALLER."}
        reason = re.sub(r"\s+", " ", str(b.get("reason", "")).strip())[:1000]
        if len(reason) < 12:
            return {"error": "Explique la raison en au moins 12 caractères."}
        now = int(time.time())
        with LOCK:
            pdata = private_load()
            click = pdata.setdefault("site_clicks", {}).get(str(uid))
            if not click or not click.get("followup", {}).get("started_at"):
                return {"error": "Aucun retour de site n’attend ta réponse."}
            followup = click["followup"]
            if followup.get("status") not in ("awaiting", "overdue"):
                return {"error": "Cette réponse a déjà été enregistrée."}
            late = now >= int(followup.get("deadline", 0))
            reports = pdata.setdefault("payment_reports", [])
            report = next((x for x in reversed(reports)
                           if x.get("launcher") == uid and x.get("site") == click.get("site")
                           and x.get("status") == "overdue"), None)
            if report:
                report.update({"status": "reason_pending", "reason": reason, "late": True, "time": now})
            else:
                report = {"id": secrets.token_hex(8), "launcher": uid, "site": click.get("site", ""),
                    "url": click.get("url", ""), "time": now, "status": "reason_pending",
                    "reason": reason, "article": "", "amount": "", "currency": "", "late": late}
                reports.append(report)
            followup.update({"status": "submitted", "answer": "not_paid", "report_id": report["id"], "submitted_at": now})
            thread = pdata.setdefault("admin_chats", {}).setdefault(str(uid), [])
            thread.append({"from": uid, "name": user.get("first_name", "CALLER"),
                "text": "❌ PAIEMENT NON EFFECTUÉ — Site : " + str(click.get("site", "")) +
                        "\\nRaison : " + reason + ("\\nRéponse hors délai." if late else ""),
                "time": now})
            private_save()
        monitor_event("payment_not_done", "Paiement non effectué — raison à examiner",
            "Site : " + str(click.get("site", "")) + " · CALLER : " + str(uid) + (" · Hors délai" if late else ""),
            "warning", False)
        try:
            say(OWNER, "❌ PAIEMENT NON EFFECTUÉ — raison à examiner\\nCALLER : " + str(uid) +
                "\\nSite : " + str(click.get("site", "")) + "\\nRaison : " + reason +
                ("\\nRéponse hors délai." if late else "") + "\\nOuvre Administration > Suivi des paiements.")
        except Exception as e:
            print("notification raison paiement:", repr(e))
        return {"ok": True, "late": late}
    if path == "/api/admin/payment-reports":
        if uid != OWNER or r != "owner":
            return {"error": "Accès réservé à l’administrateur."}
        with LOCK:
            pdata = private_load()
            reports = [dict(x) for x in pdata.setdefault("payment_reports", [])]
        reports.sort(key=lambda x: int(x.get("time", 0)), reverse=True)
        reports = reports[:100]
        for report in reports:
            report.pop("proof_file", None)
        return {"ok": True, "reports": reports}
    if path == "/api/admin/payment-file":
        if uid != OWNER or r != "owner":
            return {"error": "Accès réservé à l’administrateur."}
        report_id = str(b.get("id", ""))[:40]
        with LOCK:
            report = next((dict(x) for x in private_load().setdefault("payment_reports", [])
                           if x.get("id") == report_id), None)
        if not report:
            return {"error": "Déclaration introuvable."}
        proof_file = str(report.get("proof_file", ""))
        expected_prefix = "private_payment_proofs/" + str(report.get("launcher", "")) + "/"
        if not proof_file.startswith(expected_prefix):
            return {"error": "Justificatif introuvable ou accès refusé."}
        try:
            if proof_file not in PROOF_CACHE:
                encrypted = base64.b64decode(gh_get(proof_file).get("content", ""))
                PROOF_CACHE[proof_file] = base64.b64encode(FERNET.decrypt(encrypted)).decode()
            return {"ok": True, "data": "data:image/jpeg;base64," + PROOF_CACHE[proof_file],
                    "name": "Justificatif de paiement", "mime": "image/jpeg"}
        except Exception as e:
            print("lecture preuve paiement admin:", repr(e))
            return {"error": "Impossible de charger ce justificatif."}
    if path == "/api/admin/payment-decision":
        if uid != OWNER or r != "owner":
            return {"error": "Accès réservé à l’administrateur."}
        report_id = str(b.get("id", ""))[:40]
        decision = str(b.get("decision", ""))
        if decision not in ("valid", "invalid"):
            return {"error": "Choisis une décision valide."}
        now = int(time.time())
        with LOCK:
            pdata = private_load()
            report = next((x for x in pdata.setdefault("payment_reports", []) if x.get("id") == report_id), None)
            if not report or report.get("status") in ("approved", "rejected"):
                return {"error": "Déclaration introuvable ou déjà traitée."}
            was_payment = report.get("status") == "declared_unverified"
            report["status"] = "approved" if decision == "valid" else "rejected"
            report["decision"] = decision
            report["decided_at"] = now
            decision_label = ("justificatif validé" if decision == "valid" else "justificatif non validé") if was_payment else ("raison valable" if decision == "valid" else "raison non valable")
            launcher = str(report.get("launcher", ""))
            click = pdata.setdefault("site_clicks", {}).get(launcher)
            if click and click.get("followup", {}).get("report_id") == report_id:
                click["followup"].update({"status": "decided", "decision": decision, "decision_label": decision_label, "decided_at": now})
            pdata.setdefault("admin_chats", {}).setdefault(launcher, []).append({
                "from": OWNER, "name": "RADAR PAYMENT",
                "text": "Décision de l’administration : " + decision_label.upper() +
                        " — retour à l’accueil pour effectuer un nouvel achat.",
                "time": now})
            private_save()
        try:
            say(int(launcher), "L’administration a répondu : " + decision_label +
                ". Tu peux retourner à l’accueil de RADARPAY pour effectuer un nouvel achat.")
        except Exception as e:
            print("notification décision paiement:", repr(e))
        return {"ok": True}
    if path == "/api/payment/report":
        if r != "lanceur":
            return {"error": "Seuls les CALLER peuvent déclarer un paiement."}
        amount = str(b.get("amount", "")).strip().replace(",", ".")
        currency = str(b.get("currency", "")).upper().strip()
        article = re.sub(r"\s+", " ", str(b.get("article", "")).strip())[:160]
        site = str(b.get("site", "")).strip()[:100]
        image = str(b.get("image", ""))
        if not re.fullmatch(r"\d{1,9}(?:\.\d{1,2})?", amount) or float(amount) <= 0:
            return {"error": "Indique un montant valide."}
        if currency not in ("EUR", "USD", "GBP", "CHF"):
            return {"error": "Choisis une devise valide."}
        if not article:
            return {"error": "Indique l’article acheté."}
        if not image.startswith("data:image/") or len(image) > 4_200_000:
            return {"error": "La capture d’écran de paiement est obligatoire."}
        try:
            raw_image = decode_jpeg_data_uri(image)
        except Exception as e:
            print("validation preuve paiement:", repr(e))
            return {"error": "Capture de paiement invalide ou trop lourde."}
        now = int(time.time())
        proof_file = "private_payment_proofs/" + str(uid) + "/" + secrets.token_hex(12) + ".enc"
        # Vérifier d'abord l'état, puis envoyer le fichier hors du verrou global.
        with LOCK:
            pdata = private_load()
            click = pdata.setdefault("site_clicks", {}).get(str(uid))
            if not click or click.get("site") != site:
                return {"error": "Sélectionne le site partenaire qui a été ouvert."}
            followup = dict(click.get("followup") or {})
            if not payment_report_followup_valid(followup, followup.get("started_at")):
                return {"error": "Aucune réponse de paiement n’attend actuellement."}
            late = payment_report_is_late(followup, now)
            started_at = followup.get("started_at")
            click_snapshot = dict(click)
        try:
            gh_put(proof_file, FERNET.encrypt(raw_image), "Sauvegarde chiffrée d'une preuve de paiement")
            PROOF_CACHE[proof_file] = base64.b64encode(raw_image).decode()
        except Exception as e:
            print("sauvegarde preuve paiement:", repr(e))
            return {"error": "Impossible de sauvegarder la capture."}
        report_id = secrets.token_hex(8)
        try:
            with LOCK:
                pdata = private_load()
                current_click = pdata.setdefault("site_clicks", {}).get(str(uid))
                current_followup = (current_click or {}).get("followup") or {}
                if (not current_click or current_click.get("site") != site
                        or current_followup.get("status") not in ("awaiting", "overdue")
                        or not payment_report_followup_valid(current_followup, started_at)):
                    raise ValueError("Cette déclaration a déjà été enregistrée ou le suivi a changé.")
                late = late or payment_report_is_late(current_followup, int(time.time()))
                text_report = "💳 PAIEMENT DÉCLARÉ — NON VÉRIFIÉ" + (" — DÉCLARATION TARDIVE" if late else "") + "\nSite : " + click_snapshot["site"] + "\nArticle : " + article + "\nMontant : " + amount + " " + currency + "\nCapture d’écran jointe."
                pdata.setdefault("admin_chats", {}).setdefault(str(uid), []).append({
                    "from": uid, "name": user.get("first_name", "CALLER"), "text": text_report,
                    "proof_file": proof_file, "time": now
                })
                payment_reports = pdata.setdefault("payment_reports", [])
                if late:
                    for previous_report in payment_reports:
                        if (previous_report.get("launcher") == uid
                                and previous_report.get("site") == site
                                and previous_report.get("status") == "overdue"):
                            previous_report["status"] = "superseded"
                            previous_report["resolved_by_report"] = report_id
                payment_reports.append({
                    "id": report_id, "launcher": uid, "site": click_snapshot["site"], "url": click_snapshot["url"],
                    "article": article, "amount": amount, "currency": currency, "proof_file": proof_file,
                    "time": now, "status": "declared_unverified", "late": late
                })
                current_click["followup"] = {
                    "status": "submitted", "answer": "paid", "report_id": report_id,
                    "submitted_at": now, "deadline": current_followup.get("deadline", 0),
                    "started_at": current_followup.get("started_at", 0), "late": late}
                private_save()
        except Exception as e:
            print("enregistrement déclaration paiement:", repr(e))
            gh_del(proof_file, "Nettoyage d'une preuve de paiement non enregistrée")
            PROOF_CACHE.pop(proof_file, None)
            return {"error": str(e) if isinstance(e, ValueError) else "Impossible d’enregistrer la déclaration."}
        monitor_event("payment", "Paiement déclaré — à vérifier" + (" (tardif)" if late else ""), "Site : " + click_snapshot["site"] + " · Article : " + article + " · Montant : " + amount + " " + currency + " · Compte : " + str(uid), "critical", False)
        try:
            say(OWNER, "💳 PAIEMENT DÉCLARÉ (NON VÉRIFIÉ)" + (" — DÉCLARATION TARDIVE" if late else "") + "\nCALLER : " + str(uid) +
                "\nSite : " + click_snapshot["site"] + "\nMontant : " + amount + " " + currency +
                "\nCapture jointe dans le chat admin de la mini-app.")
        except Exception as e:
            print("notification paiement admin:", repr(e))
        return {"ok": True}
    if path == "/api/admin/monitor":
        if uid != OWNER:
            return {"error": "Accès réservé à l’administrateur."}
        with LOCK:
            events = [dict(item) for item in private_load().setdefault("monitor_events", [])]
        events.sort(key=lambda item: int(item.get("time", 0)), reverse=True)
        storage = "PostgreSQL" if DATABASE_URL else "GitHub (lent)"
        service_status = "ok" if DATABASE_URL else "degraded"
        if DATABASE_URL:
            try:
                pool = ensure_db()
                with pool.connection() as conn:
                    conn.execute("SELECT 1").fetchone()
            except Exception as e:
                service_status = "degraded"
                storage = "PostgreSQL indisponible"
                print("contrôle santé PostgreSQL:", repr(e))
        return {"ok": True, "events": events[:200], "server_time": int(time.time()),
                "service": {"status": service_status, "storage": storage,
                            "uptime_seconds": max(0, int(time.time()) - PROCESS_STARTED_AT)},
                "counts": {"total": len(events), "critical": sum(1 for x in events if x.get("level") == "critical"),
                           "warning": sum(1 for x in events if x.get("level") == "warning"),
                           "info": sum(1 for x in events if x.get("level") == "info")}}
    if path == "/api/admin/reset-registrations":
        if uid != OWNER:
            return {"error": "Accès réservé à l’administrateur."}
        configured_code = os.environ.get("ADMIN_ACCESS_CODE", "").strip().strip(chr(34)).strip(chr(39)).strip()
        supplied_code = str(b.get("admin_code", "")).strip()
        if not re.fullmatch(r"\d{6}", configured_code or "") or not re.fullmatch(r"\d{6}", supplied_code):
            return {"error": "Code administrateur invalide ou non configuré."}
        if not hmac.compare_digest(supplied_code, configured_code):
            return {"error": "Code administrateur incorrect."}
        if b.get("confirmation") != "REINITIALISER RADARPAY":
            return {"error": "Confirmation exacte requise."}
        with LOCK:
            state = private_load()
            counts = {
                "registrations": len(state.setdefault("registrations", [])),
                "roles": len(state.setdefault("roles", {})),
                "blacklist": len(state.setdefault("blacklist", {}))
            }
            state["registrations"] = []
            state["roles"] = {}
            state["blacklist"] = {}
            private_save()
        monitor_event("admin_reset", "Réinitialisation des inscriptions",
                      "Profils : " + str(counts["registrations"]) + " · Rôles : " +
                      str(counts["roles"]) + " · Blocages : " + str(counts["blacklist"]),
                      "warning", True)
        return {"ok": True, "deleted": counts}
    if path == "/api/admin/registrations":
        if uid != OWNER:
            return {"error": "Accès réservé à l’administrateur."}
        with LOCK:
            state = private_load()
            roles = dict(state.setdefault("roles", {}))
            source_registrations = [dict(item) for item in state.setdefault("registrations", [])]
        registrations = []
        for item in source_registrations:
            profile = dict(item)
            role_is_approved = roles.get(str(profile.get("user_id"))) == profile.get("role")
            profile["status"] = "approved" if role_is_approved else (profile.get("status") or "pending")
            for secret_key in ("pin_hash", "pin_salt", "session_hash", "session_expires",
                               "pin_failures", "pin_locked_until"):
                profile.pop(secret_key, None)
            registrations.append(profile)
        return {"ok": True, "registrations": sorted(registrations, key=lambda x: x.get("created", 0), reverse=True)}
    if path == "/api/admin/approve-registration":
        if uid != OWNER:
            return {"error": "Accès réservé à l’administrateur."}
        target = str(b.get("user_id", "")).strip()
        with LOCK:
            state = private_load()
            roles = state.setdefault("roles", {})
            profile = next((x for x in state.setdefault("registrations", [])
                            if str(x.get("user_id")) == target), None)
            if not profile:
                return {"error": "Demande introuvable ou déjà traitée."}
            chosen = profile.get("role")
            if chosen not in ("lanceur", "spitta"):
                return {"error": "Rôle de demande invalide."}
            if roles.get(target) == chosen:
                profile["status"] = "approved"
                private_save()
                return {"error": "Cette inscription est déjà approuvée."}
            if profile.get("status", "pending") != "pending":
                return {"error": "Demande introuvable ou déjà traitée."}
            roles[target] = chosen
            profile["status"] = "approved"
            profile["approved_at"] = int(time.time())
            private_save()
        monitor_event("approval", "Compte approuvé", "Rôle : " + chosen + " · Compte Telegram : " + target, "info", True)
        return {"ok": True, "user_id": target, "role": chosen}
    if path == "/api/me":
        data, _ = load()
        mine = [p["id"] for p in data["photos"] if p.get("id") and
                (r == "owner" or (r == "spitta" and owner_of(p) == uid))]
        with LOCK:
            state = private_load()
            profile = next((x for x in state.setdefault("registrations", [])
                            if str(x.get("user_id")) == str(uid)), None)
            if r in ("lanceur", "spitta"):
                changed = False
                if not profile:
                    profile = {"user_id": str(uid), "role": r, "status": "approved",
                               "pseudo": str(user.get("first_name") or "Utilisateur")[:24],
                               "created": int(time.time())}
                    state["registrations"].append(profile)
                    changed = True
                telegram_username = str(user.get("username") or profile.get("telegram_username") or "")
                first_name = str(user.get("first_name") or profile.get("first_name") or "")
                if profile.get("telegram_username") != telegram_username:
                    profile["telegram_username"] = telegram_username
                    changed = True
                if profile.get("first_name") != first_name:
                    profile["first_name"] = first_name
                    changed = True
                profile["last_seen"] = int(time.time())  # Mis à jour en mémoire sans écriture DB à chaque heartbeat.
                if changed:
                    private_save()
                ACTIVE_PRESENCE[str(uid)] = {"role": r, "seen": profile["last_seen"]}
            pending = bool(profile and profile.get("status", "pending") == "pending" and not r)
            has_pin = bool(profile and profile.get("pin_hash"))
            active_cutoff = int(time.time()) - 3600  # Présence conservée 1 h après la dernière activité.
            active_counts = {"caller": 0, "spitta": 0}
            for active_id, presence in list(ACTIVE_PRESENCE.items()):
                if int(presence.get("seen", 0)) < active_cutoff:
                    ACTIVE_PRESENCE.pop(active_id, None)
                    continue
                active_role = presence.get("role")
                if active_role == "lanceur":
                    active_counts["caller"] += 1
                elif active_role == "spitta":
                    active_counts["spitta"] += 1
        return {"role": r, "mine": mine, "pending": pending,
                "has_pin": has_pin, "active_counts": active_counts,
                "telegram_username": str(user.get("username") or ""),
                "first_name": str(user.get("first_name") or "")}
    if path == "/api/blacklist":
        if uid != OWNER:
            return {"error": "Accès réservé à l’administrateur."}
        if not role(user):
            return {"error": "Ton accès à RADARPAY est en attente d’autorisation par l’administrateur."}
        now = int(time.time())
        entries = []
        with LOCK:
            state = private_load()
            blacklist = state.setdefault("blacklist", {})
            registrations = state.setdefault("registrations", [])
            changed = False
            for blocked_id, entry in list(blacklist.items()):
                until = int(entry.get("until", 0))
                if until <= now:
                    blacklist.pop(blocked_id, None)
                    changed = True
                    continue
                # Le pseudo d'inscription est affiché publiquement; aucun identifiant Telegram n'est exposé.
                profile = next((x for x in registrations
                                if str(x.get("user_id")) == str(blocked_id)), {})
                display_name = ((("@" + str(profile.get("telegram_username")).lstrip("@")) if profile.get("telegram_username") else None)
                                or profile.get("pseudo") or profile.get("first_name") or "SPITTA")
                entries.append({
                    "label": str(display_name)[:40],
                    "until": until,
                    "reason": entry.get("reason") or "Non-respect du délai EROP",
                    "guarantee_fee_due": bool(entry.get("guarantee_fee_due")),
                    "guarantee_fee_rate": entry.get("guarantee_fee_rate"),
                    "guarantee_fee_amount": entry.get("guarantee_fee_amount"),
                    "guarantee_fee_currency": entry.get("guarantee_fee_currency")
                })
            if changed:
                private_save()
        entries.sort(key=lambda x: x["until"], reverse=True)
        return {"ok": True, "entries": entries}
    if not r:
        return {"role": None, "error": "Ton accès à RADARPAY est en attente d’autorisation par l’administrateur."}
    if path == "/api/chat/list":
        with LOCK:
            chats_data, _ = chats_load()
            if r == "owner":
                visible = chats_data.get("chats", [])
            else:
                visible = [c for c in chats_data.get("chats", []) if uid in (c.get("client"), c.get("vendor"))]
            result = [{k: c.get(k) for k in
                       ("id", "client", "vendor", "product", "price", "client_offer", "client_name", "created", "status",
                        "guarantee_fee_due", "guarantee_fee_rate", "guarantee_fee_amount", "guarantee_fee_currency")}
                      | {"last": (c.get("messages") or [{}])[-1].get("text", "")}
                      for c in sorted(visible, key=lambda x: x.get("created", 0), reverse=True)]
        return {"ok": True, "chats": result}
    if path == "/api/chat/messages":
        rid = str(b.get("id", ""))
        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if not c or (uid not in (c.get("client"), c.get("vendor")) and r != "owner"):
                return {"error": "Discussion introuvable ou accès refusé."}
            changed = False
            if expire_erop_sender_step(c, int(time.time())):
                changed = True
            if c.get("status") == "erop_pending" and int(c.get("erop_deadline", 0)) > 0 and time.time() >= c.get("erop_deadline", 0):
                now = int(time.time())
                c["status"] = "erop_expired"
                blacklist_entry = {
                    "until": now + EROP_BLACKLIST_SECONDS, "reason": "Délai EROP non respecté",
                    "chat_id": c.get("id")}
                record_guarantee_fee(c, blacklist_entry, now)
                private_load().setdefault("blacklist", {})[str(c.get("vendor"))] = blacklist_entry
                c["erop_blacklist_until"] = now + EROP_BLACKLIST_SECONDS
                c.setdefault("messages", []).append({"from": 0, "name": "RADAR PAYMENT",
                    "text": "⛔ Délai EROP dépassé. Discussion bloquée. Le SPITTA est inscrit sur la liste noire pendant 7 jours. Les frais de garantie, fixés à 50 % du montant de la Target concernée, sont signalés comme dus et doivent être vérifiés par l’administration.", "time": now})
                changed = True
            if c.get("status") == "proof_pending" and time.time() >= c.get("proof_deadline", 0):
                c["status"] = "proof_expired"
                c.setdefault("messages", []).append({"from": 0, "name": "RADAR PAYMENT", "text": "⏰ Délai de 2 minutes dépassé. La vente est bloquée.", "time": int(time.time())})
                changed = True
            # Une lecture est enregistrée uniquement lorsque le destinataire ouvre/actualise ce chat.
            if uid in (c.get("client"), c.get("vendor")):
                read_time = int(time.time())
                for item in (c.get("messages") or []):
                    if item.get("from") not in (uid, 0) and not item.get("read_at"):
                        item["read_at"] = read_time
                        changed = True
            if changed:
                chats_save(chats_data, None)
            c = dict(c)
            c["messages"] = [dict(msg) for msg in (c.get("messages") or [])]
        public_chat = {k: c.get(k) for k in
                ("id", "client", "vendor", "price", "client_name", "status", "proof_deadline", "erop_deadline", "erop_sender_deadline", "erop_photo_sent", "erop_confirmed", "erop_sender_proof_submitted", "erop_sender_proof_unavailable", "eraf_deadline")}
        public_chat["messages"] = c["messages"]
        for msg in public_chat.get("messages") or []:
            proof_file = msg.get("proof_file")
            voice_file = msg.get("voice_file")
            # Les fichiers joints sont chargés à la demande via /api/chat/file,
            # jamais dans chaque réponse de polling du chat.
            msg.pop("attachment_data", None)
            # Les captures sont elles aussi chargées à la demande pour alléger le polling.
            # Les anciens vocaux sont aussi chargés à la demande, jamais à chaque polling.
        return {"ok": True, "chat": public_chat}
    if path == "/api/chat/file":
        rid, file_key = str(b.get("id", "")), str(b.get("file", ""))
        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if not c or (uid not in (c.get("client"), c.get("vendor")) and r != "owner"):
                return {"error": "Discussion introuvable ou accès refusé."}
            msg = next((m for m in (c.get("messages") or [])
                        if m.get("attachment_file") == file_key or m.get("voice_file") == file_key
                        or m.get("proof_file") == file_key), None)
            is_voice = bool(msg and msg.get("voice_file") == file_key)
            is_proof = bool(msg and msg.get("proof_file") == file_key)
            allowed_prefix = ("private_voice/" + rid + "/" if is_voice else
                              "private_proofs/" + rid if is_proof else "private_chat_files/" + rid + "/")
            if not msg or not file_key.startswith(allowed_prefix):
                return {"error": "Fichier introuvable ou accès refusé."}
            mime = (msg.get("mime", "audio/webm") if is_voice else
                    "image/jpeg" if is_proof else msg.get("attachment_mime", "application/octet-stream"))
            name = "Message vocal" if is_voice else "Capture" if is_proof else msg.get("attachment_name", "fichier")
            cache = VOICE_CACHE if is_voice else PROOF_CACHE if is_proof else ATTACHMENT_CACHE
        try:
            if file_key not in cache:
                encrypted = base64.b64decode(gh_get(file_key).get("content", ""))
                cache[file_key] = base64.b64encode(FERNET.decrypt(encrypted)).decode()
            return {"ok": True, "data": "data:" + mime + ";base64," + cache[file_key],
                    "name": name, "mime": mime}
        except Exception as e:
            print("lecture pièce jointe chiffrée:", repr(e))
            return {"error": "Impossible de charger ce fichier."}
    if path == "/api/chat/attachment":
        rid = str(b.get("id", ""))
        data_uri = str(b.get("data", ""))
        name = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.basename(str(b.get("name", "fichier"))))[:100] or "fichier"
        mime = str(b.get("mime", "")).split(";", 1)[0].lower()
        allowed_mimes = ("image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf")
        if mime not in allowed_mimes or not data_uri.startswith("data:" + mime + ";base64,"):
            return {"error": "Format non pris en charge. Envoie une image ou un PDF."}
        try:
            raw_file = base64.b64decode(data_uri.split(",", 1)[1], validate=True)
        except Exception:
            return {"error": "Fichier invalide. Réessaie."}
        if not raw_file or len(raw_file) > 4_000_000:
            return {"error": "Fichier trop lourd. Limite : 4 Mo."}
        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if not c or uid not in (c.get("client"), c.get("vendor")):
                return {"error": "Discussion introuvable ou accès refusé."}
            if c.get("status") not in ("proof_received", "expired"):
                return {"error": "La discussion est verrouillée jusqu’à la capture ou à l’expiration du délai."}
        file_key = "private_chat_files/" + rid + "/" + secrets.token_hex(12) + ".enc"
        try:
            gh_put(file_key, FERNET.encrypt(raw_file), "Ajout d'un fichier chiffré à une discussion RADARPAY")
            ATTACHMENT_CACHE[file_key] = base64.b64encode(raw_file).decode()
        except Exception as e:
            print("sauvegarde pièce jointe chiffrée:", repr(e))
            return {"error": "Impossible d’envoyer le fichier. Réessaie."}
        accepted = False
        other = None
        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if c and uid in (c.get("client"), c.get("vendor")) and c.get("status") in ("proof_received", "expired"):
                c.setdefault("messages", []).append({
                    "from": uid, "name": user.get("first_name", "Utilisateur"),
                    "text": "📎 " + name, "attachment_file": file_key,
                    "attachment_name": name, "attachment_mime": mime,
                    "attachment_size": len(raw_file), "time": int(time.time())
                })
                chats_save(chats_data, None)
                other = c.get("vendor") if uid == c.get("client") else c.get("client")
                accepted = True
        if not accepted:
            gh_del(file_key, "Nettoyage d'une pièce jointe non enregistrée")
            ATTACHMENT_CACHE.pop(file_key, None)
            return {"error": "La discussion a changé d’état. Le fichier n’a pas été envoyé."}
        def notify_attachment():
            try:
                say(other, "📎 Nouveau fichier reçu dans ta discussion RADAR PAYMENT.")
            except Exception as e:
                print("notification fichier impossible:", repr(e))
        threading.Thread(target=notify_attachment, daemon=True).start()
        public_chat = {k: c.get(k) for k in ("id", "client", "vendor", "price", "client_name", "status", "proof_deadline")}
        public_chat["messages"] = [dict(msg) for msg in (c.get("messages") or [])]
        return {"ok": True, "chat": public_chat}
    if path == "/api/chat/accept":
        rid = str(b.get("id", ""))
        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if not c or uid != c.get("client"):
                return {"error": "Seul le CALLER peut accepter cette vente."}
            if c.get("status") != "vendor_confirmed":
                return {"error": "Cette vente n'attend pas ton acceptation."}
            c["status"] = "proof_pending"
            c["proof_deadline"] = int(time.time()) + 120
            chats_save(chats_data, None)
        try:
            say(c["vendor"], "⏱️ Le CALLER a accepté la vente. Tu as 2 minutes pour envoyer la capture d’écran dans la discussion RADAR PAYMENT.")
        except Exception as e:
            print("notification délai preuve impossible:", repr(e))
        return {"ok": True}
    if path == "/api/chat/erop":
        rid = str(b.get("id", ""))
        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if not c or uid != c.get("client"):
                return {"error": "Seul le CALLER peut demander une capture EROP."}
            if eraf_validation_pending(c):
                return {"error": "Le SPITTA doit d’abord valider ERAF après la deuxième capture avant une nouvelle demande EROP."}
            if c.get("status") not in ("proof_received", "expired"):
                return {"error": "La discussion doit être ouverte et ne doit pas déjà être verrouillée."}
            c["erop_previous_status"] = c.get("status")
            c["status"] = "erop_pending"
            c["erop_photo_sent"] = False
            c["erop_sender_proof_submitted"] = False
            c["erop_sender_proof_unavailable"] = False
            c["erop_sender_deadline"] = int(time.time()) + 120
            c["erop_deadline"] = 0
            c.setdefault("messages", []).append({"from": 0, "name": "RADAR PAYMENT",
                "text": "📸 EROP — étape 1/2 : le CALLER doit envoyer une photo lisible de sa preuve d’envoi ou déclarer qu’il ne peut pas fournir de photo dans les 2 minutes. Le délai du SPITTA (3 min 30) ne démarre qu’après l’enregistrement de cette étape.", "time": int(time.time())})
            chats_save(chats_data, None)
            vendor_id = c.get("vendor")
        try:
            say(vendor_id, "📸 EROP — étape 1/2 : le CALLER doit envoyer sa photo de preuve ou déclarer qu’il ne peut pas fournir de photo dans les 2 minutes. Le délai du SPITTA (3 min 30) ne démarre qu’après cette étape. Aucune capture n’est prise à distance.")
        except Exception as e:
            print("notification EROP impossible:", repr(e))
        return {"ok": True}
    if path == "/api/chat/erop-sender-proof":
        rid = str(b.get("id", ""))
        mode = str(b.get("mode", "photo"))
        image = str(b.get("image", ""))
        raw_image = None
        if mode == "photo":
            if not image.startswith("data:image/") or len(image) > 4_200_000:
                return {"error": "Photo invalide ou trop lourde. Réessaie avec une image plus légère."}
            try:
                raw_image = decode_jpeg_data_uri(image)
            except Exception as e:
                print("validation preuve EROP du CALLER:", repr(e))
                return {"error": "Photo invalide ou trop lourde. Réessaie."}
        elif mode != "unavailable":
            return {"error": "Choisis une photo ou confirme que l’envoi de photo est impossible."}

        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if not c or uid != c.get("client"):
                return {"error": "Seul le CALLER de cette discussion peut envoyer sa preuve EROP."}
            if c.get("status") != "erop_pending":
                return {"error": "Aucune demande EROP n’attend ta preuve."}
            if c.get("erop_sender_proof_submitted"):
                return {"error": "Ta preuve EROP a déjà été enregistrée."}
            if int(time.time()) >= int(c.get("erop_sender_deadline", 0)):
                return {"error": "Le délai de 2 minutes pour l’étape du CALLER est dépassé. Cette demande EROP est clôturée."}
            vendor_id = c.get("vendor")

        proof_file = None
        if raw_image is not None:
            proof_file = "private_proofs/" + rid + "/erop_sender_" + secrets.token_hex(12) + ".enc"
            try:
                gh_put(proof_file, FERNET.encrypt(raw_image), "Sauvegarde chiffrée de la preuve EROP du CALLER")
                PROOF_CACHE[proof_file] = base64.b64encode(raw_image).decode()
            except Exception as e:
                print("sauvegarde preuve EROP du CALLER:", repr(e))
                return {"error": "Impossible de sauvegarder la photo. Réessaie."}

        accepted = False
        deadline = 0
        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if (c and uid == c.get("client") and c.get("status") == "erop_pending"
                    and not c.get("erop_sender_proof_submitted")
                    and int(time.time()) < int(c.get("erop_sender_deadline", 0))):
                now = int(time.time())
                c["erop_sender_proof_submitted"] = True
                c["erop_sender_proof_unavailable"] = mode == "unavailable"
                c.pop("erop_sender_deadline", None)
                deadline = now + 210
                c["erop_deadline"] = deadline
                message = {
                    "from": uid, "name": user.get("first_name", "CALLER"),
                    "text": ("⚠️ Le CALLER déclare ne pas pouvoir envoyer une photo de sa preuve EROP. Cette déclaration ne constitue pas une preuve photographique."
                             if mode == "unavailable" else
                             "📸 Preuve d’envoi EROP transmise par le CALLER."),
                    "time": now
                }
                if proof_file:
                    message["proof_file"] = proof_file
                c.setdefault("messages", []).append(message)
                chats_save(chats_data, None)
                accepted = True
        if not accepted:
            if proof_file:
                gh_del(proof_file, "Nettoyage d'une preuve EROP non enregistrée")
                PROOF_CACHE.pop(proof_file, None)
            return {"error": "La demande EROP a changé d’état. La preuve n’a pas été enregistrée."}
        try:
            say(vendor_id, ("⚠️ EROP — étape 1 terminée : le CALLER a déclaré ne pas pouvoir fournir de photo. Ce message est une déclaration, pas une preuve photographique. Ton délai de 3 min 30 commence maintenant : envoie volontairement la deuxième capture dans RADAR PAYMENT." if mode == "unavailable" else "📸 EROP — étape 1 terminée : le CALLER a envoyé sa photo de preuve. Ton délai de 3 min 30 commence maintenant : vérifie la photo et envoie volontairement la deuxième capture dans RADAR PAYMENT."))
        except Exception as e:
            print("notification preuve EROP au SPITTA impossible:", repr(e))
        return {"ok": True, "deadline": deadline}

    if path == "/api/chat/eraf":
        rid = str(b.get("id", ""))
        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if not c or uid != c.get("vendor"):
                return {"error": "Seul le SPITTA de cette discussion peut valider ERAF."}
            if not c.get("erop_photo_sent"):
                return {"error": "Envoie d’abord la deuxième capture EROP avant de valider ERAF."}
            if c.get("erop_confirmed"):
                return {"error": "ERAF a déjà été validé pour cette demande."}
            if int(time.time()) >= int(c.get("eraf_deadline", 0)):
                return {"error": "Le délai de 10 minutes pour valider ERAF est dépassé."}
            c["erop_confirmed"] = True
            c["erop_photo_sent"] = False
            c.setdefault("messages", []).append({"from": uid, "name": user.get("first_name", "SPITTA"),
                "text": "✅ ERAF : le SPITTA confirme que tout est OK après la deuxième capture.", "time": int(time.time())})
            chats_save(chats_data, None)
            client_id = c.get("client")
        try:
            say(client_id, "✅ Le SPITTA a validé ERAF : tout est OK après la deuxième capture.")
        except Exception as e:
            print("notification ERAF au CALLER impossible:", repr(e))
        return {"ok": True}

    if path == "/api/chat/proof":
        rid, image = str(b.get("id", "")), str(b.get("image", ""))
        if not image.startswith("data:image/") or len(image) > 4_200_000:
            return {"error": "Capture invalide ou trop lourde. Réessaie avec une image plus légère."}
        try:
            raw_image = decode_jpeg_data_uri(image)
        except Exception as e:
            print("validation capture chiffrée:", repr(e))
            return {"error": "Capture invalide ou trop lourde. Réessaie avec une image plus légère."}

        # Vérification sous verrou, transfert GitHub hors verrou pour ne pas bloquer les autres chats.
        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if not c or uid != c.get("vendor"):
                return {"error": "Seul le SPITTA peut envoyer la capture."}
            if c.get("status") == "erop_pending":
                if not c.get("erop_sender_proof_submitted"):
                    return {"error": "Le CALLER doit d’abord envoyer sa preuve EROP ou déclarer qu’il ne peut pas joindre de photo."}
                if time.time() >= c.get("erop_deadline", 0):
                    now = int(time.time())
                    c["status"] = "erop_expired"
                    blacklist_entry = {
                        "until": now + EROP_BLACKLIST_SECONDS, "reason": "Délai EROP non respecté",
                        "chat_id": c.get("id")}
                    record_guarantee_fee(c, blacklist_entry, now)
                    private_load().setdefault("blacklist", {})[str(c.get("vendor"))] = blacklist_entry
                    c["erop_blacklist_until"] = now + EROP_BLACKLIST_SECONDS
                    c.setdefault("messages", []).append({"from": 0, "name": "RADAR PAYMENT",
                        "text": "⛔ Délai EROP dépassé. Discussion bloquée. Le SPITTA est inscrit sur la liste noire pendant 7 jours. Les frais de garantie, fixés à 50 % du montant de la Target concernée, sont signalés comme dus et doivent être vérifiés par l’administration.", "time": now})
                    chats_save(chats_data, None)
                    return {"error": "Le délai EROP est dépassé. La discussion est bloquée et le compte du SPITTA est inscrit sur la liste noire pendant 7 jours. Les frais de garantie, fixés à 50 % du montant de la Target concernée, sont signalés comme dus."}
                is_erop = True
            elif c.get("status") == "proof_pending" and time.time() < c.get("proof_deadline", 0):
                is_erop = False
            else:
                if c.get("status") == "proof_pending":
                    c["status"] = "proof_expired"
                    chats_save(chats_data, None)
                return {"error": "Aucune demande de capture n’est en cours."}

        proof_file = ("private_proofs/" + rid + "/" + secrets.token_hex(12) + ".enc"
                      if is_erop else "private_proofs/" + rid + ".enc")
        try:
            gh_put(proof_file, FERNET.encrypt(raw_image), "Sauvegarde chiffrée d'une capture")
            PROOF_CACHE[proof_file] = base64.b64encode(raw_image).decode()
        except Exception as e:
            print("sauvegarde capture chiffrée:", repr(e))
            return {"error": "Impossible de sauvegarder la capture. Réessaie."}

        accepted = False
        client_id = None
        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if c and uid == c.get("vendor"):
                if is_erop and c.get("status") == "erop_pending" and time.time() < c.get("erop_deadline", 0):
                    c["status"] = c.get("erop_previous_status") or "proof_received"
                    c.pop("erop_previous_status", None)
                    c.pop("erop_deadline", None)
                    c["erop_photo_sent"] = True
                    c["erop_confirmed"] = False
                    c["eraf_deadline"] = int(time.time()) + 600
                    message_text = "📸 Deuxième capture EROP envoyée — en attente de validation ERAF par le SPITTA."
                    accepted = True
                elif not is_erop and c.get("status") == "proof_pending" and time.time() < c.get("proof_deadline", 0):
                    c["status"] = "proof_received"
                    message_text = "📸 Capture d’écran envoyée — preuve reçue."
                    accepted = True
                if accepted:
                    c.setdefault("messages", []).append({"from": uid, "name": user.get("first_name", "SPITTA"),
                        "text": message_text, "proof_file": proof_file, "time": int(time.time())})
                    chats_save(chats_data, None)
                    client_id = c.get("client")
        if not accepted:
            gh_del(proof_file, "Nettoyage d'une capture arrivée hors délai")
            PROOF_CACHE.pop(proof_file, None)
            return {"error": "Le délai est terminé ou la vente a changé d’état. La capture n’a pas été enregistrée."}
        try:
            say(client_id, "📸 Le SPITTA a envoyé la capture demandée. La discussion est ouverte.")
        except Exception as e:
            print("notification capture impossible:", repr(e))
        return {"ok": True}
    if path == "/api/chat/voice":
        rid, audio = str(b.get("id", "")), str(b.get("audio", ""))
        mime = str(b.get("mime", "audio/webm")).split(";", 1)[0].lower()
        allowed_mimes = ("audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/aac", "audio/3gpp")
        if mime not in allowed_mimes or not audio.startswith("data:audio/") or len(audio) > 900_000:
            return {"error": "Vocal invalide ou trop lourd. Réessaie avec un message plus court."}
        try:
            raw_audio = decode_audio_data_uri(audio, mime)
        except Exception as e:
            print("validation vocal chiffré:", repr(e))
            return {"error": "Vocal invalide ou trop lourd. Réessaie avec un message plus court."}

        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if not c or uid not in (c.get("client"), c.get("vendor")):
                return {"error": "Discussion introuvable ou accès refusé."}
            if c.get("status") not in ("proof_received", "expired"):
                return {"error": "La discussion est verrouillée jusqu’à la capture du SPITTA ou à l’expiration du délai de réponse."}

        voice_file = "private_voice/" + rid + "/" + str(int(time.time() * 1000)) + ".enc"
        try:
            gh_put(voice_file, FERNET.encrypt(raw_audio), "Sauvegarde chiffrée d'un message vocal")
            VOICE_CACHE[voice_file] = base64.b64encode(raw_audio).decode()
        except Exception as e:
            print("sauvegarde vocal chiffré:", repr(e))
            return {"error": "Impossible de sauvegarder le vocal. Réessaie."}

        accepted = False
        other = None
        with LOCK:
            chats_data, _ = chats_load()
            c = next((x for x in chats_data.get("chats", []) if x.get("id") == rid), None)
            if (c and uid in (c.get("client"), c.get("vendor"))
                    and c.get("status") in ("proof_received", "expired")):
                c.setdefault("messages", []).append({"from": uid, "name": user.get("first_name", "Utilisateur"),
                    "text": "🎤 Message vocal", "voice_file": voice_file, "mime": mime, "time": int(time.time())})
                chats_save(chats_data, None)
                other = c.get("vendor") if uid == c.get("client") else c.get("client")
                accepted = True
        if not accepted:
            gh_del(voice_file, "Nettoyage d'un vocal non enregistré")
            VOICE_CACHE.pop(voice_file, None)
            return {"error": "La discussion a changé d’état. Le vocal n’a pas été enregistré."}

        def notify_voice():
            try:
                say(other, "🎤 Nouveau message vocal dans RADAR PAYMENT. Ouvre la mini-app pour l'écouter.")
            except Exception as e:
                print("alerte vocal non envoyée:", repr(e))
        threading.Thread(target=notify_voice, daemon=True).start()
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
                return {"error": "La discussion est verrouillée jusqu’à la capture du SPITTA ou à l’expiration du délai de réponse."}
            c.setdefault("messages", []).append({"from": uid, "name": user.get("first_name", "Utilisateur"),
                                                  "text": msg, "time": int(time.time())})
            chats_save(chats_data, sha_ch)
        monitor_event("message", "Message envoyé dans une discussion", "Discussion : " + rid + " · Compte : " + str(uid), "info", False)
        other = c.get("vendor") if uid == c.get("client") else c.get("client")
        def notify():
            try:
                say(other, "💬 Nouveau message dans RADAR PAYMENT. Ouvre la mini-app pour répondre.")
            except Exception as e:
                print("alerte nouveau message non envoyée:", repr(e))
        threading.Thread(target=notify, daemon=True).start()
        public_chat = {k: c.get(k) for k in ("id", "client", "vendor", "price", "client_name", "status", "proof_deadline")}
        public_chat["messages"] = [dict(item) for item in (c.get("messages") or [])]
        return {"ok": True, "chat": public_chat}
    if path == "/api/add" and r != "spitta":
        return {"error": "Seuls les comptes SPITTA peuvent publier une TARGET."}
    if path == "/api/update" and r not in ("owner", "spitta"):
        return {"error": "Accès réservé."}
    if path == "/api/delete" and r != "owner":
        return {"error": "Seul l’administrateur peut supprimer une publication."}
    if path == "/api/add":
        private_state = private_load()
        profile = next((x for x in private_state.setdefault("registrations", [])
                        if str(x.get("user_id")) == str(uid)), None)
        signup_pseudo = str((profile or {}).get("pseudo") or "").strip()
        if (not profile or profile.get("role") != "spitta" or
                len(signup_pseudo) < 3 or len(signup_pseudo) > 24):
            return {"error": "Profil SPITTA invalide : ton pseudo d’inscription est introuvable. Contacte l’administration avant de publier."}
        display_name = signup_pseudo
        try:
            raw = decode_jpeg_data_uri(b.get("image", ""), max_bytes=3_000_000)
        except (ValueError, KeyError):
            return {"error": "Photo invalide ou trop lourde. Utilise une image JPEG, PNG, GIF ou WebP."}
        pid = str(int(time.time() * 1000)) + "-" + secrets.token_hex(4)
        photo = {"id": pid, "created": int(time.time()), "image": pid + ".jpg", "prix": str(b.get("prix", "")).strip()[:40], "pays": str(b.get("pays", ""))[:30], "pourcentage": str(b.get("pourcentage", ""))[:10], "categorie": str(b.get("categorie", ""))[:10], "auteur": display_name, "o": enc(uid, pid)}
        allowed_prices = {"2500 €": "2500", "5000 €": "5000", "9900 €": "9900"}
        if photo["prix"] not in allowed_prices or photo["categorie"] != allowed_prices[photo["prix"]]:
            return {"error": "Choisis uniquement 2 500 €, 5 000 € ou 9 900 €."}
        if not photo["pourcentage"]:
            return {"error": "Indique le pourcentage souhaité avant de publier."}
        try:
            percentage = float(photo["pourcentage"])
        except ValueError:
            return {"error": "Le pourcentage doit être un nombre entre 0 et 100."}
        if not 0 <= percentage <= 100:
            return {"error": "Le pourcentage doit être compris entre 0 et 100."}
        try:
            # Le transfert de l'image ne doit pas bloquer le verrou partagé de l'application.
            gh_put(f"photos/{pid}.jpg", raw, "Ajout d'une photo")
        except Exception as e:
            print("upload TARGET impossible:", repr(e))
            return {"error": "Impossible d’envoyer la photo. Réessaie."}
        try:
            with LOCK:
                data, sha = load()
                data["photos"].insert(0, photo)
                save(data, sha)
        except Exception as e:
            # En cas de délai réseau ambigu, le commit photos.json peut déjà être passé.
            # Conserver l'image évite de casser une publication qui aurait été enregistrée.
            print("enregistrement TARGET non confirmé:", repr(e))
            return {"error": "Enregistrement non confirmé. Actualise la galerie avant de réessayer pour éviter un doublon."}
        monitor_event("publication", "Nouvelle TARGET publiée", "SPITTA : " + display_name + " · Prix : " + photo["prix"] + " · Pays : " + (photo["pays"] or "Non précisé"), "info", False)
        notification = True
        try:
            who = display_name + " (ID Telegram " + str(uid) + ")"
            say(OWNER, "📢 NOUVELLE PUBLICATION RADAR PAYMENT\n"
                + "Publié par : " + who + "\n"
                + "Prix : " + photo["prix"] + "\n"
                + "Pays : " + (photo["pays"] or "Non précisé") + "\n"
                + "Pourcentage : " + photo["pourcentage"] + " %\n"
                + "Publication : " + pid)
        except Exception as e:
            notification = False
            print("notification publication Admin impossible:", repr(e))
        return {"ok": True, "notification": notification,
                "photo": {k: photo[k] for k in ("id", "image", "prix", "pays", "pourcentage", "categorie", "auteur")}}
    if path == "/api/update":
        with LOCK:
            data, sha = load()
            p = next((x for x in data["photos"] if x.get("id") == b.get("id")), None)
            if not p or not (r == "owner" or (r == "spitta" and owner_of(p) == uid)):
                return {"error": "Tu peux modifier uniquement tes propres publications."}
            if "prix" in b:
                allowed_prices = {"2500 €": "2500", "5000 €": "5000", "9900 €": "9900"}
                new_price = str(b.get("prix", "")).strip()
                if new_price not in allowed_prices:
                    return {"error": "Choisis uniquement 2 500 €, 5 000 € ou 9 900 €."}
                p["prix"] = new_price
                p["categorie"] = allowed_prices[new_price]
            if "pays" in b:
                p["pays"] = str(b.get("pays", ""))[:30]
            save(data, sha)
        return {"ok": True}
    if path == "/api/delete":
        with LOCK:
            data, sha = load()
            p = next((x for x in data["photos"] if x.get("id") == b.get("id")), None)
            if not p or r != "owner":
                return {"error": "Seul l’administrateur peut supprimer une publication."}
            data["photos"].remove(p)
            save(data, sha)
            image_path = "photos/" + str(p.get("image", ""))
        # La suppression du média se fait après libération du verrou partagé.
        image_removed = gh_del(image_path, "Suppression d'une photo") if p.get("image") else True
        return {"ok": True, "image_cleanup": image_removed}
    return {"error": "?"}


class Ping(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _s(self, obj):
        body = json.dumps(obj).encode()
        status = 503 if obj.get("unhealthy") else (200 if obj.get("ok") or obj.get("pending") or "role" in obj else (401 if "Ouvre l'app depuis Telegram." in obj.get("error", "") else 400))
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "keep-alive")
        for k, v in (("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*"),
                     ("Access-Control-Allow-Headers", "Content-Type"), ("Access-Control-Allow-Methods", "POST, GET, OPTIONS")):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            health = {"ok": True, "service": "RADARPAY"}
            if DATABASE_URL:
                try:
                    pool = ensure_db()
                    with pool.connection() as conn:
                        conn.execute("SELECT 1").fetchone()
                    health["storage"] = "PostgreSQL"
                except Exception as e:
                    print("health check PostgreSQL:", repr(e))
                    health = {"ok": False, "unhealthy": True, "service": "RADARPAY",
                              "storage": "PostgreSQL indisponible"}
            else:
                health["storage"] = "GitHub (mode dégradé)"
            if not STORAGE_READY:
                health = {"ok": False, "unhealthy": True, "service": "RADARPAY",
                          "storage": "État privé non chargé ou non déchiffrable"}
            return self._s(health)
        self._s({"ok": True})

    def do_OPTIONS(self):
        # Les navigateurs envoient une prérequête CORS avant les POST JSON.
        # Elle doit réussir sans authentification pour laisser passer le POST réel.
        self._s({"ok": True})

    def do_POST(self):
        started_at = time.perf_counter()
        try:
            length = self.headers.get("Content-Length")
            if length is None:
                return self._s({"error": "Longueur de requête manquante."})
            n = int(length)
            if n <= 0 or n > 6_000_000:
                return self._s({"error": "Corps de requête vide ou trop gros."})
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return self._s({"error": "Format de requête invalide."})
            raw = self.rfile.read(n)
            if len(raw) != n:
                return self._s({"error": "Requête incomplète."})
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                return self._s({"error": "Objet JSON attendu."})
            self._s(api(self.path, payload))
        except Exception as e:
            print("api:", e)
            self._s({"error": "Erreur, réessaie."})
        finally:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            if elapsed_ms >= 1200:
                print(f"LATENCE API {self.path}: {elapsed_ms} ms")

    def log_message(self, *a):
        pass


class FastThreadingHTTPServer(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


def serve():
    port = int(os.environ.get("PORT", "10000"))
    FastThreadingHTTPServer(("0.0.0.0", port), Ping).serve_forever()


def startup():
    # GITHUB_REPO doit pointer vers le dépôt de la mini-app (RADARPAY),
    # car c'est là que se trouvent photos.json et le dossier photos/.
    print(f"RADAR PAYMENT config: repo={REPO}, branch={BRANCH}, api_url={'définie' if API_URL else 'ABSENTE'}, site_url={'définie' if SITE else 'ABSENTE'}")
    try:
        if DATABASE_URL:
            ensure_db()
            with LOCK:
                private_load()  # préchauffe PostgreSQL et importe l'historique chiffré si nécessaire
            print("PostgreSQL RADARPAY activé; état privé chargé.")
        else:
            with LOCK:
                private_load()  # vérifie que l'état privé chiffré est lisible avant de déclarer le service prêt
            print("ATTENTION: DATABASE_URL absent; stockage historique GitHub (lent) encore utilisé.")
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