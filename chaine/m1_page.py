"""M1 etapes 1, 2, 3 et 6 : acquisition, segmentation, contexte, classes d'obsolescence.

Specification : ``docs/METHODOLOGIES.txt`` (M1 etapes 1, 2, 3, 6 et statut
``perimee_par_date`` de M3 etape 5). Contrat : ``docs/CONTRAT.md`` section 5.

Fonctions publiques :

- ``acquerir(url=None, html=None, texte=None, titre='') -> version_id`` (etape 1) ;
- ``metadonnees(html, url='', texte=None)`` (etape 1) ;
- ``structurer_texte(texte)`` : structure d'un texte deja importe (sans HTML) ;
- ``segmenter(version_id, forcer=False)`` (etape 2) ;
- ``resoudre_contexte(version_id, forcer=False)`` (etape 3) et ``ancrer(unit_ids=None)`` ;
- ``classer_obsolescence(unit_ids=None)`` (etape 6) et ``perimer(aujourdhui=None)`` ;
- ``executer(version_ids=None, forcer=False, aujourdhui=None)`` ;
- outils : ``simhash``, ``distance_simhash``, ``contexte_texte``, ``phrases_du_chunk``.

Les nouveaux chunks se distinguent des anciens chunks de 5 000 caracteres
(``kind IS NULL``, toujours references par ``occurrences``) par leur ``kind`` :
``titre | paragraphe | liste | citation | note | metadonnee | usage``. Ils sont
inseres avec ``prepared=1`` pour que l'ancien pipeline (``server.prepare``) les
ignore. Leur colonne ``context`` est un JSON ::

    {"titre": titre de la page, "section": "H2 > H3", "date": date de la page,
     "date_type": "maj" | "publication" | null, "position": rang du bloc,
     "phrases": [[debut, fin], ...],          # offsets absolus dans versions.text
     "dates": [...],                           # etape 3 : expressions temporelles (offsets absolus)
     "coreferences": [...],                    # etape 3 : anaphores resolues
     "reference": date de reference des expressions relatives}
"""
import bisect
import importlib
import hashlib
import html as html_lib
import json
import re
from contextlib import contextmanager
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin

from chaine import dates
from chaine import moteurs
from chaine import noyau
from chaine import schema
from chaine import seuils

VERSION_REGLES = 'm1-page-v1'
NOUVEAUX_KINDS = ('titre', 'paragraphe', 'liste', 'citation', 'note', 'metadonnee', 'usage')
CLASSES = ('immuable', 'etat_courant', 'annonce', 'metadonnee', 'usage')
# Le classifieur ne departage que les classes de contenu ; metadonnee et usage relevent du segment et des regles.
CLASSES_CLASSIFIEUR = ('immuable', 'etat_courant', 'annonce')
PRIORITE_CLASSES = {'usage': 5, 'metadonnee': 4, 'annonce': 3, 'etat_courant': 2, 'immuable': 1, 'inconnu': 0}

# Table de secours prédicat -> classe, utilisee seulement si ``chaine.predicats``
# (lot « m1_unites ») est absent. Le module des predicats fait foi.
PREDICATS_SECOURS = {
    'est_un': 'immuable', 'a_pour_auteur': 'immuable', 'date_de_naissance': 'immuable', 'date_de_creation': 'immuable',
    'a_eu_lieu_le': 'immuable', 'date_de_publication': 'immuable',
    'a_pour_dirigeant': 'etat_courant', 'a_pour_proprietaire': 'etat_courant', 'prix': 'etat_courant',
    'taux': 'etat_courant', 'statut': 'etat_courant', 'regle': 'etat_courant',
    'aura_lieu_le': 'annonce', 'date_de_debut': 'annonce', 'date_de_fin': 'annonce', 'prevoit': 'annonce',
    'a_pour_objectif': 'annonce',
}


_COLONNES_PRETES = set()


# ---------------------------------------------------------------------------
# Outils communs
# ---------------------------------------------------------------------------

def _seuil(nom):
    """Seuil de ``chaine.seuils`` (tous les seuils du module y sont declares)."""
    return seuils.get(nom)


def _assurer_colonnes():
    """Colonnes ajoutees par ce lot (hors contrat, signalees dans le rapport). Idempotent."""
    serveur = noyau.srv()
    cle = str(serveur.DB)
    if cle in _COLONNES_PRETES:
        return
    schema.colonne('chunks', 'run_id', 'TEXT')
    schema.colonne('versions', 'segment_run', 'TEXT')
    schema.colonne('units', 'anchor_run', 'TEXT')
    schema.colonne('units', 'obs_run', 'TEXT')
    serveur.query('CREATE INDEX IF NOT EXISTS idx_m1_chunks_version_kind ON chunks(version_id, kind)')
    _COLONNES_PRETES.add(cle)


@contextmanager
def _execution(nom, **options):
    """Identifiant d'execution : celui de l'etape en cours, sinon une nouvelle etape ``nom``."""
    courante = noyau.execution_courante()
    if courante:
        yield courante
        return
    with noyau.etape(nom, **options) as run:
        yield run.id


def _json(valeur):
    """JSON compact pour stockage."""
    return json.dumps(valeur, ensure_ascii=False, separators=(',', ':'))


def _charger_json(texte, defaut=None):
    """JSON tolerant (None ou invalide -> defaut)."""
    if not texte:
        return defaut
    try:
        return json.loads(texte)
    except (ValueError, TypeError):
        return defaut


def _marques(valeurs):
    """« ?,?,? » pour une clause IN."""
    return ','.join('?' for _ in valeurs)


def _par_lots(valeurs, taille):
    """Decoupe une liste en lots."""
    for debut in range(0, len(valeurs), taille):
        yield valeurs[debut:debut + taille]


def _aujourdhui(valeur):
    """Date du jour injectable."""
    jour = dates.en_date(valeur)
    return jour or date.today()


# ---------------------------------------------------------------------------
# Etape 1 — Extraction HTML (repli maison) et metadonnees
# ---------------------------------------------------------------------------

BLOCS_HTML = {'p', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'figcaption', 'dt', 'dd', 'tr', 'caption'}
CONTENEURS_HTML = {'div', 'section', 'article', 'main', 'header', 'ul', 'ol', 'dl', 'table', 'tbody', 'thead', 'tfoot',
                   'figure', 'body', 'html', 'details', 'summary', 'address', 'center', 'hgroup'}
IGNORES_HTML = {'script', 'style', 'noscript', 'svg', 'template', 'iframe', 'button', 'select', 'option', 'form', 'nav',
                'footer', 'aside', 'canvas', 'object', 'embed', 'head', 'textarea', 'label', 'input', 'audio', 'video', 'map'}
VIDES_HTML = {'br', 'img', 'meta', 'link', 'input', 'hr', 'wbr', 'source', 'area', 'base', 'col', 'embed', 'param', 'track'}
PARTIES_USAGE = {
    'share', 'sharing', 'sharer', 'partage', 'partager', 'social', 'socials', 'related', 'similar', 'similaires',
    'comment', 'comments', 'commentaires', 'commentaire', 'disqus', 'newsletter', 'breadcrumb', 'breadcrumbs', 'ariane',
    'toc', 'sommaire', 'pagination', 'cookie', 'cookies', 'advert', 'ads', 'pub', 'promo', 'readtime', 'readingtime',
    'reading', 'recommended', 'recommandations', 'aussi', 'readalso', 'popular', 'subscribe', 'abonnement',
}
PARTIES_CHAPO = {'chapo', 'chapeau', 'lead', 'lede', 'standfirst', 'intro', 'introduction', 'excerpt', 'summary',
                 'accroche', 'chapô', 'kicker', 'deck'}
ROLES_IGNORES = {'navigation', 'complementary', 'banner', 'contentinfo', 'search', 'dialog'}

_RE_USAGE = re.compile(
    r"temps de lecture|\b\d+\s*min(?:utes?)?\s+(?:de\s+)?lecture|lecture\s*:\s*\d+\s*min|\bpartagez\b|\bpartager (?:sur|cet|cette|ce|l')|\bpartage sur\b"
    r"|\ba lire (?:aussi|egalement)\b|\blire aussi\b|\bvoir aussi\b|\babonnez-vous\b|\binscrivez-vous\b|\bnewsletter\b"
    r"|\b\d+\s+commentaires?\b|\blaisser un commentaire\b|\breagissez\b|^sommaire\b|^table des matieres\b"
    r"|\bcliquez ici\b|\bsuivez-nous\b|\bimprimer (?:cet|l')article\b|\bcopier le lien\b"
)


def _est_usage(norm):
    """Bloc ou unite d'usage : texte court qui commence par une formule d'usage (lecture, partage, « a lire aussi »...)."""
    texte = norm.strip()
    if len(texte) >= 150:
        return False
    correspondance = _RE_USAGE.search(texte)
    return bool(correspondance) and correspondance.start() <= 15


_RE_META_BLOC = re.compile(
    r"^(?:publie(?:e)?|mis en ligne|mise? a jour|derniere mise a jour|actualise(?:e)?|modifie(?:e)?|redige par|ecrit par"
    r"|auteur|autrice|par\s+[a-z][\w'-]+(?:\s+[a-z][\w'-]+){0,3}\s*(?:[,|–—-]|$|le\b))"
    r"|^[—–-]\s*[a-z][\w.'-]*(?:\s+[a-z][\w.'-]*){0,3}\s*$"
    r"|\b(?:etat|situation|mise a jour)\s+(?:du|au)\s+\d"
)
_RE_SECTION_NOTES = re.compile(r"^(?:sources?|references?|notes?|bibliographie|pour aller plus loin|liens?|credits?)\b")
_RE_URL_SEULE = re.compile(r'^\s*(?:https?://|www\.)\S+\s*$')
_RE_LISTE = re.compile(r'^\s*(?:[-*•–—]\s+|\d{1,3}[.)]\s+)')
_RE_MARKDOWN_TITRE = re.compile(r'^\s*(#{1,6}|={2,6})\s*\S')
_FIN_PHRASE = re.compile(r'[.!?;:,…»"”)\]]\s*$')


def _parties_classes(attributs):
    """Mots des attributs class et id, decoupes sur - et _."""
    valeurs = ' '.join(str(attributs.get(cle) or '') for cle in ('class', 'id'))
    parties = set()
    for jeton in valeurs.lower().split():
        parties.add(jeton)
        parties.update(p for p in re.split(r'[-_]', jeton) if p)
    return parties


class _ExtracteurHTML(HTMLParser):
    """Parseur HTML maison : blocs (titres Hn, paragraphes, listes, citations), liens et citations
    avec offsets, metadonnees d'en-tete (title, meta, JSON-LD, time) et exclusions d'usage."""

    def __init__(self, url='', principal=True, classes_usage=True):
        super().__init__(convert_charrefs=True)
        self.url = url or ''
        self.filtrer_principal = principal
        self.classes_usage = classes_usage
        self.pile = []
        self.tampon = ''
        self.type_tampon = None
        self.niveau_tampon = None
        self.etat_tampon = {}
        self.liens_tampon = []
        self.citations_tampon = []
        self.liens_ouverts = []
        self.citations_ouvertes = []
        self.blocs = []
        self.exclus = []
        self.texte_exclu = ''
        self.titre_html = ''
        self.metas = {}
        self.jsonld = []
        self.temps = []
        self.langue = ''
        self._script = None

    # -- etat de la pile -------------------------------------------------
    def _drapeau(self, nom):
        return any(element[1].get(nom) for element in self.pile)

    def _ignore(self):
        return self._drapeau('ignore') or self._drapeau('usage')

    def _type_courant(self):
        niveau = None
        for tag, _ in reversed(self.pile):
            if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
                niveau = int(tag[1])
                return 'titre', niveau
        tags = {tag for tag, _ in self.pile}
        if 'blockquote' in tags:
            return 'citation', None
        if tags & {'li', 'dd', 'dt'}:
            return 'liste', None
        if tags & {'figcaption', 'caption'}:
            return 'note', None
        return 'paragraphe', None

    # -- blocs -------------------------------------------------------------
    def _vider(self):
        texte = self.tampon.rstrip()
        if texte.strip():
            longueur = len(texte)
            self.blocs.append({
                'kind': self.type_tampon or 'paragraphe',
                'niveau': self.niveau_tampon,
                'texte': texte,
                'liens': [(d, min(f, longueur), h) for d, f, h in self.liens_tampon if d < longueur],
                'citations': [(d, min(f, longueur)) for d, f in self.citations_tampon if d < longueur],
                'principal': bool(self.etat_tampon.get('principal')),
                'chapo': bool(self.etat_tampon.get('chapo')),
            })
        self.tampon = ''
        self.type_tampon = None
        self.niveau_tampon = None
        self.etat_tampon = {}
        self.liens_tampon = []
        self.citations_tampon = []
        self.liens_ouverts = []
        self.citations_ouvertes = []

    def _ajouter(self, donnees):
        donnees = re.sub(r'\s+', ' ', donnees)
        if not self.tampon or self.tampon.endswith(' '):
            donnees = donnees.lstrip()
        if not donnees:
            return
        if not self.tampon:
            self.type_tampon, self.niveau_tampon = self._type_courant()
            self.etat_tampon = {'principal': self._drapeau('principal'), 'chapo': self._drapeau('chapo')}
        self.tampon += donnees

    # -- evenements du parseur ----------------------------------------------
    def handle_starttag(self, tag, attrs):
        attributs = {cle: (valeur or '') for cle, valeur in attrs}
        if tag == 'html':
            self.langue = attributs.get('lang', '')[:10]
        if tag == 'meta':
            cle = (attributs.get('property') or attributs.get('name') or attributs.get('itemprop') or '').strip().lower()
            if cle and 'content' in attributs and cle not in self.metas:
                self.metas[cle] = attributs['content'].strip()
            return
        if tag == 'time':
            self.temps.append({'datetime': attributs.get('datetime', ''), 'itemprop': attributs.get('itemprop', '').lower(),
                               'classe': ' '.join(_parties_classes(attributs))})
        if tag == 'br':
            if not self._ignore() and self.tampon and not self.tampon.endswith(' '):
                self.tampon += ' '
            return
        if tag in VIDES_HTML:
            return
        if tag == 'title':
            self.pile.append((tag, {'titre': True, 'ignore': True}))
            return
        if tag == 'script':
            type_script = attributs.get('type', '').lower()
            self._script = [] if 'ld+json' in type_script else None
            self.pile.append((tag, {'ignore': True}))
            return
        drapeaux = {}
        parties = _parties_classes(attributs)
        role = attributs.get('role', '').lower()
        if tag in IGNORES_HTML or role in ROLES_IGNORES or 'hidden' in attributs:
            drapeaux['ignore'] = True
        if self.classes_usage and (parties & PARTIES_USAGE or 'partag' in attributs.get('aria-label', '').lower()):
            drapeaux['usage'] = True
        if tag in ('main', 'article') or role == 'main' or attributs.get('itemprop', '').lower() == 'articlebody':
            drapeaux['principal'] = True
        if parties & PARTIES_CHAPO or attributs.get('itemprop', '').lower() == 'description':
            drapeaux['chapo'] = True
        if tag in BLOCS_HTML or tag in CONTENEURS_HTML:
            if self.pile and self.pile[-1][0] == tag and tag in ('p', 'li', 'dt', 'dd', 'tr'):
                self._fermer(tag)
            self._vider()
        if tag in ('td', 'th') and self.tampon and not self.tampon.endswith(' | '):
            self.tampon = self.tampon.rstrip() + ' | '
        if tag == 'a' and not self._ignore():
            lien = attributs.get('href', '').strip()
            if lien and not lien.startswith(('#', 'javascript:', 'mailto:')):
                self.liens_ouverts.append((urljoin(self.url, lien), len(self.tampon)))
        if tag in ('q', 'cite') and not self._ignore():
            self.citations_ouvertes.append(len(self.tampon))
        self.pile.append((tag, drapeaux))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VIDES_HTML and tag not in ('title', 'script'):
            self.handle_endtag(tag)

    def _fermer(self, tag):
        """Depile jusqu'a ``tag`` inclus."""
        while self.pile:
            ouvert, drapeaux = self.pile.pop()
            if ouvert == 'title':
                self.titre_html = self.titre_html.strip()
            if ouvert == 'script' and self._script is not None:
                self.jsonld.append(''.join(self._script))
                self._script = None
            if (drapeaux.get('usage') or drapeaux.get('ignore') and ouvert in ('aside', 'nav', 'footer')) and self.texte_exclu.strip():
                if not self._ignore():
                    self.exclus.append({'balise': ouvert, 'texte': self.texte_exclu.strip()[:300]})
                    self.texte_exclu = ''
            if ouvert == tag:
                return

    def handle_endtag(self, tag):
        if tag in VIDES_HTML:
            return
        if not any(ouvert == tag for ouvert, _ in self.pile):
            return
        if tag == 'a' and self.liens_ouverts:
            lien, debut = self.liens_ouverts.pop()
            fin = len(self.tampon.rstrip()) if self.tampon.endswith(' ') else len(self.tampon)
            while debut < fin and self.tampon[debut] == ' ':
                debut += 1
            if fin > debut:
                self.liens_tampon.append((debut, fin, lien))
        if tag in ('q', 'cite') and self.citations_ouvertes:
            debut = self.citations_ouvertes.pop()
            fin = len(self.tampon.rstrip())
            if fin > debut:
                self.citations_tampon.append((debut, fin))
        if tag in BLOCS_HTML or tag in CONTENEURS_HTML:
            self._vider()
        self._fermer(tag)

    def handle_data(self, donnees):
        if self.pile and self.pile[-1][0] == 'title':
            self.titre_html += donnees
            return
        if self.pile and self.pile[-1][0] == 'script':
            if self._script is not None:
                self._script.append(donnees)
            return
        if self._ignore():
            if self._drapeau('usage') or any(tag in ('aside', 'nav', 'footer') for tag, _ in self.pile):
                self.texte_exclu += ' ' + re.sub(r'\s+', ' ', donnees)
            return
        self._ajouter(donnees)

    def close(self):
        super().close()
        self._fermer('__fin__')
        self._vider()


def extraire_html(html, url='', principal=True, classes_usage=True):
    """Repli maison de l'extraction (html.parser) : texte, blocs, liens, citations, exclusions.

    Conserve l'information utile de la sortie XML de Trafilatura : titres Hn,
    paragraphes, listes, citations, liens (offsets dans le texte produit). Si
    la page a un ``<main>`` ou un ``<article>``, seuls ses blocs sont gardes.
    Les blocs d'usage (temps de lecture, partage, « a lire aussi »,
    commentaires) sont exclus et listes dans ``exclus``.
    """
    analyseur = _ExtracteurHTML(url=url, principal=principal, classes_usage=classes_usage)
    analyseur.feed(html or '')
    analyseur.close()
    blocs = analyseur.blocs
    if principal and any(b['principal'] for b in blocs):
        blocs = [b for b in blocs if b['principal']]
    exclus = list(analyseur.exclus)
    retenus = []
    for bloc in blocs:
        if _est_usage(dates.normaliser(bloc['texte'])):
            exclus.append({'balise': 'texte', 'texte': bloc['texte'][:300]})
            continue
        retenus.append(bloc)
    morceaux = []
    position = 0
    sortie_blocs = []
    liens = []
    citations = []
    precedent = None
    for bloc in retenus:
        if morceaux:
            separateur = '\n' if precedent == 'liste' and bloc['kind'] == 'liste' else '\n\n'
            morceaux.append(separateur)
            position += len(separateur)
        debut = position
        morceaux.append(bloc['texte'])
        position += len(bloc['texte'])
        sortie_blocs.append({'start': debut, 'end': position, 'kind': bloc['kind'], 'niveau': bloc['niveau']})
        for d, f, cible in bloc['liens']:
            liens.append({'start': debut + d, 'end': debut + f, 'href': cible, 'texte': bloc['texte'][d:f]})
        for d, f in bloc['citations']:
            citations.append({'start': debut + d, 'end': debut + f})
        precedent = bloc['kind']
    texte = ''.join(morceaux)
    chapo = next((b['texte'] for b in retenus if b['chapo'] and b['kind'] != 'titre'), None)
    titre_h1 = next((b['texte'] for b in analyseur.blocs if b['kind'] == 'titre' and b['niveau'] == 1), None)
    return {
        'texte': texte,
        'blocs': sortie_blocs,
        'liens': liens,
        'citations': citations,
        'exclus': exclus,
        'titre': (titre_h1 or analyseur.titre_html or '').strip(),
        'titre_html': analyseur.titre_html.strip(),
        'metas': analyseur.metas,
        'jsonld': analyseur.jsonld,
        'temps': analyseur.temps,
        'chapo': chapo,
        'langue': analyseur.langue,
    }


# Correspondance des balises du XML de Trafilatura vers le HTML lu par l'extracteur.
_XML_VERS_HTML = {'p': 'p', 'list': 'ul', 'item': 'li', 'quote': 'blockquote', 'ref': 'a', 'hi': 'span',
                  'table': 'table', 'row': 'tr', 'cell': 'td', 'code': 'pre', 'main': 'main', 'doc': 'div', 'del': 'span',
                  'lb': 'br'}


def _xml_vers_html(xml):
    """Convertit la sortie XML de Trafilatura (head, p, list/item, quote, ref target) en HTML simple."""
    import xml.etree.ElementTree as ET
    racine = ET.fromstring(xml)

    def convertir(element):
        balise = element.tag
        if balise in ('comments', 'graphic'):
            return html_lib.escape(element.tail or '')
        if balise == 'head':
            rend = (element.get('rend') or 'h2').lower()
            nom = rend if re.fullmatch(r'h[1-6]', rend) else 'h2'
        else:
            nom = _XML_VERS_HTML.get(balise, 'span')
        attributs = ''
        if balise == 'ref' and element.get('target'):
            attributs = ' href="' + html_lib.escape(element.get('target'), quote=True) + '"'
        interieur = html_lib.escape(element.text or '') + ''.join(convertir(enfant) for enfant in element)
        if nom == 'br':
            return '<br>' + html_lib.escape(element.tail or '')
        return '<' + nom + attributs + '>' + interieur + '</' + nom + '>' + html_lib.escape(element.tail or '')

    principal = racine.find('main')
    return convertir(principal if principal is not None else racine)


def _trafilatura(html, url):
    """Sortie XML de Trafilatura (liens, titres, citations) et ses metadonnees ; (None, {}) si absent."""
    try:
        import trafilatura
    except ImportError:
        return None, {}
    try:
        xml = trafilatura.extract(html, url=url or None, output_format='xml', include_links=True, include_comments=False,
                                  include_tables=True, include_formatting=False, with_metadata=True)
    except Exception:
        return None, {}
    if not xml:
        return None, {}
    infos = {}
    try:
        import xml.etree.ElementTree as ET
        racine = ET.fromstring(xml)
        for cle in ('title', 'author', 'date', 'description'):
            if racine.get(cle):
                infos[cle] = racine.get(cle)
    except Exception:
        return None, {}
    return xml, infos


def _htmldate(html):
    """Dates de publication et de mise a jour par htmldate (optionnel)."""
    try:
        from htmldate import find_date
    except ImportError:
        return None, None
    try:
        publication = find_date(html, original_date=True, extensive_search=False)
        mise_a_jour = find_date(html, original_date=False, extensive_search=False)
    except Exception:
        return None, None
    return publication, mise_a_jour


def _jsonld_objets(html, url, scripts):
    """Objets JSON-LD de la page : extruct si installe, sinon lecture directe des scripts."""
    objets = []
    try:
        import extruct
        donnees = extruct.extract(html, base_url=url or None, syntaxes=['json-ld'], uniform=True)
        objets = list(donnees.get('json-ld') or [])
    except ImportError:
        objets = []
    except Exception:
        objets = []
    if not objets:
        for script in scripts:
            contenu = script.strip()
            if not contenu:
                continue
            try:
                valeur = json.loads(contenu, strict=False)
            except ValueError:
                try:
                    valeur = json.loads(re.sub(r',\s*([}\]])', r'\1', contenu), strict=False)
                except ValueError:
                    continue
            objets.extend(valeur if isinstance(valeur, list) else [valeur])
    aplatis = []
    for objet in objets:
        if isinstance(objet, dict) and isinstance(objet.get('@graph'), list):
            aplatis.extend(o for o in objet['@graph'] if isinstance(o, dict))
        elif isinstance(objet, dict):
            aplatis.append(objet)
    return aplatis


TYPES_CONTENU = ('NewsArticle', 'Article', 'BlogPosting', 'ReportageNewsArticle', 'AnalysisNewsArticle', 'OpinionNewsArticle',
                 'TechArticle', 'ScholarlyArticle', 'Report', 'HowTo', 'FAQPage', 'Recipe', 'Review', 'Product', 'Event',
                 'WebPage', 'AboutPage', 'CollectionPage', 'QAPage', 'ItemPage')


def _type_jsonld(objet):
    """Type schema.org d'un objet (premier type si liste)."""
    valeur = objet.get('@type')
    if isinstance(valeur, list):
        valeur = next((v for v in valeur if isinstance(v, str)), None)
    if isinstance(valeur, str):
        return valeur.split('/')[-1]
    return None


def _noms_auteurs(valeur):
    """Nom(s) d'auteur d'une valeur schema.org (texte, objet ou liste)."""
    if not valeur:
        return None
    if isinstance(valeur, str):
        return valeur.strip() or None
    if isinstance(valeur, dict):
        nom = valeur.get('name')
        if isinstance(nom, list):
            nom = ', '.join(str(n) for n in nom)
        return str(nom).strip() if nom else None
    if isinstance(valeur, list):
        noms = [n for n in (_noms_auteurs(v) for v in valeur) if n]
        return ', '.join(noms) if noms else None
    return None


def _date_iso(valeur):
    """Date ISO ``AAAA-MM-JJ`` d'une valeur de metadonnee (ISO, ISO horodate, texte francais)."""
    if not valeur:
        return None
    if isinstance(valeur, list):
        valeur = valeur[0] if valeur else None
        if not valeur:
            return None
    if isinstance(valeur, dict):
        valeur = valeur.get('@value')
        if not valeur:
            return None
    texte = str(valeur).strip()
    jour = dates.en_date(texte)
    if jour:
        return jour.isoformat()
    trouvees = dates.absolues(texte)
    if trouvees:
        return trouvees[0]['debut']
    return None


_RE_VISIBLE_PUBLICATION = re.compile(r"\b(?:publie|publiee|mis en ligne|cree|creee|paru|parue)\s+le\s+")
_RE_VISIBLE_MAJ = re.compile(r"\b(?:mis a jour|mise a jour|derniere mise a jour|actualise|actualisee|modifie|modifiee|maj|etat|situation)\s*(?:le|du|au|:)?\s*")
_RE_VISIBLE_AUTEUR = re.compile(r"(?:^|\n)\s*(?:[Pp]ar|[Rr][ée]dig[ée]e? par|[ÉEé]crit(?:e)? par|[Aa]uteur\s*:|[Aa]utrice\s*:)\s+([A-ZÀ-Ý][\w'’.-]+(?:\s+[A-ZÀ-Ý][\w'’.-]+){0,3})")
_RE_SIGNATURE = re.compile(r"^\s*[—–-]\s*([A-ZÀ-Ý][\w.'’-]*(?:\s+[A-ZÀ-Ý][\w.'’-]*){0,3})\s*$")


def _visibles(texte, blocs=None):
    """Date de publication, de mise a jour et auteur visibles dans le texte (en-tete, blocs metadonnee, signature)."""
    texte = texte or ''
    zones = [(0, min(len(texte), 1500))]
    for bloc in blocs or []:
        if bloc.get('kind') == 'metadonnee':
            zones.append((bloc['start'], bloc['end']))
    resultat = {}
    for debut, fin in zones:
        extrait = texte[debut:fin]
        norm = dates.normaliser(extrait)
        for cle, motif in (('publication', _RE_VISIBLE_PUBLICATION), ('mise_a_jour', _RE_VISIBLE_MAJ)):
            if cle in resultat:
                continue
            for correspondance in motif.finditer(norm):
                suite = extrait[correspondance.end():correspondance.end() + 40]
                trouvees = dates.absolues(suite)
                if trouvees and trouvees[0]['start'] <= 3 and trouvees[0]['precision'] == 'jour':
                    resultat[cle] = trouvees[0]['debut']
                    break
        if 'auteur' not in resultat:
            correspondance = _RE_VISIBLE_AUTEUR.search(extrait)
            if correspondance:
                resultat['auteur'] = correspondance.group(1).strip()
    if 'auteur' not in resultat and blocs:
        dernier = blocs[-1]
        correspondance = _RE_SIGNATURE.match(texte[dernier['start']:dernier['end']])
        if correspondance:
            resultat['auteur'] = correspondance.group(1).strip()
    return resultat


def metadonnees(html, url='', texte=None, _analyse=None):
    """Metadonnees de la page dans l'ordre de confiance du document.

    1. JSON-LD schema.org (``datePublished``, ``dateModified``, ``author``,
       ``@type``, ``description``) lu avec extruct, sinon lecture directe des
       ``<script type="application/ld+json">`` ;
    2. metadonnees Trafilatura et htmldate (si installes), balises ``meta`` et ``time`` ;
    3. date visible dans le texte (« Publie le », « Mis a jour le », signature).

    Renvoie ``{titre, auteur, publication, mise_a_jour, type_schema, chapo,
    langue, sources: {champ: origine}, jsonld: [types]}`` ; dates en ISO.
    """
    analyse = _analyse or extraire_html(html, url)
    resultat = {'titre': None, 'auteur': None, 'publication': None, 'mise_a_jour': None, 'type_schema': None,
                'chapo': None, 'langue': analyse.get('langue') or None, 'sources': {}, 'jsonld': []}

    def poser(cle, valeur, origine):
        if valeur and not resultat.get(cle):
            resultat[cle] = valeur
            resultat['sources'][cle] = origine

    # Chapo present dans la page (ancre dans le texte) avant toute description declarative.
    poser('chapo', analyse.get('chapo'), 'html')
    # 1. JSON-LD
    objets = _jsonld_objets(html, url, analyse.get('jsonld') or [])
    resultat['jsonld'] = [t for t in (_type_jsonld(o) for o in objets) if t]
    objets_tries = sorted(objets, key=lambda o: TYPES_CONTENU.index(_type_jsonld(o)) if _type_jsonld(o) in TYPES_CONTENU else len(TYPES_CONTENU))
    for objet in objets_tries:
        poser('publication', _date_iso(objet.get('datePublished') or objet.get('dateCreated')), 'jsonld')
        poser('mise_a_jour', _date_iso(objet.get('dateModified')), 'jsonld')
        poser('auteur', _noms_auteurs(objet.get('author') or objet.get('creator')), 'jsonld')
        if _type_jsonld(objet) in TYPES_CONTENU:
            poser('type_schema', _type_jsonld(objet), 'jsonld')
            poser('titre', objet.get('headline') or objet.get('name'), 'jsonld')
            poser('chapo', objet.get('description') if isinstance(objet.get('description'), str) else None, 'jsonld')
    # 2. Trafilatura, htmldate, balises meta et time
    _, infos = _trafilatura(html, url) if html else (None, {})
    poser('auteur', infos.get('author'), 'trafilatura')
    poser('publication', _date_iso(infos.get('date')), 'trafilatura')
    poser('titre', infos.get('title'), 'trafilatura')
    publication_hd, maj_hd = _htmldate(html) if html else (None, None)
    poser('publication', _date_iso(publication_hd), 'htmldate')
    if maj_hd and maj_hd != publication_hd:
        poser('mise_a_jour', _date_iso(maj_hd), 'htmldate')
    metas = analyse.get('metas') or {}
    for cle in ('article:published_time', 'og:published_time', 'datepublished', 'date', 'dc.date', 'dc.date.issued',
                'dcterms.date', 'publication_date', 'pubdate', 'sailthru.date', 'parsely-pub-date'):
        poser('publication', _date_iso(metas.get(cle)), 'meta:' + cle)
    for cle in ('article:modified_time', 'og:updated_time', 'datemodified', 'dc.date.modified', 'dcterms.modified',
                'last-modified', 'revised'):
        poser('mise_a_jour', _date_iso(metas.get(cle)), 'meta:' + cle)
    for cle in ('author', 'article:author', 'dc.creator', 'byl', 'parsely-author', 'sailthru.author'):
        valeur = metas.get(cle)
        if valeur and not valeur.startswith('http'):
            poser('auteur', re.sub(r'^par\s+', '', valeur, flags=re.I), 'meta:' + cle)
    for temps in analyse.get('temps') or []:
        valeur = _date_iso(temps.get('datetime'))
        if 'modified' in temps.get('itemprop', '') or re.search(r'updat|modif|maj', temps.get('classe', '')):
            poser('mise_a_jour', valeur, 'time')
        else:
            poser('publication', valeur, 'time')
    poser('titre', metas.get('og:title') or analyse.get('titre') or analyse.get('titre_html'), 'html')
    poser('chapo', analyse.get('chapo'), 'html')
    poser('chapo', metas.get('description') or metas.get('og:description'), 'meta:description')
    if metas.get('og:type'):
        poser('type_schema', 'og:' + metas['og:type'], 'meta:og:type')
    # 3. Date visible dans le texte
    visibles = _visibles(texte if texte is not None else analyse.get('texte', ''), analyse.get('blocs'))
    poser('publication', visibles.get('publication'), 'texte')
    poser('mise_a_jour', visibles.get('mise_a_jour'), 'texte')
    poser('auteur', visibles.get('auteur'), 'texte')
    return resultat


def structurer_texte(texte):
    """Structure d'un texte brut deja importe : blocs titre, paragraphe, liste, citation, note, metadonnee, usage.

    Les titres sont reconnus par le balisage Markdown (#), les lignes en
    majuscules, ou une ligne courte sans ponctuation finale isolee entre deux
    lignes vides ; le premier bloc court est le titre de la page (niveau 1).
    Les blocs sous une section « Sources », « Notes », « References »... sont
    des notes. Renvoie ``{'blocs': [{start, end, kind, niveau}], 'titre'}``.
    """
    texte = texte or ''
    blocs = []
    titre = None
    section_notes = False
    for rang, (debut, fin) in enumerate(moteurs.blocs(texte)):
        contenu = texte[debut:fin]
        norm = dates.normaliser(contenu).strip()
        kind = 'paragraphe'
        niveau = None
        lettres = [c for c in contenu if c.isalpha()]
        markdown = _RE_MARKDOWN_TITRE.match(contenu)
        isole = _ligne_isolee(texte, debut, fin)
        court = len(contenu) <= 150 and len(contenu.split()) <= 16 and '\n' not in contenu.strip()
        if markdown:
            kind = 'titre'
            niveau = len(markdown.group(1)) + (1 if markdown.group(1).startswith('=') else 0)
        elif _RE_URL_SEULE.match(contenu):
            kind = 'metadonnee' if rang < 4 else 'note'
        elif rang == 0 and court and not _FIN_PHRASE.search(contenu):
            kind = 'titre'
            niveau = 1
        elif len(contenu) < 60 and _RE_SIGNATURE.match(contenu):
            kind = 'metadonnee'
        elif _RE_LISTE.match(contenu):
            kind = 'liste'
        elif court and len(lettres) >= 4 and sum(1 for c in lettres if c.isupper()) >= 0.8 * len(lettres):
            kind = 'titre'
            niveau = 2
        elif court and isole and not _FIN_PHRASE.search(contenu) and len(lettres) >= 4:
            kind = 'titre'
            niveau = 3
        elif _est_usage(norm):
            kind = 'usage'
        elif len(contenu) < 200 and _RE_META_BLOC.search(norm):
            kind = 'metadonnee'
        elif norm.startswith(('«', '"', '“', '>')) and norm.endswith(('»', '"', '”')):
            kind = 'citation'
        if kind == 'titre':
            section_notes = bool(_RE_SECTION_NOTES.match(re.sub(r'^[#=]+\s*', '', norm)))
            if niveau == 1 and titre is None:
                titre = re.sub(r'^[#=]+\s*|\s*[#=]+\s*$', '', contenu).strip()
        elif section_notes and kind in ('paragraphe', 'liste'):
            kind = 'note'
        blocs.append({'start': debut, 'end': fin, 'kind': kind, 'niveau': niveau})
    return {'blocs': blocs, 'titre': titre}


def _ligne_isolee(texte, debut, fin):
    """Vrai si le bloc est precede et suivi d'une ligne vide (ou d'un bord du texte)."""
    avant = texte[max(0, debut - 6):debut]
    apres = texte[fin:fin + 6]
    vide_avant = debut == 0 or bool(re.search(r'\n[ \t\r]*\n[ \t\r]*$', avant)) or not avant.strip(' \t\r\n')
    vide_apres = fin >= len(texte) or bool(re.match(r'^[ \t\r]*\n[ \t\r]*\n', apres)) or not texte[fin:].strip()
    return vide_avant and vide_apres


def simhash(texte):
    """Empreinte SimHash 64 bits en hexadecimal (``versions.simhash``).

    Utilise ``chaine.simhash`` (lot M5, meme methode que ContentKB) pour que
    les empreintes des pages et des pages externes soient comparables ; a
    defaut, calcul identique en local (blake2b, fenetres de 3 mots normalises).
    """
    try:
        module = importlib.import_module('chaine.simhash')
        return module.hexa(module.empreinte(texte or ''))
    except (ImportError, AttributeError):
        pass
    import unicodedata
    decompose = unicodedata.normalize('NFD', (texte or '').casefold())
    mots = re.findall(r'[a-z0-9]+', ''.join(c for c in decompose if not unicodedata.combining(c)))
    if not mots:
        return None
    elements = mots if len(mots) < 3 else [' '.join(mots[i:i + 3]) for i in range(len(mots) - 2)]
    comptes = {}
    for element in elements:
        comptes[element] = comptes.get(element, 0) + 1
    vecteur = [0] * 64
    for element, poids in comptes.items():
        valeur = int.from_bytes(hashlib.blake2b(element.encode('utf-8'), digest_size=8).digest(), 'big')
        for bit in range(64):
            if valeur >> bit & 1:
                vecteur[bit] += poids
            else:
                vecteur[bit] -= poids
    empreinte = 0
    for bit in range(64):
        if vecteur[bit] > 0:
            empreinte |= 1 << bit
    return format(empreinte, '016x')


def distance_simhash(a, b):
    """Distance de Hamming entre deux empreintes hexadecimales (None si l'une manque)."""
    if not a or not b:
        return None
    return bin(int(a, 16) ^ int(b, 16)).count('1')


def _telecharger(url):
    """Telecharge une page avec les garde-fous du serveur (``S.public_url``, ``S.PublicRedirect``).

    Memes limites que ``server.fetch_page`` mais renvoie le HTML brut :
    ``(contenu, type_mime)``.
    """
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, build_opener
    serveur = noyau.srv()
    serveur.public_url(url)
    limite = int(_seuil('m1.acquisition.octets_max'))
    redirection = getattr(serveur, 'PublicRedirect', None)
    ouvreur = build_opener(redirection()) if redirection else build_opener()
    try:
        requete = Request(url, headers={'User-Agent': 'FactCheckAtelier/1.0'})
        with ouvreur.open(requete, timeout=int(_seuil('m1.acquisition.delai_secondes'))) as reponse:
            type_mime = reponse.headers.get_content_type()
            if type_mime not in ('text/html', 'text/plain', 'application/xhtml+xml'):
                raise ValueError('Import limité aux pages HTML et au texte. Collez le texte pour les PDF.')
            brut = reponse.read(limite + 1)
            if len(brut) > limite:
                raise ValueError('Page trop volumineuse : collez le passage à analyser.')
            contenu = brut.decode(reponse.headers.get_content_charset() or 'utf-8', errors='replace')
    except (HTTPError, URLError, TimeoutError):
        raise ValueError('Source inaccessible : vous pouvez coller son texte dans le formulaire.')
    return contenu, type_mime


def acquerir(url=None, html=None, texte=None, titre='', publiee=''):
    """Etape 1 : acquiert une page et ses metadonnees ; renvoie l'identifiant de version.

    - ``html`` (ou ``url`` seule, telechargee avec les garde-fous du serveur) :
      Trafilatura en sortie XML si installe (XML garde dans ``versions.xml``),
      sinon parseur HTML maison ; metadonnees par ``metadonnees`` ;
    - ``texte`` : texte brut, structure par ``structurer_texte``, date et
      auteur visibles dans le texte.

    Le texte extrait est enregistre par ``S.add_content`` (dedoublonnage et
    versions du serveur), puis ``versions`` recoit auteur, mise a jour,
    SimHash, type schema.org, XML, metadonnees (JSON : blocs, liens,
    citations, exclusions, sources des metadonnees) et chapo. La date de
    publication va dans ``versions.published`` ; ``publiee`` (date saisie par
    l'utilisateur, ``/api/import``) l'emporte sur celle des metadonnees.
    Aucune segmentation ici.
    """
    serveur = noyau.srv()
    _assurer_colonnes()
    xml = None
    extracteur = 'texte'
    if html is None and texte is None:
        if not url:
            raise ValueError('URL, HTML ou texte requis.')
        contenu, type_mime = _telecharger(url)
        if 'html' in type_mime:
            html = contenu
        else:
            texte = contenu
    if html is not None:
        brute = extraire_html(html, url or '')
        extraction = None
        xml, _ = _trafilatura(html, url or '')
        if xml:
            try:
                extraction = extraire_html(_xml_vers_html(xml), url or '', principal=False, classes_usage=False)
                extracteur = 'trafilatura'
            except Exception:
                extraction = None
                xml = None
            if extraction is not None and not extraction['texte'].strip():
                extraction = None
                xml = None
        if extraction is None:
            extraction = brute
            extracteur = 'html.parser'
        texte_final = extraction['texte']
        meta = metadonnees(html, url or '', texte=texte_final, _analyse=brute)
        blocs = extraction['blocs']
        _completer_blocs_meta(texte_final, blocs)
        details = {'liens': extraction['liens'], 'citations': extraction['citations'], 'exclus': brute['exclus'] + [
            e for e in extraction['exclus'] if e not in brute['exclus']]}
    else:
        texte_final = texte or ''
        structure = structurer_texte(texte_final)
        blocs = structure['blocs']
        meta = _metadonnees_texte(texte_final, structure)
        details = {'liens': _liens_texte(texte_final), 'citations': [], 'exclus': []}
    if not texte_final.strip():
        raise ValueError('Le contenu est vide.')
    titre_final = titre or meta.get('titre') or ''
    if publiee:
        meta['publication'] = publiee
        meta.setdefault('sources', {})['publication'] = 'saisie'
    version_id = serveur.add_content(url or '', titre_final, texte_final, meta.get('publication') or '')
    _enregistrer_meta(version_id, texte_final, blocs, meta, details, extracteur, xml)
    return version_id


def _completer_blocs_meta(texte, blocs):
    """Marque ``metadonnee`` les blocs courts de signature (« Par X », « Publie le ») d'un texte extrait du HTML."""
    for bloc in blocs:
        if bloc['kind'] in ('paragraphe', 'note') and bloc['end'] - bloc['start'] < 200:
            if _RE_META_BLOC.search(dates.normaliser(texte[bloc['start']:bloc['end']]).strip()):
                bloc['kind'] = 'metadonnee'


def _metadonnees_texte(texte, structure):
    """Metadonnees d'un texte brut : titre, date et auteur visibles."""
    visibles = _visibles(texte, structure['blocs'])
    sources = {cle: 'texte' for cle in visibles}
    if structure.get('titre'):
        sources['titre'] = 'texte'
    return {'titre': structure.get('titre'), 'auteur': visibles.get('auteur'), 'publication': visibles.get('publication'),
            'mise_a_jour': visibles.get('mise_a_jour'), 'type_schema': None, 'chapo': None, 'langue': None,
            'sources': sources, 'jsonld': []}


def _liens_texte(texte):
    """URL ecrites en clair dans un texte brut, avec offsets."""
    liens = []
    for correspondance in re.finditer(r'https?://[^\s<>"»”]+', texte or ''):
        cible = correspondance.group(0).rstrip('.,;:)]')
        liens.append({'start': correspondance.start(), 'end': correspondance.start() + len(cible), 'href': cible, 'texte': cible})
    return liens


def _enregistrer_meta(version_id, texte, blocs, meta, details, extracteur, xml):
    """Ecrit les metadonnees de l'etape 1 dans ``versions``."""
    serveur = noyau.srv()
    contenu = {
        'regles': VERSION_REGLES,
        'extracteur': extracteur,
        'longueur': len(texte),
        'blocs': blocs,
        'liens': details.get('liens') or [],
        'citations': details.get('citations') or [],
        'exclus': details.get('exclus') or [],
        'metadonnees': meta,
    }
    type_page = meta.get('type_schema')
    serveur.query(
        'UPDATE versions SET author=?, updated=?, simhash=?, page_type=COALESCE(?, page_type), '
        'page_type_engine=CASE WHEN ? IS NULL THEN page_type_engine ELSE ? END, xml=?, meta=?, lede=? WHERE id=?',
        (meta.get('auteur'), meta.get('mise_a_jour'), simhash(texte), type_page, type_page,
         (meta.get('sources') or {}).get('type_schema'), xml, _json(contenu), meta.get('chapo'), version_id),
    )


# ---------------------------------------------------------------------------
# Etape 2 — Segmentation par la structure
# ---------------------------------------------------------------------------

def _version(version_id):
    """Version, titre de page et metadonnees."""
    ligne = noyau.srv().query(
        'SELECT v.id, v.text, v.published, v.updated, v.author, v.meta, v.simhash, v.lede, v.segment_run, p.title, p.url '
        'FROM versions v LEFT JOIN pages p ON p.id=v.page_id WHERE v.id=?',
        (version_id,), True,
    )
    if ligne is None:
        raise ValueError('Version inconnue : ' + str(version_id))
    ligne['meta_json'] = _charger_json(ligne.get('meta'), {}) or {}
    return ligne


def _date_page(version):
    """Date de la page et son type : mise a jour, sinon publication (``versions`` puis texte visible)."""
    meta = (version.get('meta_json') or {}).get('metadonnees') or {}
    mise_a_jour = _date_iso(version.get('updated')) or _date_iso(meta.get('mise_a_jour'))
    publication = _date_iso(version.get('published')) or _date_iso(meta.get('publication'))
    if mise_a_jour:
        return mise_a_jour, 'maj'
    if publication:
        return publication, 'publication'
    return None, None


def _date_reference(version):
    """Reference des expressions relatives : date de publication, sinon de mise a jour."""
    meta = (version.get('meta_json') or {}).get('metadonnees') or {}
    return (_date_iso(version.get('published')) or _date_iso(meta.get('publication'))
            or _date_iso(version.get('updated')) or _date_iso(meta.get('mise_a_jour')))


def _structure_version(version):
    """Blocs d'une version : ceux de l'acquisition s'ils correspondent au texte, sinon structure deduite du texte.

    Pour un contenu deja importe sans metadonnees, complete ``versions``
    (metadonnees visibles, SimHash) sans re-telechargement et sans toucher
    ``versions.published``.
    """
    texte = version.get('text') or ''
    meta = version.get('meta_json') or {}
    blocs = meta.get('blocs')
    if blocs and meta.get('longueur') == len(texte):
        return blocs
    structure = structurer_texte(texte)
    infos = _metadonnees_texte(texte, structure)
    contenu = dict(meta)
    contenu.update({'regles': VERSION_REGLES, 'extracteur': meta.get('extracteur') or 'texte', 'longueur': len(texte),
                    'blocs': structure['blocs'], 'liens': meta.get('liens') or _liens_texte(texte),
                    'citations': meta.get('citations') or [], 'exclus': meta.get('exclus') or [],
                    'metadonnees': meta.get('metadonnees') or infos})
    noyau.srv().query(
        'UPDATE versions SET meta=?, simhash=COALESCE(simhash, ?), author=COALESCE(author, ?), updated=COALESCE(updated, ?) WHERE id=?',
        (_json(contenu), simhash(texte), infos.get('auteur'), infos.get('mise_a_jour'), version['id']),
    )
    version['meta_json'] = contenu
    if not version.get('updated') and infos.get('mise_a_jour'):
        version['updated'] = infos['mise_a_jour']
    return structure['blocs']


def _id_chunk(version_id, debut, fin, kind):
    """Identifiant deterministe d'un nouveau chunk (stable d'une segmentation a l'autre)."""
    return 'k' + hashlib.sha1(f'{version_id}:{debut}:{fin}:{kind}'.encode()).hexdigest()[:15]


def _decouper(texte, blocs, phrases):
    """Chunks (dict) a partir des blocs et des phrases ; un paragraphe trop long est recoupe par phrases."""
    maximum = int(_seuil('m1.segment.chunk_caracteres_max'))
    debuts_phrases = [d for d, _ in phrases]
    titres = {}
    resultat = []
    for position, bloc in enumerate(blocs):
        debut_bloc = bloc['start']
        fin_bloc = bloc['end']
        kind = bloc['kind'] if bloc['kind'] in NOUVEAUX_KINDS else 'paragraphe'
        libelle = re.sub(r'^\s*[#=]+\s*|\s*[#=]+\s*$', '', texte[debut_bloc:fin_bloc]).strip()
        if kind == 'titre':
            niveau = int(bloc.get('niveau') or 2)
            titres = {n: t for n, t in titres.items() if n < niveau}
            titres[niveau] = libelle
        section = ' > '.join(titres[n] for n in sorted(titres) if n > 1)
        # phrases du bloc (rognees aux bornes du bloc)
        i = bisect.bisect_right(debuts_phrases, debut_bloc) - 1
        i = max(i, 0)
        dans_bloc = []
        while i < len(phrases) and phrases[i][0] < fin_bloc:
            debut_p = max(phrases[i][0], debut_bloc)
            fin_p = min(phrases[i][1], fin_bloc)
            if fin_p > debut_p and texte[debut_p:fin_p].strip():
                dans_bloc.append((debut_p, fin_p))
            i += 1
        if not dans_bloc or kind == 'titre':
            dans_bloc = [(debut_bloc, fin_bloc)]
        groupes = [[]]
        for phrase in dans_bloc:
            courant = groupes[-1]
            if courant and phrase[1] - courant[0][0] > maximum:
                groupes.append([])
            groupes[-1].append(phrase)
        for groupe in groupes:
            if not groupe:
                continue
            resultat.append({
                'start': groupe[0][0], 'end': groupe[-1][1], 'kind': kind, 'section': section,
                'phrases': [list(p) for p in groupe], 'position': position,
            })
    return resultat


def segmenter(version_id, forcer=False):
    """Etape 2 : segmente une version par section (Hn) et paragraphe, puis par phrase.

    Jamais par nombre de mots. Chaque chunk (``kind`` non nul) porte
    ``section``, ``start``, ``end``, ``kind`` et son contexte JSON (titre de
    la page, titre de section, date de la page, phrases). Les anciens chunks
    (``kind IS NULL``) ne sont jamais supprimes. Les unites de la version
    sont rattachees au nouveau chunk qui contient le debut de leur span.
    Reprenable : une version deja segmentee n'est pas refaite sans ``forcer``.
    Renvoie ``{'chunks', 'unites_rattachees', 'deja'}``.
    """
    _assurer_colonnes()
    with _execution('m1.segmentation', moteurs={'phrases': moteurs.dernier('phrases') or 'auto'}, prompt=VERSION_REGLES,
                    seuils=['m1.segment.chunk_caracteres_max', 'm1.segment.phrase_caracteres_max'], pages=1) as run_id:
        return _segmenter(version_id, forcer, run_id)


def _segmenter(version_id, forcer, run_id):
    """Corps de ``segmenter``."""
    serveur = noyau.srv()
    version = _version(version_id)
    if not forcer and version.get('segment_run'):
        existe = serveur.query('SELECT 1 FROM chunks WHERE version_id=? AND kind IS NOT NULL LIMIT 1', (version_id,), True)
        if existe:
            # Unites arrivees apres la segmentation (migration, ancien parcours) encore rattachees a un ancien chunk.
            rattachees = 0
            if serveur.query('SELECT 1 FROM units u LEFT JOIN chunks c ON c.id=u.chunk_id WHERE u.version_id=? '
                             'AND u.span_start IS NOT NULL AND c.kind IS NULL LIMIT 1', (version_id,), True):
                rattachees = _rattacher_unites(version_id)
            return {'chunks': 0, 'unites_rattachees': rattachees, 'deja': True}
    texte = version.get('text') or ''
    blocs = _structure_version(version)
    phrases, moteur = moteurs.phrases(texte, avec_moteur=True)
    phrases = sorted(phrases)
    morceaux = _decouper(texte, blocs, phrases)
    date_page, type_date = _date_page(version)
    titre = version.get('title') or ((version.get('meta_json') or {}).get('metadonnees') or {}).get('titre') or ''
    lignes = []
    for rang, morceau in enumerate(morceaux):
        identifiant = _id_chunk(version_id, morceau['start'], morceau['end'], morceau['kind'])
        contexte = {'titre': titre, 'section': morceau['section'], 'date': date_page, 'date_type': type_date,
                    'position': morceau['position'], 'phrases': morceau['phrases'], 'segmenteur': moteur}
        lignes.append((identifiant, version_id, rang, morceau['start'], texte[morceau['start']:morceau['end']], 1,
                       morceau['section'], morceau['end'], _json(contexte), morceau['kind'], run_id))
    identifiants = [ligne[0] for ligne in lignes]
    with noyau.connexion() as base:
        anciens = [r[0] for r in base.execute('SELECT id FROM chunks WHERE version_id=? AND kind IS NOT NULL', (version_id,))]
        a_supprimer = sorted(set(anciens) - set(identifiants))
        for lot in _par_lots(a_supprimer, 500):
            base.execute('DELETE FROM chunks WHERE id IN (' + _marques(lot) + ')', tuple(lot))
        base.executemany(
            'INSERT OR REPLACE INTO chunks(id,version_id,position,start,text,prepared,section,"end",context,kind,run_id) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?)', lignes,
        )
        base.execute('UPDATE versions SET segment_run=? WHERE id=?', (run_id, version_id))
    rattachees = _rattacher_unites(version_id)
    return {'chunks': len(lignes), 'unites_rattachees': rattachees, 'deja': False, 'moteur_phrases': moteur}


def _rattacher_unites(version_id):
    """Rattache chaque unite de la version au nouveau chunk qui contient le debut de son span."""
    serveur = noyau.srv()
    chunks = serveur.query(
        'SELECT id, start, "end" FROM chunks WHERE version_id=? AND kind IS NOT NULL ORDER BY start', (version_id,))
    if not chunks:
        return 0
    debuts = [c['start'] for c in chunks]
    unites = serveur.query(
        'SELECT id, chunk_id, span_start, span_end FROM units WHERE version_id=? AND span_start IS NOT NULL', (version_id,))
    mises_a_jour = []
    for unite in unites:
        rang = bisect.bisect_right(debuts, unite['span_start']) - 1
        cible = None
        if rang >= 0 and unite['span_start'] < chunks[rang]['end']:
            cible = chunks[rang]
        elif rang + 1 < len(chunks) and chunks[rang + 1]['start'] < (unite['span_end'] or unite['span_start'] + 1):
            cible = chunks[rang + 1]
        elif rang >= 0:
            cible = chunks[rang]
        else:
            cible = chunks[0]
        if cible['id'] != unite['chunk_id']:
            mises_a_jour.append((cible['id'], unite['id']))
    if mises_a_jour:
        with noyau.connexion() as base:
            base.executemany('UPDATE units SET chunk_id=? WHERE id=?', mises_a_jour)
    return len(mises_a_jour)


def contexte_texte(chunk):
    """Contexte lisible d'un chunk (ligne ``chunks`` ou JSON) : « Titre — Section (date) »."""
    contexte = chunk.get('context') if isinstance(chunk, dict) and 'context' in chunk else chunk
    if isinstance(contexte, str):
        contexte = _charger_json(contexte, {}) or {}
    contexte = contexte or {}
    morceaux = [m for m in (contexte.get('titre'), contexte.get('section')) if m]
    texte = ' — '.join(morceaux)
    if contexte.get('date'):
        texte += ' (' + contexte['date'] + ')'
    return texte


def phrases_du_chunk(chunk_id):
    """Phrases d'un nouveau chunk : liste de ``(debut, fin, texte)`` (offsets dans ``versions.text``)."""
    ligne = noyau.srv().query('SELECT start, text, context FROM chunks WHERE id=?', (chunk_id,), True)
    if ligne is None:
        return []
    contexte = _charger_json(ligne['context'], {}) or {}
    resultat = []
    for debut, fin in contexte.get('phrases') or []:
        resultat.append((debut, fin, ligne['text'][debut - ligne['start']:fin - ligne['start']]))
    return resultat


# ---------------------------------------------------------------------------
# Etape 3 — Resolution du contexte, sans LLM
# ---------------------------------------------------------------------------

PRONOMS_SUJETS = {
    'il': ('Masc', 'Sing'), 'elle': ('Fem', 'Sing'), 'ils': ('Masc', 'Plur'), 'elles': ('Fem', 'Plur'),
    'celui-ci': ('Masc', 'Sing'), 'celle-ci': ('Fem', 'Sing'), 'ceux-ci': ('Masc', 'Plur'), 'celles-ci': ('Fem', 'Plur'),
    'celui-la': ('Masc', 'Sing'), 'celle-la': ('Fem', 'Sing'),
}
DEMONSTRATIFS = {'ce', 'cet', 'cette', 'ces'}
_RE_ANAPHORE = re.compile(
    r"^(?P<a>(?:il|elle|ils|elles|celui-ci|celle-ci|ceux-ci|celles-ci|ce dernier|cette derniere|ces derniers|ces dernieres)\b"
    r"|(?:cet|cette|ces|ce)\s+[a-z][\w-]+)")
_RE_SUJET_REGLE = re.compile(
    r"^(?:(?i:en|selon|depuis|pour|dans|apr[eè]s|avant|pendant|lors|d[eè]s|sur)\b[^,]{0,80},\s*)?"
    r"(?P<s>(?:(?i:le|la|les|l['’]|un|une|des|du|son|sa|ses|leur|leurs|notre|nos)\s*[\w-]+|[A-ZÀ-Ý][\w-]+)"
    r"(?:\s+[A-ZÀ-Ý][\w-]+)*(?:\s+(?i:de|du|des|d['’])\s*(?:(?i:la)\s+|(?i:l)['’])?[\w-]+(?:\s+[A-ZÀ-Ý][\w-]+)*)?)")
_MOTS_NON_NOMINAUX = {'il', 'elle', 'ils', 'elles', 'on', 'nous', 'vous', 'je', 'tu', 'ce', 'cela', 'ceci', 'ca', 'c', 'qui',
                      'que', 'ici', 'mais', 'et', 'ou', 'donc', 'puis', 'ainsi', 'alors', 'pourtant', 'cependant'}


def resoudre_contexte(version_id, forcer=False):
    """Etape 3 : coreferences et expressions temporelles des chunks d'une version, sans LLM.

    Coreferences : coreferee si installe (et compatible), sinon regle
    « dernier sujet nominal du meme paragraphe » sur l'arbre spaCy, sinon
    heuristique sur le texte. Expressions temporelles : ``dates.trouver``
    avec la date de publication comme reference. Resultats dans le JSON
    ``chunks.context`` (``dates``, ``coreferences``, ``reference``). Segmente
    la version si ce n'est pas fait. Renvoie des compteurs.
    """
    _assurer_colonnes()
    with _execution('m1.contexte', moteurs={'coreferences': 'auto', 'dates': 'regles:' + VERSION_REGLES},
                    prompt=VERSION_REGLES, pages=1) as run_id:
        return _resoudre_contexte(version_id, forcer, run_id)


def _resoudre_contexte(version_id, forcer, run_id):
    """Corps de ``resoudre_contexte``."""
    serveur = noyau.srv()
    version = _version(version_id)
    chunks = serveur.query(
        'SELECT id, start, "end", text, kind, context FROM chunks WHERE version_id=? AND kind IS NOT NULL ORDER BY start',
        (version_id,))
    if not chunks:
        _segmenter(version_id, True, run_id)
        version = _version(version_id)
        chunks = serveur.query(
            'SELECT id, start, "end", text, kind, context FROM chunks WHERE version_id=? AND kind IS NOT NULL ORDER BY start',
            (version_id,))
    for chunk in chunks:
        chunk['contexte'] = _charger_json(chunk['context'], {}) or {}
    if not forcer and chunks and all('contexte_run' in c['contexte'] for c in chunks):
        return {'chunks': 0, 'dates': 0, 'coreferences': 0, 'deja': True}
    reference = _date_reference(version)
    texte = version.get('text') or ''
    nombre_dates = 0
    for chunk in chunks:
        trouvees = []
        if chunk['kind'] not in ('usage',):
            for expression in dates.trouver(chunk['text'], reference):
                expression['start'] += chunk['start']
                expression['end'] += chunk['start']
                expression.pop('motif', None)
                if expression['debut'] and expression['debut'] == expression['fin']:
                    expression['iso'] = expression['debut']
                elif expression['debut'] or expression['fin']:
                    expression['iso'] = (expression['debut'] or '..') + '/' + (expression['fin'] or '..')
                trouvees.append(expression)
        chunk['contexte']['dates'] = trouvees
        chunk['contexte']['reference'] = reference
        chunk['contexte']['coreferences'] = []
        nombre_dates += len(trouvees)
    coreferences, moteur = _coreferences(texte, chunks)
    for chunk in chunks:
        chunk['contexte']['coreferences'] = coreferences.get(chunk['id'], [])
        chunk['contexte']['moteur_coreferences'] = moteur
        chunk['contexte']['contexte_run'] = run_id
    with noyau.connexion() as base:
        base.executemany('UPDATE chunks SET context=? WHERE id=?', [(_json(c['contexte']), c['id']) for c in chunks])
    return {'chunks': len(chunks), 'dates': nombre_dates, 'coreferences': sum(len(v) for v in coreferences.values()),
            'moteur_coreferences': moteur, 'deja': False}


def _coreferences(texte, chunks):
    """Anaphores resolues par chunk : coreferee, sinon regle sur spaCy, sinon heuristique."""
    paragraphes = [c for c in chunks if c['kind'] in ('paragraphe', 'liste', 'citation', 'note')]
    if not paragraphes:
        return {}, 'aucun'
    resultat = _coreferences_coreferee(texte, paragraphes)
    if resultat is not None:
        return resultat, 'coreferee'
    doc = moteurs.analyse(texte) if texte else None
    if doc is not None:
        return _coreferences_spacy(doc, paragraphes), 'regle_sujet:' + (moteurs.dernier('analyse') or 'spacy')
    return _coreferences_regles(texte, paragraphes), 'regle_sujet:heuristique'


def _coreferences_coreferee(texte, paragraphes):
    """Coreferences par coreferee (modele spaCy, francais) ; None s'il est absent ou incompatible."""
    if not moteurs.paquet_present('coreferee'):
        return None
    try:
        import coreferee  # noqa: F401  (enregistre la composante spaCy)
        nlp = moteurs._spacy()
        if 'coreferee' not in nlp.pipe_names:
            nlp.add_pipe('coreferee')
        doc = nlp(texte)
        chaines = doc._.coref_chains
    except Exception:
        return None
    debuts = [c['start'] for c in paragraphes]
    resultat = {}
    for chaine in chaines:
        tete = chaine[chaine.most_specific_mention_index]
        jeton_tete = doc[tete[0]]
        for mention in chaine:
            jeton = doc[mention[0]]
            if mention is tete or jeton.pos_ != 'PRON':
                continue
            rang = bisect.bisect_right(debuts, jeton.idx) - 1
            if rang < 0 or jeton.idx >= paragraphes[rang]['end']:
                continue
            resultat.setdefault(paragraphes[rang]['id'], []).append({
                'start': jeton.idx, 'end': jeton.idx + len(jeton.text), 'texte': jeton.text,
                'antecedent': jeton_tete.text, 'antecedent_start': jeton_tete.idx,
                'antecedent_end': jeton_tete.idx + len(jeton_tete.text), 'regle': 'coreferee'})
    return resultat


def _groupe_nominal(jeton):
    """Span (debut, fin) du groupe nominal d'un nom spaCy : determinants, adjectifs, noms propres composes."""
    debut = jeton.left_edge.i
    fin = jeton.i
    for enfant in jeton.rights:
        if enfant.dep_ in ('amod', 'flat:name', 'flat', 'fixed', 'nummod', 'compound') and enfant.i == fin + 1:
            fin = enfant.i
        elif enfant.dep_ == 'nmod' and enfant.i <= fin + 4:
            fin = enfant.right_edge.i
        else:
            break
    doc = jeton.doc
    while debut < jeton.i and doc[debut].dep_ in ('punct', 'cc', 'mark', 'case'):
        debut += 1
    return doc[debut].idx, doc[fin].idx + len(doc[fin].text)


def _accord(jeton, attendu):
    """Accord en genre et nombre (tolerant si la morphologie manque)."""
    genre, nombre = attendu
    genres = jeton.morph.get('Gender')
    nombres = jeton.morph.get('Number')
    if genres and genre not in genres:
        return False
    if nombres and nombre not in nombres:
        return False
    return True


def _coreferences_spacy(doc, paragraphes):
    """Regle « dernier sujet nominal du meme paragraphe » sur l'arbre de dependances spaCy."""
    debuts = [c['start'] for c in paragraphes]
    resultat = {}
    candidats = {}
    for phrase in doc.sents:
        rang = bisect.bisect_right(debuts, phrase.start_char) - 1
        if rang < 0 or phrase.start_char >= paragraphes[rang]['end']:
            continue
        identifiant = paragraphes[rang]['id']
        liste = candidats.setdefault(identifiant, [])
        nouveaux = []
        for jeton in phrase:
            if not jeton.dep_.startswith('nsubj'):
                continue
            minuscule = dates.normaliser(jeton.text)
            if jeton.pos_ == 'PRON' and minuscule in PRONOMS_SUJETS:
                antecedent = next((c for c in reversed(liste) if _accord(c['jeton'], PRONOMS_SUJETS[minuscule])), None)
                if antecedent:
                    resultat.setdefault(identifiant, []).append({
                        'start': jeton.idx, 'end': jeton.idx + len(jeton.text), 'texte': jeton.text,
                        'antecedent': antecedent['texte'], 'antecedent_start': antecedent['start'],
                        'antecedent_end': antecedent['end'], 'regle': 'dernier_sujet_nominal'})
                    nouveaux.append(antecedent)
                continue
            if jeton.pos_ in ('NOUN', 'PROPN'):
                determinants = [e for e in jeton.children if e.dep_ == 'det']
                demonstratif = any(dates.normaliser(e.text) in DEMONSTRATIFS for e in determinants)
                debut, fin = _groupe_nominal(jeton)
                if demonstratif and liste:
                    attendu = (next(iter(jeton.morph.get('Gender')), None), next(iter(jeton.morph.get('Number')), None))
                    accordes = [c for c in liste if None in attendu or _accord(c['jeton'], attendu)]
                    if not accordes:
                        continue
                    antecedent = accordes[-1]
                    resultat.setdefault(identifiant, []).append({
                        'start': debut, 'end': fin, 'texte': doc.text[debut:fin], 'antecedent': antecedent['texte'],
                        'antecedent_start': antecedent['start'], 'antecedent_end': antecedent['end'],
                        'regle': 'demonstratif_dernier_sujet'})
                    continue
                nouveaux.append({'jeton': jeton, 'texte': doc.text[debut:fin], 'start': debut, 'end': fin})
        liste.extend(nouveaux)
    return resultat


def _coreferences_regles(texte, paragraphes):
    """Heuristique sans spaCy : pronom ou demonstratif en tete de phrase -> premier groupe nominal sujet precedent."""
    resultat = {}
    for chunk in paragraphes:
        sujets = []
        for debut, fin in chunk['contexte'].get('phrases') or [(chunk['start'], chunk['end'])]:
            phrase = texte[debut:fin]
            norm = dates.normaliser(phrase)
            anaphore = _RE_ANAPHORE.match(norm)
            if anaphore and sujets:
                antecedent = sujets[-1]
                mot = anaphore.group('a')
                attendu = PRONOMS_SUJETS.get(mot)
                if attendu:
                    accordes = [s for s in sujets if _accord_regles(s['texte'], attendu)]
                    antecedent = accordes[-1] if accordes else antecedent
                resultat.setdefault(chunk['id'], []).append({
                    'start': debut + anaphore.start('a'), 'end': debut + anaphore.end('a'), 'texte': phrase[anaphore.start('a'):anaphore.end('a')],
                    'antecedent': antecedent['texte'], 'antecedent_start': antecedent['start'],
                    'antecedent_end': antecedent['end'], 'regle': 'dernier_sujet_nominal:heuristique'})
                sujets.append(antecedent)
                continue
            sujet = _RE_SUJET_REGLE.match(phrase)
            if sujet:
                mots = dates.normaliser(sujet.group('s')).split()
                if mots and mots[0] not in _MOTS_NON_NOMINAUX:
                    d = debut + sujet.start('s')
                    f = debut + sujet.end('s')
                    sujets.append({'texte': texte[d:f], 'start': d, 'end': f})
    return resultat


def _accord_regles(groupe, attendu):
    """Accord approximatif par le determinant (le/un -> masculin, la/une -> feminin, les/des -> pluriel)."""
    premier = dates.normaliser(groupe).split()[0] if groupe.split() else ''
    genre, nombre = attendu
    if premier in ('les', 'des', 'ses', 'leurs', 'nos', 'ces'):
        return nombre == 'Plur'
    if premier in ('la', 'une', 'sa', 'cette'):
        return genre == 'Fem' and nombre == 'Sing'
    if premier in ('le', 'un', 'son', 'ce', 'cet', 'du'):
        return genre == 'Masc' and nombre == 'Sing'
    return True


# -- Ancrage temporel des unites --------------------------------------------

def ancrer(unit_ids=None, version_ids=None, forcer=True):
    """Date d'ancrage de chaque unite, dans l'ordre du document.

    1. date ecrite dans la phrase (span, phrase(s) du span, texte de l'unite
       et periode de l'ancienne affirmation) -> ``phrase`` ;
    2. date relative resolue dans le span ou la phrase -> ``relative`` ;
    3. date de mise a jour de la page -> ``maj`` ;
    4. date de publication -> ``publication`` ; sinon ``inconnue``.

    Ecrit ``units.anchor_date``, ``units.anchor_kind`` (et ``anchor_run``).
    ``unit_ids`` / ``version_ids`` limitent le perimetre ; ``forcer=False``
    ne traite que les unites jamais ancrees par ce module. Appelable par le
    lot unites et par la migration. Renvoie la repartition par ``anchor_kind``.
    """
    _assurer_colonnes()
    with _execution('m1.ancrage', moteurs={'dates': 'regles:' + VERSION_REGLES}, prompt=VERSION_REGLES) as run_id:
        return _ancrer(unit_ids, version_ids, forcer, run_id)


def _selection_unites(unit_ids, version_ids, colonne_run, forcer):
    """Identifiants des unites a traiter (pas de requete par unite)."""
    serveur = noyau.srv()
    condition = '' if forcer else ' AND ' + colonne_run + ' IS NULL'
    if unit_ids is not None:
        resultat = []
        for lot in _par_lots(list(unit_ids), 500):
            resultat.extend(r['id'] for r in serveur.query(
                'SELECT id FROM units WHERE id IN (' + _marques(lot) + ')' + condition, tuple(lot)))
        return resultat
    if version_ids is not None:
        resultat = []
        for lot in _par_lots(list(version_ids), 500):
            resultat.extend(r['id'] for r in serveur.query(
                'SELECT id FROM units WHERE version_id IN (' + _marques(lot) + ')' + condition + ' ORDER BY version_id, span_start',
                tuple(lot)))
        return resultat
    return [r['id'] for r in serveur.query('SELECT id FROM units WHERE 1=1' + condition + ' ORDER BY version_id, span_start')]


def _charger_unites(identifiants):
    """Unites avec chunk, periode de l'ancienne affirmation (si la table existe)."""
    serveur = noyau.srv()
    a_claims = bool(serveur.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='claims'", one=True))
    colonne_periode = 'c.period' if a_claims else 'NULL'
    jointure = 'LEFT JOIN claims c ON c.id=u.legacy_claim_id ' if a_claims else ''
    lignes = []
    for lot in _par_lots(identifiants, 500):
        lignes.extend(serveur.query(
            'SELECT u.id, u.version_id, u.chunk_id, u.text, u.quote, u.span_start, u.span_end, u.anchor_date, u.anchor_kind, '
            'u.excluded, u.extractor, ch.kind chunk_kind, ' + colonne_periode + ' periode '
            'FROM units u LEFT JOIN chunks ch ON ch.id=u.chunk_id ' + jointure +
            'WHERE u.id IN (' + _marques(lot) + ')', tuple(lot)))
    return lignes


class _CacheVersions:
    """Textes, dates et phrases des versions (une requete par version)."""

    def __init__(self):
        self.versions = {}
        self.phrases = {}

    def version(self, version_id):
        if version_id not in self.versions:
            try:
                self.versions[version_id] = _version(version_id)
            except ValueError:
                self.versions[version_id] = None
        return self.versions[version_id]

    def phrases_de(self, version_id):
        if version_id not in self.phrases:
            liste = []
            for ligne in noyau.srv().query(
                    'SELECT context FROM chunks WHERE version_id=? AND kind IS NOT NULL', (version_id,)):
                contexte = _charger_json(ligne['context'], {}) or {}
                liste.extend(tuple(p) for p in contexte.get('phrases') or [])
            liste.sort()
            self.phrases[version_id] = liste
        return self.phrases[version_id]


def _phrase_du_span(cache, unite, texte):
    """Texte des phrases qui recouvrent le span de l'unite (fenetre par regles si la version n'est pas segmentee)."""
    debut = unite['span_start']
    fin = unite['span_end'] if unite['span_end'] is not None else debut
    phrases = cache.phrases_de(unite['version_id'])
    if phrases:
        debuts = [p[0] for p in phrases]
        rang = max(0, bisect.bisect_right(debuts, debut) - 1)
        couvertes = []
        while rang < len(phrases) and phrases[rang][0] < max(fin, debut + 1):
            if phrases[rang][1] > debut:
                couvertes.append(phrases[rang])
            rang += 1
        if couvertes:
            return couvertes[0][0], texte[couvertes[0][0]:couvertes[-1][1]]
    gauche = max(0, debut - 400)
    fenetre = texte[gauche:min(len(texte), fin + 400)]
    for d, f in moteurs.phrases_regles(fenetre):
        if gauche + d <= debut < gauche + f or (gauche + d < fin and gauche + f > debut):
            return gauche + d, fenetre[d:f]
    return debut, texte[debut:fin]


def _contexte_temporel(cache, unite):
    """Expressions temporelles de l'unite : span, phrase, texte de l'unite, periode historique."""
    version = cache.version(unite['version_id']) or {}
    texte = version.get('text') or ''
    reference = _date_reference(version) if version else None
    if unite['span_start'] is not None and texte:
        span = texte[unite['span_start']:unite['span_end']]
        _, phrase = _phrase_du_span(cache, unite, texte)
    else:
        span = unite.get('quote') or ''
        phrase = ''
    return {
        'reference': reference,
        'span': dates.trouver(span, reference),
        'phrase': dates.trouver(phrase, reference) if phrase and phrase != span else [],
        'unite': dates.trouver(unite.get('text') or '', reference),
        'periode': dates.trouver(unite.get('periode') or '', reference),
        'version': version,
    }


def _ancre_unite(temporel):
    """(anchor_date, anchor_kind) selon l'ordre du document."""
    for cle in ('span', 'phrase', 'unite', 'periode'):
        absolues = [e for e in temporel[cle] if e['nature'] == 'absolue' and dates.ancre(e)]
        if absolues:
            return max(dates.ancre(e) for e in absolues), 'phrase'
    for cle in ('span', 'phrase'):
        relatives = [e for e in temporel[cle] if e['nature'] == 'relative' and e['resolue'] and dates.ancre(e)]
        if relatives:
            return max(dates.ancre(e) for e in relatives), 'relative'
    version = temporel['version'] or {}
    date_page, type_date = _date_page(version) if version else (None, None)
    meta = (version.get('meta_json') or {}).get('metadonnees') or {} if version else {}
    mise_a_jour = _date_iso(version.get('updated')) or _date_iso(meta.get('mise_a_jour')) if version else None
    if mise_a_jour:
        return mise_a_jour, 'maj'
    if date_page and type_date == 'publication':
        return date_page, 'publication'
    return None, 'inconnue'


def _ancrer(unit_ids, version_ids, forcer, run_id):
    """Corps de ``ancrer``."""
    serveur = noyau.srv()
    identifiants = _selection_unites(unit_ids, version_ids, 'anchor_run', forcer)
    repartition = {'phrase': 0, 'relative': 0, 'maj': 0, 'publication': 0, 'inconnue': 0}
    cache = _CacheVersions()
    taille = int(_seuil('m1.lot_unites'))
    total = len(identifiants)
    fait = 0
    for lot in _par_lots(identifiants, taille):
        serveur.checkpoint()
        mises_a_jour = []
        for unite in _charger_unites(lot):
            temporel = _contexte_temporel(cache, unite)
            ancre_date, ancre_type = _ancre_unite(temporel)
            repartition[ancre_type] += 1
            mises_a_jour.append((ancre_date, ancre_type, run_id, unite['id']))
        with noyau.connexion() as base:
            base.executemany('UPDATE units SET anchor_date=?, anchor_kind=?, anchor_run=? WHERE id=?', mises_a_jour)
        fait += len(lot)
        serveur.JOB.update(done=fait, total=total, message='Ancrage temporel des unites')
    repartition['total'] = total
    return repartition


# ---------------------------------------------------------------------------
# Etape 6 — Classes d'obsolescence
# ---------------------------------------------------------------------------

_RE_FUTUR = re.compile(
    r"\b(?:sera|seront|serai|serons|serez|aura|auront|fera|feront|ira|iront|devra|devront|pourra|pourront|viendra|viendront"
    r"|deviendra|deviendront|faudra|vaudra|saura|sauront|verra|verront|tiendra|tiendront)\b"
    r"|\b\w{3,}(?:erai|eras|era|erons|erez|eront|irai|iras|ira|irons|irez|iront)\b")
_RE_CONDITIONNEL_PREVISION = re.compile(r"\b(?:devrai(?:t|ent)|pourrai(?:t|ent)\s+(?:etre|atteindre|ouvrir|fermer)|serai(?:t|ent)\s+prevu)")
_RE_ANNONCE = re.compile(
    r"\b(?:prevu|prevue|prevus|prevues)\s+(?:pour|en|le|a|d'ici|au|dans|du|fin|debut)\b|\ba venir\b|\bprochainement\b"
    r"|\bprojette\s+de\b|\ba pour objectif de\b|\bvise a\b|\bambitionne\s+de\b|\benvisage\s+de\b|\bprevoit\s+(?:de|d'|la|le|une|un|des)\b"
    r"|\bva\s+(?:etre|\w+(?:er|ir|re))\b|\bvont\s+(?:etre|\w+(?:er|ir|re))\b|\bannonce\s+pour\b|\bdoit\s+(?:ouvrir|fermer|entrer|etre\s+(?:lance|livre|ouvert|ferme))")
_RE_PASSE_COMPOSE = re.compile(
    r"\b(?:a|ont|avait|avaient|eut|eurent|aurait|etait|etaient)\s+(?:(?:deja|ete|alors|ensuite|aussi|pas|jamais|bien)\s+){0,2}"
    r"(?!ete\b)[a-z]+(?:e|ee|es|ees|i|ie|is|ies|it|u|ue|us|ues|ert|erte|ort|orte|int|uit|ait)\b")
# « est / sont + participe » : passe compose seulement avec un verbe conjugue avec etre (« est ne », « sont arrives »,
# « s'est tenu ») ; sinon c'est un passif present (« est dirigee par ») et l'enonce decrit un etat.
_RE_PASSE_ETRE = re.compile(
    r"\b(?:est|sont)\s+(?:(?:deja|alors|ensuite|aussi|pas|jamais|bien)\s+){0,2}"
    r"(?:nee?s?|morte?s?|decedee?s?|venue?s?|devenue?s?|revenue?s?|parvenue?s?|survenue?s?|intervenue?s?|arrivee?s?"
    r"|partie?s?|entree?s?|sortie?s?|tombee?s?|retournee?s?|apparue?s?|disparue?s?|montee?s?|descendue?s?|passee?s?)\b"
    r"|\b(?:s'est|se sont|s'etait|s'etaient)\s+(?:(?:deja|alors|ensuite|aussi|pas|jamais|bien)\s+){0,2}"
    r"[a-z]+(?:e|ee|es|ees|i|ie|is|ies|it|u|ue|us|ues|ert|erte|ort|orte|int|uit|ait)\b")
_RE_PASSE_EVENEMENT = re.compile(
    r"\b(?:a eu lieu|ont eu lieu|est ne|est nee|sont nes|est mort|est morte|est decede|est decedee|fonde en|fondee en|cree en"
    r"|creee en|ne en|nee en|ne le|nee le|lance le|lancee le|lance en|lancee en|publie le|publiee le|publie en|publiee en"
    r"|depose le|deposee le|delivre le|introduit en|introduite en|rachete en|rachetee en|annonce le|annoncee le)\b")
_RE_PASSE_SIMPLE = re.compile(r"\b(?:fut|furent|eut|eurent|devint|devinrent|naquit|mourut|lanca|annonca|publia|crea|fonda)\b")
# Verbes d'evenement (participes passes) : seul un temps passe porte par l'un d'eux rend une unite « immuable »
# par regle (document M1 etape 6 : « evenement passe date ») ; un etat raconte au passe va au classifieur.
_RE_VERBE_EVENEMENT = re.compile(
    r"\b(?:inaugure|fonde|cree|lance|ouvert|rouvert|ferme|signe|publie|paru|annonce|vote|adopte|promulgue|ratifie|elu|nomme"
    r"|remporte|gagne|perdu|rachete|acquis|achete|vendu|cede|fusionne|construit|livre|acheve|termine|demarre|commence|debute"
    r"|ne|mort|decede|tenu|deroule|organise|celebre|presente|devoile|condamne|juge|arrete|licencie|demissionne|quitte"
    r"|rejoint|decouvert|invente|ecrit|compose|realise|tourne|sorti|introduit|depose|delivre|detruit|incendie|demoli"
    r"|renove|restaure|conclu|approuve|rejete|annule|reporte|supprime|disparu|survenu|arrive|parti|entre|retire|interdit"
    r"|autorise|legalise|abroge|instaure|mis en service|mis en place|eu lieu)(?:e|s|es)?\b"
    r"|\b(?:fut|furent|naquit|mourut|lanca|annonca|publia|crea|fonda|devint)\b")
# Negation au present (« ne decrit pas », « n'est pas connue », « aucun document ») : un etat de la connaissance
# ou de la documentation, qui peut changer ; jamais « immuable » par regle.
_RE_NEGATION = re.compile(r"\b(?:ne|n')\s*[a-z]+\s+(?:pas|plus|jamais|guere)\b|\bn'(?:est|a|ont|sont)\s+(?:pas|plus|jamais)\b|\baucun(?:e)?\b"
                          r"|\bne\s+[a-z']+\s+rien\b|\bpersonne ne\b")
# Passe compose nie (« n'a pas publie », « n'a jamais ete annonce ») : une absence, donc un etat courant.
_RE_PASSE_NIE = re.compile(r"\bn'(?:a|ont|avait|avaient|est|sont|etait|etaient)\s+(?:toujours\s+|encore\s+)?(?:pas|jamais|plus|rien)\b")
_RE_DEFINITION = re.compile(r"\b(?:designe|designent|se definit|se definissent|est defini|est definie|on appelle|signifie|consiste a|consistent a"
                            r"|est un|est une|sont des|correspond a|correspondent a)\b")   # « renvoie a » : un renvoi, pas une definition
_RE_ETAT = re.compile(
    r"\b(?:actuellement|desormais|aujourd'hui|a ce jour|a l'heure actuelle|en vigueur|depuis|toujours|encore|en cours"
    r"|n'est plus|ne sont plus|n'a plus|n'ont plus|reste|restent|demeure|demeurent|actuel|actuelle|actuels|actuelles"
    r"|dorenavant|a present|en ce moment|courant)\b")
_RE_FONCTION = re.compile(
    r"\b(?:pdg|p-dg|president|presidente|directeur|directrice|dirigeant|dirigeante|ceo|proprietaire|ministre|maire|patron"
    r"|patronne|gerant|gerante|responsable|dirige|dirigent|possede|possedent|detient|detiennent|preside"
    r"|dirigee|dirigees|diriges|possedee|possedees|detenue|detenues|detenus|presidee|presidees)\b")
_RE_CHIFFRE = re.compile(r"\d|%|\b(?:euros?|dollars?|millions?|milliards?|prix|tarif|taux|cout|coute|coutent)\b")
_RE_SELON_STAT = re.compile(r"\bselon (?:l'insee|l'ined|eurostat|la dares|la banque de france|l'ocde|le fmi)\b")
_RE_METADONNEE_UNITE = re.compile(
    r"^(?:cet article|cette page|ce contenu|ce texte|ce billet|le present article)\b.{0,40}\b(?:a ete|est|sera)\s+"
    r"(?:publie|publiee|mis a jour|mise a jour|redige|redigee|ecrit|ecrite|signe|signee)\b"
    r"|^(?:cet article|cette page|ce contenu|ce texte|ce billet)\b.{0,20}\b(?:compte|contient|comporte)\s+(?:environ\s+)?[\d\s.,]+\s*mots\b"
    r"|^(?:publie|publiee|mis a jour|mise a jour|derniere mise a jour)\s*(?:le|:)\s*\d|^(?:auteur|autrice)\s*:")

# Exemples du classifieur few-shot (10 a 20 par classe, document M1 etape 6).
EXEMPLES = {
    'immuable': [
        "Victor Hugo est l'auteur des Misérables.",
        "Marie Curie est née le 7 novembre 1867 à Varsovie.",
        "La finale a eu lieu le 15 juillet 2018 à Moscou.",
        "Le traité de Rome a été signé en 1957.",
        "Google a lancé la mise à jour Panda en février 2011.",
        "Le brevet a été déposé en 2005 et délivré en 2014.",
        "Un moteur de recherche désigne un logiciel qui indexe des documents.",
        "Le PageRank se définit comme une probabilité de visite.",
        "La poule est un oiseau qui couve ses œufs.",
        "La vache est un mammifère qui donne du lait.",
        "Les abeilles fabriquent du miel.",
        "En 2009, la latence moyenne était passée sous 0,2 seconde.",
        "L'entreprise fut fondée en 1998 par deux étudiants.",
        "La Révolution française a commencé en 1789.",
        "Le mot algorithme vient du nom du mathématicien al-Khwarizmi.",
    ],
    'etat_courant': [
        "Le PDG de l'entreprise est Jean Dupont.",
        "Le ticket de métro coûte 2,15 euros.",
        "Selon l'Insee, le taux de chômage s'élève à 7,4 % de la population active.",
        "La société appartient désormais au groupe Alphabet.",
        "Le taux du livret A est actuellement de 3 %.",
        "La règle en vigueur impose un délai de trente jours.",
        "Google utilise aujourd'hui plusieurs centaines de signaux.",
        "Le service compte 200 salariés.",
        "La directive noindex retire la page de l'index.",
        "Le site est géré par la mairie de Paris.",
        "Depuis 2023, l'entreprise est dirigée par Anne Martin.",
        "Le moteur prend en charge les données structurées.",
        "La documentation recommande de déclarer un sitemap.",
        "Bing affiche les résultats en moins d'une seconde.",
        "Le statut du projet est toujours en cours d'examen.",
    ],
    'annonce': [
        "Le boulevard périphérique sera fermé du 1er au 31 août 2026.",
        "La ligne 18 doit ouvrir en 2027.",
        "Le gouvernement prévoit une baisse des impôts l'an prochain.",
        "Les travaux devraient s'achever d'ici la fin de l'été.",
        "L'entreprise vise 1 000 recrutements d'ici 2030.",
        "La conférence aura lieu le 12 mars à Lyon.",
        "La nouvelle version sera disponible au troisième trimestre.",
        "Le magasin ouvrira ses portes à partir du 1er septembre.",
        "Le projet de loi sera examiné en septembre.",
        "L'objectif est d'atteindre la neutralité carbone en 2050.",
        "Google va déployer cette fonctionnalité dans les prochains mois.",
        "Le festival se tiendra du 3 au 5 juillet prochain.",
    ],
    'metadonnee': [
        "Publié le 3 mars 2025 par Jean Dupont.",
        "Mis à jour le 12 janvier 2026.",
        "Article rédigé par la rédaction.",
        "Auteur : Marie Durand.",
        "Cet article a été mis à jour le 4 février 2026.",
        "Dernière mise à jour : 10 décembre 2025.",
        "L'article compte environ 6 000 mots.",
        "Cette page a été publiée en 2024.",
        "Texte écrit par Pierre Martin, journaliste.",
        "Par la rédaction, le 1er juin 2025.",
    ],
    'usage': [
        "Temps de lecture : 5 minutes.",
        "Partagez cet article sur les réseaux sociaux.",
        "À lire aussi : nos conseils pour bien démarrer.",
        "12 commentaires.",
        "Laisser un commentaire.",
        "Abonnez-vous à notre newsletter.",
        "Sommaire de l'article.",
        "Cliquez ici pour en savoir plus.",
        "Suivez-nous sur les réseaux sociaux.",
        "Lire aussi notre dossier complet.",
    ],
}


def _predicats():
    """Table prédicat -> definition (``chaine.predicats`` du lot unites, sinon table de secours)."""
    try:
        module = importlib.import_module('chaine.predicats')
        valeurs = module.tous()
    except Exception:
        return {nom: {'nom': nom, 'classe': classe} for nom, classe in PREDICATS_SECOURS.items()}, 'secours'
    if isinstance(valeurs, dict):
        valeurs = [dict(v, nom=v.get('nom', k)) if isinstance(v, dict) else {'nom': k, 'classe': v} for k, v in valeurs.items()]
    table = {}
    for valeur in valeurs or []:
        if isinstance(valeur, dict) and valeur.get('nom'):
            table[valeur['nom']] = valeur
        elif hasattr(valeur, 'nom'):
            table[valeur.nom] = {'nom': valeur.nom, 'classe': getattr(valeur, 'classe', None)}
    if not table:
        return {nom: {'nom': nom, 'classe': classe} for nom, classe in PREDICATS_SECOURS.items()}, 'secours'
    return table, 'predicats'


def _classe_predicat(definition):
    """Classe normalisee d'un predicat (valeurs ASCII du contrat)."""
    classe = dates.normaliser(str((definition or {}).get('classe') or '')).replace(' ', '_').replace('-', '_')
    alias = {'etat': 'etat_courant', 'annonce_ou_prevision': 'annonce', 'prevision': 'annonce', 'metadonnee_de_page': 'metadonnee',
             'usage_du_contenu': 'usage'}
    classe = alias.get(classe, classe)
    return classe if classe in CLASSES else None


def _temps_regles(norm):
    """Temps verbaux par expressions regulieres (repli sans spaCy)."""
    temps = set()
    if _RE_FUTUR.search(norm):
        temps.add('futur')
    if _RE_CONDITIONNEL_PREVISION.search(norm):
        temps.add('conditionnel')
    if _RE_PASSE_COMPOSE.search(norm) or _RE_PASSE_ETRE.search(norm) or _RE_PASSE_SIMPLE.search(norm):
        temps.add('passe')
    if not temps:
        temps.add('present')
    return temps


def _futur_ecrit(jeton):
    """Le jeton a-t-il une terminaison de futur ? (la morphologie spaCy marque a tort des presents en futur)."""
    return bool(_RE_FUTUR.search(dates.normaliser(jeton.text)))


def _temps_spacy(jetons):
    """Temps verbaux d'une suite de jetons spaCy (morphologie : futur, present, passe, conditionnel)."""
    temps = set()
    for jeton in jetons:
        if jeton.pos_ not in ('VERB', 'AUX'):
            continue
        morph = jeton.morph
        valeurs_temps = morph.get('Tense')
        if 'Cnd' in morph.get('Mood'):
            temps.add('conditionnel')
            continue
        if 'Part' in morph.get('VerbForm'):
            # participe passe : le temps est porte par l'auxiliaire
            auxiliaires = [e for e in jeton.children if e.dep_.startswith('aux')]
            if not auxiliaires:
                continue
            temps_aux = set()
            for auxiliaire in auxiliaires:
                temps_aux.update(auxiliaire.morph.get('Tense'))
            seulement_passif = all(e.dep_ == 'aux:pass' for e in auxiliaires)
            if 'Fut' in temps_aux and any(_futur_ecrit(e) for e in auxiliaires):
                temps.add('futur')
            elif seulement_passif and temps_aux <= {'Pres'}:
                temps.add('present')
            else:
                temps.add('passe')
            continue
        if jeton.pos_ == 'AUX' and jeton.dep_.startswith('aux'):
            # auxiliaire de temps compose : traite avec son participe, sauf le futur
            if 'Fut' in valeurs_temps and _futur_ecrit(jeton):
                temps.add('futur')
            continue
        if 'Fut' in valeurs_temps and _futur_ecrit(jeton):
            temps.add('futur')
        elif 'Fut' in valeurs_temps:
            temps.add('present')
        elif 'Past' in valeurs_temps or 'Imp' in valeurs_temps:
            temps.add('passe')
        elif 'Pres' in valeurs_temps:
            temps.add('present')
    return temps


def _temps_des_textes(textes):
    """Temps verbaux de chaque texte : morphologie spaCy (analyse groupee), sinon regles."""
    resultat = [None] * len(textes)
    taille_lot = 200
    for debut in range(0, len(textes), taille_lot):
        lot = textes[debut:debut + taille_lot]
        jointure = ''
        bornes = []
        for texte in lot:
            propre = (texte or '').replace('\n', ' ')
            bornes.append((len(jointure), len(jointure) + len(propre)))
            jointure += propre + '\n\n'
        doc = moteurs.analyse(jointure) if jointure.strip() else None
        if doc is None:
            break
        departs = [b[0] for b in bornes]
        groupes = [[] for _ in lot]
        for jeton in doc:
            rang = bisect.bisect_right(departs, jeton.idx) - 1
            if 0 <= rang < len(lot) and jeton.idx < bornes[rang][1]:
                groupes[rang].append(jeton)
        for rang, jetons in enumerate(groupes):
            temps = _temps_spacy(jetons)
            resultat[debut + rang] = temps
    for rang, texte in enumerate(textes):
        if resultat[rang] is None or not resultat[rang]:
            resultat[rang] = _temps_regles(dates.normaliser(texte or ''))
    return resultat


def _indices(texte, temps, expressions, reference):
    """Indices du classement : temps verbal, dates (passees/futures), marqueurs, chiffres."""
    norm = dates.normaliser(texte or '')
    ecrites = [e for e in expressions if e['nature'] == 'absolue' or e['resolue']]
    point = reference or date.today().isoformat()
    futures = [e for e in ecrites if (dates.ancre(e) or '') > point or (e.get('fin') or '') > point and e['modificateur'] in ('dici', 'jusqu', 'partir')]
    passees = [e for e in ecrites if (e.get('fin') or e.get('debut') or '9999') < point]
    return {
        'norm': norm,
        'temps': set(temps or ()),
        'dates': bool(ecrites),
        'dates_futures': bool(futures),
        'dates_passees': bool(passees) and not futures,
        'depuis': any(e['modificateur'] == 'depuis' for e in ecrites),
        'etat': bool(_RE_ETAT.search(norm)),
        'fonction': bool(_RE_FONCTION.search(norm)),
        'chiffre': bool(_RE_CHIFFRE.search(re.sub(r'\b(?:19|20)\d{2}\b', '', norm))),
        'annonce': bool(_RE_ANNONCE.search(norm)),
        'evenement': bool(_RE_PASSE_EVENEMENT.search(norm)),
        'verbe_evenement': bool(_RE_VERBE_EVENEMENT.search(norm)),
        'definition': bool(_RE_DEFINITION.search(norm)),
        'negation': bool(_RE_NEGATION.search(norm)),
        'passe_nie': bool(_RE_PASSE_NIE.search(norm)),
        'usage': _est_usage(norm),
        'metadonnee': bool(_RE_METADONNEE_UNITE.search(norm.strip())),
        'stat': bool(_RE_SELON_STAT.search(norm)),
        'generique': _generique(texte or ''),
    }


_RE_GENERIQUE = re.compile(r"^(?:Le|La|Les|L['’]|Un|Une)\s*[a-zà-ÿœ-]+")


def _generique(texte):
    """Phrase courte a sujet generique (determinant + nom commun) sans nom propre ni chiffre."""
    propre = texte.strip()
    mots = propre.split()
    if not 2 <= len(mots) <= 10 or not _RE_GENERIQUE.match(propre):
        return False
    return not any(m[:1].isupper() for m in mots[1:])


def _jetons_indices(indices):
    """Indices ajoutes au texte pour le classifieur (mots artificiels)."""
    jetons = ['indicetemps' + t for t in sorted(indices['temps'])]
    for cle in ('dates', 'dates_futures', 'dates_passees', 'depuis', 'etat', 'fonction', 'chiffre', 'annonce', 'evenement', 'definition'):
        if indices.get(cle):
            jetons.append('indice' + cle.replace('_', ''))
    return ' '.join(jetons)


def _score_regle(regle):
    """Score d'une regle de classement (``seuils`` : ``m1.regle.<regle>``, « : » -> « . », « + » -> « _ »)."""
    return float(_seuil('m1.regle.' + regle.replace(':', '.').replace('+', '_')))


def _regles_classe(indices):
    """Regles lexicales et temporelles : (classe, score, regle) ou (None, 0, '')."""
    candidats = []
    temps = indices['temps']
    if indices['usage']:
        candidats.append(('usage', _score_regle('lexique:usage'), 'lexique:usage'))
    if indices['metadonnee']:
        candidats.append(('metadonnee', _score_regle('lexique:metadonnee'), 'lexique:metadonnee'))
    prevision = 'futur' in temps or indices['annonce'] or 'conditionnel' in temps and indices['dates_futures']
    if prevision and indices['dates_futures']:
        candidats.append(('annonce', _score_regle('temps:futur+date_future'), 'temps:futur+date_future'))
    elif 'futur' in temps and indices['dates']:
        candidats.append(('annonce', _score_regle('temps:futur+date'), 'temps:futur+date'))
    elif indices['annonce'] and 'passe' not in temps:
        candidats.append(('annonce', _score_regle('lexique:prevision'), 'lexique:prevision'))
    elif 'futur' in temps and 'passe' not in temps:
        # futur sans date : souvent une generalite (« seront rappelees ») ; le classifieur tranche
        candidats.append(('annonce', _score_regle('temps:futur'), 'temps:futur'))
    elif indices['dates_futures'] and 'passe' not in temps:
        candidats.append(('annonce', _score_regle('date_future'), 'date_future'))
    if indices['etat'] and 'futur' not in temps:
        candidats.append(('etat_courant', _score_regle('lexique:marqueur_actuel'), 'lexique:marqueur_actuel'))
    # Une date passee (celle d'une citation, d'un communique) ne desactive ni la fonction ni le chiffre : un etat
    # au present reste un etat courant, a verifier (B3 de la relecture).
    if indices['fonction'] and 'present' in temps:
        candidats.append(('etat_courant', _score_regle('lexique:fonction_present'), 'lexique:fonction_present'))
    if (indices['chiffre'] or indices['stat']) and 'present' in temps and 'passe' not in temps:
        candidats.append(('etat_courant', _score_regle('temps:present+chiffre'), 'temps:present+chiffre'))
    # « Immuable » par regle : temps passe ET verbe d'evenement (« a eu lieu le », « a ete fonde en »), jamais une
    # date passee seule ; le reste va au classifieur.
    evenement_passe = ('passe' in temps and (indices['evenement'] or indices.get('verbe_evenement')) and not indices['etat']
                       and not indices.get('passe_nie'))
    if evenement_passe and indices['dates_passees']:
        candidats.append(('immuable', _score_regle('lexique:evenement_date' if indices['evenement'] else 'temps:passe+evenement+date'),
                           'temps:passe+evenement+date'))
    elif evenement_passe and not indices['dates']:
        candidats.append(('immuable', _score_regle('temps:passe+evenement'), 'temps:passe+evenement'))
    sans_negation = not indices.get('negation') or 'passe' in temps
    if indices['definition'] and not indices['dates'] and not indices['chiffre'] and not indices['etat'] \
            and not indices['fonction'] and sans_negation:
        candidats.append(('immuable', _score_regle('lexique:definition'), 'lexique:definition'))
    if indices['generique'] and temps == {'present'} and not indices['dates'] and not indices['chiffre'] \
            and not indices['etat'] and not indices['fonction'] and sans_negation:
        # verite generale courte, sujet generique sans nom propre (« La poule pond des oeufs »)
        # Indice faible seulement (la « verite generale » n'est pas dans la liste « immuable » du document ; sur un corpus
        # technique, ces phrases decrivent souvent l'etat courant d'un systeme) : le classifieur tranche.
        candidats.append(('immuable', _score_regle('lexique:verite_generale'), 'lexique:verite_generale'))
    if temps == {'present'}:
        candidats.append(('etat_courant', _score_regle('temps:present'), 'temps:present'))
    if indices['chiffre'] and indices['dates_passees'] and 'present' in temps:
        candidats.append(('etat_courant', _score_regle('chiffre_date_passee'), 'chiffre_date_passee'))
    if not candidats:
        return None, 0.0, ''
    candidats.sort(key=lambda c: (c[1], PRIORITE_CLASSES[c[0]]), reverse=True)
    return candidats[0]


def _confiance(resultat):
    """Confiance d'une sortie de ``moteurs.classer`` : score calibre (SetFit, LLM) ou part du meilleur score (centroides)."""
    moteur = str(resultat.get('engine') or '')
    score = float(resultat.get('score') or 0.0)
    if moteur.startswith(('lexical', 'centroide')):
        scores = [max(0.0, float(v)) for v in (resultat.get('scores') or {}).values()]
        somme = sum(scores)
        return score / somme if somme else 0.0
    return score


def _expiration(temporel, faits_dates=None):
    """Date de peremption d'une annonce : fin la plus tardive des dates ecrites ou resolues."""
    candidates = list(faits_dates or [])
    for cle in ('span', 'phrase', 'unite', 'periode'):
        for expression in temporel[cle]:
            if expression['nature'] == 'relative' and not expression['resolue']:
                continue
            valeur = expression.get('fin') or expression.get('debut')
            if valeur:
                candidates.append(valeur)
        if candidates:
            break
    return max(candidates) if candidates else None


def classer_obsolescence(unit_ids=None, version_ids=None, forcer=True, aujourdhui=None):
    """Etape 6 : classe d'obsolescence de chaque unite.

    D'abord par predicat (table predicat -> classe de ``chaine.predicats``) pour
    les unites qui ont des faits ; sinon (unites migrees sans faits) regles
    lexicales et temporelles, puis classifieur few-shot ``moteurs.classer``
    (indices : temps verbal, dates, marqueurs « actuellement », « desormais »,
    « depuis ») pour les cas sous ``m1.classe.confiance_min``.

    Traitements : ``immuable`` (conserve, jamais verifie pour l'obsolescence),
    ``etat_courant`` (obsolescible), ``annonce`` (``expires`` = date annoncee),
    ``metadonnee`` (conservee, sert d'ancrage), ``usage`` (``excluded=1``),
    ``inconnu`` si rien n'est assez sur. Renvoie la repartition par classe.
    """
    _assurer_colonnes()
    with _execution('m1.obsolescence', moteurs={'classer': 'auto', 'regles': VERSION_REGLES}, prompt=VERSION_REGLES,
                    seuils=['m1.classe.confiance_min']) as run_id:
        return _classer(unit_ids, version_ids, forcer, aujourdhui, run_id)


def _faits_par_unite(identifiants):
    """Faits (predicat, objet, periode) des unites, en une requete par lot."""
    serveur = noyau.srv()
    resultat = {}
    for lot in _par_lots(identifiants, 500):
        for ligne in serveur.query(
                "SELECT unit_id, predicate, object, object_norm, object_type, period FROM facts WHERE unit_id IN (" + _marques(lot) + ") "
                "AND COALESCE(status,'') NOT IN ('rejete','rejected','remplace')", tuple(lot)):
            resultat.setdefault(ligne['unit_id'], []).append(ligne)
    return resultat


def _classer(unit_ids, version_ids, forcer, aujourdhui, run_id):
    """Corps de ``classer_obsolescence``."""
    serveur = noyau.srv()
    seuil = float(_seuil('m1.classe.confiance_min'))
    jour = _aujourdhui(aujourdhui).isoformat()
    identifiants = _selection_unites(unit_ids, version_ids, 'obs_run', forcer)
    table, origine_table = _predicats()
    repartition = {c: 0 for c in CLASSES + ('inconnu',)}
    regles_utilisees = {'predicat': 0, 'regle': 0, 'classifieur': 0, 'regle_faible': 0, 'segment': 0, 'inconnu': 0}
    moteurs_classifieur = set()
    cache = _CacheVersions()
    taille = int(_seuil('m1.lot_unites'))
    total = len(identifiants)
    fait = 0
    exemples_indices = None
    for lot in _par_lots(identifiants, taille):
        serveur.checkpoint()
        unites = _charger_unites(lot)
        faits = _faits_par_unite([u['id'] for u in unites])
        textes = [u.get('text') or u.get('quote') or '' for u in unites]
        temps = _temps_des_textes(textes)
        decisions = {}
        a_classer = []
        for rang, unite in enumerate(unites):
            temporel = _contexte_temporel(cache, unite)
            expressions = temporel['span'] + temporel['phrase'] + temporel['unite']
            reference = temporel['reference'] or jour
            indices = _indices(textes[rang], temps[rang], expressions, reference)
            unite['temporel'] = temporel
            unite['indices'] = indices
            if unite.get('chunk_kind') in ('usage', 'metadonnee'):
                decisions[unite['id']] = (unite['chunk_kind'], 'segment:' + unite['chunk_kind'], None)
                continue
            faits_unite = faits.get(unite['id']) or []
            classes_faits = []
            dates_faits = []
            for fait_ in faits_unite:
                classe = _classe_predicat(table.get(fait_['predicate']))
                if classe:
                    classes_faits.append((classe, fait_['predicate']))
                for champ in ('object_norm', 'period', 'object'):
                    for expression in dates.trouver(fait_.get(champ) or '', temporel['reference']):
                        if expression.get('fin') or expression.get('debut'):
                            dates_faits.append(expression.get('fin') or expression.get('debut'))
            if classes_faits:
                classe, predicat = max(classes_faits, key=lambda c: PRIORITE_CLASSES[c[0]])
                expire = _expiration(temporel, dates_faits) if classe == 'annonce' else None
                decisions[unite['id']] = (classe, 'predicat:' + predicat + ('' if origine_table == 'predicats' else ':secours'), expire)
                continue
            classe, score, regle = _regles_classe(indices)
            unite['regle'] = (classe, score, regle)
            if classe and score >= seuil:
                expire = _expiration(temporel) if classe == 'annonce' else None
                decisions[unite['id']] = (classe, 'regle:' + regle, expire)
                continue
            a_classer.append(unite)
        if a_classer:
            if exemples_indices is None:
                exemples_indices = _exemples_avec_indices()
            entrees = [(u.get('text') or u.get('quote') or '') + ' ' + _jetons_indices(u['indices']) for u in a_classer]
            try:
                sorties = moteurs.classer(entrees, list(CLASSES_CLASSIFIEUR), exemples_indices)
            except moteurs.Indisponible:
                sorties = [None] * len(a_classer)
            for unite, sortie in zip(a_classer, sorties):
                classe_regle, score_regle, regle = unite['regle']
                if sortie:
                    moteurs_classifieur.add(sortie.get('engine'))
                    confiance = _confiance(sortie)
                    etiquette = sortie['label']
                    indices = unite['indices']
                    if etiquette == 'annonce' and not (indices['temps'] & {'futur', 'conditionnel'} or indices['annonce']
                                                       or indices['dates_futures']):
                        # une annonce suppose un futur, une prevision ou une date a venir : sinon, seconde etiquette
                        scores = {k: v for k, v in (sortie.get('scores') or {}).items() if k != 'annonce'}
                        etiquette = max(scores, key=scores.get) if scores else (classe_regle or 'etat_courant')
                        sortie = dict(sortie, label=etiquette, score=scores.get(etiquette, 0.0))
                        confiance = _confiance(sortie)
                    trace = 'classifieur:' + str(sortie.get('engine')) + ':' + format(confiance, '.2f')
                    if confiance >= seuil or (classe_regle and etiquette == classe_regle):
                        classe = etiquette
                        regle_finale = trace + ('+regle:' + regle if classe_regle == etiquette else '')
                    elif classe_regle == 'immuable':
                        # « Immuable » = jamais verifie : une regle faible contredite par le classifieur ne suffit pas.
                        classe = 'inconnu'
                        regle_finale = 'regle_faible_contredite:' + regle + '+' + trace + ':' + etiquette
                    elif classe_regle:
                        classe = classe_regle
                        regle_finale = 'regle_faible:' + regle + '+' + trace + ':' + etiquette
                    else:
                        classe = 'inconnu'
                        regle_finale = trace + ':' + etiquette
                elif classe_regle:
                    classe = classe_regle
                    regle_finale = 'regle_faible:' + regle
                else:
                    classe = 'inconnu'
                    regle_finale = 'aucune_regle'
                expire = _expiration(unite['temporel']) if classe == 'annonce' else None
                decisions[unite['id']] = (classe, regle_finale, expire)
        mises_a_jour = []
        for unite in unites:
            classe, regle, expire = decisions[unite['id']]
            repartition[classe] = repartition.get(classe, 0) + 1
            famille = regle.split(':')[0]
            regles_utilisees[famille] = regles_utilisees.get(famille, 0) + 1
            exclue = 1 if classe == 'usage' else unite.get('excluded') or 0
            mises_a_jour.append((classe, regle[:300], expire, exclue, run_id, unite['id']))
        with noyau.connexion() as base:
            base.executemany('UPDATE units SET obs_class=?, obs_rule=?, expires=?, excluded=?, obs_run=? WHERE id=?', mises_a_jour)
        fait += len(lot)
        serveur.JOB.update(done=fait, total=total, message="Classes d'obsolescence")
    repartition['total'] = total
    repartition['origine'] = regles_utilisees
    repartition['table_predicats'] = origine_table
    repartition['moteurs_classifieur'] = sorted(m for m in moteurs_classifieur if m)
    return repartition


def _exemples_avec_indices():
    """Exemples du classifieur augmentes des memes indices que les textes classes."""
    textes = [t for liste in EXEMPLES.values() for t in liste]
    temps = _temps_des_textes(textes)
    resultat = {}
    rang = 0
    for classe, liste in EXEMPLES.items():
        if classe not in CLASSES_CLASSIFIEUR:
            rang += len(liste)
            continue
        resultat[classe] = []
        for texte in liste:
            expressions = dates.trouver(texte, None)
            indices = _indices(texte, temps[rang], expressions, '2026-01-01')
            resultat[classe].append(texte + ' ' + _jetons_indices(indices))
            rang += 1
    return resultat


def perimer(aujourdhui=None):
    """Statut ``perimee_par_date`` : toute annonce dont la date (``expires``) est passee.

    Une annonce perimee devient « a-t-elle eu lieu, et comme annonce ? ».
    Seules les unites ``a_jour`` basculent (les statuts de M3 ne sont pas
    ecrases) ; une unite perimee dont l'annonce n'est plus passee (date
    corrigee, classe changee) revient a ``a_jour``. Renvoie les compteurs.
    """
    _assurer_colonnes()
    jour = _aujourdhui(aujourdhui).isoformat()
    with _execution('m1.peremption', moteurs={'peremption': 'regles'}, prompt=VERSION_REGLES, seuils={'aujourdhui': jour}) as run_id:
        with noyau.connexion() as base:
            perimees = base.execute(
                "UPDATE units SET temporal_status='perimee_par_date', temporal_run=? WHERE obs_class='annonce' AND expires IS NOT NULL "
                "AND expires < ? AND COALESCE(temporal_status,'a_jour')='a_jour'", (run_id, jour)).rowcount
            retablies = base.execute(
                "UPDATE units SET temporal_status='a_jour', temporal_run=? WHERE temporal_status='perimee_par_date' "
                "AND (COALESCE(obs_class,'')!='annonce' OR expires IS NULL OR expires >= ?)", (run_id, jour)).rowcount
            total = base.execute("SELECT count(*) FROM units WHERE temporal_status='perimee_par_date'").fetchone()[0]
    return {'perimees': perimees, 'retablies': retablies, 'total_perimees': total, 'aujourdhui': jour}


# ---------------------------------------------------------------------------
# Enchainement
# ---------------------------------------------------------------------------

def executer(version_ids=None, forcer=False, aujourdhui=None):
    """Enchaine segmentation, contexte, ancrage, classement et peremption.

    Reprenable : sans ``forcer``, une version deja segmentee ou contextualisee
    et une unite deja ancree ou classee par ce module ne sont pas refaites.
    Chaque etape a sa ligne ``step_runs``. Renvoie un dict de compteurs.
    """
    serveur = noyau.srv()
    _assurer_colonnes()
    if version_ids is None:
        version_ids = [r['id'] for r in serveur.query('SELECT id FROM versions ORDER BY created, id')]
    version_ids = list(version_ids)
    compteurs = {'versions': len(version_ids)}
    serveur.JOB.update(stage='m1.segmentation', done=0, total=len(version_ids), message='Segmentation des pages')
    with noyau.etape('m1.segmentation', moteurs={'phrases': 'auto'}, prompt=VERSION_REGLES,
                     seuils=['m1.segment.chunk_caracteres_max', 'm1.segment.phrase_caracteres_max'], pages=len(version_ids)) as run:
        segmentation = {'chunks': 0, 'unites_rattachees': 0, 'deja': 0}
        for rang, version_id in enumerate(version_ids):
            serveur.checkpoint()
            resultat = _segmenter(version_id, forcer, run.id)
            segmentation['chunks'] += resultat['chunks']
            segmentation['unites_rattachees'] += resultat['unites_rattachees']
            segmentation['deja'] += 1 if resultat['deja'] else 0
            if resultat.get('moteur_phrases'):
                run.moteur('phrases', resultat['moteur_phrases'])
            serveur.JOB.update(done=rang + 1)
        run.items = segmentation['chunks']
    compteurs['segmentation'] = segmentation
    serveur.JOB.update(stage='m1.contexte', done=0, total=len(version_ids), message='Coreferences et dates')
    with noyau.etape('m1.contexte', moteurs={'dates': 'regles:' + VERSION_REGLES}, prompt=VERSION_REGLES, pages=len(version_ids)) as run:
        contexte = {'chunks': 0, 'dates': 0, 'coreferences': 0, 'deja': 0}
        for rang, version_id in enumerate(version_ids):
            serveur.checkpoint()
            resultat = _resoudre_contexte(version_id, forcer, run.id)
            for cle in ('chunks', 'dates', 'coreferences'):
                contexte[cle] += resultat[cle]
            contexte['deja'] += 1 if resultat['deja'] else 0
            if resultat.get('moteur_coreferences'):
                run.moteur('coreferences', resultat['moteur_coreferences'])
            serveur.JOB.update(done=rang + 1)
        run.items = contexte['chunks']
    compteurs['contexte'] = contexte
    serveur.JOB.update(stage='m1.ancrage', done=0, total=0, message='Ancrage temporel des unites')
    with noyau.etape('m1.ancrage', moteurs={'dates': 'regles:' + VERSION_REGLES}, prompt=VERSION_REGLES, pages=len(version_ids)) as run:
        compteurs['ancrage'] = _ancrer(None, version_ids, forcer, run.id)
        run.items = compteurs['ancrage']['total']
    serveur.JOB.update(stage='m1.obsolescence', done=0, total=0, message="Classes d'obsolescence")
    with noyau.etape('m1.obsolescence', moteurs={'classer': 'auto', 'regles': VERSION_REGLES}, prompt=VERSION_REGLES,
                     seuils=['m1.classe.confiance_min'], pages=len(version_ids)) as run:
        compteurs['obsolescence'] = _classer(None, version_ids, forcer, aujourdhui, run.id)
        for moteur in compteurs['obsolescence']['moteurs_classifieur']:
            run.moteur('classer', moteur)
        run.items = compteurs['obsolescence']['total']
    serveur.JOB.update(stage='m1.peremption', message='Annonces perimees')
    compteurs['peremption'] = perimer(aujourdhui)
    serveur.JOB.update(message='M1 (page) termine')
    return compteurs
