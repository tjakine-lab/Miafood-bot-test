"""
Moteur de suggestion de repas MiaFood (diabète).

Ce module n'utilise pas Telegram : il lit la base d'aliments (aliments.json),
comprend ce que la personne a mangé, puis compose un repas.

Toutes les règles sont dans la section RÈGLES ci-dessous, à faire valider par
un professionnel de santé avant d'ouvrir le bot au public.
"""

import difflib
import json
import logging
import os
import random
import re
import unicodedata

log = logging.getLogger("miafood.repas")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ALIMENTS_PATH = os.path.join(BASE_DIR, "aliments.json")

# ---------------------------------------------------------------------------
# RÈGLES (à valider par un professionnel de santé)
# ---------------------------------------------------------------------------
IG_MAX_BASE = 55  # limite « IG bas » du fichier pour le féculent ou la céréale
IG_MAX_LEGUME = 40  # légumes proposés
IG_ELEVE = 70  # au-delà, l'aliment mangé est considéré à IG élevé
NB_BASES_AVANT_LEGUMINEUSE = 2  # déjà 2 féculents/céréales mangés => légumineuse
CATS_BASE = {"Féculents", "Céréales"}
CATS_LEGUMES = {"Légumes", "Légumes verts"}
# Mots qui écartent un aliment des bases proposées (fritures, sucrés, petit-déjeuner...)
MOTS_EXCLUS_BASE = [
    "frit", "frits", "chips", "beignet", "beignets", "sucre", "sucree",
    "industriel", "bouillie", "porridge", "pain", "corn", "flakes", "biscuits",
    "croissant", "farine", "houmous", "arachide", "gari", "tapioca",
]
# Mentions de disponibilité qui rendent un aliment difficile à trouver
DISPO_DIFFICILE = ["importe", "supermarche", "boulangerie", "specialise", "urbain", "rare"]

REGIONS = [
    "Adamaoua", "Centre", "Est", "Extrême-Nord", "Littoral",
    "Nord", "Nord-Ouest", "Ouest", "Sud", "Sud-Ouest",
]
GROUPES_REGIONS = {
    "Adamaoua": "nord", "Nord": "nord", "Extrême-Nord": "nord",
    "Littoral": "cote", "Sud-Ouest": "cote", "Sud": "cote",
    "Ouest": "ouest", "Nord-Ouest": "ouest",
    "Centre": "autre", "Est": "autre",
}


# ---------------------------------------------------------------------------
# Lecture de la base d'aliments
# ---------------------------------------------------------------------------
def normaliser(texte: str) -> str:
    t = texte.lower().replace("œ", "oe").replace("æ", "ae")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"['’`]", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _singulier(mot: str) -> str:
    return mot[:-1] if len(mot) > 3 and mot.endswith("s") else mot


STOP = {"de", "du", "des", "la", "le", "les", "a", "au", "aux", "et", "d", "l", "en", "un", "une", "sans"}
SYNONYMES = {"braise": "grille", "braisee": "grille", "braises": "grille", "bicyclette": "bicyclette"}


def _tokens(texte: str) -> set:
    return {_singulier(t) for t in normaliser(texte).split() if t not in STOP}


def charger_aliments() -> dict:
    try:
        with open(ALIMENTS_PATH, encoding="utf-8") as fh:
            liste = json.load(fh)
    except (OSError, ValueError):
        log.error("aliments.json introuvable ou invalide")
        return {}
    return {a["id"]: a for a in liste}


ALIMENTS = charger_aliments()

for _a in ALIMENTS.values():
    court = re.sub(r"\(.*?\)", " ", _a["nom"])
    _a["_court"] = normaliser(court)
    _a["_complet"] = normaliser(_a["nom"])
    _a["_tokens"] = _tokens(court)

_PAR_COURT = {a["_court"]: a for a in ALIMENTS.values()}
_PAR_COMPLET = {a["_complet"]: a for a in ALIMENTS.values()}

# Mots courants -> aliment le plus probable (affiché dans la confirmation)
_ALIAS_NOMS = {
    "riz": "Riz blanc (cuit)",
    "plantain": "Plantain mûr (bouilli)",
    "poisson": "Poisson grillé",
    "poulet": "Poulet grillé (sans peau)",
    "oeuf": "Œuf (bouilli)",
    "oeufs": "Œuf (bouilli)",
    "haricot": "Haricots rouges (cuits)",
    "haricots": "Haricots rouges (cuits)",
    "pain": "Pain blanc",
    "bouillie": "Bouillie de maïs (non sucrée)",
    "viande": "Viande de bœuf grillée (maigre)",
    "boeuf": "Viande de bœuf grillée (maigre)",
    "fufu": "Foufou de manioc (fufu)",
    "foufou": "Foufou de manioc (fufu)",
    "ndole": "N'dolè (plat complet)",
    "eru": "Eru (plat complet)",
    "okok": "Okok (plat complet aux feuilles)",
    "pate": "Spaghetti (cuit al dente)",
    "pates": "Spaghetti (cuit al dente)",
    "spaghetti": "Spaghetti (cuit al dente)",
    "avocat": "Avocat",
    "arachides": "Arachide grillée (sans sel ajouté)",
    "arachide": "Arachide grillée (sans sel ajouté)",
    "the": "Thé (sans sucre)",
    "cafe": "Café noir (sans sucre)",
    "soda": "Boisson gazeuse sucrée",
    "jus": "Jus de fruit industriel",
}
_ALIAS = {}
for _cle, _nom in _ALIAS_NOMS.items():
    _a = _PAR_COMPLET.get(normaliser(_nom))
    if _a:
        _ALIAS[_cle] = _a


def trouver_aliment(texte: str):
    """Retourne l'aliment le plus proche de ce que la personne a écrit, ou None."""
    q = normaliser(texte)
    q = re.sub(r"\b\d+([.,]\d+)?\s*(kg|g|ml|cl|l|x|fois)?\b", " ", q)  # quantités
    q = " ".join(SYNONYMES.get(t, t) for t in q.split())
    q = re.sub(r"\s+", " ", q).strip()
    if not q:
        return None

    if q in _PAR_COURT:
        return _PAR_COURT[q]
    if q in _PAR_COMPLET:
        return _PAR_COMPLET[q]
    if q in _ALIAS:
        return _ALIAS[q]

    jetons = {_singulier(t) for t in q.split() if t not in STOP}
    if not jetons:
        return None
    if len(jetons) == 1:
        seul = next(iter(jetons))
        if seul in _ALIAS:
            return _ALIAS[seul]
    candidats = [a for a in ALIMENTS.values() if jetons <= a["_tokens"]]
    if candidats:
        return sorted(candidats, key=lambda a: (len(a["_tokens"]), a["id"]))[0]

    proche = difflib.get_close_matches(q, list(_PAR_COURT), n=1, cutoff=0.8)
    return _PAR_COURT[proche[0]] if proche else None


def analyser_texte(texte: str):
    """Découpe « riz, poisson braisé et gombo » -> (aliments trouvés, mots inconnus)."""
    morceaux = re.split(r"[,;\n+]| et | avec ", texte.lower())
    trouves, inconnus, vus = [], [], set()
    for m in morceaux:
        m = m.strip()
        if not m:
            continue
        a = trouver_aliment(m)
        if a is None:
            inconnus.append(m)
        elif a["id"] not in vus:
            vus.add(a["id"])
            trouves.append(a)
    return trouves, inconnus


# ---------------------------------------------------------------------------
# Disponibilité par région
# ---------------------------------------------------------------------------
def disponible(aliment: dict, region: str) -> bool:
    d = normaliser(aliment["dispo"])
    groupe = GROUPES_REGIONS.get(region, "autre")
    if any(mot in d for mot in DISPO_DIFFICILE):
        return False
    if "nord cameroun" in d:
        return groupe == "nord"
    if "zones cotieres" in d:
        return groupe == "cote"
    if "ouest cameroun" in d:
        return groupe == "ouest"
    return True  # « Oui », « Oui (saison) », « Oui (marchés) », etc.


# ---------------------------------------------------------------------------
# Composition d'un repas
# ---------------------------------------------------------------------------
def est_local(aliment: dict, region: str) -> bool:
    """Vrai si l'aliment est signalé comme typique de cette zone dans la base."""
    d = normaliser(aliment["dispo"])
    groupe = GROUPES_REGIONS.get(region, "autre")
    return (
        ("nord cameroun" in d and groupe == "nord")
        or ("zones cotieres" in d and groupe == "cote")
        or ("ouest cameroun" in d and groupe == "ouest")
    )


def _choisir(rng, pool: list, region: str):
    """Choisit dans pool, avec une chance sur deux de prendre un aliment typique de la région."""
    locaux = [a for a in pool if est_local(a, region)]
    if locaux and rng.random() < 0.5:
        return rng.choice(locaux)
    return rng.choice(pool)


def _mots(nom: str) -> set:
    return set(normaliser(nom).split())


def _est_base_valide(a: dict) -> bool:
    return a["ig"] <= IG_MAX_BASE and not (_mots(a["nom"]) & set(MOTS_EXCLUS_BASE))


def nom_affiche(a: dict) -> str:
    if a["cat"] == "Légumes verts":
        return re.sub(r"\s*\(.*?\)", "", a["nom"])
    return a["nom"]


def portion_affichee(a: dict) -> str:
    p = a["portion"]
    return "à volonté" if p.lower() == "libre" else p


def proposer(patho: str, region: str, mange: list, essai: int = 0, graine: int = 0):
    """
    Compose un repas : une base + une protéine + deux légumes + de l'eau.
    `mange` : liste d'aliments déjà mangés aujourd'hui.
    Retourne un dict (base, proteine, legumes, raisons, signature) ou None.
    """
    rng = random.Random(f"{graine}-{essai}")
    hypertension = "hypertension" in patho.lower()
    deja = {a["id"] for a in mange}
    ok = [a for a in ALIMENTS.values() if disponible(a, region)]

    nb_bases = sum(1 for a in mange if a["cat"] in CATS_BASE)
    ig_eleves = [a for a in mange if a["ig"] >= IG_ELEVE]
    a_mange_legumes = any(a["cat"] in CATS_LEGUMES for a in mange)

    feculents = [a for a in ok if a["cat"] in CATS_BASE and _est_base_valide(a)]
    legumineuses = [
        a for a in ok if a["cat"] == "Légumineuses" and _est_base_valide(a)
    ]
    raisons = []

    # --- Base ---
    if nb_bases >= NB_BASES_AVANT_LEGUMINEUSE or ig_eleves:
        pool = legumineuses
        if nb_bases >= NB_BASES_AVANT_LEGUMINEUSE:
            raisons.append(
                f"Vous avez déjà mangé {nb_bases} féculents ou céréales aujourd'hui : "
                "je propose une légumineuse plutôt qu'un nouveau féculent."
            )
        else:
            noms = ", ".join(nom_affiche(a) for a in ig_eleves[:2])
            raisons.append(
                f"Vous avez mangé {noms} (IG élevé) aujourd'hui : je choisis une "
                "base à IG bas, une légumineuse."
            )
    else:
        pool = feculents
        if legumineuses and rng.random() < 0.25:
            pool = legumineuses
        raisons.append("La base proposée a un IG bas.")
    libre = [a for a in pool if a["id"] not in deja] or pool
    if not libre:
        return None
    candidats = sorted(libre, key=lambda a: (a["ig"], a["nom"]))[:8]
    rng.shuffle(candidats)
    base = candidats[0]

    # --- Protéine ---
    prefs = ("poisson", "poulet", "oeuf", "escargot", "crevette", "maquereau", "tilapia")
    proteines = [
        a for a in ok
        if a["cat"] == "Protéines" and a["ig"] == 0
        and "frit" not in _mots(a["nom"]) and "frits" not in _mots(a["nom"])
        and not (hypertension and a["sale"])
    ]
    if hypertension:
        raisons.append("J'ai écarté les aliments salés (poisson fumé, conserves...).")
    libres = [a for a in proteines if a["id"] not in deja] or proteines
    if not libres:
        return None
    maigres = [a for a in libres if any(p in normaliser(a["nom"]) for p in prefs)]
    locales = [a for a in libres if est_local(a, region)]
    proteine = _choisir(rng, locales + (maigres or libres), region)

    # --- Légumes : un légume vert et un autre légume ---
    def valide_legume(a):
        mots = _mots(a["nom"])
        return (
            a["cat"] in CATS_LEGUMES and a["ig"] <= IG_MAX_LEGUME
            and "condiment" not in a["portion"].lower()
            and not ({"piment", "poudre"} & mots)
        )

    verts = [a for a in ok if a["cat"] == "Légumes verts" and valide_legume(a)]
    autres = [a for a in ok if a["cat"] == "Légumes" and valide_legume(a)]
    legumes = []
    for pool_l in (verts, autres):
        libres = [a for a in pool_l if a["id"] not in deja] or pool_l
        if libres:
            legumes.append(_choisir(rng, libres, region))
    if not legumes:
        return None
    if not a_mange_legumes:
        raisons.append(
            "Vous n'avez pas encore mangé de légumes aujourd'hui : j'en ajoute "
            "à ce repas."
        )
    if mange:
        raisons.append("J'évite les aliments déjà mangés aujourd'hui.")
    locaux_choisis = [a for a in [base, proteine, *legumes] if est_local(a, region)]
    if locaux_choisis:
        noms = ", ".join(nom_affiche(a) for a in locaux_choisis)
        raisons.append(f"{noms} : typique de votre région ({region}).")
    raisons.append(f"Les aliments viennent de ceux qu'on trouve facilement en région {region}.")

    signature = (base["id"], proteine["id"], tuple(l["id"] for l in legumes))
    return {
        "base": base, "proteine": proteine, "legumes": legumes,
        "raisons": raisons, "signature": signature,
    }
