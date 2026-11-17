"""Tests du lot integration : pipeline de bout en bout, routes HTTP /api/chaine/*, reprise, arrets, journal.

LLM (``server.llm_request``), NLI (``moteurs.nli_lot``), embeddings (``moteurs.embeddings``), reranker,
perplexite et Ibou (``m5._http``, ``m5._telecharger``, ``m5._solde``) sont simules ; aucun appel reseau.
Les paquets tiers sont masques (``moteurs.paquet_present`` -> False) pour que les regles soient deterministes.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s
import chaine
from chaine import m4
from chaine import m5
from chaine import moteurs
from chaine import noyau
from chaine import pipeline
from chaine import predicats
from chaine import seuils

JOUR = '2026-11-13'
SITE = 'site-audite.fr'
PERIPH = ('La voie gauche du périphérique parisien sera immobilisée du 1er au 31 août 2026 entre la porte Maillot '
          'et la porte de la Chapelle pour des travaux de rénovation.')
PERIPH_PASSE = ('La voie gauche du périphérique parisien a été immobilisée du 1er au 31 août 2026 entre la porte Maillot '
                'et la porte de la Chapelle pour des travaux de rénovation.')
PDG = 'Le PDG de X est B.'
CONTENUS_IBOU = {
    'https://a.fr/1': 'Actualité économique.\n\nDepuis octobre 2026, X est dirigé par Claire Martin, nommée par le conseil.',
    'https://b.fr/1': 'Gouvernance.\n\nClaire Martin a pris la tête de X en octobre 2026, succédant à B.',
}
DATES_IBOU = {'https://a.fr/1': '2026-10-05', 'https://b.fr/1': '2026-10-20'}
MOTS_VIDES = {'le', 'la', 'les', 'de', 'du', 'des', 'et', 'a', 'au', 'en', 'un', 'une', 'pour', 'par', 'est', 'sera', 'l', 'd'}


def plat(texte):
    """Minuscules sans accents."""
    decompose = unicodedata.normalize('NFD', str(texte or '').casefold())
    return ''.join(c for c in decompose if not unicodedata.combining(c))


def mots(texte):
    """Mots en forme plate."""
    return re.findall(r'[a-z0-9]+', plat(texte))


def cles(texte):
    """Mots porteurs : majuscule hors debut de phrase, ou chiffres (forme plate)."""
    jetons = re.findall(r"[\wÀ-ÿ]+", str(texte or ''))
    return {plat(j) for i, j in enumerate(jetons) if (i > 0 and j[:1].isupper()) or any(c.isdigit() for c in j)}


def nli_simule(paires):
    """NLI deterministe : contradiction B / Claire Martin, implication par mots porteurs ou recouvrement."""
    sortie = []
    for premisse, hypothese in paires:
        p_mots = set(mots(premisse))
        h_mots = set(mots(hypothese))
        if 'claire' in p_mots and 'b' in h_mots and 'claire' not in h_mots:
            label = 'contradiction'
        else:
            porteurs = cles(hypothese)
            if porteurs:
                label = 'implication' if porteurs <= p_mots else 'neutre'
            else:
                pleins = h_mots - MOTS_VIDES
                commun = len(pleins & p_mots) / len(pleins) if pleins else 0.0
                label = 'implication' if commun >= 0.6 else 'neutre'
        probas = {'implication': 0.02, 'contradiction': 0.02, 'neutre': 0.02}
        probas[label] = 0.96
        sortie.append({'label': label, 'score': 0.96, 'probas': probas, 'engine': 'test:nli'})
    return sortie


def embeddings_simules(textes):
    """Sac de mots haché, normalisé (64 dimensions)."""
    vecteurs = []
    for texte in textes:
        vecteur = [0.0] * 64
        for mot in mots(texte):
            if mot in MOTS_VIDES:
                continue
            vecteur[sum(ord(c) for c in mot) % 64] += 1.0
        norme = sum(v * v for v in vecteur) ** 0.5 or 1.0
        vecteurs.append([v / norme for v in vecteur])
    return vecteurs, 'test:embeddings'


def remplir(definition):
    """Instance minimale conforme a un schema JSON strict."""
    if 'enum' in definition:
        return definition['enum'][0]
    nature = definition.get('type')
    if nature == 'object':
        return {cle: remplir(sous) for cle, sous in definition.get('properties', {}).items()}
    if nature == 'array':
        return []
    if nature == 'boolean':
        return False
    if nature in ('integer', 'number'):
        return 0
    return ''


def faits_simules(payload):
    """Sortie du LLM pour ``chaine:faits`` : faits du PDG et du peripherique, citations exactes."""
    reponses = []
    for unite in payload['unites']:
        texte = unite['texte']
        faits = []
        inconnues = []
        if 'PDG de X' in texte:
            faits.append({'sujet': 'X', 'predicat': 'a_pour_dirigeant', 'objet': 'B', 'portee': '', 'periode': '',
                          'origine': 'explicite', 'citation': 'PDG de X est B'})
        if 'voie gauche du périphérique' in texte:
            faits.append({'sujet': 'voie gauche', 'predicat': 'fait_partie_de', 'objet': 'périphérique parisien', 'portee': '',
                          'periode': '', 'origine': 'explicite', 'citation': 'La voie gauche du périphérique parisien'})
            faits.append({'sujet': 'voie gauche', 'predicat': 'sera_ferme', 'objet': 'du 1er au 31 août 2026',
                          'portee': 'périphérique parisien', 'periode': 'août 2026', 'origine': 'explicite',
                          'citation': 'sera immobilisée du 1er au 31 août 2026'})
            faits.append({'sujet': 'voie gauche', 'predicat': 'date_de_fin', 'objet': '31 août 2026', 'portee': '',
                          'periode': 'août 2026', 'origine': 'explicite', 'citation': 'au 31 août 2026'})
            inconnues.append({'sujet': 'périphérique parisien', 'predicat': 'sera_ferme', 'question': 'Quelles voies concernées ?'})
            inconnues.append({'sujet': 'périphérique parisien', 'predicat': 'date_de_fin', 'question': 'Quelles dates précises ?'})
        reponses.append({'unite': unite['id'], 'faits': faits, 'inconnues': inconnues})
    return {'unites': reponses}


def llm_simule(tache, payload):
    """LLM simule, par mode de sortie structuree."""
    mode = payload.get('mode')
    if mode == 'chaine:faits':
        return faits_simules(payload)
    if mode == 'chaine:m3_cadre':
        return {'meme_cadre': 'oui', 'plus_recente': 'indeterminable'}
    if mode == 'chaine:m5_selection':
        label = nli_simule([(payload['passage'], payload['fait'])])[0]['label']
        return {'position': {'implication': 'soutient', 'contradiction': 'contredit'}.get(label, 'hors_sujet'),
                'citation': payload['passage'], 'motif': 'Selection du contenu simulee pour le test.'}
    if mode == 'chaine:m5_valeur':
        passage = json.dumps(payload, ensure_ascii=False)
        if 'Claire Martin' in passage:
            return {'trouve': True, 'valeur': 'Claire Martin', 'citation': 'Claire Martin'}
        return {'trouve': False, 'valeur': '', 'citation': ''}
    if mode == 'chaine:m6.reecriture':
        if 'immobilisée' in json.dumps(payload, ensure_ascii=False):
            return {'phrase': PERIPH_PASSE}
    if mode and mode.startswith('chaine:'):
        return remplir(noyau.schema_pour(payload))
    return {'ok': True}


def requete_llm_simulee(task, payload, provider, model, meta):
    """Remplace ``server.llm_request`` : ``server.llm`` journalise donc vraiment chaque appel dans ``api_log``."""
    meta.update(tokens_in=100, tokens_out=10, text='{}')
    return llm_simule(task, payload)


def page_html(texte, publiee):
    """Page HTML simulee, datee en JSON-LD."""
    ld = '<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"' + publiee + '"}</script>'
    corps = ''.join('<p>' + p + '</p>' for p in texte.split('\n\n'))
    return '<html><head><title>Page</title>' + ld + '</head><body>' + corps + '</body></html>'


def reponse_ibou(*args, **kwargs):
    """Reponse /passage simulee (toujours les deux memes pages)."""
    corps = {'results': [{'url': url, 'title': 'Titre ' + url, 'passages': [{'text': texte}]}
                         for url, texte in CONTENUS_IBOU.items()]}
    return 200, json.dumps(corps).encode(), {'X-Units-Remaining': '900'}


class Base(unittest.TestCase):
    """Base temporaire, chaine branchee par server.init(), moteurs et services simules."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        s.DB = Path(self.tmp.name) / 'test.sqlite3'
        s.STOP.clear()
        self.config = dict(s.CONFIG)
        s.CONFIG.update(embeddings=True, embedding_model='test-embeddings', ibou_key='test-only', model='test-model',
                        provider='lmstudio', openai_key='', lm_key='')
        chaine.brancher(s)
        s.init()
        moteurs.reinitialiser()
        noyau.vider_reglages()
        seuils._vider_cache()
        s.CTX.step = None
        s.CTX.claim_id = None
        s.JOB.clear()
        s.JOB.update(status='idle', message='', done=0, total=0)
        m5._ETAT['pages'].clear()
        m5._ETAT['site'] = {'cle': None, 'empreintes': {}}
        m5._GSC_CLICS['cle'] = None
        m4._GSC['cle'] = None
        copie = Path(self.tmp.name) / 'predicats.json'
        shutil.copy(predicats.CHEMIN, copie)
        self.environ = patch.dict(os.environ, {'FC_PREDICATS': str(copie)})
        self.environ.start()
        predicats.recharger()
        self.pile = ExitStack()
        self.pile.enter_context(patch.object(moteurs, 'paquet_present', lambda paquet: False))
        self.pile.enter_context(patch.object(moteurs, 'nli_lot', side_effect=nli_simule))
        # Configuration simulee declaree : un NLI local distinct du LLM (double passe forte, relecture M1).
        self.pile.enter_context(patch.object(moteurs, 'nli_prevu', return_value='test:nli'))
        self.pile.enter_context(patch.object(moteurs, 'embeddings', side_effect=embeddings_simules))
        self.pile.enter_context(patch.object(moteurs, 'reordonner',
                                             side_effect=lambda q, ps, avec_moteur=False: ([0.9] * len(ps), 'test') if avec_moteur else [0.9] * len(ps)))
        self.pile.enter_context(patch.object(moteurs, 'perplexite', return_value=None))
        self.pile.enter_context(patch.object(s, 'llm_request', side_effect=requete_llm_simulee))
        self.pile.enter_context(patch.object(s, 'embed', side_effect=AssertionError('S.embed ne doit pas etre appele')))
        self.http = self.pile.enter_context(patch.object(m5, '_http', side_effect=reponse_ibou))
        self.pile.enter_context(patch.object(m5, '_telecharger',
                                             side_effect=lambda url: page_html(CONTENUS_IBOU[url], DATES_IBOU[url])))
        self.pile.enter_context(patch.object(m5, '_solde', return_value=900))
        self.pile.enter_context(patch.object(m5, '_attendre', return_value=None))
        moteurs.reinitialiser()
        noyau.definir_reglage('site:domaines', json.dumps([SITE]))
        noyau.definir_reglage('m1:unites:option', 'A')

    def tearDown(self):
        self.pile.close()
        self.environ.stop()
        predicats.recharger()
        s.CONFIG.clear()
        s.CONFIG.update(self.config)
        s.STOP.clear()
        s.JOB.clear()
        s.JOB.update(status='idle', message='', done=0, total=0)
        moteurs.reinitialiser()
        noyau.vider_reglages()
        seuils._vider_cache()
        self.tmp.cleanup()

    # --- corpus -------------------------------------------------------------
    def corpus(self):
        """Trois pages : le peripherique (exemple du document), « PDG de X » et sa reprise sur une autre page."""
        self.v_periph = s.add_content('https://' + SITE + '/peripherique', 'Travaux sur le périphérique',
                                      'Travaux d’été\n\n' + PERIPH + '\n\nLa circulation reste possible sur les autres voies.',
                                      '2026-05-02')
        self.v_pdg = s.add_content('https://' + SITE + '/x', 'Gouvernance de X',
                                   'Gouvernance\n\n' + PDG + '\n\nX emploie des ingénieurs à Lyon.', '2025-03-01')
        self.v_reprise = s.add_content('https://' + SITE + '/marche', 'Le marché en bref',
                                       'Le marché\n\nSelon le rapport annuel, le secteur progresse.\n\n' + PDG, '2025-06-01')
        return [self.v_periph, self.v_pdg, self.v_reprise]

    def unite_de(self, version_id, fragment):
        """Unite d'une version dont le texte contient ``fragment``."""
        return s.query('SELECT * FROM units WHERE version_id=? AND text LIKE ?', (version_id, '%' + fragment + '%'), True)

    def journal_sans_etape(self):
        """Lignes api_log ecrites hors de toute etape de la chaine."""
        return s.query('SELECT service, operation FROM api_log WHERE step IS NULL')


class PipelineTests(Base):

    def test_bout_en_bout_jusqu_a_une_decision_puis_m1_sur_la_nouvelle_version(self):
        self.corpus()
        seuils.definir('m5.critique', 0.0)   # tout est critique : M5 verifie tout de suite dans ``tout``
        resultat = pipeline.tout(budget=60, aujourdhui=JOUR)
        self.assertTrue(resultat['ok'], resultat)
        etapes = {e['etape']: e for e in resultat['etapes']}
        self.assertTrue(etapes['migration'].get('saute'))       # aucune occurrence ancienne
        for nom in ('m1.page', 'm1.unites', 'm1.faits', 'm1.sujets', 'm2', 'm3', 'm4', 'm5', 'm6'):
            self.assertTrue(etapes[nom]['ok'], (nom, etapes[nom]))
        # M1 : unites et faits, peripherique perime par date, PDG en etat courant.
        periph = self.unite_de(self.v_periph, 'voie gauche')
        pdg = self.unite_de(self.v_pdg, 'PDG de X')
        reprise = self.unite_de(self.v_reprise, 'PDG de X')
        self.assertEqual(periph['temporal_status'], 'perimee_par_date')
        self.assertEqual(pdg['obs_class'], 'etat_courant')
        faits = s.query('SELECT predicate, object, status FROM facts WHERE unit_id=?', (pdg['id'],))
        self.assertIn(('a_pour_dirigeant', 'B'), {(f['predicate'], f['object']) for f in faits})
        # Deduplication et paternite : la reprise est dans le groupe du PDG.
        groupe = s.query('SELECT group_id FROM unit_group_members WHERE unit_id=?', (pdg['id'],), True)['group_id']
        self.assertTrue(s.query('SELECT 1 x FROM unit_group_members WHERE group_id=? AND unit_id=?', (groupe, reprise['id']), True))
        paternite = s.query('SELECT * FROM paternity WHERE group_id=?', (groupe,), True)
        self.assertEqual(paternite['parent_unit_id'], pdg['id'])
        # M4 : requete par cadre, question ouverte qui ne contient pas la valeur ; jamais l'affirmation telle quelle.
        ouvertes = s.query("SELECT q.* FROM queries q JOIN query_units qu ON qu.query_id=q.id WHERE qu.unit_id=? AND q.kind='ouverte'",
                           (pdg['id'],))
        self.assertTrue(ouvertes)
        self.assertNotIn(' B ', ' ' + ouvertes[0]['text'] + ' ')
        self.assertFalse(s.query("SELECT 1 x FROM queries WHERE template LIKE '%unite:fermee'", one=True))
        requete_periph = s.query("SELECT q.text FROM queries q JOIN query_units qu ON qu.query_id=q.id WHERE qu.unit_id=? "
                                 "AND q.kind='ouverte'", (periph['id'],), True)
        self.assertIn('août 2026', requete_periph['text'])
        self.assertIn('a-t-il été', requete_periph['text'])
        # M5 : verdict corrige, plafond respecte, couts journalises.
        pdg = s.query('SELECT * FROM units WHERE id=?', (pdg['id'],), True)
        self.assertEqual(pdg['verdict'], 'corrigee')
        utilise = s.query('SELECT sum(used) n FROM budgets', one=True)['n']
        self.assertLessEqual(utilise, 60)
        self.assertEqual(utilise, s.query("SELECT sum(units) n FROM ibou_calls WHERE status='success'", one=True)['n'])
        # api_log.step : chaque appel LLM et Ibou de la chaine est rattache a son etape.
        self.assertEqual(self.journal_sans_etape(), [])
        etapes_llm = {r['step'] for r in s.query("SELECT r.step FROM api_log a JOIN step_runs r ON r.id=a.step WHERE a.service='lmstudio'")}
        self.assertIn('m1.faits', etapes_llm)
        cout = s.query("SELECT sum(tokens_in) t, sum(ibou_units) u FROM step_runs", one=True)
        self.assertGreater(cout['t'], 0)
        self.assertEqual(cout['u'], utilise)
        verification = s.query("SELECT * FROM step_runs WHERE step='m5.verification'", one=True)
        self.assertEqual(verification['ibou_units'], utilise)
        # M6 : proposition de substitution sur le parent, decidee avec propagation a la reprise.
        proposition = s.query("SELECT * FROM proposals WHERE unit_id=? AND kind='corrigee'", (pdg['id'],), True)
        self.assertEqual(proposition['status'], 'proposee')
        self.assertIn('Claire Martin', proposition['after'])
        from chaine import m6
        decision = m6.decider(proposition['id'], 'accepter', propager=True)
        self.assertTrue(decision['ok'])
        nouvelle = decision['version_id']
        self.assertIn('Claire Martin', s.query('SELECT text FROM versions WHERE id=?', (nouvelle,), True)['text'])
        self.assertEqual(len(decision['propagees']), 1)
        # La nouvelle version n'a pas d'unites : relancer la chaine la decompose (M1 sur la nouvelle version).
        self.assertIn(nouvelle, pipeline.versions_sans_unites())
        suite = pipeline.tout(budget=0, aujourdhui=JOUR)
        self.assertTrue(suite['ok'], suite)
        self.assertTrue(self.unite_de(nouvelle, 'Claire Martin'))
        self.assertEqual(pipeline.versions_sans_unites(), [])
        self.assertEqual(s.query("SELECT count(*) n FROM chunks WHERE version_id=? AND kind IS NULL AND prepared=0",
                                 (nouvelle,), True)['n'], 0)
        etat = pipeline.etat()
        self.assertTrue(etat['compteurs']['m6']['par_statut'].get('appliquee'))
        self.assertEqual(etat['reste']['m1.unites']['a_faire'], 0)

    def test_reprise_apres_interruption(self):
        self.corpus()
        original = pipeline.FONCTIONS['m2']

        def m2_interrompue(params):
            s.STOP.set()
            return original(params)
        with patch.dict(pipeline.FONCTIONS, {'m2': m2_interrompue}):
            with self.assertRaises(s.OperationalError):
                pipeline.tout(budget=0, aujourdhui=JOUR)
        self.assertEqual(s.query("SELECT status FROM step_runs WHERE step='m2'", one=True)['status'], 'interrompue')
        unites = s.query('SELECT count(*) n FROM units', one=True)['n']
        faits = s.query('SELECT count(*) n FROM facts', one=True)['n']
        self.assertGreater(unites, 0)
        s.STOP.clear()
        resultat = pipeline.tout(budget=0, aujourdhui=JOUR)
        self.assertTrue(resultat['ok'], resultat)
        etapes = {e['etape']: e for e in resultat['etapes']}
        self.assertTrue(etapes['m1.unites'].get('saute'))
        self.assertTrue(etapes['m1.faits'].get('saute'))
        self.assertEqual(s.query('SELECT count(*) n FROM units', one=True)['n'], unites)
        self.assertEqual(s.query('SELECT count(*) n FROM facts', one=True)['n'], faits)
        self.assertEqual(s.query('SELECT count(*) n FROM units WHERE sourcing IS NULL AND COALESCE(excluded,0)=0', one=True)['n'], 0)
        self.assertTrue(etapes['m5'].get('saute'))

    def test_arret_propre_sans_nli(self):
        self.corpus()
        sans_nli = dict(moteurs.etat())
        sans_nli['nli'] = dict(sans_nli['nli'], actif='aucun', disponible=[])
        with patch.object(pipeline, '_etat_moteurs', return_value=sans_nli):
            resultat = pipeline.tout(budget=10, aujourdhui=JOUR)
            self.assertFalse(resultat['ok'])
            self.assertEqual(resultat['arret'], 'm1.faits')
            self.assertIn('NLI', resultat['raison'])
            self.assertEqual(s.query('SELECT count(*) n FROM facts', one=True)['n'], 0)
            self.assertGreater(s.query('SELECT count(*) n FROM units', one=True)['n'], 0)
            # Par la tache du serveur : pause motivee, historique job_runs.
            fini = s.start_job('chaine', lambda: pipeline.executer_tache('tout', budget=10, aujourdhui=JOUR))
            self.assertTrue(fini.wait(20))
        self.assertEqual(s.JOB['status'], 'paused')
        self.assertIn('NLI', s.JOB['message'])
        run = s.query("SELECT * FROM job_runs WHERE kind='chaine'", one=True)
        self.assertEqual(run['status'], 'paused')
        self.assertIn('NLI', run['message'])

    def test_sans_llm_ni_moteurs_les_etapes_de_regles_tournent(self):
        """Comme l'essai reel : sans LLM ni NLI ni embeddings, M1 page, dedoublonnage exact, M2, M3 3-5, M4, criticite."""
        self.corpus()
        s.CONFIG.update(model='', embeddings=False)
        self.assertIn('extracteur', pipeline.lancer('m1.faits')['raison'])
        self.assertTrue(pipeline.lancer('m1.page', aujourdhui=JOUR)['ok'])
        self.assertTrue(pipeline.lancer('m1.unites', aujourdhui=JOUR)['ok'])
        sujets = pipeline.lancer('m1.sujets')
        self.assertFalse(sujets['ok'])
        self.assertTrue(sujets['partiel'])
        self.assertIn('embeddings', sujets['raison'])
        self.assertEqual(pipeline.reste()['m1.sujets']['unites_sans_groupe'], 0)
        self.assertTrue(pipeline.lancer('m2', aujourdhui=JOUR)['ok'])
        m3_partiel = pipeline.lancer('m3', aujourdhui=JOUR)
        self.assertTrue(m3_partiel['partiel'])
        self.assertTrue(s.query('SELECT 1 x FROM paternity', one=True))
        self.assertTrue(pipeline.lancer('m4', aujourdhui=JOUR)['ok'])
        self.assertTrue(pipeline.lancer('m5.criticite', aujourdhui=JOUR)['ok'])
        self.assertIn('budget', pipeline.lancer('m5', aujourdhui=JOUR)['raison'])
        self.assertEqual(self.http.call_count, 0)

    def test_requete_de_repli_jamais_envoyee_et_unites_en_attente(self):
        # Ancien parcours : une affirmation et son occurrence, puis migration (unite sans fait).
        version = s.add_content('https://' + SITE + '/ancien', 'Ancien', 'Google utilise plus de 100 signaux bruts.', '2025-01-01')
        chunk = s.query('SELECT id FROM chunks WHERE version_id=?', (version,), True)['id']
        s.query("INSERT INTO claims(id,text,period,status,created) VALUES('c1','Google utilise plus de 100 signaux bruts.','','pending',?)",
                (s.now(),))
        s.query("INSERT INTO occurrences(id,claim_id,chunk_id,quote,sources) VALUES('o1','c1',?,'Google utilise plus de 100 signaux bruts.','[]')",
                (chunk,))
        self.assertEqual(pipeline.reste()['migration']['a_faire'], 1)
        self.assertTrue(pipeline.lancer('migration')['ok'])
        self.assertTrue(pipeline.est_migre())
        self.assertEqual(pipeline.reste()['migration']['a_faire'], 0)
        pipeline.lancer('m1.page', aujourdhui=JOUR)
        s.query("UPDATE units SET obs_class='etat_courant', status='a_verifier', critical=1")
        resultat = pipeline.lancer('m4', aujourdhui=JOUR)
        self.assertEqual(resultat['resultat']['en_attente_faits'], 1)
        self.assertEqual(pipeline.reste()['m4']['en_attente_decomposition'], 1)
        self.assertFalse(s.query("SELECT 1 x FROM queries WHERE status IN ('prete','a_revoir')", one=True))
        # Une requete de repli ecrite a la main (ou heritee) n'est jamais envoyee par M5.
        s.query("INSERT INTO queries(id,group_id,text,kind,template,frame,status,critical,priority) VALUES('qr','c1',"
                "'Est-ce que Google utilise plus de 100 signaux bruts ?','ouverte','unite:fermee','{}','prete',1,1)")
        s.query("INSERT INTO query_units(query_id,unit_id) VALUES('qr','o1')")
        envoi = pipeline.lancer('m5', budget=30, aujourdhui=JOUR, plus_tard=True)
        self.assertTrue(envoi['ok'])
        self.assertEqual(envoi['resultat']['requetes'], 0)
        self.assertEqual(self.http.call_count, 0)
        self.assertEqual(s.query('SELECT sum(used) n FROM budgets', one=True)['n'], 0)
        # L'ancien parcours ne relance jamais la migration ; ses nouvelles affirmations sont reprises par migrer().
        s.query("INSERT INTO claims(id,text,period,status,created) VALUES('c2','Google classe les pages.','','pending',?)", (s.now(),))
        s.query("INSERT INTO occurrences(id,claim_id,chunk_id,quote,sources) VALUES('o2','c2',?,'Google utilise','[]')", (chunk,))
        self.assertEqual(pipeline.reste()['migration']['a_faire'], 1)
        self.assertEqual(pipeline.lancer('migration')['resultat']['crees']['units'], 1)

    def test_plafond_ibou_respecte(self):
        self.corpus()
        seuils.definir('m5.critique', 0.0)
        resultat = pipeline.tout(budget=3, aujourdhui=JOUR)
        self.assertTrue(resultat['ok'], resultat)
        m5_resultat = {e['etape']: e for e in resultat['etapes']}['m5']
        self.assertEqual(m5_resultat['resultat']['arret'], 'plafond')
        self.assertEqual(s.query('SELECT sum(used) n FROM budgets', one=True)['n'], 3)
        self.assertEqual(self.http.call_count, 1)

    def test_lancer_refuse_une_etape_inconnue_et_m5_sans_budget(self):
        with self.assertRaises(ValueError):
            pipeline.lancer('m9')
        self.assertFalse(pipeline.lancer('m5')['ok'])
        s.CONFIG['ibou_key'] = ''
        self.assertIn('Ibou', pipeline.lancer('m5', budget=10)['raison'])


class HttpTests(Base):
    """Serveur HTTP reel sur port ephemere (modele : test_suivi.test_http_pulse_history_and_new_assets)."""

    def setUp(self):
        super().setUp()
        self.corpus()
        seuils.definir('m5.critique', 0.0)
        resultat = pipeline.tout(budget=60, aujourdhui=JOUR)
        self.assertTrue(resultat['ok'], resultat)
        self.serveur = s.ThreadingHTTPServer(('127.0.0.1', 0), s.Handler)
        self.fil = threading.Thread(target=self.serveur.serve_forever, daemon=True)
        self.fil.start()
        self.base = 'http://127.0.0.1:' + str(self.serveur.server_port)

    def tearDown(self):
        self.serveur.shutdown()
        self.serveur.server_close()
        self.fil.join()
        super().tearDown()

    def get(self, chemin):
        with urlopen(self.base + chemin, timeout=20) as reponse:
            corps = reponse.read()
            self.assertEqual(reponse.status, 200)
            if 'json' in reponse.headers.get('Content-Type', ''):
                return json.loads(corps)
            return corps, reponse.headers

    def post(self, chemin, donnees, jeton=True):
        entetes = {'Content-Type': 'application/json'}
        if jeton:
            entetes['X-Session-Token'] = s.TOKEN
        requete = Request(self.base + chemin, data=json.dumps(donnees).encode(), headers=entetes)
        try:
            with urlopen(requete, timeout=20) as reponse:
                return reponse.status, json.loads(reponse.read())
        except HTTPError as erreur:
            return erreur.code, json.loads(erreur.read())

    def attendre_tache(self):
        for _ in range(200):
            if s.JOB.get('status') != 'running':
                return s.JOB
            time.sleep(0.05)
        self.fail('tache toujours en cours')

    def test_routes_de_lecture(self):
        etat = self.get('/api/chaine/etat')
        self.assertTrue(etat['migre'] is False)
        self.assertIn('m1.faits', etat['reste'])
        self.assertTrue(etat['couts'])
        self.assertIn('fonctions', etat['moteurs'])
        self.assertGreater(etat['compteurs']['m1']['unites'], 0)
        unites = self.get('/api/chaine/unites?statut=a_verifier')
        self.assertLessEqual(len(unites['unites']), 50)
        self.assertNotIn('vector', unites['unites'][0] if unites['unites'] else {})
        tout = self.get('/api/chaine/unites?exclues=tous&limite=2&offset=1')
        self.assertEqual(tout['limite'], 2)
        pdg = self.unite_de(self.v_pdg, 'PDG de X')
        filtre = self.get('/api/chaine/unites?q=PDG&page=' + self.v_pdg)
        self.assertEqual([u['id'] for u in filtre['unites']], [pdg['id']])
        carte = self.get('/api/chaine/unite/' + pdg['id'])
        for cle in ('unite', 'page', 'faits', 'hypotheses', 'sources', 'type', 'groupes', 'relations', 'requetes', 'appels_ibou',
                    'preuves', 'propositions'):
            self.assertIn(cle, carte)
        self.assertNotIn('vector', carte['unite'])
        self.assertTrue(carte['faits'])
        self.assertTrue(carte['requetes'])
        self.assertTrue(carte['appels_ibou'])
        self.assertTrue(carte['preuves'])
        self.assertTrue(carte['propositions'])
        self.assertEqual(carte['groupes'][0]['paternite']['parent_unit_id'], pdg['id'])
        with self.assertRaises(HTTPError) as erreur:
            urlopen(self.base + '/api/chaine/unite/inconnue', timeout=10)
        self.assertEqual(erreur.exception.code, 404)
        self.assertEqual(self.get('/api/chaine/pages')['total'], 3)
        self.assertTrue(self.get('/api/chaine/groupes?multi=1')['groupes'])
        requetes = self.get('/api/chaine/requetes?kind=ouverte')
        self.assertTrue(all('envoyable' in r for r in requetes['requetes']))
        self.assertTrue(self.get('/api/chaine/verifications')['appels'])
        propositions = self.get('/api/chaine/propositions?statut=en_attente')
        self.assertTrue(propositions['cartes'])
        self.assertIn('avant_apres', propositions['cartes'][0])
        evaluation = self.get('/api/chaine/evaluation')
        self.assertIn('mesures', evaluation)
        gabarit = self.get('/api/chaine/evaluation/gabarit?versions=' + self.v_pdg)
        self.assertEqual(len(gabarit['pages']), 1)
        reglages = self.get('/api/chaine/reglages')
        self.assertEqual(reglages['domaines'], [SITE])
        self.assertEqual(len(self.get('/api/chaine/predicats')['predicats']), len(predicats.noms()))
        corps, entetes = self.get('/api/chaine/export.xlsx')
        self.assertTrue(corps.startswith(b'PK'))
        self.assertIn('spreadsheetml', entetes['Content-Type'])
        self.assertIn('attachment', entetes['Content-Disposition'])
        pouls = self.get('/api/pulse')
        self.assertIn('chaine', pouls)
        self.assertGreater(pouls['chaine']['unites'], 0)

    def test_routes_d_ecriture(self):
        # Jeton de session obligatoire.
        code, reponse = self.post('/api/chaine/reglages', {'option_unites': 'A'}, jeton=False)
        self.assertEqual(code, 400)
        # Reglages : domaines (reclassement des sources), seuils, moteurs, modeles, option, repli.
        s.query("INSERT INTO sources(id,unit_id,url,domain,kind,scope,confidence) VALUES('sx','u','https://www.autre-site.fr/a',"
                "'autre-site.fr','lien_externe','phrase',0.9)")
        code, reponse = self.post('/api/chaine/reglages', {'domaines': [SITE, 'https://www.autre-site.fr/']})
        self.assertEqual(code, 200, reponse)
        self.assertEqual(reponse['changes']['domaines']['vers_lien_interne'], 1)
        self.assertEqual(s.query("SELECT kind FROM sources WHERE id='sx'", one=True)['kind'], 'lien_interne')
        code, reponse = self.post('/api/chaine/reglages', {'seuils': {'m5.critique': 0.7}, 'moteurs': {'nli': 'llm'},
                                                           'modeles': {'spacy': 'fr_core_news_md'}, 'option_unites': 'C',
                                                           'repli_requetes_unite': False})
        self.assertEqual(code, 200, reponse)
        self.assertEqual(seuils.get('m5.critique'), 0.7)
        self.assertEqual(moteurs.mode('nli'), 'llm')
        self.assertEqual(moteurs.modele('spacy'), 'fr_core_news_md')
        code, reponse = self.post('/api/chaine/reglages', {'seuils': {'m5.critique': 'beaucoup'}})
        self.assertEqual(code, 400)
        self.assertIn('m5.critique', reponse['error'])
        self.assertEqual(self.post('/api/chaine/reglages', {'moteurs': {'nli': 'magie'}})[0], 400)
        self.post('/api/chaine/reglages', {'seuils': {'m5.critique': None}, 'moteurs': {'nli': 'auto'}, 'option_unites': 'A'})
        # Dates d'une page : reancrage prepare.
        code, reponse = self.post('/api/chaine/page', {'version_id': self.v_pdg, 'updated': '2026-02-01'})
        self.assertEqual(code, 200, reponse)
        self.assertGreater(reponse['unites_a_reancrer'], 0)
        self.assertEqual(self.post('/api/chaine/page', {'version_id': self.v_pdg, 'updated': '01/02/2026'})[0], 400)
        self.assertEqual(self.post('/api/chaine/page', {'version_id': 'nope', 'updated': '2026-02-01'})[0], 404)
        # Lancer une etape (tache du serveur), puis l'etat la reflete.
        code, reponse = self.post('/api/chaine/lancer', {'etape': 'm1.page'})
        self.assertEqual(code, 200, reponse)
        job = self.attendre_tache()
        self.assertEqual(job['status'], 'completed', job.get('message'))
        self.assertEqual(self.get('/api/chaine/etat')['reste']['m1.page']['a_faire'], 0)
        self.assertEqual(self.post('/api/chaine/lancer', {'etape': 'm9'})[0], 400)
        self.assertEqual(self.post('/api/chaine/lancer', {'etape': 'm5'})[0], 400)
        code, reponse = self.post('/api/chaine/migrer', {})
        self.assertEqual(code, 200)
        self.assertEqual(self.attendre_tache()['status'], 'completed')
        self.assertTrue(self.get('/api/chaine/etat')['migre'])
        # Paternite : arbitrage humain.
        pdg = self.unite_de(self.v_pdg, 'PDG de X')
        reprise = self.unite_de(self.v_reprise, 'PDG de X')
        groupe = s.query('SELECT group_id FROM unit_group_members WHERE unit_id=?', (pdg['id'],), True)['group_id']
        code, reponse = self.post('/api/chaine/paternite', {'group_id': groupe, 'unit_id': reprise['id']})
        self.assertEqual(code, 200, reponse)
        self.assertEqual(reponse['paternite']['criterion'], 'arbitrage_humain')
        self.assertEqual(self.post('/api/chaine/paternite', {'group_id': groupe, 'unit_id': 'autre'})[0], 400)
        # Requete : modification humaine controlee.
        requete = s.query("SELECT q.* FROM queries q JOIN query_units qu ON qu.query_id=q.id WHERE qu.unit_id=? AND q.kind='ouverte'",
                          (pdg['id'],), True)
        code, reponse = self.post('/api/chaine/requete', {'id': requete['id'], 'texte': 'x_y'})
        self.assertEqual(code, 200)
        self.assertFalse(reponse['ok'])
        code, reponse = self.post('/api/chaine/requete', {'id': requete['id'], 'texte': 'Qui dirige X depuis octobre 2026 ?'})
        self.assertTrue(reponse['ok'], reponse)
        # Decision M6 : ok / raison.
        proposition = s.query("SELECT * FROM proposals WHERE status='proposee' AND kind='corrigee'", one=True)
        code, reponse = self.post('/api/chaine/decision', {'id': proposition['id'], 'decision': 'modifier', 'texte': 'Tout autre chose.'})
        self.assertEqual(code, 200)
        self.assertFalse(reponse['ok'])
        self.assertEqual(reponse['raison'], 'controle_echoue')
        code, reponse = self.post('/api/chaine/decision', {'id': proposition['id'], 'decision': 'accepter'})
        self.assertEqual(code, 200, reponse)
        self.assertTrue(reponse['ok'])
        self.assertIsNone(reponse['raison'])
        self.assertTrue(reponse['version_id'])
        code, reponse = self.post('/api/chaine/decision', {'id': proposition['id'], 'decision': 'accepter'})
        self.assertEqual(code, 400)
        # Predicats : remplacement valide, refus d'une liste invalide.
        liste = predicats.tous()
        code, reponse = self.post('/api/chaine/predicats', {'predicats': liste[:40]})
        self.assertEqual(code, 200, reponse)
        self.assertEqual(len(predicats.noms()), 40)
        code, reponse = self.post('/api/chaine/predicats', {'predicats': [{'nom': 'Mauvais nom'}]})
        self.assertEqual(code, 400)
        self.assertEqual(len(predicats.noms()), 40)
        # Evaluation : chargement d'une reference et saisie de notes humaines.
        reference = json.loads((Path(__file__).resolve().parents[1] / 'docs' / 'reference_exemple.json').read_text(encoding='utf-8'))
        code, reponse = self.post('/api/chaine/evaluation', {'action': 'charger', 'reference': reference})
        self.assertEqual(code, 200, reponse)
        code, reponse = self.post('/api/chaine/evaluation', {'action': 'saisir', 'mesure': 'm4.note_humaine', 'valeur': 4})
        self.assertEqual((code, reponse['notes']), (200, 1))
        self.assertEqual(self.post('/api/chaine/evaluation', {'action': 'saisir', 'mesure': 'm4.note_humaine', 'valeur': 9})[0], 400)
        self.assertTrue(self.get('/api/chaine/evaluation')['reference']['chargee'])
        # Pendant un traitement, les ecritures sont refusees (garde-fou existant).
        s.JOB['status'] = 'running'
        try:
            self.assertEqual(self.post('/api/chaine/reglages', {'option_unites': 'A'})[0], 400)
        finally:
            s.JOB['status'] = 'idle'
        self.assertEqual(self.post('/api/chaine/inconnue', {})[0], 404)

    def test_routes_du_lot_de_finition(self):
        pdg = self.unite_de(self.v_pdg, 'PDG de X')
        # Carte d'unite : paragraphe entier avec les offsets de la phrase.
        carte = self.get('/api/chaine/unite/' + pdg['id'])
        paragraphe = carte['paragraphe']
        self.assertTrue(paragraphe['trouve'])
        self.assertEqual(paragraphe['texte'][paragraphe['phrase_debut']:paragraphe['phrase_fin']], pdg['quote'] or pdg['text'])
        version = s.query('SELECT text FROM versions WHERE id=?', (self.v_pdg,), True)['text']
        self.assertEqual(version[paragraphe['debut']:paragraphe['fin']], paragraphe['texte'])
        self.assertNotIn('\n\n', paragraphe['texte'])
        # Pages : taux d'obsolescence calcule par le serveur (definition unique, celle de l'export) et sujets.
        from chaine import export, m5
        pages = {p['version_id']: p for p in self.get('/api/chaine/pages')['pages']}
        unites = s.query('SELECT * FROM units WHERE version_id=?', (self.v_periph,))
        attendu = export.taux_obsolescence(unites)
        self.assertIsNotNone(attendu)
        self.assertGreater(attendu, 0)
        self.assertAlmostEqual(pages[self.v_periph]['taux_obsolescence']['taux'], round(attendu, 4))
        self.assertAlmostEqual(m5.taux_obsolescence([self.v_periph])[self.v_periph]['taux'], round(attendu, 4))
        self.assertIn('sujets', pages[self.v_pdg])
        self.assertEqual(self.get('/api/chaine/pages?filtre=sans_maj')['total'], 3)
        self.assertEqual(self.get('/api/chaine/pages?filtre=propositions')['total'],
                         len({p['version_id'] for p in s.query("SELECT version_id FROM proposals WHERE status IN ('proposee','a_rediger','reportee')")}))
        with self.assertRaises(HTTPError) as erreur:
            urlopen(self.base + '/api/chaine/pages?filtre=nimporte', timeout=10)
        self.assertEqual(erreur.exception.code, 400)
        # Compteurs : pages = URL distinctes, dates manquantes comptees sur la derniere version de chaque page.
        compteurs = self.get('/api/chaine/etat')['compteurs']['pages']
        self.assertEqual(compteurs['pages'], 3)
        self.assertEqual(compteurs['pages_sans_date_maj'], 3)
        self.assertEqual(compteurs['pages_sans_date_publication'], 0)
        # Propositions : filtres motif, page, unite ; comptes par motif.
        toutes = self.get('/api/chaine/propositions')
        self.assertTrue(toutes['motifs'])
        motif = next(iter(toutes['motifs']))
        filtrees = self.get('/api/chaine/propositions?motif=' + motif)
        self.assertEqual(filtrees['total'], toutes['motifs'][motif])
        self.assertTrue(all(c['motif']['code'] == motif for c in filtrees['cartes']))
        par_unite = self.get('/api/chaine/propositions?unite=' + pdg['id'])
        self.assertTrue(par_unite['cartes'])
        self.assertTrue(all(c['unit_id'] == pdg['id'] for c in par_unite['cartes']))
        par_page = self.get('/api/chaine/propositions?page=' + self.v_pdg)
        self.assertTrue(all(c['page']['version_id'] == self.v_pdg for c in par_page['cartes']))
        # Liens internes recommandes.
        s.query("INSERT INTO link_recommendations(id,unit_id,from_version,to_version,anchor,reason,status,created) "
                "VALUES('lr1',?,?,?,'PDG de X','source manquante','proposee','2026-11-01')", (pdg['id'], self.v_reprise, self.v_pdg))
        liens = self.get('/api/chaine/liens?statut=proposee')
        lien = next(l for l in liens['liens'] if l['id'] == 'lr1')
        self.assertEqual((lien['source_titre'], lien['cible_titre']), ('Le marché en bref', 'Gouvernance de X'))
        self.assertGreaterEqual(liens['par_statut']['proposee'], 1)
        # Appel Ibou : preuves de l'appel (retenues, ecartees avec raison), unites couvertes et verdict.
        appel = s.query('SELECT * FROM ibou_calls WHERE query_id IS NOT NULL ORDER BY created LIMIT 1', one=True)
        detail = self.get('/api/chaine/appel/' + appel['id'])
        self.assertEqual(detail['requete']['id'], appel['query_id'])
        self.assertTrue(detail['unites'])
        self.assertEqual(len(detail['preuves']), s.query('SELECT count(*) n FROM evidence WHERE call_id=?', (appel['id'],), True)['n'])
        self.assertEqual(detail['retenues'] + detail['ecartees'], len(detail['preuves']))
        self.assertTrue(all(p['reject_reason'] for p in detail['preuves'] if not p['kept']))
        with self.assertRaises(HTTPError) as erreur:
            urlopen(self.base + '/api/chaine/appel/inconnu', timeout=10)
        self.assertEqual(erreur.exception.code, 404)
        # Solde Ibou par la chaine : sans cle, aucune requete et la raison.
        cle = s.CONFIG['ibou_key']
        s.CONFIG['ibou_key'] = ''
        try:
            solde = self.get('/api/chaine/solde')
        finally:
            s.CONFIG['ibou_key'] = cle
        self.assertFalse(solde['disponible'])
        self.assertIn('Clé Ibou', solde['raison'])
        with patch.object(s, 'ibou_balance', return_value={'remaining': 812}):
            self.assertEqual(self.get('/api/chaine/solde'), {'disponible': True, 'solde': 812, 'raison': None})
        # Requete a revoir : raison lisible (controles echoues en francais).
        requete = s.query("SELECT * FROM queries WHERE kind='ouverte' ORDER BY created LIMIT 1", one=True)
        s.query("UPDATE queries SET status='a_revoir', checks=? WHERE id=?",
                (json.dumps({'ok': False, 'problemes': ['accents absents : a', 'nombre de mots hors de 4 a 30'],
                             'reformulation_rejetee': {'texte': None, 'problemes': []}}), requete['id']))
        revue = next(r for r in self.get('/api/chaine/requetes?statut=a_revoir')['requetes'] if r['id'] == requete['id'])
        self.assertEqual(revue['problemes_lisibles'], ['accents manquants sur : « a »', 'nombre de mots hors de 4 à 30'])
        self.assertIn('contrôles échoués', revue['raison_non_envoyable'])
        self.assertNotIn('a_revoir', revue['raison_non_envoyable'])
        code, reponse = self.post('/api/chaine/requete', {'id': requete['id'], 'texte': 'x_y'})
        self.assertTrue(reponse['problemes_lisibles'])
        self.assertFalse(any('_' in p and 'tiret bas' not in p for p in reponse['problemes_lisibles']))
        # Journal leger et pagine.
        journal = self.get('/api/chaine/journal?limite=5')
        self.assertLessEqual(len(journal['evenements']), 5)
        self.assertGreater(journal['total'], 0)
        self.assertTrue(all(e['message'].startswith('Chaîne') for e in self.get('/api/chaine/journal?chaine=1')['evenements']))
        # Issue des etapes : connue apres le lancement.
        issues = self.get('/api/chaine/etat')['issues']
        self.assertEqual(issues['m6']['issue'], 'terminee')
        # Predicat : edition ligne a ligne validee par le serveur.
        nom = next(p['nom'] for p in predicats.tous() if p['classe'] == 'etat_courant')
        code, reponse = self.post('/api/chaine/predicat', {'nom': nom, 'libelle': 'Libellé révisé', 'volatilite_jours': 90,
                                                           'critique': True, 'gabarit': 'etat_courant'})
        self.assertEqual(code, 200, reponse)
        modifie = next(p for p in predicats.tous() if p['nom'] == nom)
        self.assertEqual((modifie['libelle'], modifie['volatilite_jours'], modifie['critique']), ('Libellé révisé', 90, True))
        code, reponse = self.post('/api/chaine/predicat', {'nom': nom, 'classe': 'immuable'})
        self.assertEqual(code, 400)
        self.assertIn('immuable', reponse['error'])
        self.assertEqual(self.post('/api/chaine/predicat', {'nom': nom, 'volatilite_jours': -3})[0], 400)
        self.assertEqual(self.post('/api/chaine/predicat', {'nom': nom, 'gabarit': 'magie'})[0], 400)
        self.assertEqual(self.post('/api/chaine/predicat', {'nom': 'inconnu', 'libelle': 'x'})[0], 404)
        self.assertEqual(next(p for p in predicats.tous() if p['nom'] == nom)['classe'], 'etat_courant')

if __name__ == '__main__':
    unittest.main()
