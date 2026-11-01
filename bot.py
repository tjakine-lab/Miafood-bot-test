"""
MiaFood - assistant nutritionnel (version 1, Telegram)

Ce que fait cette version :
- questionnaire d'accueil (/start) avec boutons
- calcul de l'IMC et d'une estimation des besoins en calories
- enregistrement du profil dans une base SQLite
- commandes /profil, /menu, /aide, /annuler

Lancement :
    export TELEGRAM_TOKEN="ton_token"
    python bot.py
"""

import logging
import os
import sqlite3

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
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

DB_PATH = os.environ.get("DB_PATH", "miafood.db")

# Étapes du questionnaire
SEXE, AGE, TAILLE, POIDS, ACTIVITE, OBJECTIF, PATHO = range(7)

# ---------------------------------------------------------------------------
# Options proposées (boutons)
# ---------------------------------------------------------------------------
OPT_SEXE = ["Femme", "Homme"]
OPT_ACTIVITE = {
    "Peu active (assis la plupart du temps)": 1.2,
    "Légèrement active (marche, tâches légères)": 1.375,
    "Modérément active (travail physique ou sport 3-5 fois/semaine)": 1.55,
    "Très active (travail très physique ou sport quotidien)": 1.725,
}
OPT_OBJECTIF = ["Perdre du poids", "Améliorer ma santé", "Garder mon poids"]
OPT_PATHO = [
    "Aucune",
    "Diabète",
    "Hypertension",
    "Foie gras",
    "Enceinte ou allaitante",
]

AVERTISSEMENT = (
    "Ce bot donne des conseils généraux d'alimentation. Il ne remplace pas "
    "un médecin ou un nutritionniste."
)

# Conseils généraux par situation. À REMPLACER par des règles validées par un
# professionnel de santé avant d'ouvrir le bot à du public.
CONSEILS = {
    "Aucune": "Variez vos repas : légumes, protéines (poisson, œufs, haricots), "
    "féculents en portion modérée, et peu de boissons sucrées.",
    "Diabète": "Privilégiez les aliments qui font peu monter le sucre (légumes, "
    "haricots, poisson) et limitez les boissons sucrées. Répartissez les "
    "féculents sur la journée. Suivez toujours les consignes de votre médecin.",
    "Hypertension": "Limitez le sel, les cubes d'assaisonnement et les aliments "
    "très salés (poisson fumé salé, charcuterie). Mangez plus de légumes et de "
    "fruits. Suivez toujours les consignes de votre médecin.",
    "Foie gras": "Réduisez les boissons sucrées, l'alcool et les fritures. "
    "Privilégiez légumes, poisson et cuissons à l'eau ou grillées. Un avis "
    "médical reste indispensable.",
}


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
                imc REAL, kcal INTEGER
            )
            """
        )


def sauver_profil(user_id: int, p: dict) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT OR REPLACE INTO profils
            (user_id, sexe, age, taille, poids, activite, objectif, patho, imc, kcal)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, p["sexe"], p["age"], p["taille"], p["poids"],
                p["activite"], p["objectif"], p["patho"], p["imc"], p["kcal"],
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
# Utilitaires
# ---------------------------------------------------------------------------
def clavier(options, colonnes=1):
    lignes = [options[i:i + colonnes] for i in range(0, len(options), colonnes)]
    return ReplyKeyboardMarkup(lignes, one_time_keyboard=True, resize_keyboard=True)


def nombre(texte: str):
    try:
        return float(texte.replace(",", ".").strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Questionnaire
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Bonjour, je suis MiaFood, votre assistant pour mieux manger.\n\n"
        f"{AVERTISSEMENT}\n\n"
        "Quelques questions pour créer votre profil. "
        "Tapez /annuler pour arrêter à tout moment.\n\n"
        "Vous êtes :",
        reply_markup=clavier(OPT_SEXE, 2),
    )
    return SEXE


async def recevoir_sexe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texte = update.message.text.strip()
    if texte not in OPT_SEXE:
        await update.message.reply_text(
            "Merci de choisir un des boutons.", reply_markup=clavier(OPT_SEXE, 2)
        )
        return SEXE
    context.user_data["sexe"] = texte
    await update.message.reply_text(
        "Quel est votre âge (en années) ?", reply_markup=ReplyKeyboardRemove()
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
            "Parlez-en à un médecin ou à un nutritionniste."
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
        "Quel est votre niveau d'activité au quotidien ?",
        reply_markup=clavier(list(OPT_ACTIVITE.keys())),
    )
    return ACTIVITE


async def recevoir_activite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texte = update.message.text.strip()
    if texte not in OPT_ACTIVITE:
        await update.message.reply_text(
            "Merci de choisir un des boutons.",
            reply_markup=clavier(list(OPT_ACTIVITE.keys())),
        )
        return ACTIVITE
    context.user_data["activite"] = texte
    await update.message.reply_text(
        "Quel est votre objectif principal ?",
        reply_markup=clavier(OPT_OBJECTIF),
    )
    return OBJECTIF


async def recevoir_objectif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texte = update.message.text.strip()
    if texte not in OPT_OBJECTIF:
        await update.message.reply_text(
            "Merci de choisir un des boutons.", reply_markup=clavier(OPT_OBJECTIF)
        )
        return OBJECTIF
    context.user_data["objectif"] = texte
    await update.message.reply_text(
        "Avez-vous un problème de santé à prendre en compte ?",
        reply_markup=clavier(OPT_PATHO),
    )
    return PATHO


async def recevoir_patho(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texte = update.message.text.strip()
    if texte not in OPT_PATHO:
        await update.message.reply_text(
            "Merci de choisir un des boutons.", reply_markup=clavier(OPT_PATHO)
        )
        return PATHO

    if texte == "Enceinte ou allaitante":
        await update.message.reply_text(
            "Pendant la grossesse et l'allaitement, l'alimentation doit être "
            "suivie par un professionnel de santé. Je ne peux pas vous donner "
            "de plan ici. Parlez-en à votre sage-femme ou à votre médecin.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    d = context.user_data
    d["patho"] = texte
    d["imc"] = calcul_imc(d["poids"], d["taille"])
    d["kcal"], note = calcul_calories(
        d["sexe"], d["age"], d["taille"], d["poids"],
        OPT_ACTIVITE[d["activite"]], d["objectif"], d["imc"],
    )
    sauver_profil(update.effective_user.id, d)

    message = (
        "Votre profil est enregistré.\n\n"
        f"IMC : {d['imc']} ({categorie_imc(d['imc'])})\n"
        f"Besoin estimé : environ {d['kcal']} kcal par jour\n\n"
        f"{CONSEILS[texte]}\n"
    )
    if note:
        message += f"\n{note}\n"
    message += f"\n{AVERTISSEMENT}\n\nTapez /menu pour la suite."
    await update.message.reply_text(message, reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def annuler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "D'accord, on arrête ici. Tapez /start pour recommencer.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Autres commandes
# ---------------------------------------------------------------------------
async def profil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    p = lire_profil(update.effective_user.id)
    if p is None:
        await update.message.reply_text("Vous n'avez pas encore de profil. Tapez /start.")
        return
    await update.message.reply_text(
        "Votre profil :\n"
        f"{p['sexe']}, {p['age']} ans, {p['taille']:.0f} cm, {p['poids']:.1f} kg\n"
        f"Activité : {p['activite']}\n"
        f"Objectif : {p['objectif']}\n"
        f"Situation de santé : {p['patho']}\n"
        f"IMC : {p['imc']} - Besoin estimé : environ {p['kcal']} kcal/jour\n\n"
        "Pour le modifier, tapez /start."
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if lire_profil(update.effective_user.id) is None:
        await update.message.reply_text("Créez d'abord votre profil avec /start.")
        return
    await update.message.reply_text(
        "Les plans de repas arrivent bientôt. Cette partie sera ajoutée à la "
        "prochaine version."
    )


async def aide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start - créer ou modifier mon profil\n"
        "/profil - voir mon profil\n"
        "/menu - mon plan de repas\n"
        "/annuler - arrêter le questionnaire\n\n"
        f"{AVERTISSEMENT}"
    )


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------
def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("La variable TELEGRAM_TOKEN est absente.")

    init_db()
    app = Application.builder().token(token).build()

    texte = filters.TEXT & ~filters.COMMAND
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("start", start)],
            states={
                SEXE: [MessageHandler(texte, recevoir_sexe)],
                AGE: [MessageHandler(texte, recevoir_age)],
                TAILLE: [MessageHandler(texte, recevoir_taille)],
                POIDS: [MessageHandler(texte, recevoir_poids)],
                ACTIVITE: [MessageHandler(texte, recevoir_activite)],
                OBJECTIF: [MessageHandler(texte, recevoir_objectif)],
                PATHO: [MessageHandler(texte, recevoir_patho)],
            },
            fallbacks=[
                CommandHandler("annuler", annuler),
                CommandHandler("start", start),
            ],
        )
    )
    app.add_handler(CommandHandler("profil", profil))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("aide", aide))

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
