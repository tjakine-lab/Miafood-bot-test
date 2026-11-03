"""
MiaFood - assistant nutritionnel (version 3, Telegram)

Nouveautés de cette version :
- le questionnaire demande le type de diabète (Type 1, Type 2, prédiabète)
- « Mon plan de repas » affiche le plan du jour, avec un choix d'autres
  situations (jour de marché, fête, harmattan, etc.)
- les plans viennent du fichier plans.json (extrait du fichier Excel)
- mémoire permanente (db.py), prénom, accueil personnalisé, code d'invitation
  (variable CODE_TEST), bouton d'avis, suppression des données
- « Que manger aujourd'hui ? » : idée de déjeuner ou de dîner selon ce que la
  personne a déjà mangé, sa région et son état de santé (moteur : repas.py)

Lancement local :
    export TELEGRAM_TOKEN="ton_token"
    python bot.py
Sur Render, la variable RENDER_EXTERNAL_URL active le mode webhook.
"""

import json
import logging
import os
import re
from datetime import date

import db
import repas
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
# Les bibliothèques réseau écrivent l'adresse complète des requêtes (qui contient
# le token Telegram) : on les réduit au silence pour ne rien divulguer dans les logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("miafood")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Code d'invitation (facultatif) et identifiant de l'administrateur
CODE_TEST = os.environ.get("CODE_TEST", "").strip()
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "0").strip() or 0)
except ValueError:
    ADMIN_ID = 0
PLANS_PATH = os.path.join(BASE_DIR, "plans.json")

# Étapes du questionnaire
SEXE, AGE, TAILLE, POIDS, ACTIVITE, OBJECTIF, PATHO, TYPE_DIAB = range(8)
# Étapes de « Que manger aujourd'hui ? »
REGION, REPAS_CHOIX, MANGE = range(10, 13)
# Prénom et avis
PRENOM, AVIS = 20, 21

# ---------------------------------------------------------------------------
# Options proposées (boutons)
# ---------------------------------------------------------------------------
SEXES = ["Femme", "Homme"]
ACTIVITES = [
    ("Peu active", 1.2),
    ("Légèrement active", 1.375),
    ("Modérément active", 1.55),
    ("Très active", 1.725),
]
LIBELLES_ACTIVITE = [a[0] for a in ACTIVITES]
OBJECTIFS = ["Perdre du poids", "Améliorer ma santé", "Garder mon poids"]
PATHOS = [
    "Aucune",
    "Diabète",
    "Hypertension",
    "Foie gras",
    "Enceinte ou allaitante",
]
TYPES_DIABETE = ["Type 1", "Type 2", "Prédiabète", "Diabète de grossesse", "Je ne sais pas"]
TYPE_GESTATIONNEL = "Diabète de grossesse"
PATHO_GROSSESSE = "Enceinte ou allaitante"
MENU = "__menu__"  # repère : « afficher le menu adapté à la personne »


def liste_patho(patho) -> list:
    """« Diabète, Hypertension » -> ['Diabète', 'Hypertension'] (gère l'ancien format)."""
    if not patho:
        return []
    if patho == "Diabète et hypertension":  # ancien profil à valeur unique
        return ["Diabète", "Hypertension"]
    return [x.strip() for x in patho.split(",") if x.strip()]


def a_diabete(patho) -> bool:
    return "Diabète" in liste_patho(patho)


def a_hypertension(patho) -> bool:
    return "Hypertension" in liste_patho(patho)


def grossesse(p) -> bool:
    """Enceinte ou allaitante, ou diabète de grossesse."""
    return PATHO_GROSSESSE in liste_patho(p["patho"]) or p.get("type_diabete") == TYPE_GESTATIONNEL


MSG_GROSSESSE = (
    "Pendant la grossesse et l'allaitement, et encore plus avec un diabète, "
    "l'alimentation doit être suivie de près par votre médecin ou votre "
    "sage-femme. Je ne propose donc ni plan de repas ni idée de repas pour "
    "le moment."
)

TXT_SEXE = "Vous êtes :"
TXT_ACTIVITE = (
    "Quel est votre niveau d'activité au quotidien ?\n\n"
    "- Peu active : assis la plupart du temps\n"
    "- Légèrement active : marche, tâches légères\n"
    "- Modérément active : travail physique ou sport 3 à 5 fois par semaine\n"
    "- Très active : travail très physique ou sport tous les jours"
)
TXT_OBJECTIF = "Quel est votre objectif principal ?"
TXT_PATHO = (
    "Avez-vous un ou plusieurs problèmes de santé à prendre en compte ?\n"
    "Appuyez sur chacun de ceux qui vous concernent, puis sur « Valider »."
)
TXT_TYPE = (
    "Quel type de diabète avez-vous ?\n"
    "Si vous ne le savez pas, choisissez « Je ne sais pas » et demandez-le "
    "à votre médecin."
)

AVERTISSEMENT = (
    "Ce bot donne des conseils généraux d'alimentation. Il ne remplace pas "
    "un médecin ou un nutritionniste."
)
AVERTISSEMENT_PLAN = (
    "Ce plan est indicatif. Adaptez-le avec votre médecin, surtout si vous "
    "prenez de l'insuline ou des médicaments."
)

ACCUEIL = (
    "Bonjour, je suis MiaFood, votre assistant pour mieux manger.\n\n"
    "Que voulez-vous faire ?"
)
ACCUEIL_NOUVEAU = (
    "Bonjour, je suis MiaFood, votre assistant pour mieux manger.\n\n"
    "Pour commencer, créons votre profil : cela me permet de vous donner des "
    "conseils adaptés à vous. Cela prend environ deux minutes."
)

# Conseils généraux par situation. À REMPLACER par des règles validées par un
# professionnel de santé avant d'ouvrir le bot à du public.
CONSEILS = {
    "Aucune": "Variez vos repas : légumes, protéines (poisson, œufs, haricots), "
    "féculents en portion modérée, et peu de boissons sucrées.",
    "Diabète": "Privilégiez les aliments qui font peu monter le sucre (légumes, "
    "haricots, poisson) et limitez les boissons sucrées. Répartissez les "
    "féculents sur la journée.",
    "Hypertension": "Limitez le sel, les cubes d'assaisonnement et les aliments "
    "très salés (poisson fumé salé, charcuterie). Mangez plus de légumes et de "
    "fruits.",
    "Foie gras": "Réduisez les boissons sucrées, l'alcool et les fritures. "
    "Privilégiez légumes, poisson et cuissons à l'eau ou grillées.",
}


def conseils_pour(patho) -> str:
    """Rassemble les conseils de toutes les situations de la personne."""
    conditions = [c for c in liste_patho(patho) if c in CONSEILS and c != "Aucune"]
    if not conditions:
        return CONSEILS["Aucune"]
    return " ".join(CONSEILS[c] for c in conditions) + (
        " Suivez toujours les consignes de votre médecin."
    )

# ---------------------------------------------------------------------------
# Plans de repas (fichier plans.json, extrait de l'onglet Plans_Repas)
# ---------------------------------------------------------------------------
# Plans masqués en attendant une validation médicale (jeûne). Les plans de
# diabète gestationnel ne sont jamais servis : le bot ne traite pas la grossesse.
PLANS_EN_ATTENTE = {"PLAN-022", "PLAN-023"}
TYPES_PLANS_SERVIS = {"Type 1", "Type 2", "Prédiabète", "Type 2 + Hypertension"}


def charger_plans() -> dict:
    try:
        with open(PLANS_PATH, encoding="utf-8") as fh:
            plans = json.load(fh)
    except (OSError, ValueError):
        log.error("plans.json introuvable ou invalide : aucun plan disponible")
        return {}
    return {
        p["id"]: p
        for p in plans
        if p["id"] not in PLANS_EN_ATTENTE and p["type"] in TYPES_PLANS_SERVIS
    }


PLANS = charger_plans()


def plans_pour(p) -> list:
    """Plans adaptés à un profil (ligne SQLite ou dict)."""
    if not a_diabete(p["patho"]) or grossesse(p):
        return []
    type_diab = p["type_diabete"] or ""
    if type_diab not in ("Type 1", "Type 2", "Prédiabète"):
        return []
    types = [type_diab]
    if type_diab == "Type 2" and a_hypertension(p["patho"]):
        types = ["Type 2 + Hypertension", "Type 2"]
    return [pl for t in types for pl in PLANS.values() if pl["type"] == t]


def plan_par_defaut(liste: list, p):
    if a_hypertension(p["patho"]):
        for pl in liste:
            if pl["type"] == "Type 2 + Hypertension":
                return pl
    for pl in liste:
        if pl["situation"].lower().startswith("jour standard"):
            return pl
    return liste[0]


def texte_plan(pl: dict, p) -> str:
    r = pl["repas"]
    t = (
        f"Plan du jour : {pl['situation']}\n\n"
        f"7h00, petit-déjeuner : {r['petit_dejeuner']}\n"
        f"10h00, collation : {r['collation_matin']}\n"
        f"13h00, déjeuner : {r['dejeuner']}\n"
        f"16h00, collation : {r['collation_apres_midi']}\n"
        f"19h00, dîner : {r['diner']}\n\n"
        f"IG moyen estimé : {pl['ig']}"
    )
    if a_hypertension(p["patho"]) and pl["type"] != "Type 2 + Hypertension":
        t += "\nPensez aussi à limiter le sel."
    return t + "\n\n" + AVERTISSEMENT_PLAN


# ---------------------------------------------------------------------------
# Base de données (voir db.py : Postgres si DATABASE_URL existe, sinon SQLite)
# ---------------------------------------------------------------------------
init_db = db.init_db
lire_profil = db.lire_profil
sauver_profil = db.sauver_profil
lire_region = db.lire_region
sauver_region = db.sauver_region


# ---------------------------------------------------------------------------
# Calculs (faits par le code, jamais par l'IA)
# ---------------------------------------------------------------------------
def calcul_imc(poids: float, taille_cm: float) -> float:
    return round(poids / ((taille_cm / 100) ** 2), 1)


def categorie_imc(imc: float) -> str:
    if imc < 18.5:
        return "insuffisance pondérale"
    if imc < 25:
        return "poids normal"
    if imc < 30:
        return "surpoids"
    return "obésité"


def calcul_calories(sexe, age, taille, poids, facteur, objectif, imc):
    """Formule de Mifflin-St Jeor. Retourne (kcal, note)."""
    base = 10 * poids + 6.25 * taille - 5 * age + (5 if sexe == "Homme" else -161)
    besoin = base * facteur
    note = ""
    if objectif == "Perdre du poids":
        if imc < 18.5:
            cible = besoin
            note = (
                "Votre IMC est bas : je ne recommande pas de perdre du poids. "
                "Parlez-en à un professionnel de santé."
            )
        else:
            cible = besoin * 0.85  # déficit modéré d'environ 15 %
            plancher = 1500 if sexe == "Homme" else 1200
            if cible < plancher:
                cible = plancher
                note = "J'ai appliqué un minimum de sécurité pour ne pas trop réduire."
    else:
        cible = besoin
    return int(round(cible / 10) * 10), note


# ---------------------------------------------------------------------------
# Utilitaires : boutons et envoi de messages
# ---------------------------------------------------------------------------
def boutons(prefixe, options, colonnes=1, annuler=True):
    """Boutons attachés au message. callback_data = prefixe:numéro."""
    lignes, ligne = [], []
    for i, texte in enumerate(options):
        ligne.append(InlineKeyboardButton(texte, callback_data=f"{prefixe}:{i}"))
        if len(ligne) == colonnes:
            lignes.append(ligne)
            ligne = []
    if ligne:
        lignes.append(ligne)
    if annuler:
        lignes.append([InlineKeyboardButton("Annuler", callback_data="annuler")])
    return InlineKeyboardMarkup(lignes)


def menu_principal(a_profil: bool = True):
    """Menu complet si la personne a un profil, sinon invitation à le créer."""
    if not a_profil:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Créer mon profil", callback_data="creer")],
                [InlineKeyboardButton("Aide", callback_data="aide")],
            ]
        )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Voir mon profil", callback_data="profil")],
            [InlineKeyboardButton("Modifier mon profil", callback_data="creer")],
            [InlineKeyboardButton("Mon plan de repas", callback_data="plan")],
            [InlineKeyboardButton("Que manger aujourd'hui ?", callback_data="repas")],
            [InlineKeyboardButton("Donner mon avis", callback_data="avis")],
            [InlineKeyboardButton("Aide", callback_data="aide")],
        ]
    )


def boutons_patho(selection: set):
    """Choix multiple : chaque bouton s'active ou se désactive, puis « Valider »."""
    lignes = [
        [InlineKeyboardButton(("✔ " if i in selection else "") + nom, callback_data=f"pat:{i}")]
        for i, nom in enumerate(PATHOS)
    ]
    lignes.append([InlineKeyboardButton("Valider", callback_data="pat:ok")])
    lignes.append([InlineKeyboardButton("Annuler", callback_data="annuler")])
    return InlineKeyboardMarkup(lignes)


def boutons_profil():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Supprimer mes données", callback_data="suppr")],
            [InlineKeyboardButton("Menu", callback_data="accueil")],
        ]
    )


def boutons_plan(autres_situations: bool):
    lignes = []
    if autres_situations:
        lignes.append([InlineKeyboardButton("Autre situation", callback_data="sits")])
    lignes.append([InlineKeyboardButton("Menu", callback_data="accueil")])
    return InlineKeyboardMarkup(lignes)


async def repondre(update: Update, texte: str, markup=None) -> None:
    """Envoie un message, que l'on vienne d'un texte ou d'un bouton."""
    if isinstance(markup, str) and markup == MENU:
        markup = menu_principal(lire_profil(update.effective_user.id) is not None)
    await update.effective_chat.send_message(texte, reply_markup=markup)


async def lire_choix(update: Update, options):
    """Lit le bouton appuyé, l'affiche dans le message et retire les boutons."""
    q = update.callback_query
    await q.answer()
    try:
        i = int(q.data.split(":")[1])
        choix = options[i]
    except (ValueError, IndexError):
        return None
    try:
        await q.edit_message_text(f"{q.message.text}\n\n✔ {choix}")
    except Exception:  # message déjà modifié
        pass
    return i


def nombre(texte: str):
    try:
        return float(texte.replace(",", ".").strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Accueil, profil, plan de repas (sans commandes à taper)
# ---------------------------------------------------------------------------
async def accueil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    prenom = db.lire_prenom(uid)
    if lire_profil(uid) is None:
        texte = ACCUEIL_NOUVEAU
    elif prenom:
        texte = f"Bonjour {prenom}, content de vous revoir.\n\nQue voulez-vous faire ?"
    else:
        texte = "Bonjour, content de vous revoir.\n\nQue voulez-vous faire ?"
    await repondre(update, texte, MENU)


async def afficher_profil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    p = lire_profil(update.effective_user.id)
    if p is None:
        await repondre(update, "Vous n'avez pas encore de profil.", MENU)
        return
    region = lire_region(update.effective_user.id)
    sante = p["patho"]
    if p["type_diabete"]:
        sante += f" ({p['type_diabete']})"
    await repondre(
        update,
        (f"Profil de {p['prenom']} :\n" if p["prenom"] else "Votre profil :\n")
        + f"{p['sexe']}, {p['age']} ans, {p['taille']:.0f} cm, {p['poids']:.1f} kg\n"
        f"Activité : {p['activite']}\n"
        f"Objectif : {p['objectif']}\n"
        f"Situation de santé : {sante}\n"
        + (f"Région : {region}\n" if region else "")
        + (
            f"IMC : {p['imc']} - Besoin estimé : environ {p['kcal']} kcal/jour"
            if p["kcal"] else f"IMC : {p['imc']}"
        ),
        boutons_profil(),
    )


async def afficher_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    p = lire_profil(update.effective_user.id)
    if p is None:
        await repondre(update, "Créez d'abord votre profil.", MENU)
        return

    liste = plans_pour(p)
    if not liste:
        if grossesse(p):
            message = MSG_GROSSESSE
        elif a_diabete(p["patho"]):
            message = (
                "Pour vous proposer un plan, j'ai besoin de connaître votre type "
                "de diabète. Demandez-le à votre médecin, puis mettez à jour "
                "votre profil."
            )
        else:
            message = (
                "Pour l'instant, les plans de repas sont disponibles pour les "
                "personnes diabétiques (Type 1, Type 2, prédiabète). Les autres "
                "profils arriveront bientôt."
            )
        await repondre(update, message, MENU)
        return

    pl = plan_par_defaut(liste, p)
    await repondre(update, texte_plan(pl, p), boutons_plan(len(liste) > 1))


async def liste_situations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    p = lire_profil(update.effective_user.id)
    liste = plans_pour(p) if p else []
    if not liste:
        await afficher_plan(update, context)
        return
    lignes = [
        [InlineKeyboardButton(pl["situation"], callback_data=f"pl:{pl['id']}")]
        for pl in liste
    ]
    lignes.append([InlineKeyboardButton("Menu", callback_data="accueil")])
    await repondre(update, "Choisissez votre situation :", InlineKeyboardMarkup(lignes))


async def montrer_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    plan_id = q.data.split(":", 1)[1]
    p = lire_profil(update.effective_user.id)
    liste = plans_pour(p) if p else []
    pl = next((x for x in liste if x["id"] == plan_id), None)
    if pl is None:
        await afficher_plan(update, context)
        return
    await repondre(update, texte_plan(pl, p), boutons_plan(len(liste) > 1))


async def afficher_aide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await repondre(
        update,
        "Utilisez les boutons du menu pour créer votre profil, le consulter, "
        "voir votre plan de repas ou demander une idée de repas selon ce que "
        "vous avez déjà mangé. Pour revenir au menu à tout moment, "
        "envoyez simplement un message.\n\n" + AVERTISSEMENT,
        MENU,
    )


async def bouton_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if q.data == "profil":
        await afficher_profil(update, context)
    elif q.data == "plan":
        await afficher_plan(update, context)
    elif q.data == "aide":
        await afficher_aide(update, context)
    elif q.data == "accueil":
        prenom = db.lire_prenom(update.effective_user.id)
        await repondre(
            update,
            f"Que voulez-vous faire{', ' + prenom if prenom else ''} ?",
            MENU,
        )


# ---------------------------------------------------------------------------
# Questionnaire
# ---------------------------------------------------------------------------
MENTION_DONNEES = (
    "Vos réponses sont enregistrées pour personnaliser vos conseils. Vous "
    "pouvez les effacer à tout moment (bouton « Supprimer mes données » dans "
    "votre profil)."
)
_PRENOM_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ' -]{0,29}$")


async def demarrer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    prenom = db.lire_prenom(update.effective_user.id)
    context.user_data.clear()
    await repondre(
        update,
        "Parfait, quelques questions pour créer votre profil.\n\n"
        + AVERTISSEMENT + "\n\n" + MENTION_DONNEES,
    )
    if prenom:  # déjà connu : on ne le redemande pas
        context.user_data["prenom"] = prenom
        await repondre(update, TXT_SEXE, boutons("sexe", SEXES, 2))
        return SEXE
    await repondre(update, "Comment dois-je vous appeler ? Écrivez votre prénom.")
    return PRENOM


async def recevoir_prenom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    saisie = " ".join(update.message.text.split())
    if not _PRENOM_RE.match(saisie):
        await update.message.reply_text(
            "Écrivez seulement votre prénom, avec des lettres (30 caractères au maximum)."
        )
        return PRENOM
    prenom = saisie.title() if (saisie.islower() or saisie.isupper()) else saisie
    context.user_data["prenom"] = prenom
    await update.message.reply_text(f"Enchanté {prenom} !")
    await update.message.reply_text(TXT_SEXE, reply_markup=boutons("sexe", SEXES, 2))
    return SEXE


async def recevoir_sexe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    i = await lire_choix(update, SEXES)
    if i is None:
        return SEXE
    context.user_data["sexe"] = SEXES[i]
    await repondre(
        update,
        "Quel est votre âge (en années) ?\n"
        "(Vous pouvez écrire « annuler » pour arrêter.)",
    )
    return AGE


async def recevoir_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    age = nombre(update.message.text)
    if age is None or not 5 <= age <= 110:
        await update.message.reply_text("Entrez votre âge avec des chiffres, par exemple 35.")
        return AGE
    if age < 18:
        await repondre(
            update,
            "Pour les moins de 18 ans, l'alimentation doit être suivie par un "
            "professionnel de santé. Je ne peux pas créer de plan ici. "
            "Parlez-en à un médecin ou à un nutritionniste.",
            MENU,
        )
        return ConversationHandler.END
    context.user_data["age"] = int(age)
    await update.message.reply_text("Quelle est votre taille en cm ? (ex. 170)")
    return TAILLE


async def recevoir_taille(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    taille = nombre(update.message.text)
    if taille is None or not 120 <= taille <= 220:
        await update.message.reply_text("Entrez votre taille en cm, entre 120 et 220.")
        return TAILLE
    context.user_data["taille"] = taille
    await update.message.reply_text("Quel est votre poids en kg ? (ex. 72)")
    return POIDS


async def recevoir_poids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    poids = nombre(update.message.text)
    if poids is None or not 30 <= poids <= 250:
        await update.message.reply_text("Entrez votre poids en kg, entre 30 et 250.")
        return POIDS
    context.user_data["poids"] = poids
    await update.message.reply_text(
        TXT_ACTIVITE, reply_markup=boutons("act", LIBELLES_ACTIVITE)
    )
    return ACTIVITE


async def recevoir_activite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    i = await lire_choix(update, LIBELLES_ACTIVITE)
    if i is None:
        return ACTIVITE
    context.user_data["activite"] = LIBELLES_ACTIVITE[i]
    context.user_data["facteur"] = ACTIVITES[i][1]
    await repondre(update, TXT_OBJECTIF, boutons("obj", OBJECTIFS))
    return OBJECTIF


async def recevoir_objectif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    i = await lire_choix(update, OBJECTIFS)
    if i is None:
        return OBJECTIF
    context.user_data["objectif"] = OBJECTIFS[i]
    context.user_data["patho_sel"] = set()
    await repondre(update, TXT_PATHO, boutons_patho(set()))
    return PATHO


async def recevoir_patho(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Choix multiple : chaque appui active ou désactive une situation."""
    q = update.callback_query
    sel = context.user_data.setdefault("patho_sel", set())
    action = q.data.split(":", 1)[1]

    if action != "ok":
        try:
            i = int(action)
            PATHOS[i]
        except (ValueError, IndexError):
            await q.answer()
            return PATHO
        if i == 0:  # « Aucune » annule tout le reste
            deja = 0 in sel
            sel.clear()
            if not deja:
                sel.add(0)
        else:
            sel.discard(0)
            sel.symmetric_difference_update({i})
        await q.answer()
        try:
            await q.edit_message_reply_markup(boutons_patho(sel))
        except Exception:
            pass
        return PATHO

    if not sel:
        await q.answer("Choisissez au moins une réponse (ou « Aucune »).", show_alert=True)
        return PATHO
    await q.answer()
    choisis = [PATHOS[i] for i in sorted(sel)]
    try:
        await q.edit_message_text(f"{TXT_PATHO}\n\n✔ " + ", ".join(choisis))
    except Exception:
        pass
    context.user_data["patho"] = ", ".join(choisis)
    context.user_data["type_diabete"] = ""
    context.user_data.pop("patho_sel", None)
    if "Diabète" in choisis:
        await repondre(update, TXT_TYPE, boutons("typ", TYPES_DIABETE))
        return TYPE_DIAB
    return await terminer_profil(update, context)


async def rappel_patho(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sel = context.user_data.setdefault("patho_sel", set())
    await update.message.reply_text("Merci d'appuyer sur les boutons ci-dessous.")
    await update.message.reply_text(TXT_PATHO, reply_markup=boutons_patho(sel))
    return PATHO


async def recevoir_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    i = await lire_choix(update, TYPES_DIABETE)
    if i is None:
        return TYPE_DIAB
    context.user_data["type_diabete"] = (
        "" if TYPES_DIABETE[i] == "Je ne sais pas" else TYPES_DIABETE[i]
    )
    return await terminer_profil(update, context)


async def terminer_profil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = context.user_data
    d["imc"] = calcul_imc(d["poids"], d["taille"])
    enceinte = grossesse(d)
    if enceinte:
        d["kcal"], note = 0, ""  # pas d'objectif calorique pendant la grossesse
    else:
        d["kcal"], note = calcul_calories(
            d["sexe"], d["age"], d["taille"], d["poids"],
            d["facteur"], d["objectif"], d["imc"],
        )
    sauver_profil(update.effective_user.id, d)

    debut = (
        f"Merci {d['prenom']}, votre profil est enregistré.\n\n"
        if d.get("prenom") else "Votre profil est enregistré.\n\n"
    )
    if enceinte:
        message = debut + MSG_GROSSESSE
    else:
        message = debut + (
            f"IMC : {d['imc']} ({categorie_imc(d['imc'])})\n"
            f"Besoin estimé : environ {d['kcal']} kcal par jour\n\n"
            f"{conseils_pour(d['patho'])}\n"
        )
        if note:
            message += f"\n{note}\n"
        if a_diabete(d["patho"]) and not d["type_diabete"]:
            message += (
                "\nDemandez à votre médecin votre type de diabète, puis mettez à "
                "jour votre profil pour recevoir un plan de repas.\n"
            )
    message += f"\n{AVERTISSEMENT}"
    await repondre(update, message, MENU)
    return ConversationHandler.END


async def annuler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_reply_markup(None)
        except Exception:
            pass
    context.user_data.clear()
    await repondre(update, "D'accord, on arrête ici.", MENU)
    return ConversationHandler.END


async def start_et_fin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/start pendant le questionnaire : retour au menu."""
    context.user_data.clear()
    await accueil(update, context)
    return ConversationHandler.END


def rappel_boutons(texte, prefixe, options, colonnes, etat):
    """Si la personne écrit au lieu d'appuyer sur un bouton, on réaffiche la question."""

    async def _rappel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text("Merci d'appuyer sur l'un des boutons ci-dessous.")
        await update.message.reply_text(
            texte, reply_markup=boutons(prefixe, options, colonnes)
        )
        return etat

    return _rappel


# ---------------------------------------------------------------------------
# Que manger aujourd'hui ? (idée de repas pour les personnes diabétiques)
# ---------------------------------------------------------------------------
CHOIX_REPAS = ["Déjeuner", "Dîner"]
TXT_REGION = (
    "Dans quelle région du Cameroun vivez-vous ?\n"
    "Je choisis des aliments qu'on y trouve facilement."
)
TXT_REPAS = "Pour quel repas voulez-vous une idée ?"


def boutons_mange():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Je n'ai encore rien mangé", callback_data="rien")],
            [InlineKeyboardButton("Annuler", callback_data="annuler")],
        ]
    )


def boutons_proposition():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Autre proposition", callback_data="autre")],
            [InlineKeyboardButton("Corriger ce que j'ai mangé", callback_data="corr")],
            [InlineKeyboardButton("Changer ma région", callback_data="chreg")],
            [InlineKeyboardButton("Menu", callback_data="accueil")],
        ]
    )


def texte_question_mange(repas_nom: str) -> str:
    return (
        f"Qu'avez-vous mangé aujourd'hui avant ce {repas_nom.lower()} ?\n"
        "Écrivez les aliments séparés par des virgules (par exemple : "
        "bouillie de mil, plantain, poisson braisé, gombo)."
    )


def _etat_repas(context) -> dict:
    return context.user_data.setdefault(
        "repas", {"meal": None, "eaten": [], "inconnus": [], "essai": 0, "derniere": None}
    )


async def _ack(update: Update) -> None:
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:  # déjà acquitté
            pass


def texte_proposition(prop: dict, d: dict, region: str) -> str:
    base, prot, legumes = prop["base"], prop["proteine"], prop["legumes"]
    lignes = [
        (f"{d['prenom']}, voici" if d.get("prenom") else "Voici")
        + f" une idée pour ce {d['meal'].lower()} (région {region}) :",
        "",
        f"- Base : {base['nom']} ({repas.portion_affichee(base)})",
        f"- Protéine : {prot['nom']} ({repas.portion_affichee(prot)})",
        "- Légumes : "
        + " et ".join(
            f"{repas.nom_affiche(x)} ({repas.portion_affichee(x)})" for x in legumes
        ),
        "- Boisson : eau",
        "",
        "Pourquoi ce choix :",
    ]
    lignes += [f"- {r}" for r in prop["raisons"]]
    mange = [repas.ALIMENTS[i]["nom"] for i in d["eaten"] if i in repas.ALIMENTS]
    if mange:
        lignes += ["", "Pris en compte aujourd'hui : " + ", ".join(mange)]
    if d["inconnus"]:
        lignes += [
            "",
            "Non reconnu (ignoré) : " + ", ".join(d["inconnus"]) + ". "
            "Utilisez « Corriger ce que j'ai mangé » pour le reformuler.",
        ]
    lignes += ["", AVERTISSEMENT_PLAN]
    return "\n".join(lignes)


async def proposer_repas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    p = lire_profil(uid)
    region = lire_region(uid)
    d = _etat_repas(context)
    d["prenom"] = p["prenom"] if p else None
    if p is None or not region or not d.get("meal"):
        await repondre(
            update,
            "Je n'ai plus les informations de cette conversation. Relancez "
            "« Que manger aujourd'hui ? ».",
            MENU,
        )
        return
    mange = [repas.ALIMENTS[i] for i in d["eaten"] if i in repas.ALIMENTS]
    graine = uid + date.today().toordinal()
    essai, prop = d["essai"], None
    for _ in range(8):  # on évite de répéter la proposition précédente
        prop = repas.proposer(p["patho"], region, mange, essai, graine)
        if prop is None or prop["signature"] != d["derniere"]:
            break
        essai += 1
    d["essai"] = essai
    if prop is None:
        await repondre(
            update,
            "Je n'ai pas trouvé de combinaison adaptée. Parlez-en à un "
            "nutritionniste.",
            MENU,
        )
        return
    d["derniere"] = prop["signature"]
    await repondre(update, texte_proposition(prop, d, region), boutons_proposition())


async def repas_debut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack(update)
    uid = update.effective_user.id
    p = lire_profil(uid)
    if p is None:
        await repondre(update, "Créez d'abord votre profil.", MENU)
        return ConversationHandler.END
    if grossesse(p):
        await repondre(update, MSG_GROSSESSE, MENU)
        return ConversationHandler.END
    if not a_diabete(p["patho"]):
        await repondre(
            update,
            "Pour l'instant, cette fonction est disponible pour les personnes "
            "diabétiques. Les autres profils arriveront bientôt.",
            MENU,
        )
        return ConversationHandler.END
    context.user_data.pop("repas", None)
    _etat_repas(context)
    if not lire_region(uid):
        await repondre(update, TXT_REGION, boutons("reg", repas.REGIONS, 2))
        return REGION
    await repondre(update, TXT_REPAS, boutons("meal", CHOIX_REPAS, 2))
    return REPAS_CHOIX


async def changer_region(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack(update)
    _etat_repas(context)
    await repondre(update, TXT_REGION, boutons("reg", repas.REGIONS, 2))
    return REGION


async def recevoir_region(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    i = await lire_choix(update, repas.REGIONS)
    if i is None:
        return REGION
    sauver_region(update.effective_user.id, repas.REGIONS[i])
    await repondre(update, TXT_REPAS, boutons("meal", CHOIX_REPAS, 2))
    return REPAS_CHOIX


async def recevoir_repas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    i = await lire_choix(update, CHOIX_REPAS)
    if i is None:
        return REPAS_CHOIX
    d = _etat_repas(context)
    d.update({"meal": CHOIX_REPAS[i], "eaten": [], "inconnus": [], "essai": 0, "derniere": None})
    await repondre(update, texte_question_mange(d["meal"]), boutons_mange())
    return MANGE


async def recevoir_mange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    trouves, inconnus = repas.analyser_texte(update.message.text)
    d = _etat_repas(context)
    d.update(
        {"eaten": [a["id"] for a in trouves], "inconnus": inconnus, "essai": 0, "derniere": None}
    )
    await proposer_repas(update, context)
    return ConversationHandler.END


async def rien_mange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack(update)
    try:
        await update.callback_query.edit_message_reply_markup(None)
    except Exception:
        pass
    d = _etat_repas(context)
    d.update({"eaten": [], "inconnus": [], "essai": 0, "derniere": None})
    await proposer_repas(update, context)
    return ConversationHandler.END


async def corriger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack(update)
    d = context.user_data.get("repas")
    if not d or not d.get("meal"):
        return await repas_debut(update, context)
    await repondre(update, texte_question_mange(d["meal"]), boutons_mange())
    return MANGE


async def autre_proposition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ack(update)
    d = context.user_data.get("repas")
    if not d or not d.get("meal"):
        await repondre(
            update,
            "Relancez « Que manger aujourd'hui ? » pour une nouvelle idée.",
            MENU,
        )
        return
    d["essai"] += 1
    await proposer_repas(update, context)


# ---------------------------------------------------------------------------
# Accès par code, avis, suppression des données, commandes d'administration
# ---------------------------------------------------------------------------
async def verifier_acces(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Si un code d'invitation est défini, bloque tant que la personne ne l'a pas saisi."""
    if not CODE_TEST:
        return
    user = update.effective_user
    if user is None or user.id == ADMIN_ID or db.a_acces(user.id):
        return
    msg = update.message
    if msg and msg.text:
        saisie = msg.text.strip()
        if saisie.lower().startswith("/monid"):
            return
        if saisie.lower() == CODE_TEST.lower():
            db.donner_acces(user.id)
            await msg.reply_text("Code accepté, bienvenue !")
            await accueil(update, context)
            raise ApplicationHandlerStop
    if update.callback_query:
        await update.callback_query.answer()
    await repondre(
        update,
        "Ce bot est en phase de test. Pour l'utiliser, écrivez votre code "
        "d'invitation.",
    )
    raise ApplicationHandlerStop


async def monid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Votre identifiant Telegram : {update.effective_user.id}"
    )


def _est_admin(update: Update) -> bool:
    return bool(ADMIN_ID) and update.effective_user.id == ADMIN_ID


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _est_admin(update):
        await update.message.reply_text("Commande réservée à l'administrateur.")
        return
    s = db.statistiques()
    await update.message.reply_text(
        f"Profils complets : {s['profils']}\n"
        f"Personnes ayant saisi le code : {s['acces']}\n"
        f"Avis reçus : {s['avis']}"
    )


async def avis_liste(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _est_admin(update):
        await update.message.reply_text("Commande réservée à l'administrateur.")
        return
    liste = db.derniers_avis(15)
    if not liste:
        await update.message.reply_text("Aucun avis pour l'instant.")
        return
    lignes = [f"{d:%d/%m %H:%M} - {prenom} : {texte}" for d, prenom, texte in liste]
    await update.message.reply_text("\n\n".join(lignes)[:3900])


async def avis_debut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _ack(update)
    await repondre(
        update,
        "Votre avis m'aide à améliorer MiaFood. Écrivez-le en un seul message : "
        "ce qui vous a plu, ce qui vous a bloqué ou ce qui manque.",
        InlineKeyboardMarkup([[InlineKeyboardButton("Annuler", callback_data="annuler")]]),
    )
    return AVIS


async def recevoir_avis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db.ajouter_avis(update.effective_user.id, update.message.text.strip()[:1500])
    await repondre(update, "Merci pour votre avis, il est bien enregistré.", MENU)
    return ConversationHandler.END


async def suppr_demande(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ack(update)
    await repondre(
        update,
        "Voulez-vous vraiment supprimer votre profil et vos avis ? "
        "Cette action est définitive.",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Oui, tout supprimer", callback_data="suppr_ok")],
                [InlineKeyboardButton("Non, garder", callback_data="accueil")],
            ]
        ),
    )


async def suppr_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ack(update)
    db.supprimer_utilisateur(update.effective_user.id)
    context.user_data.clear()
    await repondre(
        update,
        "Vos données ont été supprimées. Vous pouvez recréer un profil quand vous voulez.",
        MENU,
    )


# ---------------------------------------------------------------------------
# Construction de l'application
# ---------------------------------------------------------------------------
def construire_app(token: str) -> Application:
    app = Application.builder().token(token).build()

    # Texte libre, hors commandes et hors le mot « annuler »
    texte = (
        filters.TEXT
        & ~filters.COMMAND
        & ~filters.Regex(re.compile(r"^\s*annuler\s*$", re.IGNORECASE))
    )

    # Contrôle d'accès : s'exécute avant tout le reste
    app.add_handler(MessageHandler(filters.ALL, verifier_acces), group=-1)
    app.add_handler(CallbackQueryHandler(verifier_acces), group=-1)

    # Une seule conversation : on peut passer d'un parcours à un autre à tout
    # moment (allow_reentry), et les boutons du menu la terminent proprement.
    def puis_fin(fonction):
        async def _executer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
            await fonction(update, context)
            return ConversationHandler.END

        return _executer

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(demarrer, pattern="^creer$"),
                CommandHandler("creer", demarrer),
                CallbackQueryHandler(repas_debut, pattern="^repas$"),
                CommandHandler("repas", repas_debut),
                CallbackQueryHandler(corriger, pattern="^corr$"),
                CallbackQueryHandler(changer_region, pattern="^chreg$"),
                CallbackQueryHandler(avis_debut, pattern="^avis$"),
                CommandHandler("avis", avis_debut),
            ],
            states={
                PRENOM: [MessageHandler(texte, recevoir_prenom)],
                SEXE: [
                    CallbackQueryHandler(recevoir_sexe, pattern="^sexe:"),
                    MessageHandler(texte, rappel_boutons(TXT_SEXE, "sexe", SEXES, 2, SEXE)),
                ],
                AGE: [MessageHandler(texte, recevoir_age)],
                TAILLE: [MessageHandler(texte, recevoir_taille)],
                POIDS: [MessageHandler(texte, recevoir_poids)],
                ACTIVITE: [
                    CallbackQueryHandler(recevoir_activite, pattern="^act:"),
                    MessageHandler(
                        texte,
                        rappel_boutons(TXT_ACTIVITE, "act", LIBELLES_ACTIVITE, 1, ACTIVITE),
                    ),
                ],
                OBJECTIF: [
                    CallbackQueryHandler(recevoir_objectif, pattern="^obj:"),
                    MessageHandler(
                        texte, rappel_boutons(TXT_OBJECTIF, "obj", OBJECTIFS, 1, OBJECTIF)
                    ),
                ],
                PATHO: [
                    CallbackQueryHandler(recevoir_patho, pattern="^pat:"),
                    MessageHandler(texte, rappel_patho),
                ],
                TYPE_DIAB: [
                    CallbackQueryHandler(recevoir_type, pattern="^typ:"),
                    MessageHandler(
                        texte, rappel_boutons(TXT_TYPE, "typ", TYPES_DIABETE, 1, TYPE_DIAB)
                    ),
                ],
                REGION: [
                    CallbackQueryHandler(recevoir_region, pattern="^reg:"),
                    MessageHandler(
                        texte, rappel_boutons(TXT_REGION, "reg", repas.REGIONS, 2, REGION)
                    ),
                ],
                REPAS_CHOIX: [
                    CallbackQueryHandler(recevoir_repas, pattern="^meal:"),
                    MessageHandler(
                        texte, rappel_boutons(TXT_REPAS, "meal", CHOIX_REPAS, 2, REPAS_CHOIX)
                    ),
                ],
                MANGE: [
                    CallbackQueryHandler(rien_mange, pattern="^rien$"),
                    MessageHandler(texte, recevoir_mange),
                ],
                AVIS: [MessageHandler(texte, recevoir_avis)],
            },
            fallbacks=[
                CallbackQueryHandler(annuler, pattern="^annuler$"),
                CommandHandler("annuler", annuler),
                MessageHandler(
                    filters.Regex(re.compile(r"^\s*annuler\s*$", re.IGNORECASE)), annuler
                ),
                CommandHandler("start", start_et_fin),
                CommandHandler("menu", start_et_fin),
                CallbackQueryHandler(
                    puis_fin(bouton_menu), pattern="^(profil|plan|aide|accueil)$"
                ),
                CallbackQueryHandler(puis_fin(liste_situations), pattern="^sits$"),
                CallbackQueryHandler(puis_fin(montrer_plan), pattern="^pl:"),
                CallbackQueryHandler(puis_fin(autre_proposition), pattern="^autre$"),
                CallbackQueryHandler(puis_fin(suppr_demande), pattern="^suppr$"),
                CallbackQueryHandler(puis_fin(suppr_ok), pattern="^suppr_ok$"),
                CommandHandler("profil", puis_fin(afficher_profil)),
                CommandHandler("plan", puis_fin(afficher_plan)),
                CommandHandler("aide", puis_fin(afficher_aide)),
                CommandHandler("supprimer", puis_fin(suppr_demande)),
            ],
            allow_reentry=True,
        )
    )

    # Hors questionnaire
    app.add_handler(CommandHandler("start", accueil))
    app.add_handler(CommandHandler("menu", accueil))
    app.add_handler(CommandHandler("profil", afficher_profil))
    app.add_handler(CommandHandler("plan", afficher_plan))
    app.add_handler(CommandHandler("aide", afficher_aide))
    app.add_handler(CommandHandler("monid", monid))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("avis_liste", avis_liste))
    app.add_handler(CommandHandler("supprimer", suppr_demande))
    app.add_handler(CallbackQueryHandler(suppr_demande, pattern="^suppr$"))
    app.add_handler(CallbackQueryHandler(suppr_ok, pattern="^suppr_ok$"))
    app.add_handler(CallbackQueryHandler(bouton_menu, pattern="^(profil|plan|aide|accueil)$"))
    app.add_handler(CallbackQueryHandler(liste_situations, pattern="^sits$"))
    app.add_handler(CallbackQueryHandler(autre_proposition, pattern="^autre$"))
    app.add_handler(CallbackQueryHandler(montrer_plan, pattern="^pl:"))
    # Tout autre message : on affiche le menu
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, accueil))
    return app


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("La variable TELEGRAM_TOKEN est absente.")

    init_db()
    log.info("%d plans de repas chargés", len(PLANS))
    app = construire_app(token)

    # Mode webhook si une URL publique est connue (Render la fournit dans
    # RENDER_EXTERNAL_URL), sinon mode polling.
    base_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if base_url:
        port = int(os.environ.get("PORT", "10000"))
        log.info("MiaFood démarre en mode webhook sur le port %s", port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=token,
            webhook_url=f"{base_url.rstrip('/')}/{token}",
        )
    else:
        log.info("MiaFood démarre en mode polling...")
        app.run_polling()


if __name__ == "__main__":
    main()
