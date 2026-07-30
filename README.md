# vie-bot

Bot qui surveille les nouvelles offres **VIE** sur
[mon-vie-via.businessfrance.fr](https://mon-vie-via.businessfrance.fr) et les
poste sur un salon **Discord** via un webhook. Prevu pour tourner gratuitement
sur **GitHub Actions** (cron toutes les 30 min).

- Python 3.12, une seule dependance : `requests`
- Pas de scraping HTML : on interroge directement l'API JSON du front Angular
- L'etat (offres deja notifiees) est stocke dans `seen.json`, committe apres
  chaque run

---

## Comment ca marche

1. `vie_bot.py` fait un `POST` sur l'API de recherche avec un payload de filtres.
2. `normalize()` transforme chaque offre brute en dict propre. La fonction
   `pick()` essaie plusieurs noms de cles possibles, pour ne pas planter si
   l'API renomme ses champs.
3. On compare les IDs a `seen.json` pour ne garder que les **nouvelles** offres.
4. Filtre local optionnel sur le titre via `KEYWORDS`.
5. Un **embed Discord** est envoye par nouvelle offre retenue.
6. `seen.json` est mis a jour (max 2000 IDs).

Point important : **toutes** les offres vues sont memorisees, meme celles
ecartees par `KEYWORDS`. Sinon, changer les mots-cles ferait "reapparaitre" de
vieilles offres comme si elles etaient neuves.

---

## Installation locale

```bash
cd vie-bot
pip install requests
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/XXX/YYY"
```

Sur Windows PowerShell :

```powershell
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/XXX/YYY"
```

---

## Premier lancement (important)

Au tout premier run, on **memorise l'historique existant sans notifier**, pour
ne pas flooder le salon avec des centaines d'offres deja en ligne :

```bash
python vie_bot.py --init
```

Ensuite, les runs normaux ne notifient que les offres apparues depuis :

```bash
python vie_bot.py
```

---

## L'API (verifiee le 2026-07-30)

- Endpoint : `POST https://civiweb-api-prd.azurewebsites.net/api/Offers/search`
- Reponse : `{ "result": [ ... ], "count": 895, ... }`
- Champs utiles d'une offre : `id`, `missionTitle`, `organizationName`,
  `cityName`, `countryName`, `missionDuration` (entier = nb de mois),
  `missionType` (`VIE`/`VIA`), `reference`.
- URL publique d'une offre : `https://mon-vie-via.businessfrance.fr/offres/{id}`

### Point crucial : l'en-tete `X-API-KEY`

Sans l'en-tete **`X-API-KEY`**, l'API repond **401** quels que soient le
User-Agent, l'Origin ou le Referer (teste aussi avec `curl` et une empreinte
TLS de Chrome : 401 dans tous les cas). Cette cle n'est PAS une authentification
par utilisateur : c'est une cle statique que le site livre a tous les
navigateurs. Elle est donc mise en dur dans `vie_bot.py` (constante `API_KEY`),
et surchargeable via la variable d'environnement `VIE_API_KEY`.

Si un jour le bot se met a renvoyer 401, la cle a probablement change : la
relever a nouveau sur le site (F12 > Network, en-tete `X-API-KEY` de la requete
`search`) et remplacer `API_KEY`.

### Re-inspecter la reponse brute

```bash
python vie_bot.py --debug
```

Affiche le nombre d'offres, les **cles** de la premiere offre et son JSON brut.
Si les noms de champs changent, ajoute-les dans les appels `pick()` de
`normalize()`.

> Note sur `limit` : l'API plafonne a ~100 (`limit:900` -> 400) et ne renvoie
> pas le lot trie par date. Le tri par recence (plus recent d'abord) est refait
> cote client dans `run()`.

---

## Configuration (en haut de `vie_bot.py`)

| Variable      | Role                                                        |
|---------------|-------------------------------------------------------------|
| `KEYWORDS`    | Filtre sur le titre. Liste vide = tout passe.               |
| `PAYLOAD`     | Filtres envoyes a l'API (a caler avec `--debug`).           |
| `MAX_NOTIFS`  | Garde-fou anti-spam : max 15 messages par run.              |
| `MAX_SEEN`    | Nombre d'IDs conserves dans `seen.json` (2000).             |

Variables d'environnement :

| Variable              | Role                                                  |
|-----------------------|-------------------------------------------------------|
| `DISCORD_WEBHOOK_URL` | Webhook Discord (secret, obligatoire pour notifier).  |
| `VIE_API_KEY`         | Surcharge la cle `X-API-KEY` si elle change un jour.  |
| `VIE_API_URL`         | Surcharge l'URL de l'API (utile pour les tests).      |
| `VIE_SEEN_FILE`       | Surcharge le chemin de `seen.json` (tests).           |

---

## Deploiement GitHub Actions

1. Pousser ce dossier `vie-bot/` **a la racine** d'un depot GitHub (le
   `.github/workflows/` doit se retrouver a la racine du repo).
2. Dans **Settings > Secrets and variables > Actions**, creer le secret
   `DISCORD_WEBHOOK_URL`.
3. Lancer une premiere fois manuellement (onglet **Actions >
   workflow_dispatch**) apres avoir fait `--init` en local, ou committer un
   `seen.json` deja rempli.

Le workflow a besoin de `permissions: contents: write` pour committer
`seen.json` en fin de job. Sans ca, l'etat serait perdu a chaque run et le bot
renotifierait tout.

---

## Tests

Aucun acces reseau reel : un faux serveur HTTP local simule a la fois l'API VIE
et le webhook Discord.

```bash
python test_e2e.py
```

Couverture :

1. `--init` ne notifie pas
2. Run sans nouveaute -> 0 message
3. 2 offres ajoutees -> 2 messages
4. Relance immediate -> 0 doublon
5. `KEYWORDS` filtre (et memorise quand meme les offres ecartees)
6. Structure de l'embed conforme a l'API Discord
7. Retry apres un `429` Discord
