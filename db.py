"""
Base de données MiaFood.

- Si la variable DATABASE_URL existe (Neon / Postgres), les données sont
  conservées de façon permanente, même après une mise à jour du bot.
- Sinon, un fichier SQLite local est utilisé (effacé à chaque mise à jour sur
  l'hébergement gratuit : pratique pour les tests seulement).

L'identifiant d'une personne est son identifiant Telegram. Sur WhatsApp, ce
sera son numéro de téléphone : seule la colonne user_id changera de sens.
"""

import datetime
import logging
import os

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

log = logging.getLogger("miafood.db")


def _maintenant() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Base(DeclarativeBase):
    pass


class Profil(Base):
    __tablename__ = "profils"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    prenom: Mapped[str | None] = mapped_column(String(60), nullable=True)
    sexe: Mapped[str | None] = mapped_column(String(20), nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    taille: Mapped[float | None] = mapped_column(Float, nullable=True)
    poids: Mapped[float | None] = mapped_column(Float, nullable=True)
    activite: Mapped[str | None] = mapped_column(String(60), nullable=True)
    objectif: Mapped[str | None] = mapped_column(String(60), nullable=True)
    patho: Mapped[str | None] = mapped_column(String(60), nullable=True)
    type_diabete: Mapped[str | None] = mapped_column(String(30), nullable=True)
    imc: Mapped[float | None] = mapped_column(Float, nullable=True)
    kcal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    region: Mapped[str | None] = mapped_column(String(30), nullable=True)
    cree_le: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_maintenant
    )
    modifie_le: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_maintenant, onupdate=_maintenant
    )


class Acces(Base):
    __tablename__ = "acces"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cree_le: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_maintenant
    )


class Avis(Base):
    __tablename__ = "avis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    texte: Mapped[str] = mapped_column(Text)
    cree_le: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=_maintenant
    )


# Champs d'un profil que le questionnaire a le droit d'écrire
CHAMPS_PROFIL = (
    "prenom", "sexe", "age", "taille", "poids", "activite", "objectif",
    "patho", "type_diabete", "imc", "kcal",
)

_engine = None


def _url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{os.environ.get('DB_PATH', 'miafood.db')}"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def _session() -> Session:
    if _engine is None:
        init_db()
    return Session(_engine)


def init_db() -> None:
    """Crée la connexion et les tables si elles n'existent pas."""
    global _engine
    url = _url()
    _engine = create_engine(url, pool_pre_ping=True, pool_recycle=300)
    Base.metadata.create_all(_engine)
    log.info(
        "Base de données prête (%s)",
        "Postgres, mémoire permanente" if "postgresql" in url else "SQLite, temporaire",
    )


def _profil_en_dict(p: Profil) -> dict:
    return {
        "user_id": p.user_id, "prenom": p.prenom, "sexe": p.sexe, "age": p.age,
        "taille": p.taille, "poids": p.poids, "activite": p.activite,
        "objectif": p.objectif, "patho": p.patho, "type_diabete": p.type_diabete,
        "imc": p.imc, "kcal": p.kcal, "region": p.region,
    }


# ---------------------------------------------------------------------------
# Profils
# ---------------------------------------------------------------------------
def lire_profil(user_id: int):
    """Profil complet sous forme de dictionnaire, ou None s'il n'existe pas."""
    with _session() as s:
        p = s.get(Profil, user_id)
        if p is None or p.kcal is None:  # questionnaire non terminé
            return None
        return _profil_en_dict(p)


def sauver_profil(user_id: int, donnees: dict) -> None:
    """Enregistre les champs du questionnaire sans toucher à la région."""
    with _session() as s, s.begin():
        p = s.get(Profil, user_id)
        if p is None:
            p = Profil(user_id=user_id)
            s.add(p)
        for champ in CHAMPS_PROFIL:
            if champ in donnees:
                setattr(p, champ, donnees[champ])


def lire_prenom(user_id: int):
    with _session() as s:
        p = s.get(Profil, user_id)
        return p.prenom if p and p.prenom else None


def lire_region(user_id: int):
    with _session() as s:
        p = s.get(Profil, user_id)
        return p.region if p and p.region else None


def sauver_region(user_id: int, region: str) -> None:
    with _session() as s, s.begin():
        p = s.get(Profil, user_id)
        if p is None:
            p = Profil(user_id=user_id)
            s.add(p)
        p.region = region


def supprimer_utilisateur(user_id: int) -> None:
    """Efface le profil et les avis de la personne (droit à l'effacement)."""
    with _session() as s, s.begin():
        s.execute(delete(Profil).where(Profil.user_id == user_id))
        s.execute(delete(Avis).where(Avis.user_id == user_id))


# ---------------------------------------------------------------------------
# Accès par code d'invitation
# ---------------------------------------------------------------------------
def a_acces(user_id: int) -> bool:
    with _session() as s:
        return s.get(Acces, user_id) is not None


def donner_acces(user_id: int) -> None:
    with _session() as s, s.begin():
        if s.get(Acces, user_id) is None:
            s.add(Acces(user_id=user_id))


# ---------------------------------------------------------------------------
# Avis des testeurs
# ---------------------------------------------------------------------------
def ajouter_avis(user_id: int, texte: str) -> None:
    with _session() as s, s.begin():
        s.add(Avis(user_id=user_id, texte=texte))


def derniers_avis(n: int = 15) -> list:
    """Retourne [(date, prénom, texte)] du plus récent au plus ancien."""
    with _session() as s:
        avis = s.scalars(select(Avis).order_by(Avis.id.desc()).limit(n)).all()
        resultat = []
        for a in avis:
            p = s.get(Profil, a.user_id)
            resultat.append((a.cree_le, p.prenom if p and p.prenom else "?", a.texte))
        return resultat


def statistiques() -> dict:
    with _session() as s:
        return {
            "profils": s.scalar(
                select(func.count()).select_from(Profil).where(Profil.kcal.is_not(None))
            ),
            "acces": s.scalar(select(func.count()).select_from(Acces)),
            "avis": s.scalar(select(func.count()).select_from(Avis)),
        }
