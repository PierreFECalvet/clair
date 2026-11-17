"""Expressions temporelles francaises -> dates ISO (M1 etape 3).

Module de regles sans dependance. HeidelTime n'a pas d'interface Python
standard ; ``dateparser`` est utilise en option, seulement pour une expression
isolee que les regles ne reconnaissent pas (``resoudre``).

Deux fonctions publiques :

- ``trouver(texte, reference)`` : toutes les expressions temporelles d'un
  texte, avec leurs offsets et leur resolution ;
- ``resoudre(expr, reference)`` : resolution d'une expression isolee.

Une resolution est un dict ::

    {'texte', 'start', 'end',
     'debut': 'AAAA-MM-JJ' | None, 'fin': 'AAAA-MM-JJ' | None,
     'type': 'date' | 'intervalle',          # date si debut == fin
     'nature': 'absolue' | 'relative',       # relative : la reference a servi
     'precision': 'jour' | 'semaine' | 'mois' | 'saison' | 'trimestre' | 'semestre' | 'annee' | 'decennie',
     'modificateur': None | 'depuis' | 'jusqu' | 'dici' | 'avant' | 'apres' | 'partir',
     'resolue': bool}                        # False : relative sans reference

``reference`` : date de reference (publication de la page), ``date``,
``datetime`` ou texte ISO ; ``None`` laisse les expressions relatives non
resolues (``resolue=False``). Les intervalles ouverts (« depuis 2023 » sans
reference) ont ``fin=None``.

Conventions documentees (a evaluer) :
- « mardi » seul : le mardi de la semaine (lundi-dimanche) de la reference ;
  « mardi dernier » : le precedent strictement avant ; « mardi prochain » : le
  suivant strictement apres.
- « le 1er aout » sans annee : annee de la reference.
- Saisons astronomiques : printemps 20/03-20/06, ete 21/06-22/09, automne
  23/09-20/12, hiver 21/12-19/03 (annee suivante). « l'ete » sans annee : ete
  de l'annee de reference.
- « debut / mi / fin » d'une periode : premier, deuxieme ou dernier tiers, en
  mois pour une periode de mois entiers (« debut 2026 » = 01/01-30/04), en
  jours sinon (« fin aout » = 22-31/08).
- Annee seule (« 2009 ») : retenue si une preposition temporelle la precede
  (« en », « depuis », « fin »...) ou si aucun nom ne la suit (« 2048
  serveurs » n'est pas une date).
- « en debut d'annee » : debut de l'annee de reference.
"""
import calendar
import re
import unicodedata
from datetime import date, datetime, timedelta

MOIS = {
    'janvier': 1, 'janv': 1, 'fevrier': 2, 'fevr': 2, 'fev': 2, 'mars': 3, 'avril': 4, 'avr': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'juil': 7, 'aout': 8, 'septembre': 9, 'sept': 9,
    'octobre': 10, 'oct': 10, 'novembre': 11, 'nov': 11, 'decembre': 12, 'dec': 12,
}
JOURS_SEMAINE = {'lundi': 0, 'mardi': 1, 'mercredi': 2, 'jeudi': 3, 'vendredi': 4, 'samedi': 5, 'dimanche': 6}
NOMBRES = {
    'un': 1, 'une': 1, 'deux': 2, 'trois': 3, 'quatre': 4, 'cinq': 5, 'six': 6, 'sept': 7, 'huit': 8,
    'neuf': 9, 'dix': 10, 'onze': 11, 'douze': 12, 'treize': 13, 'quatorze': 14, 'quinze': 15,
    'seize': 16, 'vingt': 20, 'trente': 30, 'quarante': 40, 'cinquante': 50, 'soixante': 60, 'cent': 100,
}
ORDINAUX = {
    'premier': 1, 'premiere': 1, '1er': 1, '1re': 1, '1ere': 1, '1e': 1,
    'deuxieme': 2, 'second': 2, 'seconde': 2, '2e': 2, '2eme': 2, '2nd': 2, '2nde': 2,
    'troisieme': 3, '3e': 3, '3eme': 3, 'quatrieme': 4, '4e': 4, '4eme': 4,
}
SAISONS = {
    # nom -> (mois debut, jour debut, mois fin, jour fin, la fin est-elle l'annee suivante)
    'printemps': (3, 20, 6, 20, False),
    'ete': (6, 21, 9, 22, False),
    'automne': (9, 23, 12, 20, False),
    'hiver': (12, 21, 3, 19, True),
}

# ---------------------------------------------------------------------------
# Briques d'expressions regulieres (texte normalise : minuscules sans accents,
# apostrophes droites, meme longueur que l'original)
# ---------------------------------------------------------------------------

_M = r'(?P<{n}>janvier|janv\.?|fevrier|fevr\.?|fev\.?|mars|avril|avr\.?|mai|juin|juillet|juil\.?|aout|septembre|sept\.?|octobre|oct\.?|novembre|nov\.?|decembre|dec\.?)'
_J = r'(?P<{n}>1er|premier|[0-3]?\d)'
_A = r'(?P<{n}>(?:19|20)\d{{2}})'
_JS = r'(?:(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+)?'
_NB = r'(?P<{n}>\d{{1,3}}|un|une|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|onze|douze|treize|quatorze|quinze|seize|vingt|trente|quarante|cinquante|soixante|cent|quelques)'
_UNITE = r'(?P<{n}>jours?|semaines?|mois|ans|annees?|trimestres?|semestres?|decennies?)'
_PARTIE = r"(?:(?:la|le|au|en|a\s+la)\s+)?(?P<partie>debut|fin|mi|milieu|courant)(?:\s*-\s*|\s+)(?:de\s+l'|de\s+la\s+|du\s+|des\s+|de\s+|d')?"


def _m(nom):
    return _M.format(n=nom)


def _j(nom):
    return _J.format(n=nom)


def _a(nom):
    return _A.format(n=nom)


# Modificateurs places avant l'expression (recherches en arriere).
_MODIFICATEURS = re.compile(
    r"(?P<mod>depuis|des|a\s+partir\s+d[eu]|a\s+partir\s+de\s+la|a\s+partir\s+de\s+l'|a\s+partir\s+d'|a\s+compter\s+d[eu]|a\s+compter\s+d'"
    r"|jusqu'(?:a|au|en)|jusqu'a\s+la|jusqu'a\s+l'|d'ici(?:\s+a)?|avant|apres|au\s+plus\s+tard)\s*$"
)

# Motifs dans l'ordre de priorite (les premiers gagnent en cas de chevauchement).
_MOTIFS = [
    # du 1er au 31 aout 2026 / du 3 juillet au 5 aout 2026 / du 1er juillet 2025 au 30 juin 2026
    ('jours_intervalle', re.compile(
        r"\b(?:du|entre\s+le)\s+" + _JS + _j('j1') + r"(?:\s+" + _m('m1') + r"(?:\s+" + _a('a1') + r")?)?"
        r"\s+(?:au|et\s+le)\s+" + _JS + _j('j2') + r"\s+" + _m('m2') + r"(?:\s+" + _a('a2') + r")?\b")),
    # de janvier a mars 2026 / entre mai et juillet 2025
    ('mois_intervalle', re.compile(
        r"\b(?:de|d'|entre)\s*" + _m('m1') + r"(?:\s+" + _a('a1') + r")?\s+(?:a|au|et)\s+" + _m('m2') + r"(?:\s+" + _a('a2') + r")?\b")),
    # de 2019 a 2023 / entre 1999 et 2009 / 2019-2023
    ('annees_intervalle', re.compile(
        r"(?:\b(?:de|entre)\s+)" + _a('a1') + r"\s+(?:a|et)\s+" + _a('a2') + r"\b"
        r"|\b" + _a('b1') + r"\s*[-–]\s*" + _a('b2') + r"\b")),
    # ISO et numerique
    ('iso', re.compile(r"\b(?P<a>(?:19|20)\d{2})-(?P<m>[01]\d)-(?P<j>[0-3]\d)\b")),
    ('numerique', re.compile(r"\b(?P<j>[0-3]?\d)[/.](?P<m>[01]?\d)[/.](?P<a>(?:19|20)\d{2})\b")),
    # (lundi) 1er aout (2026)
    ('jour_mois', re.compile(r"\b(?:le\s+)?" + _JS + _j('j') + r"\s+" + _m('m') + r"(?:\s+" + _a('a') + r")?(?![\w])")),
    # dans les annees 1990 / les annees 90
    ('decennie', re.compile(r"\b(?:dans\s+|durant\s+|pendant\s+)?les\s+annees\s+(?P<d>(?:19|20)?\d0)\b")),
    # trimestres et semestres
    ('trimestre', re.compile(
        r"\b(?:(?:le|au|du|ce)\s+)?(?P<o>premier|1er|deuxieme|2e|2eme|second|troisieme|3e|3eme|quatrieme|4e|4eme|dernier)\s+trimestre"
        r"(?:\s+(?:de\s+l'annee\s+|de\s+|)" + _a('a') + r")?\b"
        r"|\b(?P<t>[tq])(?P<n>[1-4])\s*(?P<a2>(?:19|20)\d{2})\b")),
    ('semestre', re.compile(
        r"\b(?:(?:le|au|du|ce)\s+)?(?P<o>premier|1er|deuxieme|2e|second|dernier)\s+semestre(?:\s+(?:de\s+)?" + _a('a') + r")?\b"
        r"|\bs(?P<n>[12])\s*(?P<a2>(?:19|20)\d{2})\b")),
    ('periode_relative', re.compile(
        r"\b(?P<quoi>ce\s+trimestre|ce\s+semestre|le\s+trimestre\s+(?:dernier|prochain|passe|precedent)"
        r"|le\s+semestre\s+(?:dernier|prochain|passe|precedent))\b")),
    # saisons
    ('saison', re.compile(
        r"(?:" + _PARTIE + r")?\b(?:(?:l'|cet\s+|ce\s+|a\s+l'|au\s+|en\s+)?)(?P<s>printemps|ete|automne|hiver)"
        r"(?:\s+(?P<a>(?:19|20)\d{2})|\s+(?P<r>dernier|prochain|passe|precedent))?\b")),
    # mois (annee)
    ('mois_annee', re.compile(r"(?:" + _PARTIE + r")?\b" + _m('m') + r"\s+" + _a('a') + r"\b")),
    # mois seul, s'il est precede d'une preposition ou d'une partie
    ('mois_seul', re.compile(
        r"(?:" + _PARTIE + r"|\b(?:en|depuis|des|courant|au\s+mois\s+de|au\s+mois\s+d'|jusqu'en|d'ici)\s+)" + _m('m') + r"(?![\w])"
        r"(?!\s+(?:19|20)\d{2})(?P<r>\s+(?:dernier|prochain))?")),
    # annees relatives
    ('annee_relative', re.compile(
        r"\b(?P<quoi>l'an\s+(?:dernier|passe|prochain)|l'annee\s+(?:derniere|passee|prochaine|precedente|suivante|en\s+cours)"
        r"|cette\s+annee|l'an\s+(?:d'avant|d'apres))\b")),
    ('partie_annee', re.compile(
        r"(?:\b(?:en|au|a\s+la|la|le)\s+)?(?P<partie>debut|fin|milieu|mi)(?:\s*-\s*|\s+)(?:de\s+l'|d')annee(?!\s+(?:19|20)\d{2})\b")),
    ('partie_annee_num', re.compile(r"(?:\b(?:en|au|a\s+la|la|le)\s+)?(?P<partie>debut|fin|milieu|mi)(?:\s*-\s*|\s+)(?:de\s+l'annee\s+|d'annee\s+)?" + _a('a') + r"\b")),
    # il y a trois mois / dans deux ans / depuis dix jours / d'ici trois mois
    ('decalage', re.compile(
        r"\b(?P<sens>il\s+y\s+a|voici|dans|depuis|d'ici|pendant\s+les|au\s+cours\s+des|ces)\s+" + _NB.format(n='n') + r"\s+"
        + _UNITE.format(n='u') + r"(?P<der>\s+(?:derniers|dernieres|prochains|prochaines|a\s+venir))?\b")),
    # jours
    ('jour_relatif', re.compile(r"\b(?P<quoi>aujourd'hui|avant-hier|apres-demain|hier|demain|ce\s+matin|ce\s+soir|cet\s+apres-midi)\b")),
    ('semaine', re.compile(
        r"\b(?P<quoi>cette\s+semaine|la\s+semaine\s+(?:derniere|prochaine|passee|precedente)|ce\s+mois-ci|ce\s+mois"
        r"|le\s+mois\s+(?:dernier|prochain|passe|precedent)|ce\s+week-end|le\s+week-end\s+(?:dernier|prochain))\b")),
    ('jour_semaine', re.compile(r"\b(?P<w>lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)(?P<r>\s+(?:dernier|prochain|passe))?\b(?!\s+(?:1er|[0-3]?\d)\b)")),
    # annee seule : verifiee ensuite (preposition avant, pas d'unite apres)
    ('annee', re.compile(r"(?<![\w.,/-])(?P<a>(?:19|20)\d{2})(?![\w]|[.,]\d|\s*%|\s*€|\s*-\s*\d)")),
]

_PREPOSITIONS_ANNEE = re.compile(
    r"\b(?:en|depuis|des|avant|apres|jusqu'en|jusqu'a|d'ici|vers|fin|debut|mi|courant|annee|l'an|au\s+cours\s+de"
    r"|a\s+partir\s+de|a\s+compter\s+de|janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre"
    r"|printemps|ete|automne|hiver)\s*-?\s*$"
)
_CONNECTEURS = {
    'et', 'ou', 'puis', 'a', 'au', 'aux', 'contre', 'par', 'pour', 'avec', 'dans', 'sur', 'selon', 'avant', 'apres',
    'deja', 'lors', 'quand', 'alors', 'le', 'la', 'les', "l'", 'un', 'une', 'des', 'du', 'de', "d'", 'il', 'elle', 'ils',
    'elles', 'on', 'ce', 'cette', 'ces', 'qui', 'que', 'mais', 'donc', 'encore', 'meme', 'seulement', 'environ', 'aussi',
    'en', 'son', 'sa', 'ses', 'leur', 'leurs', 'cela', 'ont', 'est', 'sont', 'avait', 'etait',
}


# ---------------------------------------------------------------------------
# Outils de dates
# ---------------------------------------------------------------------------

def normaliser(texte):
    """Minuscules sans accents, apostrophes droites, longueur conservee (offsets identiques)."""
    resultat = []
    for caractere in texte or '':
        if caractere in '’‘ʼ`´':
            resultat.append("'")
            continue
        if caractere in '  ':
            resultat.append(' ')
            continue
        decompose = unicodedata.normalize('NFD', caractere)
        base = decompose[0] if decompose else caractere
        minuscule = base.lower()
        resultat.append(minuscule if len(minuscule) == 1 else base)
    return ''.join(resultat)


def en_date(valeur):
    """``date`` depuis une date, un datetime ou un texte ISO (les 10 premiers caracteres) ; None sinon."""
    if valeur is None or valeur == '':
        return None
    if isinstance(valeur, datetime):
        return valeur.date()
    if isinstance(valeur, date):
        return valeur
    texte = str(valeur).strip()
    correspondance = re.match(r'^((?:19|20)\d{2})-(\d{2})-(\d{2})', texte)
    if correspondance:
        try:
            return date(int(correspondance.group(1)), int(correspondance.group(2)), int(correspondance.group(3)))
        except ValueError:
            return None
    correspondance = re.match(r'^((?:19|20)\d{2})-(\d{2})$', texte)
    if correspondance:
        return date(int(correspondance.group(1)), int(correspondance.group(2)), 1)
    correspondance = re.match(r'^((?:19|20)\d{2})$', texte)
    if correspondance:
        return date(int(correspondance.group(1)), 1, 1)
    return None


def iso(valeur):
    """Texte ISO ``AAAA-MM-JJ`` ou None."""
    if valeur is None:
        return None
    return valeur.isoformat()


def fin_de_mois(annee, mois):
    """Dernier jour du mois."""
    return date(annee, mois, calendar.monthrange(annee, mois)[1])


def ajouter_mois(jour, nombre):
    """Ajoute ``nombre`` mois (negatif possible) en bornant le jour."""
    total = jour.year * 12 + (jour.month - 1) + nombre
    annee = total // 12
    mois = total % 12 + 1
    dernier = calendar.monthrange(annee, mois)[1]
    return date(annee, mois, min(jour.day, dernier))


def _nombre(valeur):
    """Nombre ecrit en chiffres ou en lettres."""
    if valeur is None:
        return None
    if valeur.isdigit():
        return int(valeur)
    if valeur == 'quelques':
        return 3
    return NOMBRES.get(valeur)


def _mois(valeur):
    """Numero du mois d'un nom (avec ou sans point d'abreviation)."""
    return MOIS.get((valeur or '').rstrip('.'))


def _jour(valeur):
    """Jour du mois (« 1er », « premier » -> 1)."""
    if valeur in ('1er', 'premier'):
        return 1
    return int(valeur)


def _tiers(debut, fin, partie):
    """Premier, deuxieme ou dernier tiers d'une periode."""
    if not partie or partie == 'courant' or debut is None or fin is None:
        return debut, fin
    mois_entiers = debut.day == 1 and fin == fin_de_mois(fin.year, fin.month)
    nombre_mois = (fin.year - debut.year) * 12 + fin.month - debut.month + 1
    if mois_entiers and nombre_mois >= 3 and nombre_mois % 3 == 0:
        # Periode en mois entiers (annee, trimestre...) : tiers en mois.
        tiers_mois = nombre_mois // 3
        if partie == 'debut':
            return debut, ajouter_mois(debut, tiers_mois) - timedelta(days=1)
        if partie in ('mi', 'milieu'):
            return ajouter_mois(debut, tiers_mois), ajouter_mois(debut, 2 * tiers_mois) - timedelta(days=1)
        return ajouter_mois(debut, 2 * tiers_mois), fin
    duree = (fin - debut).days + 1
    tiers = max(1, duree // 3)
    if partie == 'debut':
        return debut, debut + timedelta(days=tiers - 1)
    if partie in ('mi', 'milieu'):
        return debut + timedelta(days=tiers), fin - timedelta(days=tiers)
    return fin - timedelta(days=tiers - 1), fin


def _saison(nom, annee):
    """Bornes d'une saison d'une annee donnee."""
    m1, j1, m2, j2, suivante = SAISONS[nom]
    return date(annee, m1, j1), date(annee + (1 if suivante else 0), m2, j2)


def _lundi(jour):
    """Lundi de la semaine du jour."""
    return jour - timedelta(days=jour.weekday())


# ---------------------------------------------------------------------------
# Resolution d'un motif
# ---------------------------------------------------------------------------

class _NonResolue(Exception):
    """Expression relative sans reference."""


def _ref(reference):
    """Reference obligatoire pour une expression relative."""
    if reference is None:
        raise _NonResolue()
    return reference


def _resoudre_motif(nom, g, reference, texte_norm, debut_match):
    """Renvoie (debut, fin, precision, relative) pour un motif reconnu, ou None si invalide."""
    if nom == 'jours_intervalle':
        a2 = int(g['a2']) if g['a2'] else None
        relative = a2 is None
        annee_fin = a2 if a2 else _ref(reference).year
        m2 = _mois(g['m2'])
        m1 = _mois(g['m1']) if g['m1'] else m2
        a1 = int(g['a1']) if g['a1'] else annee_fin
        debut = date(a1, m1, _jour(g['j1']))
        fin = date(annee_fin, m2, _jour(g['j2']))
        if fin < debut and not g['a1']:
            debut = date(a1 - 1, m1, _jour(g['j1']))
        return debut, fin, 'jour', relative
    if nom == 'mois_intervalle':
        a2 = int(g['a2']) if g['a2'] else None
        relative = a2 is None and not g['a1']
        annee_fin = a2 if a2 else (int(g['a1']) if g['a1'] else _ref(reference).year)
        m1 = _mois(g['m1'])
        m2 = _mois(g['m2'])
        a1 = int(g['a1']) if g['a1'] else (annee_fin if m1 <= m2 else annee_fin - 1)
        return date(a1, m1, 1), fin_de_mois(annee_fin, m2), 'mois', relative
    if nom == 'annees_intervalle':
        a1 = int(g['a1'] or g['b1'])
        a2 = int(g['a2'] or g['b2'])
        if a2 < a1:
            return None
        return date(a1, 1, 1), date(a2, 12, 31), 'annee', False
    if nom in ('iso', 'numerique'):
        jour = date(int(g['a']), int(g['m']), int(g['j']))
        return jour, jour, 'jour', False
    if nom == 'jour_mois':
        relative = not g['a']
        annee = int(g['a']) if g['a'] else _ref(reference).year
        jour = date(annee, _mois(g['m']), _jour(g['j']))
        return jour, jour, 'jour', relative
    if nom == 'decennie':
        valeur = g['d']
        if len(valeur) == 2:
            valeur = ('19' if int(valeur) >= 30 else '20') + valeur
        annee = int(valeur)
        return date(annee, 1, 1), date(annee + 9, 12, 31), 'decennie', False
    if nom == 'trimestre':
        if g['t']:
            numero = int(g['n'])
            annee = int(g['a2'])
            relative = False
        else:
            numero = 4 if g['o'] == 'dernier' else ORDINAUX.get(g['o'])
            relative = not g['a']
            annee = int(g['a']) if g['a'] else _ref(reference).year
        debut = date(annee, 3 * (numero - 1) + 1, 1)
        return debut, fin_de_mois(annee, 3 * numero), 'trimestre', relative
    if nom == 'semestre':
        if g['n']:
            numero = int(g['n'])
            annee = int(g['a2'])
            relative = False
        else:
            numero = 2 if g['o'] == 'dernier' else min(2, ORDINAUX.get(g['o'], 1))
            relative = not g['a']
            annee = int(g['a']) if g['a'] else _ref(reference).year
        debut = date(annee, 6 * (numero - 1) + 1, 1)
        return debut, fin_de_mois(annee, 6 * numero), 'semestre', relative
    if nom == 'periode_relative':
        ref = _ref(reference)
        quoi = g['quoi']
        taille = 3 if 'trimestre' in quoi else 6
        numero = (ref.month - 1) // taille
        debut = date(ref.year, numero * taille + 1, 1)
        if any(mot in quoi for mot in ('dernier', 'passe', 'precedent')):
            debut = ajouter_mois(debut, -taille)
        elif 'prochain' in quoi:
            debut = ajouter_mois(debut, taille)
        fin = ajouter_mois(debut, taille) - timedelta(days=1)
        return debut, fin, 'trimestre' if taille == 3 else 'semestre', True
    if nom == 'saison':
        saison = g['s']
        if g['a']:
            debut, fin = _saison(saison, int(g['a']))
            relative = False
        else:
            ref = _ref(reference)
            relative = True
            debut, fin = _saison(saison, ref.year)
            if saison == 'hiver' and ref.month <= 3:
                debut, fin = _saison(saison, ref.year - 1)
            if g['r'] in ('dernier', 'passe', 'precedent'):
                if fin >= ref:
                    debut, fin = _saison(saison, debut.year - 1)
            elif g['r'] == 'prochain':
                if debut <= ref:
                    debut, fin = _saison(saison, debut.year + 1)
        debut, fin = _tiers(debut, fin, g.get('partie'))
        return debut, fin, 'saison', relative
    if nom == 'mois_annee':
        annee = int(g['a'])
        mois = _mois(g['m'])
        debut, fin = _tiers(date(annee, mois, 1), fin_de_mois(annee, mois), g.get('partie'))
        return debut, fin, 'mois' if not g.get('partie') else 'jour', False
    if nom == 'mois_seul':
        ref = _ref(reference)
        mois = _mois(g['m'])
        annee = ref.year
        if g['r'] and 'dernier' in g['r']:
            annee = ref.year if mois < ref.month else ref.year - 1
        elif g['r'] and 'prochain' in g['r']:
            annee = ref.year if mois > ref.month else ref.year + 1
        debut, fin = _tiers(date(annee, mois, 1), fin_de_mois(annee, mois), g.get('partie'))
        return debut, fin, 'mois', True
    if nom == 'annee_relative':
        ref = _ref(reference)
        quoi = g['quoi']
        annee = ref.year
        if any(mot in quoi for mot in ('dernier', 'derniere', 'passe', 'passee', 'precedente', "d'avant")):
            annee -= 1
        elif any(mot in quoi for mot in ('prochain', 'prochaine', 'suivante', "d'apres")):
            annee += 1
        return date(annee, 1, 1), date(annee, 12, 31), 'annee', True
    if nom == 'partie_annee':
        ref = _ref(reference)
        debut, fin = _tiers(date(ref.year, 1, 1), date(ref.year, 12, 31), g['partie'])
        return debut, fin, 'mois', True
    if nom == 'partie_annee_num':
        annee = int(g['a'])
        debut, fin = _tiers(date(annee, 1, 1), date(annee, 12, 31), g['partie'])
        return debut, fin, 'mois', False
    if nom == 'decalage':
        return _decalage(g, _ref(reference))
    if nom == 'jour_relatif':
        ref = _ref(reference)
        decalages = {"aujourd'hui": 0, 'hier': -1, 'avant-hier': -2, 'demain': 1, 'apres-demain': 2}
        jour = ref + timedelta(days=decalages.get(g['quoi'], 0))
        return jour, jour, 'jour', True
    if nom == 'semaine':
        ref = _ref(reference)
        quoi = g['quoi']
        if 'semaine' in quoi or 'week-end' in quoi:
            lundi = _lundi(ref)
            if any(mot in quoi for mot in ('derniere', 'passee', 'precedente', 'dernier')):
                lundi -= timedelta(days=7)
            elif 'prochain' in quoi:
                lundi += timedelta(days=7)
            if 'week-end' in quoi:
                return lundi + timedelta(days=5), lundi + timedelta(days=6), 'jour', True
            return lundi, lundi + timedelta(days=6), 'semaine', True
        debut = date(ref.year, ref.month, 1)
        if any(mot in quoi for mot in ('dernier', 'passe', 'precedent')):
            debut = ajouter_mois(debut, -1)
        elif 'prochain' in quoi:
            debut = ajouter_mois(debut, 1)
        return debut, fin_de_mois(debut.year, debut.month), 'mois', True
    if nom == 'jour_semaine':
        ref = _ref(reference)
        cible = JOURS_SEMAINE[g['w']]
        sens = (g['r'] or '').strip()
        if sens in ('dernier', 'passe'):
            ecart = (ref.weekday() - cible) % 7 or 7
            jour = ref - timedelta(days=ecart)
        elif sens == 'prochain':
            ecart = (cible - ref.weekday()) % 7 or 7
            jour = ref + timedelta(days=ecart)
        else:
            jour = _lundi(ref) + timedelta(days=cible)
        return jour, jour, 'jour', True
    if nom == 'annee':
        if not _annee_plausible(texte_norm, debut_match, g):
            return None
        annee = int(g['a'])
        return date(annee, 1, 1), date(annee, 12, 31), 'annee', False
    return None


def _annee_plausible(texte_norm, position, g):
    """Une annee isolee est une date si une preposition la precede ou si aucun nom d'unite ne la suit."""
    avant = texte_norm[max(0, position - 30):position]
    if _PREPOSITIONS_ANNEE.search(avant):
        return True
    apres = texte_norm[position + 4:position + 30]
    suivant = re.match(r"\s+([a-z']+)", apres)
    if suivant and suivant.group(1) not in _CONNECTEURS:
        return False
    return True


def _decalage(g, ref):
    """« il y a trois mois », « dans deux ans », « depuis dix jours », « d'ici trois mois »."""
    nombre = _nombre(g['n'])
    if nombre is None:
        return None
    unite = g['u']
    sens = re.sub(r'\s+', ' ', g['sens'])
    passe = sens in ('il y a', 'voici', 'depuis', 'pendant les', 'au cours des') or (sens == 'ces' and not (g['der'] and 'prochain' in g['der']))
    if g['der'] and ('prochain' in g['der'] or 'venir' in g['der']):
        passe = False
    signe = -1 if passe else 1
    if unite.startswith('jour'):
        autre = ref + timedelta(days=signe * nombre)
        precision = 'jour'
    elif unite.startswith('semaine'):
        autre = ref + timedelta(days=signe * 7 * nombre)
        precision = 'semaine'
    elif unite == 'mois':
        autre = ajouter_mois(ref, signe * nombre)
        precision = 'mois'
    elif unite.startswith('trimestre'):
        autre = ajouter_mois(ref, signe * 3 * nombre)
        precision = 'trimestre'
    elif unite.startswith('semestre'):
        autre = ajouter_mois(ref, signe * 6 * nombre)
        precision = 'semestre'
    elif unite.startswith('decennie'):
        autre = ajouter_mois(ref, signe * 120 * nombre)
        precision = 'decennie'
    else:
        autre = ajouter_mois(ref, signe * 12 * nombre)
        precision = 'annee'
    if sens in ('il y a', 'voici', 'dans'):
        return autre, autre, precision, True
    if passe:
        return autre, ref, precision, True
    return ref, autre, precision, True


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def trouver(texte, reference=None):
    """Toutes les expressions temporelles de ``texte``, dans l'ordre, sans chevauchement.

    Chaque element : voir la docstring du module ; ``start``/``end`` sont les
    offsets dans ``texte``. Les expressions relatives sans reference sont
    renvoyees avec ``resolue=False`` et des dates None.
    """
    reference = en_date(reference)
    norm = normaliser(texte)
    occupe = []
    trouvees = []
    for nom, motif in _MOTIFS:
        for correspondance in motif.finditer(norm):
            debut_m, fin_m = correspondance.span()
            while debut_m < fin_m and norm[debut_m] == ' ':
                debut_m += 1
            if any(not (fin_m <= a or debut_m >= b) for a, b in occupe):
                continue
            groupes = correspondance.groupdict()
            if nom == 'saison' and groupes.get('s') == 'ete' and _ete_participe(norm, correspondance.start('s')):
                continue
            try:
                resultat = _resoudre_motif(nom, groupes, reference, norm, correspondance.start('a') if nom == 'annee' else debut_m)
                resolue = True
            except _NonResolue:
                resultat = (None, None, _precision_par_defaut(nom), True)
                resolue = False
            except (ValueError, KeyError, TypeError, OverflowError):
                continue
            if resultat is None:
                continue
            debut, fin, precision, relative = resultat
            occupe.append((debut_m, fin_m))
            trouvees.append({
                'start': debut_m, 'end': fin_m, 'motif': nom,
                'debut': debut, 'fin': fin, 'precision': precision,
                'relative': relative, 'resolue': resolue,
            })
    trouvees.sort(key=lambda e: e['start'])
    return [_finaliser(element, texte, norm, reference) for element in trouvees]


_AUXILIAIRES_ETE = {'a', 'ai', 'as', 'avons', 'avez', 'ont', 'avait', 'avais', 'avions', 'aviez', 'avaient', 'aura', 'auront',
                    'aurait', 'auraient', 'eut', 'eurent', 'ayant', 'avoir', 'pas', 'jamais', 'deja', 'toujours', 'bien'}


def _ete_participe(norm, position):
    """Vrai si « ete » a ``position`` est le participe d'etre (« a ete », « n'ont jamais ete ») et non la saison."""
    avant = norm[:position]
    if not avant.endswith(' '):
        return False
    precedent = avant.split()[-1:] or ['']
    return precedent[0] in _AUXILIAIRES_ETE


def _precision_par_defaut(nom):
    """Precision affichee pour une expression relative non resolue."""
    return {
        'jour_relatif': 'jour', 'jour_semaine': 'jour', 'semaine': 'semaine', 'decalage': 'mois',
        'annee_relative': 'annee', 'partie_annee': 'mois', 'mois_seul': 'mois', 'saison': 'saison',
        'trimestre': 'trimestre', 'semestre': 'semestre', 'periode_relative': 'trimestre', 'jour_mois': 'jour',
        'jours_intervalle': 'jour', 'mois_intervalle': 'mois',
    }.get(nom, 'jour')


def _finaliser(element, texte, norm, reference):
    """Applique le modificateur place avant l'expression et met en forme."""
    debut = element['debut']
    fin = element['fin']
    start = element['start']
    modificateur = None
    if element['motif'] != 'decalage':
        avant = norm[max(0, start - 40):start]
        correspondance = _MODIFICATEURS.search(avant)
        if correspondance:
            mot = re.sub(r'\s+', ' ', correspondance.group('mod'))
            start = start - (len(avant) - correspondance.start('mod'))
            if mot in ('depuis', 'des'):
                modificateur = 'depuis'
                fin = reference if reference and (debut is None or reference >= debut) else None
                if mot == 'des':
                    modificateur = 'partir'
            elif mot.startswith('a partir') or mot.startswith('a compter'):
                modificateur = 'partir'
                fin = None
            elif mot.startswith('jusqu'):
                modificateur = 'jusqu'
                debut = reference if reference and fin and reference <= fin else None
            elif mot.startswith("d'ici"):
                modificateur = 'dici'
                debut = reference if reference and fin and reference <= fin else None
            elif mot == 'avant' or mot == 'au plus tard':
                modificateur = 'avant'
                fin = debut if mot == 'avant' else fin
                debut = None
            elif mot == 'apres':
                modificateur = 'apres'
                debut = fin
                fin = None
    else:
        sens = norm[element['start']:element['end']]
        if sens.startswith('depuis'):
            modificateur = 'depuis'
        elif sens.startswith("d'ici"):
            modificateur = 'dici'
    if modificateur == 'partir' and element['motif'] != 'decalage':
        fin = None
    type_resultat = 'date' if debut is not None and debut == fin else 'intervalle'
    return {
        'texte': texte[start:element['end']],
        'start': start,
        'end': element['end'],
        'debut': iso(debut),
        'fin': iso(fin),
        'type': type_resultat,
        'nature': 'relative' if element['relative'] else 'absolue',
        'precision': element['precision'],
        'modificateur': modificateur,
        'resolue': element['resolue'],
        'motif': element['motif'],
    }


def resoudre(expr, reference=None):
    """Resolution d'une expression temporelle isolee, ou None si rien n'est reconnu.

    Renvoie la plus longue expression trouvee dans ``expr`` (voir le module).
    Si les regles ne reconnaissent rien et que ``dateparser`` est installe,
    il est essaye en dernier recours (``motif='dateparser'``).
    """
    trouvees = trouver(expr, reference)
    if trouvees:
        return max(trouvees, key=lambda e: e['end'] - e['start'])
    return _dateparser(expr, reference)


def _dateparser(expr, reference):
    """Repli optionnel sur dateparser (import paresseux)."""
    try:
        import dateparser
    except ImportError:
        return None
    reference = en_date(reference)
    reglages = {'PREFER_DATES_FROM': 'past'}
    if reference is not None:
        reglages['RELATIVE_BASE'] = datetime(reference.year, reference.month, reference.day)
    try:
        valeur = dateparser.parse(expr, languages=['fr'], settings=reglages)
    except Exception:
        return None
    if valeur is None:
        return None
    jour = valeur.date().isoformat()
    return {
        'texte': expr, 'start': 0, 'end': len(expr), 'debut': jour, 'fin': jour, 'type': 'date',
        'nature': 'relative' if reference is not None else 'absolue', 'precision': 'jour',
        'modificateur': None, 'resolue': True, 'motif': 'dateparser',
    }


def absolues(texte, reference=None):
    """Expressions dont la date principale est ecrite (nature absolue)."""
    return [e for e in trouver(texte, reference) if e['nature'] == 'absolue' and (e['debut'] or e['fin'])]


def ancre(expression):
    """Date d'ancrage d'une expression : fin connue pour « depuis », sinon debut, sinon fin."""
    if expression.get('modificateur') == 'depuis' and expression.get('fin'):
        return expression['fin']
    return expression.get('debut') or expression.get('fin')
