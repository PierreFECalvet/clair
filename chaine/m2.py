"""M2 : identifier les sources et typer les affirmations non sourcees.

Section M2 de docs/METHODOLOGIES.txt. S'applique aux seules unites
obsolescibles (classes ``etat_courant`` et ``annonce`` ; les unites migrees
dont la classe est encore ``inconnu`` ou vide sont traitees aussi).

Etape 1, ``sources_explicites`` : trois signaux deterministes (liens,
attributions textuelles croisees avec la NER, appareil de citation), rattaches
a l'unite si l'indice tombe dans la meme phrase, sinon au paragraphe (confiance
moindre), sinon a la page (encadre « Sources » rapproche par un nom rare).
Etape 2, ``typer`` : une unite qui cite une source est « sourcee_externe » ou
« sourcee_interne » ; les autres passent la cascade du tableau du document
(information_source, opinion, interpretation, connaissance_generale, sans_source),
la premiere regle assez confiante l'emporte. Etape 3, ``signaler_manquantes`` :
drapeau ``units.source_manquante``.

Hierarchie des moyens : regles, puis classifieurs locaux (``moteurs.classer``,
``moteurs.nli_lot``), puis LLM local contraint par schema pour la seule
question fermee de la connaissance generale. Aucun appel reseau, aucune
dependance obligatoire.

Bornes et choix documentes
--------------------------
* Test NLI d'interpretation (peut exploser combinatoirement) : pour une unite
  testee, candidats = unites SOURCEES de la meme section, classees par
  recouvrement lexical avec l'unite ; on garde les ``m2.interpretation.candidats_max``
  (4) meilleurs, on forme leurs paires (6 au plus), et on ne soumet au NLI que
  les ``m2.interpretation.paires_max`` (3) meilleures paires (somme des
  recouvrements). Les unites testees sont celles qui portent un connecteur
  inferentiel, plus celles dont le meilleur candidat recouvre au moins
  ``m2.interpretation.recouvrement_sans_connecteur`` (0,50) ; au plus
  ``m2.interpretation.paires_max_page`` (600) paires par page, connecteurs
  d'abord. Cout total borne : 3 paires par unite testee, 600 par page.
  La similarite est lexicale (pas d'embeddings) pour rester deterministe et peu couteuse.
* Une source ne fait « sourcer » l'unite que si sa confiance atteint
  ``m2.confiance_min`` (0,60) : phrase (0,90) et paragraphe (0,60) oui, page
  (0,40) non (elle reste enregistree pour M3 et le maillage).
* Les lignes ``sources`` heritees (``inherited_from``) et les verifications
  externes (``kind='verification'``, M5) ne comptent pas comme sources explicites.
* Les liens vers la page elle-meme (URL de l'en-tete) ne sont jamais des sources.
"""
import bisect
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from urllib.parse import urljoin

from chaine import lexiques as L
from chaine import migration
from chaine import moteurs
from chaine import noyau
from chaine import seuils

VERSION_PROMPT = 'm2-v1'
CONTRIBUTION = 'm2'

# Les seuils propres a M2 (m2.*) sont declares dans ``chaine.seuils.DEFAUTS``.

NOMS_SEUILS = [
    'm2.confiance.phrase', 'm2.confiance.paragraphe', 'm2.confiance.page', 'm2.confiance_min', 'm2.opinion.score_min',
    'm2.interpretation.nli', 'm2.connaissance_generale.score_min', 'm2.source_manquante.cosinus',
] + sorted(
    nom for nom in seuils.DEFAUTS
    if nom.startswith('m2.') and nom not in (
        'm2.confiance.phrase', 'm2.confiance.paragraphe', 'm2.confiance.page', 'm2.confiance_min', 'm2.opinion.score_min',
        'm2.interpretation.nli', 'm2.connaissance_generale.score_min', 'm2.source_manquante.cosinus',
    )
)

TYPE_VERS_SOURCING = {
    'information_source': 'auto_sourcee',
    'opinion': 'opinion',
    'interpretation': 'dependante',
    'connaissance_generale': 'connaissance_generale',
    'sans_source': 'sans_source',
}
# Suite donnee par le tableau du document (statut ecrit seulement si l'unite n'a pas de verdict).
TYPE_VERS_STATUT = {
    'information_source': 'sourcee',
    'opinion': 'non_verifiable',
    'interpretation': 'dependante',
    'connaissance_generale': 'a_verifier_faible',
}

CLAUSE_ELIGIBLE = (
    "(u.obs_class IN ('etat_courant','annonce','inconnu','') OR u.obs_class IS NULL) AND COALESCE(u.excluded,0)=0"
)

_MOTS_VIDES = frozenset(
    'dans avec pour sont cette leur leurs plus comme mais aussi entre vers chez sous sans donc alors ainsi tout tous toute '
    'toutes elle elles nous vous ils ces ses son sa aux des les une que qui quoi dont par sur est ete etre avoir fait fait '
    'peut peuvent doit doivent quand lors apres avant depuis tres bien encore deja meme autres autre'.split()
)

noyau.schema('chaine:m2.generale', noyau.objet({
    'resultats': noyau.tableau(noyau.objet({
        'id': {'type': 'integer'},
        'general': {'type': 'boolean'},
        'confiance': {'type': 'number'},
    })),
}))

TACHE_GENERALE = (
    "Pour chaque phrase (id, texte), reponds a la question fermee : " + L.QUESTION_FERMEE +
    " general = true seulement si la reponse est oui sans aucune reserve. "
    "confiance = ta certitude entre 0 et 1. Reponds pour chaque id, sans rien ajouter."
)


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def _seuil(nom):
    """Valeur d'un seuil (fondation ou ajout M2)."""
    return seuils.get(nom)


def _horodatage():
    """Horodatage ISO du serveur."""
    return noyau.srv().now()


def _id(*parties):
    """Identifiant deterministe de 16 caracteres."""
    return hashlib.sha1('|'.join(str(p) for p in parties).encode('utf-8')).hexdigest()[:16]


def _lots(liste, taille):
    """Decoupe une liste en lots."""
    taille = max(1, int(taille))
    for debut in range(0, len(liste), taille):
        yield liste[debut:debut + taille]


@contextmanager
def _execution(run, nom, moteurs_utilises=None, **parametres):
    """Utilise l'execution ``run`` fournie, sinon ouvre ``noyau.etape(nom)``."""
    if run is not None:
        yield run
        return
    with noyau.etape(nom, moteurs=moteurs_utilises, prompt=VERSION_PROMPT, seuils=NOMS_SEUILS, **parametres) as nouvelle:
        yield nouvelle


def _llm_autorise(avec_llm):
    """Vrai si le LLM local peut etre appele (argument, sinon reglage ``m2:llm`` different de non/regles)."""
    if avec_llm is not None:
        return bool(avec_llm)
    return str(noyau.reglage('m2:llm', 'auto')).lower() not in ('non', 'no', 'false', '0', 'regles')


def _arret_demande():
    """Vrai si l'utilisateur a demande l'arret (pour ne pas avaler l'exception)."""
    arret = getattr(noyau.srv(), 'STOP', None)
    return bool(arret is not None and arret.is_set())


def _mots_pleins(texte_plat):
    """Ensemble des mots pleins (4 lettres ou plus, hors mots vides) d'un texte a plat."""
    return {m for m in re.findall(r"[a-z0-9]{4,}", texte_plat) if m not in _MOTS_VIDES}


def _recouvrement(a, b):
    """Coefficient de recouvrement entre deux ensembles de mots pleins."""
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def _specificites(texte, plat):
    """Drapeaux de specificite d'un texte : chiffre, annee, mois, majuscule interne."""
    return {
        'chiffre': bool(L.CHIFFRE.search(texte)),
        'annee': bool(L.ANNEE.search(plat)),
        'mois': bool(L.MOIS.search(plat)),
        'majuscule': bool(re.search(r"(?<=[\w\u00c0-\u017f,;:)\u00bb\"'])\s+[A-Z\u00c0-\u00de][\w\u00c0-\u017f]", texte)),
    }


# ---------------------------------------------------------------------------
# Contexte d'une page (version)
# ---------------------------------------------------------------------------

class Page:
    """Texte d'une version et ses structures : paragraphes, phrases, sections, catalogue, indices."""

    def __init__(self, ligne, site):
        self.id = ligne['id']
        self.texte = ligne.get('text') or ''
        self.plat = L.plat(self.texte)
        self.ligne = ligne
        self.site = set(site)
        self.url_propre = _url_propre(self.texte, ligne.get('purl'))
        self.paragraphes = _paragraphes(self.texte)
        self.par_debuts = [p[0] for p in self.paragraphes]
        offsets = moteurs.phrases(self.texte) if self.texte else []
        self.moteur_phrases = moteurs.dernier('phrases')
        self.phr_debuts = [o[0] for o in offsets]
        self.phr_fins = [o[1] for o in offsets]
        self.sections = _sections(self.texte)
        self.sec_debuts = [s for s in self.sections]
        self._catalogue = None
        self._indices = None
        self._entites_phrases = {}
        self._par_indices = None
        self.type_page = None

    # --- navigation -------------------------------------------------------

    def zone(self, debut, fin):
        """Phrases couvrant [debut, fin) ; l'etendue elle-meme si elles sont trop nombreuses."""
        n = len(self.phr_debuts)
        if n == 0 or debut is None or fin is None:
            return debut, fin
        i = bisect.bisect_right(self.phr_debuts, debut) - 1
        if i < 0:
            i = 0
        if self.phr_fins[i] <= debut and i + 1 < n:
            i += 1
        j = max(i, bisect.bisect_left(self.phr_debuts, fin) - 1)
        zone_debut, zone_fin = self.phr_debuts[i], self.phr_fins[j]
        if j - i >= 3 or zone_fin - zone_debut > 2000:
            return debut, fin
        return min(zone_debut, debut), max(zone_fin, fin)

    def paragraphe_de(self, position):
        """Numero du paragraphe contenant ``position`` (-1 si hors paragraphe)."""
        if not self.par_debuts or position is None:
            return -1
        i = bisect.bisect_right(self.par_debuts, position) - 1
        return i if i >= 0 else -1

    def section_de(self, position):
        """Numero de la section contenant ``position``."""
        if position is None:
            return 0
        return bisect.bisect_right(self.sec_debuts, position) - 1

    def domaine_interne(self, domaine):
        """Vrai si ``domaine`` est celui du site (ou un sous-domaine)."""
        return bool(domaine) and migration.est_interne(domaine, self.site)

    # --- catalogue --------------------------------------------------------

    @property
    def catalogue(self):
        """Entrees de l'encadre « Sources » (voir ``_catalogue``)."""
        if self._catalogue is None:
            self._catalogue = _catalogue(self)
        return self._catalogue

    # --- indices ----------------------------------------------------------

    @property
    def indices(self):
        """Indices de source de la page, tries par debut."""
        if self._indices is None:
            self._indices = _detecter_indices(self)
            self._par_indices = {}
            for indice in self._indices:
                for numero in indice['paragraphes']:
                    self._par_indices.setdefault(numero, []).append(indice)
        return self._indices

    @property
    def debuts_indices(self):
        """Debuts des indices (pour la recherche par bissection)."""
        if getattr(self, '_debuts_indices', None) is None:
            self._debuts_indices = [i['debut'] for i in self.indices]
        return self._debuts_indices

    def indices_du_paragraphe(self, numero):
        """Indices rattaches au paragraphe ``numero``."""
        self.indices
        return self._par_indices.get(numero, [])

    def entites_phrase(self, index_phrase):
        """Entites (moteurs.entites) de la phrase ``index_phrase``, avec offsets absolus."""
        if index_phrase not in self._entites_phrases:
            debut, fin = self.phr_debuts[index_phrase], self.phr_fins[index_phrase]
            liste = []
            for entite in moteurs.entites(self.texte[debut:fin]):
                copie = dict(entite)
                copie['start'] = debut + entite['start']
                copie['end'] = debut + entite['end']
                liste.append(copie)
            self._entites_phrases[index_phrase] = liste
        return self._entites_phrases[index_phrase]


def _url_propre(texte, url_page):
    """URL de la page elle-meme : ligne d'en-tete ne contenant qu'une URL, sinon ``pages.url`` http."""
    lignes = [ligne.strip() for ligne in texte[:1500].splitlines() if ligne.strip()][:6]
    for ligne in lignes[1:]:
        if re.fullmatch(r'https?://\S+', ligne):
            return ligne.rstrip('/')
    if url_page and str(url_page).lower().startswith('http'):
        return str(url_page).rstrip('/')
    return None


def _paragraphes(texte):
    """Paragraphes (debut, fin) : blocs separes par une ligne vide ; listes et tableaux ligne par ligne."""
    resultat = []
    for match in re.finditer(r'(?:[^\n]*\S[^\n]*(?:\n|$))+', texte):
        bloc_debut, bloc_fin = match.start(), match.end()
        bloc = texte[bloc_debut:bloc_fin]
        lignes = bloc.split('\n')
        utiles = [ligne for ligne in lignes if ligne.strip()]
        liste = sum(1 for ligne in utiles if re.match(r'\s*(?:[-*\u2022\u2013]|\d{1,3}[.)])\s', ligne) or ' | ' in ligne)
        if len(utiles) > 1 and liste * 2 >= len(utiles):
            position = bloc_debut
            for ligne in lignes:
                if ligne.strip():
                    resultat.append((position, position + len(ligne.rstrip())))
                position += len(ligne) + 1
        else:
            resultat.append((bloc_debut, bloc_debut + len(bloc.rstrip())))
    return resultat


def _sections(texte):
    """Debuts des sections : ligne ``== Titre`` ou titre en capitales ; 0 pour le preambule."""
    debuts = [0]
    position = 0
    for ligne in texte.split('\n'):
        propre = ligne.strip()
        if propre and position > 0:
            if propre.startswith('== ') or (propre.isupper() and 4 <= len(propre) <= 140 and not propre.endswith('.')
                                          and not propre.startswith('http')):
                debuts.append(position)
        position += len(ligne) + 1
    return debuts


# ---------------------------------------------------------------------------
# Catalogue : encadre « Sources », bibliographie, notes
# ---------------------------------------------------------------------------

def _entite_reference(texte):
    """Nom cite d'une entree de bibliographie : debut jusqu'a la premiere virgule, guillemet ou deux-points."""
    propre = re.sub(r'\s+', ' ', texte).strip()
    coupe = re.split(r'\s[\u00ab"]|,\s|\s:\s', propre, maxsplit=1)[0]
    return coupe[:120].strip() or propre[:80]


def _catalogue(page):
    """Entrees ``{n, texte, debut, fin, url, domaine, entite}`` apres le dernier titre « Sources »."""
    texte = page.texte
    position = 0
    titre = None
    lignes = texte.split('\n')
    positions = []
    for ligne in lignes:
        positions.append(position)
        if L.TITRES_BIBLIOGRAPHIE.match(L.plat(ligne)):
            titre = len(positions) - 1
        position += len(ligne) + 1
    if titre is None:
        return {'entrees': [], 'debut': None, 'par_numero': {}}
    entrees = []
    courante = None
    precedente_vide = True
    for index in range(titre + 1, len(lignes)):
        ligne = lignes[index]
        debut_ligne = positions[index]
        if not ligne.strip():
            precedente_vide = True
            continue
        numerotee = L.ENTREE_NUMEROTEE.match(ligne)
        puce = L.ENTREE_PUCE.match(ligne)
        if numerotee or puce:
            if courante:
                entrees.append(courante)
            numero = int(numerotee.group(1) or numerotee.group(2)) if numerotee else None
            corps = numerotee.group(3) if numerotee else puce.group(1)
            courante = {'n': numero, 'texte': corps.strip(), 'debut': debut_ligne, 'fin': debut_ligne + len(ligne.rstrip())}
        elif courante and not precedente_vide:
            courante['texte'] += ' ' + ligne.strip()
            courante['fin'] = debut_ligne + len(ligne.rstrip())
        precedente_vide = False
    if courante:
        entrees.append(courante)
    for entree in entrees:
        lien = L.URL.search(entree['texte'])
        entree['url'] = lien.group().rstrip(L.PONCTUATION_URL) if lien else None
        entree['domaine'] = migration.domaine(entree['url']) if entree['url'] else ''
        entree['entite'] = _entite_reference(entree['texte'])
    par_numero = {e['n']: e for e in entrees if e['n'] is not None}
    return {'entrees': entrees, 'debut': positions[titre], 'par_numero': par_numero}


# ---------------------------------------------------------------------------
# Detection des indices de source
# ---------------------------------------------------------------------------

def _nature_lien(page, domaine):
    """``lien_interne`` si le domaine est celui du site, sinon ``lien_externe``."""
    return 'lien_interne' if page.domaine_interne(domaine) else 'lien_externe'


def _indice(page, nature, debut, fin, url=None, entite=None, passage='', origine='', facteur=1.0, paragraphes=None):
    """Dictionnaire d'un indice de source."""
    domaine = migration.domaine(url) if url else ''
    if paragraphes is None:
        paragraphes = {page.paragraphe_de(debut)}
    return {
        'type': nature, 'url': url, 'domaine': domaine, 'entite': entite, 'debut': debut, 'fin': fin,
        'passage': (passage or '')[:300], 'origine': origine, 'facteur': facteur, 'paragraphes': set(paragraphes),
    }


def _detecter_indices(page):
    """Les trois signaux deterministes : liens, attributions, appareil de citation."""
    indices = []
    indices.extend(_indices_liens(page))
    indices.extend(_indices_xml(page))
    indices.extend(_indices_meta(page))
    indices.extend(_indices_attributions(page))
    indices.extend(_indices_appareil(page))
    indices.sort(key=lambda i: (i['debut'], i['fin']))
    return indices


def _est_page_elle_meme(page, url):
    """Vrai si ``url`` designe la page elle-meme."""
    if not page.url_propre or not url:
        return False
    return url.rstrip('/').lower() == page.url_propre.lower()


def _indices_liens(page):
    """URL presentes en clair dans le texte (corpus importe en texte brut)."""
    resultat = []
    for match in L.URL.finditer(page.texte):
        url = match.group().rstrip(L.PONCTUATION_URL)
        if not url or _est_page_elle_meme(page, url):
            continue
        domaine = migration.domaine(url)
        if not domaine:
            continue
        resultat.append(_indice(page, _nature_lien(page, domaine), match.start(), match.start() + len(url), url=url,
                                passage=url, origine='lien_texte'))
    return resultat


def _indices_xml(page):
    """``<ref target>`` du XML Trafilatura (``versions.xml``), ancres localisees dans le texte."""
    xml = page.ligne.get('xml')
    if not xml:
        return []
    try:
        racine = ET.fromstring(xml)
    except ET.ParseError:
        return []
    resultat = []
    curseur = 0
    for element in racine.iter('ref'):
        cible = (element.get('target') or '').strip()
        ancre = re.sub(r'\s+', ' ', ''.join(element.itertext())).strip()
        if not cible or cible.startswith(('#', 'mailto:', 'javascript:', 'tel:')):
            continue
        url = cible if cible.lower().startswith('http') else (urljoin(page.url_propre + '/', cible) if page.url_propre else None)
        if not url or not url.lower().startswith('http') or _est_page_elle_meme(page, url):
            continue
        if not ancre:
            continue
        place = page.texte.find(ancre, curseur)
        if place < 0:
            place = page.texte.find(ancre)
        if place < 0:
            continue
        curseur = place + len(ancre)
        domaine = migration.domaine(url)
        resultat.append(_indice(page, _nature_lien(page, domaine), place, place + len(ancre), url=url, passage=ancre,
                                origine='lien_xml'))
    return resultat


def _indices_meta(page):
    """Liens du HTML extraits par M1 etape 1 sans Trafilatura (``versions.meta`` : ``liens`` avec offsets).

    Sans XML, ce sont les seuls ``<a href>`` conserves ; un lien deja ecrit en clair dans le texte
    est laisse a ``_indices_liens``.
    """
    if page.ligne.get('xml'):
        return []
    try:
        meta = json.loads(page.ligne.get('meta') or '{}')
    except ValueError:
        return []
    resultat = []
    for lien in (meta.get('liens') if isinstance(meta, dict) else None) or []:
        cible = str(lien.get('href') or '').strip()
        debut, fin = lien.get('start'), lien.get('end')
        if not cible or not isinstance(debut, int) or not isinstance(fin, int) or not 0 <= debut < fin <= len(page.texte):
            continue
        if page.texte[debut:fin] == cible or cible.startswith(('#', 'mailto:', 'javascript:', 'tel:')):
            continue
        url = cible if cible.lower().startswith('http') else (urljoin(page.url_propre + '/', cible) if page.url_propre else None)
        if not url or not url.lower().startswith('http') or _est_page_elle_meme(page, url):
            continue
        domaine = migration.domaine(url)
        if not domaine:
            continue
        resultat.append(_indice(page, _nature_lien(page, domaine), debut, fin, url=url, passage=page.texte[debut:fin],
                                origine='lien_html'))
    return resultat


def _marques_du_site(page):
    """Noms du site (libelle des domaines, auteur) a ne pas prendre pour une source externe nommee."""
    marques = {d.split('.')[0] for d in page.site if len(d.split('.')[0]) >= 5}
    auteur = page.ligne.get('author')
    if auteur:
        marques.add(re.sub(r'\W+', '', L.plat(auteur)))
    return marques


def _index_phrase(page, position):
    """Index de la phrase contenant ``position`` (-1 s'il n'y en a pas)."""
    if not page.phr_debuts:
        return -1
    i = bisect.bisect_right(page.phr_debuts, position) - 1
    if i < 0 or position >= page.phr_fins[i] + 1:
        return -1
    return i


def _entite_valide(page, entite, marques):
    """Type admis comme source nommee, et pas le site lui-meme."""
    if entite['type'] not in L.TYPES_SOURCE_NOMMEE or len(entite['text'].strip()) < 2:
        return False
    if not L.NOM_SOURCE.match(entite['text'].strip()):
        return False
    nom = re.sub(r'\W+', '', L.plat(entite['text']))
    return not any(m and (m in nom or nom in m) for m in marques if len(nom) >= 4)


def _indices_attributions(page):
    """Marqueurs d'attribution (lexique) croises avec les entites organisations / personnes."""
    resultat = []
    marques = _marques_du_site(page)
    vus = set()
    plat = page.plat
    for match in L.ATTRIBUTION_AVANT.finditer(plat):
        i = _index_phrase(page, match.start())
        if i < 0:
            continue
        if match.group(0) == "d'apres" and L.D_APRES_ADVERBIAL.search(plat[max(0, match.start() - 20):match.start()]):
            continue
        fin_phrase = page.phr_fins[i]
        entite = None
        for candidate in page.entites_phrase(i):
            if candidate['start'] < match.end() or candidate['start'] - match.end() > 45:
                continue
            ecart = plat[match.end():candidate['start']]
            if re.search(r"[.;!?,(]", ecart) or len(ecart.split()) > 6 or not _entite_valide(page, candidate, marques):
                continue
            entite = candidate
            break
        if entite is None:
            # Repli defensif : « selon <Nom propre> » quand la NER ne trouve rien (confiance reduite).
            entite = _entite_apres_marqueur(page, match, fin_phrase, marques)
            if entite is None:
                continue
        cle = (entite['start'], entite['end'])
        if cle in vus:
            continue
        vus.add(cle)
        resultat.append(_indice(page, 'source_nommee', match.start(), entite['end'], entite=entite['text'].strip(),
                                passage=page.texte[match.start():entite['end']], origine='attribution',
                                facteur=1.0 if entite.get('engine') != 'regles_lexique' else 0.85))
    for match in L.ATTRIBUTION_VERBE.finditer(plat):
        i = _index_phrase(page, match.start())
        if i < 0:
            continue
        entite = None
        for candidate in page.entites_phrase(i):
            if not _entite_valide(page, candidate, marques):
                continue
            if candidate['end'] <= match.start() and re.fullmatch(r"\s*", plat[candidate['end']:match.start()]):
                entite = candidate
                break
            ecart = plat[match.end():candidate['start']]
            if candidate['start'] >= match.end() and len(ecart) <= 12 and L.ECART_ENTITE.match(ecart):
                entite = candidate
                break
        if entite is None:
            continue
        cle = (entite['start'], entite['end'])
        if cle in vus:
            continue
        vus.add(cle)
        debut, fin = min(entite['start'], match.start()), max(entite['end'], match.end())
        resultat.append(_indice(page, 'source_nommee', debut, fin, entite=entite['text'].strip(),
                                passage=page.texte[debut:fin], origine='attribution'))
    return resultat


def _entite_apres_marqueur(page, match, fin_phrase, marques):
    """Nom propre juste apres un marqueur prepositionnel (« selon l'Insee ») sans passer par la NER."""
    reste = page.texte[match.end():min(fin_phrase, match.end() + 80)]
    trouve = re.match(r"\s*(?:(?:le|la|les|l'|l\u2019|un|une)\s*)?([A-Z\u00c0-\u00de][\w\u00c0-\u017f&-]*(?:\s+[A-Z\u00c0-\u00de][\w\u00c0-\u017f&-]*)*)", reste)
    if not trouve:
        return None
    nom = trouve.group(1)
    candidate = {'text': nom, 'type': 'nom_propre', 'start': match.end() + trouve.start(1),
                 'end': match.end() + trouve.end(1), 'engine': 'regles_lexique'}
    return candidate if _entite_valide(page, candidate, marques) else None


def _indices_appareil(page):
    """Appareil de citation : notes numerotees, marqueurs entre crochets, lignes « Source : », legendes."""
    resultat = []
    texte = page.texte
    plat = page.plat
    catalogue = page.catalogue
    debut_biblio = catalogue['debut']
    for match in L.MARQUEUR_CROCHETS.finditer(texte):
        etiquette = plat[match.start(1):match.end(1)].strip()
        source = match.group(2)
        if etiquette in L.ETIQUETTES_EPISTEMIQUES and source and source.strip():
            resultat.append(_indice(page, 'note', match.start(), match.end(), entite=source.strip()[:200],
                                    passage=match.group(0), origine='crochets'))
    if catalogue['par_numero']:
        for match in L.NOTE_NUMERIQUE.finditer(texte):
            if debut_biblio is not None and match.start() >= debut_biblio:
                continue
            for numero in re.findall(r'\d{1,3}', match.group(1)):
                entree = catalogue['par_numero'].get(int(numero))
                if entree:
                    resultat.append(_indice(page, 'note', match.start(), match.end(), url=entree['url'],
                                            entite=entree['entite'], passage=match.group(0), origine='note_numerique'))
        for match in L.NOTE_EXPOSANT.finditer(texte):
            if debut_biblio is not None and match.start() >= debut_biblio:
                continue
            numero = int(''.join(L.EXPOSANTS.get(c, '') for c in match.group(1)) or 0)
            entree = catalogue['par_numero'].get(numero)
            if entree:
                resultat.append(_indice(page, 'note', match.start(), match.end(), url=entree['url'], entite=entree['entite'],
                                        passage=match.group(0), origine='note_exposant'))
    position = 0
    for ligne in texte.split('\n'):
        debut_ligne, fin_ligne = position, position + len(ligne.rstrip())
        position += len(ligne) + 1
        propre = ligne.strip()
        if not propre:
            continue
        plat_ligne = L.plat(propre)
        source = L.LIGNE_SOURCE.match(plat_ligne)
        if source:
            entite = propre[source.start(1) + (len(propre) - len(plat_ligne)):][:200].strip()
            precedent = page.paragraphe_de(debut_ligne) - 1
            numeros = {page.paragraphe_de(debut_ligne)}
            if precedent >= 0:
                numeros.add(precedent)
            resultat.append(_indice(page, 'note', debut_ligne, fin_ligne, entite=entite, passage=propre,
                                    origine='ligne_source', paragraphes=numeros))
        elif L.LEGENDE.match(plat_ligne):
            interne = re.search(r"\((?:source|sources|d'apres)\s*:\s*([^)]{2,160})\)", plat_ligne)
            if interne or L.URL.search(propre):
                entite = propre[interne.start(1):interne.end(1)].strip() if interne else None
                numeros = {page.paragraphe_de(debut_ligne)}
                precedent = page.paragraphe_de(debut_ligne) - 1
                if precedent >= 0:
                    numeros.add(precedent)
                resultat.append(_indice(page, 'note', debut_ligne, fin_ligne, entite=entite, passage=propre,
                                        origine='legende', paragraphes=numeros))
    return resultat


# ---------------------------------------------------------------------------
# Etape 1 : rattachement aux unites
# ---------------------------------------------------------------------------

def _unites_eligibles(version_id=None, unit_ids=None):
    """Unites obsolescibles (classe etat_courant / annonce / inconnue) ; filtrees par version ou identifiants."""
    serveur = noyau.srv()
    requete = ('SELECT u.id, u.version_id, u.text, u.span_start, u.span_end, u.obs_class, u.anchor_date, u.anchor_kind, '
               'u.status, u.verdict, u.sourcing FROM units u WHERE ' + CLAUSE_ELIGIBLE)
    arguments = []
    if version_id is not None:
        requete += ' AND u.version_id=?'
        arguments.append(version_id)
    if unit_ids is not None:
        resultat = []
        for lot in _lots(list(unit_ids), 500):
            marques = ','.join('?' for _ in lot)
            resultat.extend(serveur.query(requete + ' AND u.id IN (' + marques + ')', tuple(arguments) + tuple(lot)))
        return resultat
    return serveur.query(requete + ' ORDER BY u.span_start, u.id', tuple(arguments))


def _domaines_site():
    """Domaines internes : ``pages``, reglage ``site:domaines``, et URL d'en-tete des versions."""
    serveur = noyau.srv()
    site = set(migration.domaines_du_site())
    for ligne in serveur.query('SELECT substr(text,1,1500) t FROM versions'):
        url = _url_propre(ligne['t'] or '', None)
        if url:
            site.add(migration.domaine(url))
    site.discard('')
    return site


_CACHE_PAGES = {}
_CACHE_PAGES_MAX = 3


def vider_cache():
    """Oublie les pages en cache (tests, changement de base)."""
    _CACHE_PAGES.clear()


def _charger_page(version_id, site=None):
    """Objet ``Page`` d'une version, ou None si la version n'existe pas.

    Les derniers objets ``Page`` sont gardes en cache (cle : base, version,
    empreinte du texte) pour ne segmenter qu'une fois une page traitee en
    deux etapes. Le cache est invalide si le texte ou les domaines du site changent.
    """
    ligne = noyau.srv().query(
        'SELECT v.*, p.title ptitre, p.url purl FROM versions v LEFT JOIN pages p ON p.id=v.page_id WHERE v.id=?',
        (version_id,), True)
    if not ligne:
        return None
    site = _domaines_site() if site is None else site
    empreinte = hashlib.sha1((ligne.get('text') or '').encode('utf-8')).hexdigest()
    cle = (str(noyau.srv().DB), version_id, empreinte, tuple(sorted(site)))
    page = _CACHE_PAGES.get(cle)
    if page is None:
        page = Page(ligne, site)
        while len(_CACHE_PAGES) >= _CACHE_PAGES_MAX:
            _CACHE_PAGES.pop(next(iter(_CACHE_PAGES)))
        _CACHE_PAGES[cle] = page
    return page


def _rattacher(page, unite):
    """Sources d'une unite : liste de dict(indice, portee, confiance), phrase puis paragraphe puis page."""
    debut, fin = unite['span_start'], unite['span_end']
    if debut is None or fin is None:
        return []
    zone_debut, zone_fin = page.zone(debut, fin)
    indices = page.indices
    debuts = page.debuts_indices
    depart = bisect.bisect_left(debuts, max(0, zone_debut - 600))
    retenus = []
    vus = set()
    for indice in indices[depart:]:
        if indice['debut'] >= zone_fin:
            break
        if indice['fin'] > zone_debut and indice['debut'] < zone_fin:
            cle = (indice['type'], indice['url'] or indice['entite'])
            if cle not in vus:
                vus.add(cle)
                retenus.append({'indice': indice, 'portee': 'phrase', 'confiance': _seuil('m2.confiance.phrase') * indice['facteur']})
    numero = page.paragraphe_de(debut)
    candidats = []
    for indice in page.indices_du_paragraphe(numero):
        cle = (indice['type'], indice['url'] or indice['entite'])
        if cle in vus:
            continue
        distance = min(abs(indice['debut'] - debut), abs(indice['fin'] - fin))
        candidats.append((distance, indice['debut'], cle, indice))
    candidats.sort(key=lambda c: (c[0], c[1]))
    for _, _, cle, indice in candidats[:int(_seuil('m2.sources.paragraphe_max'))]:
        if cle in vus:
            continue
        vus.add(cle)
        retenus.append({'indice': indice, 'portee': 'paragraphe',
                        'confiance': _seuil('m2.confiance.paragraphe') * indice['facteur']})
    if not any(r['portee'] in ('phrase', 'paragraphe') for r in retenus):
        retenus.extend(_apparier_catalogue(page, unite))
    return retenus


def _apparier_catalogue(page, unite):
    """Rapproche une unite des references de l'encadre « Sources » par noms rares (portee page, confiance faible)."""
    entrees = page.catalogue['entrees']
    if not entrees:
        return []
    cache = getattr(page, '_jetons_catalogue', None)
    if cache is None:
        par_jeton = {}
        for index, entree in enumerate(entrees):
            for jeton in set(re.findall(r"[A-Z\u00c0-\u00de][\w\u00c0-\u017f'-]{3,}", entree['texte'])):
                par_jeton.setdefault(jeton, set()).add(index)
        cache = {jeton: indices for jeton, indices in par_jeton.items() if len(indices) <= 3}
        page._jetons_catalogue = cache
    texte = unite['text'] or ''
    portees = {}
    for jeton in set(re.findall(r"[A-Z\u00c0-\u00de][\w\u00c0-\u017f'-]{3,}", texte)):
        for index in cache.get(jeton, ()):
            portees[index] = portees.get(index, 0) + 1
    classes = sorted(portees.items(), key=lambda element: (-element[1], element[0]))
    resultat = []
    for index, _ in classes[:int(_seuil('m2.sources.page_max'))]:
        entree = entrees[index]
        indice = _indice(page, 'note', entree['debut'], entree['fin'], url=entree['url'], entite=entree['entite'],
                         passage=entree['texte'], origine='catalogue', paragraphes=set())
        resultat.append({'indice': indice, 'portee': 'page', 'confiance': _seuil('m2.confiance.page')})
    return resultat


def _marqueur_sources(version_id):
    """Cle ``settings`` qui marque les sources M2 calculees pour une version."""
    return 'm2:sources:' + version_id


def sources_explicites(version_id, run=None):
    """Etape 1 : sources explicites des unites obsolescibles d'une version.

    Ecrit des lignes ``sources`` (``contribution='m2'``) : type (lien_interne,
    lien_externe, source_nommee, note), portee (phrase, paragraphe, page) et
    confiance. Rejouable : les lignes M2 precedentes de la version sont
    remplacees. Renvoie un dict de compteurs.
    """
    serveur = noyau.srv()
    compteurs = {'version': version_id, 'unites': 0, 'unites_avec_source': 0, 'sources': 0, 'par_type': {}, 'par_portee': {},
                 'indices': 0, 'duree_s': 0.0}
    debut_calcul = time.monotonic()
    page = _charger_page(version_id)
    if page is None:
        return compteurs
    unites = _unites_eligibles(version_id)
    with _execution(run, 'm2.sources', {'phrases': 'regles', 'attributions': 'lexique+ner'}, pages=1, items=len(unites)) as exec_:
        horodatage = serveur.now()
        lignes = []
        avec_source = 0
        for unite in unites:
            rattaches = _rattacher(page, unite)
            if rattaches:
                avec_source += 1
            for element in rattaches:
                indice = element['indice']
                identifiant = _id('m2src', unite['id'], indice['type'], indice['url'] or indice['entite'], indice['debut'])
                lignes.append((identifiant, unite['id'], indice['url'], indice['domaine'] or None, indice['entite'],
                               indice['type'], element['portee'], round(element['confiance'], 4), None, CONTRIBUTION, None,
                               indice['passage'], exec_.id, horodatage))
                compteurs['par_type'][indice['type']] = compteurs['par_type'].get(indice['type'], 0) + 1
                compteurs['par_portee'][element['portee']] = compteurs['par_portee'].get(element['portee'], 0) + 1
        identifiants = [u['id'] for u in unites]
        with noyau.connexion() as base:
            for lot in _lots(identifiants, 500):
                marques = ','.join('?' for _ in lot)
                base.execute('DELETE FROM sources WHERE contribution=? AND inherited_from IS NULL AND unit_id IN (' + marques + ')',
                             (CONTRIBUTION,) + tuple(lot))
            base.executemany(
                'INSERT OR REPLACE INTO sources(id,unit_id,url,domain,cited_entity,kind,scope,confidence,inherited_from,contribution,'
                'source_date,passage,run_id,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', lignes)
            base.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                         (_marqueur_sources(version_id), json.dumps({'run_id': exec_.id, 'date': horodatage})))
        exec_.moteur('phrases', moteurs.dernier('phrases') or page.moteur_phrases)
        compteurs.update(unites=len(unites), unites_avec_source=avec_source, sources=len(lignes), indices=len(page.indices))
    compteurs['duree_s'] = round(time.monotonic() - debut_calcul, 3)
    return compteurs


# ---------------------------------------------------------------------------
# Etape 2 : type de page (calcule une fois par page)
# ---------------------------------------------------------------------------

def _normaliser_type_page(valeur):
    """Type de page normalise (cle de ``FAMILLES_PAGE``) depuis une etiquette libre, ou None."""
    plat = re.sub(r'[^a-z0-9]+', '_', L.plat(str(valeur or ''))).strip('_')
    if plat in L.SCHEMA_ORG:
        return L.SCHEMA_ORG[plat]
    if plat in L.FAMILLES_PAGE:
        return plat
    if plat.replace('_', '-') in L.RUBRIQUES:
        return L.RUBRIQUES[plat.replace('_', '-')]
    return None


def _valeurs_json(objet, cles):
    """Valeurs textuelles des cles ``cles`` (insensible a la casse) dans un JSON imbrique."""
    valeurs = []
    if isinstance(objet, dict):
        for cle, valeur in objet.items():
            if str(cle).lower().lstrip('@') in cles:
                if isinstance(valeur, (list, tuple)):
                    valeurs.extend(str(v) for v in valeur)
                elif isinstance(valeur, (str, int)):
                    valeurs.append(str(valeur))
            valeurs.extend(_valeurs_json(valeur, cles))
    elif isinstance(objet, (list, tuple)):
        for element in objet:
            valeurs.extend(_valeurs_json(element, cles))
    return valeurs


def _type_page_par_signaux(page):
    """Types de page par schema.org, rubrique, fil d'Ariane ou chemin d'URL : (type, confiance, signal) ou None."""
    ligne = page.ligne
    meta = ligne.get('meta')
    donnees = None
    if meta:
        try:
            donnees = json.loads(meta)
        except ValueError:
            donnees = None
    types_schema = _valeurs_json(donnees, {'type', 'schema_type', 'schematype', 'pagetype'}) if donnees is not None else []
    if donnees is None and meta:
        types_schema = [n for n in L.SCHEMA_ORG if n in L.plat(meta)]
    for valeur in types_schema:
        trouve = L.SCHEMA_ORG.get(re.sub(r'[^a-z0-9]+', '', L.plat(valeur)))
        if trouve:
            return trouve, _seuil('m2.page.confiance_schema'), 'schema.org:' + str(valeur)[:40]
    rubriques = []
    if donnees is not None:
        rubriques.extend(_valeurs_json(donnees, {'breadcrumb', 'breadcrumbs', 'fil_ariane', 'rubrique', 'section', 'categorie',
                                                  'categories', 'category', 'articlesection', 'name'}))
    xml = ligne.get('xml')
    if xml:
        try:
            racine = ET.fromstring(xml)
            for attribut in ('categories', 'tags', 'pagetype'):
                if racine.get(attribut):
                    rubriques.extend(re.split(r'[,;/>|]', racine.get(attribut)))
        except ET.ParseError:
            pass
    if page.url_propre:
        rubriques.extend(page.url_propre.split('/')[3:])
    for valeur in rubriques:
        cle = re.sub(r'[^a-z0-9]+', '-', L.plat(str(valeur))).strip('-')
        if cle in L.RUBRIQUES:
            return L.RUBRIQUES[cle], _seuil('m2.page.confiance_rubrique'), 'rubrique:' + cle
    return None


def _titre_et_chapo(page):
    """Titre (premiere ligne) et chapo (``versions.lede`` sinon premier paragraphe de prose) de la page."""
    lignes = [ligne.strip() for ligne in page.texte[:6000].splitlines() if ligne.strip()]
    titre = lignes[0][:300] if lignes else ''
    chapo = (page.ligne.get('lede') or '').strip()
    if not chapo:
        for debut, fin in page.paragraphes:
            bloc = page.texte[debut:fin].strip()
            if bloc == titre or re.fullmatch(r'https?://\S+', bloc):
                continue
            if len(bloc.split()) >= 12:
                chapo = bloc
                break
    return titre, chapo[:500]


def _classer(textes, etiquettes, exemples, avec_llm):
    """``moteurs.classer`` ; sans LLM autorise, seul le classifieur lexical de repli est utilise."""
    if avec_llm:
        resultat = moteurs.classer(textes, etiquettes, exemples)
        return resultat, moteurs.dernier('classer')
    par_etiquette = {e: list(exemples.get(e, [])) for e in etiquettes}
    resultat = moteurs.classer_lexical(textes, par_etiquette)
    for element in resultat:
        element['engine'] = 'lexical'
    return resultat, 'lexical'


def _confiance_classifieur(element):
    """Confiance exploitable d'un resultat de classifieur selon son moteur (le repli lexical est plafonne)."""
    score = max(0.0, min(1.0, float(element.get('score') or 0.0)))
    moteur = element.get('engine') or ''
    if moteur == 'setfit':
        return score
    if moteur == 'llm':
        return min(score, 0.85)
    if moteur == 'centroide':
        return score * 0.9
    scores = sorted((element.get('scores') or {}).values(), reverse=True)
    marge = (scores[0] - scores[1]) if len(scores) > 1 else score
    return max(0.0, min(_seuil('m2.page.confiance_lexical_max'), marge * 2.0))


def type_page(page, avec_llm=False):
    """Type de la page, calcule une fois : dict(type, famille, confiance, moteur, signal).

    Ordre : ``versions.page_type`` deja pose (M1), schema.org, rubrique / fil
    d'Ariane / chemin d'URL, puis classifieur sur titre + chapo
    (``moteurs.classer``). Le resultat est memorise dans ``versions.page_type``
    et ``page_type_engine`` lorsqu'ils sont vides.
    """
    if page.type_page is not None:
        return page.type_page
    resultat = {'type': None, 'famille': None, 'confiance': 0.0, 'moteur': None, 'signal': None}
    existant = _normaliser_type_page(page.ligne.get('page_type'))
    moteur_existant = str(page.ligne.get('page_type_engine') or '')
    if existant and moteur_existant.startswith('m2:'):
        # Type deja calcule par M2 : « m2:<moteur>:<confiance> » (calcule une seule fois par page).
        morceaux = moteur_existant.split(':')
        try:
            confiance = float(morceaux[-1])
        except ValueError:
            confiance = _seuil('m2.page.confiance_existant')
        resultat.update(type=existant, confiance=confiance, moteur=':'.join(morceaux[1:-1]) or 'm2', signal='m2:memorise')
    elif existant:
        resultat.update(type=existant, confiance=_seuil('m2.page.confiance_existant'),
                        moteur=moteur_existant or 'm1', signal='versions.page_type')
    else:
        trouve = _type_page_par_signaux(page)
        if trouve:
            resultat.update(type=trouve[0], confiance=trouve[1], moteur='regles', signal=trouve[2])
        else:
            titre, chapo = _titre_et_chapo(page)
            if (titre + chapo).strip():
                try:
                    liste, moteur = _classer([(titre + '. ' + chapo).strip()], L.ETIQUETTES_PAGE, L.EXEMPLES_PAGE, avec_llm)
                    element = liste[0]
                    resultat.update(type=element['label'], confiance=_confiance_classifieur(element), moteur=moteur,
                                    signal='classifieur')
                except (moteurs.Indisponible, ValueError, KeyError):
                    pass
                except (noyau.srv().OperationalError, RuntimeError):
                    if _arret_demande():
                        raise
    resultat['famille'] = L.FAMILLES_PAGE.get(resultat['type'])
    page.type_page = resultat
    if (resultat['type'] and resultat['signal'] not in ('versions.page_type', 'm2:memorise')
            and resultat['confiance'] >= _seuil('m2.confiance_min') and {'page_type', 'page_type_engine'} <= set(page.ligne)):
        # On ne memorise que les types de confiance suffisante, jamais une supposition du classifieur de repli.
        noyau.srv().query('UPDATE versions SET page_type=?, page_type_engine=? WHERE id=? AND (page_type IS NULL OR page_type=?)',
                          (resultat['type'], 'm2:' + str(resultat['moteur']) + ':' + str(round(resultat['confiance'], 3)), page.id, ''))
    return resultat


# ---------------------------------------------------------------------------
# Etape 2 : regles de la cascade
# ---------------------------------------------------------------------------

def _combiner(poids):
    """Combinaison de poids independants : 1 - produit(1 - p)."""
    reste = 1.0
    for p in poids:
        reste *= 1.0 - p
    return 1.0 - reste


def _regle_information_source(page, texte_zone, plat_zone, famille, auteur_expert):
    """Regle 1 : l'auteur fait foi. Renvoie (confiance, regle) ou None."""
    meilleur = None
    if L.PREMIERE_PERSONNE_METHODO.search(plat_zone):
        confiance = _seuil('m2.information_source.premiere_personne')
        meilleur = (confiance, 'm2.info_source.premiere_personne')
        if L.CHIFFRE.search(texte_zone):
            confiance = max(confiance, _seuil('m2.information_source.chiffres_auteur'))
            meilleur = (confiance, 'm2.info_source.chiffres_auteur')
    elif auteur_expert and L.EXPERT_AUTEUR.search(plat_zone):
        meilleur = (_seuil('m2.information_source.auteur_expert'), 'm2.info_source.auteur_expert')
    if meilleur:
        return meilleur
    type_p = page.type_page
    if famille == 'auteur' and type_p['confiance'] >= _seuil('m2.confiance_min') and _seuil('m2.information_source.page_seule'):
        return type_p['confiance'], 'm2.info_source.page_type:' + str(type_p['type'])
    return None


def _score_opinion(plat_zone):
    """Score d'opinion par regles : combinaison des poids des motifs lexicaux."""
    poids = [p for motif, p in L.INDICES_OPINION if motif.search(plat_zone)]
    return _combiner(poids) if poids else 0.0


def _score_connecteur(plat_zone):
    """Poids du meilleur connecteur inferentiel present dans la phrase (0 si aucun)."""
    meilleur = 0.0
    for motif, poids in L.CONNECTEURS_INFERENTIELS:
        if poids > meilleur and motif.search(plat_zone):
            meilleur = poids
    return meilleur


def _score_generale(texte, plat):
    """Score de connaissance generale par heuristiques : (score, veto_specifique).

    Sujet generique (article + nom commun), present gnomique, ni date ni chiffre
    ni entite specifique ni identifiant technique, pas de verbe de
    rapport ni de sujet « meta » (« le texte indique »). Sans marqueur
    gnomique le score plafonne a 0,50 : le cas est ambigu et revient au LLM.
    """
    drapeaux = _specificites(texte, plat)
    if any(drapeaux.values()) or L.IDENTIFIANT_TECHNIQUE.search(texte):
        return 0.0, True
    texte_plat = plat.strip()
    if not L.SUJET_GENERIQUE.match(texte_plat) or L.SUJET_META.match(texte_plat):
        return 0.0, False
    if L.MARQUEURS_ETAT_COURANT.search(plat) or L.ATTRIBUTION_VERBE.search(plat) or re.search(r"\bselon\b", plat):
        return 0.0, False
    score = 0.20
    if not L.TEMPS_NON_GNOMIQUES.search(plat):
        score += 0.10
    score += 0.10
    score += 0.05
    if len(texte.split()) <= 25:
        score += 0.05
    if L.MARQUEURS_GNOMIQUES.search(plat):
        score += 0.35
    return min(score, 1.0), False


# ---------------------------------------------------------------------------
# Etape 2 : sources effectives et typage
# ---------------------------------------------------------------------------

def _sources_des_unites(identifiants):
    """Lignes ``sources`` explicites (ni heritees ni verifications) groupees par unite."""
    serveur = noyau.srv()
    par_unite = {}
    for lot in _lots(list(identifiants), 500):
        marques = ','.join('?' for _ in lot)
        for ligne in serveur.query(
                "SELECT unit_id,url,domain,cited_entity,kind,scope,confidence,contribution FROM sources WHERE unit_id IN (" + marques +
                ") AND inherited_from IS NULL AND kind IN ('lien_interne','lien_externe','source_nommee','note')", tuple(lot)):
            par_unite.setdefault(ligne['unit_id'], []).append(ligne)
    return par_unite


def _statut_source(page, lignes):
    """Meilleure source effective : (sourcing, confiance, regle) ou None.

    Le domaine est reclasse ici (interne / externe) avec les domaines du site,
    car les lignes migrees ont ete classees sans connaitre le site. Un lien
    vers la page elle-meme n'est pas une source.
    """
    minimum = _seuil('m2.confiance_min')
    meilleur_externe = None
    meilleur_interne = None
    for ligne in lignes:
        confiance = float(ligne['confidence'] or 0.0)
        if confiance < minimum:
            continue
        url = ligne['url']
        if url and _est_page_elle_meme(page, url):
            continue
        domaine = ligne['domain'] or (migration.domaine(url) if url else '')
        interne = bool(url) and page.domaine_interne(domaine)
        nature = ligne['kind'] if ligne['kind'] in ('source_nommee', 'note') else ('lien_interne' if interne else 'lien_externe')
        if url and ligne['kind'] == 'note':
            nature = 'note'
        element = (confiance, nature, ligne['scope'])
        if interne:
            if meilleur_interne is None or element[0] > meilleur_interne[0]:
                meilleur_interne = element
        elif meilleur_externe is None or element[0] > meilleur_externe[0]:
            meilleur_externe = element
    if meilleur_externe:
        confiance, nature, portee = meilleur_externe
        return 'sourcee_externe', confiance, 'm2.source.' + str(portee) + '.' + nature
    if meilleur_interne:
        confiance, nature, portee = meilleur_interne
        return 'sourcee_interne', confiance, 'm2.source.' + str(portee) + '.' + nature
    return None


class _Analyse:
    """Caracteristiques d'une unite non sourcee, calculees une fois."""

    def __init__(self, page, unite):
        self.unite = unite
        self.texte_unite = unite['text'] or ''
        debut, fin = unite['span_start'], unite['span_end']
        if debut is not None and fin is not None and page.texte:
            zone_debut, zone_fin = page.zone(debut, fin)
            self.texte_zone = page.texte[zone_debut:zone_fin] or self.texte_unite
            self.position = debut
        else:
            self.texte_zone = self.texte_unite
            self.position = None
        self.plat_zone = L.plat(self.texte_zone)
        self.plat_unite = L.plat(self.texte_unite)
        self.mots = _mots_pleins(self.plat_unite)
        self.section = page.section_de(self.position)
        self.connecteur = _score_connecteur(self.plat_zone + ' ' + self.plat_unite)
        self.score_opinion = _score_opinion(self.plat_zone)
        self.resultat = None


def _unite_precedente(page, analyse, sourcees, analyses):
    """Unite dont depend une interpretation a connecteur (« donc », « cela signifie ») sans paire confirmee :
    l'unite sourcee precedente de la meme section, sinon l'unite precedente de la page. Liste de 0 ou 1 identifiant."""
    position = analyse.unite.get('span_start')
    if position is None:
        return []
    section = page.section_de(position)
    avant = [u for u in sourcees if u.get('span_start') is not None and u['span_start'] < position and u['id'] != analyse.unite['id']]
    meme_section = [u for u in avant if page.section_de(u['span_start']) == section]
    for liste in (meme_section, [a.unite for a in analyses if a.unite.get('span_start') is not None
                                 and a.unite['span_start'] < position]):
        if liste:
            return [max(liste, key=lambda u: u['span_start'])['id']]
    return []


def _decider(analyse, type_, confiance, regle, depend=None):
    """Fixe le resultat d'une unite (la premiere regle confiante l'emporte)."""
    if analyse.resultat is None:
        analyse.resultat = {'type': type_, 'confiance': round(float(confiance), 4), 'regle': regle,
                            'depend': list(depend or [])}


def _entites_specifiques(page, analyse):
    """Vrai si la NER trouve une entite specifique (organisation, personne, lieu, date, montant...) dans l'unite."""
    for entite in moteurs.entites(analyse.texte_unite):
        if entite['type'] in ('personne', 'organisation', 'lieu', 'date', 'montant', 'pourcentage', 'nom_propre', 'produit',
                              'loi', 'collectivite', 'evenement', 'infrastructure'):
            return True
    return False


def _pool_sources(analyses_sourcees):
    """Pool des unites sourcees par section : {section: [(unite_id, texte, mots)]}."""
    pool = {}
    for element in analyses_sourcees:
        pool.setdefault(element['section'], []).append((element['id'], element['texte'], element['mots']))
    return pool


def _paires_nli(analyse, pool, compteurs):
    """Meilleures paires de candidats pour une unite : liste de (id_a, id_b, premisse), au plus ``paires_max``.

    Borne documentee : candidats = unites sourcees de la meme section, les
    ``candidats_max`` mieux recouvrantes ; paires de ces candidats classees par
    somme des recouvrements ; on garde les ``paires_max`` meilleures.
    """
    candidats = []
    for identifiant, texte, mots in pool.get(analyse.section, []):
        if identifiant == analyse.unite['id']:
            continue
        recouvrement = _recouvrement(analyse.mots, mots)
        if recouvrement >= _seuil('m2.interpretation.recouvrement_candidat'):
            candidats.append((recouvrement, identifiant, texte))
    candidats.sort(key=lambda c: (-c[0], c[1]))
    candidats = candidats[:int(_seuil('m2.interpretation.candidats_max'))]
    paires = []
    for i in range(len(candidats)):
        for j in range(i + 1, len(candidats)):
            paires.append((candidats[i][0] + candidats[j][0], candidats[i][1], candidats[j][1],
                           candidats[i][2] + ' ' + candidats[j][2]))
    paires.sort(key=lambda p: (-p[0], p[1], p[2]))
    meilleur = candidats[0][0] if candidats else 0.0
    analyse.meilleur_recouvrement = meilleur
    analyse.candidats = [c[1] for c in candidats]
    return [(a, b, premisse) for _, a, b, premisse in paires[:int(_seuil('m2.interpretation.paires_max'))]]


def _tester_interpretations(page, analyses, pool, run, compteurs, avec_llm):
    """Test NLI borne : pour chaque unite testee, l'unite est-elle impliquee par deux autres unites sourcees ?

    Renvoie {unit_id: (score, [id_a, id_b])} pour les implications au-dessus de ``m2.interpretation.nli``,
    et ``nli_a_tourne`` (False si le NLI est indisponible ou interdit).
    """
    confirmes = {}
    a_tester = []
    for analyse in analyses:
        analyse.meilleur_recouvrement = 0.0
        analyse.candidats = []
        if analyse.resultat is not None or not analyse.mots:
            continue
        paires = _paires_nli(analyse, pool, compteurs)
        if not paires or not avec_llm:
            continue
        if analyse.connecteur > 0 or analyse.meilleur_recouvrement >= _seuil('m2.interpretation.recouvrement_sans_connecteur'):
            a_tester.append((0 if analyse.connecteur > 0 else 1, -analyse.meilleur_recouvrement, analyse.unite['id'], analyse, paires))
    if not avec_llm:
        return confirmes, False
    a_tester.sort(key=lambda e: (e[0], e[1], e[2]))
    plafond = int(_seuil('m2.interpretation.paires_max_page'))
    soumises = []
    for _, _, identifiant, analyse, paires in a_tester:
        for a, b, premisse in paires:
            if len(soumises) >= plafond:
                break
            soumises.append((identifiant, a, b, premisse, analyse.texte_unite))
    if not soumises:
        return confirmes, True
    serveur = noyau.srv()
    taille = int(_seuil('nli.taille_lot'))
    seuil = _seuil('m2.interpretation.nli')
    try:
        for lot in _lots(soumises, taille):
            serveur.checkpoint()
            sorties = moteurs.nli_lot([(premisse, hypothese) for _, _, _, premisse, hypothese in lot])
            compteurs['paires_nli'] += len(lot)
            for (identifiant, a, b, _, _), sortie in zip(lot, sorties):
                if sortie['label'] == 'implication' and sortie['score'] >= seuil:
                    ancien = confirmes.get(identifiant)
                    if ancien is None or sortie['score'] > ancien[0]:
                        confirmes[identifiant] = (float(sortie['score']), [a, b])
        run.moteur('nli', moteurs.dernier('nli'))
    except (serveur.OperationalError, RuntimeError, moteurs.Indisponible):
        if _arret_demande():
            raise
        compteurs['nli_indisponible'] += 1
        return {}, False
    return confirmes, True


def _trancher_generales(analyses, run, compteurs):
    """Question fermee au LLM local (schema contraint) pour les cas ambigus ; revalidation par veto specifique.

    Renvoie {unit_id: (general, confiance)}.
    """
    reponses = {}
    serveur = noyau.srv()
    taille = int(_seuil('m2.llm.taille_lot'))
    for lot in _lots(analyses, taille):
        serveur.checkpoint()
        payload = {'mode': 'chaine:m2.generale', 'question': L.QUESTION_FERMEE,
                   'items': [{'id': index, 'texte': a.texte_unite} for index, a in enumerate(lot)]}
        try:
            sortie = noyau.llm(TACHE_GENERALE, payload)
        except (serveur.OperationalError, RuntimeError):
            if _arret_demande():
                raise
            compteurs['llm_echecs'] += 1
            continue
        compteurs['llm_appels'] += 1
        run.moteur('llm', noyau.moteur_llm())
        for element in sortie['resultats']:
            index = element['id']
            if 0 <= index < len(lot) and lot[index].unite['id'] not in reponses:
                reponses[lot[index].unite['id']] = (bool(element['general']), min(1.0, max(0.0, float(element['confiance']))))
    return reponses


def typer(unit_ids, run=None, avec_llm=None):
    """Etape 2 : source ou type de chaque unite, par la cascade du document.

    Une unite avec une source de confiance suffisante est ``sourcee_externe`` ou
    ``sourcee_interne`` (pas de ligne ``claim_types``). Les autres : information_source
    (-> auto_sourcee), opinion, interpretation (-> dependante, ``depends_on``),
    connaissance_generale, sans_source ; la premiere regle assez confiante
    (``m2.confiance_min``) l'emporte. Ecrit ``units.sourcing``, ``sourcing_conf``,
    ``sourcing_rule`` et ``claim_types``. Appelle ``sources_explicites`` pour les
    versions dont les sources n'ont pas ete calculees. Renvoie un dict de compteurs.
    """
    serveur = noyau.srv()
    autorise = _llm_autorise(avec_llm)
    compteurs = {'unites': 0, 'sourcees': 0, 'par_sourcing': {}, 'par_regle': {}, 'paires_nli': 0, 'nli_indisponible': 0,
                 'llm_appels': 0, 'llm_echecs': 0, 'pages': 0, 'classifieur_unites': 0}
    unites = _unites_eligibles(unit_ids=unit_ids)
    par_version = {}
    for unite in unites:
        par_version.setdefault(unite['version_id'], []).append(unite)
    site = _domaines_site()
    with _execution(run, 'm2.typer', {'typologie': 'regles+classifieur'}, pages=len(par_version), items=len(unites)) as exec_:
        for version_id, liste in par_version.items():
            serveur.checkpoint()
            page = _charger_page(version_id, site)
            if page is None:
                continue
            if not serveur.query('SELECT 1 FROM settings WHERE key=?', (_marqueur_sources(version_id),), True):
                sources_explicites(version_id, run=exec_)
            _typer_page(page, liste, exec_, compteurs, autorise)
            compteurs['pages'] += 1
        exec_.moteur('phrases', moteurs.dernier('phrases'))
        exec_.moteur('entites', moteurs.dernier('entites'))
    return compteurs


def _typer_page(page, unites, run, compteurs, avec_llm):
    """Cascade complete pour les unites d'une page, puis ecriture."""
    serveur = noyau.srv()
    par_unite = _sources_des_unites([u['id'] for u in unites])
    infos_page = type_page(page, avec_llm)
    famille = infos_page['famille']
    auteurs = str(noyau.reglage('m2:auteurs_experts', '') or '')
    auteur_expert = bool(page.ligne.get('author')) and L.plat(page.ligne['author']) in L.plat(auteurs)
    resultats = {}
    analyses = []
    sourcees = []
    for unite in unites:
        statut = _statut_source(page, par_unite.get(unite['id'], []))
        if statut:
            resultats[unite['id']] = {'sourcing': statut[0], 'confiance': round(statut[1], 4), 'regle': statut[2], 'type': None,
                                      'depend': []}
            sourcees.append(unite)
        else:
            analyses.append(_Analyse(page, unite))
    # Regle 1 : information source.
    for analyse in analyses:
        trouve = _regle_information_source(page, analyse.texte_zone, analyse.plat_zone, famille, auteur_expert)
        if trouve:
            _decider(analyse, 'information_source', trouve[0], trouve[1])
    # Regle 2 : opinion (regles, puis classifieur de subjectivite sur les cas ambigus).
    minimum = _seuil('m2.confiance_min')
    seuil_opinion = _seuil('m2.opinion.score_min')
    a_classer = []
    for analyse in analyses:
        if analyse.resultat is not None:
            continue
        score = analyse.score_opinion
        if score >= seuil_opinion:
            _decider(analyse, 'opinion', score, 'm2.opinion.regles')
            continue
        page_opinion = famille == 'opinion' and infos_page['confiance'] >= minimum
        if page_opinion:
            specifique = any(_specificites(analyse.texte_unite, analyse.plat_unite).values())
            base = _seuil('m2.opinion.page') - (0.10 if specifique else 0.0)
            confiance = _combiner([base, score]) if score else base
            if confiance >= seuil_opinion:
                _decider(analyse, 'opinion', confiance, 'm2.opinion.page:' + str(infos_page['type']))
                continue
        if score >= _seuil('m2.opinion.classifieur_ambigu_bas') or _seuil('m2.opinion.classifieur_toutes'):
            if len(analyse.texte_zone.split()) >= 5:
                a_classer.append(analyse)
    if a_classer:
        try:
            sorties, moteur = _classer([a.texte_zone for a in a_classer], L.ETIQUETTES_SUBJECTIVITE, L.EXEMPLES_SUBJECTIVITE, avec_llm)
            run.moteur('subjectivite', moteur)
            compteurs['classifieur_unites'] += len(a_classer)
            for analyse, sortie in zip(a_classer, sorties):
                if sortie['label'] != 'subjectif':
                    continue
                confiance = _confiance_classifieur(sortie)
                if confiance >= seuil_opinion:
                    _decider(analyse, 'opinion', confiance, 'm2.opinion.classifieur:' + moteur)
                elif analyse.score_opinion and _combiner([analyse.score_opinion, 0.5 * confiance]) >= seuil_opinion:
                    _decider(analyse, 'opinion', _combiner([analyse.score_opinion, 0.5 * confiance]),
                             'm2.opinion.regles+classifieur:' + moteur)
        except (moteurs.Indisponible, ValueError, KeyError):
            pass
        except (serveur.OperationalError, RuntimeError):
            if _arret_demande():
                raise
    # Regle 3 : interpretation (connecteurs + NLI borne sur les unites sourcees de la meme section).
    pool_unites = []
    for unite in sourcees:
        plat_u = L.plat(unite['text'] or '')
        position = unite['span_start']
        pool_unites.append({'id': unite['id'], 'texte': unite['text'] or '', 'mots': _mots_pleins(plat_u),
                            'section': page.section_de(position)})
    pool = _pool_sources(pool_unites)
    candidats_interpretation = [a for a in analyses if a.resultat is None]
    confirmes, nli_a_tourne = _tester_interpretations(page, candidats_interpretation, pool, run, compteurs, avec_llm)
    for analyse in candidats_interpretation:
        identifiant = analyse.unite['id']
        if identifiant in confirmes:
            score, parents = confirmes[identifiant]
            confiance = (min(_seuil('m2.interpretation.confiance_max'), score + _seuil('m2.interpretation.bonus_connecteur'))
                         if analyse.connecteur else score)
            _decider(analyse, 'interpretation', confiance, 'm2.interpretation.nli', parents)
        elif analyse.connecteur:
            facteur = _seuil('m2.connecteur.non_confirme') if nli_a_tourne else 1.0
            confiance = analyse.connecteur * facteur
            if confiance >= minimum:
                parents = list(getattr(analyse, 'candidats', []))[:2] or _unite_precedente(page, analyse, sourcees, analyses)
                if parents:
                    # Sans unite dont elle depend, une « interpretation » n'aurait aucun statut a suivre.
                    _decider(analyse, 'interpretation', confiance, 'm2.interpretation.connecteur', parents)
    # Regle 4 : connaissance generale (heuristiques, puis question fermee au LLM local pour les ambigus).
    ambigus = []
    seuil_general = _seuil('m2.connaissance_generale.score_min')
    bas = _seuil('m2.generale.ambigu_bas')
    for analyse in analyses:
        if analyse.resultat is not None:
            continue
        score, veto = _score_generale(analyse.texte_unite, analyse.plat_unite)
        if veto or score < bas:
            continue
        if _entites_specifiques(page, analyse):
            continue
        if score >= seuil_general:
            _decider(analyse, 'connaissance_generale', score, 'm2.generale.regles')
        elif avec_llm:
            ambigus.append(analyse)
    if ambigus:
        reponses = _trancher_generales(ambigus, run, compteurs)
        for analyse in ambigus:
            reponse = reponses.get(analyse.unite['id'])
            if reponse and reponse[0]:
                _decider(analyse, 'connaissance_generale', min(0.85, max(seuil_general, reponse[1])), 'm2.generale.llm')
            elif reponse:
                _decider(analyse, 'sans_source', 0.70, 'm2.sans_source.llm_non_general')
    # Regle 5 : sans source, par defaut (confiance plus forte avec chiffre, date, entite nommee ou etat courant).
    for analyse in analyses:
        if analyse.resultat is None:
            drapeaux = _specificites(analyse.texte_unite, analyse.plat_unite)
            confiance = _seuil('m2.sans_source.confiance_base')
            confiance += 0.10 * (drapeaux['chiffre'] or drapeaux['annee'])
            confiance += 0.10 * (drapeaux['mois'] or drapeaux['majuscule'])
            confiance += 0.10 * (analyse.unite['obs_class'] == 'etat_courant')
            _decider(analyse, 'sans_source', min(confiance, 0.95), 'm2.sans_source.defaut')
        resultats[analyse.unite['id']] = {
            'sourcing': TYPE_VERS_SOURCING[analyse.resultat['type']], 'confiance': analyse.resultat['confiance'],
            'regle': analyse.resultat['regle'], 'type': analyse.resultat['type'], 'depend': analyse.resultat['depend'],
        }
    _ecrire_resultats(page, unites, resultats, run, compteurs)


def _ecrire_resultats(page, unites, resultats, run, compteurs):
    """Ecrit ``units.sourcing*``, ``claim_types``, la suite donnee (statut, date) en une transaction."""
    serveur = noyau.srv()
    precedents = {}
    identifiants = [u['id'] for u in unites]
    for lot in _lots(identifiants, 500):
        marques = ','.join('?' for _ in lot)
        for ligne in serveur.query('SELECT unit_id, type FROM claim_types WHERE unit_id IN (' + marques + ')', tuple(lot)):
            precedents[ligne['unit_id']] = ligne['type']
    publication = migration.ancre_periode(page.ligne.get('updated')) or migration.ancre_periode(page.ligne.get('published'))
    genre_date = 'maj' if migration.ancre_periode(page.ligne.get('updated')) else 'publication'
    with noyau.connexion() as base:
        for unite in unites:
            resultat = resultats[unite['id']]
            base.execute('UPDATE units SET sourcing=?, sourcing_conf=?, sourcing_rule=? WHERE id=?',
                         (resultat['sourcing'], resultat['confiance'], resultat['regle'], unite['id']))
            type_ = resultat['type']
            if type_ is None:
                base.execute('DELETE FROM claim_types WHERE unit_id=?', (unite['id'],))
            else:
                base.execute('INSERT OR REPLACE INTO claim_types(unit_id,type,confidence,rule,depends_on,run_id) VALUES(?,?,?,?,?,?)',
                             (unite['id'], type_, resultat['confiance'], resultat['regle'],
                              json.dumps(resultat['depend']) if resultat['depend'] else None, run.id))
            statut = TYPE_VERS_STATUT.get(type_)
            if statut:
                base.execute('UPDATE units SET status=?, status_run=? WHERE id=? AND verdict IS NULL', (statut, run.id, unite['id']))
            elif precedents.get(unite['id']) in TYPE_VERS_STATUT:
                base.execute("UPDATE units SET status='a_verifier', status_run=? WHERE id=? AND verdict IS NULL",
                             (run.id, unite['id']))
            if type_ == 'information_source' and publication:
                base.execute("UPDATE units SET anchor_date=?, anchor_kind=? WHERE id=? AND (anchor_date IS NULL OR anchor_date='')",
                             (publication, genre_date, unite['id']))
            nom = resultat['sourcing']
            compteurs['unites'] += 1
            if nom.startswith('sourcee'):
                compteurs['sourcees'] += 1
            compteurs['par_sourcing'][nom] = compteurs['par_sourcing'].get(nom, 0) + 1
            regle = resultat['regle'].split(':')[0]
            compteurs['par_regle'][regle] = compteurs['par_regle'].get(regle, 0) + 1


# ---------------------------------------------------------------------------
# Etape 3 : references internes manquantes
# ---------------------------------------------------------------------------

def signaler_manquantes(run=None):
    """Etape 3 : drapeau ``units.source_manquante`` sur les unites sans source dont l'equivalent existe ailleurs.

    Une unite ``sans_source`` est signalee si son groupe (``unit_groups``)
    contient une unite d'une autre page du site (autre ``versions.page_id``).
    M2 ne retrouve pas la page d'origine : c'est M3. Le drapeau est recalcule a
    chaque appel (remis a 0 s'il ne se justifie plus). Renvoie les compteurs.
    """
    with _execution(run, 'm2.manquantes', {'groupes': 'sql'}) as exec_:
        with noyau.connexion() as base:
            base.execute(
                "UPDATE units SET source_manquante=0 WHERE COALESCE(source_manquante,0)=1 AND sourcing IS NOT NULL AND (" + CLAUSE_ELIGIBLE.replace('u.', 'units.') + ")")
            curseur = base.execute(
                "UPDATE units SET source_manquante=1 WHERE sourcing='sans_source' AND COALESCE(excluded,0)=0 AND EXISTS("
                "SELECT 1 FROM unit_group_members m1 "
                "JOIN unit_group_members m2 ON m2.group_id=m1.group_id AND m2.unit_id<>m1.unit_id "
                "JOIN units u2 ON u2.id=m2.unit_id AND COALESCE(u2.excluded,0)=0 "
                "JOIN versions v2 ON v2.id=u2.version_id "
                "JOIN versions v1 ON v1.id=units.version_id "
                "WHERE m1.unit_id=units.id AND COALESCE(v2.page_id,v2.id)<>COALESCE(v1.page_id,v1.id))")
            signalees = curseur.rowcount
            total = base.execute("SELECT COUNT(*) FROM units WHERE sourcing='sans_source'").fetchone()[0]
        exec_.items = signalees
    return {'signalees': signalees, 'sans_source': total}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _versions_a_traiter(version_ids):
    """Versions ayant des unites obsolescibles : liste de (version_id, nombre d'unites)."""
    serveur = noyau.srv()
    requete = ('SELECT u.version_id, COUNT(*) n FROM units u WHERE ' + CLAUSE_ELIGIBLE + ' AND u.version_id IS NOT NULL')
    arguments = []
    if version_ids is not None:
        version_ids = list(version_ids)
        if not version_ids:
            return []
        requete += ' AND u.version_id IN (' + ','.join('?' for _ in version_ids) + ')'
        arguments = version_ids
    return [(ligne['version_id'], ligne['n']) for ligne in
            serveur.query(requete + ' GROUP BY u.version_id ORDER BY u.version_id', tuple(arguments))]


def executer(version_ids=None, forcer=False, aujourdhui=None, avec_llm=None):
    """Execute M2 : sources explicites, typologie, drapeau source_manquante.

    Reprenable : une version dont les sources M2 sont calculees n'est pas
    recalculee, une unite deja typee (``sourcing`` renseigne) n'est pas retypee,
    sauf ``forcer=True``. ``avec_llm=False`` (ou reglage ``m2:llm`` = non)
    interdit tout appel LLM : pas de NLI par repli LLM, pas de question fermee,
    classifieur lexical seul. ``aujourdhui`` est accepte pour homogeneite
    (M2 ne depend pas de la date du jour). Renvoie un dict de compteurs.
    """
    serveur = noyau.srv()
    autorise = _llm_autorise(avec_llm)
    versions = _versions_a_traiter(version_ids)
    debut = time.monotonic()
    total = {'versions': len(versions), 'versions_traitees': 0, 'unites': 0, 'sources': 0, 'unites_avec_source': 0,
             'par_sourcing': {}, 'par_regle': {}, 'paires_nli': 0, 'llm_appels': 0, 'llm_echecs': 0, 'nli_indisponible': 0,
             'classifieur_unites': 0, 'par_type_source': {}, 'par_portee': {}}
    with noyau.etape('m2', moteurs={'sources': 'regles', 'typologie': 'regles+classifieur'}, prompt=VERSION_PROMPT,
                     seuils=NOMS_SEUILS, pages=len(versions)) as run:
        run.note = 'aujourdhui=' + str(aujourdhui or '') + ' llm=' + ('oui' if autorise else 'non')
        serveur.JOB.update(stage='m2.sources', done=0, total=len(versions), message='M2 : sources et typologie')
        for index, (version_id, _) in enumerate(versions):
            serveur.checkpoint()
            serveur.JOB.update(stage='m2.sources', done=index, total=len(versions),
                               message='M2 : version ' + str(index + 1) + ' sur ' + str(len(versions)))
            if forcer or not serveur.query('SELECT 1 FROM settings WHERE key=?', (_marqueur_sources(version_id),), True):
                rapport = sources_explicites(version_id, run=run)
                total['sources'] += rapport['sources']
                total['unites_avec_source'] += rapport['unites_avec_source']
                for cle, valeur in rapport['par_type'].items():
                    total['par_type_source'][cle] = total['par_type_source'].get(cle, 0) + valeur
                for cle, valeur in rapport['par_portee'].items():
                    total['par_portee'][cle] = total['par_portee'].get(cle, 0) + valeur
            serveur.JOB.update(stage='m2.typologie')
            unites = _unites_eligibles(version_id)
            a_typer = [u['id'] for u in unites if forcer or not u['sourcing']]
            if a_typer:
                rapport = typer(a_typer, run=run, avec_llm=autorise)
                total['unites'] += rapport['unites']
                for cle in ('paires_nli', 'llm_appels', 'llm_echecs', 'nli_indisponible', 'classifieur_unites'):
                    total[cle] += rapport[cle]
                for cle in ('par_sourcing', 'par_regle'):
                    for nom, valeur in rapport[cle].items():
                        total[cle][nom] = total[cle].get(nom, 0) + valeur
            total['versions_traitees'] += 1
            run.pages = index + 1
        serveur.JOB.update(stage='m2.manquantes', done=len(versions), total=len(versions), message='M2 : références manquantes')
        total['manquantes'] = signaler_manquantes(run=run)
        run.items = total['unites']
        run.moteur('phrases', moteurs.dernier('phrases'))
        serveur.JOB.update(message='M2 terminée')
    total['run_id'] = run.id
    total['duree_s'] = round(time.monotonic() - debut, 2)
    return total
