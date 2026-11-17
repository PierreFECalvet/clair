"""Moteurs locaux de la chaine et leurs replis (contrat section 3).

Hierarchie du document : regles -> modeles discriminatifs locaux -> LLM local
contraint -> LLM tiers. Chaque fonction essaie ses moteurs dans l'ordre et
renvoie (ou memorise, voir ``dernier``) l'identifiant du moteur utilise.

Reglages (table ``settings``) :
- ``moteur:<nom>`` = ``auto`` | ``local`` | ``llm`` | ``regles`` : niveau le plus
  haut autorise. ``auto`` et ``local`` commencent aux modeles locaux, ``llm``
  saute les modeles locaux, ``regles`` ne garde que les regles. On descend
  toujours vers les niveaux inferieurs si le niveau demande est indisponible ;
  la raison est donnee par ``etat()``. ``nli`` n'a pas de niveau ``regles``.
- ``modele:<nom>`` : nom du modele (voir ``MODELES``).

Aucune bibliotheque tierce n'est importee au chargement de ce module : les
imports sont faits dans les fonctions, sous ``try/except``, et les modeles
sont charges une fois (cache module).
"""
import gc
import hashlib
import importlib
import importlib.metadata
import importlib.util
import math
import re
import sys
import threading
import unicodedata

from chaine import noyau
from chaine import seuils

MODES = ('auto', 'local', 'llm', 'regles')
RANG_MODE = {'auto': 0, 'local': 0, 'llm': 1, 'regles': 2}
RANG_NIVEAU = {'local': 0, 'llm': 1, 'regles': 2}

# Modeles par defaut (surcharge : settings ``modele:<cle>``).
MODELES = {
    'spacy': 'fr_core_news_lg',
    'sat': 'sat-3l-sm',
    'entites': 'urchade/gliner_multi-v2.1',
    'nli': 'MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7',
    'embeddings': 'Qwen/Qwen3-Embedding-0.6B',
    'reordonner': 'BAAI/bge-reranker-v2-m3',
    'perplexite': 'asi/gpt-fr-cased-small',
    'classer': 'sentence-transformers/paraphrase-multilingual-mpnet-base-v2',
}
REPLIS_SPACY = ('fr_core_news_lg', 'fr_core_news_md', 'fr_core_news_sm')

# Candidats par fonction : (niveau, moteur, paquet python requis ou None, cle de modele ou None)
NIVEAUX = {
    'phrases': [('local', 'spacy', 'spacy', 'spacy'), ('local', 'sat', 'wtpsplit', 'sat'), ('regles', 'regles', None, None)],
    'analyse': [('local', 'spacy', 'spacy', 'spacy')],
    'entites': [('local', 'gliner', 'gliner', 'entites'), ('local', 'spacy', 'spacy', 'spacy'), ('regles', 'regles', None, None)],
    'nli': [('local', 'transformers', 'transformers', 'nli'), ('llm', 'llm', None, None)],
    'embeddings': [('local', 'sentence_transformers', 'sentence_transformers', 'embeddings'), ('llm', 'api', None, None)],
    'reordonner': [('local', 'cross_encoder', 'sentence_transformers', 'reordonner'), ('llm', 'cosinus', None, None),
                   ('regles', 'lexical', None, None)],
    'perplexite': [('local', 'transformers', 'transformers', 'perplexite')],
    # Regles avant LLM (hierarchie des moyens) : le centroide lexical passe avant le LLM, qui ne sert que sans exemples.
    'classer': [('local', 'setfit', 'setfit', 'classer'), ('local', 'centroide', None, None), ('regles', 'lexical', None, None),
                ('llm', 'llm', None, None)],
}

LABELS_NLI = ('implication', 'contradiction', 'neutre')
TYPES_ENTITES = ('personne', 'organisation', 'lieu', 'date', 'montant', 'pourcentage', 'infrastructure',
                 'collectivite', 'evenement', 'produit', 'loi')
TYPES_SPACY = {'PER': 'personne', 'PERSON': 'personne', 'ORG': 'organisation', 'LOC': 'lieu', 'GPE': 'lieu', 'MISC': 'divers'}
TYPES_REGLES = ('date', 'montant', 'pourcentage', 'personne', 'organisation', 'nom_propre')

_VERROU = threading.RLock()
_CHARGES = {}
_ECHECS = {}
_DERNIER = threading.local()


class Indisponible(Exception):
    """Un moteur ne peut pas etre utilise (paquet ou modele absent, echec de chargement)."""


# ---------------------------------------------------------------------------
# Selection des moteurs
# ---------------------------------------------------------------------------

def mode(nom):
    """Mode reglé pour la fonction ``nom`` (``auto`` par defaut)."""
    valeur = noyau.reglage('moteur:' + nom, 'auto')
    return valeur if valeur in MODES else 'auto'


def modele(cle):
    """Nom du modele configure pour ``cle``."""
    return noyau.reglage('modele:' + cle, MODELES.get(cle))


def candidats(nom):
    """Candidats autorises par le reglage, dans l'ordre d'essai."""
    depart = RANG_MODE[mode(nom)]
    liste = [c for c in NIVEAUX[nom] if RANG_NIVEAU[c[0]] >= depart]
    if not liste:
        liste = list(NIVEAUX[nom])
    return liste


def dernier(nom):
    """Identifiant du dernier moteur utilise par ``nom`` dans ce thread."""
    return getattr(_DERNIER, nom, None)


def reinitialiser():
    """Oublie les modeles charges et les echecs memorises (tests, changement de reglage).

    Si des modeles etaient en memoire, un ramasse-miettes explicite les libere
    tout de suite (les modeles spaCy ou transformers forment des cycles de
    references : sans cela, plusieurs copies peuvent coexister apres un
    changement de reglage).
    """
    with _VERROU:
        avait_modeles = bool(_CHARGES)
        _CHARGES.clear()
        _ECHECS.clear()
    if avait_modeles:
        gc.collect()


def _noter(nom, identifiant):
    """Memorise le moteur utilise et le renvoie."""
    setattr(_DERNIER, nom, identifiant)
    return identifiant


def paquet_present(paquet):
    """Vrai si le paquet est importable, sans l'importer."""
    if paquet in sys.modules:
        return sys.modules[paquet] is not None
    try:
        return importlib.util.find_spec(paquet) is not None
    except (ImportError, ValueError):
        return False


def _version(paquet):
    """Version installee d'un paquet, ou ''."""
    module = sys.modules.get(paquet)
    if module is not None and getattr(module, '__version__', None):
        return str(module.__version__)
    noms = {'sentence_transformers': 'sentence-transformers'}
    try:
        return importlib.metadata.version(noms.get(paquet, paquet))
    except importlib.metadata.PackageNotFoundError:
        return ''


def _importer(paquet):
    """Import paresseux ; leve Indisponible si le paquet manque."""
    try:
        return importlib.import_module(paquet)
    except ImportError as erreur:
        raise Indisponible('paquet ' + paquet + ' non installe (' + str(erreur)[:120] + ')') from erreur


def _charger(cle, fabrique):
    """Charge une fois un objet lourd ; memorise aussi les echecs."""
    with _VERROU:
        if cle in _CHARGES:
            return _CHARGES[cle]
        if cle in _ECHECS:
            raise Indisponible(_ECHECS[cle])
        try:
            objet = fabrique()
        except Indisponible as erreur:
            _ECHECS[cle] = str(erreur)
            raise
        except Exception as erreur:
            raison = 'echec de chargement de ' + cle[1] + ' : ' + type(erreur).__name__ + ' ' + str(erreur)[:200]
            _ECHECS[cle] = raison
            raise Indisponible(raison) from erreur
        _CHARGES[cle] = objet
        return objet


def _modele_hf_present(depot):
    """True/False si le modele est (ou non) dans le cache Hugging Face ; None si inconnu."""
    if not paquet_present('huggingface_hub'):
        return None
    try:
        hub = importlib.import_module('huggingface_hub')
        chemin = hub.try_to_load_from_cache(depot, 'config.json')
    except Exception:
        return None
    return isinstance(chemin, str)


def _llm_disponible():
    """Vrai et raison si le LLM configure est utilisable comme « LLM local » de la chaine."""
    if not noyau.est_branche():
        return False, 'serveur non branche'
    config = noyau.srv().CONFIG
    if not config.get('model'):
        return False, 'aucun modele LLM configure'
    if config.get('provider') == 'openai' and not config.get('openai_key'):
        return False, 'cle OpenAI manquante'
    if config.get('provider') == 'openrouter' and not config.get('openrouter_key'):
        return False, 'cle OpenRouter manquante'
    refus = noyau.raison_llm_local()
    if refus:
        return False, refus
    return True, ''


def _api_embeddings_disponible():
    """Vrai et raison si ``S.embed`` est utilisable."""
    if not noyau.est_branche():
        return False, 'serveur non branche'
    config = noyau.srv().CONFIG
    if not config.get('embeddings'):
        return False, 'embeddings desactives dans les connexions'
    if not config.get('embedding_model'):
        return False, "aucun modele d'embeddings configure"
    if config.get('provider') == 'openrouter':
        return False, "OpenRouter ne sert pas d'embeddings ici : installez le moteur local"
    if config.get('provider') == 'openai' and not config.get('openai_key'):
        return False, 'cle OpenAI manquante'
    return True, ''


def _verifier(nom, candidat):
    """Disponibilite d'un candidat sans rien charger : (ok, raison)."""
    niveau, moteur, paquet, cle_modele = candidat
    cle = (nom, moteur)
    if cle in _ECHECS:
        return False, _ECHECS[cle]
    for (_, moteur_charge), raison in _ECHECS.items():
        if moteur_charge == moteur and moteur in ('spacy',):
            return False, raison
    if paquet and not paquet_present(paquet):
        return False, 'paquet ' + paquet + ' non installe'
    if moteur == 'spacy':
        if not _modeles_spacy_installes():
            return False, 'aucun modele spaCy francais installe (fr_core_news_lg/md/sm)'
        return True, ''
    if moteur == 'sat':
        return True, ''
    if paquet and cle_modele:
        present = _modele_hf_present(modele(cle_modele))
        if present is False:
            return False, 'modele ' + modele(cle_modele) + ' absent du cache Hugging Face'
        return True, ''
    if moteur == 'llm':
        return _llm_disponible()
    if moteur == 'api':
        return _api_embeddings_disponible()
    if moteur in ('cosinus', 'centroide'):
        ok_local, _ = _verifier('embeddings', NIVEAUX['embeddings'][0])
        ok_api, raison_api = _api_embeddings_disponible()
        if ok_local or ok_api:
            return True, ''
        return False, "aucun moteur d'embeddings (" + raison_api + ')'
    return True, ''


def _identifiant(nom, candidat):
    """Identifiant trace du moteur (ex. ``spacy:fr_core_news_lg``)."""
    niveau, moteur, paquet, cle_modele = candidat
    if moteur == 'spacy':
        charge = _CHARGES.get(('spacy', 'spacy'))
        installes = _modeles_spacy_installes()
        nom_modele = charge.meta.get('name') if charge is not None and hasattr(charge, 'meta') else (installes[0] if installes else modele('spacy'))
        langue = charge.meta.get('lang') if charge is not None and hasattr(charge, 'meta') else None
        if langue and nom_modele and not nom_modele.startswith(langue + '_'):
            nom_modele = langue + '_' + nom_modele
        return 'spacy:' + str(nom_modele)
    if moteur == 'llm':
        return noyau.moteur_llm() if noyau.est_branche() else 'llm'
    if moteur == 'api':
        config = noyau.srv().CONFIG if noyau.est_branche() else {}
        return 'api:' + str(config.get('provider', '')) + ':' + str(config.get('embedding_model', ''))
    if cle_modele:
        return moteur + ':' + str(modele(cle_modele))
    return moteur


def _essayer(nom, actions):
    """Essaie les candidats dans l'ordre ; ``actions[moteur]`` est appelee.

    Renvoie (resultat, identifiant). Leve Indisponible si aucun ne convient.
    """
    raisons = []
    for candidat in candidats(nom):
        moteur = candidat[1]
        if moteur not in actions:
            continue
        ok, raison = _verifier(nom, candidat)
        if not ok:
            raisons.append(moteur + ' : ' + raison)
            continue
        try:
            resultat = actions[moteur]()
        except Indisponible as erreur:
            raisons.append(moteur + ' : ' + str(erreur))
            continue
        identifiant = _identifiant(nom, candidat)
        if isinstance(resultat, tuple) and len(resultat) == 2 and resultat[0] == '__id__':
            identifiant = resultat[1][1]
            resultat = resultat[1][0]
        return resultat, _noter(nom, identifiant)
    raise Indisponible('Aucun moteur disponible pour ' + nom + ' : ' + ' ; '.join(raisons))


# ---------------------------------------------------------------------------
# spaCy
# ---------------------------------------------------------------------------

def _modeles_spacy_installes():
    """Modeles spaCy francais installes (sans import de spaCy)."""
    souhaite = modele('spacy')
    liste = [souhaite] + [m for m in REPLIS_SPACY if m != souhaite]
    return [m for m in liste if m and paquet_present(m)]


def _spacy():
    """Pipeline spaCy francais (modele configure, sinon md, sinon sm)."""
    def fabrique():
        spacy = _importer('spacy')
        installes = _modeles_spacy_installes()
        if not installes:
            raise Indisponible('aucun modele spaCy francais installe')
        nlp = spacy.load(installes[0])
        nlp.max_length = max(getattr(nlp, 'max_length', 1000000), 2000000)
        return nlp
    return _charger(('spacy', 'spacy'), fabrique)


# ---------------------------------------------------------------------------
# Phrases
# ---------------------------------------------------------------------------

ABREVIATIONS = {
    'm', 'mm', 'mme', 'mmes', 'mlle', 'mlles', 'dr', 'drs', 'pr', 'me', 'st', 'ste', 'sts', 'mgr', 'cf', 'ex', 'env',
    'av', 'apr', 'n', 'no', 'nos', 'p', 'pp', 'vol', 'chap', 'art', 'fig', 'ed', 'eds', 'janv', 'fevr', 'fev', 'avr',
    'juil', 'sept', 'oct', 'nov', 'dec', 'tel', 'hab', 'vs', 'op', 'cit', 'ibid', 'al', 'sq', 'ref', 'inc', 'ltd', 'co',
    'dept', 'bd', 'boul', 'gen', 'col', 'cdt', 'lt', 'jr', 'sr', 'prof', 'min', 'max', 'approx', 'resp', 'cie', 'ets',
    'ill', 'coll', 'trad', 'dir', 'hors', 'suppl', 'anc', 'nb', 'sect', 'vve', 'av. j.-c', 'j.-c', 'apr. j.-c',
}
ABREVIATIONS_FINALES = {'etc'}
_PONCTUATION_FINALE = re.compile(r'(?:\.{3}|[.!?…])+(?:[\s\u00a0\u202f]*[»”"’\')\]])*')
_MARQUEUR_LISTE = re.compile(r'^\s*(?:[-*•–—]\s|\d{1,3}[.)]\s|#{1,6}\s)')
_ESPACES = ' \t\r\n\u00a0\u202f'


def phrases(texte, avec_moteur=False):
    """Offsets ``(debut, fin)`` des phrases de ``texte``.

    spaCy (``modele:spacy``), puis SaT (wtpsplit), puis regles. Le texte est
    d'abord coupe en blocs par la structure (retours a la ligne), puis chaque
    bloc en phrases. ``avec_moteur=True`` renvoie ``(offsets, moteur)`` ;
    sinon le moteur est disponible via ``dernier('phrases')``.
    """
    texte = texte or ''
    actions = {
        'spacy': lambda: _phrases_spacy(texte),
        'sat': lambda: _phrases_sat(texte),
        'regles': lambda: phrases_regles(texte),
    }
    offsets, moteur = _essayer('phrases', actions)
    offsets = _limiter_longueur(texte, offsets)
    if avec_moteur:
        return offsets, moteur
    return offsets


def blocs(texte):
    """Blocs structurels : un retour a la ligne coupe sauf si la ligne suivante continue la phrase."""
    resultat = []
    debut = 0
    position = 0
    longueur = len(texte)
    while position < longueur:
        fin_ligne = texte.find('\n', position)
        if fin_ligne < 0:
            break
        ligne = texte[position:fin_ligne]
        suite = texte[fin_ligne + 1:texte.find('\n', fin_ligne + 1) if texte.find('\n', fin_ligne + 1) >= 0 else longueur]
        if _coupe_ligne(ligne, suite):
            resultat.append((debut, fin_ligne))
            debut = fin_ligne + 1
        position = fin_ligne + 1
    resultat.append((debut, longueur))
    return [b for b in (_rogner(texte, d, f) for d, f in resultat) if b]


def _coupe_ligne(ligne, suite):
    """Vrai si le retour a la ligne entre ``ligne`` et ``suite`` separe deux blocs."""
    avant = ligne.rstrip(_ESPACES)
    apres = suite.lstrip(_ESPACES)
    if not avant or not apres:
        return True
    if _MARQUEUR_LISTE.match(suite) or _MARQUEUR_LISTE.match(ligne):
        return True
    if apres[0].islower():
        return False
    return True


def phrases_regles(texte):
    """Segmentation par regles, robuste au francais.

    Gere les abreviations (M., Mme, cf., p., n., env., etc.), les initiales
    (J. R. R. Tolkien), les nombres decimaux (3,5 ; 2.7), les points de
    suspension, les guillemets (pas de coupure a l'interieur de « ... » sauf
    citation tres longue) et les retours a la ligne structurels.
    """
    resultat = []
    for debut_bloc, fin_bloc in blocs(texte):
        resultat.extend(_phrases_bloc(texte, debut_bloc, fin_bloc))
    return resultat


def _phrases_bloc(texte, debut_bloc, fin_bloc):
    """Phrases d'un bloc sans retour a la ligne structurel."""
    resultat = []
    debut = debut_bloc
    bloc = texte[debut_bloc:fin_bloc]
    for match in _PONCTUATION_FINALE.finditer(bloc):
        fin = debut_bloc + match.end()
        if fin >= fin_bloc:
            break
        if texte[fin] not in _ESPACES:
            continue
        suivant = _premier_non_espace(texte, fin, fin_bloc)
        if suivant is None:
            break
        if not _debut_de_phrase(texte[suivant]):
            continue
        ponctuation = match.group(0)
        if ponctuation.startswith('.') and not ponctuation.startswith('...'):
            if re.fullmatch(r'\d{1,3}|[IVXLC]{1,6}|[a-zA-Z]', texte[debut:debut_bloc + match.start()].strip()):
                continue
            if _abreviation_avant(texte, debut_bloc + match.start(), texte[suivant]):
                continue
        if _dans_guillemets(texte, debut, debut_bloc + match.start()):
            continue
        segment = _rogner(texte, debut, fin)
        if segment:
            resultat.append(segment)
        debut = suivant
    segment = _rogner(texte, debut, fin_bloc)
    if segment:
        resultat.append(segment)
    return resultat


def _premier_non_espace(texte, position, limite):
    """Index du premier caractere non blanc a partir de ``position``."""
    while position < limite and texte[position] in _ESPACES:
        position += 1
    return position if position < limite else None


def _debut_de_phrase(caractere):
    """Vrai si ``caractere`` peut ouvrir une phrase."""
    return caractere.isupper() or caractere.isdigit() or caractere in '«"“(–—-•*['


def _abreviation_avant(texte, position_point, suivant):
    """Vrai si le point en ``position_point`` termine une abreviation ou une initiale."""
    debut = position_point
    while debut > 0 and (texte[debut - 1].isalnum() or texte[debut - 1] in ".-'’°"):
        debut -= 1
    mot = texte[debut:position_point]
    if not mot:
        return False
    if "'" in mot or '’' in mot:
        mot = re.split("['’]", mot)[-1]
    simple = _sans_accents(mot).strip('.')
    if len(mot) == 1 and mot.isalpha() and mot.isupper():
        return True
    if simple in ABREVIATIONS:
        return True
    if simple in ABREVIATIONS_FINALES:
        return False
    if re.fullmatch(r'(?:[a-z]\.)+[a-z]', simple):
        return True
    return False


def _dans_guillemets(texte, debut, position):
    """Vrai si ``position`` est a l'interieur de guillemets francais ouverts depuis ``debut``."""
    extrait = texte[debut:position]
    ouverts = extrait.count('«') - extrait.count('»')
    if ouverts <= 0:
        return False
    ouverture = extrait.rfind('«')
    return position - (debut + ouverture) < 600


def _rogner(texte, debut, fin):
    """Retire les blancs aux extremites ; None si vide."""
    while debut < fin and texte[debut] in _ESPACES:
        debut += 1
    while fin > debut and texte[fin - 1] in _ESPACES:
        fin -= 1
    if fin <= debut:
        return None
    return (debut, fin)


def _limiter_longueur(texte, offsets):
    """Recoupe les phrases anormalement longues sur « ; » ou « : »."""
    maximum = seuils.get('m1.segment.phrase_caracteres_max')
    resultat = []
    for debut, fin in offsets:
        while fin - debut > maximum:
            coupe = max(texte.rfind('; ', debut, debut + maximum), texte.rfind(': ', debut, debut + maximum))
            if coupe <= debut:
                coupe = texte.rfind(' ', debut, debut + maximum)
            if coupe <= debut:
                break
            morceau = _rogner(texte, debut, coupe + 1)
            if morceau:
                resultat.append(morceau)
            debut = coupe + 1
        morceau = _rogner(texte, debut, fin)
        if morceau:
            resultat.append(morceau)
    return resultat


def _phrases_spacy(texte):
    """Phrases par spaCy, bloc structurel par bloc structurel."""
    nlp = _spacy()
    resultat = []
    for debut_bloc, fin_bloc in blocs(texte):
        doc = nlp(texte[debut_bloc:fin_bloc], disable=[p for p in ('ner',) if p in nlp.pipe_names])
        segments = []
        for phrase in doc.sents:
            segment = _rogner(texte, debut_bloc + phrase.start_char, debut_bloc + phrase.end_char)
            if segment:
                segments.append(segment)
        resultat.extend(_fusionner_fragments(texte, segments))
    return resultat


def _fusionner_fragments(texte, segments):
    """Rattache a la phrase precedente (ou suivante) les fragments sans lettre (« . », « 01 »).

    Les segmenteurs statistiques isolent parfois une ponctuation ou un numero
    sur du texte web ; on ne fusionne qu'a l'interieur d'un meme bloc.
    """
    resultat = []
    en_attente = None
    for debut, fin in segments:
        sans_lettre = not any(c.isalpha() for c in texte[debut:fin])
        if sans_lettre and resultat:
            resultat[-1] = (resultat[-1][0], fin)
            continue
        if sans_lettre:
            en_attente = debut if en_attente is None else en_attente
            continue
        if en_attente is not None:
            debut = en_attente
            en_attente = None
        resultat.append((debut, fin))
    if en_attente is not None:
        resultat.append((en_attente, segments[-1][1]))
    return resultat


def _phrases_sat(texte):
    """Phrases par SaT (wtpsplit) ; offsets recalcules sur le texte."""
    def fabrique():
        wtpsplit = _importer('wtpsplit')
        return wtpsplit.SaT(modele('sat'))
    sat = _charger(('phrases', 'sat'), fabrique)
    resultat = []
    for debut_bloc, fin_bloc in blocs(texte):
        bloc = texte[debut_bloc:fin_bloc]
        curseur = 0
        segments = []
        for morceau in sat.split(bloc):
            propre = morceau.strip(_ESPACES)
            if not propre:
                continue
            position = bloc.find(propre, curseur)
            if position < 0:
                raise Indisponible('SaT a modifie le texte : offsets introuvables')
            segments.append((debut_bloc + position, debut_bloc + position + len(propre)))
            curseur = position + len(propre)
        resultat.extend(_fusionner_fragments(texte, segments))
    return resultat


# ---------------------------------------------------------------------------
# Analyse syntaxique
# ---------------------------------------------------------------------------

def analyse(texte):
    """Document spaCy (arbre de dependances, morphologie) ou None si spaCy est absent."""
    try:
        doc, _ = _essayer('analyse', {'spacy': lambda: _spacy()(texte or '')})
    except Indisponible:
        _noter('analyse', None)
        return None
    return doc


# ---------------------------------------------------------------------------
# Entites
# ---------------------------------------------------------------------------

_NOMBRE = r'\d{1,3}(?:[ \u00a0\u202f.]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?'
_ECHELLE = r'(?:milliards?|millions?|mds?|md|m|k|mille)'
_DEVISE = r"(?:€|euros?|eur\b|\$|dollars?|usd\b|£|livres?\s+sterling|francs?\s+suisses?|chf\b)"
_RE_POURCENTAGE = re.compile(r'(?<![\w.,])[-+\u2212]?(?:' + _NOMBRE + r')\s?(?:%|pour\s?cent\b|points?\s+de\s+pourcentage\b)', re.I)
_RE_MONTANT = re.compile(
    r'(?<![\w.,])(?:(?:' + _NOMBRE + r')(?:\s?' + _ECHELLE + r"\.?)?(?:\s?(?:d['’]|de\s+))?\s?" + _DEVISE + r'(?:\s?(?:HT|TTC)\b)?'
    r'|(?:\$|€|£)\s?(?:' + _NOMBRE + r')(?:\s?' + _ECHELLE + r'\b)?)',
    re.I,
)
_MOIS_FR = r'(?:janvier|f[eé]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[eé]cembre|janv\.|f[eé]vr\.|avr\.|juil\.|sept\.|oct\.|nov\.|d[eé]c\.)'
_JOURS_FR = r'(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)'
_RE_DATES = [
    re.compile(r'\b(?:' + _JOURS_FR + r'\s+)?(?:1er|[0-3]?\d)\s+' + _MOIS_FR + r'(?:\s+(?:19|20)\d{2})?(?![\w])', re.I),
    re.compile(r'\b' + _MOIS_FR + r'\s+(?:19|20)\d{2}\b', re.I),
    re.compile(r'\b[0-3]?\d/[01]?\d/(?:19|20)\d{2}\b'),
    re.compile(r'\b(?:19|20)\d{2}-[01]\d-[0-3]\d\b'),
    re.compile(r"(?:\b(?:en|depuis|d[eè]s|jusqu['’]en|avant|apr[eè]s|fin|d[eé]but|mi-|courant|ann[eé]e|l['’]an|au\s+cours\s+de)\s+)((?:19|20)\d{2})\b(?![,.]\d)", re.I),
]
_CIVILITES = re.compile(r'(?:\bM\.|\bMM\.|\bMme|\bMlle|\bDr|\bPr|\bMe)\s*$')
_MOTS_ORGANISATION = {
    'societe', 'groupe', 'ministere', 'agence', 'institut', 'universite', 'banque', 'commission', 'conseil',
    'federation', 'association', 'office', 'autorite', 'fondation', 'syndicat', 'compagnie', 'direction', 'cour',
    'tribunal', 'assemblee', 'senat', 'mairie', 'region', 'departement', 'prefecture', 'insee', 'cnrs',
}
_LIAISONS = {'de', 'du', 'des', 'd', 'la', 'le', 'les', 'l', '&', 'von', 'van', 'der', 'sur'}
_DEBUTS_COMMUNS = {
    'le', 'la', 'les', 'l', 'un', 'une', 'des', 'du', 'de', 'd', 'il', 'elle', 'ils', 'elles', 'on', 'nous', 'vous', 'je',
    'ce', 'cet', 'cette', 'ces', 'cela', 'ceci', 'selon', 'depuis', 'en', 'dans', 'pour', 'par', 'sur', 'sous', 'avec',
    'sans', 'mais', 'et', 'ou', 'donc', 'or', 'ni', 'car', 'si', 'au', 'aux', 'a', 'apres', 'avant', 'lors', 'quand',
    'comme', 'chaque', 'tout', 'tous', 'toute', 'toutes', 'son', 'sa', 'ses', 'leur', 'leurs', 'notre', 'nos', 'votre',
    'vos', 'mon', 'ma', 'mes', 'aucun', 'aucune', 'plusieurs', 'certains', 'certaines', 'cependant', 'ainsi', 'enfin',
    'pourtant', 'toutefois', 'alors', 'puis', 'ensuite', 'qui', 'que', 'quoi', 'quel', 'quelle', 'quels', 'quelles',
    'oui', 'non', 'voici', 'voila', 'entre', 'vers', 'chez', 'contre', 'parmi', 'pendant', 'des', 'outre', 'meme',
    'lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche', 'janvier', 'fevrier', 'mars', 'avril',
    'mai', 'juin', 'juillet', 'aout', 'septembre', 'octobre', 'novembre', 'decembre', 'titre', 'source', 'sources',
}
_RE_MOT_MAJ = re.compile(r"[A-ZÀ-ÖØ-Þ][\wÀ-ſ\-]*(?:['’][\wÀ-ſ]+)?")
_RE_JETON = re.compile(r"[\wÀ-ſ&]+(?:[-'’][\wÀ-ſ]+)*|['’]")


def entites(texte, types=None):
    """Entites nommees : liste de dict(text, type, start, end, engine, score).

    GLiNER (types libres, ``modele:entites``), sinon spaCy NER completee par
    les regles pour les dates, montants et pourcentages, sinon regles seules.
    ``types`` filtre les types renvoyes (pour GLiNER : etiquettes demandees).
    """
    texte = texte or ''
    demandes = list(types) if types else None
    actions = {
        'gliner': lambda: _entites_gliner(texte, demandes or list(TYPES_ENTITES)),
        'spacy': lambda: _entites_spacy(texte),
        'regles': lambda: entites_regles(texte),
    }
    resultat, _ = _essayer('entites', actions)
    if demandes:
        resultat = [e for e in resultat if e['type'] in demandes]
    return resultat


def entites_regles(texte):
    """Entites par regles : dates, montants, pourcentages, noms propres multi-mots."""
    candidats_trouves = []
    for match in _RE_POURCENTAGE.finditer(texte):
        candidats_trouves.append(_entite(texte, match.start(), match.end(), 'pourcentage', 'regles', 0.95))
    for match in _RE_MONTANT.finditer(texte):
        candidats_trouves.append(_entite(texte, match.start(), match.end(), 'montant', 'regles', 0.9))
    for motif in _RE_DATES:
        for match in motif.finditer(texte):
            debut = match.start(1) if match.groups() else match.start()
            fin = match.end(1) if match.groups() else match.end()
            candidats_trouves.append(_entite(texte, debut, fin, 'date', 'regles', 0.9))
    candidats_trouves.extend(_noms_propres(texte))
    return _sans_chevauchement(candidats_trouves)


def _entite(texte, debut, fin, type_entite, moteur, score):
    """Construit le dict d'une entite (offsets rognes)."""
    while debut < fin and texte[debut] in _ESPACES:
        debut += 1
    while fin > debut and texte[fin - 1] in _ESPACES + '.,;:':
        if texte[fin - 1] == '.' and type_entite == 'date' and texte[debut:fin].lower().endswith(('janv.', 'fevr.', 'févr.', 'avr.', 'juil.', 'sept.', 'oct.', 'nov.', 'dec.', 'déc.')):
            break
        fin -= 1
    return {'text': texte[debut:fin], 'type': type_entite, 'start': debut, 'end': fin, 'engine': moteur, 'score': score}


_ELISIONS = {'c', 'l', 'd', 'j', 'n', 's', 'm', 't', 'qu', 'jusqu', 'lorsqu', 'puisqu', 'quoiqu'}
_CIVILITES_MOTS = {'M', 'MM', 'Mme', 'Mmes', 'Mlle', 'Dr', 'Pr', 'Me', 'Mgr'}


def _jetons(texte):
    """Jetons ``(debut, fin, mot)`` ; l'elision est detachee (« l'Insee » -> « Insee »)."""
    resultat = []
    for match in _RE_JETON.finditer(texte):
        mot = match.group(0)
        debut = match.start()
        morceaux = re.split("(['\u2019])", mot, maxsplit=1)
        if len(morceaux) == 3 and morceaux[0].lower() in _ELISIONS and morceaux[2]:
            debut += len(morceaux[0]) + 1
            mot = morceaux[2]
        resultat.append((debut, match.end(), mot))
    return resultat


def _ecart_admis(texte, precedent, suivant):
    """Vrai si l'ecart entre deux jetons n'interrompt pas un nom propre."""
    ecart = texte[precedent[1]:suivant[0]]
    if '\n' in ecart:
        return False
    propre = ecart.strip(" \u00a0\u202f-")
    if propre == '':
        return True
    initiale = len(precedent[2]) == 1 and precedent[2].isupper()
    return propre == '.' and (initiale or precedent[2] in _CIVILITES_MOTS)


def _noms_propres(texte):
    """Suites de mots a majuscule (initiales et liaisons « de », « la »... comprises) ; type devine."""
    debuts_phrase = {d for d, _ in phrases_regles(texte)}
    jetons = _jetons(texte)
    resultat = []
    index = 0
    while index < len(jetons):
        if not _majuscule(jetons[index][2]):
            index += 1
            continue
        groupe = [index]
        suivant = index + 1
        while suivant < len(jetons) and _ecart_admis(texte, jetons[suivant - 1], jetons[suivant]):
            mot_suivant = jetons[suivant][2]
            if _majuscule(mot_suivant):
                groupe.append(suivant)
                suivant += 1
                continue
            if _sans_accents(mot_suivant) in _LIAISONS:
                fin_liaison = suivant
                while fin_liaison + 1 < len(jetons) and _sans_accents(jetons[fin_liaison + 1][2]) in _LIAISONS:
                    fin_liaison += 1
                if fin_liaison + 1 < len(jetons) and _majuscule(jetons[fin_liaison + 1][2]):
                    groupe.extend(range(suivant, fin_liaison + 2))
                    suivant = fin_liaison + 2
                    continue
            break
        personne = False
        while groupe and jetons[groupe[0]][2] in _CIVILITES_MOTS and len(groupe) > 1:
            groupe = groupe[1:]
            personne = True
        groupe = _retirer_debuts_communs(texte, jetons, groupe, debuts_phrase)
        if groupe and not (len(groupe) == 1 and len(jetons[groupe[0]][2]) == 1):
            premier = jetons[groupe[0]]
            dernier_jeton = jetons[groupe[-1]]
            nom = texte[premier[0]:dernier_jeton[1]]
            type_entite = 'personne' if personne else _type_nom_propre(texte, premier[0], nom, len(groupe))
            resultat.append(_entite(texte, premier[0], dernier_jeton[1], type_entite, 'regles', 0.6))
        index = max(suivant, index + 1)
    return resultat


def _majuscule(mot):
    """Vrai si le mot commence par une majuscule (sigles compris)."""
    return bool(mot) and mot[0].isupper()


def _retirer_debuts_communs(texte, jetons, groupe, debuts_phrase):
    """Retire les mots-outils capitalises en tete (« Selon », « Le »...).

    Un mot seul en debut de phrase est ecarte s'il apparait ailleurs en
    minuscules dans le texte ou s'il finit en « -ment » (adverbe).
    """
    while groupe:
        debut, _, mot = jetons[groupe[0]]
        simple = _sans_accents(mot).strip("'\u2019")
        if simple in _DEBUTS_COMMUNS or simple in _LIAISONS:
            groupe = groupe[1:]
            continue
        if len(groupe) == 1 and debut in debuts_phrase and not mot.isupper():
            motif = r'(?<![\w\u00c0-\u017f])' + re.escape(mot.lower()) + r'(?![\w\u00c0-\u017f])'
            if re.search(motif, texte) or simple.endswith('ment'):
                return []
        break
    while groupe and _sans_accents(jetons[groupe[-1]][2]).strip("'’") in _LIAISONS:
        groupe = groupe[:-1]
    return groupe


def _type_nom_propre(texte, debut, nom, taille):
    """Type devine d'un nom propre, ou None pour l'ignorer."""
    avant = texte[max(0, debut - 6):debut]
    if _CIVILITES.search(avant):
        return 'personne'
    mots = {_sans_accents(m) for m in re.findall(r'[\wÀ-ſ]+', nom)}
    if mots & _MOTS_ORGANISATION:
        return 'organisation'
    if taille == 1 and nom.isupper() and 2 <= len(nom) <= 8:
        return 'organisation'
    return 'nom_propre'


def _sans_chevauchement(liste):
    """Garde les entites sans chevauchement : types numeriques prioritaires, puis plus longues."""
    priorite = {'date': 0, 'montant': 0, 'pourcentage': 0}
    ordre = sorted(liste, key=lambda e: (priorite.get(e['type'], 1), -(e['end'] - e['start']), e['start']))
    gardees = []
    for entite in ordre:
        if entite['end'] <= entite['start']:
            continue
        if any(entite['start'] < g['end'] and g['start'] < entite['end'] for g in gardees):
            continue
        gardees.append(entite)
    return sorted(gardees, key=lambda e: (e['start'], e['end']))


def _entites_spacy(texte):
    """NER spaCy (PER, ORG, LOC, MISC) completee par les regles numeriques et de dates."""
    nlp = _spacy()
    identifiant = _identifiant('entites', NIVEAUX['entites'][1])
    resultat = []
    for debut_bloc, fin_bloc in blocs(texte):
        doc = nlp(texte[debut_bloc:fin_bloc])
        for ent in doc.ents:
            type_entite = TYPES_SPACY.get(ent.label_, 'divers')
            resultat.append(_entite(texte, debut_bloc + ent.start_char, debut_bloc + ent.end_char, type_entite, identifiant, 0.8))
    numeriques = [e for e in entites_regles(texte) if e['type'] in ('date', 'montant', 'pourcentage')]
    return _sans_chevauchement(numeriques + resultat)


def _entites_gliner(texte, etiquettes):
    """NER zero-shot GLiNER, phrase par phrase (limite de longueur du modele)."""
    nom = modele('entites')

    def fabrique():
        gliner = _importer('gliner')
        return gliner.GLiNER.from_pretrained(nom)
    modele_gliner = _charger(('entites', 'gliner'), fabrique)
    seuil = seuils.get('m1.entites.score_min')
    identifiant = 'gliner:' + nom
    resultat = []
    for debut, fin in _fenetres(texte, phrases_regles(texte), 1000):
        morceau = texte[debut:fin]
        for prediction in modele_gliner.predict_entities(morceau, etiquettes, threshold=seuil):
            entite = _entite(texte, debut + int(prediction['start']), debut + int(prediction['end']),
                             str(prediction['label']), identifiant, float(prediction.get('score', 0.0)))
            resultat.append(entite)
    return _sans_chevauchement(resultat)


def _fenetres(texte, offsets, taille):
    """Regroupe des phrases consecutives en fenetres d'au plus ``taille`` caracteres."""
    fenetres = []
    debut = None
    fin = None
    for d, f in offsets:
        if debut is None:
            debut, fin = d, f
        elif f - debut <= taille:
            fin = f
        else:
            fenetres.append((debut, fin))
            debut, fin = d, f
    if debut is not None:
        fenetres.append((debut, fin))
    return fenetres


# ---------------------------------------------------------------------------
# NLI
# ---------------------------------------------------------------------------

SCHEMA_NLI = noyau.schema('chaine:nli', noyau.objet({
    'resultats': noyau.tableau(noyau.objet({
        'id': {'type': 'integer'},
        'label': noyau.enum(LABELS_NLI),
        'score': {'type': 'number'},
    })),
}))

TACHE_NLI = (
    "Inference en langue naturelle. Pour chaque paire (id, premisse, hypothese), dis si la PREMISSE seule "
    "implique l'hypothese (implication), la contredit (contradiction) ou ni l'un ni l'autre (neutre). "
    "N'utilise aucune connaissance exterieure : une hypothese plausible mais absente de la premisse est neutre. "
    "score = ta confiance entre 0 et 1. Reponds pour chaque id, sans en omettre."
)


def nli(premisse, hypothese):
    """NLI d'une paire : dict(label, score, engine, probas)."""
    return nli_lot([(premisse, hypothese)])[0]


def nli_lot(paires):
    """NLI d'une liste de paires ``(premisse, hypothese)``.

    mDeBERTa-v3 XNLI local, sinon LLM contraint (mode ``chaine:nli``). Jamais
    de repli par regles : leve ``S.OperationalError`` (ou RuntimeError sans
    serveur) si aucun moteur n'est disponible.
    ``score`` = probabilite du label retenu ; ``probas`` = les trois probabilites.
    """
    paires = [(str(p or ''), str(h or '')) for p, h in paires]
    if not paires:
        return []
    actions = {
        'transformers': lambda: _nli_local(paires),
        'llm': lambda: _nli_llm(paires),
    }
    try:
        resultat, moteur = _essayer('nli', actions)
    except Indisponible as erreur:
        _lever(str(erreur))
    for element in resultat:
        element['engine'] = moteur
    return resultat


REGLAGE_NLI_PAR_LLM = 'chaine:nli_par_llm'


def nli_par_llm_autorise():
    """Vrai si le reglage ``chaine:nli_par_llm`` autorise le LLM a controler ses propres sorties (double passe faible)."""
    return noyau._vrai(noyau.reglage(REGLAGE_NLI_PAR_LLM, '0'))


def nli_prevu():
    """Identifiant du moteur NLI qui serait utilise maintenant (sans rien charger), ou None."""
    for candidat in candidats('nli'):
        ok, _ = _verifier('nli', candidat)
        if ok:
            return _identifiant('nli', candidat)
    return None


def _est_llm(identifiant):
    """Vrai si l'identifiant de moteur designe le LLM configure."""
    return str(identifiant or '').startswith('llm')


def raison_double_passe():
    """Raison qui empeche de controler par NLI une sortie generative du LLM, ou None.

    Principe « Double passe » : toute sortie generative est reverifiee par un autre mecanisme.
    Si le seul NLI disponible est le LLM lui-meme, le controle est illusoire : refuse, sauf
    reglage explicite ``chaine:nli_par_llm`` (l'execution est alors marquee ``double_passe: faible``).
    """
    prevu = nli_prevu()
    if prevu is None:
        return ('aucun moteur NLI : installez le modèle local (installer_modeles.cmd) pour contrôler les sorties du LLM')
    if _est_llm(prevu) and not nli_par_llm_autorise():
        return ('double passe illusoire : le seul NLI disponible est le LLM (' + prevu + ') qui produit les sorties à contrôler ; '
                'installez le NLI local (mDeBERTa, installer_modeles.cmd) ou autorisez explicitement le réglage '
                + REGLAGE_NLI_PAR_LLM + ' (double passe faible)')
    return None


def constater_double_passe(resultats):
    """Apres un NLI de controle d'une sortie du LLM : note la force de la double passe sur l'execution courante.

    Leve l'erreur operationnelle si le NLI a ete fait par le LLM sans ``chaine:nli_par_llm``.
    Renvoie ``'forte'`` ou ``'faible'``.
    """
    moteur = resultats[0].get('engine') if resultats else None
    if _est_llm(moteur):
        if not nli_par_llm_autorise():
            _lever(raison_double_passe() or 'double passe illusoire : NLI fait par le LLM controle')
        noyau.noter_moteur('double_passe', 'faible')
        return 'faible'
    noyau.noter_moteur('double_passe', 'forte')
    return 'forte'


def _lever(message):
    """Leve l'erreur operationnelle du serveur (RuntimeError sans serveur)."""
    if noyau.est_branche():
        raise noyau.srv().OperationalError(message)
    raise RuntimeError(message)


def _nli_local(paires):
    """mDeBERTa XNLI via transformers + torch."""
    nom = modele('nli')

    def fabrique():
        transformers = _importer('transformers')
        _importer('torch')
        tokenizer = transformers.AutoTokenizer.from_pretrained(nom)
        reseau = transformers.AutoModelForSequenceClassification.from_pretrained(nom)
        reseau.eval()
        return tokenizer, reseau
    tokenizer, reseau = _charger(('nli', 'transformers'), fabrique)
    torch = _importer('torch')
    correspondance = _labels_modele_nli(reseau)
    taille = seuils.get('nli.taille_lot')
    jetons_max = int(seuils.get('nli.jetons_max'))
    # Premisse trop longue pour la fenetre du modele : decoupee en fenetres de phrases (jamais tronquee en silence).
    elementaires = []
    origine = []
    for rang, (premisse, hypothese) in enumerate(paires):
        fenetres = _fenetres_nli(tokenizer, premisse, hypothese, jetons_max)
        for fenetre in fenetres:
            elementaires.append((fenetre, hypothese))
            origine.append(rang)
    sorties = []
    for debut in range(0, len(elementaires), taille):
        lot = elementaires[debut:debut + taille]
        encodage = tokenizer([p for p, _ in lot], [h for _, h in lot], truncation=True, padding=True,
                             max_length=jetons_max, return_tensors='pt')
        with torch.no_grad():
            logits = reseau(**encodage).logits
        probabilites = torch.softmax(logits, dim=-1).tolist()
        for ligne in probabilites:
            probas = {label: 0.0 for label in LABELS_NLI}
            for index, valeur in enumerate(ligne):
                probas[correspondance[index]] += float(valeur)
            sorties.append(probas)
    par_paire = [[] for _ in paires]
    for rang, probas in zip(origine, sorties):
        par_paire[rang].append(probas)
    resultat = []
    for liste in par_paire:
        probas = _agreger_fenetres(liste)
        label = max(probas, key=probas.get)
        element = {'label': label, 'score': probas[label], 'probas': probas}
        if len(liste) > 1:
            element['fenetres'] = len(liste)
        resultat.append(element)
    return resultat


def _nombre_jetons(tokenizer, texte):
    """Nombre de jetons d'un texte pour le tokeniseur (listes Python ou tenseurs)."""
    identifiants = tokenizer(texte)['input_ids']
    if hasattr(identifiants, 'tolist'):
        identifiants = identifiants.tolist()
    if identifiants and isinstance(identifiants[0], list):
        identifiants = identifiants[0]
    return len(identifiants)


def _fenetres_nli(tokenizer, premisse, hypothese, jetons_max):
    """Premisse decoupee en fenetres de phrases qui tiennent, avec l'hypothese, dans ``jetons_max`` jetons."""
    marge = 8   # jetons speciaux du modele (debut, separateurs, fin)
    budget = jetons_max - _nombre_jetons(tokenizer, hypothese) - marge
    if budget <= 0 or _nombre_jetons(tokenizer, premisse) <= budget:
        return [premisse]
    fenetres = []
    courante = ''
    for debut, fin in phrases_regles(premisse):
        phrase = premisse[debut:fin]
        essai = (courante + ' ' + phrase).strip()
        if courante and _nombre_jetons(tokenizer, essai) > budget:
            fenetres.append(courante)
            courante = phrase
        else:
            courante = essai
    if courante:
        fenetres.append(courante)
    return fenetres or [premisse]


def _agreger_fenetres(liste):
    """Probabilites d'une paire a partir de ses fenetres : la fenetre la plus probante (implication ou contradiction)."""
    if len(liste) == 1:
        return liste[0]
    meilleure_implication = max(liste, key=lambda p: p['implication'])
    meilleure_contradiction = max(liste, key=lambda p: p['contradiction'])
    if meilleure_implication['implication'] >= meilleure_contradiction['contradiction']:
        return dict(meilleure_implication)
    return dict(meilleure_contradiction)


def _labels_modele_nli(reseau):
    """Index de sortie du modele -> label de la chaine, d'apres ``config.id2label``."""
    id2label = getattr(getattr(reseau, 'config', None), 'id2label', None) or {0: 'entailment', 1: 'neutral', 2: 'contradiction'}
    correspondance = {}
    for index, label in id2label.items():
        texte = str(label).lower()
        if texte.startswith('entail'):
            correspondance[int(index)] = 'implication'
        elif texte.startswith('contra'):
            correspondance[int(index)] = 'contradiction'
        else:
            correspondance[int(index)] = 'neutre'
    return correspondance


def _nli_llm(paires):
    """NLI par le LLM configure, sortie contrainte et revalidee ; lots de ``llm.nli.taille_lot``."""
    ok, raison = _llm_disponible()
    if not ok:
        raise Indisponible(raison)
    taille = seuils.get('llm.nli.taille_lot')
    resultat = []
    for debut in range(0, len(paires), taille):
        lot = paires[debut:debut + taille]
        reponses = _nli_llm_lot(lot)
        manquants = [i for i in range(len(lot)) if i not in reponses]
        for index in manquants:
            seul = _nli_llm_lot([lot[index]])
            if 0 not in seul:
                _lever('NLI par LLM : reponse absente pour une paire, meme seule.')
            reponses[index] = seul[0]
        resultat.extend(reponses[i] for i in range(len(lot)))
    return resultat


def _nli_llm_lot(lot):
    """Un appel LLM pour un lot ; renvoie {index: resultat} pour les id valides."""
    payload = {
        'mode': 'chaine:nli',
        'paires': [{'id': index, 'premisse': p, 'hypothese': h} for index, (p, h) in enumerate(lot)],
    }
    sortie = noyau.llm(TACHE_NLI, payload)
    reponses = {}
    for element in sortie['resultats']:
        index = element['id']
        if not 0 <= index < len(lot) or index in reponses:
            continue
        score = min(1.0, max(0.0, float(element['score'])))
        reste = (1.0 - score) / 2.0
        probas = {label: reste for label in LABELS_NLI}
        probas[element['label']] = score
        reponses[index] = {'label': element['label'], 'score': score, 'probas': probas}
    return reponses


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------

def embeddings(textes):
    """Vecteurs des textes : ``(liste de vecteurs, moteur)``.

    sentence-transformers local (``modele:embeddings``, vecteurs normalises),
    sinon ``S.embed`` (fournisseur configure), sinon ``([], 'aucun')``.
    """
    textes = [str(t or '') for t in textes]
    if not textes:
        return [], _noter('embeddings', 'aucun')
    actions = {
        'sentence_transformers': lambda: _embeddings_local(textes),
        'api': lambda: _embeddings_api(textes),
    }
    try:
        return _essayer('embeddings', actions)
    except Indisponible:
        return [], _noter('embeddings', 'aucun')


def _embeddings_local(textes):
    """sentence-transformers."""
    nom = modele('embeddings')

    def fabrique():
        module = _importer('sentence_transformers')
        return module.SentenceTransformer(nom)
    encodeur = _charger(('embeddings', 'sentence_transformers'), fabrique)
    vecteurs = encodeur.encode(textes, batch_size=16, normalize_embeddings=True, show_progress_bar=False)
    return [_liste(v) for v in vecteurs]


_CACHE_EMBEDDINGS_API = {}


def _embeddings_api(textes):
    """``S.embed`` texte par texte ; Indisponible si une reponse manque."""
    serveur = noyau.srv()
    vecteurs = []
    modele_api = None
    for texte in textes:
        serveur.checkpoint()
        config = serveur.CONFIG
        cle = (str(serveur.DB), config.get('provider'), config.get('embedding_model'),
               config.get('lm_url'), config.get('embeddings'), texte)
        try:
            reponse = _CACHE_EMBEDDINGS_API.get(cle)
            if reponse is None:
                reponse = serveur.embed(texte)
                if reponse and reponse.get('values'):
                    if len(_CACHE_EMBEDDINGS_API) >= 2048:
                        _CACHE_EMBEDDINGS_API.pop(next(iter(_CACHE_EMBEDDINGS_API)))
                    _CACHE_EMBEDDINGS_API[cle] = reponse
        except serveur.OperationalError as erreur:
            raise Indisponible('API embeddings : ' + str(erreur)[:200]) from erreur
        if not reponse:
            raise Indisponible('API embeddings desactivee')
        modele_api = reponse.get('model')
        vecteurs.append([float(x) for x in reponse['values']])
    return '__id__', (vecteurs, 'api:' + str(modele_api))


def _liste(vecteur):
    """Vecteur numpy/torch/liste -> liste de floats."""
    if hasattr(vecteur, 'tolist'):
        vecteur = vecteur.tolist()
    return [float(x) for x in vecteur]


def cosinus(a, b):
    """Cosinus de deux vecteurs (0 si l'un est nul ou de taille differente)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    produit = sum(x * y for x, y in zip(a, b))
    normes = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return produit / normes if normes else 0.0


# ---------------------------------------------------------------------------
# Reordonnancement
# ---------------------------------------------------------------------------

def reordonner(requete, passages, avec_moteur=False):
    """Score de pertinence de chaque passage pour la requete, dans l'ordre des passages.

    Cross-encoder bge-reranker-v2-m3 (score sigmoide dans [0, 1]), sinon
    cosinus des embeddings (borne a 0), sinon recouvrement lexical pondere par
    idf (dans [0, 1]). Les echelles different selon le moteur : les seuils
    (``m3.reranker.score_min``, ``m5.pertinence.reranker_min``) sont a
    calibrer par moteur ; voir ``dernier('reordonner')``.
    """
    passages = [str(p or '') for p in passages]
    if not passages:
        return ([], _noter('reordonner', 'aucun')) if avec_moteur else []
    actions = {
        'cross_encoder': lambda: _reordonner_local(requete, passages),
        'cosinus': lambda: _reordonner_cosinus(requete, passages),
        'lexical': lambda: recouvrement_lexical(requete, passages),
    }
    scores, moteur = _essayer('reordonner', actions)
    if avec_moteur:
        return scores, moteur
    return scores


def _reordonner_local(requete, passages):
    """CrossEncoder de sentence-transformers."""
    nom = modele('reordonner')

    def fabrique():
        module = _importer('sentence_transformers')
        return module.CrossEncoder(nom, max_length=512)
    encodeur = _charger(('reordonner', 'cross_encoder'), fabrique)
    bruts = [float(x) for x in _liste(encodeur.predict([(requete, p) for p in passages]))]
    if any(x < 0.0 or x > 1.0 for x in bruts):
        bruts = [1.0 / (1.0 + math.exp(-x)) for x in bruts]
    return bruts


def _reordonner_cosinus(requete, passages):
    """Cosinus requete/passage sur les embeddings."""
    vecteurs, moteur = embeddings([requete] + passages)
    if moteur == 'aucun' or len(vecteurs) != len(passages) + 1:
        raise Indisponible("aucun moteur d'embeddings")
    scores = [max(0.0, cosinus(vecteurs[0], v)) for v in vecteurs[1:]]
    return '__id__', (scores, 'cosinus:' + moteur)


_MOTS_VIDES = set('''
a au aux avec ce ces cet cette dans de des du elle en et eux il ils je la le les leur lui ma mais me meme mes moi mon ne
nos notre nous on ou par pas pour qu que qui sa se ses son sur ta te tes toi ton tu un une vos votre vous c d j l m n s t
y est sont ete etre avoir a ont fait plus moins tres comme dont ou si cela ceci quel quelle quels quelles
'''.split())


def mots(texte):
    """Mots pleins normalises (minuscules, sans accents, pluriel simple retire)."""
    resultat = []
    for mot in re.findall(r'[a-z0-9]+', _sans_accents(texte or '')):
        if mot in _MOTS_VIDES or len(mot) < 2:
            continue
        if len(mot) > 3 and mot[-1] in 'sx' and not mot.isdigit():
            mot = mot[:-1]
        resultat.append(mot)
    return resultat


def recouvrement_lexical(requete, passages):
    """Part (ponderee par idf) des mots de la requete presents dans chaque passage, dans [0, 1]."""
    mots_requete = set(mots(requete))
    ensembles = [set(mots(p)) for p in passages]
    if not mots_requete:
        return [0.0 for _ in passages]
    total = len(ensembles)
    poids = {}
    for mot in mots_requete:
        frequence = sum(1 for e in ensembles if mot in e)
        poids[mot] = math.log((total + 1.0) / (frequence + 0.5)) + 1.0
    somme = sum(poids.values())
    return [sum(poids[m] for m in mots_requete if m in e) / somme for e in ensembles]


# ---------------------------------------------------------------------------
# Perplexite
# ---------------------------------------------------------------------------

def perplexite(texte):
    """Perplexite d'un petit modele de langue causal local ; None si absent."""
    try:
        valeur, _ = _essayer('perplexite', {'transformers': lambda: _perplexite_locale(texte or '')})
    except Indisponible:
        _noter('perplexite', None)
        return None
    return valeur


def _perplexite_locale(texte):
    """exp(perte moyenne) sous le modele ``modele:perplexite``."""
    nom = modele('perplexite')

    def fabrique():
        transformers = _importer('transformers')
        _importer('torch')
        tokenizer = transformers.AutoTokenizer.from_pretrained(nom)
        reseau = transformers.AutoModelForCausalLM.from_pretrained(nom)
        reseau.eval()
        return tokenizer, reseau
    tokenizer, reseau = _charger(('perplexite', 'transformers'), fabrique)
    torch = _importer('torch')
    encodage = tokenizer(texte, return_tensors='pt', truncation=True, max_length=256)
    if int(encodage['input_ids'].shape[-1]) < 2:
        return None
    with torch.no_grad():
        sortie = reseau(**encodage, labels=encodage['input_ids'])
    return float(math.exp(float(sortie.loss)))


# ---------------------------------------------------------------------------
# Classification few-shot
# ---------------------------------------------------------------------------

def _schema_classer(payload):
    """Schema du classement par LLM : etiquettes en enum."""
    return noyau.objet({
        'resultats': noyau.tableau(noyau.objet({
            'id': {'type': 'integer'},
            'label': noyau.enum(payload['etiquettes']),
            'score': {'type': 'number'},
        })),
    })


noyau.schema('chaine:classer', _schema_classer)

TACHE_CLASSER = (
    "Classe chaque texte (id, texte) dans une seule des etiquettes fournies, en t'appuyant sur les exemples. "
    "score = ta confiance entre 0 et 1. Reponds pour chaque id."
)


def classer(textes, etiquettes, exemples):
    """Classement few-shot : liste de dict(label, score, engine, scores).

    SetFit (``modele:classer`` comme base), sinon plus proche centroide sur les
    embeddings, sinon LLM contraint (mode ``chaine:classer``), sinon plus proche
    centroide lexical (tf-idf). ``exemples`` : dict ``etiquette -> [textes]``
    ou liste de ``(texte, etiquette)``.
    """
    textes = [str(t or '') for t in textes]
    etiquettes = list(etiquettes)
    par_etiquette = _normaliser_exemples(exemples, etiquettes)
    if not textes:
        return []
    actions = {
        'setfit': lambda: _classer_setfit(textes, etiquettes, par_etiquette),
        'centroide': lambda: _classer_centroide(textes, par_etiquette),
        'llm': lambda: _classer_llm(textes, etiquettes, par_etiquette),
        'lexical': lambda: classer_lexical(textes, par_etiquette),
    }
    resultat, moteur = _essayer('classer', actions)
    for element in resultat:
        element['engine'] = moteur
    return resultat


def _normaliser_exemples(exemples, etiquettes):
    """dict etiquette -> liste de textes, limite aux etiquettes demandees."""
    resultat = {e: [] for e in etiquettes}
    if isinstance(exemples, dict):
        elements = [(t, e) for e, liste in exemples.items() for t in liste]
    else:
        elements = []
        for element in exemples or []:
            if isinstance(element, dict):
                elements.append((element.get('texte', element.get('text')), element.get('label', element.get('etiquette'))))
            else:
                elements.append((element[0], element[1]))
    for texte, etiquette in elements:
        if etiquette in resultat and texte:
            resultat[etiquette].append(str(texte))
    return resultat


def _classer_setfit(textes, etiquettes, par_etiquette):
    """SetFit entraine sur les exemples (mis en cache par empreinte des exemples)."""
    utiles = [e for e in etiquettes if par_etiquette[e]]
    if len(utiles) < 2:
        raise Indisponible('SetFit : au moins deux etiquettes avec exemples sont necessaires')
    base = modele('classer')
    empreinte = hashlib.sha1(repr((base, sorted((e, tuple(par_etiquette[e])) for e in utiles))).encode()).hexdigest()

    def fabrique():
        setfit = _importer('setfit')
        datasets = _importer('datasets')
        modele_setfit = setfit.SetFitModel.from_pretrained(base)
        x = [t for e in utiles for t in par_etiquette[e]]
        y = [utiles.index(e) for e in utiles for _ in par_etiquette[e]]
        donnees = datasets.Dataset.from_dict({'text': x, 'label': y})
        arguments = setfit.TrainingArguments(batch_size=16, num_epochs=1)
        entraineur = setfit.Trainer(model=modele_setfit, args=arguments, train_dataset=donnees)
        entraineur.train()
        return modele_setfit
    modele_setfit = _charger(('classer', 'setfit:' + empreinte), fabrique)
    probabilites = modele_setfit.predict_proba(textes)
    classes = getattr(getattr(modele_setfit, 'model_head', None), 'classes_', None)
    classes = [int(c) for c in _liste(classes)] if classes is not None else list(range(len(utiles)))
    resultat = []
    for ligne in probabilites:
        valeurs = _liste(ligne)
        scores = {utiles[classes[i]]: valeurs[i] for i in range(len(valeurs))}
        label = max(scores, key=scores.get)
        resultat.append({'label': label, 'score': scores[label], 'scores': scores})
    return '__id__', (resultat, 'setfit:' + base)


def _classer_centroide(textes, par_etiquette):
    """Plus proche centroide (cosinus) sur les embeddings."""
    utiles = [e for e, liste in par_etiquette.items() if liste]
    if not utiles:
        raise Indisponible('aucun exemple')
    exemples = [t for e in utiles for t in par_etiquette[e]]
    vecteurs, moteur = embeddings(exemples + textes)
    if moteur == 'aucun' or len(vecteurs) != len(exemples) + len(textes):
        raise Indisponible("aucun moteur d'embeddings")
    centroides = {}
    position = 0
    for etiquette in utiles:
        nombre = len(par_etiquette[etiquette])
        centroides[etiquette] = _moyenne(vecteurs[position:position + nombre])
        position += nombre
    resultat = []
    for vecteur in vecteurs[len(exemples):]:
        scores = {e: max(0.0, cosinus(vecteur, c)) for e, c in centroides.items()}
        label = max(scores, key=scores.get)
        resultat.append({'label': label, 'score': scores[label], 'scores': scores})
    return '__id__', (resultat, 'centroide:' + moteur)


def _classer_llm(textes, etiquettes, par_etiquette):
    """Classement par le LLM configure, sortie contrainte."""
    ok, raison = _llm_disponible()
    if not ok:
        raise Indisponible(raison)
    taille = seuils.get('llm.classer.taille_lot')
    apercu = {e: liste[:5] for e, liste in par_etiquette.items()}
    resultat = []
    for debut in range(0, len(textes), taille):
        lot = textes[debut:debut + taille]
        payload = {
            'mode': 'chaine:classer',
            'etiquettes': etiquettes,
            'exemples': apercu,
            'textes': [{'id': i, 'texte': t} for i, t in enumerate(lot)],
        }
        sortie = noyau.llm(TACHE_CLASSER, payload)
        reponses = {}
        for element in sortie['resultats']:
            if 0 <= element['id'] < len(lot) and element['id'] not in reponses:
                score = min(1.0, max(0.0, float(element['score'])))
                reponses[element['id']] = {'label': element['label'], 'score': score, 'scores': {element['label']: score}}
        if len(reponses) != len(lot):
            _lever('Classement par LLM : reponses manquantes.')
        resultat.extend(reponses[i] for i in range(len(lot)))
    return resultat


def classer_lexical(textes, par_etiquette):
    """Plus proche centroide sur des vecteurs tf-idf (sans dependance)."""
    utiles = [e for e, liste in par_etiquette.items() if liste]
    if not utiles:
        raise Indisponible('aucun exemple')
    documents = [mots(t) for e in utiles for t in par_etiquette[e]] + [mots(t) for t in textes]
    total = len(documents)
    frequences = {}
    for document in documents:
        for mot in set(document):
            frequences[mot] = frequences.get(mot, 0) + 1
    idf = {mot: math.log((total + 1.0) / (n + 1.0)) + 1.0 for mot, n in frequences.items()}

    def vecteur(liste):
        compte = {}
        for mot in liste:
            compte[mot] = compte.get(mot, 0) + 1
        return {mot: n * idf[mot] for mot, n in compte.items()}
    centroides = {}
    for etiquette in utiles:
        somme = {}
        for texte in par_etiquette[etiquette]:
            v = _normer(vecteur(mots(texte)))
            for mot, valeur in v.items():
                somme[mot] = somme.get(mot, 0.0) + valeur
        centroides[etiquette] = _normer(somme)
    resultat = []
    for texte in textes:
        v = _normer(vecteur(mots(texte)))
        scores = {e: sum(valeur * c.get(mot, 0.0) for mot, valeur in v.items()) for e, c in centroides.items()}
        label = max(utiles, key=lambda e: (scores[e], len(par_etiquette[e])))
        resultat.append({'label': label, 'score': scores[label], 'scores': scores})
    return resultat


def _normer(vecteur):
    """Normalise un vecteur creux (dict)."""
    norme = math.sqrt(sum(v * v for v in vecteur.values()))
    if not norme:
        return {}
    return {k: v / norme for k, v in vecteur.items()}


def _moyenne(vecteurs):
    """Moyenne de vecteurs denses."""
    taille = len(vecteurs[0])
    return [sum(v[i] for v in vecteurs) / len(vecteurs) for i in range(taille)]


# ---------------------------------------------------------------------------
# Etat
# ---------------------------------------------------------------------------

def etat():
    """Pour chaque fonction : moteur actif, moteurs disponibles, modele, version, raison du repli.

    Ne charge aucun modele : la disponibilite est estimee (paquet installe,
    modele spaCy installe, modele present dans le cache Hugging Face si
    ``huggingface_hub`` est installe, LLM configure) et completee par les
    echecs de chargement deja constates.
    """
    resultat = {}
    for nom, liste in NIVEAUX.items():
        reglage = mode(nom)
        autorises = candidats(nom)
        disponibles = []
        raisons = []
        actif = None
        actif_candidat = None
        for candidat in liste:
            ok, raison = _verifier(nom, candidat)
            if ok:
                disponibles.append(_identifiant(nom, candidat))
            if candidat not in autorises:
                raisons.append(candidat[1] + ' : exclu par le reglage moteur:' + nom + '=' + reglage)
                continue
            if ok and actif is None:
                actif = _identifiant(nom, candidat)
                actif_candidat = candidat
            elif not ok and actif is None:
                raisons.append(candidat[1] + ' : ' + raison)
        premier = liste[0]
        cle_modele = (actif_candidat or premier)[3]
        paquet = (actif_candidat or premier)[2]
        resultat[nom] = {
            'actif': actif or 'aucun',
            'disponible': disponibles,
            'modele': modele(cle_modele) if cle_modele else None,
            'version': _version(paquet) if paquet else '',
            'raison_repli': '' if actif_candidat == premier else ' ; '.join(raisons),
            'mode': reglage,
            'charge': _est_charge(nom, actif_candidat),
        }
    # Double passe : le NLI peut-il controler les sorties du LLM ? (forte : NLI distinct du LLM)
    actif_nli = resultat['nli']['actif']
    if actif_nli == 'aucun':
        resultat['nli']['double_passe'] = 'impossible'
    elif _est_llm(actif_nli):
        resultat['nli']['double_passe'] = 'faible' if nli_par_llm_autorise() else 'refusee'
    else:
        resultat['nli']['double_passe'] = 'forte'
    return resultat


def _est_charge(nom, candidat):
    """Vrai si le modele du moteur actif est deja en memoire."""
    if candidat is None:
        return False
    if candidat[1] == 'spacy':
        return ('spacy', 'spacy') in _CHARGES
    return any(cle[0] == nom and cle[1].split(':')[0] == candidat[1] for cle in _CHARGES)


def _sans_accents(texte):
    """Minuscules sans diacritiques."""
    decompose = unicodedata.normalize('NFD', (texte or '').casefold())
    return ''.join(c for c in decompose if not unicodedata.combining(c))
