#!/usr/bin/env python3
"""
Surveille la page publique « Liste des inscrits » du tournoi national B de l'OCC Cesson
(tennis de table) et envoie une notification push (via ntfy) dès qu'un tableau suivi
repasse avec des places restantes. La page est lisible sans numéro de licence.

(Ce fichier garde le nom ttsh_watch.py pour ne pas toucher à l'automatisation GitHub.)

Aucune dépendance : Python 3.8+ suffit.

  NTFY_TOPIC=... python3 ttsh_watch.py --once   # un seul passage (GitHub Actions / cron)
  NTFY_TOPIC=... python3 ttsh_watch.py          # boucle (Ctrl+C pour arrêter)
  python3 ttsh_watch.py --dump                  # montre ce que le script comprend de la page
  NTFY_TOPIC=... python3 ttsh_watch.py --test   # notification de test

Réglages optionnels (variables d'environnement) :
  FILTRE      regex des tableaux suivis (défaut : 500-1199 et 500-1399 le samedi, 500-1299 le dimanche)
              FILTRE=" " pour tout suivre
  INTERVAL    secondes entre deux vérifications en mode boucle (défaut 300)
  REMIND_MIN  minutes avant de te relancer si des places restent libres (défaut 10)
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

URL = os.environ.get("WATCH_URL", "https://occessontt.fr/tournoi-visual/liste-des-inscrits/")
CLICK_URL = os.environ.get("CLICK_URL", "https://occessontt.fr/tournoi-visual/tournoi-visual-sinscrire/")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
INTERVAL = int(os.environ.get("INTERVAL", "300"))
REMIND_MIN = int(os.environ.get("REMIND_MIN", "10"))
FILTRE = os.environ.get("FILTRE", r"\b(1199|1299|1399)\b")
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).resolve().with_name("state.json")))
FAIL_ALERT_AFTER = 5          # échecs de suite avant d'être prévenu d'une panne (1 seul tant que jamais lu)
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
    "form", "option", "select", "a", "summary", "details",
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
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def page_text(html):
    p = _Text()
    p.feed(html)
    return re.sub(r"\s+", " ", "".join(p.parts).replace("\xa0", " ")).strip()


# « Samedi 14h30 : 500-1199 (61 inscriptions - 0 place restante) »
ITEM = re.compile(
    r"(?P<day>samedi|dimanche)?\s*(?P<hour>\d{1,2}\s*h\s*\d{0,2})\s*:\s*(?P<name>[^()]{2,80}?)\s*"
    r"\(\s*(?P<n>\d+)\s*inscriptions?\s*[-–—:,]?\s*"
    r"(?:(?P<left>\d+)\s*places?\s*restantes?|(?P<full>complet))\s*\)",
    re.I,
)


def parse(text):
    """Retourne [{'title', 'left', 'available'}] : un élément par tableau de la page."""
    out, seen = [], {}
    for m in ITEM.finditer(text):
        day = (m.group("day") or "?").capitalize()
        hour = re.sub(r"\s+", "", m.group("hour")).lower()
        name = re.sub(r"\s+", " ", m.group("name")).strip(" -–—")
        left = 0 if m.group("full") else int(m.group("left"))
        title = f"{day} {hour} : {name}"
        seen[title] = seen.get(title, 0) + 1
        if seen[title] > 1:
            title += f" (#{seen[title]})"
        out.append({"title": title, "left": left, "available": left > 0})
    return out


def suivi(t):
    """Vrai si ce tableau fait partie de ceux à surveiller (filtre FILTRE)."""
    return not FILTRE.strip() or bool(re.search(FILTRE, t["title"], re.I))


def places(t):
    return f"{t['left']} place" + ("s" if t["left"] > 1 else "")


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")


# --- Notifications ------------------------------------------------------------

def notify(title, message, priority=3, tags=None):
    body = json.dumps({
        "topic": NTFY_TOPIC, "title": title, "message": message,
        "priority": priority, "tags": tags or [], "click": CLICK_URL,
    }).encode("utf-8")
    req = urllib.request.Request(NTFY_SERVER, data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def log(msg):
    print(f"[{datetime.now():%d/%m %H:%M:%S}] {msg}", flush=True)


# --- Boucle de surveillance ---------------------------------------------------

def load_state():
    try:
        state = json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        state = {}
    if state.get("site") != URL:          # état d'un autre site (ex. ancien test HelloAsso) : on repart de zéro
        state = {"site": URL}
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


def check(state):
    now = time.time()
    try:
        tarifs = parse(page_text(fetch(URL)))
        if not tarifs:
            raise RuntimeError("aucun tableau reconnu (accès bloqué ou structure de la page changée ?)")
        tarifs = [t for t in tarifs if suivi(t)]
        if not tarifs:
            raise RuntimeError(f"aucun tableau ne correspond au filtre « {FILTRE} »")
    except Exception as e:
        state["fail"] = state.get("fail", 0) + 1
        log(f"échec n°{state['fail']} : {e}")
        seuil = FAIL_ALERT_AFTER if state.get("started") else 1
        if state["fail"] >= seuil and now - state.get("fail_alert", 0) > FAIL_REALERT_SECONDS:
            try:
                notify("Surveillance OCC en panne", f"Je n'arrive plus à lire la page : {e}",
                       priority=4, tags=["warning"])
                state["fail_alert"] = now
            except Exception as e2:
                log(f"notification impossible : {e2}")
        return

    if state.get("fail_alert"):
        try:
            notify("Surveillance OCC rétablie", "La page est de nouveau lisible.", priority=2, tags=["white_check_mark"])
            state.pop("fail_alert")
        except Exception as e:
            log(f"notification impossible : {e}")
    state["fail"] = 0

    if not state.get("started"):
        lignes = [("✅ " if t["available"] else "❌ ") + f"{t['title']} ({places(t)})" for t in tarifs]
        try:
            notify("🏓 Surveillance OCC démarrée",
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
        titre = (f"Place libre : {due[0]['title']}" if len(due) == 1
                 else f"{len(due)} tableaux avec des places libres")
        msg = "\n".join(f"• {t['title']} : {places(t)}" for t in due) + "\n\nÇa part vite : touche pour ouvrir l'inscription."
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

    log(f"{len(tarifs)} tableaux suivis, {sum(t['available'] for t in tarifs)} avec des places"
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
        notify("🏓 Test OCC", "Si tu vois ça sur ton téléphone, les notifications fonctionnent.", priority=3)
        print(f"Notification envoyée sur le canal « {NTFY_TOPIC} ».")
        return

    if args.dump:
        text = page_text(fetch(URL))
        tarifs = parse(text)
        if not tarifs:
            print("Aucun tableau reconnu. Début du texte de la page :\n")
            print(text[:1500])
        for t in tarifs:
            etat = "✅ DISPO " if t["available"] else "❌ complet"
            print(f"{etat}  {'👀 suivi  ' if suivi(t) else '   ignoré '}  {t['title']}  ({places(t)})")
        print(f"\n{sum(suivi(t) for t in tarifs)} tableau(x) suivi(s) sur {len(tarifs)} (filtre : {FILTRE.strip() or 'aucun'})")
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
