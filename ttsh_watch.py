#!/usr/bin/env python3
"""
Surveille la billetterie HelloAsso du tournoi TTSH et envoie une notification
push sur ton téléphone (via ntfy) dès qu'un tableau n'est plus « Épuisé ».

Aucune dépendance : Python 3.8+ suffit.

  python3 ttsh_watch.py --test    # envoie une notification de test
  python3 ttsh_watch.py --dump    # affiche ce que le script comprend de la page
  python3 ttsh_watch.py           # surveille en boucle (Ctrl+C pour arrêter)
  python3 ttsh_watch.py --once    # un seul passage (pour cron / GitHub Actions)

Réglages optionnels (variables d'environnement) :
  FILTRE      regex des tableaux suivis (défaut : B, G, J, K et O). Vide = tous.
              ex. FILTRE="Tableau (K|L)\\b" ou FILTRE=" " pour tout suivre
  INTERVAL    secondes entre deux vérifications (défaut 90, ±20 % aléatoire)
  REMIND_MIN  minutes avant de te relancer si la place est toujours libre (défaut 10)
  NTFY_TOPIC  nom du canal ntfy (obligatoire, sauf pour --dump)
"""
import argparse
import json
import os
import random
import re
import time
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

URL = os.environ.get(
    "WATCH_URL",
    "https://www.helloasso.com/associations/ttsh/evenements/"
    "tournoi-national-b-du-ttsh-les-19-et-20-decembre-2026",
)
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
INTERVAL = int(os.environ.get("INTERVAL", "90"))
REMIND_MIN = int(os.environ.get("REMIND_MIN", "10"))
FILTRE = os.environ.get("FILTRE", r"Tableau (B|G|J|K|O)\b")
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).resolve().with_name("state.json")))
FAIL_ALERT_AFTER = 5          # nb d'échecs de suite avant de te prévenir que la surveillance est en panne
FAIL_REALERT_SECONDS = 6 * 3600

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# --- Lecture de la page -------------------------------------------------------

BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "section",
    "article", "br", "tr", "td", "th", "button", "label", "header", "footer",
    "form", "option", "select", "a",
}
SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def page_lines(html):
    p = _Text()
    p.feed(html)
    text = "".join(p.parts).replace("\xa0", " ")
    return [re.sub(r"\s+", " ", l).strip() for l in text.split("\n") if l.strip()]


START = re.compile(r"^début\s+à\b", re.I)       # « Début à 10h45 » : juste sous le nom du tableau
STOP = re.compile(r"^commander\b", re.I)         # barre du bas de la page
SOLD_OUT = re.compile(
    r"épuisé|plus de places|\bcomplet\b|indisponible|n'est plus disponible"
    r"|vente[s]? (terminée|close|fermée)s?|pas encore ouverte",
    re.I,
)


def parse(lines):
    """Retourne [{'title', 'available'}] : un élément par tableau de la billetterie."""
    starts = [i for i, l in enumerate(lines) if START.match(l) and i > 0]
    out, seen = [], {}
    for n, i in enumerate(starts):
        title = lines[i - 1]
        end = starts[n + 1] - 1 if n + 1 < len(starts) else len(lines)
        body = []
        for l in lines[i:min(end, i + 10)]:
            if STOP.match(l):
                break
            body.append(l)
        seen[title] = seen.get(title, 0) + 1
        label = title if seen[title] == 1 else f"{title} (#{seen[title]})"
        out.append({"title": label, "available": not SOLD_OUT.search(" ".join(body))})
    return out


def suivi(t):
    """Vrai si ce tableau fait partie de ceux à surveiller (filtre FILTRE)."""
    return not FILTRE.strip() or bool(re.search(FILTRE, t["title"], re.I))


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")


# --- Notifications ------------------------------------------------------------

def notify(title, message, priority=3, tags=None):
    body = json.dumps({
        "topic": NTFY_TOPIC, "title": title, "message": message,
        "priority": priority, "tags": tags or [], "click": URL,
    }).encode("utf-8")
    req = urllib.request.Request(NTFY_SERVER, data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def log(msg):
    print(f"[{datetime.now():%d/%m %H:%M:%S}] {msg}", flush=True)


# --- Boucle de surveillance ---------------------------------------------------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


def check(state):
    now = time.time()
    try:
        tarifs = parse(page_lines(fetch(URL)))
        if not tarifs:
            raise RuntimeError("aucun tableau reconnu (accès bloqué ou structure de la page changée ?)")
        tarifs = [t for t in tarifs if suivi(t)]
        if not tarifs:
            raise RuntimeError(f"aucun tableau ne correspond au filtre « {FILTRE} »")
    except Exception as e:
        state["fail"] = state.get("fail", 0) + 1
        log(f"échec n°{state['fail']} : {e}")
        # Tant que la page n'a jamais été lue avec succès, on prévient dès le 1er échec (utile à l'installation).
        seuil = FAIL_ALERT_AFTER if state.get("started") else 1
        if state["fail"] >= seuil and now - state.get("fail_alert", 0) > FAIL_REALERT_SECONDS:
            try:
                notify("⚠️ Surveillance TTSH en panne", f"Je n'arrive plus à lire la page : {e}",
                       priority=4, tags=["warning"])
                state["fail_alert"] = now
            except Exception as e2:
                log(f"notification impossible : {e2}")
        return

    if state.get("fail_alert"):
        try:
            notify("✅ Surveillance TTSH rétablie", "La page est de nouveau lisible.", priority=2)
            state.pop("fail_alert")
        except Exception as e:
            log(f"notification impossible : {e}")
    state["fail"] = 0

    if not state.get("started"):
        lignes = [("✅ " if t["available"] else "❌ ") + t["title"] for t in tarifs]
        try:
            notify("🏓 Surveillance TTSH démarrée",
                   f"{len(tarifs)} tableaux suivis, {sum(t['available'] for t in tarifs)} avec des places.\n"
                   + "\n".join(lignes), priority=2, tags=["eyes"])
            state["started"] = True
        except Exception as e:
            log(f"notification de démarrage impossible : {e}")

    known = state.setdefault("tarifs", {})

    def is_due(t):
        prev = known.get(t["title"], {})
        return t["available"] and (
            not prev.get("available") or now - prev.get("last_alert", 0) >= REMIND_MIN * 60
        )

    due = [t for t in tarifs if is_due(t)]
    sent = False
    if due:
        titre = (f"🏓 Place libre : {due[0]['title']}" if len(due) == 1
                 else f"🏓 {len(due)} tableaux avec des places libres")
        msg = "\n".join(f"• {t['title']}" for t in due) + "\n\nÇa part vite : touche pour ouvrir la page."
        try:
            notify(titre, msg, priority=5, tags=["rotating_light", "table_tennis_paddle_and_ball"])
            sent = True
        except Exception as e:
            log(f"notification d'alerte impossible, nouvelle tentative au prochain passage : {e}")

    for t in tarifs:
        prev = known.get(t["title"], {})
        last = prev.get("last_alert", 0) if t["available"] else 0
        if sent and t in due:
            last = now
        known[t["title"]] = {"available": t["available"], "last_alert": last}

    log(f"{len(tarifs)} tableaux, {sum(t['available'] for t in tarifs)} avec des places"
        + (f" → alerte envoyée pour {len(due)}" if sent else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="un seul passage puis quitte")
    ap.add_argument("--dump", action="store_true", help="affiche ce qui est lu sur la page")
    ap.add_argument("--test", action="store_true", help="envoie une notification de test")
    args = ap.parse_args()

    if not NTFY_TOPIC and not args.dump:
        raise SystemExit("NTFY_TOPIC manquant : définis le nom de ton canal ntfy "
                         "(dans GitHub : Settings > Secrets and variables > Actions > secret NTFY_TOPIC).")

    if args.test:
        notify("🏓 Test TTSH", "Si tu vois ça sur ton téléphone, les notifications fonctionnent.", priority=3)
        print(f"Notification envoyée sur le canal « {NTFY_TOPIC} ».")
        return

    if args.dump:
        lines = page_lines(fetch(URL))
        tarifs = parse(lines)
        if not tarifs:
            print("Aucun tableau reconnu. Début du texte de la page :\n")
            print("\n".join(lines[:60]))
        for t in tarifs:
            etat = "✅ DISPO " if t["available"] else "❌ épuisé"
            print(f"{etat}  {'👀 suivi  ' if suivi(t) else '   ignoré '}  {t['title']}")
        print(f"\n{sum(suivi(t) for t in tarifs)} tableau(x) suivi(s) sur {len(tarifs)} (filtre : {FILTRE or 'aucun'})")
        return

    state = load_state()
    if args.once:
        check(state)
        save_state(state)
        return

    log(f"Surveillance de {URL}\nCanal ntfy : {NTFY_TOPIC} — Ctrl+C pour arrêter")
    while True:
        try:
            check(state)
            save_state(state)
        except Exception as e:  # ne jamais mourir silencieusement
            log(f"erreur inattendue : {e}")
        time.sleep(INTERVAL * random.uniform(0.8, 1.2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
