"""M1 etapes 4 et 5 : unites d'information puis faits atomiques.

Document (docs/METHODOLOGIES.txt, M1) : « L'unite d'information est une
proposition autoportante en langue naturelle. Le fait atomique est un triplet
normalise tire de cette unite. Le LLM local n'intervient qu'au niveau de
l'unite, encadre par un ancrage obligatoire et une verification NLI. »

Etape 4 - ``unites(version_id, option)`` :
  A. regles syntaxiques sur l'arbre de dependances spaCy (texte recopie) ;
     repli sans spaCy : une unite par phrase, coordinations de propositions
     decoupees par regles prudentes ;
  B. propositionizer seq2seq (Dense X Retrieval), import paresseux ;
  C. LLM local contraint par schema JSON (consigne figee, prompt versionne) ;
  D. LLM tiers, meme consigne, reserve au jeu de reference (n'ecrit rien).
  Chaque unite : span verifie dans le texte source, puis NLI « le texte
  implique-t-il l'unite ? » ; sinon rejet compte.

Etape 5 - ``faits(unit_ids)`` : entites (``moteurs.entites``), canonicalisation
par alias (``entities``, ``unit_entities``), Wikidata optionnel ; relations par
extracteur non generatif (GLiREL) ou LLM local contraint a la liste fermee
(``chaine.predicats``). Origine ``explicite`` ou ``inference_contextuelle``
uniquement : connaissance externe et informations inconnues vont dans
``hypotheses``. Garde-fou : verbalisation + NLI (premisse = unite) + entites
presentes dans le span ; les faits rejetes sont traces (``fait_rejete``).
"""
import difflib
import json
import re
import unicodedata
import urllib.parse
import urllib.request

from chaine import moteurs
from chaine import noyau
from chaine import predicats
from chaine import seuils

OPTIONS = ('auto', 'A', 'B', 'C', 'D')
PROMPT_UNITES = 'm1-unites-c-v1'
PROMPT_FAITS = 'm1-faits-v1'
ORIGINES_FAITS = ('explicite', 'inference_contextuelle')
ORIGINES_LLM = ('explicite', 'inference_contextuelle', 'connaissance_externe')
TYPES_ENTITE_OBJET = ('entite', 'personne', 'organisation', 'lieu', 'produit', 'loi')
TYPES_NUMERIQUES = ('date', 'montant', 'pourcentage', 'nombre', 'duree', 'periode')
KINDS_IGNORES = ('titre',)


# --- Consignes figees (versionnees par PROMPT_UNITES / PROMPT_FAITS) ---------
CONSIGNE_UNITES = (
    "Decoupe chaque PASSAGE en unites d'information. Consigne figee :\n"
    "1. une unite = un seul enonce verifiable ;\n"
    "2. l'unite est autoportante : sujet explicite (aucun pronom ni anaphore comme « il », « cette mesure » : "
    "reprends le groupe nominal du passage ou du CONTEXTE) et cadre temporel explicite (date ou periode tiree "
    "du passage ou du CONTEXTE, chaine vide s'il n'y en a pas) ;\n"
    "3. elle reprend les mots du texte ;\n"
    "4. elle cite son span : « citation » est la sous-chaine EXACTE du passage, recopiee caractere pour "
    "caractere, qui porte l'enonce ;\n"
    "5. elle ne contient aucun ajout de connaissance : rien qui ne soit ecrit dans le passage ou le CONTEXTE.\n"
    "Ignore les metadonnees d'usage (temps de lecture, partage, sommaire, « a lire aussi »). "
    "« passage » = id du passage d'ou vient l'unite."
)
CONSIGNE_FAITS = (
    "Pour chaque UNITE, extrais des faits atomiques (sujet, predicat, objet). Le predicat est OBLIGATOIREMENT "
    "un nom de la liste PREDICATS. origine : « explicite » si le triplet est ecrit dans l'unite ; "
    "« inference_contextuelle » s'il decoule de l'unite seule sans aucune connaissance exterieure ; "
    "« connaissance_externe » si tu le sais par ailleurs mais que l'unite ne le dit pas (il ne sera jamais "
    "traite comme un fait). citation = sous-chaine exacte de l'unite qui porte le fait (vide pour "
    "connaissance_externe). portee = lieu ou perimetre, periode = date ou periode, tels qu'ecrits (sinon vide). "
    "inconnues = informations absentes de l'unite et necessaires pour la verifier (dates precises, lieux, "
    "acteurs), au plus deux, formulees comme des questions courtes."
)

_STOP = {
    'le', 'la', 'les', 'l', 'un', 'une', 'des', 'du', 'de', 'd', 'et', 'ou', 'a', 'au', 'aux', 'en', 'dans', 'par',
    'pour', 'sur', 'sous', 'avec', 'sans', 'que', 'qui', 'quoi', 'dont', 'ce', 'cet', 'cette', 'ces', 'se', 'sa',
    'son', 'ses', 'leur', 'leurs', 'est', 'sont', 'il', 'elle', 'ils', 'elles', 'on', 'ne', 'pas', 'plus', 'y',
    'n', 's', 'c', 'qu', 'the', 'of', 'to', 'and',
}
_PRONOMS_SUJETS = {'il', 'elle', 'ils', 'elles'}
_MARQUEURS_CONTRASTE = {'tandis', 'alors'}
_TYPO = str.maketrans({'’': "'", '‘': "'", '“': '"', '”': '"', ' ': ' ', ' ': ' '})
_RE_MOT = re.compile(r"[\w\-]+", re.UNICODE)
_RE_NOMBRE = re.compile(r'\d+(?:[.,]\d+)?')
_RE_URL = re.compile(r'^\s*(?:https?://|www\.)\S+\s*$')
_VERBES_FREQUENTS = {
    'est', 'sont', 'a', 'ont', 'etait', 'etaient', 'sera', 'seront', 'fait', 'font', 'peut', 'peuvent', 'doit',
    'doivent', 'reste', 'restent', 'devient', 'deviennent', 'permet', 'permettent', 'utilise', 'utilisent', 'va',
    'vont', 'avait', 'avaient', 'serait', 'seraient', 'aurait', 'auraient', 'indique', 'decrit', 'montre',
}
_RE_VERBE = re.compile(r'\b\w+(?:ait|aient|era|eront|erait|eraient)\b')
_SEPARATEURS_REGLES = re.compile(r'\s*;\s+|,\s+(?:mais|tandis\s+qu(?:e|\')|alors\s+qu(?:e|\'))\s*', re.I)
_RE_URL_INTERNE = re.compile(r'(?:https?://|www\.)\S+')
_RE_CROCHETS = re.compile(r'\[[^\[\]\n]{0,300}\]')
_DEBUT_CONNECTEUR = re.compile(r'^(?:mais|tandis\s+qu(?:e\s+|\')|alors\s+qu(?:e\s+|\'))', re.I)


# ---------------------------------------------------------------------------
# Outils communs
# ---------------------------------------------------------------------------

def _seuil(nom):
    """Seuil de ``chaine.seuils`` (tous les seuils du module y sont declares)."""
    return seuils.get(nom)


def cle(texte):
    """Forme de comparaison : typographie unifiee, minuscules sans accents, espaces reduits."""
    texte = unicodedata.normalize('NFC', str(texte or '')).translate(_TYPO)
    decompose = unicodedata.normalize('NFD', texte.casefold())
    sans = ''.join(c for c in decompose if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', sans).strip()


def _mots_pleins(texte):
    """Mots significatifs (cle, hors mots vides)."""
    return [m for m in _RE_MOT.findall(cle(texte)) if m not in _STOP and len(m) > 1]


def _nb_mots(texte):
    """Nombre de mots."""
    return len(_RE_MOT.findall(texte or ''))


def _nombres(texte):
    """Nombres ecrits (virgule decimale unifiee)."""
    return {n.replace(',', '.') for n in _RE_NOMBRE.findall(cle(texte))}


def localiser(citation, texte):
    """Offset ``(debut, fin)`` de ``citation`` dans ``texte``, ou None.

    Reprend le rapprochement typographique du serveur (``S.source_quote``) :
    seuls espaces et typographie sont reconcilies, jamais paraphrases, nombres
    ou negations.
    """
    if not citation or not citation.strip() or not texte:
        return None
    trouvee = noyau.srv().source_quote(citation, texte)
    if trouvee is None:
        return None
    debut = texte.find(trouvee)
    if debut < 0:
        return None
    return debut, debut + len(trouvee)


def _job(**valeurs):
    """Met a jour l'etat du traitement du serveur."""
    noyau.srv().JOB.update(**valeurs)


def _proba_implication(resultat):
    """Probabilite d'implication d'un resultat de ``moteurs.nli_lot``."""
    probas = resultat.get('probas')
    if isinstance(probas, dict) and 'implication' in probas:
        return float(probas['implication'])
    if resultat.get('label') == 'implication':
        return float(resultat.get('score') or 0.0)
    return 0.0


def _arret_demande():
    """Vrai si l'utilisateur a demande l'arret."""
    arret = getattr(noyau.srv(), 'STOP', None)
    return bool(arret is not None and arret.is_set())


def _ancre(texte, version):
    """Ancre temporelle par defaut : date de la phrase, sinon mise a jour, sinon publication.

    Les dates relatives sont resolues par M1 etape 3 (lot m1_page), qui peut
    reecrire ces colonnes.
    """
    from chaine import migration
    dates = migration.lire_dates(texte)
    if dates:
        return max(iso for iso, _ in dates), 'phrase'
    mise_a_jour = migration.ancre_periode(version.get('updated'))
    if mise_a_jour:
        return mise_a_jour, 'maj'
    publication = migration.ancre_periode(version.get('published'))
    if publication:
        return publication, 'publication'
    return None, 'inconnue'


# ---------------------------------------------------------------------------
# Passages : chunks structurels (lot m1_page) ou paragraphes de la version
# ---------------------------------------------------------------------------

def _version(version_id):
    """Version et titre de sa page."""
    ligne = noyau.srv().query(
        'SELECT v.*, p.title AS page_title, p.url AS page_url FROM versions v LEFT JOIN pages p ON p.id=v.page_id WHERE v.id=?',
        (version_id,), True)
    if not ligne:
        raise KeyError('Version inconnue : ' + str(version_id))
    return dict(ligne)


def _contexte_texte(contexte):
    """Contexte lisible a partir de ``chunks.context`` (JSON du lot m1_page ou texte libre)."""
    if not contexte:
        return ''
    if isinstance(contexte, str):
        try:
            donnees = json.loads(contexte)
        except ValueError:
            return contexte
    else:
        donnees = contexte
    if not isinstance(donnees, dict):
        return str(donnees)
    morceaux = []
    for cle_ctx, libelle in (('titre', 'Titre'), ('title', 'Titre'), ('section', 'Section'), ('date', 'Date'),
                             ('published', 'Date'), ('date_page', 'Date')):
        valeur = donnees.get(cle_ctx)
        if valeur:
            morceaux.append(libelle + ' : ' + str(valeur))
    for cle_res in ('coreferences', 'resolutions', 'dates', 'expressions_temporelles', 'temps'):
        valeur = donnees.get(cle_res)
        if isinstance(valeur, list):
            for element in valeur[:20]:
                if isinstance(element, dict):
                    source = element.get('texte') or element.get('text') or element.get('expr') or ''
                    cible = element.get('antecedent') or element.get('resolu') or element.get('iso') or element.get('date') or ''
                    if source and cible:
                        morceaux.append(str(source) + ' = ' + str(cible))
    return ' ; '.join(morceaux)


def _est_titre(ligne):
    """Heuristique de titre pour le repli sans chunks structurels."""
    propre = ligne.strip()
    if not propre:
        return True
    if propre[-1] in '.!?:;»)"…':
        return False
    if propre.isupper():
        return True
    return _nb_mots(propre) <= 15


def passages(version_id):
    """Passages a decomposer : list[dict(chunk_id, start, end, text, context, kind)].

    Chunks structurels du lot m1_page (colonne ``kind`` renseignee) hors titres ;
    a defaut, paragraphes du texte de la version (blocs), avec pour contexte le
    titre de la page, le dernier titre de section et la date de publication.
    """
    serveur = noyau.srv()
    version = _version(version_id)
    texte = version.get('text') or ''
    colonnes = noyau.colonnes('chunks')
    resultat = []
    if 'kind' in colonnes:
        lignes = serveur.query(
            'SELECT * FROM chunks WHERE version_id=? AND kind IS NOT NULL ORDER BY start, position', (version_id,))
        for ligne in lignes:
            ligne = dict(ligne)
            if ligne.get('kind') in KINDS_IGNORES:
                continue
            debut = int(ligne.get('start') or 0)
            morceau = ligne.get('text') or ''
            if texte[debut:debut + len(morceau)] != morceau:
                position = localiser(morceau, texte)
                if position is None:
                    continue
                debut = position[0]
                morceau = texte[position[0]:position[1]]
            contexte = _contexte_texte(ligne.get('context'))
            if not contexte:
                contexte = _contexte_texte({'titre': version.get('page_title'), 'section': ligne.get('section'),
                                            'date': version.get('published')})
            resultat.append({'chunk_id': ligne['id'], 'start': debut, 'end': debut + len(morceau), 'text': morceau,
                             'context': contexte, 'kind': ligne.get('kind')})
        if resultat:
            return resultat
    anciens = serveur.query('SELECT id, start, text FROM chunks WHERE version_id=? ORDER BY position', (version_id,))
    bornes = [(int(c['start'] or 0), int(c['start'] or 0) + len(c['text'] or ''), c['id']) for c in anciens]
    section = ''
    for debut, fin in moteurs.blocs(texte):
        morceau = texte[debut:fin]
        if not morceau.strip() or _RE_URL.match(morceau):
            continue
        if _est_titre(morceau):
            section = morceau.strip()
            continue
        chunk_id = next((ident for d, f, ident in bornes if d <= debut < f), None)
        contexte = _contexte_texte({'titre': version.get('page_title'), 'section': section,
                                    'date': version.get('published')})
        resultat.append({'chunk_id': chunk_id, 'start': debut, 'end': fin, 'text': morceau, 'context': contexte,
                         'kind': 'paragraphe'})
    return resultat


# ---------------------------------------------------------------------------
# Option A : regles syntaxiques
# ---------------------------------------------------------------------------

def _sous_arbre(jeton):
    """Indices du sous-arbre d'un jeton spaCy."""
    return {t.i for t in jeton.subtree}


def _a_sujet(jeton):
    """Enfant sujet d'un predicat, ou None."""
    for enfant in jeton.children:
        if enfant.dep_ in ('nsubj', 'nsubj:pass', 'expl:subj', 'csubj'):
            return enfant
    return None


def _est_predicat(jeton):
    """Vrai si le jeton porte une proposition (verbe, ou adjectif/nom avec copule ou sujet)."""
    if jeton.pos_ in ('VERB', 'AUX'):
        return True
    deps = {e.dep_ for e in jeton.children}
    return bool(deps & {'cop', 'aux', 'aux:pass', 'aux:tense', 'nsubj', 'nsubj:pass'})


def _contrastive(jeton):
    """Subordonnee « tandis que » / « alors que » : deux assertions independantes."""
    for enfant in jeton.children:
        if enfant.dep_ == 'mark' and enfant.lower_ in _MARQUEURS_CONTRASTE:
            return True
    return False


def _relative_sujet(jeton):
    """Relative introduite par « qui » sujet : renvoie le jeton « qui » ou None."""
    for enfant in jeton.children:
        if enfant.dep_.startswith('nsubj') and enfant.lower_ == 'qui':
            return enfant
    return None


def _groupe_nominal(nom, exclus):
    """Indices du groupe nominal de ``nom`` sans relatives, appositions ni ponctuation."""
    indices = _sous_arbre(nom)
    for enfant in nom.children:
        if enfant.dep_ in ('acl:relcl', 'appos', 'acl', 'conj', 'cc', 'punct'):
            indices -= _sous_arbre(enfant)
    indices -= exclus
    return indices


def _texte_jetons(doc, indices):
    """Recopie les jetons dans l'ordre ; les virgules bordant un trou sont retirees."""
    tries = sorted(indices)
    gardes = []
    for position, index in enumerate(tries):
        jeton = doc[index]
        if jeton.is_punct and jeton.text in ',;:':
            precedent = tries[position - 1] if position > 0 else None
            suivant = tries[position + 1] if position + 1 < len(tries) else None
            if precedent != index - 1 or suivant != index + 1:
                continue
        gardes.append(index)
    morceaux = []
    precedent = None
    for index in gardes:
        jeton = doc[index]
        if precedent is not None and index != precedent + 1 and morceaux and not morceaux[-1].endswith(' '):
            morceaux.append(' ')
        morceaux.append(jeton.text_with_ws)
        precedent = index
    return ''.join(morceaux)


def _nettoyer(texte, point_final=True):
    """Espaces, ponctuation de bord, majuscule initiale, point final."""
    texte = re.sub(r'\s+', ' ', texte or '').strip()
    texte = re.sub(r'\s+([,.;:])', r'\1', texte)
    texte = re.sub(r'^[,;:.\-–—\s]+', '', texte)
    texte = re.sub(r'[,;:\-–—\s]+$', '', texte)
    if not texte:
        return ''
    texte = texte[0].upper() + texte[1:]
    if point_final and texte[-1] not in '.!?…»"':
        texte += '.'
    return texte


def _bornes(doc, indices, decalage):
    """Offsets (debut, fin) dans le texte source couvrant les jetons, ponctuation de bord exclue."""
    pleins = [i for i in indices if not doc[i].is_punct] or list(indices)
    debut = min(doc[i].idx for i in pleins)
    fin = max(doc[i].idx + len(doc[i].text) for i in pleins)
    return decalage + debut, decalage + fin


def _queue_orpheline(phrase, racine, extraits, retires):
    """Jetons rattaches a tort a la racine apres la derniere proposition extraite (« km/h », etc.).

    Si la derniere proposition coordonnee ou subordonnee termine la phrase,
    les jetons qui la suivent (hors ponctuation finale) lui reviennent.
    """
    propositions = [j for j, nature in extraits if nature in ('coordination', 'subordonnee')]
    if not propositions:
        return set()
    derniere = max(propositions, key=lambda j: max(_sous_arbre(j)))
    fin = max(_sous_arbre(derniere))
    if fin < racine.i:
        return set()
    queue = set()
    for jeton in phrase:
        if jeton.i <= fin or jeton.i in retires:
            continue
        if jeton.is_punct and jeton.text in '.!?…':
            continue
        queue.add(jeton.i)
    return queue


def _unites_phrase_spacy(doc, phrase, decalage, exclus=frozenset()):
    """Unites d'une phrase spaCy : propositions coordonnees, relatives « qui », appositions.

    ``exclus`` : indices de jetons ecartes (renvois de sources entre crochets,
    traites par M2).
    """
    racine = phrase.root
    dans_phrase = {t.i for t in phrase} - set(exclus)
    extraits = []
    for jeton in phrase:
        if jeton.i == racine.i:
            continue
        if jeton.dep_ == 'conj' and jeton.head.i == racine.i and _est_predicat(jeton):
            extraits.append((jeton, 'coordination'))
        elif jeton.dep_ == 'advcl' and jeton.head.i == racine.i and _contrastive(jeton):
            extraits.append((jeton, 'subordonnee'))
        elif jeton.dep_ == 'acl:relcl' and jeton.head.pos_ in ('NOUN', 'PROPN') and _relative_sujet(jeton) is not None:
            extraits.append((jeton, 'relative'))
        elif jeton.dep_ == 'appos' and jeton.head.pos_ in ('NOUN', 'PROPN'):
            extraits.append((jeton, 'apposition'))
    tetes = {j.i for j, _ in extraits}
    retires = set()
    for jeton, _ in extraits:
        retires |= _sous_arbre(jeton)
    absorbes = _queue_orpheline(phrase, racine, extraits, retires)
    retires |= absorbes
    sujet_racine = _a_sujet(racine)
    groupe_sujet = set()
    if sujet_racine is not None and sujet_racine.pos_ in ('NOUN', 'PROPN'):
        groupe_sujet = _groupe_nominal(sujet_racine, set())
    unites = []
    principal = (_sous_arbre(racine) & dans_phrase) - retires
    if principal:
        texte = _nettoyer(_texte_jetons(doc, principal))
        unites.append((texte, _bornes(doc, principal, decalage), 'principale'))
    for jeton, nature in extraits:
        propres = _sous_arbre(jeton) & dans_phrase
        for autre in tetes:
            if autre != jeton.i and autre in propres:
                propres -= _sous_arbre(doc[autre])
        if absorbes and max(absorbes) > jeton.i and min(absorbes) > max(_sous_arbre(jeton)) - 1:
            propres |= absorbes
        for enfant in jeton.children:
            if enfant.dep_ in ('cc', 'mark') and enfant.i < jeton.i:
                propres -= _sous_arbre(enfant)
        if not propres:
            continue
        bornes = _bornes(doc, propres, decalage)
        prefixe = ''
        liaison = ''
        if nature in ('coordination', 'subordonnee'):
            sujet = _a_sujet(jeton)
            if sujet is None and groupe_sujet:
                prefixe = _texte_jetons(doc, groupe_sujet)
            elif sujet is not None and sujet.lower_ in _PRONOMS_SUJETS and groupe_sujet:
                propres = propres - {sujet.i}
                prefixe = _texte_jetons(doc, groupe_sujet)
        elif nature == 'relative':
            qui = _relative_sujet(jeton)
            propres = propres - {qui.i}
            prefixe = _texte_jetons(doc, _groupe_nominal(jeton.head, retires - _sous_arbre(jeton.head)))
        elif nature == 'apposition':
            prefixe = _texte_jetons(doc, _groupe_nominal(jeton.head, set()))
            pluriel = 'Plur' in jeton.head.morph.get('Number')
            liaison = 'sont' if pluriel else 'est'
        corps = _texte_jetons(doc, propres)
        texte = _nettoyer(' '.join(p for p in (prefixe.strip(), liaison, corps.strip()) if p))
        unites.append((texte, bornes, nature))
    return unites


def _verbe_probable(texte):
    """Indice prudent de proposition conjuguee dans un fragment."""
    mots = _RE_MOT.findall(cle(texte))
    if not mots:
        return False
    if mots[0] in _PRONOMS_SUJETS | {'on', 'ce', 'cela', 'ceci'}:
        return True
    return any(m in _VERBES_FREQUENTS for m in mots) or bool(_RE_VERBE.search(cle(texte)))


def _unites_phrase_regles(texte, debut, fin):
    """Repli sans spaCy : la phrase, decoupee aux « ; », « , mais », « , tandis que », « , alors que »."""
    phrase = texte[debut:fin]
    mots_min = seuils.get('m1.unites.mots_min')
    coupes = [0]
    for match in _SEPARATEURS_REGLES.finditer(phrase):
        gauche = phrase[coupes[-1]:match.start()]
        droite = phrase[match.end():]
        subordonnant = re.search(r'tandis|alors', match.group(0), re.I) is not None
        if _nb_mots(gauche) >= mots_min and _nb_mots(droite) >= mots_min and (subordonnant or _verbe_probable(droite)):
            coupes.append(match.start())
            coupes.append(match.end())
    coupes.append(len(phrase))
    morceaux = [(coupes[i], coupes[i + 1]) for i in range(0, len(coupes) - 1, 2)]
    resultat = []
    for d, f in morceaux:
        brut = phrase[d:f]
        retrait = len(brut) - len(brut.lstrip())
        brut = brut.strip()
        connecteur = _DEBUT_CONNECTEUR.match(brut)
        decal = connecteur.end() if connecteur else 0
        corps = brut[decal:].strip()
        if not corps:
            continue
        absolu = debut + d + retrait + decal + (len(brut[decal:]) - len(brut[decal:].lstrip()))
        bornes = (absolu, absolu + len(corps.rstrip(' .;,')))
        nature = 'phrase' if len(morceaux) == 1 else 'coordination'
        resultat.append((_nettoyer(corps), bornes, nature))
    return resultat


def _candidats_a(version, liste_passages):
    """Option A sur tous les passages : candidats et moteur utilise."""
    texte = version.get('text') or ''
    candidats = []
    doc_essai = moteurs.analyse('Test.') if liste_passages else None
    moteur = moteurs.dernier('analyse') if doc_essai is not None else None
    for index, passage in enumerate(liste_passages):
        noyau.srv().checkpoint()
        if moteur:
            doc = moteurs.analyse(passage['text'])
            crochets = [(c.start(), c.end()) for c in _RE_CROCHETS.finditer(passage['text'])]
            exclus = {t.i for t in doc if any(d <= t.idx < f for d, f in crochets)}
            for phrase in doc.sents:
                if not ({t.i for t in phrase} - exclus):
                    continue
                for texte_unite, bornes, nature in _unites_phrase_spacy(doc, phrase, passage['start'], exclus):
                    candidats.append({'passage': index, 'texte': texte_unite, 'debut': bornes[0], 'fin': bornes[1],
                                      'nature': nature})
        else:
            offsets = moteurs.phrases_regles(passage['text'])
            for d, f in offsets:
                for texte_unite, bornes, nature in _unites_phrase_regles(texte, passage['start'] + d, passage['start'] + f):
                    candidats.append({'passage': index, 'texte': texte_unite, 'debut': bornes[0], 'fin': bornes[1],
                                      'nature': nature})
    gardes = []
    for candidat in candidats:
        candidat['citation'] = texte[candidat['debut']:candidat['fin']]
        sans_crochets = _RE_CROCHETS.sub('', candidat['citation'])
        sans_url = _RE_URL_INTERNE.sub('', sans_crochets)
        if len(sans_url.strip()) * 2 < len(candidat['citation'].strip()):
            continue
        if _nb_mots(sans_crochets) * 2 < _nb_mots(candidat['citation']):
            continue
        gardes.append(candidat)
    candidats = gardes
    identifiant = 'A:' + (moteur or 'regles')
    return candidats, identifiant


# ---------------------------------------------------------------------------
# Option C (et D) : LLM contraint
# ---------------------------------------------------------------------------

SCHEMA_UNITES = noyau.schema('chaine:unites', noyau.objet({
    'unites': noyau.tableau(noyau.objet({
        'passage': {'type': 'integer'},
        'texte': {'type': 'string'},
        'citation': {'type': 'string'},
        'sujet': {'type': 'string'},
        'cadre_temporel': {'type': 'string'},
    })),
}))


def _lots_passages(liste_passages):
    """Groupes de passages consecutifs d'au plus ``m1.unites.lot_caracteres`` caracteres."""
    limite = _seuil('m1.unites.lot_caracteres')
    lots = []
    courant = []
    taille = 0
    for index, passage in enumerate(liste_passages):
        longueur = len(passage['text'])
        if courant and taille + longueur > limite:
            lots.append(courant)
            courant = []
            taille = 0
        courant.append(index)
        taille += longueur
    if courant:
        lots.append(courant)
    return lots


def _payload_unites(version, liste_passages, lot):
    """Charge utile d'un appel (consigne figee dans la tache, donnees dans le payload)."""
    return {
        'mode': 'chaine:unites',
        'prompt': PROMPT_UNITES,
        'contexte': {'titre': version.get('page_title') or '', 'date_publication': version.get('published') or ''},
        'passages': [{'id': i, 'contexte': liste_passages[i]['context'], 'texte': liste_passages[i]['text']} for i in lot],
    }


def _candidats_llm(version, liste_passages, tiers=False):
    """Options C (LLM local) et D (LLM tiers) : candidats et moteur."""
    serveur = noyau.srv()
    candidats = []
    if tiers:
        fournisseur = noyau.reglage('modele:llm_tiers_fournisseur', 'openai')
        modele_tiers = noyau.reglage('modele:llm_tiers', serveur.CONFIG.get('review_model') or serveur.CONFIG.get('model'))
        identifiant = 'D:llm:' + str(fournisseur) + ':' + str(modele_tiers)
    else:
        identifiant = 'C:' + noyau.moteur_llm()
    for lot in _lots_passages(liste_passages):
        serveur.checkpoint()
        payload = _payload_unites(version, liste_passages, lot)
        if tiers:
            sortie = serveur.llm(CONSIGNE_UNITES, payload, provider=fournisseur, model=modele_tiers)
            problemes = noyau.valider(sortie, noyau.schema_pour(payload))
            if problemes:
                raise serveur.OperationalError('Sortie LLM tiers non conforme : ' + '; '.join(problemes[:5]))
        else:
            sortie = noyau.llm(CONSIGNE_UNITES, payload)
        for element in sortie['unites']:
            if element['passage'] not in lot:
                candidats.append({'passage': None, 'texte': element['texte'], 'citation': element['citation'],
                                  'rejet': 'passage_inconnu'})
                continue
            candidats.append({'passage': element['passage'], 'texte': element['texte'].strip(),
                              'citation': element['citation'], 'cadre_temporel': element['cadre_temporel'],
                              'nature': 'llm'})
    return candidats, identifiant


# ---------------------------------------------------------------------------
# Option B : propositionizer seq2seq (non executable sans le modele)
# ---------------------------------------------------------------------------

def _candidats_b(version, liste_passages):
    """Propositionizer type Flan-T5 (Dense X Retrieval) ; span = phrase la plus proche."""
    try:
        import transformers  # noqa: F401  import paresseux
    except ImportError as erreur:
        raise noyau.srv().OperationalError('Option B indisponible : transformers non installe (' + str(erreur) + ').')
    nom = noyau.reglage('modele:propositionizer', 'chentong00/propositionizer-wiki-flan-t5-large')
    tokenizer = transformers.AutoTokenizer.from_pretrained(nom)
    reseau = transformers.AutoModelForSeq2SeqLM.from_pretrained(nom)
    texte = version.get('text') or ''
    candidats = []
    for index, passage in enumerate(liste_passages):
        noyau.srv().checkpoint()
        entree = 'Title: ' + (version.get('page_title') or '') + '. Section: ' + passage['context'] + '. Content: ' + passage['text']
        encodage = tokenizer(entree, return_tensors='pt', truncation=True, max_length=512)
        sortie = reseau.generate(**encodage, max_new_tokens=512)
        brut = tokenizer.decode(sortie[0], skip_special_tokens=True)
        try:
            propositions = json.loads(brut)
        except ValueError:
            propositions = [p for p in brut.split('", "') if p.strip()]
        phrases = moteurs.phrases(passage['text'])
        for proposition in propositions:
            proposition = str(proposition).strip(' "[]')
            mots = set(_mots_pleins(proposition))
            meilleure = max(phrases, key=lambda o: len(mots & set(_mots_pleins(passage['text'][o[0]:o[1]]))), default=None)
            if meilleure is None:
                continue
            debut = passage['start'] + meilleure[0]
            fin = passage['start'] + meilleure[1]
            candidats.append({'passage': index, 'texte': proposition, 'citation': texte[debut:fin], 'nature': 'propositionizer'})
    return candidats, 'B:' + nom


# ---------------------------------------------------------------------------
# Verification des candidats : span, mots du texte, NLI
# ---------------------------------------------------------------------------

def _premisse(passage, debut_relatif, fin_relatif):
    """Contexte + fenetre du passage autour du span (limite du modele NLI)."""
    largeur = _seuil('m1.unites.premisse_caracteres')
    texte = passage['text']
    if len(texte) > largeur:
        marge = max(0, (largeur - (fin_relatif - debut_relatif)) // 2)
        gauche = max(0, debut_relatif - marge)
        texte = texte[gauche:gauche + largeur]
    if passage.get('context'):
        return passage['context'] + '\n' + texte
    return texte


def _verifier(version, liste_passages, candidats, generatif, compteurs, controle_llm=False):
    """Applique ancrage, recouvrement lexical et NLI ; renvoie les unites retenues.

    ``controle_llm`` : les candidats viennent du LLM ; le NLI doit en etre distinct (``moteurs.constater_double_passe``).
    """
    texte = version.get('text') or ''
    mots_min = seuils.get('m1.unites.mots_min')
    mots_max = seuils.get('m1.unites.mots_max')
    recouvrement_min = seuils.get('m1.unites.span_recouvrement_min')
    retenues = []
    vues = set()
    for candidat in candidats:
        if candidat.get('rejet'):
            compteurs[candidat['rejet']] = compteurs.get(candidat['rejet'], 0) + 1
            continue
        passage = liste_passages[candidat['passage']]
        if 'debut' in candidat:
            bornes = (candidat['debut'], candidat['fin'])
        else:
            relatif = localiser(candidat['citation'], passage['text'])
            bornes = None if relatif is None else (passage['start'] + relatif[0], passage['start'] + relatif[1])
        if bornes is None or texte[bornes[0]:bornes[1]] == '':
            compteurs['rejet_span'] += 1
            continue
        unite_texte = candidat['texte']
        nombre_mots = _nb_mots(unite_texte)
        if nombre_mots < mots_min:
            compteurs['rejet_trop_courte'] += 1
            continue
        if nombre_mots > mots_max:
            compteurs['trop_longues'] += 1
        if generatif:
            reference = texte[bornes[0]:bornes[1]] + ' ' + passage['text'] + ' ' + passage.get('context', '')
            if not _nombres(unite_texte) <= _nombres(reference):
                compteurs['rejet_nombre_absent'] += 1
                continue
            mots = _mots_pleins(unite_texte)
            presents = set(_mots_pleins(reference))
            part = sum(1 for m in mots if m in presents) / len(mots) if mots else 1.0
            if part < recouvrement_min:
                compteurs['rejet_mots_ajoutes'] += 1
                continue
        cle_unite = cle(unite_texte)
        if cle_unite in vues:
            compteurs['doublons'] += 1
            continue
        vues.add(cle_unite)
        retenues.append({'passage': passage, 'texte': unite_texte, 'debut': bornes[0], 'fin': bornes[1],
                         'citation': texte[bornes[0]:bornes[1]], 'nature': candidat.get('nature', ''),
                         'cadre_temporel': candidat.get('cadre_temporel', '')})
    if not retenues:
        return retenues, None
    paires = [(_premisse(u['passage'], u['debut'] - u['passage']['start'], u['fin'] - u['passage']['start']), u['texte'])
              for u in retenues]
    try:
        resultats = moteurs.nli_lot(paires)
    except noyau.srv().OperationalError:
        if _arret_demande() or generatif:
            raise
        compteurs['nli_indisponible'] += len(retenues)
        for unite in retenues:
            unite['nli'] = None
        return retenues, 'indisponible'
    if controle_llm:
        moteurs.constater_double_passe(resultats)
    seuil = seuils.get('nli.implication')
    gardees = []
    for unite, resultat in zip(retenues, resultats):
        score = _proba_implication(resultat)
        if score < seuil:
            compteurs['rejet_nli'] += 1
            continue
        unite['nli'] = score
        gardees.append(unite)
    return gardees, resultats[0].get('engine') if resultats else None


# ---------------------------------------------------------------------------
# Etape 4 : unites
# ---------------------------------------------------------------------------

def choisir_option(option='auto'):
    """Option effective : reglage ``m1:unites:option`` pour ``auto``, sinon C si un LLM est configure, sinon A."""
    if option not in OPTIONS:
        raise ValueError('Option inconnue : ' + repr(option) + ' (A, B, C, D ou auto)')
    if option != 'auto':
        return option
    regle = noyau.reglage('m1:unites:option', None)
    if regle in ('A', 'B', 'C'):
        return regle
    ok, _ = moteurs._llm_disponible()
    return 'C' if ok else 'A'


def _compteurs_vides():
    """Compteurs d'une extraction d'unites."""
    return {'versions': 0, 'versions_ignorees': 0, 'versions_en_echec': 0, 'passages': 0, 'candidats': 0, 'unites': 0,
            'rejet_span': 0, 'rejet_trop_courte': 0, 'rejet_mots_ajoutes': 0, 'rejet_nombre_absent': 0,
            'rejet_nli': 0, 'doublons': 0, 'trop_longues': 0, 'nli_indisponible': 0, 'passage_inconnu': 0}


def unites(version_id, option='auto', forcer=False, reference=False, run=None, compteurs=None):
    """Extrait les unites d'information d'une version (M1 etape 4).

    ``option`` : ``A`` regles syntaxiques (base de comparaison), ``B``
    propositionizer, ``C`` LLM local contraint (production), ``D`` LLM tiers
    (``reference=True`` obligatoire : renvoie les unites sans rien ecrire, pour
    le jeu de reference), ``auto`` (voir ``choisir_option``).
    Une version qui a deja des unites (migrees ou extraites) n'est pas
    re-extraite sauf ``forcer=True`` ; ``forcer`` remplace alors les unites
    extraites par ce module (jamais les unites migrees).
    Renvoie un dict de compteurs (+ ``unit_ids``, ``extracteur``).
    """
    serveur = noyau.srv()
    effective = choisir_option(option)
    if effective == 'D' and not reference:
        raise ValueError("L'option D (LLM tiers) est reservee au jeu de reference : passer reference=True.")
    if run is None:
        moteur = {'A': 'regles+spacy', 'B': 'propositionizer', 'C': 'llm', 'D': 'llm_tiers'}[effective]
        with noyau.etape('m1.unites', moteurs={'unites': moteur}, prompt=PROMPT_UNITES if effective in 'CD' else '',
                         seuils=['m1.unites.mots_min', 'm1.unites.mots_max', 'm1.unites.span_recouvrement_min',
                                 'nli.implication'], pages=1) as execution:
            resultat = unites(version_id, effective, forcer, reference, execution, compteurs)
            execution.items = resultat['unites']
            return resultat
    compteurs = compteurs if compteurs is not None else _compteurs_vides()
    existantes = serveur.query('SELECT extractor, COUNT(*) n FROM units WHERE version_id=? GROUP BY extractor', (version_id,))
    migrees = sum(l['n'] for l in existantes if l['extractor'] == 'migration')
    autres = sum(l['n'] for l in existantes if l['extractor'] != 'migration')
    if effective != 'D' and (migrees or autres) and not forcer:
        compteurs['versions_ignorees'] += 1
        compteurs.setdefault('unit_ids', [])
        return compteurs
    version = _version(version_id)
    liste_passages = passages(version_id)
    compteurs['passages'] += len(liste_passages)
    if effective == 'A':
        candidats, extracteur = _candidats_a(version, liste_passages)
    elif effective == 'B':
        candidats, extracteur = _candidats_b(version, liste_passages)
    else:
        # Double passe : les unites du LLM ne sont acceptees que si un NLI distinct du LLM peut les controler.
        refus = moteurs.raison_double_passe()
        if refus:
            compteurs['versions_en_echec'] += 1
            erreurs = compteurs.setdefault('erreurs', [])
            if len(erreurs) < 5:
                erreurs.append(refus)
            compteurs.setdefault('unit_ids', [])
            return compteurs
        try:
            candidats, extracteur = _candidats_llm(version, liste_passages, tiers=(effective == 'D'))
        except serveur.OperationalError as erreur:
            if _arret_demande():
                raise
            compteurs['versions_en_echec'] += 1
            erreurs = compteurs.setdefault('erreurs', [])
            if len(erreurs) < 5:
                erreurs.append(str(erreur)[:300])
            compteurs.setdefault('unit_ids', [])
            return compteurs
    compteurs['candidats'] += len(candidats)
    retenues, moteur_nli = _verifier(version, liste_passages, candidats, effective != 'A', compteurs,
                                     controle_llm=effective in ('C', 'D'))
    run.moteur('unites', extracteur)
    if moteur_nli:
        run.moteur('nli', moteur_nli)
    compteurs['extracteur'] = extracteur
    if effective == 'D':
        compteurs['reference'] = [{'texte': u['texte'], 'citation': u['citation'], 'span_start': u['debut'],
                                   'span_end': u['fin'], 'nli_score': u.get('nli')} for u in retenues]
        compteurs['unites'] += len(retenues)
        compteurs['versions'] += 1
        compteurs.setdefault('unit_ids', [])
        return compteurs
    horodatage = serveur.now()
    nouveaux = []
    with noyau.connexion() as base:
        if forcer and autres:
            _supprimer_unites(base, version_id)
        for unite in retenues:
            ident = serveur.uid()
            ancre, type_ancre = _ancre(unite['texte'] + ' ' + (unite.get('cadre_temporel') or ''), version)
            base.execute(
                'INSERT INTO units(id, version_id, chunk_id, text, quote, span_start, span_end, anchor_date, anchor_kind, '
                'extractor, nli_score, run_id, created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (ident, version_id, unite['passage']['chunk_id'], unite['texte'], unite['citation'], unite['debut'],
                 unite['fin'], ancre, type_ancre, extracteur, unite.get('nli'), run.id, horodatage))
            nouveaux.append(ident)
    compteurs['unites'] += len(nouveaux)
    compteurs['versions'] += 1
    compteurs.setdefault('unit_ids', []).extend(nouveaux)
    return compteurs


def _supprimer_unites(base, version_id):
    """Supprime les unites extraites par ce module (pas les migrees) et ce qui en depend."""
    selection = "SELECT id FROM units WHERE version_id=? AND COALESCE(extractor,'')<>'migration'"
    for table in ('facts', 'hypotheses', 'unit_entities'):
        base.execute(f'DELETE FROM {table} WHERE unit_id IN ({selection})', (version_id,))
    base.execute("DELETE FROM units WHERE version_id=? AND COALESCE(extractor,'')<>'migration'", (version_id,))


# ---------------------------------------------------------------------------
# Etape 5 : entites
# ---------------------------------------------------------------------------

_ARTICLES = re.compile(r"^(?:le|la|les|l'|un|une|des|du|de la|de l'|d')\s*", re.I)


def cle_entite(nom):
    """Cle de canonicalisation : cle() sans article initial ni ponctuation de bord."""
    valeur = _ARTICLES.sub('', cle(nom))
    return valeur.strip(' .,;:!?«»"\'()[]')


class Resolveur:
    """Canonicalisation des entites par alias, en memoire (une lecture de ``entities`` par execution)."""

    def __init__(self):
        self.par_cle = {}
        self.par_prefixe = {}
        self.entites = {}
        self.nouvelles = {}
        self.alias_modifies = set()
        self.creees = 0
        for ligne in noyau.srv().query('SELECT id, name, type, aliases FROM entities'):
            try:
                alias = json.loads(ligne['aliases'] or '[]')
            except ValueError:
                alias = []
            self._indexer(ligne['id'], ligne['name'], ligne['type'], alias)

    def _indexer(self, ident, nom, type_entite, alias):
        """Enregistre une entite et ses alias dans les index."""
        self.entites[ident] = {'id': ident, 'name': nom, 'type': type_entite, 'aliases': list(alias)}
        for valeur in [nom] + list(alias):
            k = cle_entite(valeur)
            if k:
                self.par_cle.setdefault(k, ident)
                self.par_prefixe.setdefault(k[:4], set()).add(k)

    def resoudre(self, nom, type_entite='entite'):
        """Identifiant canonique de ``nom`` (creee si absente) ; None si nom vide."""
        k = cle_entite(nom)
        if not k:
            return None
        if k in self.par_cle:
            return self.par_cle[k]
        seuil = seuils.get('m1.entites.alias_similarite')
        meilleur = None
        for candidat in self.par_prefixe.get(k[:4], ()):
            ratio = difflib.SequenceMatcher(None, k, candidat).ratio()
            if ratio >= seuil and (meilleur is None or ratio > meilleur[0]):
                meilleur = (ratio, candidat)
        if meilleur is not None:
            ident = self.par_cle[meilleur[1]]
            entite = self.entites[ident]
            entite['aliases'].append(str(nom).strip())
            self.alias_modifies.add(ident)
            self.par_cle[k] = ident
            self.par_prefixe.setdefault(k[:4], set()).add(k)
            return ident
        ident = noyau.srv().uid()
        self._indexer(ident, str(nom).strip(), type_entite or 'entite', [])
        self.nouvelles[ident] = self.entites[ident]
        return ident

    def nom(self, ident):
        """Nom canonique d'une entite."""
        entite = self.entites.get(ident)
        return entite['name'] if entite else None

    def ecrire(self, base):
        """Ecrit les entites nouvelles et les alias ajoutes."""
        horodatage = noyau.srv().now()
        self.creees += len(self.nouvelles)
        for ident, entite in self.nouvelles.items():
            base.execute('INSERT OR IGNORE INTO entities(id, name, type, aliases, external_id, created) VALUES(?,?,?,?,?,?)',
                         (ident, entite['name'], entite['type'], json.dumps(entite['aliases'], ensure_ascii=False),
                          None, horodatage))
        for ident in self.alias_modifies - set(self.nouvelles):
            entite = self.entites[ident]
            base.execute('UPDATE entities SET aliases=? WHERE id=?',
                         (json.dumps(sorted(set(entite['aliases'])), ensure_ascii=False), ident))
        self.nouvelles = {}
        self.alias_modifies = set()


def rattacher_wikidata(entity_ids, langue='fr'):
    """Rattachement optionnel a Wikidata (API publique wbsearchentities).

    Desactive par defaut (``faits(..., wikidata=False)`` ou reglage
    ``m1:entites:wikidata`` = ``1``) ; jamais appele dans les tests. Ne remplit
    que ``entities.external_id`` vide, avec le premier resultat. Renvoie le
    nombre d'entites rattachees.
    """
    serveur = noyau.srv()
    rattachees = 0
    for ident in entity_ids:
        ligne = serveur.query('SELECT name, external_id FROM entities WHERE id=?', (ident,), True)
        if not ligne or ligne['external_id']:
            continue
        parametres = urllib.parse.urlencode({'action': 'wbsearchentities', 'search': ligne['name'], 'language': langue,
                                             'format': 'json', 'limit': 1})
        requete = urllib.request.Request('https://www.wikidata.org/w/api.php?' + parametres,
                                         headers={'User-Agent': 'verificateur-obsolescence/1.0'})
        try:
            with urllib.request.urlopen(requete, timeout=10) as reponse:
                donnees = json.loads(reponse.read().decode('utf-8'))
        except (OSError, ValueError):
            continue
        resultats = donnees.get('search') or []
        if resultats:
            serveur.query('UPDATE entities SET external_id=? WHERE id=?', ('wikidata:' + resultats[0]['id'], ident))
            rattachees += 1
    return rattachees


# ---------------------------------------------------------------------------
# Etape 5 : relations
# ---------------------------------------------------------------------------

def _schema_faits(payload):
    """Schema strict des faits ; le predicat est un enum de la liste fermee (relue a chaque appel)."""
    noms = predicats.noms()
    return noyau.objet({
        'unites': noyau.tableau(noyau.objet({
            'unite': {'type': 'integer'},
            'faits': noyau.tableau(noyau.objet({
                'sujet': {'type': 'string'},
                'predicat': noyau.enum(noms),
                'objet': {'type': 'string'},
                'portee': {'type': 'string'},
                'periode': {'type': 'string'},
                'origine': noyau.enum(ORIGINES_LLM),
                'citation': {'type': 'string'},
            })),
            'inconnues': noyau.tableau(noyau.objet({
                'sujet': {'type': 'string'},
                'predicat': noyau.enum(noms),
                'question': {'type': 'string'},
            })),
        })),
    })


noyau.schema('chaine:faits', _schema_faits)


def choisir_extracteur():
    """Extracteur de relations : reglage ``moteur:relations`` (auto|local|llm|regles).

    ``auto``/``local`` : GLiREL s'il est installe, sinon LLM local contraint ;
    ``llm`` : LLM ; sinon ``aucun``.
    """
    reglage = noyau.reglage('moteur:relations', 'auto')
    if reglage in ('auto', 'local') and moteurs.paquet_present('glirel'):
        return 'glirel'
    ok, _ = moteurs._llm_disponible()
    if reglage != 'regles' and ok:
        return 'llm'
    return 'aucun'


def _relations_llm(lot):
    """Un appel LLM pour un lot d'unites ; renvoie {index: {'faits': [...], 'inconnues': [...]}}."""
    payload = {
        'mode': 'chaine:faits',
        'prompt': PROMPT_FAITS,
        'predicats': predicats.pour_prompt(),
        'unites': [{'id': i, 'texte': u['text'], 'citation': u.get('quote') or ''} for i, u in enumerate(lot)],
    }
    sortie = noyau.llm(CONSIGNE_FAITS, payload)
    reponses = {}
    for element in sortie['unites']:
        index = element['unite']
        if 0 <= index < len(lot) and index not in reponses:
            reponses[index] = {'faits': element['faits'], 'inconnues': element['inconnues']}
    return reponses


def _relations_glirel(lot, entites_par_unite):
    """GLiREL (zero-shot, non generatif) : relations entre entites detectees, etiquettes = liste fermee."""
    import glirel  # noqa: F401  import paresseux ; leve ImportError si absent
    from glirel import GLiREL
    nom = noyau.reglage('modele:relations', 'jackboyla/glirel-large-v0')
    modele_rel = moteurs._charger(('relations', 'glirel'), lambda: GLiREL.from_pretrained(nom))
    libelles = {p['libelle']: p['nom'] for p in predicats.tous()}
    seuil = seuils.get('m1.entites.score_min')
    reponses = {}
    for index, unite in enumerate(lot):
        texte = unite['text']
        jetons = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r'\S+', texte)]
        spans = []
        for entite in entites_par_unite.get(unite['id'], []):
            debut = next((i for i, j in enumerate(jetons) if j[1] > entite['start']), None)
            fin = next((i for i, j in enumerate(jetons) if j[1] >= entite['end']), None)
            if debut is not None and fin is not None:
                spans.append([debut, fin, entite['type'], entite['text']])
        faits_unite = []
        if len(spans) >= 2:
            relations = modele_rel.predict_relations([j[2] for j in jetons], list(libelles), threshold=seuil, ner=spans, top_k=5)
            for relation in relations:
                tete = ' '.join(relation['head_text']) if isinstance(relation['head_text'], list) else relation['head_text']
                queue = ' '.join(relation['tail_text']) if isinstance(relation['tail_text'], list) else relation['tail_text']
                faits_unite.append({'sujet': tete, 'predicat': libelles.get(relation['label'], relation['label']),
                                    'objet': queue, 'portee': '', 'periode': '', 'origine': 'explicite', 'citation': texte})
        reponses[index] = {'faits': faits_unite, 'inconnues': []}
    return reponses


# ---------------------------------------------------------------------------
# Etape 5 : verbalisation et garde-fou
# ---------------------------------------------------------------------------

def verbaliser(fait):
    """Phrase du fait d'apres le patron du predicat (+ periode si elle n'est pas deja dans l'objet)."""
    phrase = predicats.verbaliser(fait['predicat'], fait.get('sujet'), fait.get('objet'))
    periode = (fait.get('periode') or '').strip()
    if periode and cle(periode) not in cle(phrase):
        phrase = phrase.rstrip('.') + ', ' + periode
    if not phrase.endswith('.'):
        phrase += '.'
    return phrase


def _present(valeur, reference_cle, reference_nombres, type_objet):
    """Vrai si une entite (ou valeur) du fait figure dans le span."""
    valeur_cle = cle_entite(valeur)
    if not valeur_cle:
        return True
    if type_objet in TYPES_NUMERIQUES:
        nombres = _nombres(valeur)
        if nombres:
            return nombres <= reference_nombres
    if valeur_cle in reference_cle:
        return True
    mots = [m for m in _RE_MOT.findall(valeur_cle) if m not in _STOP and len(m) > 2]
    return bool(mots) and all(m in reference_cle for m in mots)


def entites_absentes(fait, unite):
    """Entites du fait (sujet, objet entite ou valeur) absentes du span de l'unite."""
    reference = (unite.get('quote') or '') + ' ' + (unite.get('text') or '')
    reference_cle = cle(reference)
    reference_nombres = _nombres(reference)
    predicat = predicats.get(fait['predicat']) or {}
    type_objet = predicat.get('type_objet', 'texte')
    absentes = []
    if not _present(fait.get('sujet'), reference_cle, reference_nombres, 'entite'):
        absentes.append(fait.get('sujet'))
    if type_objet in TYPES_ENTITE_OBJET + TYPES_NUMERIQUES:
        if not _present(fait.get('objet'), reference_cle, reference_nombres, type_objet):
            absentes.append(fait.get('objet'))
    return absentes


def _garde_fou_lot(liste, controle_llm=False):
    """Garde-fou d'une liste de (fait, unite) ; un seul appel NLI pour les survivants.

    ``controle_llm`` : faits extraits par le LLM ; le NLI doit en etre distinct (double passe).
    """
    seuil = seuils.get('m1.faits.nli_implication')
    verifier_entites = seuils.get('m1.faits.entites_dans_span')
    resultats = []
    a_tester = []
    for fait, unite in liste:
        verdict = {'ok': False, 'raison': '', 'nli_score': None, 'verbalisation': '', 'engine': None, 'entites_absentes': []}
        if not predicats.existe(fait.get('predicat')):
            verdict['raison'] = 'predicat_hors_liste'
            resultats.append(verdict)
            continue
        verdict['verbalisation'] = verbaliser(fait)
        if verifier_entites:
            absentes = entites_absentes(fait, unite)
            if absentes:
                verdict['raison'] = 'entite_absente_du_span'
                verdict['entites_absentes'] = absentes
                resultats.append(verdict)
                continue
        a_tester.append((len(resultats), unite.get('text') or unite.get('quote') or '', verdict['verbalisation']))
        resultats.append(verdict)
    if a_tester:
        sorties = moteurs.nli_lot([(premisse, hypothese) for _, premisse, hypothese in a_tester])
        if controle_llm:
            moteurs.constater_double_passe(sorties)
        for (position, _, _), sortie in zip(a_tester, sorties):
            verdict = resultats[position]
            score = _proba_implication(sortie)
            verdict['nli_score'] = score
            verdict['engine'] = sortie.get('engine')
            if score >= seuil:
                verdict['ok'] = True
            else:
                verdict['raison'] = 'nli_sous_seuil'
    return resultats


def garde_fou(fait, unite):
    """Garde-fou d'un fait (M1 etape 5).

    ``fait`` : dict(sujet, predicat, objet, periode?) ; ``unite`` : dict(text,
    quote). Verbalise le fait, verifie que ses entites figurent dans le span,
    puis soumet la verbalisation au NLI avec l'unite comme premisse. Renvoie
    dict(ok, raison, nli_score, verbalisation, engine, entites_absentes).
    """
    return _garde_fou_lot([(fait, unite)])[0]


# ---------------------------------------------------------------------------
# Etape 5 : faits
# ---------------------------------------------------------------------------

def _normaliser_objet(objet, type_objet, resolveur):
    """Forme normalisee de l'objet selon son type (date ISO, nombre, nom canonique, cle)."""
    from chaine import migration
    if type_objet in ('date', 'periode'):
        dates = migration.lire_dates(objet)
        if dates:
            return dates[0][0]
    if type_objet in ('montant', 'pourcentage', 'nombre', 'duree'):
        compact = re.sub(r'(?<=\d)[\s  .](?=\d{3}\b)', '', str(objet))
        nombre = _RE_NOMBRE.search(compact)
        if nombre:
            reste = cle(compact[nombre.end():]).strip()
            return nombre.group(0).replace(',', '.') + ((' ' + reste) if reste else '')
    if type_objet in TYPES_ENTITE_OBJET:
        ident = resolveur.resoudre(objet, type_objet)
        if ident:
            return resolveur.nom(ident)
    return cle(objet)


def _span_fait(citation, unite):
    """Span du fait dans la version : citation dans la citation de l'unite, sinon span de l'unite."""
    if unite.get('span_start') is None:
        return None
    quote = unite.get('quote') or ''
    position = localiser(citation, quote) if citation else None
    if position is not None:
        return unite['span_start'] + position[0], unite['span_start'] + position[1]
    if citation and localiser(citation, unite.get('text') or '') is None:
        return None
    return unite['span_start'], unite['span_end']


def _charger_unites(unit_ids):
    """Unites demandees, par paquets (une requete par paquet de 500)."""
    serveur = noyau.srv()
    resultat = []
    ids = list(unit_ids)
    for debut in range(0, len(ids), 500):
        paquet = ids[debut:debut + 500]
        marques = ','.join('?' * len(paquet))
        lignes = serveur.query(
            f'SELECT id, version_id, text, quote, span_start, span_end, extractor, facts_run_id FROM units WHERE id IN ({marques})',
            tuple(paquet))
        resultat.extend(dict(l) for l in lignes)
    ordre = {ident: i for i, ident in enumerate(ids)}
    resultat.sort(key=lambda u: ordre.get(u['id'], 0))
    return resultat


def _effacer_faits(base, unit_ids):
    """Avant une reextraction forcee : faits et hypotheses non resolues produits par l'etape 5."""
    if not unit_ids:
        return
    marques = ','.join('?' * len(unit_ids))
    base.execute(f"DELETE FROM facts WHERE unit_id IN ({marques}) AND COALESCE(origin,'') IN ('explicite','inference_contextuelle')",
                 tuple(unit_ids))
    base.execute(f"DELETE FROM hypotheses WHERE unit_id IN ({marques}) AND resolved_fact_id IS NULL "
                 f"AND kind IN ('connaissance_externe','information_inconnue','fait_rejete')", tuple(unit_ids))


def preparer_schema():
    """Colonne de reprise ``units.facts_run_id`` (ajout hors contrat, idempotent)."""
    from chaine import schema
    schema.colonne('units', 'facts_run_id', 'TEXT')


def faits(unit_ids, forcer=False, wikidata=None, run=None, compteurs=None):
    """Faits atomiques d'une liste d'unites (M1 etape 5), migrees comprises.

    Entites -> ``entities``/``unit_entities`` ; relations par GLiREL ou LLM
    contraint a la liste fermee ; faits ``explicite``/``inference_contextuelle``
    soumis au garde-fou puis ecrits dans ``facts`` ; connaissance externe,
    informations inconnues et faits rejetes dans ``hypotheses``. Une unite deja
    traitee (``units.facts_run_id``) est ignoree sauf ``forcer=True``.
    Renvoie un dict de compteurs.
    """
    preparer_schema()
    if run is None:
        with noyau.etape('m1.faits', moteurs={'relations': choisir_extracteur()},
                         prompt=PROMPT_FAITS + '+' + predicats.version(),
                         seuils=['m1.faits.nli_implication', 'm1.faits.entites_dans_span', 'm1.entites.score_min',
                                 'm1.entites.alias_similarite']) as execution:
            resultat = faits(unit_ids, forcer, wikidata, execution, compteurs)
            execution.items = resultat['faits']
            return resultat
    serveur = noyau.srv()
    compteurs = compteurs if compteurs is not None else {
        'unites': 0, 'unites_ignorees': 0, 'unites_sans_reponse': 0, 'unites_sans_extracteur': 0, 'entites': 0,
        'mentions': 0, 'faits': 0, 'faits_rejetes': 0, 'connaissance_externe': 0, 'information_inconnue': 0,
        'rejets': {},
    }
    toutes = _charger_unites(unit_ids)
    cibles = [u for u in toutes if forcer or not u.get('facts_run_id')]
    compteurs['unites_ignorees'] += len(toutes) - len(cibles)
    extracteur = choisir_extracteur()
    if extracteur == 'llm':
        # Double passe : les faits du LLM ne sont pas extraits si aucun NLI distinct du LLM ne peut les controler.
        refus = moteurs.raison_double_passe()
        if refus:
            raise serveur.OperationalError(refus)
    run.moteur('relations', extracteur if extracteur != 'llm' else noyau.moteur_llm())
    resolveur = Resolveur()
    if wikidata is None:
        wikidata = noyau.reglage('m1:entites:wikidata', '0') == '1'
    taille = _seuil('m1.faits.taille_lot')
    total = len(cibles)
    _job(stage='m1.faits', done=0, total=total, message='Faits atomiques : ' + str(total) + ' unites')
    nouvelles_entites = []
    for debut in range(0, total, taille):
        serveur.checkpoint()
        lot = cibles[debut:debut + taille]
        entites_par_unite = {}
        for unite in lot:
            liste = moteurs.entites(unite['text'] or '')
            entites_par_unite[unite['id']] = liste
            run.moteur('entites', moteurs.dernier('entites'))
        reponses = {}
        if extracteur == 'glirel':
            reponses = _relations_glirel(lot, entites_par_unite)
        elif extracteur == 'llm':
            reponses = _relations_llm(lot)
            for index in range(len(lot)):
                if index not in reponses:
                    seul = _relations_llm([lot[index]])
                    if 0 in seul:
                        reponses[index] = seul[0]
        candidats = []
        hypotheses = []
        for index, unite in enumerate(lot):
            reponse = reponses.get(index)
            if reponse is None:
                continue
            for fait in reponse['faits']:
                fait = dict(fait)
                if fait.get('origine') not in ORIGINES_FAITS:
                    hypotheses.append((unite, 'connaissance_externe', fait, 'origine connaissance_externe : jamais un fait'))
                    continue
                span = _span_fait(fait.get('citation') or '', unite)
                if span is None:
                    raison = 'unite_sans_span' if unite.get('span_start') is None else 'citation_introuvable'
                    hypotheses.append((unite, 'fait_rejete', fait, raison))
                    continue
                fait['span'] = span
                candidats.append((fait, unite))
            for inconnue in reponse['inconnues']:
                hypotheses.append((unite, 'information_inconnue',
                                   {'sujet': inconnue['sujet'], 'predicat': inconnue['predicat'], 'objet': '',
                                    'texte': inconnue['question']}, 'information absente de l unite'))
        verdicts = _garde_fou_lot(candidats, controle_llm=extracteur == 'llm') if candidats else []
        horodatage = serveur.now()
        with noyau.connexion() as base:
            if forcer:
                _effacer_faits(base, [u['id'] for u in lot if u.get('facts_run_id')])
            for unite in lot:
                for entite in entites_par_unite[unite['id']]:
                    avant = len(resolveur.nouvelles)
                    ident = resolveur.resoudre(entite['text'], entite['type'])
                    if ident is None:
                        continue
                    if len(resolveur.nouvelles) > avant:
                        nouvelles_entites.append(ident)
                    curseur = base.execute(
                        'INSERT OR IGNORE INTO unit_entities(unit_id, entity_id, "start", "end", role) VALUES(?,?,?,?,?)',
                        (unite['id'], ident, entite['start'], entite['end'], 'mention'))
                    compteurs['mentions'] += curseur.rowcount
            for (fait, unite), verdict in zip(candidats, verdicts):
                if not verdict['ok']:
                    hypotheses.append((unite, 'fait_rejete', dict(fait, verbalisation=verdict['verbalisation'],
                                                                  nli=verdict['nli_score']), verdict['raison']))
                    continue
                predicat = predicats.get(fait['predicat'])
                sujet_id = resolveur.resoudre(fait['sujet'], 'entite')
                if sujet_id and sujet_id in resolveur.nouvelles and sujet_id not in nouvelles_entites:
                    nouvelles_entites.append(sujet_id)
                base.execute(
                    'INSERT INTO facts(id, unit_id, subject_id, subject, predicate, object, object_norm, object_type, scope, '
                    'period, origin, span_start, span_end, verbalization, nli_score, status, run_id, created) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (serveur.uid(), unite['id'], sujet_id, fait['sujet'], fait['predicat'], fait['objet'],
                     _normaliser_objet(fait['objet'], predicat['type_objet'], resolveur), predicat['type_objet'],
                     fait.get('portee') or None, fait.get('periode') or None, fait['origine'], fait['span'][0],
                     fait['span'][1], verdict['verbalisation'], verdict['nli_score'], 'extrait', run.id, horodatage))
                compteurs['faits'] += 1
                if verdict.get('engine'):
                    run.moteur('nli', verdict['engine'])
            for unite, kind, fait, raison in hypotheses:
                texte = fait.get('texte') or fait.get('verbalisation') or ''
                if not texte and predicats.existe(fait.get('predicat')):
                    texte = verbaliser(fait)
                if fait.get('nli') is not None:
                    raison = raison + ' (nli=' + format(fait['nli'], '.2f') + ')'
                base.execute(
                    'INSERT INTO hypotheses(id, unit_id, kind, text, subject, predicate, object, reason, run_id, created) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (serveur.uid(), unite['id'], kind, texte, fait.get('sujet'), fait.get('predicat'), fait.get('objet'),
                     raison, run.id, horodatage))
                if kind == 'fait_rejete':
                    compteurs['faits_rejetes'] += 1
                    cle_raison = raison.split(' ')[0]
                    compteurs['rejets'][cle_raison] = compteurs['rejets'].get(cle_raison, 0) + 1
                else:
                    compteurs[kind] += 1
            resolveur.ecrire(base)
            for index, unite in enumerate(lot):
                if index in reponses:
                    base.execute('UPDATE units SET facts_run_id=? WHERE id=?', (run.id, unite['id']))
                    compteurs['unites'] += 1
                elif extracteur == 'aucun':
                    compteurs['unites_sans_extracteur'] += 1
                else:
                    compteurs['unites_sans_reponse'] += 1
        _job(done=min(total, debut + len(lot)))
    compteurs['entites'] += resolveur.creees
    if wikidata and nouvelles_entites:
        compteurs['wikidata'] = rattacher_wikidata(set(nouvelles_entites))
    return compteurs


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def executer(version_ids=None, unit_ids=None, option='auto', forcer=False):
    """M1 etapes 4 et 5, reprenables.

    - ``unit_ids`` fournis : etape 5 seulement sur ces unites (migrees comprises) ;
    - sinon : etape 4 sur ``version_ids`` (toutes les versions si None ; les
      versions deja decomposees, par migration ou extraction, sont ignorees sauf
      ``forcer``), puis etape 5 sur toutes les unites de ces versions qui n'ont
      pas encore de faits.
    Renvoie dict(unites=..., faits=...).
    """
    serveur = noyau.srv()
    resultat = {}
    if unit_ids is None:
        if version_ids is None:
            version_ids = [l['id'] for l in serveur.query('SELECT id FROM versions ORDER BY created, id')]
        effective = choisir_option(option)
        if effective == 'D':
            raise ValueError("L'option D est reservee au jeu de reference : utiliser unites(..., option='D', reference=True).")
        compteurs = _compteurs_vides()
        _job(stage='m1.unites', done=0, total=len(version_ids), message='Unites d information')
        with noyau.etape('m1.unites', moteurs={'unites': effective}, prompt=PROMPT_UNITES if effective == 'C' else '',
                         seuils=['m1.unites.mots_min', 'm1.unites.mots_max', 'm1.unites.span_recouvrement_min',
                                 'nli.implication']) as run:
            for numero, version_id in enumerate(version_ids, 1):
                serveur.checkpoint()
                unites(version_id, effective, forcer, False, run, compteurs)
                run.pages = compteurs['versions']
                _job(done=numero, message='Unites : ' + str(compteurs['unites']) + ' retenues')
            run.items = compteurs['unites']
            run.note = json.dumps({k: v for k, v in compteurs.items() if k != 'unit_ids'}, sort_keys=True)
        compteurs.pop('unit_ids', None)
        resultat['unites'] = compteurs
        unit_ids = []
        for debut in range(0, len(version_ids), 500):
            paquet = version_ids[debut:debut + 500]
            marques = ','.join('?' * len(paquet))
            unit_ids.extend(l['id'] for l in serveur.query(
                f'SELECT id FROM units WHERE version_id IN ({marques}) AND COALESCE(excluded,0)=0 ORDER BY version_id, span_start',
                tuple(paquet)))
    resultat['faits'] = faits(unit_ids, forcer=forcer)
    _job(message='M1 unites et faits termines')
    return resultat
