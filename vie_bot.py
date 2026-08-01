#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vie_bot.py - surveille les nouvelles offres VIE et les envoie sur Discord.

Fonctionnement general :
  1. On interroge l'API JSON du site mon-vie-via.businessfrance.fr
     (celle qu'utilise le front Angular, reperee via F12 > Network).
  2. On normalise les offres (les noms de champs de l'API ne sont pas garantis,
     donc on essaie plusieurs cles possibles).
  3. On compare les identifiants a un fichier d'etat "seen.json" pour ne garder
     que les offres jamais vues.
  4. On filtre eventuellement par mots-cles (KEYWORDS).
  5. On envoie un embed Discord par nouvelle offre via un webhook.
  6. On met a jour seen.json.

Commentaires en francais SANS accents, volontairement, pour eviter tout souci
d'encodage selon la machine qui execute le script (Windows, Actions, etc.).
"""

import argparse
import json
import os
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Fuseau de Paris pour dater le bilan du soir. zoneinfo gere l'heure d'ete/hiver.
# Secours (offset fixe) si la base de fuseaux manque (rare, surtout hors Linux).
try:
    from zoneinfo import ZoneInfo
    _PARIS_TZ = ZoneInfo("Europe/Paris")
except Exception:  # pragma: no cover - depend de l'OS
    _PARIS_TZ = timezone(timedelta(hours=2))

import requests


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

# URL de l'API de recherche. Surchargeable par variable d'environnement pour
# les tests (le test lance un faux serveur local et pointe VIE_API_URL dessus).
API_URL = os.environ.get(
    "VIE_API_URL",
    "https://civiweb-api-prd.azurewebsites.net/api/Offers/search",
)

# Webhook Discord : c'est un SECRET, on ne le met jamais en dur.
# En local : export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
# Sur GitHub Actions : secret du repo injecte dans l'environnement du job.
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Fichier d'etat. Surchargeable pour les tests (fichier temporaire).
SEEN_FILE = Path(os.environ.get("VIE_SEEN_FILE", "seen.json"))

# Bilan du soir : petit fichier retenant la derniere date de bilan envoye
# (format "AAAA-MM-JJ"), pour ne poster qu'un seul bilan par jour. Surchargeable
# pour les tests.
DIGEST_FILE = Path(os.environ.get("VIE_DIGEST_FILE", "last_digest.txt"))

# Heure (de Paris) a partir de laquelle on poste le bilan du jour. 21 = 21h.
DIGEST_HOUR = 21

# Nombre max d'offres listees (nom + lien) dans le bilan du soir. Au-dela, on
# affiche "... et N autres" pour rester sous la limite Discord (4096 car.).
MAX_DIGEST_LIST = 40

# Nombre max d'IDs conserves dans seen.json. Au-dela on tronque pour eviter que
# le fichier gonfle indefiniment. Les offres VIE tournent, les vieux IDs ne
# reviennent pas, donc 2000 est large.
MAX_SEEN = 2000

# Garde-fou anti-spam : on n'envoie jamais plus de MAX_NOTIFS messages par run.
# Le bot voyant desormais TOUTES les offres, un run retarde par GitHub peut
# cumuler plus de nouveautes : on monte le plafond a 40 (le volume reel est de
# ~25 offres/jour) pour ne pas perdre d'offres. Au-dela, le bouton de rattrapage
# (--catchup, sans plafond) recupere le reste.
MAX_NOTIFS = 40

# Nombre max de resultats postes par une recherche (--search). Une recherche
# large (ex. "data") peut matcher beaucoup d'offres : on plafonne pour ne pas
# noyer le salon.
MAX_SEARCH = 25

# Filtre local optionnel sur le titre de l'offre. Liste vide = aucun filtre
# (toutes les offres passent). Sinon on ne notifie que si un des mots (en
# minuscules) apparait dans le titre.
# Exemple : KEYWORDS = ["data", "python", "software"]
KEYWORDS: list[str] = []

# Payload envoye a l'API. VERIFIE le 2026-07-30 sur le vrai endpoint : ces noms
# de champs sont les bons. On reste large (aucun filtre serveur), le tri fin se
# fait cote KEYWORDS.
# NB sur "limit" : l'API accepte jusqu'a ~100 (limit=900 -> 400 Bad Request) et
# ne renvoie PAS le lot trie par date. 100 couvre tres largement les nouveautes
# entre deux runs de 30 min ; le tri par recence est refait cote client (voir
# run()).
PAYLOAD = {
    "limit": 100,
    "skip": 0,
    "query": "",
    "activitySectorId": [],
    "missionsTypesIds": [],
    "countriesIds": [],
    "studiesLevelId": [],
    "companiesSizes": [],
    "specializationsIds": [],
    "entreprisesIds": [],
    "missionStartDate": None,
    "gerographicZones": [],
    "countriesFilterOperator": "OR",
    "specializationsFilterOperator": "OR",
}

# Cle d'API du front. VERIFIE le 2026-07-30 : sans l'en-tete "X-API-KEY",
# l'API repond 401 (peu importe User-Agent / Origin / Referer). Ce n'est PAS
# une auth par utilisateur : c'est une cle statique livree a tous les
# navigateurs par le site (repere via le fetch patche du front). On peut donc
# la mettre en dur. Si un jour ca casse en 401, il suffit de la re-relever
# depuis le site (F12) et de la remplacer, ou de definir VIE_API_KEY.
API_KEY = os.environ.get(
    "VIE_API_KEY", "l+KwpoLPiXlsjxNT/NQ2iOFz8+iuygxAODs9FeAEWYM="
)

# Headers de la requete. Le seul vraiment indispensable est X-API-KEY ; on
# ajoute un User-Agent + Origin/Referer realistes par bonne hygiene.
SITE = "https://mon-vie-via.businessfrance.fr"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "X-API-KEY": API_KEY,
    "Origin": SITE,
    "Referer": SITE + "/",
}

# Timeout reseau (secondes) pour ne jamais bloquer un job Actions.
HTTP_TIMEOUT = 30


# ---------------------------------------------------------------------------
# ETAT (seen.json)
# ---------------------------------------------------------------------------

def load_seen() -> set[str]:
    """Charge l'ensemble des IDs deja vus. Retourne un set vide si absent."""
    if not SEEN_FILE.exists():
        return set()
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Fichier corrompu ou illisible : on repart d'un etat vide plutot que
        # de planter. Au pire on renotifie, ce n'est pas dramatique.
        return set()
    # On stocke une liste dans le JSON, on manipule un set en memoire.
    return set(str(x) for x in data.get("seen", []))


def save_seen(seen: set[str]) -> None:
    """Sauvegarde les IDs vus, tronques aux MAX_SEEN plus recents.

    Un set n'a pas d'ordre, donc "les plus recents" est approximatif. Ce qui
    compte : ne pas laisser le fichier grossir sans limite. On garde une tranche
    stable et on ecrit du JSON lisible (trie) pour des diffs git propres.
    """
    ids = sorted(seen)
    if len(ids) > MAX_SEEN:
        ids = ids[-MAX_SEEN:]
    SEEN_FILE.write_text(
        json.dumps({"seen": ids}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# API VIE
# ---------------------------------------------------------------------------

def fetch_offers(skip: int = 0) -> list[dict]:
    """Interroge l'API et retourne une liste d'offres brutes (dicts).

    'skip' permet la pagination (0 = premier lot). La reponse peut avoir deux
    formes selon l'API :
      - directement une liste [ {...}, {...} ]
      - un objet { "count": N, "result": [ {...} ] }
    On gere les deux.
    """
    payload = dict(PAYLOAD, skip=skip)
    resp = requests.post(
        API_URL, json=payload, headers=HEADERS, timeout=HTTP_TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # On essaie les cles les plus probables pour la liste de resultats.
        for key in ("result", "results", "data", "offers", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    # Forme inattendue : on renvoie une liste vide, le run ne notifie rien.
    return []


def fetch_all_offers(max_pages: int = 15) -> list[dict]:
    """Recupere TOUTES les offres en paginant (pour la recherche).

    On avance par lots (skip += limit) jusqu'a ce que l'API ne renvoie plus
    rien, ou qu'on atteigne max_pages (garde-fou anti-boucle infinie). On
    deduplique par id car l'ordre de l'API n'est pas garanti.
    """
    step = PAYLOAD.get("limit", 100)
    vus, tout = set(), []
    for page in range(max_pages):
        lot = fetch_offers(skip=page * step)
        if not lot:
            break
        nouveaux = 0
        for o in lot:
            oid = o.get("id")
            if oid not in vus:
                vus.add(oid)
                tout.append(o)
                nouveaux += 1
        if nouveaux == 0:  # plus rien de neuf -> on arrete
            break
    return tout


def pick(offer: dict, *names, default=None):
    """Retourne la premiere valeur non vide parmi plusieurs cles possibles.

    L'API n'est pas documentee et ses noms de champs peuvent changer. Plutot
    que de coder en dur "offer['missionTitle']" (qui planterait si la cle
    disparait), on tente une liste de noms et on prend le premier qui repond.
    """
    for name in names:
        if name in offer and offer[name] not in (None, "", []):
            return offer[name]
    return default


def normalize(offer: dict) -> dict:
    """Transforme une offre brute de l'API en dict propre et stable.

    Les cles de sortie sont fixes (id, title, company, city, country, duration,
    url), ce qui isole le reste du code des noms de champs de l'API.
    """
    offer_id = pick(offer, "id", "offerId", "Id", "ID")
    title = pick(offer, "missionTitle", "title", "mission", "label", "name",
                 default="(sans titre)")
    company = pick(offer, "organizationName", "companyName", "company",
                   "entreprise", "organisation", default="?")
    city = pick(offer, "cityName", "city", "ville", default="")
    country = pick(offer, "countryName", "country", "pays", default="")
    # L'API renvoie missionDuration = un entier (nombre de mois), ex. 12.
    duration = pick(offer, "missionDuration", "duration", "duree", default="")
    # Date de creation, ex. "2026-07-30T14:56:46Z" -> on garde "2026-07-30".
    date = pick(offer, "creationDate", "startBroadcastDate", "dateCreation",
                default="")
    # Date de MISE EN LIGNE (diffusion). Peut differer de la creation : une offre
    # creee le 31 peut n'etre visible que le 1er. C'est cette date qui colle au
    # moment ou l'offre "apparait" -> on l'utilise pour le bilan du jour.
    bdate = pick(offer, "startBroadcastDate", "creationDate", default="")
    # Indemnite mensuelle. L'API renvoie un nombre en euros, ex. 2978.53.
    indemnite = pick(offer, "indemnite", "indemnity", "allowance", default="")

    return {
        "id": str(offer_id) if offer_id is not None else None,
        "title": str(title),
        "company": str(company),
        "city": str(city),
        "country": str(country),
        "duration": _format_duration(duration),
        "indemnite": _format_indemnite(indemnite),
        "date": str(date)[:10],  # YYYY-MM-DD (chaine vide si absente)
        "bdate": str(bdate)[:10],  # date de mise en ligne (pour le bilan)
        # URL reelle d'une offre : /offres/{id} (verifie sur le site).
        "url": f"{SITE}/offres/{offer_id}" if offer_id is not None else SITE,
    }


def _id_sort_key(offer_id: str) -> int:
    """Cle de tri par recence : id numerique croissant = offre plus recente.

    Les id de l'API sont des entiers (sous forme de chaine). Si jamais un id
    n'est pas numerique, on renvoie 0 pour ne pas planter le tri.
    """
    try:
        return int(offer_id)
    except (ValueError, TypeError):
        return 0


def _recent_enough(date_str: str, days: int) -> bool:
    """Vrai si la date "YYYY-MM-DD" est dans les N derniers jours.

    Si la date est absente ou illisible, on renvoie True (on prefere garder une
    offre douteuse plutot que de la perdre par erreur de date).
    """
    if not date_str:
        return True
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return True
    return d >= date.today() - timedelta(days=days)


def _format_duration(value) -> str:
    """Met la duree en forme. L'API donne un entier de mois -> 'N mois'."""
    if value in (None, ""):
        return ""
    # Si c'est un nombre (ou une chaine numerique), on ajoute 'mois'.
    try:
        return f"{int(value)} mois"
    except (ValueError, TypeError):
        return str(value)


def _format_indemnite(value) -> str:
    """Met l'indemnite en forme : 2978.53 -> '2 978 EUR/mois'.

    L'API renvoie un montant mensuel en euros (nombre). On arrondit a l'entier
    (les centimes n'apportent rien pour comparer des offres) et on separe les
    milliers par une espace, a la francaise. Valeur absente ou nulle -> ''.
    """
    if value in (None, "", 0):
        return ""
    try:
        montant = round(float(value))
    except (ValueError, TypeError):
        return str(value)
    if montant <= 0:
        return ""
    # Espace fine comme separateur de milliers : "2 978".
    return f"{montant:,}".replace(",", " ") + " EUR/mois"


def _strip_accents(text: str) -> str:
    """Enleve les accents d'une chaine (pour comparer des noms de pays)."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# Table nom de pays (FR, sans accent, en majuscules) -> drapeau emoji. Couvre
# les principales destinations VIE. Un pays absent -> globe neutre (voir plus
# bas). On normalise la cle d'entree (majuscules, sans accent) avant de chercher.
_COUNTRY_FLAGS = {
    "ALLEMAGNE": "🇩🇪", "ROYAUME-UNI": "🇬🇧", "ETATS-UNIS": "🇺🇸",
    "ESPAGNE": "🇪🇸", "ITALIE": "🇮🇹", "BELGIQUE": "🇧🇪", "SUISSE": "🇨🇭",
    "PAYS-BAS": "🇳🇱", "LUXEMBOURG": "🇱🇺", "IRLANDE": "🇮🇪",
    "PORTUGAL": "🇵🇹", "AUTRICHE": "🇦🇹", "POLOGNE": "🇵🇱", "SUEDE": "🇸🇪",
    "DANEMARK": "🇩🇰", "NORVEGE": "🇳🇴", "FINLANDE": "🇫🇮", "GRECE": "🇬🇷",
    "REPUBLIQUE TCHEQUE": "🇨🇿", "TCHEQUIE": "🇨🇿", "HONGRIE": "🇭🇺",
    "ROUMANIE": "🇷🇴", "SLOVAQUIE": "🇸🇰", "BULGARIE": "🇧🇬",
    "CROATIE": "🇭🇷", "SERBIE": "🇷🇸", "UKRAINE": "🇺🇦", "TURQUIE": "🇹🇷",
    "RUSSIE": "🇷🇺", "CANADA": "🇨🇦", "MEXIQUE": "🇲🇽", "BRESIL": "🇧🇷",
    "ARGENTINE": "🇦🇷", "CHILI": "🇨🇱", "COLOMBIE": "🇨🇴", "PEROU": "🇵🇪",
    "CHINE": "🇨🇳", "HONG KONG": "🇭🇰", "SINGAPOUR": "🇸🇬", "JAPON": "🇯🇵",
    "COREE DU SUD": "🇰🇷", "TAIWAN": "🇹🇼", "INDE": "🇮🇳", "THAILANDE": "🇹🇭",
    "VIETNAM": "🇻🇳", "INDONESIE": "🇮🇩", "MALAISIE": "🇲🇾",
    "PHILIPPINES": "🇵🇭", "AUSTRALIE": "🇦🇺", "NOUVELLE-ZELANDE": "🇳🇿",
    "EMIRATS ARABES UNIS": "🇦🇪", "QATAR": "🇶🇦", "ARABIE SAOUDITE": "🇸🇦",
    "ISRAEL": "🇮🇱", "MAROC": "🇲🇦", "TUNISIE": "🇹🇳", "ALGERIE": "🇩🇿",
    "EGYPTE": "🇪🇬", "AFRIQUE DU SUD": "🇿🇦", "NIGERIA": "🇳🇬",
    "KENYA": "🇰🇪", "SENEGAL": "🇸🇳", "COTE D'IVOIRE": "🇨🇮",
    "FRANCE": "🇫🇷",
}


def _country_flag(country: str) -> str:
    """Retourne le drapeau emoji d'un pays, ou un globe si inconnu/absent."""
    if not country:
        return ""
    cle = _strip_accents(country).upper().strip()
    return _COUNTRY_FLAGS.get(cle, "🌍")


def matches_keywords(offer: dict) -> bool:
    """Vrai si l'offre passe le filtre KEYWORDS (ou si aucun filtre defini)."""
    if not KEYWORDS:
        return True
    title = offer["title"].lower()
    return any(kw.lower() in title for kw in KEYWORDS)


# ---------------------------------------------------------------------------
# DISCORD
# ---------------------------------------------------------------------------

def build_embed(offer: dict) -> dict:
    """Construit l'embed Discord pour une offre (structure API Discord)."""
    lieu = " / ".join(p for p in (offer["city"], offer["country"]) if p) or "?"
    fields = [
        {"name": "Entreprise", "value": offer["company"] or "?", "inline": True},
        {"name": "Lieu", "value": lieu, "inline": True},
    ]
    if offer["duration"]:
        fields.append(
            {"name": "Duree", "value": offer["duration"], "inline": True}
        )
    if offer.get("indemnite"):
        fields.append(
            {"name": "Indemnite", "value": offer["indemnite"], "inline": True}
        )
    if offer.get("date"):
        fields.append(
            {"name": "Publiee le", "value": _date_fr(offer["date"]),
             "inline": True}
        )
    # Drapeau du pays en tete de titre : reperage visuel immediat dans le flux.
    flag = _country_flag(offer["country"])
    titre = f"{flag} {offer['title']}".strip() if flag else offer["title"]
    return {
        "title": titre[:256],  # Discord limite le titre a 256 chars.
        "url": offer["url"],
        "color": 0x1B6CA8,
        "fields": fields,
    }


def _date_fr(date_str: str) -> str:
    """Convertit 'YYYY-MM-DD' en 'JJ/MM/AAAA' (plus lisible). Sinon, tel quel."""
    try:
        return date.fromisoformat(date_str).strftime("%d/%m/%Y")
    except ValueError:
        return date_str


def _post_webhook(payload: dict) -> None:
    """Poste un payload sur le webhook Discord. Gere le 429 (retry unique).

    Discord repond 429 avec un champ JSON "retry_after" (secondes) quand on va
    trop vite. On lit ce delai, on attend, et on reessaie UNE fois. Si ca rate
    encore, on abandonne ce message sans crasher le run.
    """
    for attempt in range(2):  # tentative initiale + 1 retry
        resp = requests.post(
            WEBHOOK_URL, json=payload, headers={"Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 429:
            # On lit retry_after depuis le corps JSON (ou l'en-tete en secours).
            retry_after = 1.0
            try:
                retry_after = float(resp.json().get("retry_after", 1.0))
            except (ValueError, json.JSONDecodeError):
                retry_after = float(resp.headers.get("Retry-After", 1.0))
            if attempt == 0:
                print(f"  429 Discord, attente {retry_after:.1f}s puis retry")
                time.sleep(retry_after)
                continue
            print("  429 Discord encore apres retry, message ignore")
            return
        # 2xx attendu (204 en general). On leve pour les autres erreurs.
        resp.raise_for_status()
        return


def send_discord(offer: dict) -> None:
    """Envoie l'embed Discord d'une offre."""
    _post_webhook({"embeds": [build_embed(offer)]})


# ---------------------------------------------------------------------------
# BILAN DU SOIR
# ---------------------------------------------------------------------------

def _load_last_digest() -> str:
    """Retourne la date du dernier bilan envoye ('AAAA-MM-JJ'), ou '' si aucun."""
    try:
        return DIGEST_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _save_last_digest(jour: str) -> None:
    """Memorise la date du dernier bilan envoye."""
    DIGEST_FILE.write_text(jour, encoding="utf-8")


def build_digest_embed(offers: list[dict], jour: str) -> dict:
    """Construit l'embed du bilan quotidien.

    'offers' = toutes les offres vues aujourd'hui (deja paginees), 'jour' = la
    date du jour au format 'AAAA-MM-JJ'. Comme le bot voit desormais TOUTES les
    offres, le nombre publie aujourd'hui = le nombre reellement envoye : le bilan
    certifie donc que rien n'a ete manque.
    """
    # "Du jour" = mise en ligne aujourd'hui (bdate), ou a defaut creee aujourd'hui.
    du_jour = [o for o in offers
               if o.get("bdate") == jour or o.get("date") == jour]
    # Plus recentes en haut (id decroissant).
    du_jour.sort(key=lambda o: _id_sort_key(o["id"]), reverse=True)
    n = len(du_jour)
    if not n:
        desc = "Aucune nouvelle offre publiee aujourd'hui. À demain ! 🌙"
    else:
        # Liste "nom cliquable" (avec drapeau), les plus recentes d'abord. On
        # plafonne pour rester sous la limite Discord (4096 car. de description).
        lignes = []
        for o in du_jour[:MAX_DIGEST_LIST]:
            flag = _country_flag(o["country"])
            prefixe = f"{flag} " if flag else ""
            lignes.append(f"- [{prefixe}{o['title']}]({o['url']})")
        if n > MAX_DIGEST_LIST:
            lignes.append(f"- … et **{n - MAX_DIGEST_LIST}** autre(s)")
        entete = (f"**{n}** offre(s) publiee(s) aujourd'hui, "
                  f"**toutes envoyees** ✅\n\n")
        desc = entete + "\n".join(lignes)
    return {
        "title": f"📊 Bilan du {_date_fr(jour)}",
        "description": desc[:4096],  # garde-fou limite Discord
        "color": 0x2ECC71,  # vert : tout est ok
    }


def send_digest(offers: list[dict], jour: str) -> None:
    """Poste le bilan du jour sur Discord et memorise qu'il a ete envoye."""
    _post_webhook({"embeds": [build_digest_embed(offers, jour)]})
    _save_last_digest(jour)
    print(f"Bilan du {jour} envoye.")


def maybe_send_digest(offers: list[dict], force: bool = False) -> bool:
    """Envoie le bilan si on est apres DIGEST_HOUR (Paris) et pas deja fait.

    Retourne True si un bilan a ete envoye. 'force=True' (mode --digest manuel)
    ignore l'heure et l'etat, pour tester tout de suite.
    """
    now = datetime.now(_PARIS_TZ)
    jour = now.date().isoformat()
    if not force:
        if now.hour < DIGEST_HOUR:
            return False  # trop tot dans la journee
        if _load_last_digest() == jour:
            return False  # bilan du jour deja poste
    if not WEBHOOK_URL:
        print("ATTENTION : DISCORD_WEBHOOK_URL non defini, bilan non envoye.")
        return False
    send_digest(offers, jour)
    return True


# ---------------------------------------------------------------------------
# LOGIQUE PRINCIPALE
# ---------------------------------------------------------------------------

def _run_search(term: str) -> int:
    """Recherche : poste toutes les offres contenant 'term'. Voir run(search=)."""
    term_low = term.strip().lower()
    offers = [o for o in (normalize(r) for r in fetch_all_offers())
              if o["id"] is not None]

    def matche(o):
        # On cherche le mot dans plusieurs champs, sans tenir compte de la casse.
        blob = " ".join((o["title"], o["company"], o["city"],
                         o["country"])).lower()
        return term_low in blob

    trouves = [o for o in offers if matche(o)]
    trouves.sort(key=lambda o: _id_sort_key(o["id"]), reverse=True)

    print(f"Recherche '{term}' : {len(trouves)} offre(s) trouvee(s) "
          f"sur {len(offers)} au total.")
    if not WEBHOOK_URL:
        print("ATTENTION : DISCORD_WEBHOOK_URL non defini, pas d'envoi.")
        return 0

    a_poster = trouves[:MAX_SEARCH]
    for o in a_poster:
        send_discord(o)
        print(f"  envoye : {o['title']} ({o['company']})")
    if len(trouves) > MAX_SEARCH:
        print(f"  ({len(trouves) - MAX_SEARCH} autres non postees, "
              f"affine ta recherche pour en voir moins.)")
    print(f"Recherche terminee : {len(a_poster)} offres postees "
          f"(seen.json inchange).")
    return len(a_poster)


def run(init: bool = False, debug: bool = False, latest: int = 0,
        catchup: bool = False, days: int = 0, search: str = "",
        digest: bool = False) -> int:
    """Execute un cycle complet. Retourne le nombre de messages envoyes.

    Modes :
      - normal         : ne poste QUE les nouvelles offres (compare a seen.json),
                         plafonne a MAX_NOTIFS par run.
      - init=True      : memorise tout sans rien poster (1er lancement).
      - debug=True     : affiche le JSON brut de l'API et s'arrete.
      - latest=N (>0)  : poste les N offres les plus RECENTES tout de suite, SANS
                         toucher a seen.json (mode "a la demande").
      - catchup=True   : RATTRAPAGE. Poste TOUTES les offres pas encore vues (pas
                         de plafond), et les memorise. Sert a recuperer ce que le
                         bot automatique aurait rate (panne, PC eteint...). Ne
                         renvoie jamais une offre deja stockee.
      - days=N (>0)    : filtre optionnel : ne garder que les offres des N
                         derniers jours (par date de creation). Se combine avec
                         le mode normal ou catchup.
      - search="mot"   : RECHERCHE. Fouille TOUTES les offres et poste celles qui
                         contiennent "mot" (titre, entreprise, ville, pays). Ne
                         touche PAS a seen.json. Plafonne a MAX_SEARCH.
    """
    # La recherche fouille TOUTES les offres (pagination).
    if search:
        return _run_search(search)

    if debug:
        # Mode diagnostic : une seule page suffit pour inspecter les cles.
        raw = fetch_offers()
        print(f"Offres recues : {len(raw)}")
        if raw:
            print("Cles de la premiere offre :")
            print("  " + ", ".join(sorted(raw[0].keys())))
            print("JSON brut de la premiere offre :")
            print(json.dumps(raw[0], ensure_ascii=False, indent=2)[:2000])
        return 0

    # IMPORTANT : on pagine TOUTES les offres (pas juste la 1re page de 100).
    # Verifie le 2026-08-01 : l'API ne trie pas par date, donc une seule page
    # rate ~26% des offres les plus recentes -> des offres n'etaient detectees
    # que des jours plus tard (ou jamais). En paginant, aucune offre n'echappe
    # au bot : toute nouveaute est vue des le passage suivant.
    raw = fetch_all_offers()

    # On normalise et on jette les offres sans id (inexploitables : impossible
    # de savoir si on les a deja vues).
    offers = [o for o in (normalize(r) for r in raw) if o["id"] is not None]

    # IMPORTANT : l'API ne renvoie PAS le lot trie du plus recent au plus ancien
    # (verifie le 2026-07-30 : avec limit=100 l'ordre est arbitraire). On trie
    # donc nous-memes par id decroissant (id plus grand = offre plus recente).
    # Utile pour MAX_NOTIFS : si beaucoup de nouveautes d'un coup, on notifie
    # bien les PLUS RECENTES, pas 15 offres au hasard.
    offers.sort(key=lambda o: _id_sort_key(o["id"]), reverse=True)

    # Mode bilan manuel : on poste le bilan du jour tout de suite (ignore l'heure
    # et l'etat) et on s'arrete. Sert a tester le rendu.
    if digest:
        maybe_send_digest(offers, force=True)
        return 0

    # Mode "a la demande" : on poste les N plus recentes et on s'arrete. On NE
    # touche PAS a seen.json (le suivi automatique n'est donc pas perturbe).
    if latest > 0:
        choix = [o for o in offers if matches_keywords(o)][:latest]
        if not WEBHOOK_URL:
            print("ATTENTION : DISCORD_WEBHOOK_URL non defini, pas d'envoi.")
            return 0
        for o in choix:
            send_discord(o)
            print(f"  envoye : {o['title']} ({o['company']})")
        print(f"--latest {latest} : {len(choix)} offres postees "
              f"(seen.json inchange).")
        return len(choix)

    seen = load_seen()
    # Nouvelles = jamais vues, tous filtres confondus.
    new_offers = [o for o in offers if o["id"] not in seen]

    if init:
        # Premier lancement : on enregistre TOUT sans notifier, pour ne pas
        # spammer le salon avec l'historique complet des offres existantes.
        for o in offers:
            seen.add(o["id"])
        save_seen(seen)
        print(f"--init : {len(offers)} offres memorisees, aucune notification.")
        return 0

    # POINT IMPORTANT : on memorise TOUTES les nouvelles offres vues, y compris
    # celles ecartees par KEYWORDS. Sinon, si on change les mots-cles plus tard,
    # de vieilles offres "reapparaitraient" comme nouvelles.
    for o in new_offers:
        seen.add(o["id"])

    # Parmi les nouvelles, on ne notifie que celles qui passent KEYWORDS.
    to_notify = [o for o in new_offers if matches_keywords(o)]

    # Filtre date optionnel : on ne garde que les offres des N derniers jours.
    # (Les offres plus anciennes restent memorisees : on ne les reverra pas.)
    if days > 0:
        to_notify = [o for o in to_notify if _recent_enough(o["date"], days)]

    # En mode rattrapage, pas de plafond : on veut TOUT recuperer.
    plafond = None if catchup else MAX_NOTIFS

    sent = 0
    if not WEBHOOK_URL and to_notify:
        print("ATTENTION : DISCORD_WEBHOOK_URL non defini, pas d'envoi.")
    else:
        for o in to_notify:
            if plafond is not None and sent >= plafond:
                print(f"MAX_NOTIFS ({MAX_NOTIFS}) atteint, on s'arrete pour ce run.")
                break
            send_discord(o)
            sent += 1
            print(f"  envoye : {o['title']} ({o['company']})")

    # On sauvegarde l'etat APRES avoir tente les envois. Les offres non
    # envoyees pour cause de MAX_NOTIFS restent memorisees : c'est voulu, elles
    # ne seront pas renvoyees. (Compromis : anti-spam prime sur l'exhaustivite.)
    save_seen(seen)

    # Bilan du soir : au 1er passage apres DIGEST_HOUR (Paris), on poste le recap
    # du jour (une seule fois par jour). Pas sur le bouton rattrapage (--catchup).
    if not catchup:
        maybe_send_digest(offers)

    print(f"Termine : {len(new_offers)} nouvelles offres, {sent} notifications.")
    return sent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Bot d'alerte VIE vers Discord.")
    parser.add_argument(
        "--init", action="store_true",
        help="Remplit seen.json sans notifier (a lancer au tout debut).",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Affiche le JSON brut et les cles de l'API, puis quitte.",
    )
    parser.add_argument(
        "--latest", type=int, metavar="N", default=0,
        help="Poste tout de suite les N offres les plus recentes, sans toucher "
             "a seen.json (mode 'a la demande').",
    )
    parser.add_argument(
        "--catchup", action="store_true",
        help="Rattrapage : poste TOUTES les offres pas encore vues (sans "
             "plafond) et les memorise. Recupere ce que le bot aurait rate.",
    )
    parser.add_argument(
        "--days", type=int, metavar="N", default=0,
        help="Ne garder que les offres des N derniers jours (filtre optionnel, "
             "se combine avec le mode normal ou --catchup).",
    )
    parser.add_argument(
        "--search", metavar="MOT", default="",
        help="Recherche : poste toutes les offres contenant MOT (titre, "
             "entreprise, ville, pays). Ne touche pas a seen.json.",
    )
    parser.add_argument(
        "--digest", action="store_true",
        help="Poste le bilan du jour tout de suite (test), sans attendre 21h.",
    )
    args = parser.parse_args(argv)

    try:
        run(init=args.init, debug=args.debug, latest=args.latest,
            catchup=args.catchup, days=args.days, search=args.search,
            digest=args.digest)
    except requests.RequestException as exc:
        # Erreur reseau/API : on log et on sort en erreur pour que le job
        # Actions apparaisse en rouge, mais sans traceback illisible.
        print(f"Erreur reseau : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
