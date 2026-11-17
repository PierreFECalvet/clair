"""Tests du lot « m1_page » : acquisition, segmentation, contexte, dates, classes d'obsolescence, peremption."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s
import chaine
from chaine import dates
from chaine import m1_page
from chaine import moteurs
from chaine import noyau
from chaine import schema
from chaine import seuils

AUJOURDHUI = '2026-11-13'

HTML_PERIPH = '''<html lang="fr"><head><title>Périph | Le Journal</title>
<meta property="article:published_time" content="2026-05-02T10:00:00+02:00">
<meta name="author" content="Quelqu'un d'autre">
<meta name="description" content="Description meta.">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebSite","name":"Le Journal"},
{"@type":"NewsArticle","headline":"Le périphérique fermé en août","datePublished":"2026-05-03",
"dateModified":"2026-06-01T08:00:00Z","author":[{"@type":"Person","name":"Jean Dupont"}],"description":"Fermeture estivale."}]}</script>
</head><body><nav><a href="/">Accueil</a></nav>
<article><h1>Le périphérique fermé en août</h1>
<div class="reading-time">Temps de lecture : 3 min</div>
<p class="chapo">Le boulevard périphérique sera fermé cet été.</p>
<h2>Les travaux</h2>
<p>Le boulevard périphérique parisien sera fermé du 1er au 31 août 2026, selon <a href="https://www.paris.fr/peripherique">la Ville de Paris</a>. Il rouvrira le 1er septembre.</p>
<blockquote><p>« C'est un chantier historique », a déclaré le préfet.</p></blockquote>
<ul><li>Voie gauche réservée</li><li>Vitesse limitée à <q>50 km/h</q></li></ul>
<div class="share-buttons">Partager sur Facebook</div>
<p>À lire aussi : la ligne 18</p>
<p>12 commentaires</p>
</article><aside>Articles similaires</aside><footer>Mentions légales</footer></body></html>'''

TEXTE_BRUT = (
    "La chaîne de crawl : une carte complète\r\n\r\n"
    "https://exemple.fr/moteurs/crawl/\r\n"
    "Publié le 3 mars 2025\r\n\r\n"
    "LE CRAWL AUJOURD'HUI\r\n\r\n"
    "Googlebot télécharge les pages selon une file de priorités. Il revient plus souvent sur les pages qui changent. "
    "Depuis 2023, le moteur limite la cadence par serveur.\r\n\r\n"
    "L'an dernier, Google a supprimé l'outil de soumission d'URL.\r\n\r\n"
    "SOURCES\r\n\r\n"
    "1. Google Search Central, documentation, 2024. https://developers.google.com/search\r\n"
    "2. Gary Illyes, podcast, mai 2024.\r\n\r\n"
    "— P.C."
)


class Base(unittest.TestCase):
    """Base temporaire initialisee par le serveur puis par la chaine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        s.DB = Path(self.tmp.name) / 'test.sqlite3'
        s.init()
        s.STOP.clear()
        self.config = dict(s.CONFIG)
        s.CONFIG.update(embeddings=False, ibou_key='test-only', model='', provider='lmstudio', openai_key='')
        chaine.brancher(s)
        schema.init()
        moteurs.reinitialiser()
        noyau.vider_reglages()
        seuils._vider_cache()
        s.CTX.step = None

    def tearDown(self):
        s.CONFIG.clear()
        s.CONFIG.update(self.config)
        s.STOP.clear()
        moteurs.reinitialiser()
        noyau.vider_reglages()
        seuils._vider_cache()
        self.tmp.cleanup()

    def unite(self, version_id, extrait, texte=None, identifiant=None, occurrence=0):
        """Insere une unite (comme la migration) dont le span est la n-ieme occurrence d'``extrait``."""
        texte_version = s.query('SELECT text FROM versions WHERE id=?', (version_id,), True)['text']
        debut = -1
        for _ in range(occurrence + 1):
            debut = texte_version.index(extrait, debut + 1)
        identifiant = identifiant or s.uid()
        ancien = s.query('SELECT id FROM chunks WHERE version_id=? AND kind IS NULL ORDER BY position', (version_id,), True)
        s.query('INSERT INTO units(id,version_id,chunk_id,text,quote,span_start,span_end,extractor,created) VALUES(?,?,?,?,?,?,?,?,?)',
                (identifiant, version_id, ancien['id'], texte or extrait, extrait, debut, debut + len(extrait), 'migration', s.now()))
        return identifiant

    def ligne(self, unit_id):
        return s.query('SELECT * FROM units WHERE id=?', (unit_id,), True)


# ---------------------------------------------------------------------------
# Expressions temporelles
# ---------------------------------------------------------------------------

class DatesTests(unittest.TestCase):

    def r(self, expression, reference='2026-03-15'):
        resultat = dates.resoudre(expression, reference)
        self.assertIsNotNone(resultat, expression)
        return resultat['debut'], resultat['fin']

    def test_exemples_du_document(self):
        self.assertEqual(self.r("l'an dernier"), ('2025-01-01', '2025-12-31'))
        self.assertEqual(self.r('mardi'), ('2026-03-10', '2026-03-10'))
        self.assertEqual(self.r('depuis 2023'), ('2023-01-01', '2026-03-15'))
        self.assertEqual(self.r("d'ici la fin de l'été"), ('2026-03-15', '2026-09-22'))

    def test_dates_absolues_et_intervalles(self):
        resultat = dates.resoudre('du 1er au 31 août 2026', None)
        self.assertEqual((resultat['debut'], resultat['fin'], resultat['type'], resultat['nature']),
                         ('2026-08-01', '2026-08-31', 'intervalle', 'absolue'))
        self.assertEqual(self.r('le 1er août'), ('2026-08-01', '2026-08-01'))
        self.assertEqual(dates.resoudre('le 1er août', '2026-03-15')['nature'], 'relative')
        self.assertEqual(self.r('du 3 juillet au 5 août 2026'), ('2026-07-03', '2026-08-05'))
        self.assertEqual(self.r('entre 1999 et 2009'), ('1999-01-01', '2009-12-31'))
        self.assertEqual(self.r('2019-2023'), ('2019-01-01', '2023-12-31'))
        self.assertEqual(self.r('12/03/2025'), ('2025-03-12', '2025-03-12'))
        self.assertEqual(self.r('2025-12-10'), ('2025-12-10', '2025-12-10'))
        self.assertEqual(self.r('dans les années 1990'), ('1990-01-01', '1999-12-31'))
        self.assertEqual(self.r('de janvier à mars 2026'), ('2026-01-01', '2026-03-31'))

    def test_relatives(self):
        self.assertEqual(self.r('il y a trois mois'), ('2025-12-15', '2025-12-15'))
        self.assertEqual(self.r("en début d'année"), ('2026-01-01', '2026-04-30'))
        self.assertEqual(self.r('fin 2025'), ('2025-09-01', '2025-12-31'))
        self.assertEqual(self.r('cette semaine'), ('2026-03-09', '2026-03-15'))
        self.assertEqual(self.r('la semaine dernière'), ('2026-03-02', '2026-03-08'))
        self.assertEqual(self.r('hier'), ('2026-03-14', '2026-03-14'))
        self.assertEqual(self.r('demain'), ('2026-03-16', '2026-03-16'))
        self.assertEqual(self.r('mardi prochain'), ('2026-03-17', '2026-03-17'))
        self.assertEqual(self.r('mardi dernier'), ('2026-03-10', '2026-03-10'))
        self.assertEqual(self.r('le mois prochain'), ('2026-04-01', '2026-04-30'))
        self.assertEqual(self.r("d'ici trois mois"), ('2026-03-15', '2026-06-15'))
        self.assertEqual(self.r('depuis trois ans'), ('2023-03-15', '2026-03-15'))
        self.assertEqual(self.r("l'année prochaine"), ('2027-01-01', '2027-12-31'))

    def test_trimestres_saisons(self):
        self.assertEqual(self.r('au troisième trimestre 2025'), ('2025-07-01', '2025-09-30'))
        self.assertEqual(self.r('T2 2026'), ('2026-04-01', '2026-06-30'))
        self.assertEqual(self.r('ce trimestre'), ('2026-01-01', '2026-03-31'))
        self.assertEqual(self.r('le premier semestre 2026'), ('2026-01-01', '2026-06-30'))
        self.assertEqual(self.r("l'été 2026"), ('2026-06-21', '2026-09-22'))
        self.assertEqual(self.r("l'hiver dernier"), ('2024-12-21', '2025-03-19'))
        self.assertEqual(self.r('début août 2026'), ('2026-08-01', '2026-08-10'))

    def test_modificateurs(self):
        resultat = dates.resoudre("jusqu'au 31 décembre 2026", '2026-03-15')
        self.assertEqual((resultat['debut'], resultat['fin'], resultat['modificateur']), ('2026-03-15', '2026-12-31', 'jusqu'))
        resultat = dates.resoudre('à partir du 1er septembre 2026', None)
        self.assertEqual((resultat['debut'], resultat['fin'], resultat['modificateur']), ('2026-09-01', None, 'partir'))
        resultat = dates.resoudre('depuis 2023', None)
        self.assertEqual((resultat['debut'], resultat['fin']), ('2023-01-01', None))

    def test_sans_reference_et_faux_positifs(self):
        resultat = dates.resoudre("l'an dernier", None)
        self.assertFalse(resultat['resolue'])
        self.assertIsNone(resultat['debut'])
        self.assertIsNone(dates.resoudre('2000 pages indexées', None))
        self.assertEqual(dates.trouver('Il a 1500 salariés et 2048 serveurs.', None), [])
        trouvees = dates.trouver('Le 26 juillet 2021, Google lance une mise à jour (p. 6399).', None)
        self.assertEqual([(e['texte'], e['debut']) for e in trouvees], [('Le 26 juillet 2021', '2021-07-26')])

    def test_offsets_conserves(self):
        texte = 'Pendant l’été 2026, la cadence évoluera.'
        trouvee = dates.trouver(texte, None)[0]
        self.assertEqual(texte[trouvee['start']:trouvee['end']], 'l’été 2026')


# ---------------------------------------------------------------------------
# Etape 1 — acquisition
# ---------------------------------------------------------------------------

class AcquisitionTests(Base):

    def test_extraction_html_maison(self):
        extraction = m1_page.extraire_html(HTML_PERIPH, 'https://journal.example/periph')
        texte = extraction['texte']
        self.assertNotIn('Temps de lecture', texte)
        self.assertNotIn('Partager', texte)
        self.assertNotIn('lire aussi', texte)
        self.assertNotIn('commentaires', texte)
        self.assertNotIn('Accueil', texte)
        exclus = ' '.join(e['texte'] for e in extraction['exclus'])
        for marque in ('Temps de lecture', 'Partager', 'lire aussi', 'commentaires', 'Articles similaires'):
            self.assertIn(marque, exclus)
        kinds = [b['kind'] for b in extraction['blocs']]
        self.assertEqual(kinds[0], 'titre')
        self.assertIn('citation', kinds)
        self.assertEqual(kinds.count('liste'), 2)
        lien = extraction['liens'][0]
        self.assertEqual(texte[lien['start']:lien['end']], 'la Ville de Paris')
        self.assertEqual(lien['href'], 'https://www.paris.fr/peripherique')
        citation = extraction['citations'][0]
        self.assertEqual(texte[citation['start']:citation['end']], '50 km/h')
        self.assertEqual(extraction['chapo'], 'Le boulevard périphérique sera fermé cet été.')

    def test_metadonnees_ordre_de_confiance(self):
        meta = m1_page.metadonnees(HTML_PERIPH, 'https://journal.example/periph')
        self.assertEqual(meta['publication'], '2026-05-03')          # JSON-LD avant la balise meta
        self.assertEqual(meta['mise_a_jour'], '2026-06-01')
        self.assertEqual(meta['auteur'], 'Jean Dupont')              # JSON-LD avant meta author
        self.assertEqual(meta['type_schema'], 'NewsArticle')
        self.assertEqual(meta['sources']['publication'], 'jsonld')
        sans_jsonld = HTML_PERIPH.split('<script')[0] + HTML_PERIPH.split('</script>')[1]
        meta = m1_page.metadonnees(sans_jsonld, 'https://journal.example/periph')
        self.assertEqual(meta['publication'], '2026-05-02')
        self.assertTrue(meta['sources']['publication'].startswith(('meta:', 'htmldate', 'trafilatura')))
        visible = '<html><body><article><h1>T</h1><p>Par Marie Durand, publié le 4 février 2026</p><p>Texte.</p></article></body></html>'
        meta = m1_page.metadonnees(visible, '')
        self.assertEqual((meta['publication'], meta['auteur'], meta['sources']['publication']), ('2026-02-04', 'Marie Durand', 'texte'))

    def test_acquerir_html_enregistre_versions(self):
        with patch.object(m1_page, '_trafilatura', return_value=(None, {})):
            version_id = m1_page.acquerir(url='https://journal.example/periph', html=HTML_PERIPH)
        version = s.query('SELECT * FROM versions WHERE id=?', (version_id,), True)
        self.assertEqual(version['published'], '2026-05-03')
        self.assertEqual(version['updated'], '2026-06-01')
        self.assertEqual(version['author'], 'Jean Dupont')
        self.assertEqual(version['page_type'], 'NewsArticle')
        self.assertEqual(version['lede'], 'Le boulevard périphérique sera fermé cet été.')
        self.assertEqual(len(version['simhash']), 16)
        meta = json.loads(version['meta'])
        self.assertEqual(meta['extracteur'], 'html.parser')
        self.assertEqual(meta['longueur'], len(version['text']))
        lien = meta['liens'][0]
        self.assertEqual(version['text'][lien['start']:lien['end']], 'la Ville de Paris')
        self.assertTrue(s.query('SELECT 1 FROM chunks WHERE version_id=? AND kind IS NULL', (version_id,), True))
        self.assertEqual(m1_page.acquerir(url='https://journal.example/periph', html=HTML_PERIPH), version_id)

    def test_acquerir_trafilatura_xml_si_present(self):
        xml = ('<doc title="T" author="A" date="2026-05-02"><main><head rend="h1">Titre</head>'
               '<p>Une phrase avec <ref target="https://source.example/x">un lien</ref>.</p>'
               '<quote><p>Une citation.</p></quote><list><item>Un</item><item>Deux</item></list></main></doc>')
        with patch.object(m1_page, '_trafilatura', return_value=(xml, {'author': 'A', 'date': '2026-05-02'})):
            version_id = m1_page.acquerir(url='https://journal.example/t', html='<html><body><p>x</p></body></html>')
        version = s.query('SELECT * FROM versions WHERE id=?', (version_id,), True)
        self.assertEqual(version['xml'], xml)
        meta = json.loads(version['meta'])
        self.assertEqual(meta['extracteur'], 'trafilatura')
        self.assertEqual([b['kind'] for b in meta['blocs']], ['titre', 'paragraphe', 'citation', 'liste', 'liste'])
        lien = meta['liens'][0]
        self.assertEqual(version['text'][lien['start']:lien['end']], 'un lien')
        self.assertEqual(version['author'], 'A')

    def test_acquerir_url_avec_garde_fous_du_serveur(self):
        with self.assertRaises(ValueError):
            m1_page.acquerir(url='http://127.0.0.1/page')
        with patch.object(m1_page, '_telecharger', return_value=(HTML_PERIPH, 'text/html')) as telechargement, \
                patch.object(m1_page, '_trafilatura', return_value=(None, {})):
            version_id = m1_page.acquerir(url='https://journal.example/periph')
        telechargement.assert_called_once()
        self.assertTrue(version_id)

    def test_acquerir_texte_brut_et_structure(self):
        version_id = m1_page.acquerir(texte=TEXTE_BRUT, titre='Crawl')
        version = s.query('SELECT * FROM versions WHERE id=?', (version_id,), True)
        self.assertEqual(version['published'], '2025-03-03')
        self.assertEqual(version['author'], 'P.C.')
        structure = m1_page.structurer_texte(TEXTE_BRUT)
        kinds = [(TEXTE_BRUT[b['start']:b['end']][:20], b['kind']) for b in structure['blocs']]
        self.assertEqual(kinds[0][1], 'titre')
        self.assertEqual(kinds[1][1], 'metadonnee')            # URL en tete
        self.assertEqual(kinds[2][1], 'metadonnee')            # « Publié le »
        self.assertIn(("LE CRAWL AUJOURD'HUI", 'titre'), kinds)
        self.assertIn(('1. Google Search Cen', 'note'), kinds)  # section Sources
        self.assertEqual(kinds[-1][1], 'metadonnee')           # signature
        self.assertEqual(structure['titre'], 'La chaîne de crawl : une carte complète')

    def test_simhash(self):
        a = m1_page.simhash('Le boulevard périphérique parisien sera fermé du 1er au 31 août 2026 pour travaux de rénovation.')
        b = m1_page.simhash('Le boulevard périphérique parisien sera fermé du 1er au 31 août 2026 pour des travaux de rénovation.')
        c = m1_page.simhash('La poule pond des œufs dans le poulailler chaque matin depuis des années, sans relâche.')
        self.assertLess(m1_page.distance_simhash(a, b), m1_page.distance_simhash(a, c))
        self.assertIsNone(m1_page.distance_simhash(a, None))


# ---------------------------------------------------------------------------
# Etape 2 — segmentation
# ---------------------------------------------------------------------------

class SegmentationTests(Base):

    def test_segmentation_structure_et_anciens_chunks(self):
        version_id = s.add_content('texte:crawl', 'Crawl', TEXTE_BRUT)
        anciens = s.query('SELECT id FROM chunks WHERE version_id=?', (version_id,))
        unite = self.unite(version_id, 'Il revient plus souvent sur les pages qui changent.')
        s.query("INSERT INTO occurrences VALUES('o1','c1',?,'x','[]')", (anciens[0]['id'],))
        resultat = m1_page.segmenter(version_id)
        self.assertGreater(resultat['chunks'], 5)
        self.assertEqual(resultat['unites_rattachees'], 1)
        for ancien in anciens:
            self.assertTrue(s.query('SELECT 1 FROM chunks WHERE id=? AND kind IS NULL', (ancien['id'],), True))
        texte = s.query('SELECT text FROM versions WHERE id=?', (version_id,), True)['text']
        nouveaux = s.query('SELECT * FROM chunks WHERE version_id=? AND kind IS NOT NULL ORDER BY start', (version_id,))
        for chunk in nouveaux:
            self.assertEqual(texte[chunk['start']:chunk['end']], chunk['text'])
            self.assertEqual(chunk['prepared'], 1)
            self.assertTrue(chunk['run_id'])
        paragraphe = next(c for c in nouveaux if c['text'].startswith('Googlebot'))
        contexte = json.loads(paragraphe['context'])
        self.assertEqual(paragraphe['kind'], 'paragraphe')
        self.assertEqual(paragraphe['section'], "LE CRAWL AUJOURD'HUI")
        self.assertEqual(contexte['titre'], 'Crawl')
        self.assertEqual(contexte['date'], '2025-03-03')
        self.assertEqual(len(contexte['phrases']), 3)
        self.assertIn("LE CRAWL AUJOURD'HUI", m1_page.contexte_texte(paragraphe))
        self.assertEqual(m1_page.phrases_du_chunk(paragraphe['id'])[1][2], 'Il revient plus souvent sur les pages qui changent.')
        note = next(c for c in nouveaux if c['text'].startswith('1. Google'))
        self.assertEqual((note['kind'], note['section']), ('note', 'SOURCES'))
        self.assertEqual(self.ligne(unite)['chunk_id'], paragraphe['id'])
        # pas de nouvel ancien chunk a extraire par l'ancien pipeline
        self.assertEqual(s.query('SELECT count(*) n FROM chunks WHERE prepared=0', one=True)['n'], len(anciens))
        # reprise : rien a refaire ; forcer : memes identifiants
        self.assertTrue(m1_page.segmenter(version_id)['deja'])
        m1_page.segmenter(version_id, forcer=True)
        apres = [c['id'] for c in s.query('SELECT id FROM chunks WHERE version_id=? AND kind IS NOT NULL ORDER BY start', (version_id,))]
        self.assertEqual(apres, [c['id'] for c in nouveaux])
        self.assertTrue(s.query("SELECT 1 FROM step_runs WHERE step='m1.segmentation' AND status='terminee'", one=True))
        # Unite arrivee apres la segmentation (migration tardive) : rattachee sans resegmenter.
        tardive = self.unite(version_id, 'Il revient plus souvent sur les pages qui changent.')
        resultat = m1_page.segmenter(version_id)
        self.assertTrue(resultat['deja'])
        self.assertEqual(resultat['unites_rattachees'], 1)
        self.assertEqual(self.ligne(tardive)['chunk_id'], paragraphe['id'])

    def test_paragraphe_long_recoupe_par_phrases(self):
        phrase = 'Cette phrase de test décrit un mécanisme du moteur de recherche avec assez de mots. '
        texte = 'Titre\n\n' + phrase * 120
        version_id = s.add_content('texte:long', 'Long', texte)
        with patch.dict(seuils.DEFAUTS, {'m1.segment.chunk_caracteres_max': 1000}):
            m1_page.segmenter(version_id)
        chunks = s.query('SELECT * FROM chunks WHERE version_id=? AND kind=?', (version_id, 'paragraphe'))
        self.assertGreater(len(chunks), 5)
        for chunk in chunks:
            self.assertLessEqual(len(chunk['text']), 1000)
            self.assertTrue(chunk['text'].endswith('.'))

    def test_contenu_html_garde_ses_blocs(self):
        with patch.object(m1_page, '_trafilatura', return_value=(None, {})):
            version_id = m1_page.acquerir(url='https://journal.example/periph', html=HTML_PERIPH)
        m1_page.segmenter(version_id)
        kinds = {c['kind'] for c in s.query('SELECT kind FROM chunks WHERE version_id=? AND kind IS NOT NULL', (version_id,))}
        self.assertTrue({'titre', 'paragraphe', 'citation', 'liste'} <= kinds)
        chunk = s.query("SELECT * FROM chunks WHERE version_id=? AND text LIKE 'Le boulevard périphérique parisien%'", (version_id,), True)
        self.assertEqual(chunk['section'], 'Les travaux')
        self.assertEqual(json.loads(chunk['context'])['date'], '2026-06-01')


# ---------------------------------------------------------------------------
# Etape 3 — contexte et ancrage
# ---------------------------------------------------------------------------

class ContexteTests(Base):

    def test_dates_resolues_par_rapport_a_la_publication(self):
        version_id = s.add_content('texte:crawl', 'Crawl', TEXTE_BRUT)
        m1_page.segmenter(version_id)
        m1_page.resoudre_contexte(version_id)
        chunk = s.query("SELECT * FROM chunks WHERE version_id=? AND text LIKE 'L''an dernier%'", (version_id,), True)
        contexte = json.loads(chunk['context'])
        self.assertEqual(contexte['reference'], '2025-03-03')
        expression = contexte['dates'][0]
        self.assertEqual((expression['debut'], expression['fin'], expression['nature']), ('2024-01-01', '2024-12-31', 'relative'))
        texte = s.query('SELECT text FROM versions WHERE id=?', (version_id,), True)['text']
        self.assertEqual(texte[expression['start']:expression['end']], "L'an dernier")

    def test_coreference_regle_sans_spacy(self):
        version_id = s.add_content('texte:co', 'Co', 'Titre\n\nLa société Acme dirige le chantier. Elle emploie 200 personnes.')
        with patch.object(moteurs, 'analyse', return_value=None), patch.object(m1_page, '_coreferences_coreferee', return_value=None):
            m1_page.segmenter(version_id)
            resultat = m1_page.resoudre_contexte(version_id)
        self.assertEqual(resultat['moteur_coreferences'], 'regle_sujet:heuristique')
        chunk = s.query("SELECT context FROM chunks WHERE version_id=? AND kind='paragraphe'", (version_id,), True)
        coreference = json.loads(chunk['context'])['coreferences'][0]
        self.assertEqual((coreference['texte'], coreference['antecedent']), ('Elle', 'La société Acme'))

    def test_coreference_spacy_si_installe(self):
        if moteurs.etat()['analyse']['actif'] == 'aucun':
            self.skipTest('spaCy francais absent')
        version_id = s.add_content('texte:co', 'Co', 'Titre\n\nLe ministre a présenté le budget hier. Il prévoit une hausse des dépenses.')
        with patch.object(m1_page, '_coreferences_coreferee', return_value=None):
            m1_page.segmenter(version_id)
            resultat = m1_page.resoudre_contexte(version_id)
        self.assertTrue(resultat['moteur_coreferences'].startswith('regle_sujet:spacy'))
        chunk = s.query("SELECT context FROM chunks WHERE version_id=? AND kind='paragraphe'", (version_id,), True)
        coreferences = json.loads(chunk['context'])['coreferences']
        self.assertEqual([(c['texte'], c['antecedent']) for c in coreferences], [('Il', 'Le ministre')])

    def test_ordre_d_ancrage(self):
        texte = ('Titre\n\nLe pont a été inauguré le 4 mai 2019. Il y a trois mois, la mairie a repeint le pont. '
                 'Le pont mesure 200 mètres.')
        version_id = s.add_content('https://site.example/pont', 'Pont', texte, '2026-02-10')
        phrase = self.unite(version_id, 'Le pont a été inauguré le 4 mai 2019.')
        relative = self.unite(version_id, 'la mairie a repeint le pont')
        publication = self.unite(version_id, 'Le pont mesure 200 mètres.')
        texte_unite = self.unite(version_id, 'Le pont mesure 200 mètres.', texte='En 2025, le pont mesure 200 mètres.')
        m1_page.segmenter(version_id)
        repartition = m1_page.ancrer()
        self.assertEqual((self.ligne(phrase)['anchor_date'], self.ligne(phrase)['anchor_kind']), ('2019-05-04', 'phrase'))
        self.assertEqual((self.ligne(relative)['anchor_date'], self.ligne(relative)['anchor_kind']), ('2025-11-10', 'relative'))
        self.assertEqual((self.ligne(publication)['anchor_date'], self.ligne(publication)['anchor_kind']), ('2026-02-10', 'publication'))
        self.assertEqual(self.ligne(texte_unite)['anchor_kind'], 'phrase')
        self.assertEqual(repartition['total'], 4)
        s.query("UPDATE versions SET updated='2026-06-01' WHERE id=?", (version_id,))
        m1_page.ancrer([publication])
        self.assertEqual((self.ligne(publication)['anchor_date'], self.ligne(publication)['anchor_kind']), ('2026-06-01', 'maj'))
        sans_date = s.add_content('texte:sd', 'SD', 'Le chat dort.')
        inconnue = self.unite(sans_date, 'Le chat dort.')
        m1_page.ancrer([inconnue])
        self.assertEqual((self.ligne(inconnue)['anchor_date'], self.ligne(inconnue)['anchor_kind']), (None, 'inconnue'))


# ---------------------------------------------------------------------------
# Etape 6 — classes d'obsolescence et peremption
# ---------------------------------------------------------------------------

class ObsolescenceTests(Base):

    TEXTE = ('Titre\n\n'
             'Le boulevard périphérique parisien sera fermé du 1er au 31 août 2026 pour travaux.\n\n'
             'Le PDG de X est A.\n\n'
             "Selon l'Insee, le taux de chômage s'élève à 7,4 % de la population active.\n\n"
             'La poule pond des œufs.\n\n'
             'Google a lancé la mise à jour Penguin le 24 avril 2012.\n\n'
             'Temps de lecture : 5 minutes.\n\n'
             'La nouvelle ligne de tramway ouvrira en 2030.\n\n'
             'Le moteur indexe désormais les pages en continu.')

    def preparer(self):
        version_id = s.add_content('https://site.example/o', 'O', self.TEXTE, '2026-05-02')
        ids = {
            'periph': self.unite(version_id, 'Le boulevard périphérique parisien sera fermé du 1er au 31 août 2026 pour travaux.'),
            'pdg': self.unite(version_id, 'Le PDG de X est A.'),
            'insee': self.unite(version_id, "Selon l'Insee, le taux de chômage s'élève à 7,4 % de la population active."),
            'poule': self.unite(version_id, 'La poule pond des œufs.'),
            'penguin': self.unite(version_id, 'Google a lancé la mise à jour Penguin le 24 avril 2012.'),
            'usage': self.unite(version_id, 'Temps de lecture : 5 minutes.'),
            'tram': self.unite(version_id, 'La nouvelle ligne de tramway ouvrira en 2030.'),
            'desormais': self.unite(version_id, 'Le moteur indexe désormais les pages en continu.'),
        }
        return version_id, ids

    def test_exemples_du_document(self):
        version_id, ids = self.preparer()
        resultat = m1_page.executer([version_id], aujourdhui=AUJOURDHUI)
        classes = {nom: self.ligne(uid)['obs_class'] for nom, uid in ids.items()}
        self.assertEqual(classes['periph'], 'annonce')
        self.assertEqual(classes['pdg'], 'etat_courant')
        self.assertEqual(classes['insee'], 'etat_courant')
        self.assertEqual(classes['poule'], 'immuable')
        self.assertEqual(classes['penguin'], 'immuable')
        self.assertEqual(classes['usage'], 'usage')
        self.assertEqual(classes['tram'], 'annonce')
        self.assertEqual(classes['desormais'], 'etat_courant')
        periph = self.ligne(ids['periph'])
        self.assertEqual(periph['expires'], '2026-08-31')
        self.assertEqual(periph['anchor_date'], '2026-08-01')
        self.assertEqual(periph['temporal_status'], 'perimee_par_date')   # perimee au 13 novembre 2026
        tram = self.ligne(ids['tram'])
        self.assertEqual((tram['expires'], tram['temporal_status']), ('2030-12-31', 'a_jour'))
        self.assertEqual(self.ligne(ids['usage'])['excluded'], 1)
        self.assertEqual(self.ligne(ids['pdg'])['temporal_status'], 'a_jour')
        self.assertTrue(all(self.ligne(uid)['obs_rule'] for uid in ids.values()))
        self.assertEqual(resultat['peremption']['total_perimees'], 1)
        etapes = {r['step'] for r in s.query("SELECT step FROM step_runs WHERE status='terminee'")}
        self.assertTrue({'m1.segmentation', 'm1.contexte', 'm1.ancrage', 'm1.obsolescence', 'm1.peremption'} <= etapes)
        # reprise : rien n'est refait
        relance = m1_page.executer([version_id], aujourdhui=AUJOURDHUI)
        self.assertEqual(relance['segmentation']['deja'], 1)
        self.assertEqual(relance['ancrage']['total'], 0)
        self.assertEqual(relance['obsolescence']['total'], 0)

    def test_perimer_depend_de_la_date_du_jour(self):
        version_id, ids = self.preparer()
        m1_page.executer([version_id], aujourdhui='2026-07-01')
        self.assertEqual(self.ligne(ids['periph'])['temporal_status'], 'a_jour')
        resultat = m1_page.perimer('2026-09-01')
        self.assertEqual(resultat['perimees'], 1)
        self.assertEqual(self.ligne(ids['periph'])['temporal_status'], 'perimee_par_date')
        resultat = m1_page.perimer('2026-08-15')
        self.assertEqual(resultat['retablies'], 1)
        s.query("UPDATE units SET temporal_status='remise_en_question' WHERE id=?", (ids['periph'],))
        m1_page.perimer(AUJOURDHUI)
        self.assertEqual(self.ligne(ids['periph'])['temporal_status'], 'remise_en_question')

    def test_classement_par_predicat(self):
        version_id = s.add_content('https://site.example/p', 'P', 'Titre\n\nLe PDG de X est A. Le salon se tient à Lyon le 3 mars 2027.', '2026-05-02')
        dirigeant = self.unite(version_id, 'Le PDG de X est A.')
        salon = self.unite(version_id, 'Le salon se tient à Lyon le 3 mars 2027.')
        s.query("INSERT INTO facts(id,unit_id,subject,predicate,object,object_norm) VALUES('f1',?,'X','a_pour_dirigeant','A','A')", (dirigeant,))
        s.query("INSERT INTO facts(id,unit_id,subject,predicate,object,object_norm) VALUES('f2',?,'salon','aura_lieu_le','3 mars 2027','2027-03-03')", (salon,))
        repartition = m1_page.classer_obsolescence([dirigeant, salon], aujourdhui=AUJOURDHUI)
        self.assertEqual(self.ligne(dirigeant)['obs_class'], 'etat_courant')
        self.assertTrue(self.ligne(dirigeant)['obs_rule'].startswith('predicat:a_pour_dirigeant'))
        self.assertEqual((self.ligne(salon)['obs_class'], self.ligne(salon)['expires']), ('annonce', '2027-03-03'))
        self.assertEqual(repartition['origine']['predicat'], 2)

    def test_table_de_secours_si_predicats_absents(self):
        with patch.dict(sys.modules, {'chaine.predicats': None}):
            table, origine = m1_page._predicats()
        self.assertEqual(origine, 'secours')
        self.assertEqual(table['a_pour_dirigeant']['classe'], 'etat_courant')

    def test_classifieur_pour_les_cas_non_couverts(self):
        version_id = s.add_content('texte:c', 'C', 'Titre\n\nGoogle range ces pages dans un autre index.')
        unite = self.unite(version_id, 'Google range ces pages dans un autre index.')
        appels = []

        def faux_classer(textes, etiquettes, exemples):
            appels.append(textes)
            return [{'label': 'immuable', 'score': 0.9, 'engine': 'setfit:test', 'scores': {}} for _ in textes]
        with patch.object(moteurs, 'classer', side_effect=faux_classer):
            m1_page.classer_obsolescence([unite], aujourdhui=AUJOURDHUI)
        self.assertEqual(len(appels), 1)
        self.assertIn('indicetemps', appels[0][0])
        ligne = self.ligne(unite)
        self.assertEqual(ligne['obs_class'], 'immuable')
        self.assertTrue(ligne['obs_rule'].startswith('classifieur:setfit:test'))
        # une « annonce » sans futur, prevision ni date a venir est refusee : seconde etiquette
        sortie = [{'label': 'annonce', 'score': 0.9, 'engine': 'setfit:test',
                   'scores': {'annonce': 0.9, 'etat_courant': 0.7, 'immuable': 0.1}}]
        with patch.object(moteurs, 'classer', return_value=sortie):
            m1_page.classer_obsolescence([unite], aujourdhui=AUJOURDHUI)
        self.assertEqual(self.ligne(unite)['obs_class'], 'etat_courant')
        self.assertIsNone(self.ligne(unite)['expires'])


if __name__ == '__main__':
    unittest.main()
