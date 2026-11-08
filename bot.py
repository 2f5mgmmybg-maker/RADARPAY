import os, time, json, base64, threading, requests
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])
GH_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPO"]            # exemple : monnom/radar-site
BRANCH = os.environ.get("GITHUB_BRANCH", "main")
SITE = os.environ.get("SITE_URL", "")
LINK = os.environ.get("MINIAPP_LINK") or SITE

API = f"https://api.telegram.org/bot{TOKEN}"
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
    return requests.post(f"{API}/{method}", json=p, timeout=40).json()


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


def send_menu(chat):
    if not LINK:
        say(chat, "Le lien de la mini app n'est pas encore réglé.")
        return
    say(chat, "Bienvenue 💡 Touche le bouton pour voir mes produits.",
        reply_markup={"inline_keyboard": [[{"text": "Ouvrir RADAR PAYMENT 💡", "url": LINK}]]})


def add_photo(msg, chat, caption):
    p = msg["photo"][-1]
    info = tg("getFile", file_id=p["file_id"])["result"]
    img = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}", timeout=60).content
    name = f"{int(time.time() * 1000)}.jpg"
    gh_put(f"photos/{name}", img, "Ajout d'une photo")
    data, sha = load()
    data["photos"].insert(0, {"image": name, "prix": caption})
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
        send_menu(chat)
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


class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def serve():
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=serve, daemon=True).start()
    offset = 0
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=45).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
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
