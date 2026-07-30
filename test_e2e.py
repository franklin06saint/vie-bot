#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_e2e.py - tests de bout en bout du bot VIE.

On ne tape JAMAIS le vrai site ni le vrai Discord. A la place on lance un faux
serveur HTTP local (http.server) qui joue les DEUX roles :
  - POST /api      : simule l'API VIE (renvoie des offres qu'on controle)
  - POST /webhook  : simule le webhook Discord (enregistre ce qu'on lui envoie
                     et peut simuler un 429 pour tester le retry)

Chaque test pointe vie_bot vers ce faux serveur en surchargeant ses variables
de module (API_URL, WEBHOOK_URL, SEEN_FILE), puis verifie le comportement.

Lancer :  python test_e2e.py        (ou : python -m unittest test_e2e -v)
"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import vie_bot


# ---------------------------------------------------------------------------
# FAUX SERVEUR (API VIE + webhook Discord)
# ---------------------------------------------------------------------------

class FakeState:
    """Etat partage entre le serveur et le test (offres a servir, messages recus)."""

    def __init__(self):
        # Ce que /api renverra. Peut etre une liste ou un dict {result: [...]}.
        self.offers_response = []
        # Tous les payloads recus sur /webhook (les embeds envoyes).
        self.webhook_payloads = []
        # Combien de fois /webhook doit repondre 429 avant de reussir.
        self.fail_429_times = 0
        # Compteur d'appels /api et /webhook (pour diagnostics).
        self.api_calls = 0
        self.webhook_calls = 0


def make_handler(state: FakeState):
    """Fabrique une classe handler liee a un etat donne."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # on tait les logs du serveur pour ne pas polluer la sortie

        def _read_json(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            return json.loads(body.decode("utf-8"))

        def _send_json(self, code, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path.startswith("/api"):
                state.api_calls += 1
                self._read_json()  # on lit le payload (ignore ici)
                self._send_json(200, state.offers_response)
                return

            if self.path.startswith("/webhook"):
                state.webhook_calls += 1
                payload = self._read_json()
                if state.fail_429_times > 0:
                    # On simule un rate limit Discord avec retry_after court.
                    state.fail_429_times -= 1
                    self._send_json(429, {"retry_after": 0.05})
                    return
                # Succes : on memorise l'embed recu et on renvoie 204.
                state.webhook_payloads.append(payload)
                self.send_response(204)
                self.end_headers()
                return

            self.send_response(404)
            self.end_headers()

    return Handler


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def make_offer(offer_id, title="Data Analyst", company="ACME",
               city="Berlin", country="Allemagne", duration="12 mois",
               creation_date=None):
    """Cree une offre brute au format API (noms de champs "reels")."""
    offer = {
        "id": offer_id,
        "missionTitle": title,
        "organizationName": company,
        "cityName": city,
        "countryName": country,
        "duration": duration,
    }
    if creation_date is not None:
        offer["creationDate"] = creation_date
    return offer


# ---------------------------------------------------------------------------
# TESTS
# ---------------------------------------------------------------------------

class VieBotE2E(unittest.TestCase):

    def setUp(self):
        # Faux serveur sur un port libre (port 0 = l'OS en choisit un).
        self.state = FakeState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        base = f"http://{host}:{port}"

        # Fichier d'etat temporaire, isole par test.
        self.tmp = tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w", encoding="utf-8"
        )
        self.tmp.close()
        self.seen_path = Path(self.tmp.name)
        self.seen_path.unlink()  # on veut qu'il soit absent au depart

        # On branche vie_bot sur le faux serveur.
        vie_bot.API_URL = base + "/api"
        vie_bot.WEBHOOK_URL = base + "/webhook"
        vie_bot.SEEN_FILE = self.seen_path
        vie_bot.KEYWORDS = []  # reset entre tests

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if self.seen_path.exists():
            self.seen_path.unlink()

    # --- Test 1 : --init ne notifie pas -----------------------------------
    def test_init_ne_notifie_pas(self):
        self.state.offers_response = [make_offer(1), make_offer(2)]
        sent = vie_bot.run(init=True)
        self.assertEqual(sent, 0)
        self.assertEqual(self.state.webhook_payloads, [])
        # Les IDs doivent bien avoir ete memorises.
        seen = json.loads(self.seen_path.read_text(encoding="utf-8"))["seen"]
        self.assertEqual(set(seen), {"1", "2"})

    # --- Test 2 : run sans nouveaute -> 0 message -------------------------
    def test_aucune_nouveaute(self):
        self.state.offers_response = [make_offer(1), make_offer(2)]
        vie_bot.run(init=True)                 # on memorise 1 et 2
        sent = vie_bot.run()                    # rien de neuf
        self.assertEqual(sent, 0)
        self.assertEqual(self.state.webhook_payloads, [])

    # --- Test 3 : 2 offres ajoutees -> 2 messages -------------------------
    def test_deux_nouvelles_offres(self):
        self.state.offers_response = [make_offer(1)]
        vie_bot.run(init=True)                  # on connait deja l'offre 1
        self.state.offers_response = [make_offer(1), make_offer(2), make_offer(3)]
        sent = vie_bot.run()
        self.assertEqual(sent, 2)
        self.assertEqual(len(self.state.webhook_payloads), 2)

    # --- Test 4 : relance immediate -> 0 doublon --------------------------
    def test_pas_de_doublon(self):
        self.state.offers_response = [make_offer(1), make_offer(2)]
        vie_bot.run(init=True)
        self.state.offers_response = [make_offer(1), make_offer(2), make_offer(3)]
        first = vie_bot.run()
        self.assertEqual(first, 1)              # seule l'offre 3 est neuve
        second = vie_bot.run()                  # relance immediate
        self.assertEqual(second, 0)             # plus rien de neuf
        self.assertEqual(len(self.state.webhook_payloads), 1)

    # --- Test 5 : KEYWORDS filtre correctement ----------------------------
    def test_keywords_filtre(self):
        self.state.offers_response = [
            make_offer(1, title="Data Engineer"),
            make_offer(2, title="Commercial export"),
            make_offer(3, title="Python Developer"),
        ]
        vie_bot.KEYWORDS = ["data", "python"]
        sent = vie_bot.run()
        # On ne notifie que "Data Engineer" et "Python Developer".
        self.assertEqual(sent, 2)
        titles = [p["embeds"][0]["title"] for p in self.state.webhook_payloads]
        self.assertIn("Data Engineer", titles)
        self.assertIn("Python Developer", titles)
        self.assertNotIn("Commercial export", titles)

        # POINT CLE : l'offre ecartee (id 2) doit quand meme etre memorisee,
        # sinon elle reviendrait si on changeait KEYWORDS.
        seen = json.loads(self.seen_path.read_text(encoding="utf-8"))["seen"]
        self.assertEqual(set(seen), {"1", "2", "3"})

        # Preuve : on vide KEYWORDS et on relance -> l'offre 2 ne revient PAS.
        vie_bot.KEYWORDS = []
        again = vie_bot.run()
        self.assertEqual(again, 0)

    # --- Test 6 : structure de l'embed conforme a Discord -----------------
    def test_structure_embed(self):
        self.state.offers_response = [make_offer(42, title="Mission Test")]
        vie_bot.run()
        self.assertEqual(len(self.state.webhook_payloads), 1)
        payload = self.state.webhook_payloads[0]
        # Discord attend une cle "embeds" contenant une liste d'embeds.
        self.assertIn("embeds", payload)
        self.assertIsInstance(payload["embeds"], list)
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "Mission Test")
        self.assertTrue(embed["url"].startswith("http"))
        self.assertIsInstance(embed["fields"], list)
        # Chaque field respecte le schema Discord {name, value, inline}.
        for field in embed["fields"]:
            self.assertIn("name", field)
            self.assertIn("value", field)
            self.assertIn("inline", field)
        names = [f["name"] for f in embed["fields"]]
        self.assertIn("Entreprise", names)
        self.assertIn("Lieu", names)

    # --- Test 7 : retry apres un 429 Discord ------------------------------
    def test_retry_sur_429(self):
        self.state.offers_response = [make_offer(1)]
        self.state.fail_429_times = 1           # le 1er POST webhook -> 429
        sent = vie_bot.run()
        self.assertEqual(sent, 1)
        # Le message finit par passer (retry reussi) : 1 embed enregistre.
        self.assertEqual(len(self.state.webhook_payloads), 1)
        # Et /webhook a bien ete appele 2 fois (429 puis 204).
        self.assertEqual(self.state.webhook_calls, 2)

    # --- Bonus : reponse API au format {result: [...]} --------------------
    def test_reponse_format_result(self):
        self.state.offers_response = {"count": 1, "result": [make_offer(7)]}
        sent = vie_bot.run()
        self.assertEqual(sent, 1)

    # --- Test 8 : mode --latest N (a la demande, seen.json intact) ---------
    def test_latest_a_la_demande(self):
        # 5 offres dispo, on en demande 3 : on doit en poster 3 (les plus
        # recentes = ids les plus grands) SANS toucher a seen.json.
        self.state.offers_response = [make_offer(i) for i in (1, 2, 3, 4, 5)]
        posted = vie_bot.run(latest=3)
        self.assertEqual(posted, 3)
        self.assertEqual(len(self.state.webhook_payloads), 3)
        titles_ids = [p["embeds"][0]["url"] for p in self.state.webhook_payloads]
        # Les 3 plus recentes sont les ids 5, 4, 3.
        self.assertTrue(all(str(i) in "".join(titles_ids) for i in (5, 4, 3)))
        # seen.json ne doit PAS avoir ete cree/modifie par ce mode.
        self.assertFalse(self.seen_path.exists())

    # --- Test 9 : --latest respecte quand meme KEYWORDS -------------------
    def test_latest_avec_keywords(self):
        self.state.offers_response = [
            make_offer(1, title="Data Analyst"),
            make_offer(2, title="Commercial"),
            make_offer(3, title="Data Engineer"),
        ]
        vie_bot.KEYWORDS = ["data"]
        posted = vie_bot.run(latest=10)
        self.assertEqual(posted, 2)  # seules les 2 offres "Data"

    # --- Test 10 : rattrapage (catchup) ignore le plafond MAX_NOTIFS ------
    def test_catchup_sans_plafond(self):
        vie_bot.MAX_NOTIFS = 3  # plafond bas volontaire
        self.state.offers_response = [make_offer(i) for i in range(1, 11)]  # 10
        posted = vie_bot.run(catchup=True)
        self.assertEqual(posted, 10)  # les 10, malgre le plafond de 3
        self.assertEqual(len(self.state.webhook_payloads), 10)

    # --- Test 11 : filtre --days ne garde que les offres recentes ---------
    def test_days_filtre_par_date(self):
        from datetime import date, timedelta
        recent = date.today().isoformat()
        vieux = (date.today() - timedelta(days=40)).isoformat()
        self.state.offers_response = [
            make_offer(1, title="Recente", creation_date=recent),
            make_offer(2, title="Vieille", creation_date=vieux),
        ]
        posted = vie_bot.run(days=7)
        self.assertEqual(posted, 1)  # seule la recente
        self.assertEqual(self.state.webhook_payloads[0]["embeds"][0]["title"],
                         "Recente")
        # Mais l'offre vieille reste memorisee (ne reviendra pas).
        seen = json.loads(self.seen_path.read_text(encoding="utf-8"))["seen"]
        self.assertEqual(set(seen), {"1", "2"})

    # --- Test 12 : la date de publication apparait dans l'embed -----------
    def test_embed_contient_date(self):
        self.state.offers_response = [
            make_offer(1, creation_date="2026-07-30T14:56:46Z")
        ]
        vie_bot.run()
        fields = self.state.webhook_payloads[0]["embeds"][0]["fields"]
        noms = {f["name"]: f["value"] for f in fields}
        self.assertIn("Publiee le", noms)
        self.assertEqual(noms["Publiee le"], "30/07/2026")


if __name__ == "__main__":
    unittest.main(verbosity=2)
