"""
MiaFood - assistant nutritionnel (version 3, Telegram)

Nouveautés de cette version :
- le questionnaire demande le type de diabète (Type 1, Type 2, prédiabète)
- « Mon plan de repas » affiche le plan du jour, avec un choix d'autres
  situations (jour de marché, fête, harmattan, etc.)
- les plans viennent du fichier plans.json (extrait du fichier Excel)

Lancement local :
    export TELEGRAM_TOKEN="ton_token"
    python bot.py
Sur Render, la variable RENDER_EXTERNAL_URL active le mode webhook.
"""

import json
import logging
import os
import re
import sqlite3

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
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
log = logging.getLogger("miafood")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", "miafood.db")
PLANS_PATH = os.path.join(BASE_DIR, "plans.json")

# Étapes du questionnaire
SEXE, AGE, TAILLE, POIDS, ACTIVITE, OBJECTIF, PATHO, TYPE_DIAB = range(8)

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
    "Diabète et hypertension",
    "Hypertension",
    "Foie gras",
    "Enceinte ou allaitante",
]
PATHOS_DIABETE = {"Diabète", "Diabète et hypertension"}
TYPES_DIABETE = ["Type 1", "Type 2", "Prédiabète", "Je ne sais pas"]

TXT_SEXE = "Vous êtes :"
TXT_ACTIVITE = (
    "Quel est votre niveau d'activité au quotidien ?\n\n"
    "- Peu active : assis la plupart du temps\n"
    "- Légèrement active : marche, tâches légères\n"
    "- Modérément active : travail physique ou sport 3 à 5 fois par semaine\n"
    "- Très active : travail très physique ou sport tous les jours"
)
TXT_OBJECTIF = "Quel est votre objectif principal ?"
TXT_PATHO = "Avez-vous un problème de santé à prendre en compte ?"
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

# Conseils généraux par situation. À REMPLACER par des règles validées par un
# professionnel de santé avant d'ouvrir le bot à du public.
CONSEILS = {
    "Aucune": "Variez vos repas : légumes, protéines (poisson, œufs, haricots), "
    "féculents en portion modérée, et peu de boissons sucrées.",
    "Diabète": "Privilégiez les aliments qui font peu monter le sucre (légumes, "
    "haricots, poisson) et limitez les boissons sucrées. Répartissez les "
    "féculents sur la journée. Suivez toujours les consignes de votre médecin.",
    "Diabète et hypertension": "Privilégiez les aliments qui font peu monter le "
    "sucre (légumes, haricots, poisson), limitez les boissons sucrées, et "
    "réduisez le sel et les cubes d'assaisonnement. Suivez toujours les "
    "consignes de votre médecin.",
    "Hypertension": "Limitez le sel, les cubes d'assaisonnement et les aliments "
    "très salés (poisson fumé salé, charcuterie). Mangez plus de légumes et de "
    "fruits. Suivez toujours les consignes de votre médecin.",
    "Foie gras": "Réduisez les boissons sucrées, l'alcool et les fritures. "
    "Privilégiez légumes, poisson et cuissons à l'eau ou grillées. Un avis "
    "médical reste indispensable.",
}

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
    if p["patho"] not in PATHOS_DIABETE:
        return []
    type_diab = p["type_diabete"] or ""
    if type_diab not in ("Type 1", "Type 2", "Prédiabète"):
        return []
    types = [type_diab]
    if type_diab == "Type 2" and p["patho"] == "Diabète et hypertension":
        types = ["Type 2 + Hypertension", "Type 2"]
    return [pl for t in types for pl in PLANS.values() if pl["type"] == t]


def plan_par_defaut(liste: list, p):
    if p["patho"] == "Diabète et hypertension":
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
    if p["patho"] == "Diabète et hypertension" and pl["type"] != "Type 2 + Hypertension":
        t += "\nPensez aussi à limiter le sel."
    return t + "\n\n" + AVERTISSEMENT_PLAN


# ---------------------------------------------------------------------------
# Base de données
# ---------------------------------------------------------------------------
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS profils (
                user_id INTEGER PRIMARY KEY,
                sexe TEXT, age INTEGER, taille REAL, poids REAL,
                activite TEXT, objectif TEXT, patho TEXT,
                imc REAL, kcal INTEGER, type_diabete TEXT
            )
            """
        )
        # Mise à jour d'une base créée par une version précédente
        colonnes = [c[1] for c in db.execute("PRAGMA table_info(profils)")]
        if "type_diabete" not in colonnes:
            db.execute("ALTER TABLE profils ADD COLUMN type_diabete TEXT")


def sauver_profil(user_id: int, p: dict) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT OR REPLACE INTO profils
            (user_id, sexe, age, taille, poids, activite, objectif, patho,
             imc, kcal, type_diabete)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, p["sexe"], p["age"], p["taille"], p["poids"],
                p["activite"], p["objectif"], p["patho"], p["imc"], p["kcal"],
                p.get("type_diabete", ""),
            ),
        )


def lire_profil(user_id: int):
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            "SELECT * FROM profils WHERE user_id = ?", (user_id,)
        ).fetchone()


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


def menu_principal():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Créer ou modifier mon profil", callback_data="creer")],
            [InlineKeyboardButton("Voir mon profil", callback_data="profil")],
            [InlineKeyboardButton("Mon plan de repas", callback_data="plan")],
            [InlineKeyboardButton("Aide", callback_data="aide")],
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
    await repondre(update, ACCUEIL, menu_principal())


async def afficher_profil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    p = lire_profil(update.effective_user.id)
    if p is None:
        await repondre(update, "Vous n'avez pas encore de profil.", menu_principal())
        return
    sante = p["patho"]
    if p["type_diabete"]:
        sante += f" ({p['type_diabete']})"
    await repondre(
        update,
        "Votre profil :\n"
        f"{p['sexe']}, {p['age']} ans, {p['taille']:.0f} cm, {p['poids']:.1f} kg\n"
        f"Activité : {p['activite']}\n"
        f"Objectif : {p['objectif']}\n"
        f"Situation de santé : {sante}\n"
        f"IMC : {p['imc']} - Besoin estimé : environ {p['kcal']} kcal/jour",
        menu_principal(),
    )


async def afficher_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    p = lire_profil(update.effective_user.id)
    if p is None:
        await repondre(update, "Créez d'abord votre profil.", menu_principal())
        return

    liste = plans_pour(p)
    if not liste:
        if p["patho"] in PATHOS_DIABETE:
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
        await repondre(update, message, menu_principal())
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
        "Utilisez les boutons du menu pour créer votre profil, le consulter "
        "ou voir votre plan de repas. Pour revenir au menu à tout moment, "
        "envoyez simplement un message.\n\n" + AVERTISSEMENT,
        menu_principal(),
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
        await accueil(update, context)


# ---------------------------------------------------------------------------
# Questionnaire
# ---------------------------------------------------------------------------
async def demarrer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data.clear()
    await repondre(
        update,
        "Parfait, quelques questions pour créer votre profil.\n\n" + AVERTISSEMENT,
    )
    await repondre(update, TXT_SEXE, boutons("sexe", SEXES, 2))
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
        await update.message.reply_text(
            "Pour les moins de 18 ans, l'alimentation doit être suivie par un "
            "professionnel de santé. Je ne peux pas créer de plan ici. "
            "Parlez-en à un médecin ou à un nutritionniste.",
            reply_markup=menu_principal(),
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
    await repondre(update, TXT_PATHO, boutons("pat", PATHOS))
    return PATHO


async def recevoir_patho(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    i = await lire_choix(update, PATHOS)
    if i is None:
        return PATHO
    choix = PATHOS[i]

    if choix == "Enceinte ou allaitante":
        await repondre(
            update,
            "Pendant la grossesse et l'allaitement, l'alimentation doit être "
            "suivie par un professionnel de santé. Je ne peux pas vous donner "
            "de plan ici. Parlez-en à votre sage-femme ou à votre médecin.",
            menu_principal(),
        )
        return ConversationHandler.END

    context.user_data["patho"] = choix
    context.user_data["type_diabete"] = ""
    if choix in PATHOS_DIABETE:
        await repondre(update, TXT_TYPE, boutons("typ", TYPES_DIABETE))
        return TYPE_DIAB
    return await terminer_profil(update, context)


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
    d["kcal"], note = calcul_calories(
        d["sexe"], d["age"], d["taille"], d["poids"],
        d["facteur"], d["objectif"], d["imc"],
    )
    sauver_profil(update.effective_user.id, d)

    message = (
        "Votre profil est enregistré.\n\n"
        f"IMC : {d['imc']} ({categorie_imc(d['imc'])})\n"
        f"Besoin estimé : environ {d['kcal']} kcal par jour\n\n"
        f"{CONSEILS[d['patho']]}\n"
    )
    if note:
        message += f"\n{note}\n"
    if d["patho"] in PATHOS_DIABETE and not d["type_diabete"]:
        message += (
            "\nDemandez à votre médecin votre type de diabète, puis mettez à "
            "jour votre profil pour recevoir un plan de repas.\n"
        )
    message += f"\n{AVERTISSEMENT}"
    await repondre(update, message, menu_principal())
    return ConversationHandler.END


async def annuler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_reply_markup(None)
        except Exception:
            pass
    context.user_data.clear()
    await repondre(update, "D'accord, on arrête ici.", menu_principal())
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

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(demarrer, pattern="^creer$"),
                CommandHandler("creer", demarrer),
            ],
            states={
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
                    MessageHandler(texte, rappel_boutons(TXT_PATHO, "pat", PATHOS, 1, PATHO)),
                ],
                TYPE_DIAB: [
                    CallbackQueryHandler(recevoir_type, pattern="^typ:"),
                    MessageHandler(
                        texte, rappel_boutons(TXT_TYPE, "typ", TYPES_DIABETE, 1, TYPE_DIAB)
                    ),
                ],
            },
            fallbacks=[
                CallbackQueryHandler(annuler, pattern="^annuler$"),
                CommandHandler("annuler", annuler),
                MessageHandler(
                    filters.Regex(re.compile(r"^\s*annuler\s*$", re.IGNORECASE)), annuler
                ),
                CommandHandler("start", start_et_fin),
                CommandHandler("menu", start_et_fin),
            ],
        )
    )

    # Hors questionnaire
    app.add_handler(CommandHandler("start", accueil))
    app.add_handler(CommandHandler("menu", accueil))
    app.add_handler(CommandHandler("profil", afficher_profil))
    app.add_handler(CommandHandler("plan", afficher_plan))
    app.add_handler(CommandHandler("aide", afficher_aide))
    app.add_handler(CallbackQueryHandler(bouton_menu, pattern="^(profil|plan|aide|accueil)$"))
    app.add_handler(CallbackQueryHandler(liste_situations, pattern="^sits$"))
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
