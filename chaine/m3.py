"""M3 — Rapprocher les articles, attribuer la paternite, propager les sources.

Specification : docs/METHODOLOGIES.txt, section M3 (etapes 1 a 5).

- Etape 1 ``voisins(unit_id)`` : recherche hybride hors de la page d'origine
  (filtre structurel, filtre thematique, similarite dense, reordonnancement).
  Mode degrade sans vecteurs : recouvrement lexical de termes porteurs, comme
  filtre de candidats seulement.
- Etape 2 ``cadre(unit)`` et ``qualifier(a, b)`` : NLI dans les deux sens,
  comparaison des cadres (sujet, predicat, portee, periode) et des dates
  d'ancrage ; le LLM local ne repond qu'aux paires de la zone grise.
- Etape 3 ``paternite(group_id)`` et ``arbitrer(group_id, unit_id)`` : ordre
  lexicographique strict sujet principal > nombre de sources > date de
  publication > arbitrage humain.
- Etape 4 ``propager(group_id)`` : sources vers le parent et vers les enfants,
  recommandations de lien interne.
- Etape 5 ``statuts_temporels(aujourdhui=None)``.
- ``executer(forcer=False, aujourdhui=None)``.

Aucun appel Ibou. Toute ligne produite porte le ``run_id`` de son etape.
"""
import hashlib
import json
import math
import re
from contextlib import contextmanager
from datetime import date, timedelta
from urllib.parse import urlparse

from chaine import migration
from chaine import moteurs
from chaine import noyau
from chaine import schema
from chaine import seuils

RELATIONS = ('equivalence', 'precision', 'remplacement', 'conflit', 'faux_conflit', 'sans_rapport')
CRITERES = ('sujet_principal', 'nombre_sources', 'date_publication', 'arbitrage_humain', 'a_arbitrer')
STATUTS_TEMPORELS = ('a_jour', 'obsolete_historique', 'remise_en_question', 'perimee_par_date')

# Types de sources produits par M2 (les sources « verification » viennent de M5).
SOURCES_M2 = ('lien_interne', 'lien_externe', 'source_nommee', 'note')

# Types d'entites numeriques : jamais sujet d'un cadre.
ENTITES_NUMERIQUES = ('date', 'montant', 'pourcentage', 'nombre', 'duree')


# Marqueurs de recence pour la double passe sur la reponse du LLM.
MARQUEURS_RECENCE = (
    'desormais', 'actuellement', 'aujourd hui', 'depuis', 'nouveau', 'nouvelle', 'nouvellement', 'a remplace',
    'remplace', 'succede', 'a succede', 'dorenavant', 'a present', 'recemment', 'vient de', 'nomme', 'nommee',
)

# Dates qui ouvrent un etat courant (« depuis 2025 ») : elles ne restreignent pas la periode du cadre.
_RE_DEBUT_ETAT = re.compile(r"\b(?:depuis|des|a partir d[eu']?|a compter d[eu']?|desormais)\s+(?:le\s+|l'|la\s+|(?:debut|fin|mi)\s*-?\s*)?(?:\d{1,2}(?:er)?\s+)?(?:[a-z]+\.?\s+)?$")
_RE_ANNEE = re.compile(r'(?<![\d.,])((?:19|20)\d{2})(?![\d.,]\d)')
_RE_TITRE_MAJ = re.compile(r"^[^a-z]*[A-Z][^a-z]*$")

TACHE_CADRE = (
    "Deux affirmations A et B semblent se contredire. Reponds a deux questions fermees, sans connaissance "
    "exterieure, d'apres le texte seul. 1) meme_cadre : parlent-elles du meme sujet, du meme predicat, de la "
    "meme portee (lieu, perimetre) et de la meme periode ? oui, non ou incertain. 2) plus_recente : laquelle "
    "est presentee par son texte comme la plus recente (marqueurs « desormais », « depuis », date plus tardive) ? "
    "a, b ou indeterminable."
)

SCHEMA_CADRE = noyau.schema('chaine:m3_cadre', noyau.objet({
    'meme_cadre': noyau.enum(('oui', 'non', 'incertain')),
    'plus_recente': noyau.enum(('a', 'b', 'indeterminable')),
}))

_PREPARE = {'base': None}


# ---------------------------------------------------------------------------
# Outils generaux
# ---------------------------------------------------------------------------

# Relation posee par la deduplication de M1 (``reason`` commence par ``m1_dedup``), pas encore qualifiee par M3.
CLAUSE_DEDUP = "COALESCE(reason,'') LIKE 'm1\\_dedup%' ESCAPE '\\'"


def _seuil(nom):
    """Seuil de ``chaine.seuils`` (tous les seuils du module y sont declares)."""
    return seuils.get(nom)


def _seuils_utilises(noms):
    """Dictionnaire nom -> valeur pour ``step_runs.thresholds``."""
    return {nom: _seuil(nom) for nom in noms}


def _preparer():
    """Colonnes ajoutees par M3 (idempotent, une fois par base)."""
    serveur = noyau.srv()
    base = str(serveur.DB)
    if _PREPARE['base'] == base:
        return
    schema.colonne('unit_groups', 'merged_into', 'TEXT')
    schema.colonne('link_recommendations', 'run_id', 'TEXT')
    schema.colonne('link_recommendations', 'group_id', 'TEXT')
    _PREPARE['base'] = base


def _id(*parties):
    """Identifiant deterministe court."""
    brut = '\x1f'.join(str(p) for p in parties)
    return hashlib.sha1(brut.encode('utf-8')).hexdigest()[:20]


def _par_lots(liste, taille=400):
    """Decoupe une liste en tranches (limite de parametres SQLite)."""
    liste = list(liste)
    for debut in range(0, len(liste), taille):
        yield liste[debut:debut + taille]


def _marques(lot):
    """« ?,?,? » pour une clause IN."""
    return ','.join('?' for _ in lot)


def _date_du_jour(aujourdhui):
    """Date du jour injectable (date, texte ISO ou None)."""
    if aujourdhui is None:
        return date.today()
    if isinstance(aujourdhui, date):
        return aujourdhui
    return date.fromisoformat(str(aujourdhui)[:10])


def _iso(valeur):
    """Date ISO ``AAAA-MM-JJ`` d'une valeur stockee, ou None."""
    texte = str(valeur or '').strip()
    if not texte:
        return None
    if re.match(r'^\d{4}-\d{2}-\d{2}', texte):
        return texte[:10]
    return migration.ancre_periode(texte)


def _cle(texte):
    """Cle de comparaison d'un nom : mots pleins normalises."""
    return ' '.join(moteurs.mots(texte or ''))


def _texte(unite):
    """Texte de travail d'une unite."""
    return unite.get('text') or unite.get('quote') or ''


def _json(valeur):
    """JSON compact pour stockage."""
    return json.dumps(valeur, ensure_ascii=False, sort_keys=True, default=str)


def _normaliser_url(url):
    """URL comparable : hote sans www, chemin sans barre finale, sans requete ni fragment."""
    try:
        morceaux = urlparse(str(url or '').strip())
    except ValueError:
        return ''
    if morceaux.scheme not in ('http', 'https') or not morceaux.hostname:
        return ''
    hote = morceaux.hostname.lower()
    if hote.startswith('www.'):
        hote = hote[4:]
    chemin = morceaux.path.rstrip('/')
    return hote + chemin


def _cle_source(source):
    """Cle de deduplication d'une source : URL, sinon entite citee, sinon passage."""
    url = _normaliser_url(source.get('url'))
    if url:
        return 'url:' + url
    if source.get('cited_entity'):
        return 'nom:' + _cle(source['cited_entity'])
    return 'autre:' + _cle(source.get('passage') or source.get('id') or '')


def _cle_domaine(source):
    """Cle « dedupliquee par domaine » du critere 2 de la paternite."""
    nom = (source.get('domain') or '').lower()
    if not nom and source.get('url'):
        nom = migration.domaine(source['url'])
    if nom:
        if nom.startswith('www.'):
            nom = nom[4:]
        return 'domaine:' + nom
    if source.get('cited_entity'):
        return 'nom:' + _cle(source['cited_entity'])
    return 'autre:' + _cle(source.get('passage') or source.get('id') or '')


class _RunExistant:
    """Execution deja ouverte par l'appelant (seul ``id`` est utile)."""

    def __init__(self, identifiant):
        self.id = identifiant
        self.pages = 0
        self.items = 0
        self.note = ''

    def moteur(self, nom, identifiant):
        """Sans effet : l'execution englobante trace ses moteurs."""
        return None


@contextmanager
def _execution(nom, **options):
    """Reutilise l'execution courante, sinon ouvre une etape ``noyau.etape``."""
    courant = noyau.execution_courante()
    if courant:
        yield _RunExistant(courant)
        return
    with noyau.etape(nom, **options) as run:
        yield run


def _job(**valeurs):
    """Met a jour ``S.JOB`` si possible."""
    serveur = noyau.srv()
    travail = getattr(serveur, 'JOB', None)
    if travail is not None and hasattr(travail, 'update'):
        travail.update(**valeurs)


def _arret():
    """Point d'arret cooperatif du serveur."""
    serveur = noyau.srv()
    if hasattr(serveur, 'checkpoint'):
        serveur.checkpoint()


def _arret_demande():
    """Vrai si l'utilisateur a demande l'arret."""
    arret = getattr(noyau.srv(), 'STOP', None)
    return bool(arret is not None and arret.is_set())


# ---------------------------------------------------------------------------
# Chargements groupes
# ---------------------------------------------------------------------------

def _unites(ids):
    """Unites (avec page et date de publication) indexees par id."""
    serveur = noyau.srv()
    resultat = {}
    for lot in _par_lots(set(ids)):
        lignes = serveur.query(
            'SELECT u.id, u.version_id, u.chunk_id, u.text, u.quote, u.span_start, u.span_end, u.anchor_date, '
            'u.anchor_kind, u.expires, u.obs_class, u.source_manquante, u.centrality, u.excluded, '
            'v.page_id, v.published FROM units u LEFT JOIN versions v ON v.id=u.version_id '
            'WHERE u.id IN (' + _marques(lot) + ')',
            tuple(lot),
        )
        for ligne in lignes:
            resultat[ligne['id']] = ligne
    return resultat


def _faits(ids):
    """Faits retenus par unite (les faits rejetes sont ignores)."""
    serveur = noyau.srv()
    resultat = {}
    for lot in _par_lots(set(ids)):
        lignes = serveur.query(
            'SELECT * FROM facts WHERE unit_id IN (' + _marques(lot) + ') ORDER BY span_start, id',
            tuple(lot),
        )
        for ligne in lignes:
            if str(ligne.get('status') or '').lower() in ('rejete', 'rejected', 'invalide'):
                continue
            resultat.setdefault(ligne['unit_id'], []).append(ligne)
    return resultat


def _entites_unites(ids):
    """Entites canoniques par unite : liste de dict(entity_id, name, aliases, type, role, start)."""
    serveur = noyau.srv()
    resultat = {}
    for lot in _par_lots(set(ids)):
        lignes = serveur.query(
            'SELECT ue.unit_id, ue.entity_id, ue.role, ue."start" AS start, e.name, e.aliases, e.type '
            'FROM unit_entities ue LEFT JOIN entities e ON e.id=ue.entity_id '
            'WHERE ue.unit_id IN (' + _marques(lot) + ') ORDER BY ue."start"',
            tuple(lot),
        )
        for ligne in lignes:
            resultat.setdefault(ligne['unit_id'], []).append(ligne)
    return resultat


def _groupes_de(ids):
    """Groupes de chaque unite (unite -> liste de groupes)."""
    serveur = noyau.srv()
    resultat = {}
    for lot in _par_lots(set(ids)):
        lignes = serveur.query(
            'SELECT group_id, unit_id FROM unit_group_members WHERE unit_id IN (' + _marques(lot) + ')',
            tuple(lot),
        )
        for ligne in lignes:
            resultat.setdefault(ligne['unit_id'], []).append(ligne['group_id'])
    return resultat


# ---------------------------------------------------------------------------
# Etape 2 — Cadre
# ---------------------------------------------------------------------------

def cadre(unit):
    """Cadre d'une unite : dict(sujet, predicat, portee, periode, origine, cles).

    ``unit`` : identifiant ou dict d'unite. Sources, dans l'ordre : faits de
    M1 (sujet, predicat, portee, periode), entites canoniques de M1, sinon
    entites par regles sur le texte (unites migrees). La periode, sans fait,
    est l'ensemble des annees ecrites dans l'unite, hors dates qui ouvrent un
    etat courant (« depuis 2025 »). ``cles`` : cles normalisees du sujet pour
    la comparaison.
    """
    if isinstance(unit, dict):
        unite = unit
    else:
        unite = _unites([unit]).get(unit)
        if unite is None:
            raise KeyError('Unite inconnue : ' + str(unit))
    identifiant = unite.get('id')
    faits = _faits([identifiant]).get(identifiant, []) if identifiant else []
    entites = _entites_unites([identifiant]).get(identifiant, []) if identifiant else []
    return _cadre(unite, faits, entites)


def _cadre(unite, faits, entites):
    """Construit le cadre a partir des donnees deja chargees."""
    texte = _texte(unite)
    resultat = {'sujet': None, 'predicat': None, 'portee': None, 'periode': None, 'origine': 'aucun', 'cles': []}
    if faits:
        fait = faits[0]
        resultat['sujet'] = fait.get('subject')
        resultat['predicat'] = fait.get('predicate') or None
        resultat['portee'] = fait.get('scope') or None
        resultat['periode'] = fait.get('period') or None
        resultat['origine'] = 'faits'
        cles = set()
        for element in faits:
            if element.get('subject_id'):
                cles.add('id:' + str(element['subject_id']))
            elif element.get('subject'):
                cles.add('nom:' + _cle(element['subject']))
        resultat['cles'] = sorted(c for c in cles if c not in ('nom:',))
    elif entites:
        utiles = [e for e in entites if str(e.get('type') or '') not in ENTITES_NUMERIQUES]
        sujets = [e for e in utiles if str(e.get('role') or '').lower() in ('sujet', 'subject')]
        choisie = (sujets or utiles or [None])[0]
        if choisie is not None:
            resultat['sujet'] = choisie.get('name') or choisie.get('entity_id')
        resultat['cles'] = sorted({'id:' + str(e['entity_id']) for e in utiles if e.get('entity_id')})
        resultat['origine'] = 'entites'
    else:
        trouvees = [e for e in moteurs.entites_regles(texte) if e['type'] not in ENTITES_NUMERIQUES]
        if trouvees:
            resultat['sujet'] = trouvees[0]['text']
        resultat['cles'] = sorted({'nom:' + _cle(e['text']) for e in trouvees if _cle(e['text'])})
        resultat['origine'] = 'regles'
    if resultat['periode'] is None:
        annees = _annees_periode(texte)
        if annees:
            resultat['periode'] = '|'.join(annees)
    return resultat


def _annees_periode(texte):
    """Annees ecrites dans le texte qui restreignent la periode (hors « depuis 2025 »)."""
    source = migration.sans_accents(texte or '').replace('’', "'")
    annees = set()
    for match in _RE_ANNEE.finditer(source):
        avant = source[max(0, match.start() - 40):match.start()]
        if _RE_DEBUT_ETAT.search(avant):
            continue
        annees.add(match.group(1))
    return sorted(annees)


def _annees(periode):
    """Ensemble des annees d'une periode stockee (texte libre)."""
    return set(_RE_ANNEE.findall(str(periode or '')))


def comparer_cadres(premier, second):
    """Compare deux cadres : (verdict, motif) avec verdict ``meme|different|indetermine``.

    Sujet : cles communes. Predicat et portee : differents seulement s'ils sont
    connus des deux cotes. Periode : differente si les annees sont disjointes.
    Sans sujet identifiable d'un cote, le cadre est indetermine.
    """
    if not premier['cles'] or not second['cles']:
        return 'indetermine', 'sujet inconnu'
    if not set(premier['cles']) & set(second['cles']):
        return 'different', 'sujet'
    if premier['predicat'] and second['predicat'] and premier['predicat'] != second['predicat']:
        return 'different', 'predicat'
    if premier['portee'] and second['portee'] and _cle(premier['portee']) != _cle(second['portee']):
        return 'different', 'portee'
    annees_premier = _annees(premier['periode'])
    annees_second = _annees(second['periode'])
    if premier['periode'] and second['periode']:
        if annees_premier and annees_second:
            if not annees_premier & annees_second:
                return 'different', 'periode'
        elif _cle(premier['periode']) != _cle(second['periode']):
            return 'different', 'periode'
    if not premier['predicat'] or not second['predicat']:
        return 'meme', 'predicat non extrait'
    return 'meme', ''


# ---------------------------------------------------------------------------
# Etape 2 — « Nettement plus recent »
# ---------------------------------------------------------------------------

def _volatilite(predicat):
    """Volatilite (jours) du predicat d'apres ``chaine.predicats`` (lot M1), ou None."""
    if not predicat:
        return None
    try:
        from chaine import predicats
    except ImportError:
        return None
    try:
        fiche = predicats.get(predicat)
    except Exception:
        return None
    if not isinstance(fiche, dict):
        return None
    try:
        valeur = float(fiche.get('volatilite_jours'))
    except (TypeError, ValueError):
        return None
    if valeur <= 0:
        return None
    return valeur


def seuil_recence(predicat=None):
    """Ecart minimal (jours) pour « nettement plus recent » : (jours, regle).

    Seuil fixe ``m3.nettement_plus_recent_jours`` ; variante : facteur
    ``m3.recent.facteur_volatilite`` x volatilite du predicat si elle est
    connue (point ouvert du document).
    """
    fixe = int(seuils.get('m3.nettement_plus_recent_jours'))
    facteur = float(_seuil('m3.recent.facteur_volatilite'))
    volatilite = _volatilite(predicat)
    if volatilite and facteur > 0:
        return max(1, int(round(volatilite * facteur))), 'volatilite:' + str(predicat)
    return fixe, 'fixe'


def _chronologie(premiere, seconde, jours):
    """Unite nettement plus recente (``'a'``, ``'b'``) ou None, avec l'ecart en jours."""
    date_a = _iso(premiere.get('anchor_date'))
    date_b = _iso(seconde.get('anchor_date'))
    if not date_a or not date_b:
        return None, None
    try:
        ecart = (date.fromisoformat(date_b) - date.fromisoformat(date_a)).days
    except ValueError:
        return None, None
    if ecart >= jours:
        return 'b', ecart
    if -ecart >= jours:
        return 'a', ecart
    return None, ecart


def _marque_recence(unite, autre):
    """Double passe sur le LLM : l'unite porte-t-elle un indice textuel de recence ?"""
    texte = ' ' + migration.sans_accents(_texte(unite)).replace('’', "'") + ' '
    texte = re.sub(r"[^a-z0-9']+", ' ', texte)
    if any(' ' + marqueur + ' ' in texte for marqueur in MARQUEURS_RECENCE):
        return True
    annees = [int(a) for a in _RE_ANNEE.findall(_texte(unite))]
    annees_autre = [int(a) for a in _RE_ANNEE.findall(_texte(autre))]
    return bool(annees and annees_autre and max(annees) > max(annees_autre))


# ---------------------------------------------------------------------------
# Etape 2 — Qualification des paires
# ---------------------------------------------------------------------------

def _probas(resultat):
    """Probabilites des trois labels d'un resultat NLI (doubles de test compris)."""
    probas = resultat.get('probas')
    if isinstance(probas, dict) and probas:
        return {label: float(probas.get(label, 0.0)) for label in ('implication', 'contradiction', 'neutre')}
    score = float(resultat.get('score') or 0.0)
    reste = (1.0 - score) / 2.0
    valeurs = {label: reste for label in ('implication', 'contradiction', 'neutre')}
    valeurs[resultat.get('label') or 'neutre'] = score
    return valeurs


def qualifier(a, b):
    """Qualifie la paire (a, b) et l'enregistre dans ``unit_relations``.

    Renvoie dict(a, b, relation, direction, nli_ab, nli_ba, engine, reason).
    ``direction`` : unite la plus precise (precision) ou la plus recente
    (remplacement). Consequences : equivalence/precision rattachent les deux
    unites au meme groupe ; remplacement rend l'ancienne
    ``obsolete_historique`` ; conflit rend les deux ``remise_en_question``.
    Leve ``S.OperationalError`` si aucun moteur NLI n'est disponible.
    """
    _preparer()
    noms = ['nli.implication', 'm3.zone_grise', 'm3.nettement_plus_recent_jours', 'm3.recent.facteur_volatilite']
    with _execution('m3.qualifier', moteurs={}, prompt='m3-cadre-v1', seuils=_seuils_utilises(noms)) as run:
        return _qualifier_lot([(a, b)], run)[0]


def _qualifier_lot(paires, run):
    """Qualifie un lot de paires (un appel NLI pour tout le lot) et enregistre."""
    ids = {x for paire in paires for x in paire}
    unites = _unites(ids)
    manquantes = sorted(ids - set(unites))
    if manquantes:
        raise KeyError('Unites inconnues : ' + ', '.join(manquantes[:5]))
    faits = _faits(ids)
    entites = _entites_unites(ids)
    cadres = {uid: _cadre(unites[uid], faits.get(uid, []), entites.get(uid, [])) for uid in ids}
    textes = []
    for a, b in paires:
        textes.append((_texte(unites[a]), _texte(unites[b])))
        textes.append((_texte(unites[b]), _texte(unites[a])))
    reponses = moteurs.nli_lot(textes)
    moteur_nli = reponses[0].get('engine') if reponses else ''
    run.moteur('nli', moteur_nli)
    resultats = []
    for index, (a, b) in enumerate(paires):
        resultat = _decider(unites[a], unites[b], cadres[a], cadres[b], reponses[2 * index], reponses[2 * index + 1], run)
        resultat['engine'] = resultat['engine'] or moteur_nli
        resultats.append(resultat)
    _enregistrer_relations(resultats, unites, run)
    return resultats


def _decider(ua, ub, cadre_a, cadre_b, nli_ab, nli_ba, run):
    """Table de l'etape 2 : relation logique x ancrage temporel -> relation retenue."""
    probas_ab = _probas(nli_ab)
    probas_ba = _probas(nli_ba)
    seuil_implication = float(seuils.get('nli.implication'))
    bas, haut = seuils.get('m3.zone_grise')
    implique_ab = probas_ab['implication'] >= seuil_implication
    implique_ba = probas_ba['implication'] >= seuil_implication
    contradiction = max(probas_ab['contradiction'], probas_ba['contradiction'])
    motif = {
        'p_implication_ab': round(probas_ab['implication'], 4),
        'p_implication_ba': round(probas_ba['implication'], 4),
        'p_contradiction': round(contradiction, 4),
        'cadre_a': {k: cadre_a[k] for k in ('sujet', 'predicat', 'portee', 'periode', 'origine')},
        'cadre_b': {k: cadre_b[k] for k in ('sujet', 'predicat', 'portee', 'periode', 'origine')},
    }
    resultat = {
        'a': ua['id'], 'b': ub['id'], 'relation': 'sans_rapport', 'direction': None,
        'nli_ab': round(probas_ab[_label(probas_ab)], 4),
        'nli_ba': round(probas_ba[_label(probas_ba)], 4),
        'engine': '', 'reason': motif,
    }
    motif['label_ab'] = _label(probas_ab)
    motif['label_ba'] = _label(probas_ba)
    if implique_ab and implique_ba:
        resultat['relation'] = 'equivalence'
        motif['regle'] = 'implication mutuelle'
        return resultat
    if implique_ab or implique_ba:
        resultat['relation'] = 'precision'
        resultat['direction'] = ua['id'] if implique_ab else ub['id']
        motif['regle'] = 'implication dans un seul sens : la plus precise implique l autre'
        return resultat
    if contradiction < bas:
        motif['regle'] = 'ni implication ni contradiction'
        return resultat
    verdict_cadre, motif_cadre = comparer_cadres(cadre_a, cadre_b)
    motif['cadre'] = verdict_cadre
    motif['cadre_motif'] = motif_cadre
    predicat = cadre_a['predicat'] if cadre_a['predicat'] == cadre_b['predicat'] else None
    jours, regle_jours = seuil_recence(predicat)
    motif['seuil_recence_jours'] = jours
    motif['seuil_recence_regle'] = regle_jours
    plus_recente, ecart = _chronologie(ua, ub, jours)
    motif['ecart_ancrage_jours'] = ecart
    if verdict_cadre == 'different':
        resultat['relation'] = 'faux_conflit'
        motif['regle'] = 'contradiction apparente, cadres differents (' + motif_cadre + ')'
        return resultat
    if contradiction >= haut:
        if verdict_cadre == 'indetermine':
            resultat['relation'] = 'conflit'
            motif['regle'] = 'contradiction, cadre indetermine : verification'
            return resultat
        return _chronologie_conflit(resultat, plus_recente, ua, ub, motif)
    # Zone grise du NLI : le LLM local repond a la question fermee.
    motif['zone_grise'] = [bas, haut]
    try:
        reponse = noyau.llm(TACHE_CADRE, {
            'mode': 'chaine:m3_cadre',
            'a': {'texte': _texte(ua), 'date_ancrage': ua.get('anchor_date')},
            'b': {'texte': _texte(ub), 'date_ancrage': ub.get('anchor_date')},
        })
    except Exception as erreur:
        if _arret_demande():
            raise
        motif['regle'] = 'zone grise, LLM indisponible : non qualifiee'
        motif['erreur_llm'] = str(erreur)[:200]
        return resultat
    resultat['engine'] = noyau.moteur_llm()
    run.moteur('m3_cadre', resultat['engine'])
    motif['llm'] = reponse
    if reponse['meme_cadre'] == 'non':
        resultat['relation'] = 'faux_conflit'
        motif['regle'] = 'zone grise : le LLM juge les cadres differents'
        return resultat
    if reponse['meme_cadre'] == 'incertain' and verdict_cadre != 'meme':
        motif['regle'] = 'zone grise : cadre incertain, non qualifiee'
        return resultat
    choix_llm = reponse['plus_recente']
    if plus_recente is not None:
        if choix_llm in ('a', 'b') and choix_llm != plus_recente:
            resultat['relation'] = 'conflit'
            motif['regle'] = 'zone grise : ancrages et LLM divergent sur la chronologie'
            return resultat
        return _chronologie_conflit(resultat, plus_recente, ua, ub, motif)
    if choix_llm in ('a', 'b') and ecart is None:
        recente, ancienne = (ua, ub) if choix_llm == 'a' else (ub, ua)
        if _marque_recence(recente, ancienne):
            motif['regle'] = 'zone grise : LLM et indice textuel de recence concordent'
            resultat['relation'] = 'remplacement'
            resultat['direction'] = recente['id']
            return resultat
        motif['regle'] = 'zone grise : chronologie du LLM non confirmee par le texte'
    else:
        motif['regle'] = 'zone grise : meme periode ou chronologie indeterminable'
    resultat['relation'] = 'conflit'
    return resultat


def _label(probas):
    """Label de probabilite maximale."""
    return max(probas, key=probas.get)


def _chronologie_conflit(resultat, plus_recente, ua, ub, motif):
    """Contradiction, meme cadre : remplacement si une unite est nettement plus recente, sinon conflit."""
    if plus_recente is None:
        resultat['relation'] = 'conflit'
        motif['regle'] = 'contradiction, meme cadre, meme periode ou chronologie indeterminable'
        return resultat
    resultat['relation'] = 'remplacement'
    resultat['direction'] = ua['id'] if plus_recente == 'a' else ub['id']
    motif['regle'] = 'contradiction, meme cadre, une unite nettement plus recente : succession'
    return resultat


def _enregistrer_relations(resultats, unites, run):
    """Ecrit ``unit_relations`` et applique les consequences immediates."""
    serveur = noyau.srv()
    horodatage = serveur.now()
    with noyau.connexion() as base:
        for resultat in resultats:
            a = resultat['a']
            b = resultat['b']
            base.execute('DELETE FROM unit_relations WHERE a=? AND b=?', (b, a))
            base.execute(
                'INSERT OR REPLACE INTO unit_relations(a,b,relation,direction,nli_ab,nli_ba,engine,reason,run_id,created) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (a, b, resultat['relation'], resultat['direction'], resultat['nli_ab'], resultat['nli_ba'],
                 resultat['engine'], _json(resultat['reason']), run.id, horodatage),
            )
            relation = resultat['relation']
            if relation in ('equivalence', 'precision'):
                resultat['groupe'] = _rattacher(base, a, b, unites, run.id, horodatage)
            elif relation == 'remplacement':
                ancienne = b if resultat['direction'] == a else a
                base.execute("UPDATE units SET temporal_status='obsolete_historique', temporal_run=? WHERE id=?", (run.id, ancienne))
            elif relation == 'conflit':
                base.execute("UPDATE units SET temporal_status='remise_en_question', temporal_run=? WHERE id IN (?,?)", (run.id, a, b))


def _rattacher(base, a, b, unites, run_id, horodatage):
    """Met deux unites equivalentes ou liees par precision dans le meme groupe.

    Aucun groupe : groupe cree. Un seul : l'autre unite le rejoint. Deux
    groupes distincts : le plus petit est absorbe (membres deplaces,
    ``unit_groups.merged_into`` renseigne, paternite non humaine et
    propagations du groupe absorbe supprimees). Renvoie le groupe retenu.
    """
    groupes_a = [r[0] for r in base.execute('SELECT group_id FROM unit_group_members WHERE unit_id=?', (a,))]
    groupes_b = [r[0] for r in base.execute('SELECT group_id FROM unit_group_members WHERE unit_id=?', (b,))]
    communs = sorted(set(groupes_a) & set(groupes_b))
    if communs:
        return communs[0]
    if not groupes_a and not groupes_b:
        cible = 'm3-' + _id('groupe', min(a, b), max(a, b))
        etiquette = _texte(unites.get(a, {}))[:300]
        base.execute(
            'INSERT OR IGNORE INTO unit_groups(id,frame,label,legacy_claim_id,run_id,created) VALUES(?,?,?,?,?,?)',
            (cible, None, etiquette, None, run_id, horodatage),
        )
        base.execute('INSERT OR IGNORE INTO unit_group_members(group_id,unit_id) VALUES(?,?)', (cible, a))
        base.execute('INSERT OR IGNORE INTO unit_group_members(group_id,unit_id) VALUES(?,?)', (cible, b))
        return cible
    if groupes_a and not groupes_b:
        cible = sorted(groupes_a)[0]
        base.execute('INSERT OR IGNORE INTO unit_group_members(group_id,unit_id) VALUES(?,?)', (cible, b))
        base.execute('DELETE FROM paternity WHERE group_id=? AND COALESCE(human,0)=0', (cible,))
        return cible
    if groupes_b and not groupes_a:
        cible = sorted(groupes_b)[0]
        base.execute('INSERT OR IGNORE INTO unit_group_members(group_id,unit_id) VALUES(?,?)', (cible, a))
        base.execute('DELETE FROM paternity WHERE group_id=? AND COALESCE(human,0)=0', (cible,))
        return cible
    groupe_a = sorted(groupes_a)[0]
    groupe_b = sorted(groupes_b)[0]
    taille_a = base.execute('SELECT count(*) FROM unit_group_members WHERE group_id=?', (groupe_a,)).fetchone()[0]
    taille_b = base.execute('SELECT count(*) FROM unit_group_members WHERE group_id=?', (groupe_b,)).fetchone()[0]
    if (taille_a, groupe_b) >= (taille_b, groupe_a):
        cible, absorbe = groupe_a, groupe_b
    else:
        cible, absorbe = groupe_b, groupe_a
    base.execute(
        'INSERT OR IGNORE INTO unit_group_members(group_id,unit_id) SELECT ?, unit_id FROM unit_group_members WHERE group_id=?',
        (cible, absorbe),
    )
    base.execute('DELETE FROM unit_group_members WHERE group_id=?', (absorbe,))
    base.execute('UPDATE unit_groups SET merged_into=? WHERE id=?', (cible, absorbe))
    base.execute('DELETE FROM paternity WHERE group_id=? AND COALESCE(human,0)=0', (cible,))
    base.execute('DELETE FROM paternity WHERE group_id=?', (absorbe,))
    prefixe = 'm3:' + absorbe + ':'
    base.execute('DELETE FROM sources WHERE substr(id,1,?)=?', (len(prefixe), prefixe))
    base.execute("DELETE FROM link_recommendations WHERE group_id=? AND status='proposee'", (absorbe,))
    return cible


# ---------------------------------------------------------------------------
# Etape 1 — Voisins hors de la page d'origine
# ---------------------------------------------------------------------------

class Index:
    """Index des unites pour la recherche de voisins (construit une fois par execution).

    Contient, sans charger les vecteurs : page et version de chaque unite,
    listes inversees des entites canoniques et des sujets de faits, sujets
    fuzzy des pages, termes porteurs (idf) pour le mode degrade.
    """

    def __init__(self, unit_ids=None):
        serveur = noyau.srv()
        self.meta = {}
        self.par_page = {}
        lignes = serveur.query(
            'SELECT u.id, u.version_id, u.text, u.quote, (u.vector IS NOT NULL AND u.vector != \'\') AS a_vecteur, '
            'v.page_id FROM units u LEFT JOIN versions v ON v.id=u.version_id WHERE COALESCE(u.excluded,0)=0'
        )
        filtre = set(unit_ids) if unit_ids is not None else None
        for ligne in lignes:
            if filtre is not None and ligne['id'] not in filtre:
                continue
            page = ligne['page_id'] or ('version:' + str(ligne['version_id']))
            self.meta[ligne['id']] = {
                'page': page, 'version': ligne['version_id'], 'texte': ligne['text'] or ligne['quote'] or '',
                'a_vecteur': bool(ligne['a_vecteur']),
            }
            self.par_page.setdefault(page, set()).add(ligne['id'])
        self._structure(serveur)
        self._sujets(serveur)
        self._lexique()
        self._vecteurs = {}
        self._matrice = None
        self._denses = {}

    def _structure(self, serveur):
        """Cles structurelles : entites canoniques et sujets de faits."""
        self.cles = {}
        self.postings_cles = {}
        for ligne in serveur.query('SELECT unit_id, entity_id FROM unit_entities'):
            self._ajouter_cle(ligne['unit_id'], 'e:' + str(ligne['entity_id']))
        for ligne in serveur.query("SELECT unit_id, subject_id, subject FROM facts WHERE COALESCE(status,'') NOT IN ('rejete','rejected')"):
            if ligne['subject_id']:
                self._ajouter_cle(ligne['unit_id'], 'e:' + str(ligne['subject_id']))
            elif ligne['subject']:
                self._ajouter_cle(ligne['unit_id'], 's:' + _cle(ligne['subject']))

    def _ajouter_cle(self, unite, cle):
        """Ajoute une cle structurelle a une unite indexee."""
        if unite not in self.meta or cle in ('s:',):
            return
        self.cles.setdefault(unite, set()).add(cle)
        self.postings_cles.setdefault(cle, set()).add(unite)

    def _sujets(self, serveur):
        """Sujets fuzzy des pages (degre au-dessus du seuil d'appartenance)."""
        seuil = float(seuils.get('m1.fcm.appartenance'))
        self.sujets_version = {}
        self.versions_sujet = {}
        self.sujets_unite = {}
        lignes = serveur.query('SELECT topic_id, target_kind, target_id, degree FROM topic_membership WHERE degree>=?', (seuil,))
        for ligne in lignes:
            if ligne['target_kind'] == 'version':
                self.sujets_version.setdefault(ligne['target_id'], set()).add(ligne['topic_id'])
                self.versions_sujet.setdefault(ligne['topic_id'], set()).add(ligne['target_id'])
            elif ligne['target_kind'] == 'unit':
                self.sujets_unite.setdefault(ligne['target_id'], set()).add(ligne['topic_id'])

    def _lexique(self):
        """Termes porteurs : mots pleins rares (df <= ``m3.voisins.df_max``), ponderes par idf."""
        total = max(1, len(self.meta))
        self.termes = {uid: set(moteurs.mots(m['texte'])) for uid, m in self.meta.items()}
        frequences = {}
        for termes in self.termes.values():
            for terme in termes:
                frequences[terme] = frequences.get(terme, 0) + 1
        # Plancher de 50 : sur un petit corpus, 2 % ne laisserait aucun terme porteur.
        plafond = max(50, int(float(_seuil('m3.voisins.df_max')) * total))
        self.idf = {t: math.log((total + 1.0) / (f + 0.5)) + 1.0 for t, f in frequences.items()}
        self.postings_termes = {}
        for uid, termes in self.termes.items():
            for terme in termes:
                if frequences[terme] <= plafond:
                    self.postings_termes.setdefault(terme, []).append(uid)

    def vecteur(self, unite):
        """Vecteur d'une unite (charge a la demande), ou None."""
        if unite not in self._vecteurs:
            self.charger_vecteurs([unite])
        return self._vecteurs.get(unite)

    def charger_vecteurs(self, ids):
        """Charge les vecteurs manquants d'une liste d'unites."""
        manquants = [u for u in ids if u not in self._vecteurs and self.meta.get(u, {}).get('a_vecteur')]
        for lot in _par_lots(manquants):
            lignes = noyau.srv().query('SELECT id, vector FROM units WHERE id IN (' + _marques(lot) + ')', tuple(lot))
            for ligne in lignes:
                try:
                    valeur = json.loads(ligne['vector']) if isinstance(ligne['vector'], str) else None
                except ValueError:
                    valeur = None
                if isinstance(valeur, dict):
                    valeur = valeur.get('values')
                self._vecteurs[ligne['id']] = [float(x) for x in valeur] if isinstance(valeur, list) and valeur else None
        for unite in ids:
            self._vecteurs.setdefault(unite, None)

    def sujets(self, unite):
        """Sujets de l'unite : les siens, sinon ceux de sa version."""
        propres = self.sujets_unite.get(unite)
        if propres:
            return propres
        return self.sujets_version.get(self.meta[unite]['version'], set())

    def lexical(self, unite):
        """Scores de recouvrement des termes porteurs (part idf des termes de l'unite)."""
        termes = self.termes.get(unite, set())
        somme = sum(self.idf.get(t, 0.0) for t in termes)
        if not somme:
            return {}
        scores = {}
        for terme in termes:
            poids = self.idf[terme]
            for autre in self.postings_termes.get(terme, ()):
                if autre != unite:
                    scores[autre] = scores.get(autre, 0.0) + poids
        return {autre: valeur / somme for autre, valeur in scores.items()}

    def _admis(self, unite):
        """Predicat « candidat admissible » de ``unite`` : hors de sa page, filtres structurel et thematique.

        Renvoie ``(admis, filtres, structurel)`` ; ``structurel`` est l'ensemble des unites partageant une
        cle (ou None). Evite de construire le pool thematique (O(n) par unite) : on ne teste que les
        candidats deja retenus par la similarite.
        """
        page = self.meta[unite]['page']
        filtres = []
        structurel = None
        cles = self.cles.get(unite)
        if cles:
            structurel = set()
            for cle in cles:
                structurel |= self.postings_cles.get(cle, set())
            filtres.append('structurel')
        sujets = self.sujets(unite)
        versions = set()
        if sujets:
            for sujet in sujets:
                versions |= self.versions_sujet.get(sujet, set())
            filtres.append('thematique')

        def admis(autre):
            meta = self.meta.get(autre)
            if meta is None or meta['page'] == page:
                return False
            if structurel is not None and autre not in structurel:
                return False
            if sujets and meta['version'] not in versions and not (self.sujets_unite.get(autre, set()) & sujets):
                return False
            return True
        return admis, filtres, structurel

    def _numpy_dense(self):
        """Matrice numpy normalisee (float32) des vecteurs et rang de chaque unite ; None sans numpy.

        Construite une fois par index, lue directement depuis la base (sans garder les listes Python).
        Les vecteurs d'une autre dimension que la plus frequente restent au repli Python.
        """
        if self._matrice is not None:
            return self._matrice or None
        try:
            import numpy
        except ImportError:
            self._matrice = False
            return None
        ids = sorted(u for u, m in self.meta.items() if m['a_vecteur'])
        lignes = []
        rangs = {}
        dimension = None
        for lot in _par_lots(ids, 2000):
            for ligne in noyau.srv().query('SELECT id, vector FROM units WHERE id IN (' + _marques(lot) + ')', tuple(lot)):
                try:
                    valeur = json.loads(ligne['vector']) if isinstance(ligne['vector'], str) else None
                except ValueError:
                    valeur = None
                if isinstance(valeur, dict):
                    valeur = valeur.get('values')
                if not isinstance(valeur, list) or not valeur:
                    continue
                if dimension is None:
                    dimension = len(valeur)
                if len(valeur) != dimension:
                    continue
                rangs[ligne['id']] = len(lignes)
                lignes.append(numpy.asarray(valeur, dtype=numpy.float32))
        if not lignes:
            self._matrice = False
            return None
        matrice = numpy.vstack(lignes)
        normes = numpy.linalg.norm(matrice, axis=1)
        normes[normes == 0] = 1.0
        matrice /= normes[:, None]
        self._matrice = {'M': matrice, 'rang': rangs, 'ids': [None] * len(lignes), 'numpy': numpy}
        for identifiant, rang in rangs.items():
            self._matrice['ids'][rang] = identifiant
        return self._matrice

    def preparer_denses(self, unites):
        """Precalcule, par produits matriciels par blocs, les voisins denses (cosinus >= ``m3.voisins.cosinus``).

        Sans numpy : sans effet (``voisins`` calcule alors le cosinus en Python).
        """
        dense = self._numpy_dense()
        if dense is None:
            return 0
        numpy = dense['numpy']
        seuil = float(seuils.get('m3.voisins.cosinus'))
        rangs = [dense['rang'][u] for u in unites if u in dense['rang'] and u not in self._denses]
        bloc = max(1, int(_seuil('m3.voisins.bloc')))
        for debut in range(0, len(rangs), bloc):
            lot = rangs[debut:debut + bloc]
            scores = dense['M'][lot] @ dense['M'].T
            lignes, colonnes = numpy.nonzero(scores >= seuil)
            for rang in lot:
                self._denses[dense['ids'][rang]] = []
            for ligne, colonne in zip(lignes.tolist(), colonnes.tolist()):
                unite = dense['ids'][lot[ligne]]
                autre = dense['ids'][colonne]
                if autre != unite:
                    self._denses[unite].append((autre, float(scores[ligne, colonne])))
        return len(rangs)

    def liberer(self, unite):
        """Oublie les voisins denses precalcules d'une unite (memoire)."""
        self._denses.pop(unite, None)

    def _scores_denses(self, unite, admis, structurel):
        """Candidats denses admissibles ``{autre: cosinus}`` (numpy si possible, sinon Python pur)."""
        seuil_cosinus = float(seuils.get('m3.voisins.cosinus'))
        dense = self._numpy_dense()
        if dense is not None and unite in dense['rang']:
            if unite not in self._denses:
                self.preparer_denses([unite])
            return {autre: valeur for autre, valeur in self._denses[unite] if admis(autre)}
        vecteur = self.vecteur(unite)
        if structurel is not None:
            pool = [u for u in structurel if admis(u) and self.meta[u]['a_vecteur']]
        else:
            pool = [u for u, m in self.meta.items() if m['a_vecteur'] and admis(u)]
        self.charger_vecteurs(sorted(pool))
        scores = {}
        for autre in pool:
            vecteur_autre = self._vecteurs.get(autre)
            if not vecteur_autre:
                continue
            valeur = moteurs.cosinus(vecteur, vecteur_autre)
            if valeur >= seuil_cosinus:
                scores[autre] = valeur
        return scores

    def voisins(self, unite):
        """Voisins de ``unite`` hors de sa page d'origine (voir ``voisins``)."""
        if unite not in self.meta:
            raise KeyError('Unite inconnue ou exclue : ' + str(unite))
        page = self.meta[unite]['page']
        admis, filtres, structurel = self._admis(unite)
        seuil_lexical = float(_seuil('m3.voisins.lexical_min'))
        dense = self._numpy_dense() if self.meta[unite]['a_vecteur'] else None
        if self.meta[unite]['a_vecteur'] and ((dense is not None and unite in dense['rang']) or self.vecteur(unite)):
            filtres.append('dense')
            scores = self._scores_denses(unite, admis, structurel)
        else:
            filtres.append('lexical')
            scores = {autre: valeur for autre, valeur in self.lexical(unite).items()
                      if valeur >= seuil_lexical and admis(autre)}
        maximum = int(_seuil('m3.voisins.candidats_max'))
        candidats = sorted(scores, key=lambda u: (-scores[u], u))[:maximum]
        if not candidats:
            return []
        reordonnes, moteur = moteurs.reordonner(self.meta[unite]['texte'], [self.meta[u]['texte'] for u in candidats], avec_moteur=True)
        seuil_reordonnancement = float(seuils.get('m3.reranker.score_min'))
        resultat = []
        for autre, score in zip(candidats, reordonnes):
            if score < seuil_reordonnancement:
                continue
            if self.meta[autre]['page'] == page:
                continue
            resultat.append({
                'unit_id': autre, 'page_id': self.meta[autre]['page'], 'version_id': self.meta[autre]['version'],
                'score': round(float(score), 4), 'prefiltre': round(float(scores[autre]), 4),
                'filtres': list(filtres), 'moteur': moteur,
            })
        resultat.sort(key=lambda v: (-v['score'], v['unit_id']))
        return resultat[:int(seuils.get('m3.voisins.max'))]


def voisins(unit_id, index=None):
    """Voisins d'une unite hors de sa page d'origine (etape 1).

    Toutes les versions de la page d'origine sont exclues. Filtre structurel
    (entites canoniques, sujets des faits), filtre thematique (sujets fuzzy de
    la page), similarite dense (cosinus sur ``units.vector`` au-dessus de
    ``m3.voisins.cosinus``) ; sans vecteur, recouvrement lexical de termes
    porteurs comme filtre de candidats. Reordonnancement par
    ``moteurs.reordonner`` (cross-encoder, sinon replis), seuil
    ``m3.reranker.score_min``, au plus ``m3.voisins.max``.
    Renvoie une liste de dict(unit_id, page_id, version_id, score, prefiltre, filtres, moteur).
    """
    if index is None:
        index = Index()
    return index.voisins(unit_id)


# ---------------------------------------------------------------------------
# Etape 3 — Centralite et paternite
# ---------------------------------------------------------------------------

def _titres_page(texte):
    """Titres de section (Hn) reperes dans un texte brut : lignes « # », capitales ou courtes isolees."""
    lignes = (texte or '').replace('\r\n', '\n').split('\n')
    titres = []
    for index, ligne in enumerate(lignes):
        propre = ligne.strip()
        if not propre or index == 0 or len(propre) > 120:
            continue
        if propre.startswith('#'):
            titres.append(propre.lstrip('# '))
            continue
        if propre.startswith(('http://', 'https://')):
            continue
        lettres = [c for c in propre if c.isalpha()]
        if len(lettres) >= 4 and _RE_TITRE_MAJ.match(migration.sans_accents(propre).upper()) and propre == propre.upper():
            titres.append(propre)
            continue
        avant = lignes[index - 1].strip() if index > 0 else ''
        apres = lignes[index + 1].strip() if index + 1 < len(lignes) else ''
        if not avant and not apres and len(propre) <= 90 and propre[-1] not in '.!?;:,»"':
            titres.append(propre)
    return titres


def _present(nom, texte_normalise):
    """Le nom (normalise) figure-t-il, en mots entiers, dans le texte normalise ?"""
    cle = _cle(nom)
    if not cle:
        return False
    return (' ' + cle + ' ') in (' ' + texte_normalise + ' ')


def centralites(version_ids=None, forcer=False):
    """Calcule ``units.centrality`` (etape 3, sujet principal) pour des versions.

    Score = poids_titre x similarite(unite, titre + chapo) + poids_hn x presence
    de l'entite ou du predicat dans le titre ou un Hn + poids_dependances x part
    des unites de la page qui en dependent (``claim_types.depends_on``).
    Similarite : cosinus des embeddings si les unites ont des vecteurs et qu'un
    moteur d'embeddings repond, sinon recouvrement lexical. Sans ``forcer``,
    seules les unites sans centralite sont calculees. M5 reutilise la valeur.
    Renvoie dict(unites, versions, similarite).
    """
    with _execution('m3.centralite', moteurs={}, prompt='', seuils=['m3.centralite.poids_titre', 'm3.centralite.poids_hn', 'm3.centralite.poids_dependances']) as run:
        return _centralites(version_ids, forcer, run)


def _centralites(version_ids, forcer, run):
    """Centralite des unites des versions donnees (toutes si None)."""
    serveur = noyau.srv()
    if version_ids is None:
        condition = '' if forcer else ' WHERE u.centrality IS NULL'
        version_ids = [l['version_id'] for l in serveur.query('SELECT DISTINCT u.version_id FROM units u' + condition)]
    poids_titre = float(seuils.get('m3.centralite.poids_titre'))
    poids_hn = float(seuils.get('m3.centralite.poids_hn'))
    poids_dependances = float(seuils.get('m3.centralite.poids_dependances'))
    dependances = _dependances()
    compteurs = {'unites': 0, 'versions': 0, 'similarite': {}}
    for version_id in sorted(v for v in version_ids if v):
        _arret()
        version = serveur.query(
            'SELECT v.id, v.text, v.lede, v.meta, p.title, p.url FROM versions v LEFT JOIN pages p ON p.id=v.page_id WHERE v.id=?',
            (version_id,), True,
        )
        if not version:
            continue
        unites = serveur.query(
            "SELECT id, text, quote, centrality, (vector IS NOT NULL AND vector != '') AS a_vecteur FROM units "
            "WHERE version_id=? AND COALESCE(excluded,0)=0",
            (version_id,),
        )
        if not unites:
            continue
        if not forcer and all(u['centrality'] is not None for u in unites):
            continue
        titre, chapo, titres_hn = _structure_page(version)
        normalise_titres = _cle(' '.join([titre] + titres_hn))
        similarites, moteur = _similarites_titre(unites, titre + ' ' + chapo)
        compteurs['similarite'][moteur] = compteurs['similarite'].get(moteur, 0) + 1
        ids = [u['id'] for u in unites]
        noms = _noms_centraux(ids, unites)
        total = len(unites)
        mises_a_jour = []
        for unite in unites:
            presence = 1.0 if any(_present(nom, normalise_titres) for nom in noms.get(unite['id'], [])) else 0.0
            part = dependances.get(unite['id'], 0) / float(total)
            score = poids_titre * similarites[unite['id']] + poids_hn * presence + poids_dependances * part
            mises_a_jour.append((round(score, 6), unite['id']))
        with noyau.connexion() as base:
            base.executemany('UPDATE units SET centrality=? WHERE id=?', mises_a_jour)
        compteurs['unites'] += len(mises_a_jour)
        compteurs['versions'] += 1
        run.pages += 1
    return compteurs


def _structure_page(version):
    """Titre, chapo et titres de section d'une version."""
    serveur = noyau.srv()
    texte = version.get('text') or ''
    meta = {}
    try:
        meta = json.loads(version.get('meta') or '{}')
    except ValueError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    premiere = texte.strip().split('\n', 1)[0].strip() if texte.strip() else ''
    titre_page = meta.get('title') or version.get('title') or ''
    titre = (titre_page + ' ' + (premiere if len(premiere) <= 200 else '')).strip()
    chunks = serveur.query(
        'SELECT text, kind, section FROM chunks WHERE version_id=? ORDER BY position', (version['id'],),
    )
    titres_hn = [c['text'] for c in chunks if str(c.get('kind') or '') == 'titre' and c['text']]
    titres_hn += sorted({c['section'] for c in chunks if c.get('section')})
    if not titres_hn:
        titres_hn = _titres_page(texte)
    chapo = version.get('lede') or meta.get('description') or ''
    if not chapo:
        for chunk in chunks:
            contenu = (chunk['text'] or '').strip()
            if len(contenu) >= 120 and str(chunk.get('kind') or 'paragraphe') not in ('titre',):
                chapo = _couper_phrase(contenu, int(_seuil('m3.centralite.chapo_caracteres')))
                break
    return titre, chapo, titres_hn


def _couper_phrase(texte, limite):
    """Debut de ``texte`` d'au plus ``limite`` caracteres, coupe en fin de phrase si possible (jamais au milieu d'un mot)."""
    if len(texte) <= limite:
        return texte
    debut = texte[:limite]
    fin = max(debut.rfind('. '), debut.rfind('! '), debut.rfind('? '))
    if fin > 0:
        return debut[:fin + 1]
    return debut.rsplit(' ', 1)[0]


def _similarites_titre(unites, reference):
    """Similarite unite / titre + chapo : cosinus si vecteurs et moteur, sinon recouvrement lexical."""
    if unites and all(u['a_vecteur'] for u in unites):
        index = {}
        for lot in _par_lots([u['id'] for u in unites]):
            for ligne in noyau.srv().query('SELECT id, vector FROM units WHERE id IN (' + _marques(lot) + ')', tuple(lot)):
                try:
                    index[ligne['id']] = [float(x) for x in json.loads(ligne['vector'])]
                except (ValueError, TypeError):
                    index[ligne['id']] = None
        vecteurs, moteur = moteurs.embeddings([reference])
        if vecteurs and moteur != 'aucun':
            dimension = len(vecteurs[0])
            if all(index.get(u['id']) and len(index[u['id']]) == dimension for u in unites):
                return {u['id']: max(0.0, moteurs.cosinus(vecteurs[0], index[u['id']])) for u in unites}, 'cosinus:' + moteur
    mots_reference = set(moteurs.mots(reference))
    resultat = {}
    for unite in unites:
        mots_unite = set(moteurs.mots(unite['text'] or unite['quote'] or ''))
        resultat[unite['id']] = (len(mots_unite & mots_reference) / float(len(mots_unite))) if mots_unite else 0.0
    return resultat, 'lexical'


def _noms_centraux(ids, unites):
    """Noms a chercher dans le titre et les Hn : entites, sujets et predicats des faits, sinon entites par regles."""
    entites = _entites_unites(ids)
    faits = _faits(ids)
    resultat = {}
    for unite in unites:
        noms = []
        for entite in entites.get(unite['id'], []):
            if str(entite.get('type') or '') in ENTITES_NUMERIQUES:
                continue
            if entite.get('name'):
                noms.append(entite['name'])
            alias = entite.get('aliases')
            if alias:
                try:
                    liste = json.loads(alias)
                except ValueError:
                    liste = [a.strip() for a in str(alias).split(',')]
                if isinstance(liste, list):
                    noms.extend(str(a) for a in liste if a)
        for fait in faits.get(unite['id'], []):
            if fait.get('subject'):
                noms.append(fait['subject'])
            libelle = _libelle_predicat(fait.get('predicate'))
            if libelle:
                noms.append(libelle)
        if not noms:
            texte = unite['text'] or unite['quote'] or ''
            noms = [e['text'] for e in moteurs.entites_regles(texte) if e['type'] not in ENTITES_NUMERIQUES]
        resultat[unite['id']] = noms
    return resultat


def _libelle_predicat(predicat):
    """Libelle du predicat (``chaine.predicats``), ou None."""
    if not predicat:
        return None
    try:
        from chaine import predicats
        fiche = predicats.get(predicat)
    except Exception:
        return None
    if isinstance(fiche, dict):
        return fiche.get('libelle')
    return None


def _dependances():
    """Nombre d'unites qui dependent de chaque unite (``claim_types.depends_on``)."""
    compte = {}
    for ligne in noyau.srv().query("SELECT unit_id, depends_on FROM claim_types WHERE depends_on IS NOT NULL AND depends_on != ''"):
        brut = ligne['depends_on']
        try:
            liste = json.loads(brut)
        except ValueError:
            liste = [x.strip() for x in str(brut).split(',')]
        if isinstance(liste, str):
            liste = [liste]
        if not isinstance(liste, list):
            continue
        for cible in set(str(x) for x in liste if x):
            if cible != ligne['unit_id']:
                compte[cible] = compte.get(cible, 0) + 1
    return compte


def paternite(group_id, forcer=True):
    """Designe l'unite parente d'un groupe (etape 3) et l'enregistre dans ``paternity``.

    Ordre lexicographique strict : (1) sujet principal (``units.centrality`` ;
    un ecart sous ``m3.centralite.ecart`` est une egalite), (2) nombre de
    sources M2 propres dedupliquees par domaine, (3) date de publication la
    plus ancienne, (4) egalite persistante -> ``a_arbitrer``. Un critere
    decide des qu'il ne reste qu'une page. Groupe a un membre : parent trivial.
    Une decision humaine (``human=1``) n'est jamais ecrasee.
    Renvoie la ligne enregistree (dict).
    """
    _preparer()
    with _execution('m3.paternite', moteurs={'paternite': 'regles'}, prompt='', seuils=['m3.centralite.ecart']) as run:
        _paternite_lot([group_id], run, forcer=forcer)
    ligne = noyau.srv().query('SELECT * FROM paternity WHERE group_id=?', (group_id,), True)
    if ligne and ligne.get('detail'):
        ligne['detail'] = json.loads(ligne['detail'])
    return ligne


def _membres(group_ids):
    """Membres des groupes avec page, date de publication, centralite, position."""
    serveur = noyau.srv()
    resultat = {}
    for lot in _par_lots(group_ids):
        lignes = serveur.query(
            'SELECT m.group_id, u.id, u.version_id, u.text, u.quote, u.span_start, u.centrality, u.anchor_date, '
            'u.source_manquante, v.page_id, v.published FROM unit_group_members m JOIN units u ON u.id=m.unit_id '
            'LEFT JOIN versions v ON v.id=u.version_id WHERE m.group_id IN (' + _marques(lot) + ')',
            tuple(lot),
        )
        for ligne in lignes:
            ligne['page'] = ligne['page_id'] or ('version:' + str(ligne['version_id']))
            resultat.setdefault(ligne['group_id'], []).append(ligne)
    return resultat


def _sources_membres(group_ids):
    """Toutes les sources des unites membres : unite -> liste de lignes."""
    serveur = noyau.srv()
    resultat = {}
    for lot in _par_lots(group_ids):
        lignes = serveur.query(
            'SELECT DISTINCT s.* FROM sources s JOIN unit_group_members m ON m.unit_id=s.unit_id '
            'WHERE m.group_id IN (' + _marques(lot) + ')',
            tuple(lot),
        )
        for ligne in lignes:
            resultat.setdefault(ligne['unit_id'], []).append(ligne)
    return resultat


def _paternite_lot(group_ids, run, forcer=False):
    """Paternite d'un ensemble de groupes, ecrite en une transaction par tranche."""
    serveur = noyau.srv()
    ecart = float(seuils.get('m3.centralite.ecart'))
    compteurs = {'groupes': 0, 'par_critere': {}, 'triviaux': 0, 'page_unique': 0, 'multi_pages': 0,
                 'humains_preserves': 0, 'deja_faits': 0, 'vides': 0}
    for tranche in _par_lots(sorted(set(group_ids)), 500):
        _arret()
        existantes = {}
        for ligne in serveur.query('SELECT group_id, human, run_id FROM paternity WHERE group_id IN (' + _marques(tranche) + ')', tuple(tranche)):
            existantes[ligne['group_id']] = ligne
        membres = _membres(tranche)
        a_calculer_versions = {m['version_id'] for liste in membres.values() for m in liste if m['centrality'] is None and len(liste) > 1}
        if a_calculer_versions:
            _centralites(sorted(v for v in a_calculer_versions if v), False, run)
            membres = _membres(tranche)
        sources = _sources_membres(tranche)
        lignes = []
        cadres_a_remplir = []
        for group_id in tranche:
            existante = existantes.get(group_id)
            if existante and existante['human']:
                compteurs['humains_preserves'] += 1
                continue
            if existante and not forcer:
                compteurs['deja_faits'] += 1
                continue
            liste = membres.get(group_id, [])
            if not liste:
                compteurs['vides'] += 1
                continue
            parent, critere, detail = _designer(liste, sources, ecart)
            compteurs['groupes'] += 1
            compteurs['par_critere'][critere] = compteurs['par_critere'].get(critere, 0) + 1
            if detail.get('trivial') == 'membre_unique':
                compteurs['triviaux'] += 1
            elif detail.get('pages') == 1:
                compteurs['page_unique'] += 1
            else:
                compteurs['multi_pages'] += 1
            lignes.append((group_id, parent, critere, _json(detail), 0, run.id, serveur.now()))
            if parent:
                cadres_a_remplir.append((group_id, parent))
        with noyau.connexion() as base:
            base.executemany(
                'INSERT OR REPLACE INTO paternity(group_id,parent_unit_id,criterion,detail,human,run_id,created) VALUES(?,?,?,?,?,?,?)',
                lignes,
            )
        _remplir_cadres(cadres_a_remplir)
        run.items += len(lignes)
    return compteurs


def _designer(liste, sources, ecart):
    """Applique l'ordre lexicographique ; renvoie (parent, critere, detail)."""
    pages = sorted({m['page'] for m in liste})
    detail = {'pages': len(pages), 'membres': len(liste)}
    if len(liste) == 1:
        detail['trivial'] = 'membre_unique'
        return liste[0]['id'], 'sujet_principal', detail
    restants = list(liste)
    # (1) Sujet principal : centralite, ecart sous le seuil = egalite.
    scores = {m['id']: float(m['centrality'] or 0.0) for m in restants}
    meilleur = max(scores.values())
    detail['centralite'] = {uid: round(v, 4) for uid, v in scores.items()}
    detail['ecart_egalite'] = ecart
    restants = [m for m in restants if meilleur - scores[m['id']] < ecart or meilleur == scores[m['id']]]
    if len({m['page'] for m in restants}) == 1:
        if len(pages) == 1:
            detail['page_unique'] = True
        return _meilleur_de_page(restants), 'sujet_principal', detail
    # (2) Nombre de sources distinctes de M2, dedupliquees par domaine.
    nombres = {}
    confiance_min = float(seuils.get('m2.confiance_min'))
    for membre in restants:
        cles = {_cle_domaine(s) for s in sources.get(membre['id'], []) if not s.get('inherited_from') and s.get('kind') in SOURCES_M2
                and (s.get('confidence') is None or float(s['confidence']) >= confiance_min)}
        nombres[membre['id']] = len(cles)
    detail['sources'] = nombres
    maximum = max(nombres.values())
    restants = [m for m in restants if nombres[m['id']] == maximum]
    if len({m['page'] for m in restants}) == 1:
        return _meilleur_de_page(restants), 'nombre_sources', detail
    # (3) Date de publication la plus ancienne (toutes connues, sinon indeterminable).
    dates = {m['id']: _iso(m['published']) for m in restants}
    detail['dates'] = dates
    if all(dates.values()):
        plus_ancienne = min(dates.values())
        restants = [m for m in restants if dates[m['id']] == plus_ancienne]
        if len({m['page'] for m in restants}) == 1:
            return _meilleur_de_page(restants), 'date_publication', detail
    else:
        detail['dates_inconnues'] = sorted(uid for uid, d in dates.items() if not d)
    # (4) Egalite persistante : arbitrage humain dans l'interface de revue.
    detail['candidats'] = sorted(m['id'] for m in restants)
    return None, 'a_arbitrer', detail


def _meilleur_de_page(restants):
    """Unite parente au sein de la page retenue : centralite, puis premiere position."""
    ordre = sorted(restants, key=lambda m: (-float(m['centrality'] or 0.0), m['span_start'] if m['span_start'] is not None else 10 ** 9, m['id']))
    return ordre[0]['id']


def _remplir_cadres(couples):
    """Renseigne ``unit_groups.frame`` (vide) a partir du cadre de l'unite parente."""
    if not couples:
        return
    serveur = noyau.srv()
    groupes = [g for g, _ in couples]
    vides = set()
    for lot in _par_lots(groupes):
        for ligne in serveur.query("SELECT id FROM unit_groups WHERE id IN (" + _marques(lot) + ") AND (frame IS NULL OR frame='')", tuple(lot)):
            vides.add(ligne['id'])
    couples = [(g, p) for g, p in couples if g in vides]
    if not couples:
        return
    parents = [p for _, p in couples]
    unites = _unites(parents)
    faits = _faits(parents)
    entites = _entites_unites(parents)
    mises_a_jour = []
    for group_id, parent in couples:
        if parent not in unites:
            continue
        valeur = _cadre(unites[parent], faits.get(parent, []), entites.get(parent, []))
        mises_a_jour.append((_json({k: valeur[k] for k in ('sujet', 'predicat', 'portee', 'periode')}), group_id))
    with noyau.connexion() as base:
        base.executemany("UPDATE unit_groups SET frame=? WHERE id=? AND (frame IS NULL OR frame='')", mises_a_jour)


def arbitrer(group_id, unit_id):
    """Enregistre l'arbitrage humain de la paternite (``human=1``) puis repropage.

    L'unite doit appartenir au groupe. Un recalcul ne remplace jamais cette
    decision. Renvoie la ligne ``paternity``.
    """
    _preparer()
    serveur = noyau.srv()
    membre = serveur.query('SELECT 1 FROM unit_group_members WHERE group_id=? AND unit_id=?', (group_id, unit_id), True)
    if not membre:
        raise ValueError("L'unite " + str(unit_id) + " n'appartient pas au groupe " + str(group_id) + '.')
    precedente = serveur.query('SELECT parent_unit_id, criterion FROM paternity WHERE group_id=?', (group_id,), True) or {}
    detail = {'precedent': precedente.get('parent_unit_id'), 'critere_precedent': precedente.get('criterion')}
    with _execution('m3.arbitrage', moteurs={'paternite': 'humain'}, prompt='', seuils={}) as run:
        serveur.query(
            'INSERT OR REPLACE INTO paternity(group_id,parent_unit_id,criterion,detail,human,run_id,created) VALUES(?,?,?,?,?,?,?)',
            (group_id, unit_id, 'arbitrage_humain', _json(detail), 1, run.id, serveur.now()),
        )
        _propager_lot([group_id], run)
    return paternite_de(group_id)


def paternite_de(group_id):
    """Ligne ``paternity`` d'un groupe (detail decode), ou None."""
    ligne = noyau.srv().query('SELECT * FROM paternity WHERE group_id=?', (group_id,), True)
    if ligne and ligne.get('detail'):
        try:
            ligne['detail'] = json.loads(ligne['detail'])
        except ValueError:
            pass
    return ligne


# ---------------------------------------------------------------------------
# Etape 4 — Propagation des sources
# ---------------------------------------------------------------------------

def propager(group_id):
    """Propage les sources d'un groupe selon sa paternite (etape 4). Idempotent.

    Vers le parent : sources propres des enfants qui le precisent
    (``precise``), le corrigent (``corrige``) ou lui sont equivalents
    (``equivalente``, reglage ``m3.propagation.equivalentes``) ;
    ``inherited_from`` = enfant d'origine, ``contribution`` = type.
    Vers les enfants : sources du parent, propagees comprises ;
    ``inherited_from`` = parent, ``contribution='heritee'``.
    Enfant d'une autre page sans lien vers la page du parent :
    ``link_recommendations`` ancree sur la phrase de l'unite.
    Identifiants deterministes (``m3:<groupe>:...``) : aucun doublon au recalcul.
    """
    _preparer()
    with _execution('m3.propagation', moteurs={'propagation': 'regles'}, prompt='', seuils=_seuils_utilises(['m3.propagation.equivalentes'])) as run:
        return _propager_lot([group_id], run)


def _relations_membres(group_ids):
    """Relations entre membres d'un meme groupe : (u1, u2) -> ligne."""
    serveur = noyau.srv()
    resultat = {}
    for lot in _par_lots(group_ids):
        lignes = serveur.query(
            'SELECT DISTINCT r.a, r.b, r.relation, r.direction FROM unit_relations r '
            'JOIN unit_group_members ma ON ma.unit_id=r.a JOIN unit_group_members mb ON mb.unit_id=r.b AND mb.group_id=ma.group_id '
            'WHERE ma.group_id IN (' + _marques(lot) + ')',
            tuple(lot),
        )
        for ligne in lignes:
            resultat[(ligne['a'], ligne['b'])] = ligne
            resultat[(ligne['b'], ligne['a'])] = ligne
    return resultat


def _urls_versions(version_ids):
    """URL publiques normalisees de la page de chaque version (URL, meta, premieres lignes du texte)."""
    serveur = noyau.srv()
    resultat = {}
    for lot in _par_lots(version_ids):
        lignes = serveur.query(
            'SELECT v.id, v.meta, substr(v.text,1,600) AS debut, p.url FROM versions v LEFT JOIN pages p ON p.id=v.page_id '
            'WHERE v.id IN (' + _marques(lot) + ')',
            tuple(lot),
        )
        for ligne in lignes:
            urls = set()
            if _normaliser_url(ligne['url']):
                urls.add(_normaliser_url(ligne['url']))
            try:
                meta = json.loads(ligne['meta'] or '{}')
            except ValueError:
                meta = {}
            if isinstance(meta, dict):
                for cle in ('url', 'canonical', 'canonique'):
                    if _normaliser_url(meta.get(cle)):
                        urls.add(_normaliser_url(meta.get(cle)))
            for trouvee in re.findall(r'https?://[^\s<>"\')]+', ligne['debut'] or '')[:1]:
                if _normaliser_url(trouvee):
                    urls.add(_normaliser_url(trouvee))
            resultat[ligne['id']] = urls
    return resultat


def _contribution(enfant, parent, relations, equivalentes):
    """Type de contribution d'un enfant au parent, ou None."""
    relation = relations.get((enfant, parent))
    if relation is None:
        # Groupe issu de la deduplication (ou migre) sans relation qualifiee : equivalence.
        return 'equivalente' if equivalentes else None
    nature = relation['relation']
    if nature == 'equivalence':
        return 'equivalente' if equivalentes else None
    if nature == 'precision':
        return 'precise' if relation['direction'] == enfant else None
    if nature == 'remplacement':
        return 'corrige' if relation['direction'] == enfant else None
    return None


def _propager_lot(group_ids, run):
    """Propagation pour un ensemble de groupes (tranches, une transaction chacune)."""
    serveur = noyau.srv()
    equivalentes = bool(_seuil('m3.propagation.equivalentes'))
    compteurs = {'groupes': 0, 'vers_parent': 0, 'vers_enfants': 0, 'supprimees': 0, 'recommandations': 0,
                 'recommandations_retirees': 0, 'non_attribues': 0}
    for tranche in _par_lots(sorted(set(group_ids)), 500):
        _arret()
        paternites = {l['group_id']: l for l in serveur.query(
            'SELECT group_id, parent_unit_id, criterion FROM paternity WHERE group_id IN (' + _marques(tranche) + ')', tuple(tranche))}
        membres = _membres(tranche)
        multi = [g for g in tranche if len(membres.get(g, [])) > 1]
        sources = _sources_membres(multi)
        relations = _relations_membres(multi)
        versions = {m['version_id'] for g in multi for m in membres[g] if m['version_id']}
        urls = _urls_versions(sorted(versions))
        existantes = {}
        for lot in _par_lots(tranche):
            for ligne in serveur.query(
                "SELECT id, unit_id FROM sources WHERE substr(id,1,3)='m3:' AND unit_id IN "
                "(SELECT unit_id FROM unit_group_members WHERE group_id IN (" + _marques(lot) + "))", tuple(lot),
            ):
                groupe = ligne['id'][3:].rsplit(':', 1)[0]
                existantes.setdefault(groupe, set()).add(ligne['id'])
        recos = {}
        for lot in _par_lots(tranche):
            for ligne in serveur.query(
                "SELECT id, group_id FROM link_recommendations WHERE status='proposee' AND group_id IN (" + _marques(lot) + ")", tuple(lot),
            ):
                recos.setdefault(ligne['group_id'], set()).add(ligne['id'])
        insertions = []
        suppressions = []
        nouvelles_recos = []
        recos_a_retirer = []
        horodatage = serveur.now()
        for group_id in tranche:
            paternite_groupe = paternites.get(group_id)
            parent = paternite_groupe['parent_unit_id'] if paternite_groupe else None
            liste = membres.get(group_id, [])
            voulues = {}
            recos_voulues = {}
            if not parent or parent not in {m['id'] for m in liste}:
                if len(liste) > 1:
                    compteurs['non_attribues'] += 1
            elif len(liste) > 1:
                voulues, recos_voulues = _plan_propagation(group_id, parent, liste, sources, relations, urls, equivalentes, run.id, horodatage)
            compteurs['groupes'] += 1
            for identifiant in existantes.get(group_id, set()) - set(voulues):
                suppressions.append((identifiant,))
            for identifiant, ligne in voulues.items():
                if identifiant not in existantes.get(group_id, set()):
                    insertions.append(ligne)
                    if ligne[9] == 'heritee':
                        compteurs['vers_enfants'] += 1
                    else:
                        compteurs['vers_parent'] += 1
            for identifiant in recos.get(group_id, set()) - set(recos_voulues):
                recos_a_retirer.append((identifiant,))
            for identifiant, ligne in recos_voulues.items():
                if identifiant not in recos.get(group_id, set()):
                    nouvelles_recos.append(ligne)
        with noyau.connexion() as base:
            base.executemany('DELETE FROM sources WHERE id=?', suppressions)
            base.executemany(
                'INSERT OR IGNORE INTO sources(id,unit_id,url,domain,cited_entity,kind,scope,confidence,inherited_from,'
                'contribution,source_date,passage,run_id,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                insertions,
            )
            base.executemany("DELETE FROM link_recommendations WHERE id=? AND status='proposee'", recos_a_retirer)
            curseur = base.executemany(
                'INSERT OR IGNORE INTO link_recommendations(id,unit_id,from_version,to_version,anchor,reason,status,created,run_id,group_id) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                nouvelles_recos,
            )
            compteurs['recommandations'] += max(0, curseur.rowcount)
        compteurs['supprimees'] += len(suppressions)
        compteurs['recommandations_retirees'] += len(recos_a_retirer)
        run.items += len(insertions)
    return compteurs


def _plan_propagation(group_id, parent, liste, sources, relations, urls, equivalentes, run_id, horodatage):
    """Lignes de sources et de recommandations voulues pour un groupe."""
    prefixe = 'm3:' + group_id + ':'
    par_id = {m['id']: m for m in liste}
    membre_parent = par_id[parent]
    propres = {m['id']: [s for s in sources.get(m['id'], []) if not s.get('inherited_from')] for m in liste}
    cles_parent = {_cle_source(s) for s in propres[parent]}
    voulues = {}
    # Vers le parent : sources propres des enfants qui l'ameliorent.
    remontees = []
    for enfant in sorted(par_id):
        if enfant == parent:
            continue
        contribution = _contribution(enfant, parent, relations, equivalentes)
        if not contribution:
            continue
        for source in propres[enfant]:
            cle = _cle_source(source)
            if cle in cles_parent:
                continue
            cles_parent.add(cle)
            identifiant = prefixe + _id('parent', parent, enfant, cle)
            ligne = (identifiant, parent, source.get('url'), source.get('domain'), source.get('cited_entity'), source.get('kind'),
                     source.get('scope'), source.get('confidence'), enfant, contribution, source.get('source_date'),
                     source.get('passage'), run_id, horodatage)
            voulues[identifiant] = ligne
            remontees.append((cle, enfant, ligne))
    # Vers les enfants : sources du parent, propagees comprises.
    reserve = [(_cle_source(s), None, s) for s in propres[parent]]
    reserve += [(cle, origine, None, ligne) for cle, origine, ligne in remontees]
    for enfant in sorted(par_id):
        if enfant == parent:
            continue
        deja = {_cle_source(s) for s in propres[enfant]}
        for element in reserve:
            cle = element[0]
            origine = element[1]
            if origine == enfant or cle in deja:
                continue
            deja.add(cle)
            if len(element) == 3:
                source = element[2]
                valeurs = (source.get('url'), source.get('domain'), source.get('cited_entity'), source.get('kind'),
                           source.get('scope'), source.get('confidence'), source.get('source_date'), source.get('passage'))
            else:
                ligne = element[3]
                valeurs = (ligne[2], ligne[3], ligne[4], ligne[5], ligne[6], ligne[7], ligne[10], ligne[11])
            identifiant = prefixe + _id('enfant', parent, enfant, cle)
            voulues[identifiant] = (identifiant, enfant, valeurs[0], valeurs[1], valeurs[2], valeurs[3], valeurs[4], valeurs[5],
                                    parent, 'heritee', valeurs[6], valeurs[7], run_id, horodatage)
    # Lien manquant : enfant d'une autre page sans lien vers la page du parent.
    recos = {}
    urls_parent = urls.get(membre_parent['version_id'], set())
    for enfant in sorted(par_id):
        if enfant == parent:
            continue
        membre = par_id[enfant]
        if membre['page'] == membre_parent['page']:
            continue
        liens = {_normaliser_url(s.get('url')) for s in propres[enfant] if s.get('url')}
        if urls_parent and liens & urls_parent:
            continue
        identifiant = _id('lien', enfant, membre_parent['version_id'])
        motif = {
            'motif': 'source_manquante' if membre.get('source_manquante') else 'lien_parent_absent',
            'parent': parent, 'groupe': group_id, 'url_parent': sorted(urls_parent)[:1],
        }
        ancre = membre.get('quote') or membre.get('text') or ''
        recos[identifiant] = (identifiant, enfant, membre['version_id'], membre_parent['version_id'], ancre,
                              _json(motif), 'proposee', horodatage, run_id, group_id)
    return voulues, recos


# ---------------------------------------------------------------------------
# Etape 5 — Statut temporel
# ---------------------------------------------------------------------------

def _fin_annonce(texte):
    """Date de fin d'une annonce lue dans le texte (repli si M1 n'a pas pose ``expires``)."""
    dates = migration.lire_dates(texte or '')
    if not dates:
        return None
    fins = []
    for iso, precision in dates:
        jour = date.fromisoformat(iso)
        if precision == 'mois':
            suivant = date(jour.year + (jour.month == 12), jour.month % 12 + 1, 1)
            jour = suivant - timedelta(days=1)
        elif precision == 'annee':
            jour = date(jour.year, 12, 31)
        fins.append(jour)
    return max(fins).isoformat()


def statuts_temporels(aujourdhui=None):
    """Recalcule ``units.temporal_status`` (etape 5) ; renvoie les effectifs par statut.

    Par defaut ``a_jour`` ; ``perimee_par_date`` si la date de peremption
    (``expires``, sinon fin d'annonce lue dans le texte pour la classe
    ``annonce``) est passee au regard de la date du jour ; ``remise_en_question``
    pour les unites en conflit ; ``obsolete_historique`` pour l'ancienne unite
    d'un remplacement. Priorite croissante dans cet ordre.
    """
    jour = _date_du_jour(aujourdhui).isoformat()
    serveur = noyau.srv()
    with _execution('m3.statuts', moteurs={'statuts': 'regles'}, prompt='', seuils={'aujourdhui': jour}) as run:
        annonces = serveur.query(
            "SELECT id, text, quote FROM units WHERE obs_class='annonce' AND (expires IS NULL OR expires='')"
        )
        perimees_texte = []
        for unite in annonces:
            fin = _fin_annonce(unite['text'] or unite['quote'] or '')
            if fin and fin < jour:
                perimees_texte.append((unite['id'],))
        with noyau.connexion() as base:
            # Cette execution recalcule le statut temporel de toutes les unites : elle en devient l'auteur (temporal_run).
            base.execute("UPDATE units SET temporal_status='a_jour', temporal_run=?", (run.id,))
            base.execute(
                "UPDATE units SET temporal_status='perimee_par_date' WHERE expires IS NOT NULL AND expires!='' AND substr(expires,1,10)<?",
                (jour,),
            )
            base.executemany("UPDATE units SET temporal_status='perimee_par_date' WHERE id=?", perimees_texte)
            # Seules les relations qualifiees par M3 (ou un humain) comptent : un « conflit » de la deduplication
            # n'a pas vu le cadre (piege du PDG).
            base.execute(
                "UPDATE units SET temporal_status='remise_en_question' WHERE id IN "
                "(SELECT a FROM unit_relations WHERE relation='conflit' AND NOT " + CLAUSE_DEDUP + " "
                "UNION SELECT b FROM unit_relations WHERE relation='conflit' AND NOT " + CLAUSE_DEDUP + ")"
            )
            base.execute(
                "UPDATE units SET temporal_status='obsolete_historique' WHERE id IN "
                "(SELECT CASE WHEN direction=a THEN b ELSE a END FROM unit_relations WHERE relation='remplacement' "
                "AND direction IS NOT NULL AND NOT " + CLAUSE_DEDUP + ")"
            )
        lignes = serveur.query('SELECT temporal_status, count(*) n FROM units GROUP BY temporal_status')
        effectifs = {statut: 0 for statut in STATUTS_TEMPORELS}
        for ligne in lignes:
            effectifs[ligne['temporal_status'] or 'a_jour'] = effectifs.get(ligne['temporal_status'] or 'a_jour', 0) + ligne['n']
        run.items = sum(effectifs.values())
        run.note = _json(effectifs)
    return effectifs


# ---------------------------------------------------------------------------
# Execution complete
# ---------------------------------------------------------------------------

def executer(forcer=False, aujourdhui=None, qualifier_paires=True, paires_max=None):
    """Execute M3 (etapes 1 a 5) ; reprenable ; renvoie un dict de compteurs.

    Sans ``forcer`` : les paires deja qualifiees, les paternites deja
    calculees et les centralites deja posees sont conservees. Les decisions
    humaines ne sont jamais remplacees. Si aucun moteur NLI n'est disponible,
    les etapes 1-2 s'arretent proprement (compteur ``nli_indisponible``) et
    les etapes 3 a 5 s'executent quand meme. ``qualifier_paires=False`` saute
    les etapes 1-2 ; ``paires_max`` plafonne le nombre de paires qualifiees.
    """
    _preparer()
    serveur = noyau.srv()
    compteurs = {}
    if qualifier_paires:
        compteurs['qualification'] = _executer_voisins(forcer, paires_max)
    groupes = [l['id'] for l in serveur.query(
        'SELECT g.id FROM unit_groups g WHERE EXISTS(SELECT 1 FROM unit_group_members m WHERE m.group_id=g.id) ORDER BY g.id')]
    noms = ['m3.centralite.ecart', 'm3.centralite.poids_titre', 'm3.centralite.poids_hn', 'm3.centralite.poids_dependances']
    with noyau.etape('m3.paternite', moteurs={'paternite': 'regles'}, prompt='', seuils=noms, items=0) as run:
        _job(stage='m3.paternite', done=0, total=len(groupes), message='Centralite et paternite')
        compteurs['centralite'] = _centralites(None, forcer, run)
        compteurs['paternite'] = _paternite_lot(groupes, run, forcer=forcer)
        _job(done=len(groupes))
    with noyau.etape('m3.propagation', moteurs={'propagation': 'regles'}, prompt='', seuils=_seuils_utilises(['m3.propagation.equivalentes'])) as run:
        _job(stage='m3.propagation', done=0, total=len(groupes), message='Propagation des sources')
        compteurs['propagation'] = _propager_lot(groupes, run)
        _job(done=len(groupes))
    _job(stage='m3.statuts', message='Statuts temporels')
    compteurs['statuts'] = statuts_temporels(aujourdhui)
    _job(message='M3 terminee')
    return compteurs


def relations_a_requalifier():
    """Paires ``(a, b)`` dont la relation vient de la deduplication de M1 (NLI seul) et vaut conflit ou precision.

    M3 les requalifie (table de l'etape 2 : cadre et ancrages) ; tant qu'elles ne le sont pas,
    ``statuts_temporels`` les ignore.
    """
    lignes = noyau.srv().query(
        "SELECT a, b FROM unit_relations WHERE relation IN ('conflit','precision') AND " + CLAUSE_DEDUP + " ORDER BY a, b")
    return [(ligne['a'], ligne['b']) for ligne in lignes]


def _executer_voisins(forcer, paires_max):
    """Etapes 1-2 sur toutes les unites eligibles."""
    serveur = noyau.srv()
    compteurs = {'unites': 0, 'paires': 0, 'qualifiees': 0, 'deja_qualifiees': 0, 'relations': {}}
    noms = ['m3.voisins.cosinus', 'm3.voisins.max', 'm3.reranker.score_min', 'nli.implication', 'm3.zone_grise',
            'm3.nettement_plus_recent_jours']
    with noyau.etape('m3.voisins', moteurs={}, prompt='m3-cadre-v1', seuils=_seuils_utilises(noms + ['m3.voisins.lexical_min', 'm3.voisins.df_max', 'm3.recent.facteur_volatilite'])) as run:
        index = Index()
        eligibles = sorted(
            l['id'] for l in serveur.query(
                "SELECT id FROM units WHERE COALESCE(excluded,0)=0 AND COALESCE(obs_class,'') NOT IN ('usage')")
            if l['id'] in index.meta
        )
        existantes = set()
        if not forcer:
            for ligne in serveur.query('SELECT a, b FROM unit_relations'):
                existantes.add((min(ligne['a'], ligne['b']), max(ligne['a'], ligne['b'])))
        # Relations posees par la deduplication de M1 d'apres le NLI seul : un « conflit » ou une « precision » doit
        # etre requalifie ici (cadre, ancrages ; piege du PDG) et les precisions rattachees au meme groupe.
        a_requalifier = relations_a_requalifier()
        compteurs['requalifiees_dedup'] = len(a_requalifier)
        _job(stage='m3.voisins', done=0, total=len(eligibles), message='Recherche des voisins')
        paires = list(a_requalifier)
        vues = set(a_requalifier)
        bloc = max(1, int(_seuil('m3.voisins.bloc')))
        for position, unite in enumerate(eligibles):
            if position % 200 == 0:
                _arret()
                _job(done=position)
            if position % bloc == 0:
                # Voisins denses du bloc suivant en un produit matriciel (numpy) ; liberes apres usage.
                index.preparer_denses(eligibles[position:position + bloc])
            voisins_unite = index.voisins(unite)
            index.liberer(unite)
            for voisin in voisins_unite:
                cle = (min(unite, voisin['unit_id']), max(unite, voisin['unit_id']))
                if cle in vues:
                    continue
                vues.add(cle)
                if cle in existantes:
                    compteurs['deja_qualifiees'] += 1
                    continue
                paires.append(cle)
            run.moteur('reordonner', moteurs.dernier('reordonner'))
        compteurs['unites'] = len(eligibles)
        if paires_max is not None:
            paires = paires[:int(paires_max)]
        compteurs['paires'] = len(paires)
        taille = int(_seuil('m3.qualification.lot'))
        _job(stage='m3.qualification', done=0, total=len(paires), message='Qualification des paires')
        for debut in range(0, len(paires), taille):
            _arret()
            lot = paires[debut:debut + taille]
            try:
                resultats = _qualifier_lot(lot, run)
            except Exception as erreur:
                if _arret_demande() or not isinstance(erreur, (getattr(serveur, 'OperationalError', RuntimeError), RuntimeError)):
                    raise
                compteurs['nli_indisponible'] = str(erreur)[:300]
                break
            for resultat in resultats:
                compteurs['relations'][resultat['relation']] = compteurs['relations'].get(resultat['relation'], 0) + 1
            compteurs['qualifiees'] += len(resultats)
            run.items += len(resultats)
            _job(done=debut + len(lot))
    return compteurs
