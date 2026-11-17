"""M6 : proposer les corrections et organiser la revue humaine.

Perimetre (unites a proposer), generation (substitution deterministe, puis
reecriture locale contrainte), controle (NLI + budget de difference), donnees
des cartes de revue, decisions (avec propagation aux enfants), apprentissage.

Toute acceptation cree une NOUVELLE version locale de la page via
``server.apply_patch_version`` (table ``patches``) ; le site d'origine n'est
jamais modifie.
"""
import difflib
import json
import re
import statistics
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from chaine import moteurs
from chaine import noyau
from chaine import schema
from chaine import seuils

PROMPT_VERSION = 'm6-reecriture-v1'
MODE_REECRITURE = 'chaine:m6.reecriture'
TACHE_REECRITURE = (
    "Tu reecris UNE SEULE phrase d'une page web francaise. Modifie le minimum de mots. "
    "Applique uniquement les changements demandes (faits_a_modifier : remplace 'avant' par 'apres' ; "
    "type_changement 'perimee_par_date' : mets la phrase au passe, avec le bilan s'il est donne). "
    "Conserve tous les faits de faits_a_conserver, sans en perdre ni en inventer. "
    "N'utilise aucune connaissance exterieure. Reponds par la phrase reecrite seule, dans le champ 'phrase'. "
    "Si 'echec_precedent' est present, corrige ces defauts."
)
noyau.schema(MODE_REECRITURE, noyau.objet({'phrase': {'type': 'string'}}))

KINDS = ('corrigee', 'perimee_par_date', 'completee', 'source_ajoutee', 'lien_interne', 'mention_maj')
LIBELLES = {
    'corrigee': 'Corrigée', 'perimee_par_date': 'Périmée par date', 'completee': 'Complétée',
    'source_ajoutee': 'Source ajoutée', 'lien_interne': 'Lien interne', 'mention_maj': 'Mention de mise à jour',
}
EN_ATTENTE = ('proposee', 'a_rediger', 'reportee')
DECISIONS = {
    'accepter': 'accepter', 'accepte': 'accepter', 'acceptee': 'accepter',
    'modifier': 'modifier', 'modifie': 'modifier', 'modifiee': 'modifier',
    'rejeter': 'rejeter', 'rejete': 'rejeter', 'rejetee': 'rejeter',
    'archive': 'archive', 'archiver': 'archive',
    'reporter': 'reporter', 'reporte': 'reporter', 'reportee': 'reporter',
}
LIEN_TERMINAUX = ('appliquee', 'acceptee', 'rejetee', 'archive', 'traitee', 'ignoree', 'faite')
PAGES_DATEES = ('actualite', 'depeche', 'compte_rendu', 'news', 'article_date')
STATUTS_FAITS_EXCLUS = ('rejete', 'historique', 'remplace')
MOIS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août', 'septembre', 'octobre',
        'novembre', 'décembre']
ABREVIATIONS = ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.', 'août', 'sept.', 'oct.', 'nov.', 'déc.']
INVARIABLES = {'mois', 'prix', 'pays', 'bras', 'fois', 'temps', 'corps', 'repas', 'bois', 'cas', 'bus', 'jus',
               'parcours', 'concours', 'secours', 'cours', 'succès', 'accès', 'progrès', 'procès', 'plus', 'moins'}
MOTS_OUTILS = {'de', 'd', 'du', 'des', 'en', 'à', 'pour', 'par', 'sur', 'et', 'ou', 'au', 'aux', 'dans', 'avec'}
FUTUR = ('sera', 'seront', 'aura', 'auront', 'prochain', 'prochaine', 'prochains', 'prochaines', 'à venir')
PREFIXE_MAJ = re.compile(r'^\s*Mise à jour du [^:]{3,40}:\s*', re.I)


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def _s():
    return noyau.srv()


def _json(valeur):
    return json.dumps(valeur, ensure_ascii=False, sort_keys=True, default=str)


def _liste_json(valeur):
    """Liste depuis un JSON stocke (ou une liste) ; [] sinon."""
    if isinstance(valeur, list):
        return valeur
    if not valeur:
        return []
    try:
        decode = json.loads(valeur)
    except (TypeError, ValueError):
        return []
    return decode if isinstance(decode, list) else []


def _jour(valeur=None):
    """Date du jour injectable : None, date, datetime ou 'AAAA-MM-JJ'."""
    if valeur is None:
        return date.today()
    if isinstance(valeur, datetime):
        return valeur.date()
    if isinstance(valeur, date):
        return valeur
    return date.fromisoformat(str(valeur)[:10])


def date_fr(valeur):
    """'2026-11-13' -> '13 novembre 2026' (1er pour le premier du mois). '' si illisible."""
    try:
        jour = _jour(valeur)
    except (TypeError, ValueError):
        return ''
    numero = '1er' if jour.day == 1 else str(jour.day)
    return f'{numero} {MOIS[jour.month - 1]} {jour.year}'


def _sans_accents(texte):
    return ''.join(c for c in unicodedata.normalize('NFD', texte or '') if unicodedata.category(c) != 'Mn')


def assurer_schema():
    """Ajoute les colonnes propres a M6 sur ``proposals`` (idempotent)."""
    if not noyau.colonnes('proposals'):
        schema.init()
    existantes = noyau.colonnes('proposals')
    for nom, type_sql in (('decision', 'TEXT'), ('applied_version', 'TEXT'), ('ref_id', 'TEXT'),
                          ('attempts', 'INTEGER')):
        if nom not in existantes:
            schema.colonne('proposals', nom, type_sql)


def est_page_datee(page_type):
    """Vrai si la nature de la page est d'etre datee (depeche, compte rendu, actualite)."""
    valeur = re.sub(r'[^a-z0-9]+', '_', _sans_accents(str(page_type or '')).lower()).strip('_')
    if not valeur:
        return False
    reglage = noyau.reglage('m6.pages_datees')
    liste = [x.strip() for x in reglage.split(',') if x.strip()] if reglage else list(PAGES_DATEES)
    return any(motif in valeur for motif in liste)


# ---------------------------------------------------------------------------
# Differences au mot pres
# ---------------------------------------------------------------------------

_ELISION = r"(?:qu|jusqu|lorsqu|puisqu|[cdjlmnst])['’](?=\w)"
_JETON = re.compile(_ELISION + r"|\w+(?:['’\-]\w+)*|[^\w\s]", re.I)


def _jetons(texte):
    return [(m.start(), m.end()) for m in _JETON.finditer(texte or '')]


def _est_mot(jeton):
    return any(c.isalnum() for c in jeton)


def _tranche(texte, jetons, i1, i2):
    """Tranche exacte couvrant les jetons [i1, i2) avec leurs blancs (les tranches pavent le texte)."""
    if i1 >= i2:
        return ''
    debut = jetons[i1 - 1][1] if i1 > 0 else 0
    fin = len(texte) if i2 == len(jetons) else jetons[i2 - 1][1]
    return texte[debut:fin]


def diff_mots(avant, apres):
    """Differences au mot pres : liste de segments ``{'op': egal|supprime|ajoute, 'texte': ...}``.

    Accents et ponctuation comptent. Les segments ``egal`` portent le texte de ``apres`` (cle
    ``avant`` en plus si les blancs different) : en concatenant egal+supprime on retrouve
    ``avant``, en concatenant egal+ajoute on retrouve ``apres``.
    """
    avant = avant or ''
    apres = apres or ''
    ja = _jetons(avant)
    jb = _jetons(apres)
    ma = [avant[s:e] for s, e in ja]
    mb = [apres[s:e] for s, e in jb]
    segments = []
    for balise, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ma, mb, autojunk=False).get_opcodes():
        texte_a = _tranche(avant, ja, i1, i2)
        texte_b = _tranche(apres, jb, j1, j2)
        if balise == 'equal':
            segment = {'op': 'egal', 'texte': texte_b}
            if texte_a != texte_b:
                segment['avant'] = texte_a
            segments.append(segment)
            continue
        if texte_a or i2 > i1:
            segments.append({'op': 'supprime', 'texte': texte_a})
        if texte_b or j2 > j1:
            segments.append({'op': 'ajoute', 'texte': texte_b})
    if not ja and not jb:
        tail_a, tail_b = avant, apres
        if tail_a or tail_b:
            return [{'op': 'egal', 'texte': tail_b}]
    return segments


def ratio_difference(avant, apres):
    """Ratio de mots changes : mots modifies / mots de ``avant`` (plafonne a 1)."""
    ja = _jetons(avant)
    jb = _jetons(apres)
    ma = [(avant or '')[s:e] for s, e in ja]
    mb = [(apres or '')[s:e] for s, e in jb]
    total_a = sum(1 for m in ma if _est_mot(m))
    total_b = sum(1 for m in mb if _est_mot(m))
    if total_a == 0:
        return 1.0 if total_b else 0.0
    change = 0
    for balise, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ma, mb, autojunk=False).get_opcodes():
        if balise == 'equal':
            continue
        change += max(sum(1 for m in ma[i1:i2] if _est_mot(m)), sum(1 for m in mb[j1:j2] if _est_mot(m)))
    return min(1.0, change / total_a)


# ---------------------------------------------------------------------------
# Formats francais : dates, nombres, accords simples
# ---------------------------------------------------------------------------

_MOIS_SA = {_sans_accents(m): i + 1 for i, m in enumerate(MOIS)}
_MOIS_SA.update({_sans_accents(a).rstrip('.'): i + 1 for i, a in enumerate(ABREVIATIONS)})
_MOTIF_MOIS = '|'.join(sorted((re.escape(k) for k in _MOIS_SA), key=len, reverse=True))
_RE_TEXTE = re.compile(r'^(?P<pre>(?:le|du|au|dès le|des le|depuis le)\s+)?(?:(?P<j>1er|\d{1,2})\s+)?(?P<m>'
                       + _MOTIF_MOIS + r')(?P<pt>\.)?(?:\s+(?P<a>\d{4}))?$', re.I)
_RE_NUM = re.compile(r'^(\d{1,2})([/.\-])(\d{1,2})(?:\2(\d{2,4}))?$')
_RE_MA = re.compile(r'^(\d{1,2})/(\d{4})$')
_RE_ISO = re.compile(r'^(\d{4})-(\d{2})(?:-(\d{2}))?$')
_RE_AN = re.compile(r'^(?P<pre>(?:en|de|l[’\']année)\s+)?(?P<a>\d{4})$', re.I)


def _iso_partiel(valeur):
    """(annee, mois|None, jour|None) depuis 'AAAA', 'AAAA-MM', 'AAAA-MM-JJ' ou une date."""
    if isinstance(valeur, (date, datetime)):
        return valeur.year, valeur.month, valeur.day
    texte = str(valeur or '').strip()
    if re.fullmatch(r'\d{4}', texte):
        return int(texte), None, None
    resultat = _RE_ISO.match(texte[:10]) if len(texte) >= 7 else None
    if not resultat:
        return None
    mois = int(resultat.group(2))
    jour = int(resultat.group(3)) if resultat.group(3) else None
    if not 1 <= mois <= 12 or (jour is not None and not 1 <= jour <= 31):
        return None
    return int(resultat.group(1)), mois, jour


def _analyser_date(texte):
    """Style d'une date ecrite (modele a imiter) ; None si non reconnue."""
    texte = (texte or '').strip()
    resultat = _RE_TEXTE.match(texte)
    if resultat:
        return {'style': 'texte', 'pre': resultat.group('pre') or '', 'jour': bool(resultat.group('j')),
                'annee': bool(resultat.group('a')), 'abrev': bool(resultat.group('pt'))}
    resultat = _RE_NUM.match(texte)
    if resultat:
        return {'style': 'numerique', 'sep': resultat.group(2), 'zero': len(resultat.group(1)) == 2,
                'annee': bool(resultat.group(4)), 'jour': True}
    if _RE_MA.match(texte):
        return {'style': 'numerique', 'sep': '/', 'zero': True, 'annee': True, 'jour': False}
    if _RE_ISO.match(texte):
        return {'style': 'iso'}
    resultat = _RE_AN.match(texte)
    if resultat:
        return {'style': 'annee', 'pre': resultat.group('pre') or ''}
    return None


def formater_date(valeur, modele=''):
    """Ecrit une date ISO (partielle ou non) dans le style de ``modele`` (ancienne valeur).

    Dates en francais : '1er août 2026', 'novembre 2026', '13/11/2026'. ``None`` si illisible.
    """
    iso = _iso_partiel(valeur)
    if iso is None:
        return None
    annee, mois, jour = iso
    style = _analyser_date(modele) or {'style': 'texte', 'pre': '', 'annee': True}
    if style['style'] == 'iso':
        return f'{annee:04d}' + (f'-{mois:02d}' if mois else '') + (f'-{jour:02d}' if jour else '')
    if style['style'] == 'numerique':
        sep = style.get('sep', '/')
        if mois is None:
            return str(annee)
        parties = []
        if jour is not None:
            parties.append(f'{jour:02d}' if style.get('zero', True) else str(jour))
        parties.append(f'{mois:02d}' if style.get('zero', True) else str(mois))
        if style.get('annee', True):
            parties.append(str(annee))
        return sep.join(parties)
    pre = style.get('pre', '') if style['style'] in ('texte', 'annee') else ''
    if mois is None:
        resultat = pre + str(annee)
    else:
        nom = (ABREVIATIONS if style.get('abrev') else MOIS)[mois - 1]
        morceaux = []
        if jour is not None:
            morceaux.append('1er' if jour == 1 else str(jour))
        morceaux.append(nom)
        if style.get('annee', True) or style['style'] == 'annee':
            morceaux.append(str(annee))
        resultat = pre + ' '.join(morceaux)
    if (modele or '')[:1].isupper() and resultat[:1].isalpha():
        resultat = resultat[:1].upper() + resultat[1:]
    return resultat


def _decimal(texte):
    """Decimal depuis un nombre ecrit (francais ou canonique) ; None si illisible."""
    brut = re.sub(r'[\s  ]', '', str(texte or '')).replace('−', '-')
    if not re.fullmatch(r'-?\d+(?:[.,]\d+)*', brut):
        return None
    if ',' in brut and '.' in brut:
        decimal = ',' if brut.rfind(',') > brut.rfind('.') else '.'
        autre = '.' if decimal == ',' else ','
        brut = brut.replace(autre, '').replace(decimal, '.')
    elif ',' in brut:
        brut = brut.replace(',', '.') if brut.count(',') == 1 else brut.replace(',', '')
    elif '.' in brut and re.fullmatch(r'-?\d{1,3}(\.\d{3})+', brut):
        brut = brut.replace('.', '')
    try:
        return Decimal(brut)
    except InvalidOperation:
        return None


_RE_NOMBRE = re.compile(r'^(?P<pre>[^\d\-−]*)(?P<num>-?\d[\d\s  .,]*\d|-?\d)(?P<suf>.*)$', re.S)


def formater_nombre(valeur, modele=''):
    """Ecrit un nombre avec les separateurs francais (milliers insecables, virgule decimale).

    Le prefixe et le suffixe ('environ ', ' %', ' millions') de ``modele`` sont conserves,
    sauf si ``valeur`` porte son propre suffixe. ``None`` si illisible.
    """
    brut = str(valeur or '').strip()
    cible = _RE_NOMBRE.match(brut)
    if not cible:
        return None
    nombre = _decimal(cible.group('num'))
    if nombre is None:
        return None
    ancien = _RE_NOMBRE.match(modele or '')
    sep = ' '
    if ancien:
        partie = ancien.group('num')
        trouve = re.search(r'\d([\s  ])\d', partie)
        if trouve:
            sep = trouve.group(1)
        elif len(re.sub(r'\D.*', '', partie.lstrip('-'))) >= 5:
            sep = ''
    texte = format(nombre, 'f')
    entier, _, decimales = texte.partition('.')
    signe = '-' if entier.startswith('-') else ''
    entier = entier.lstrip('-')
    if len(entier) >= 5 or (len(entier) == 4 and sep and ancien and re.search(r'\d[\s  ]\d', ancien.group('num'))):
        reste = entier
        groupes = []
        while len(reste) > 3:
            groupes.insert(0, reste[-3:])
            reste = reste[:-3]
        groupes.insert(0, reste)
        entier = sep.join(groupes)
    resultat = signe + entier + (',' + decimales if decimales else '')
    suffixe = cible.group('suf') if cible.group('suf').strip() else (ancien.group('suf') if ancien else '')
    prefixe = cible.group('pre') if cible.group('pre').strip() else (ancien.group('pre') if ancien else '')
    return prefixe + resultat + suffixe


TYPES_DATE = ('date', 'periode', 'jour', 'mois', 'annee')
TYPES_NOMBRE = ('nombre', 'entier', 'montant', 'pourcentage', 'quantite', 'prix', 'decimal', 'duree')


def _type_effectif(type_objet, nouveau, ancien):
    """'date', 'nombre' ou 'nom' d'apres le type declare puis la forme des valeurs."""
    declare = _sans_accents(str(type_objet or '')).lower()
    if declare in TYPES_DATE:
        return 'date'
    if declare in TYPES_NOMBRE:
        return 'nombre'
    if _iso_partiel(nouveau) and _analyser_date(ancien):
        return 'date'
    if _RE_NOMBRE.match(str(nouveau or '').strip()) and _decimal(_RE_NOMBRE.match(str(nouveau).strip()).group('num')) is not None \
            and _RE_NOMBRE.match(ancien or ''):
        return 'nombre'
    return 'nom'


def formater_valeur(type_objet, nouveau, ancien, brut=None):
    """Nouvelle valeur au bon format, calquee sur l'ancienne ; ``None`` si impossible."""
    genre = _type_effectif(type_objet, nouveau, ancien)
    if genre == 'date':
        if '/' in str(nouveau or '') and re.match(r'\d{4}-', str(nouveau)):
            return _intervalle(nouveau, ancien)
        resultat = formater_date(nouveau, ancien)
        if resultat is not None:
            return resultat
        resultat = formater_date(brut, ancien) if brut else None
        return resultat or (str(brut).strip() if brut else None)
    if genre == 'nombre':
        resultat = formater_nombre(nouveau, ancien)
        return resultat if resultat is not None else (formater_nombre(brut, ancien) if brut else None)
    texte = str(brut if brut else nouveau or '').strip()
    return texte or None


def _intervalle(nouveau, ancien):
    """'2026-08-01/2026-08-31' -> 'du 1er au 31 août 2026' (selon le style de l'ancien)."""
    debut, _, fin = str(nouveau).partition('/')
    a, b = _iso_partiel(debut), _iso_partiel(fin)
    if not a or not b or a[1] is None or b[1] is None or a[2] is None or b[2] is None:
        return None
    ancien_annee = bool(re.search(r'\d{4}', ancien or '')) or not ancien
    j1 = '1er' if a[2] == 1 else str(a[2])
    j2 = '1er' if b[2] == 1 else str(b[2])
    fin_txt = f'{j2} {MOIS[b[1] - 1]}' + (f' {b[0]}' if ancien_annee else '')
    if (a[0], a[1]) == (b[0], b[1]):
        return f'du {j1} au {fin_txt}'
    debut_txt = f'{j1} {MOIS[a[1] - 1]}' + (f' {a[0]}' if ancien_annee and a[0] != b[0] else '')
    return f'du {debut_txt} au {fin_txt}'


def _voyelle(texte):
    premier = _sans_accents((texte or '').lstrip('«"\'’ ')[:1]).lower()
    return premier in 'aeiouy'


def _accord_avant(phrase, debut, fin):
    """Accords simples avant la valeur remplacee : elisions et contractions (de le -> du)."""
    nouveau = phrase[debut:fin]
    avant = phrase[:debut]
    resultat = re.search(r"(?i)\b(de|à)\s+$", avant)
    article = re.match(r'(?i)(le|les)\s', nouveau)
    if resultat and article:
        mot = resultat.group(1).lower()
        table = {('de', 'le'): 'du', ('de', 'les'): 'des', ('à', 'le'): 'au', ('à', 'les'): 'aux'}
        contraction = table[(mot, article.group(1).lower())]
        if resultat.group(1)[0].isupper():
            contraction = contraction.capitalize()
        return avant[:resultat.start()] + contraction + ' ' + nouveau[article.end():] + phrase[fin:], \
            len(phrase) - len(avant[:resultat.start()] + contraction + ' ' + nouveau[article.end():] + phrase[fin:])
    resultat = re.search(r"(?i)\b(de|le|la)\s+$", avant)
    if resultat and _voyelle(nouveau):
        mot = resultat.group(1)
        elision = ('D' if mot[0].isupper() else 'd') + "'" if mot.lower() == 'de' else ('L' if mot[0].isupper() else 'l') + "'"
        return avant[:resultat.start()] + elision + phrase[debut:], 0
    resultat = re.search(r"(?i)\b(d)['’]$", avant)
    if resultat and nouveau and not _voyelle(nouveau) and nouveau[:1].isalpha():
        return avant[:resultat.start()] + ('D' if resultat.group(1) == 'D' else 'd') + 'e ' + phrase[debut:], 0
    return phrase, 0


def _pluriel(phrase, fin, ancien_nombre, nouveau_nombre):
    """Accorde le nom qui suit un nombre (singulier sous 2, sinon pluriel) quand le nombre change de classe."""
    if ancien_nombre is None or nouveau_nombre is None:
        return phrase
    ancien_pluriel = abs(ancien_nombre) >= 2
    nouveau_pluriel = abs(nouveau_nombre) >= 2
    if ancien_pluriel == nouveau_pluriel:
        return phrase
    suite = re.match(r'(\s+)([A-Za-zÀ-ÿ]+)', phrase[fin:])
    if not suite:
        return phrase
    mot = suite.group(2)
    if mot.lower() in MOTS_OUTILS or len(mot) < 2:
        return phrase
    if nouveau_pluriel:
        nouveau_mot = mot if mot[-1].lower() in 'sxz' else mot + 's'
    elif mot.lower() == 'ans':
        nouveau_mot = mot[:-1]
    elif mot[-1].lower() == 's' and mot.lower() not in INVARIABLES and len(mot) > 3:
        nouveau_mot = mot[:-1]
    else:
        nouveau_mot = mot
    debut_mot = fin + len(suite.group(1))
    return phrase[:debut_mot] + nouveau_mot + phrase[debut_mot + len(mot):]


def substituer(phrase, remplacements):
    """Substitution deterministe : remplace le span de chaque valeur par la nouvelle valeur.

    ``remplacements`` : liste de dicts ``debut``, ``fin`` (offsets dans ``phrase``), ``type``,
    ``norm`` (valeur normalisee), ``brut`` (valeur telle qu'ecrite). Renvoie la nouvelle phrase
    ou ``None`` si un remplacement est impossible (span incoherent, chevauchement, format).
    """
    liste = sorted(remplacements, key=lambda r: r['debut'])
    for i, r in enumerate(liste):
        if not 0 <= r['debut'] < r['fin'] <= len(phrase) or (i and r['debut'] < liste[i - 1]['fin']):
            return None
    nouveaux = []
    for r in liste:
        ancien = phrase[r['debut']:r['fin']]
        valeur = formater_valeur(r.get('type'), r.get('norm'), ancien, r.get('brut'))
        if not valeur:
            return None
        nouveaux.append((r, ancien, valeur))
    for r, ancien, valeur in reversed(nouveaux):
        fin_valeur = r['debut'] + len(valeur)
        phrase = phrase[:r['debut']] + valeur + phrase[r['fin']:]
        if _type_effectif(r.get('type'), r.get('norm'), ancien) == 'nombre' and re.fullmatch(r'[\d\s  .,\-]+', valeur):
            phrase = _pluriel(phrase, fin_valeur, _decimal(ancien), _decimal(valeur))
        phrase, _ = _accord_avant(phrase, r['debut'], fin_valeur)
    return phrase


# ---------------------------------------------------------------------------
# Localisation dans le texte
# ---------------------------------------------------------------------------

_BLANC = re.compile(r'\r?\n[ \t]*\r?\n')


def bornes_paragraphe(texte, debut, fin):
    """(debut, fin) du paragraphe (separe par lignes vides) qui contient [debut, fin)."""
    a = 0
    for fenetre in (6000, len(texte) + 1):
        base = max(0, debut - fenetre)
        dernier = None
        for m in _BLANC.finditer(texte, base, debut):
            dernier = m
        if dernier is not None:
            a = dernier.end()
            break
        if base == 0:
            break
    suivant = _BLANC.search(texte, fin)
    b = suivant.start() if suivant else len(texte)
    return a, b


def _phrase_dans_texte(texte, unite):
    """Offsets de la phrase de l'unite dans ``texte`` (span si coherent, sinon recherche de la citation)."""
    quote = unite.get('quote') or ''
    debut, fin = unite.get('span_start'), unite.get('span_end')
    if debut is not None and fin is not None and 0 <= debut < fin <= len(texte):
        morceau = texte[debut:fin]
        if morceau.strip() and (not quote or morceau == quote):
            return debut, fin
    cible = quote or unite.get('text') or ''
    if cible and cible in texte:
        positions = [m.start() for m in re.finditer(re.escape(cible), texte)]
        if debut is not None:
            positions.sort(key=lambda p: abs(p - debut))
        return positions[0], positions[0] + len(cible)
    return None


def _localiser_objet(fait, phrase, phrase_debut):
    """Offsets (relatifs a la phrase) de la valeur d'un fait ; None si introuvable ou ambigu."""
    objet = fait.get('object')
    debut, fin = fait.get('span_start'), fait.get('span_end')
    regions = []
    if debut is not None and fin is not None and fin > debut:
        if phrase_debut <= debut and fin <= phrase_debut + len(phrase):
            regions.append((debut - phrase_debut, fin - phrase_debut))
        if fin <= len(phrase):
            regions.append((debut, fin))
    if not objet:
        for a, b in regions:
            if 0 < b - a <= 40:
                return a, b
        return None
    for a, b in regions:
        position = phrase.find(objet, a, b)
        if position >= 0:
            return position, position + len(objet)
    if phrase.count(objet) == 1:
        position = phrase.find(objet)
        return position, position + len(objet)
    return None


# ---------------------------------------------------------------------------
# Faits d'une unite
# ---------------------------------------------------------------------------

def _verbaliser(fait, ancien=None, nouveau=None):
    """Phrase autonome d'un fait (verbalisation stockee, sinon sujet + predicat + objet)."""
    texte = (fait.get('verbalization') or '').strip()
    if texte and ancien and nouveau and ancien in texte:
        return texte.replace(ancien, nouveau, 1)
    if texte:
        return texte
    return ' '.join(x for x in (fait.get('subject'), str(fait.get('predicate') or '').replace('_', ' '), fait.get('object')) if x)


def _faits_unite(unit_id, phrase, phrase_debut):
    """Partage les faits d'une unite : remplacements (ancien -> nouveau), ajouts, conserves."""
    S = _s()
    faits = S.query('SELECT * FROM facts WHERE unit_id=? ORDER BY span_start', (unit_id,))
    par_id = {f['id']: f for f in faits}
    cibles = {}
    for f in faits:
        suivant = f.get('replaced_by')
        if suivant:
            cibles[f['id']] = par_id.get(suivant) or S.query('SELECT * FROM facts WHERE id=?', (suivant,), True)
    ids_nouveaux = {n['id'] for n in cibles.values() if n}
    changes, conserves, ajouts = [], [], []
    for f in faits:
        if f['id'] in cibles and cibles[f['id']]:
            nouveau = cibles[f['id']]
            position = _localiser_objet(f, phrase, phrase_debut)
            ancien_txt = phrase[position[0]:position[1]] if position else f.get('object')
            valeur = nouveau.get('object_norm') or nouveau.get('object')
            changes.append({
                'ancien_fait_id': f['id'], 'nouveau_fait_id': nouveau['id'], 'avant': ancien_txt,
                'apres': nouveau.get('object') or valeur, 'norm': valeur, 'type': nouveau.get('object_type') or f.get('object_type'),
                'position': list(position) if position else None,
                'verbalisation': _verbaliser(nouveau) if nouveau.get('verbalization') else _verbaliser(f, f.get('object'), nouveau.get('object')),
                'origin': nouveau.get('origin'),
            })
        elif f.get('origin') == 'externe':
            if f['id'] in ids_nouveaux or (f.get('status') or '') in STATUTS_FAITS_EXCLUS:
                continue
            if f.get('object') and f['object'] in phrase:
                continue
            ajouts.append({'ancien_fait_id': None, 'nouveau_fait_id': f['id'], 'avant': None, 'apres': f.get('object'),
                           'norm': f.get('object_norm') or f.get('object'), 'type': f.get('object_type'),
                           'verbalisation': _verbaliser(f), 'origin': 'externe'})
        elif (f.get('status') or '') not in STATUTS_FAITS_EXCLUS:
            conserves.append({'fait_id': f['id'], 'verbalisation': _verbaliser(f), 'origin': f.get('origin')})
    return changes, ajouts, conserves


# ---------------------------------------------------------------------------
# Perimetre
# ---------------------------------------------------------------------------

def _criticite(ligne):
    if ligne.get('critical_score') is not None:
        return float(ligne['critical_score'])
    return 1.0 if ligne.get('critical') else 0.0


def perimetre(aujourdhui=None, unit_ids=None, avec_exclusions=False):
    """Unites a proposer : liste de dicts (``unit_id``, ``kind``, ``mode``, criticite, page, ancrage...).

    Corrigee, perimee_par_date, completee (si critique), source_ajoutee (confirmee, source manquante,
    source de verification connue), lien_interne (``link_recommendations`` ouvertes). Une seule
    proposition par unite, par priorite. Exclus : unites historique, opinions ; pour les pages
    datees (depeche, compte rendu) le type devient ``mention_maj``. Seules les dernieres versions
    de page sont examinees. ``avec_exclusions=True`` renvoie aussi ``[{unit_id, raison}]``.
    """
    assurer_schema()
    S = _s()
    filtre, args = '', []
    if unit_ids:
        filtre = ' AND u.id IN (' + ','.join('?' for _ in unit_ids) + ')'
        args = list(unit_ids)
    base = (
        "SELECT u.id, u.version_id, u.status, u.verdict, u.temporal_status, u.sourcing, u.source_manquante, "
        "u.critical, u.critical_score, u.anchor_date, u.anchor_kind, u.span_start, v.page_type, p.url, p.title, "
        "ct.type claim_type, "
        "EXISTS(SELECT 1 FROM sources s WHERE s.unit_id=u.id AND s.kind='verification' AND COALESCE(s.url,'')<>'') a_source "
        "FROM units u JOIN versions v ON v.id=u.version_id JOIN pages p ON p.id=v.page_id "
        "LEFT JOIN claim_types ct ON ct.unit_id=u.id "
        "WHERE COALESCE(u.excluded,0)=0 AND v.id=(SELECT v2.id FROM versions v2 WHERE v2.page_id=v.page_id "
        "ORDER BY v2.created DESC, v2.rowid DESC LIMIT 1) "
    )
    lignes = S.query(base + "AND (u.verdict IN ('corrigee','completee','confirmee') OR u.temporal_status='perimee_par_date' "
                     "OR u.source_manquante=1)" + filtre, tuple(args))
    candidats, exclus, vus = [], [], set()
    for ligne in lignes:
        raison = _exclusion(ligne)
        if raison:
            exclus.append({'unit_id': ligne['id'], 'raison': raison})
            continue
        kind = None
        if ligne['verdict'] == 'corrigee':
            kind = 'corrigee'
        elif ligne['temporal_status'] == 'perimee_par_date':
            kind = 'perimee_par_date'
        elif ligne['verdict'] == 'completee':
            if ligne['critical']:
                kind = 'completee'
            else:
                exclus.append({'unit_id': ligne['id'], 'raison': 'non_critique'})
                continue
        elif ligne['verdict'] == 'confirmee' and ligne['a_source'] \
                and (ligne['source_manquante'] or ligne['sourcing'] in ('sans_source', 'connaissance_generale')) \
                and ligne['sourcing'] not in ('sourcee_externe', 'auto_sourcee'):
            # Sourcee a posteriori : toute unite confirmee qui n'avait pas de source (doc M6, perimetre).
            kind = 'source_ajoutee'
        if kind is None:
            continue
        mode, origine = ('ajout' if kind == 'source_ajoutee' else 'reecriture'), kind
        if kind in ('corrigee', 'perimee_par_date', 'completee') and est_page_datee(ligne['page_type']):
            kind, mode = 'mention_maj', 'mention'
        vus.add(ligne['id'])
        candidats.append(_candidat(ligne, kind, mode, origine))
    liens = S.query(
        "SELECT lr.id ref_id, lr.unit_id, lr.to_version, lr.status lien_statut, u.version_id, u.status, u.verdict, "
        "u.temporal_status, u.sourcing, u.critical, u.critical_score, u.anchor_date, u.anchor_kind, u.span_start, "
        "v.page_type, p.url, p.title, ct.type claim_type FROM link_recommendations lr JOIN units u ON u.id=lr.unit_id "
        "JOIN versions v ON v.id=u.version_id JOIN pages p ON p.id=v.page_id LEFT JOIN claim_types ct ON ct.unit_id=u.id "
        "WHERE COALESCE(u.excluded,0)=0 AND v.id=(SELECT v2.id FROM versions v2 WHERE v2.page_id=v.page_id "
        "ORDER BY v2.created DESC, v2.rowid DESC LIMIT 1)" + filtre.replace('u.id', 'lr.unit_id') + " ORDER BY lr.created, lr.id",
        tuple(args))
    for ligne in liens:
        if (ligne['lien_statut'] or '') in LIEN_TERMINAUX or ligne['unit_id'] in vus:
            continue
        raison = _exclusion(ligne)
        if raison:
            exclus.append({'unit_id': ligne['unit_id'], 'raison': raison})
            continue
        vus.add(ligne['unit_id'])
        ligne['id'] = ligne['unit_id']
        candidats.append(_candidat(ligne, 'lien_interne', 'ajout', 'lien_interne', ligne['ref_id']))
    candidats.sort(key=lambda c: (-c['criticite'], c['url'] or '', c['span_start'] if c['span_start'] is not None else 0))
    return (candidats, exclus) if avec_exclusions else candidats


def _exclusion(ligne):
    """Raison d'exclusion du document (historique, opinion) ou None."""
    if ligne.get('status') == 'historique' or ligne.get('temporal_status') == 'obsolete_historique':
        return 'historique'
    if ligne.get('sourcing') == 'opinion' or ligne.get('claim_type') == 'opinion':
        return 'opinion'
    return None


def _candidat(ligne, kind, mode, origine, ref_id=None):
    return {'unit_id': ligne['id'], 'version_id': ligne['version_id'], 'kind': kind, 'mode': mode, 'motif_origine': origine,
            'criticite': _criticite(ligne), 'url': ligne['url'], 'titre': ligne['title'], 'page_type': ligne.get('page_type'),
            'anchor_date': ligne.get('anchor_date'), 'anchor_kind': ligne.get('anchor_kind'), 'span_start': ligne.get('span_start'),
            'ref_id': ref_id}


# ---------------------------------------------------------------------------
# Controle
# ---------------------------------------------------------------------------

def _implique(resultat, seuil):
    probas = resultat.get('probas') or {}
    score = probas.get('implication', resultat.get('score') if resultat.get('label') == 'implication' else 0.0)
    return resultat.get('label') == 'implication' and score >= seuil, float(score or 0.0)


def _phrase_de(proposition):
    """Phrase modifiee contenue dans ``after`` (sans l'habillage de la mention de mise a jour)."""
    apres = proposition.get('after') or ''
    if proposition.get('kind') == 'mention_maj':
        return PREFIXE_MAJ.sub('', apres, count=1)
    return apres


def controler(proposition):
    """Controle d'une proposition : NLI des faits nouveaux et conserves, budget de difference.

    ``proposition`` : dict avec ``before``, ``after``, ``kind``, ``paragraph``, ``facts_changed``,
    ``facts_kept`` (listes ou JSON). Renvoie ``ok``, ``problemes`` (liste de dicts ``code``, ``detail``),
    ``nouveaux`` et ``conserves`` (verdict par fait), ``diff_ratio``, ``budget``, ``budget_ok``, ``nli_min``.
    Leve l'erreur du moteur NLI s'il est indisponible (jamais de repli par regles).
    """
    avant = proposition.get('before') or ''
    kind = proposition.get('kind') or 'corrigee'
    apres = proposition.get('after') or ''
    changes = _liste_json(proposition.get('facts_changed'))
    gardes = _liste_json(proposition.get('facts_kept'))
    problemes = []
    resultat = {'ok': False, 'problemes': problemes, 'nouveaux': [], 'conserves': [], 'diff_ratio': None,
                'budget': seuils.get('m6.diff_ratio_max'), 'budget_ok': None, 'nli_min': None, 'moteur': None}
    if not apres.strip():
        problemes.append({'code': 'texte_vide', 'detail': 'Proposition vide.'})
        return resultat
    if kind in ('source_ajoutee', 'lien_interne'):
        simple = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', apres)
        if kind == 'source_ajoutee':
            conserve = apres.startswith(avant)
        else:
            conserve = simple == avant
        if not conserve:
            problemes.append({'code': 'phrase_alteree', 'detail': "La phrase d'origine doit rester inchangée."})
        resultat.update(ok=conserve, budget_ok=None, diff_ratio=0.0, controle='texte_conserve')
        return resultat
    phrase = _phrase_de(proposition)
    resultat['diff_ratio'] = round(ratio_difference(avant, phrase), 4)
    if kind != 'mention_maj':
        resultat['budget_ok'] = resultat['diff_ratio'] <= resultat['budget']
        if not resultat['budget_ok']:
            problemes.append({'code': 'budget_difference',
                              'detail': f"{resultat['diff_ratio']:.2f} de mots changés pour un budget de {resultat['budget']:.2f}."})
    if phrase.strip() == avant.strip():
        problemes.append({'code': 'phrase_inchangee', 'detail': "La phrase n'a pas changé."})
    if kind == 'perimee_par_date':
        restes = [m for m in FUTUR if re.search(r'(?<!\w)' + re.escape(m) + r'(?!\w)', avant, re.I)
                  and re.search(r'(?<!\w)' + re.escape(m) + r'(?!\w)', phrase, re.I)]
        if restes:
            problemes.append({'code': 'futur_conserve', 'detail': 'Marqueur de futur encore présent : ' + ', '.join(restes)})
    if not changes and not gardes:
        problemes.append({'code': 'faits_absents', 'detail': 'Aucun fait connu : contrôle NLI impossible.'})
        return resultat
    paragraphe = proposition.get('paragraph') or ''
    avec_contexte = paragraphe.replace(avant, phrase, 1) if avant and avant in paragraphe else phrase
    paires = []
    for f in changes:
        paires.append((phrase, f.get('verbalisation') or f.get('apres') or ''))
    for f in gardes:
        premisse = avec_contexte if f.get('origin') == 'inference_contextuelle' else phrase
        paires.append((premisse, f.get('verbalisation') or ''))
    verdicts = moteurs.nli_lot(paires) if paires else []
    resultat['moteur'] = (verdicts[0].get('engine') if verdicts else None) or moteurs.dernier('nli')
    if verdicts and moteurs._est_llm(resultat['moteur']):
        resultat['double_passe'] = 'faible'
        noyau.noter_moteur('double_passe', 'faible')
    seuil_n = seuils.get('m6.nli.faits_nouveaux')
    seuil_c = seuils.get('m6.nli.faits_conserves')
    scores = []
    for index, verdict in enumerate(verdicts):
        nouveau = index < len(changes)
        fait = changes[index] if nouveau else gardes[index - len(changes)]
        ok, score = _implique(verdict, seuil_n if nouveau else seuil_c)
        scores.append(score)
        ligne = {'hypothese': paires[index][1], 'label': verdict.get('label'), 'score': round(score, 4), 'ok': ok,
                 'fait_id': fait.get('nouveau_fait_id') if nouveau else fait.get('fait_id')}
        resultat['nouveaux' if nouveau else 'conserves'].append(ligne)
        if not ok:
            problemes.append({'code': 'fait_nouveau_non_implique' if nouveau else 'information_perdue',
                              'detail': ('Fait nouveau non impliqué : ' if nouveau else 'Information perdue : ') + paires[index][1]})
    resultat['nli_min'] = round(min(scores), 4) if scores else None
    resultat['ok'] = not problemes
    return resultat


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _reecrire(contexte, changes, gardes, echec=None):
    """Reecriture locale de la seule phrase par le LLM local contraint ; None si indisponible."""
    S = _s()
    payload = {
        'mode': MODE_REECRITURE, 'prompt_version': PROMPT_VERSION, 'phrase': contexte['before'],
        'paragraphe': contexte['paragraphe'], 'date_du_jour': contexte['jour'].isoformat(),
        'type_changement': contexte['motif_origine'],
        'faits_a_modifier': [{'avant': f.get('avant'), 'apres': f.get('apres'), 'fait': f.get('verbalisation')} for f in changes],
        'faits_a_conserver': [f.get('verbalisation') for f in gardes],
        'budget_mots_changes': seuils.get('m6.diff_ratio_max'),
    }
    if echec:
        payload['echec_precedent'] = [p.get('detail') for p in echec]
    if moteurs.raison_double_passe():
        # La reecriture du LLM ne pourrait pas etre controlee par un NLI distinct : « a rediger par un humain ».
        return None
    try:
        sortie = noyau.llm(TACHE_REECRITURE, payload)
    except S.OperationalError:
        if getattr(S, 'STOP', None) is not None and S.STOP.is_set():
            raise
        return None
    phrase = (sortie.get('phrase') or '').strip().strip('«»"').strip()
    return phrase or None


def _habiller(contexte, phrase):
    """Texte final : la phrase, ou la mention datee pour les contenus d'actualite."""
    if contexte['kind'] == 'mention_maj':
        return f"Mise à jour du {date_fr(contexte['jour'])} : {phrase}"
    return phrase


def _contexte(candidat, jour):
    S = _s()
    ligne = S.query('SELECT u.*, v.text vtexte, p.url, p.title FROM units u JOIN versions v ON v.id=u.version_id '
                    'JOIN pages p ON p.id=v.page_id WHERE u.id=?', (candidat['unit_id'],), True)
    texte = ligne['vtexte'] or ''
    position = _phrase_dans_texte(texte, ligne)
    contexte = {'unite': ligne, 'texte': texte, 'position': position, 'jour': jour, 'kind': candidat['kind'],
                'motif_origine': candidat['motif_origine'], 'candidat': candidat, 'before': None, 'paragraphe': ''}
    if position:
        debut, fin = position
        a, b = bornes_paragraphe(texte, debut, fin)
        contexte.update(before=texte[debut:fin], paragraphe=texte[a:b])
    return contexte


def _proposer_texte(contexte):
    """Substitution puis reecriture ; controle ; au plus ``m6.tentatives_max`` essais."""
    S = _s()
    avant = contexte['before']
    base = {'checks': {}, 'method': 'reecriture_llm', 'after': None, 'facts_changed': [], 'facts_kept': [], 'diff_ratio': None}
    if not avant:
        base['checks'] = {'ok': False, 'problemes': [{'code': 'phrase_introuvable', 'detail': 'Phrase introuvable dans le texte.'}]}
        return dict(base, status='a_rediger', method='humain')
    changes, ajouts, gardes = _faits_unite(contexte['unite']['id'], avant, contexte['position'][0])
    motif = contexte['motif_origine']
    nouveaux = changes + (ajouts if motif in ('completee', 'perimee_par_date') else [])
    base.update(facts_changed=nouveaux, facts_kept=gardes)
    if (motif == 'corrigee' and not changes) or not (nouveaux or gardes):
        code = 'valeur_corrigee_absente' if motif == 'corrigee' and not changes else 'faits_absents'
        base['checks'] = {'ok': False, 'problemes': [{'code': code, 'detail': 'Faits M1/M5 indisponibles pour cette unité.'}]}
        return dict(base, status='a_rediger', method='humain')
    substitution = None
    if motif == 'corrigee' and changes and all(f['position'] for f in changes):
        substitution = [{'debut': f['position'][0], 'fin': f['position'][1], 'type': f['type'], 'norm': f['norm'], 'brut': f['apres']}
                        for f in changes]
    tentatives, echec, dernier = [], None, None
    for numero in range(1, int(seuils.get('m6.tentatives_max')) + 1):
        S.checkpoint()
        methode, phrase = 'reecriture_llm', None
        if numero == 1 and substitution:
            methode, phrase = 'substitution', substituer(avant, substitution)
        if phrase is None:
            phrase = _reecrire(contexte, nouveaux, gardes, echec)
        if phrase is None:
            tentatives.append({'methode': methode, 'ok': False, 'problemes': [{'code': 'generation_impossible', 'detail': 'Aucune phrase produite.'}]})
            echec = tentatives[-1]['problemes']
            continue
        proposition = {'kind': contexte['kind'], 'before': avant, 'after': _habiller(contexte, phrase), 'paragraph': contexte['paragraphe'],
                       'facts_changed': nouveaux, 'facts_kept': gardes}
        controle = controler(proposition)
        tentatives.append({'methode': methode, 'phrase': phrase, 'ok': controle['ok'], 'problemes': controle['problemes']})
        dernier = (methode, proposition, controle)
        if controle['ok']:
            break
        echec = controle['problemes']
    if dernier is None:
        base['checks'] = {'ok': False, 'tentatives': tentatives, 'problemes': echec or []}
        return dict(base, status='a_rediger')
    methode, proposition, controle = dernier
    controle = dict(controle, tentatives=tentatives, motif_origine=motif)
    return dict(base, method=methode, after=proposition['after'], checks=controle, diff_ratio=controle['diff_ratio'],
                status='proposee' if controle['ok'] else 'a_rediger')


def _format_lien(texte, url):
    if noyau.reglage('m6.format_lien', 'markdown') == 'texte':
        return f'{texte} ({url})'
    return f'[{texte}]({url})'


def _proposer_source(contexte):
    S = _s()
    avant = contexte['before']
    if not avant:
        return {'status': 'a_rediger', 'checks': {'ok': False, 'problemes': [{'code': 'phrase_introuvable', 'detail': ''}]}, 'method': 'humain'}
    source = S.query("SELECT url, domain, source_date, passage, contribution FROM sources WHERE unit_id=? AND kind='verification' "
                     "AND COALESCE(url,'')<>'' ORDER BY confidence DESC, source_date DESC LIMIT 1", (contexte['unite']['id'],), True)
    domaine = source['domain'] or _domaine(source['url'])
    apres = avant + ' (' + _format_lien(domaine, source['url']) + ')'
    controle = controler({'kind': 'source_ajoutee', 'before': avant, 'after': apres})
    return {'after': apres, 'method': 'substitution', 'checks': controle, 'diff_ratio': 0.0, 'status': 'proposee' if controle['ok'] else 'a_rediger',
            'sources': [p for p in _preuves_stockees(contexte['unite']['id']) if p['url'] == source['url']][:1]}


def _proposer_lien(contexte):
    S = _s()
    avant = contexte['before']
    ref = S.query('SELECT lr.*, p.url FROM link_recommendations lr JOIN versions v ON v.id=lr.to_version JOIN pages p ON p.id=v.page_id '
                  'WHERE lr.id=?', (contexte['candidat']['ref_id'],), True)
    if not avant or not ref:
        return {'status': 'a_rediger', 'checks': {'ok': False, 'problemes': [{'code': 'phrase_introuvable', 'detail': ''}]}, 'method': 'humain'}
    ancre = ref['anchor'] or ''
    if ref['url'] in avant:
        return None
    position = avant.find(ancre) if ancre else -1
    if position < 0 and ancre:
        position = avant.casefold().find(ancre.casefold())
    if position < 0:
        return {'status': 'a_rediger', 'method': 'humain', 'checks': {'ok': False, 'problemes': [{'code': 'ancre_introuvable', 'detail': ancre}]}}
    texte_ancre = avant[position:position + len(ancre)]
    apres = avant[:position] + _format_lien(texte_ancre, ref['url']) + avant[position + len(ancre):]
    controle = controler({'kind': 'lien_interne', 'before': avant, 'after': apres})
    return {'after': apres, 'method': 'substitution', 'checks': controle, 'diff_ratio': 0.0, 'status': 'proposee' if controle['ok'] else 'a_rediger',
            'sources': [{'url': ref['url'], 'domaine': _domaine(ref['url']), 'date': None, 'passage': ref['reason'], 'verdict_nli': None,
                         'score_nli': None, 'contribution': 'lien_interne'}]}


def _domaine(url):
    hote = urlparse(url or '').netloc.lower()
    return hote[4:] if hote.startswith('www.') else hote


def _preuves_stockees(unit_id):
    """Instantane des preuves (sources de verification) utilisees pour la proposition.

    ``verdict_nli`` / ``score_nli`` : label et score du NLI de M5 sur le passage (``evidence``), de preference
    une preuve d'un groupe de l'unite ; ``contribution`` : role de la source (confirmation, correction...).
    """
    S = _s()
    lignes = S.query("SELECT url, domain, source_date, passage, contribution FROM sources WHERE unit_id=? AND kind='verification' ORDER BY confidence DESC LIMIT 10", (unit_id,))
    preuves = []
    for l in lignes:
        nli = S.query(
            "SELECT e.nli, e.nli_score FROM evidence e WHERE e.url=? AND e.nli IS NOT NULL ORDER BY "
            "EXISTS(SELECT 1 FROM unit_group_members m WHERE m.group_id=e.group_id AND m.unit_id=?) DESC, e.nli_score DESC LIMIT 1",
            (l['url'], unit_id), True) or {}
        preuves.append({'url': l['url'], 'domaine': l['domain'] or _domaine(l['url']), 'date': l['source_date'], 'passage': l['passage'],
                        'verdict_nli': nli.get('nli'), 'score_nli': float(nli['nli_score']) if nli.get('nli_score') is not None else None,
                        'contribution': l['contribution']})
    return preuves


def proposer(unit_id, aujourdhui=None, run_id=None):
    """Genere (et enregistre) la proposition d'une unite ; renvoie la ligne ``proposals`` decodee.

    ``None`` si l'unite est hors perimetre. Une proposition en attente deja existante pour la meme
    unite et le meme type est renvoyee telle quelle. Substitution deterministe d'abord, reecriture
    locale ensuite, controle (NLI + budget), seconde tentative, puis ``a_rediger``.
    """
    assurer_schema()
    S = _s()
    jour = _jour(aujourdhui)
    candidats = perimetre(jour, unit_ids=[unit_id])
    if not candidats:
        return None
    candidat = candidats[0]
    existante = S.query("SELECT * FROM proposals WHERE unit_id=? AND kind=? AND COALESCE(ref_id,'')=COALESCE(?,'') LIMIT 1",
                        (unit_id, candidat['kind'], candidat['ref_id']), True)
    if existante:
        return _decoder(existante)
    if run_id is None:
        with noyau.etape('m6.proposer', moteurs={}, prompt=PROMPT_VERSION, seuils=['m6.diff_ratio_max', 'm6.tentatives_max'], pages=1, items=1) as run:
            return proposer(unit_id, jour, run.id)
    contexte = _contexte(candidat, jour)
    if candidat['kind'] == 'source_ajoutee':
        resultat = _proposer_source(contexte)
    elif candidat['kind'] == 'lien_interne':
        resultat = _proposer_lien(contexte)
    else:
        resultat = _proposer_texte(contexte)
    if resultat is None:
        return None
    resultat.setdefault('sources', _preuves_stockees(unit_id))
    identifiant = S.uid()
    checks = resultat.get('checks') or {}
    checks.setdefault('motif_origine', candidat['motif_origine'])
    tentatives = len(checks.get('tentatives') or []) or 1
    S.query('INSERT INTO proposals(id,unit_id,version_id,kind,"before","after",paragraph,facts_changed,facts_kept,sources,method,checks,'
            'diff_ratio,criticality,status,run_id,created,attempts,ref_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (identifiant, unit_id, candidat['version_id'], candidat['kind'], contexte['before'], resultat.get('after'),
             contexte['paragraphe'], _json(resultat.get('facts_changed') or []), _json(resultat.get('facts_kept') or []),
             _json(resultat['sources']), resultat.get('method'), _json(checks), resultat.get('diff_ratio'), candidat['criticite'],
             resultat['status'], run_id, S.now(), tentatives, candidat['ref_id']))
    return _decoder(S.query('SELECT * FROM proposals WHERE id=?', (identifiant,), True))


def _decoder(ligne):
    """Ligne ``proposals`` avec les colonnes JSON decodees."""
    if not ligne:
        return None
    resultat = dict(ligne)
    for cle in ('facts_changed', 'facts_kept', 'sources'):
        resultat[cle] = _liste_json(ligne.get(cle))
    try:
        resultat['checks'] = json.loads(ligne.get('checks') or '{}')
    except ValueError:
        resultat['checks'] = {}
    return resultat


def executer(forcer=False, aujourdhui=None):
    """Propose les corrections de tout le perimetre. Reprenable ; renvoie les compteurs.

    Une unite qui a deja une proposition (meme type) est sautee ; ``forcer=True`` regenere celles
    qui sont encore ``proposee`` ou ``a_rediger`` (les propositions decidees ne sont jamais touchees).
    """
    assurer_schema()
    S = _s()
    jour = _jour(aujourdhui)
    candidats = perimetre(jour)
    compteurs = {'candidats': len(candidats), 'proposees': 0, 'a_rediger': 0, 'deja': 0, 'hors_perimetre': 0,
                 'substitutions': 0, 'reecritures': 0, 'par_type': {}}
    pages = len({c['url'] for c in candidats})
    with noyau.etape('m6.proposer', moteurs={}, prompt=PROMPT_VERSION,
                     seuils=['m6.diff_ratio_max', 'm6.tentatives_max', 'm6.nli.faits_nouveaux', 'm6.nli.faits_conserves'],
                     pages=pages, items=len(candidats)) as run:
        for index, candidat in enumerate(candidats):
            S.checkpoint()
            S.JOB.update(stage='m6.proposer', done=index, total=len(candidats), message=f'Propositions M6 · {index + 1}/{len(candidats)}')
            existante = S.query("SELECT id, status FROM proposals WHERE unit_id=? AND kind=? AND COALESCE(ref_id,'')=COALESCE(?,'')",
                                (candidat['unit_id'], candidat['kind'], candidat['ref_id']), True)
            if existante:
                if forcer and existante['status'] in ('proposee', 'a_rediger'):
                    S.query('DELETE FROM proposals WHERE id=?', (existante['id'],))
                else:
                    compteurs['deja'] += 1
                    continue
            proposition = proposer(candidat['unit_id'], jour, run.id)
            if proposition is None:
                compteurs['hors_perimetre'] += 1
                continue
            compteurs['proposees' if proposition['status'] == 'proposee' else 'a_rediger'] += 1
            if proposition['method'] == 'substitution':
                compteurs['substitutions'] += 1
            elif proposition['method'] == 'reecriture_llm':
                compteurs['reecritures'] += 1
            compteurs['par_type'][proposition['kind']] = compteurs['par_type'].get(proposition['kind'], 0) + 1
        run.moteur('nli', moteurs.dernier('nli'))
        run.moteur('llm', noyau.moteur_llm())
        S.JOB.update(stage='m6.proposer', done=len(candidats), total=len(candidats), message='Propositions M6 terminées')
    return compteurs


# ---------------------------------------------------------------------------
# Cartes de revue
# ---------------------------------------------------------------------------

def _texte_version(version_id, cache):
    if version_id not in cache:
        ligne = _s().query('SELECT v.text, v.page_id, p.url, p.title FROM versions v JOIN pages p ON p.id=v.page_id WHERE v.id=?', (version_id,), True)
        cache[version_id] = ligne or {'text': '', 'page_id': None, 'url': None, 'title': None}
    return cache[version_id]


def _contexte_carte(prop, unite, version):
    """Paragraphe entier et offsets de la phrase reperee (relatifs au paragraphe et absolus)."""
    texte = version['text'] or ''
    avant = prop.get('before') or ''
    position = None
    if unite and unite['version_id'] == prop['version_id']:
        position = _phrase_dans_texte(texte, unite) if (unite.get('quote') == avant or not avant) else None
    if position is None and avant and avant in texte:
        position = (texte.find(avant), texte.find(avant) + len(avant))
    if position is None:
        return {'paragraphe': prop.get('paragraph') or '', 'phrase_debut': None, 'phrase_fin': None, 'debut_absolu': None, 'fin_absolue': None}
    a, b = bornes_paragraphe(texte, position[0], position[1])
    return {'paragraphe': texte[a:b], 'phrase_debut': position[0] - a, 'phrase_fin': position[1] - a,
            'debut_absolu': position[0], 'fin_absolue': position[1]}


def _preuves_carte(prop, groupes):
    S = _s()
    liste = {}
    for p in _liste_json(prop.get('sources')):
        liste[p.get('url')] = dict(p, origine='verification')
    if groupes and not liste:
        marques = ','.join('?' for _ in groupes)
        for e in S.query(f'SELECT url, domain, page_date, text, passage_start, nli, nli_score, kept FROM evidence WHERE (group_id IN ({marques}) '
                         f'OR claim_id IN ({marques})) AND COALESCE(kept,1)=1 ORDER BY COALESCE(weight,0) DESC LIMIT 20', tuple(groupes) * 2):
            liste.setdefault(e['url'], {'url': e['url'], 'domaine': e['domain'] or _domaine(e['url']), 'date': e['page_date'],
                                        'passage': e['text'], 'verdict_nli': e['nli'], 'score_nli': e['nli_score'], 'origine': 'preuve'})
    return list(liste.values())


def _paternite_carte(prop, groupes):
    S = _s()
    resultat = {'groupe': None, 'parent_unit_id': None, 'page_parente': None, 'critere': None, 'detail': None, 'humain': False,
                'est_parente': False, 'autres_pages': [], 'enfants': []}
    if not groupes:
        return resultat
    pat = None
    for g in groupes:
        pat = S.query('SELECT * FROM paternity WHERE group_id=?', (g,), True)
        if pat:
            break
    groupe = pat['group_id'] if pat else groupes[0]
    resultat.update(groupe=groupe)
    if pat:
        parent = S.query('SELECT u.id, u.version_id, p.url, p.title FROM units u JOIN versions v ON v.id=u.version_id JOIN pages p ON p.id=v.page_id '
                         'WHERE u.id=?', (pat['parent_unit_id'],), True)
        resultat.update(parent_unit_id=pat['parent_unit_id'], critere=pat['criterion'], detail=pat['detail'], humain=bool(pat['human']),
                        est_parente=pat['parent_unit_id'] == prop['unit_id'],
                        page_parente={'url': parent['url'], 'titre': parent['title'], 'version_id': parent['version_id']} if parent else None)
    membres = S.query('SELECT u.id unit_id, u.version_id, u.verdict, p.url, p.title, (SELECT id FROM proposals pr WHERE pr.unit_id=u.id '
                      "AND pr.kind IN ('corrigee','perimee_par_date','completee','mention_maj') ORDER BY pr.created DESC LIMIT 1) proposal_id, "
                      '(SELECT status FROM proposals pr WHERE pr.unit_id=u.id ORDER BY pr.created DESC LIMIT 1) proposal_status '
                      'FROM unit_group_members m JOIN units u ON u.id=m.unit_id JOIN versions v ON v.id=u.version_id JOIN pages p ON p.id=v.page_id '
                      'WHERE m.group_id=? AND u.id<>? LIMIT 30', (groupe, prop['unit_id']))
    for m in membres:
        element = {'unit_id': m['unit_id'], 'url': m['url'], 'titre': m['title'], 'proposal_id': m['proposal_id'],
                   'statut': m['proposal_status'], 'touchee': bool(m['proposal_id']) or m['verdict'] == 'corrigee'}
        if m['version_id'] != prop['version_id']:
            resultat['autres_pages'].append(element)
        if pat and pat['parent_unit_id'] == prop['unit_id']:
            resultat['enfants'].append(element)
    return resultat


def carte(proposal_id):
    """Donnees completes de la carte de revue d'une proposition (contexte, avant/apres, motif, preuves, paternite)."""
    assurer_schema()
    S = _s()
    ligne = S.query('SELECT * FROM proposals WHERE id=?', (proposal_id,), True)
    if not ligne:
        raise ValueError('Proposition inconnue.')
    return _carte(_decoder(ligne), {})


def _carte(prop, cache):
    S = _s()
    unite = S.query('SELECT * FROM units WHERE id=?', (prop['unit_id'],), True) if prop.get('unit_id') else None
    version = _texte_version(prop['version_id'], cache)
    groupes = [g['group_id'] for g in S.query('SELECT group_id FROM unit_group_members WHERE unit_id=?', (prop['unit_id'],))] if unite else []
    apres = prop.get('after')
    checks = prop.get('checks') or {}
    origine = checks.get('motif_origine') or prop['kind']
    ancrage = {'date': unite.get('anchor_date') if unite else None, 'type': unite.get('anchor_kind') if unite else None}
    ancrage['date_fr'] = date_fr(ancrage['date']) if ancrage['date'] else ''
    return {
        'id': prop['id'], 'unit_id': prop['unit_id'], 'statut': prop['status'], 'decision': prop.get('decision'),
        'page': {'version_id': prop['version_id'], 'page_id': version['page_id'], 'url': version['url'], 'titre': version['title']},
        'contexte': _contexte_carte(prop, unite, version),
        'avant_apres': {'avant': prop.get('before'), 'apres': apres, 'diff': diff_mots(prop.get('before') or '', apres or '') if apres else [],
                        'diff_ratio': prop.get('diff_ratio'), 'budget': seuils.get('m6.diff_ratio_max'), 'methode': prop.get('method')},
        'motif': {'code': prop['kind'], 'libelle': LIBELLES.get(prop['kind'], prop['kind']), 'origine': origine,
                  'libelle_origine': LIBELLES.get(origine, origine), 'ancrage': ancrage},
        'faits': {'modifies': prop.get('facts_changed'), 'conserves': prop.get('facts_kept')},
        'preuves': _preuves_carte(prop, groupes),
        'paternite': _paternite_carte(prop, groupes),
        'controle': {'ok': checks.get('ok'), 'problemes': checks.get('problemes') or [], 'tentatives': checks.get('tentatives') or []},
        'criticite': prop.get('criticality'),
        'decision_detail': {'statut': prop['status'], 'note': prop.get('decision_note'), 'date': prop.get('decided_at'),
                            'texte_final': prop.get('final_text'), 'version_appliquee': prop.get('applied_version')},
    }


def cartes(statut=None, offset=0, limite=50, motif=None, page=None, unite=None):
    """Cartes triees par criticite decroissante puis par page (URL, position). ``{total, offset, limite, cartes}``.

    ``statut`` : None (tous), un statut, une liste, ou ``'en_attente'`` (proposee, a_rediger, reportee).
    ``motif`` (``kind``), ``page`` (version_id ou page_id), ``unite`` (unit_id) : filtres facultatifs.
    """
    assurer_schema()
    S = _s()
    if statut in (None, ''):
        statuts = []
    elif statut == 'en_attente':
        statuts = list(EN_ATTENTE)
    elif isinstance(statut, str):
        statuts = [statut]
    else:
        statuts = list(statut)
    conditions = ['pr.status IN (' + ','.join('?' for _ in statuts) + ')'] if statuts else []
    arguments = list(statuts)
    if motif:
        conditions.append('pr.kind=?')
        arguments.append(motif)
    if page:
        conditions.append('(pr.version_id=? OR v.page_id=?)')
        arguments.extend([page, page])
    if unite:
        conditions.append('pr.unit_id=?')
        arguments.append(unite)
    clause = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
    base = ('FROM proposals pr LEFT JOIN versions v ON v.id=pr.version_id LEFT JOIN pages p ON p.id=v.page_id '
            'LEFT JOIN units u ON u.id=pr.unit_id' + clause)
    total = S.query('SELECT count(*) n ' + base, tuple(arguments), True)['n']
    lignes = S.query('SELECT pr.* ' + base + ' ORDER BY COALESCE(pr.criticality,0) DESC, p.url, COALESCE(u.span_start,0), pr.id LIMIT ? OFFSET ?',
                     tuple(arguments) + (int(limite), int(offset)))
    cache = {}
    return {'total': total, 'offset': int(offset), 'limite': int(limite), 'cartes': [_carte(_decoder(l), cache) for l in lignes]}


def bilan():
    """Compteurs de propositions par statut et par type (pour l'ecran)."""
    S = _s()
    assurer_schema()
    return {'statuts': {l['status']: l['n'] for l in S.query('SELECT status, count(*) n FROM proposals GROUP BY status')},
            'types': {l['kind']: l['n'] for l in S.query('SELECT kind, count(*) n FROM proposals GROUP BY kind')}}


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def _remplacement(prop, texte, texte_final):
    """(ancien, nouveau) a passer a ``apply_patch_version`` : ``ancien`` doit etre unique dans ``texte``."""
    avant = prop['before'] or ''
    occurrences = [m.start() for m in re.finditer(re.escape(avant), texte)] if avant else []
    if not occurrences:
        raise ValueError('Phrase introuvable dans la version courante : correction manuelle nécessaire.')
    unite = _s().query('SELECT span_start FROM units WHERE id=?', (prop['unit_id'],), True) if prop.get('unit_id') else None
    debut = occurrences[0]
    if len(occurrences) > 1:
        proches = [o for o in occurrences if unite and unite['span_start'] == o]
        if not proches:
            raise ValueError('Phrase ambiguë dans la page : correction manuelle nécessaire.')
        debut = proches[0]
    fin = debut + len(avant)
    if prop['kind'] == 'mention_maj':
        a, b = bornes_paragraphe(texte, debut, fin)
        paragraphe = texte[a:b]
        if texte_final in texte:
            raise ValueError('Mention de mise à jour déjà présente.')
        separateur = '\r\n\r\n' if '\r\n' in texte else '\n\n'
        ancien, nouveau, origine = paragraphe, paragraphe + separateur + texte_final, a
    else:
        ancien, nouveau, origine = avant, texte_final, debut
    if texte.count(ancien) == 1:
        return ancien, nouveau
    debut_ancien = origine
    for marge in range(10, 400, 10):
        a = max(0, debut_ancien - marge)
        b = min(len(texte), debut_ancien + len(ancien) + marge)
        etendu = texte[a:b]
        if texte.count(etendu) == 1:
            return etendu, texte[a:debut_ancien] + nouveau + texte[debut_ancien + len(ancien):b]
    raise ValueError('Passage ambigu ou introuvable : correction manuelle nécessaire.')


def _appliquer(prop, texte_final):
    """Cree la nouvelle version locale de la page ; renvoie son identifiant."""
    S = _s()
    version = S.query('SELECT v.*, p.url, p.title FROM versions v JOIN pages p ON p.id=v.page_id WHERE v.id=?', (prop['version_id'],), True)
    derniere = S.query('SELECT id FROM versions WHERE page_id=? ORDER BY created DESC, rowid DESC LIMIT 1', (version['page_id'],), True)
    if derniere['id'] != version['id']:
        version = S.query('SELECT v.*, p.url, p.title FROM versions v JOIN pages p ON p.id=v.page_id WHERE v.id=?', (derniere['id'],), True)
    ancien, nouveau = _remplacement(prop, version['text'], texte_final)
    unite = S.query('SELECT legacy_claim_id FROM units WHERE id=?', (prop['unit_id'],), True) if prop.get('unit_id') else None
    patch_id = prop.get('legacy_patch_id')
    existant = S.query('SELECT status FROM patches WHERE id=?', (patch_id,), True) if patch_id else None
    if existant and existant['status'] == 'proposed':
        S.query('UPDATE patches SET version_id=?, old=?, new=? WHERE id=?', (version['id'], ancien, nouveau, patch_id))
    else:
        patch_id = S.uid()
        S.query('INSERT INTO patches VALUES(?,?,?,?,?,?,?)', (patch_id, (unite or {}).get('legacy_claim_id') or prop.get('unit_id') or '',
                                                           version['id'], ancien, nouveau, 'proposed', S.now()))
    nouvelle = S.apply_patch_version(patch_id)
    colonnes = noyau.colonnes('versions')
    heritees = [c for c in ('author', 'updated', 'page_type', 'page_type_engine', 'meta', 'lede') if c in colonnes]
    if heritees:
        S.query('UPDATE versions SET ' + ','.join(f'{c}=(SELECT {c} FROM versions WHERE id=?)' for c in heritees) + ' WHERE id=?',
                tuple(version['id'] for _ in heritees) + (nouvelle,))
    S.query('UPDATE proposals SET legacy_patch_id=? WHERE id=?', (patch_id, prop['id']))
    _rebaser(version['id'], nouvelle, prop['id'])
    return nouvelle


def _rebaser(ancienne, nouvelle, sauf):
    """Reporte les propositions en attente de la page sur la nouvelle version (phrase retrouvee de facon unique)."""
    S = _s()
    texte = S.query('SELECT text FROM versions WHERE id=?', (nouvelle,), True)['text']
    for p in S.query("SELECT id, \"before\" avant, decision_note FROM proposals WHERE version_id=? AND id<>? AND status IN ('proposee','a_rediger','reportee')",
                     (ancienne, sauf)):
        if p['avant'] and texte.count(p['avant']) == 1:
            S.query('UPDATE proposals SET version_id=? WHERE id=?', (nouvelle, p['id']))
        else:
            S.query("UPDATE proposals SET status='reportee', decision_note=? WHERE id=?",
                    ((p['decision_note'] or '') + ' conflit : phrase modifiée par une autre décision', p['id']))


def _enfants(prop):
    """Propositions en attente des unites enfants (meme groupe, parent = l'unite de ``prop``), meme type."""
    S = _s()
    if not prop.get('unit_id'):
        return []
    return S.query("SELECT pr.* FROM paternity pa JOIN unit_group_members m ON m.group_id=pa.group_id JOIN proposals pr ON pr.unit_id=m.unit_id "
                   "WHERE pa.parent_unit_id=? AND m.unit_id<>? AND pr.kind=? AND pr.status IN ('proposee','a_rediger','reportee') AND pr.id<>?",
                   (prop['unit_id'], prop['unit_id'], prop['kind'], prop['id']))


def _decider_une(prop, decision, texte, note, forcer, aujourdhui):
    S = _s()
    if prop['status'] not in EN_ATTENTE:
        raise ValueError(f"Proposition déjà traitée (statut {prop['status']}).")
    maintenant = S.now()
    resultat = {'id': prop['id'], 'decision': decision, 'status': prop['status'], 'version_id': None, 'ok': True, 'controle': None}
    if decision in ('accepter', 'modifier'):
        if decision == 'modifier':
            if not (texte or '').strip():
                raise ValueError('Texte requis pour modifier.')
            if texte.strip() == (prop.get('after') or '').strip():
                decision = 'accepter'
                resultat['decision'] = 'accepter'
        if decision == 'accepter':
            if prop['status'] == 'a_rediger' or not (prop.get('after') or '').strip():
                raise ValueError('Rien à accepter : rédigez le texte (décision « modifier »).')
            final, methode = prop['after'], prop.get('method')
        else:
            final, methode = texte.strip(), 'humain'
            controle = None
            try:
                controle = controler({'kind': prop['kind'], 'before': prop['before'], 'after': final, 'paragraph': prop.get('paragraph'),
                                      'facts_changed': prop.get('facts_changed'), 'facts_kept': prop.get('facts_kept')})
            except S.OperationalError as erreur:
                if getattr(S, 'STOP', None) is not None and S.STOP.is_set():
                    raise
                controle = {'ok': None, 'problemes': [{'code': 'nli_indisponible', 'detail': str(erreur)}]}
            resultat['controle'] = controle
            if controle['ok'] is False and not forcer:
                resultat.update(ok=False, raison='controle_echoue')
                return resultat
        try:
            nouvelle = _appliquer(dict(prop, final_text=final), final)
            statut, mention = ('acceptee' if decision == 'accepter' else 'modifiee'), note
            statut_final = 'appliquee' if decision == 'accepter' else 'modifiee'
        except ValueError as erreur:
            nouvelle, statut_final, mention = None, ('acceptee' if decision == 'accepter' else 'modifiee'), (note + ' ' if note else '') + 'application impossible : ' + str(erreur)
            resultat['ok'] = False
            resultat['raison'] = 'application_impossible'
        champs = {'status': statut_final, 'decision': decision, 'decision_note': mention, 'decided_at': maintenant, 'final_text': final,
                  'applied_version': nouvelle, 'method': methode}
        resultat.update(status=statut_final, version_id=nouvelle)
        if prop['kind'] == 'lien_interne' and prop.get('ref_id'):
            S.query('UPDATE link_recommendations SET status=? WHERE id=?', ('appliquee' if nouvelle else 'acceptee', prop['ref_id']))
    elif decision == 'rejeter':
        champs = {'status': 'rejetee', 'decision': 'rejeter', 'decision_note': note, 'decided_at': maintenant}
        if prop['kind'] == 'lien_interne' and prop.get('ref_id'):
            S.query('UPDATE link_recommendations SET status=? WHERE id=?', ('rejetee', prop['ref_id']))
    elif decision == 'archive':
        ancre = S.query('SELECT anchor_date FROM units WHERE id=?', (prop['unit_id'],), True) if prop.get('unit_id') else None
        a_date = date_fr((ancre or {}).get('anchor_date')) if ancre and ancre.get('anchor_date') else date_fr(_jour(aujourdhui))
        champs = {'status': 'archive', 'decision': 'archive', 'decision_note': note or f'à date du {a_date}', 'decided_at': maintenant}
        if prop.get('unit_id'):
            S.query("UPDATE units SET status='historique', status_run=? WHERE id=?", ('decision:' + prop['id'], prop['unit_id']))
    else:
        champs = {'status': 'reportee', 'decision': 'reporter', 'decision_note': note, 'decided_at': maintenant}
    resultat['status'] = champs['status']
    S.query('UPDATE proposals SET ' + ','.join(f'"{c}"=?' for c in champs) + ' WHERE id=?', tuple(champs.values()) + (prop['id'],))
    return resultat


def decider(proposal_id, decision, texte=None, note='', propager=False, forcer=False, aujourdhui=None):
    """Enregistre la decision de revue : accepter, modifier (texte recontrole), rejeter, archive, reporter.

    L'acceptation cree une nouvelle version locale de la page (``apply_patch_version``). ``modifier``
    recontrole le texte : en cas d'echec du controle rien n'est applique (``ok`` False, ``controle``),
    sauf ``forcer=True``. ``propager=True`` applique la meme decision aux enfants de l'unite parente
    (``modifier`` : seulement ceux dont la phrase est identique). Renvoie ``{id, status, version_id,
    controle, propagees, ignorees}``.
    """
    assurer_schema()
    S = _s()
    nom = DECISIONS.get(_sans_accents(str(decision)).lower())
    if nom is None:
        raise ValueError('Décision inconnue : ' + str(decision))
    ligne = S.query('SELECT * FROM proposals WHERE id=?', (proposal_id,), True)
    if not ligne:
        raise ValueError('Proposition inconnue.')
    parent = _decoder(ligne)
    resultat = _decider_une(parent, nom, texte, note, forcer, aujourdhui)
    resultat['propagees'], resultat['ignorees'] = [], []
    if propager and resultat.get('ok'):
        for enfant in _enfants(parent):
            suite = _decoder(enfant)
            if nom == 'modifier' and suite['before'] != parent['before']:
                resultat['ignorees'].append({'id': suite['id'], 'raison': 'phrase_differente'})
                continue
            try:
                sortie = _decider_une(suite, nom, texte, (note + ' ' if note else '') + f'[propagée depuis {proposal_id}]', forcer, aujourdhui)
                resultat['propagees'].append({'id': suite['id'], 'status': sortie['status'], 'version_id': sortie.get('version_id')})
            except ValueError as erreur:
                resultat['ignorees'].append({'id': suite['id'], 'raison': str(erreur)})
    return resultat


# ---------------------------------------------------------------------------
# Apprentissage
# ---------------------------------------------------------------------------

def _stats(valeurs):
    if not valeurs:
        return {'n': 0, 'moyenne': None, 'mediane': None}
    return {'n': len(valeurs), 'moyenne': round(statistics.mean(valeurs), 4), 'mediane': round(statistics.median(valeurs), 4)}


def apprentissage():
    """Statistiques de recalibrage des seuils a partir des decisions de revue.

    Decisions comptees : accepter, modifier, rejeter, archive (``reporter`` exclu). Taux d'acceptation sans
    modification = acceptees telles quelles / decisions comptees, au global, par motif et par methode ;
    ratio de difference des acceptees, modifiees et rejetees ; score NLI minimal des controles ;
    ``budget_suggere`` = 90e percentile du ratio des acceptees.
    """
    assurer_schema()
    lignes = _s().query("SELECT kind, method, status, decision, diff_ratio, criticality, checks, final_text, \"after\" apres FROM proposals "
                        "WHERE status IN ('acceptee','appliquee','modifiee','rejetee','archive')")

    def categorie(l):
        if l['status'] == 'modifiee' or l['decision'] == 'modifier':
            return 'modifiees'
        if l['status'] in ('acceptee', 'appliquee'):
            return 'acceptees' if not l['final_text'] or l['final_text'] == l['apres'] else 'modifiees'
        return 'rejetees' if l['status'] == 'rejetee' else 'archivees'

    def agreger(sous):
        total = len(sous)
        compte = {c: sum(1 for l in sous if categorie(l) == c) for c in ('acceptees', 'modifiees', 'rejetees', 'archivees')}
        return dict(compte, total=total, taux_sans_modification=round(compte['acceptees'] / total, 4) if total else None)

    resultat = {'total_decisions': len(lignes), 'seuil': seuils.get('evaluation.m6.acceptees_sans_modif')}
    resultat.update(agreger(lignes))
    resultat['par_motif'] = {k: agreger([l for l in lignes if l['kind'] == k]) for k in sorted({l['kind'] for l in lignes})}
    resultat['par_methode'] = {k or 'inconnue': agreger([l for l in lignes if l['method'] == k]) for k in sorted({l['method'] for l in lignes}, key=str)}
    resultat['diff_ratio'] = {c: _stats([l['diff_ratio'] for l in lignes if categorie(l) == c and l['diff_ratio'] is not None])
                              for c in ('acceptees', 'modifiees', 'rejetees')}
    nli = {'acceptees': [], 'rejetees': []}
    for l in lignes:
        try:
            minimum = json.loads(l['checks'] or '{}').get('nli_min')
        except ValueError:
            minimum = None
        if minimum is not None and categorie(l) in ('acceptees', 'rejetees'):
            nli[categorie(l)].append(minimum)
    resultat['nli_min'] = {c: _stats(v) for c, v in nli.items()}
    resultat['criticite'] = {c: _stats([l['criticality'] for l in lignes if categorie(l) == c and l['criticality'] is not None])
                             for c in ('acceptees', 'rejetees')}
    acceptes = sorted(l['diff_ratio'] for l in lignes if categorie(l) == 'acceptees' and l['diff_ratio'] is not None)
    resultat['budget_suggere'] = round(acceptes[min(len(acceptes) - 1, int(0.9 * len(acceptes)))], 4) if acceptes else None
    return resultat
