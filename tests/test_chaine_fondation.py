"""Tests du lot fondation de la chaine : noyau, seuils, schema, migration, moteurs."""
import json
import math
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s
import chaine
from chaine import migration
from chaine import moteurs
from chaine import noyau
from chaine import schema
from chaine import seuils


class Base(unittest.TestCase):
    """Base temporaire initialisee par le serveur puis par la chaine."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        s.DB = Path(self.tmp.name) / 'test.sqlite3'
        s.init()
        s.STOP.clear()
        self.config = dict(s.CONFIG)
        s.CONFIG.update(embeddings=False, ibou_key='test-only', model='test-model', provider='lmstudio', openai_key='')
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

    def reglage(self, cle, valeur):
        noyau.definir_reglage(cle, valeur)


@contextmanager
def sans_tiers():
    """Simule un poste sans aucune dependance tierce."""
    with patch.object(moteurs, 'paquet_present', lambda paquet: False):
        moteurs.reinitialiser()
        yield
    moteurs.reinitialiser()


# ---------------------------------------------------------------------------
# Branchement et noyau
# ---------------------------------------------------------------------------

class BranchementTests(Base):

    def test_brancher_idempotent(self):
        self.assertIs(chaine.brancher(s), s)
        self.assertIs(chaine.brancher(s), s)
        self.assertIs(noyau.srv(), s)
        self.assertTrue(chaine.VERSION)

    def test_brancher_refuse_module_incomplet(self):
        with self.assertRaises(ValueError):
            chaine.brancher(types.ModuleType('vide'))
        with self.assertRaises(ValueError):
            chaine.brancher(None)
        self.assertIs(noyau.srv(), s)

    def test_srv_sans_branchement(self):
        ancien = noyau._ETAT['serveur']
        noyau._ETAT['serveur'] = None
        try:
            with self.assertRaises(RuntimeError):
                noyau.srv()
        finally:
            noyau._ETAT['serveur'] = ancien

    def test_schema_llm_enregistre_et_valide(self):
        definition = noyau.objet({'ok': {'type': 'boolean'}, 'label': noyau.enum(['a', 'b'])})
        noyau.schema('chaine:essai', definition)
        self.assertEqual(noyau.schema_pour({'mode': 'chaine:essai'}), definition)
        self.assertEqual(noyau.valider({'ok': True, 'label': 'a'}, definition), [])
        self.assertTrue(noyau.valider({'ok': 1, 'label': 'c', 'x': 0}, definition))
        with self.assertRaises(KeyError):
            noyau.schema_pour({'mode': 'chaine:inconnu'})
        with self.assertRaises(ValueError):
            noyau.schema('essai', definition)

    def test_schema_dynamique(self):
        noyau.schema('chaine:ids', lambda payload: noyau.objet({'id': noyau.enum(payload['ids'])}))
        resultat = noyau.schema_pour({'mode': 'chaine:ids', 'ids': ['x', 'y']})
        self.assertEqual(resultat['properties']['id']['enum'], ['x', 'y'])

    def test_llm_sortie_non_conforme(self):
        noyau.schema('chaine:essai2', noyau.objet({'ok': {'type': 'boolean'}}))
        with patch.object(s, 'llm', return_value={'ok': 'oui'}):
            with self.assertRaises(s.OperationalError):
                noyau.llm('tache', {'mode': 'chaine:essai2'})
        with patch.object(s, 'llm', return_value={'ok': True}):
            self.assertEqual(noyau.llm('tache', {'mode': 'chaine:essai2'}), {'ok': True})

    def test_reglages(self):
        self.assertEqual(noyau.reglage('moteur:nli', 'auto'), 'auto')
        noyau.definir_reglage('moteur:nli', 'llm')
        self.assertEqual(noyau.reglage('moteur:nli', 'auto'), 'llm')
        noyau.definir_reglage('moteur:nli', None)
        self.assertEqual(noyau.reglage('moteur:nli', 'auto'), 'auto')


# ---------------------------------------------------------------------------
# Executions et couts
# ---------------------------------------------------------------------------

def journaliser(service, tokens_in=0, tokens_out=0, units=0, duree_ms=0, step=None):
    """Ligne api_log ecrite comme le fera server.api_log apres integration (colonnes nommees)."""
    s.query(
        'INSERT INTO api_log(id,created,service,endpoint,operation,status,duration_ms,units,tokens_in,tokens_out,step) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (s.uid(), s.now(), service, '/x', 'test', 'success', duree_ms, units, tokens_in, tokens_out, step),
    )


class EtapeTests(Base):

    def test_couts_sommes_et_contexte(self):
        journaliser('openai', tokens_in=999)
        time.sleep(0.002)
        with noyau.etape('m1.unites', moteurs={'unites': 'llm:qwen3'}, prompt='unites-v1',
                         seuils={'nli.implication': 0.8}, pages=2) as run:
            self.assertEqual(s.CTX.step, run.id)
            self.assertEqual(noyau.execution_courante(), run.id)
            # server.api_log renseigne lui-meme api_log.step depuis S.CTX.step (lot integration).
            s.api_log('lmstudio', '/chat/completions', 'success', time.monotonic(), tokens_in=100, tokens_out=20)
            journaliser('ibou', units=3, duree_ms=10, step=s.CTX.step)
            s.api_log('openai', '/v1/responses', 'success', time.monotonic(), tokens_in=5, tokens_out=1)
            run.pages += 1
            run.items = 7
            run.moteur('nli', 'llm:lmstudio:test-model')
        self.assertIsNone(s.CTX.step)
        ligne = s.query('SELECT * FROM step_runs WHERE id=?', (run.id,), True)
        self.assertEqual(ligne['status'], 'terminee')
        self.assertEqual(ligne['step'], 'm1.unites')
        self.assertEqual((ligne['tokens_in'], ligne['tokens_out'], ligne['ibou_units']), (105, 21, 3))
        self.assertEqual((ligne['pages'], ligne['items']), (3, 7))
        self.assertEqual(json.loads(ligne['engines']), {'unites': 'llm:qwen3', 'nli': 'llm:lmstudio:test-model'})
        self.assertEqual(json.loads(ligne['thresholds']), {'nli.implication': 0.8})
        self.assertEqual(ligne['prompt'], 'unites-v1')
        self.assertGreaterEqual(ligne['local_seconds'], 0)
        self.assertTrue(ligne['ended'] >= ligne['started'])
        marques = s.query('SELECT count(*) n FROM api_log WHERE step=?', (run.id,), True)['n']
        self.assertEqual(marques, 3)
        self.assertEqual(s.query('SELECT count(*) n FROM api_log WHERE step IS NULL', one=True)['n'], 1)

    def test_seuils_par_noms(self):
        with noyau.etape('m3.qualifier', seuils=['m3.zone_grise', 'nli.implication']) as run:
            pass
        ligne = s.query('SELECT thresholds FROM step_runs WHERE id=?', (run.id,), True)
        self.assertEqual(json.loads(ligne['thresholds'])['m3.zone_grise'], [0.35, 0.75])

    def test_erreur_et_interruption(self):
        with self.assertRaises(ValueError):
            with noyau.etape('m2') as run:
                raise ValueError('boum')
        ligne = s.query('SELECT status, note FROM step_runs WHERE id=?', (run.id,), True)
        self.assertEqual(ligne['status'], 'erreur')
        self.assertIn('boum', ligne['note'])
        s.STOP.set()
        with self.assertRaises(s.OperationalError):
            with noyau.etape('m5') as run:
                s.checkpoint()
        self.assertEqual(s.query('SELECT status FROM step_runs WHERE id=?', (run.id,), True)['status'], 'interrompue')

    def test_etapes_imbriquees(self):
        with noyau.etape('externe') as externe:
            with noyau.etape('interne') as interne:
                s.api_log('lmstudio', '/chat/completions', 'success', time.monotonic(), tokens_in=10)
            self.assertEqual(s.CTX.step, externe.id)
            s.api_log('lmstudio', '/chat/completions', 'success', time.monotonic(), tokens_in=1)
        lignes = {r['id']: r['tokens_in'] for r in s.query('SELECT id, tokens_in FROM step_runs')}
        self.assertEqual(lignes[interne.id], 10)
        self.assertEqual(lignes[externe.id], 1)

    def test_sans_schema(self):
        # server.init() cree desormais le schema de la chaine (lot integration) : base nue sans init.
        s.DB = Path(self.tmp.name) / 'nu.sqlite3'
        sqlite3.connect(s.DB).close()
        with self.assertRaises(RuntimeError):
            with noyau.etape('m1'):
                pass


# ---------------------------------------------------------------------------
# Seuils
# ---------------------------------------------------------------------------

class SeuilsTests(Base):

    def test_defauts_complets(self):
        for nom in ('nli.implication', 'dedup.cosinus', 'm3.zone_grise', 'm5.critique', 'm1.fcm.appartenance', 'm1.fcm.m',
                    'm3.centralite.ecart', 'm3.nettement_plus_recent_jours', 'm4.requete.caracteres_max',
                    'm4.requete.mots_min', 'm4.requete.mots_max', 'm6.diff_ratio_max', 'm5.domaines_independants'):
            self.assertIn(nom, seuils.DEFAUTS)
        self.assertEqual(seuils.get('m1.fcm.appartenance'), 0.2)
        self.assertEqual(seuils.get('m4.requete.caracteres_max'), 200)
        self.assertEqual(seuils.get('m5.domaines_independants'), 2)
        for nom in seuils.DEFAUTS:
            self.assertTrue(nom.isascii())

    def test_surcharge_settings(self):
        seuils.definir('nli.implication', '0.9')
        self.assertEqual(seuils.get('nli.implication'), 0.9)
        s.query("INSERT OR REPLACE INTO settings VALUES('seuil:m3.zone_grise','[0.3, 0.7]')")
        seuils._vider_cache()
        self.assertEqual(seuils.get('m3.zone_grise'), [0.3, 0.7])
        self.assertEqual(seuils.tous()['nli.implication'], 0.9)
        seuils.reinitialiser('nli.implication')
        self.assertEqual(seuils.get('nli.implication'), 0.8)

    def test_controles(self):
        with self.assertRaises(KeyError):
            seuils.get('seuil.inexistant')
        with self.assertRaises(ValueError):
            seuils.definir('m4.requete.caracteres_max', 12.5)
        with self.assertRaises(ValueError):
            seuils.definir('m3.zone_grise', [0.9, 0.1])
        self.assertIs(seuils.definir('m1.faits.entites_dans_span', 0), False)
        s.query("INSERT OR REPLACE INTO settings VALUES('seuil:nli.contradiction','pas un nombre')")
        seuils._vider_cache()
        self.assertEqual(seuils.get('nli.contradiction'), 0.8)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class SchemaTests(Base):

    def test_idempotent_et_complet(self):
        self.assertEqual(schema.init(), [])
        tables = {r['name'] for r in s.query("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('settings', 'step_runs', 'units', 'entities', 'unit_entities', 'facts', 'hypotheses', 'topics',
                      'topic_membership', 'unit_groups', 'unit_group_members', 'unit_relations', 'sources', 'claim_types',
                      'paternity', 'link_recommendations', 'queries', 'query_units', 'ibou_calls', 'proposals'):
            self.assertIn(table, tables)
        self.assertIn('step', schema.colonnes('api_log'))
        self.assertIn('end', schema.colonnes('chunks'))
        self.assertIn('lede', schema.colonnes('versions'))
        self.assertIn('run_id', schema.colonnes('evidence'))
        # 30 colonnes du contrat + anchor_run, obs_run (m1_page) et facts_run_id (m1_unites), crees par server.init().
        self.assertEqual(len(schema.colonnes('units')), 38)   # + status_run, temporal_run, critical_run, verdict_run, status_suivi (relecture)
        index = {r['name'] for r in s.query("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn('idx_ch_units_version_id', index)
        self.assertIn('idx_ch_api_log_step', index)

    def test_mots_reserves_utilisables(self):
        s.query('INSERT INTO proposals(id,"before","after",status) VALUES(?,?,?,?)', ('p', 'avant', 'apres', 'proposee'))
        ligne = s.query('SELECT before, after FROM proposals', one=True)
        self.assertEqual((ligne['before'], ligne['after']), ('avant', 'apres'))
        s.query('UPDATE chunks SET end=1')

    def test_colonne(self):
        self.assertTrue(schema.colonne('units', 'essai_module', 'REAL'))
        self.assertFalse(schema.colonne('units', 'essai_module', 'REAL'))
        with self.assertRaises(ValueError):
            schema.colonne('units', 'x; DROP TABLE units', 'TEXT')
        with self.assertRaises(ValueError):
            schema.colonne('units', 'y', 'TEXT); DROP TABLE units; --')
        with self.assertRaises(ValueError):
            schema.colonne('inexistante', 'y', 'TEXT')

    def test_base_vide_sans_serveur(self):
        chemin = Path(self.tmp.name) / 'vide.sqlite3'
        modifications = schema.init(chemin)
        self.assertIn('table units', modifications)
        self.assertEqual(schema.init(chemin), [])
        base = sqlite3.connect(chemin)
        try:
            colonnes = [r[1] for r in base.execute('PRAGMA table_info(api_log)')]
        finally:
            base.close()
        self.assertIn('step', colonnes)

    def test_server_init_apres_schema(self):
        s.init()
        self.assertEqual(schema.init(), [])


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

class MigrationTests(Base):

    def construire(self):
        texte_a = ("Le périphérique sera fermé en août 2026. L'Insee annonce 3,5 % de chômage.\n"
                   "Selon l’Insee, le taux reste stable.")
        version_a = s.add_content('https://site.example/a', 'A', texte_a, '2025-03-01')
        version_b = s.add_content('texte:b', 'B', 'Paris est en France. Rien d’autre.')
        chunk_a = s.query('SELECT id FROM chunks WHERE version_id=?', (version_a,), True)['id']
        chunk_b = s.query('SELECT id FROM chunks WHERE version_id=?', (version_b,), True)['id']
        claims = {
            'c1': ('Le périphérique sera fermé en août 2026.', 'août 2026', 'validated', 0),
            'c2': ("L'Insee annonce 3,5 % de chômage.", '', 'correct', 0),
            'c3': ("Selon l'Insee, le taux reste stable.", 'Inconnue', 'pending', 0),
            'c4': ('Paris est en France.', '', 'not_needed', 0),
            'c5': ('Phrase absente.', '2019-2024', 'review', 1),
        }
        for cid, (texte, periode, statut, exclu) in claims.items():
            s.query('INSERT INTO claims(id,text,period,status,excluded,created) VALUES(?,?,?,?,?,?)',
                    (cid, texte, periode, statut, exclu, s.now()))
        occurrences = [
            ('o1', 'c1', chunk_a, 'Le périphérique sera fermé en août 2026.', ['https://site.example/b', 'https://www.gouv.fr/x']),
            ('o2', 'c2', chunk_a, "L'Insee annonce 3,5 % de chômage.", ['Insee']),
            ('o3', 'c3', chunk_a, "Selon l'Insee, le taux reste stable.", []),
            ('o4', 'c4', chunk_b, 'Paris est en France.', []),
            ('o5', 'c1', chunk_b, 'Paris est en France.', []),
            ('o6', 'c5', chunk_b, 'Citation inventée.', []),
        ]
        for oid, cid, chunk, citation, sources in occurrences:
            s.query('INSERT INTO occurrences VALUES(?,?,?,?,?)', (oid, cid, chunk, citation, json.dumps(sources)))
        s.query("INSERT INTO checks VALUES('k1','c1','{}','2026-11-01T00:00:00+00:00')")
        s.query("INSERT INTO screenings VALUES('c3','f','search','r','Le taux de chômage est-il stable selon l’Insee ?','{}',?)", (s.now(),))
        s.query("INSERT INTO screenings VALUES('c4','f','skip','r','','{}',?)", (s.now(),))
        s.query("INSERT INTO calls(id,endpoint,query,units,status,response,created,claim_id) VALUES('k9','passage','q',3,'success',?,?,'c3')",
                (json.dumps({'max_results': 10}), s.now()))
        s.query("INSERT INTO patches VALUES('p1','c2',?,?,?,'proposed',?)",
                (version_a, "L'Insee annonce 3,5 % de chômage.", "L'Insee annonce 3,7 % de chômage.", s.now()))
        s.query("INSERT INTO evidence(id,claim_id,url,title,text,external_chunk,created) VALUES('e1','c3','https://www.insee.fr/t','T','x','',?)",
                (s.now(),))
        return version_a, version_b

    def test_migration_complete_et_idempotente(self):
        version_a, version_b = self.construire()
        schema.init()
        rapport = migration.migrer(taille_lot=2)
        self.assertEqual(rapport['crees']['units'], 6)
        self.assertEqual(rapport['spans']['introuvables'], 1)
        self.assertEqual(rapport['spans']['retrouves_chunk'], 5)
        self.assertEqual(rapport['crees']['unit_groups'], 5)
        self.assertEqual(rapport['crees']['unit_group_members'], 6)
        self.assertEqual(rapport['crees']['sources'], 3)
        self.assertEqual(rapport['crees']['queries'], 1)
        self.assertEqual(rapport['crees']['ibou_calls'], 1)
        self.assertEqual(rapport['crees']['proposals'], 1)
        self.assertEqual(rapport['evidence_rattachees'], 1)
        self.assertEqual(rapport['domaines_site'], ['site.example'])
        textes = {v['id']: v['text'] for v in s.query('SELECT id, text FROM versions')}
        for unite in s.query('SELECT * FROM units WHERE span_start IS NOT NULL'):
            self.assertEqual(textes[unite['version_id']][unite['span_start']:unite['span_end']], unite['quote'])
        unites = {u['id']: u for u in s.query('SELECT * FROM units')}
        self.assertEqual(unites['o3']['quote'], "Selon l’Insee, le taux reste stable.")
        self.assertEqual((unites['o1']['status'], unites['o1']['verdict']), ('sourcee', 'confirmee'))
        self.assertEqual(unites['o1']['verified_at'], '2026-11-01T00:00:00+00:00')
        self.assertEqual((unites['o1']['anchor_date'], unites['o1']['anchor_kind']), ('2026-08-01', 'phrase'))
        self.assertEqual((unites['o2']['anchor_date'], unites['o2']['anchor_kind']), ('2025-03-01', 'publication'))
        self.assertEqual(unites['o4']['anchor_kind'], 'inconnue')
        self.assertEqual((unites['o2']['status'], unites['o2']['verdict']), ('a_verifier', 'corrigee'))
        self.assertEqual(unites['o3']['status'], 'a_verifier')
        self.assertEqual(unites['o4']['status'], 'non_verifiable')
        self.assertEqual((unites['o6']['excluded'], unites['o6']['span_start'], unites['o6']['anchor_date']), (1, None, '2024-01-01'))
        self.assertTrue(all(u['extractor'] == 'migration' and u['run_id'] == rapport['run_id'] for u in unites.values()))
        sources = {r['url'] or r['cited_entity']: r for r in s.query('SELECT * FROM sources')}
        self.assertEqual(sources['https://site.example/b']['kind'], 'lien_interne')
        self.assertEqual(sources['https://www.gouv.fr/x']['kind'], 'lien_externe')
        self.assertEqual(sources['https://www.gouv.fr/x']['domain'], 'gouv.fr')
        self.assertEqual(sources['Insee']['kind'], 'source_nommee')
        requete = s.query('SELECT * FROM queries', one=True)
        self.assertEqual((requete['status'], requete['template'], requete['group_id']), ('prete', 'ancien-pipeline', 'c3'))
        self.assertEqual(s.query('SELECT unit_id FROM query_units', one=True)['unit_id'], 'o3')
        appel = s.query('SELECT * FROM ibou_calls', one=True)
        self.assertEqual((appel['query_id'], appel['legacy_call_id'], appel['max_results']), (requete['id'], 'k9', 10))
        proposition = s.query('SELECT * FROM proposals', one=True)
        self.assertEqual((proposition['unit_id'], proposition['status'], proposition['legacy_patch_id']), ('o2', 'proposee', 'p1'))
        self.assertEqual(s.query("SELECT group_id, domain FROM evidence WHERE id='e1'", one=True), {'group_id': 'c3', 'domain': 'insee.fr'})
        membres = s.query("SELECT unit_id FROM unit_group_members WHERE group_id='c1' ORDER BY unit_id")
        self.assertEqual([m['unit_id'] for m in membres], ['o1', 'o5'])
        execution = s.query('SELECT * FROM step_runs WHERE id=?', (rapport['run_id'],), True)
        self.assertEqual(execution['status'], 'terminee')
        avant = {t: s.query(f'SELECT count(*) n FROM {t}', one=True)['n'] for t in migration.TABLES_SUIVIES}
        second = migration.migrer()
        self.assertEqual(sum(second['crees'].values()), 0)
        self.assertEqual(second['evidence_rattachees'], 0)
        self.assertEqual(second['avant'], avant)
        self.assertEqual(second['apres'], avant)
        self.assertEqual(s.query('SELECT count(*) n FROM claims', one=True)['n'], 5)

    def test_domaines_du_site_par_reglage(self):
        self.construire()
        noyau.definir_reglage('site:domaines', '["gouv.fr"]')
        rapport = migration.migrer()
        self.assertIn('gouv.fr', rapport['domaines_site'])
        self.assertEqual(s.query("SELECT kind FROM sources WHERE domain='gouv.fr'", one=True)['kind'], 'lien_interne')

    def test_lecture_des_periodes(self):
        self.assertEqual(migration.ancre_periode('Depuis le 1er juillet 2025'), '2025-07-01')
        self.assertEqual(migration.ancre_periode('08/11/2026'), '2026-11-08')
        self.assertEqual(migration.ancre_periode('Décembre 2024'), '2024-12-01')
        self.assertEqual(migration.ancre_periode('juillet 2020 (publication en 2022)'), '2022-01-01')
        self.assertIsNone(migration.ancre_periode('Lucene 9.12'))
        self.assertIsNone(migration.ancre_periode('RFC 9110'))
        self.assertIsNone(migration.ancre_periode('Inconnue'))


# ---------------------------------------------------------------------------
# Moteurs en repli
# ---------------------------------------------------------------------------

TEXTE = ("M. Dupont, directeur de l'Insee, a annoncé une hausse de 3,5 % en 2024. Le taux atteint 2.7 points, cf. p. 12 du rapport. "
         "« C'est une bonne nouvelle. Nous continuerons », a-t-il déclaré. Les ventes ont progressé (env. 12 %)… Elles restent fragiles ! "
         "Que faire ? J. R. R. Tolkien est cité. Le budget atteint 1,2 milliard d'euros, etc. Puis 45 000 € HT le 1er mars 2025.\n"
         "Titre de section\nUn paragraphe qui continue\nsur la ligne suivante.\n- premier point\n- second point")

PHRASES_ATTENDUES = [
    "M. Dupont, directeur de l'Insee, a annoncé une hausse de 3,5 % en 2024.",
    'Le taux atteint 2.7 points, cf. p. 12 du rapport.',
    "« C'est une bonne nouvelle. Nous continuerons », a-t-il déclaré.",
    'Les ventes ont progressé (env. 12 %)…',
    'Elles restent fragiles !',
    'Que faire ?',
    'J. R. R. Tolkien est cité.',
    "Le budget atteint 1,2 milliard d'euros, etc.",
    'Puis 45 000 € HT le 1er mars 2025.',
    'Titre de section',
    'Un paragraphe qui continue\nsur la ligne suivante.',
    '- premier point',
    '- second point',
]


class MoteursReplisTests(Base):

    def test_phrases_regles(self):
        with sans_tiers():
            offsets, moteur = moteurs.phrases(TEXTE, avec_moteur=True)
            self.assertEqual(moteur, 'regles')
            self.assertEqual(moteurs.dernier('phrases'), 'regles')
        self.assertEqual([TEXTE[d:f] for d, f in offsets], PHRASES_ATTENDUES)

    def test_phrases_cas_limites(self):
        self.assertEqual(moteurs.phrases_regles(''), [])
        self.assertEqual(moteurs.phrases_regles('   \n\n  '), [])
        texte = 'Il pèse 3.5 kg. Le site www.exemple.fr est cité. Voir le n° 5... Puis la fin'
        resultat = [texte[d:f] for d, f in moteurs.phrases_regles(texte)]
        self.assertEqual(resultat, ['Il pèse 3.5 kg.', 'Le site www.exemple.fr est cité.', 'Voir le n° 5...', 'Puis la fin'])
        long_texte = ('mot ' * 400).strip() + '; ' + ('mot ' * 10).strip() + '.'
        with sans_tiers():
            morceaux = moteurs.phrases(long_texte)
        self.assertGreater(len(morceaux), 1)
        self.assertTrue(all(f - d <= seuils.get('m1.segment.phrase_caracteres_max') for d, f in morceaux))

    def test_numerotation_et_fragments(self):
        texte = '01. Comment fonctionne un moteur. II. Partie deux. Fin.'
        self.assertEqual([texte[d:f] for d, f in moteurs.phrases_regles(texte)],
                         ['01. Comment fonctionne un moteur.', 'II. Partie deux.', 'Fin.'])
        texte = 'Une phrase. . 01 Autre phrase.'
        segments = [(0, 11), (12, 13), (14, 16), (17, 30)]
        self.assertEqual(moteurs._fusionner_fragments(texte, segments), [(0, 16), (17, 30)])
        self.assertEqual(moteurs._fusionner_fragments(texte, [(12, 13), (17, 30)]), [(12, 30)])

    def test_analyse_sans_spacy(self):
        with sans_tiers():
            self.assertIsNone(moteurs.analyse('Une phrase.'))

    def test_entites_regles(self):
        with sans_tiers():
            resultat = moteurs.entites(TEXTE)
        trouvees = {(e['type'], e['text']) for e in resultat}
        for attendu in [('personne', 'Dupont'), ('organisation', 'Insee'), ('pourcentage', '3,5 %'), ('date', '2024'),
                        ('pourcentage', '12 %'), ('nom_propre', 'J. R. R. Tolkien'), ('montant', "1,2 milliard d'euros"),
                        ('montant', '45 000 € HT'), ('date', '1er mars 2025')]:
            self.assertIn(attendu, trouvees)
        for entite in resultat:
            self.assertEqual(TEXTE[entite['start']:entite['end']], entite['text'])
            self.assertEqual(entite['engine'], 'regles')
        with sans_tiers():
            dates = moteurs.entites(TEXTE, types=['date'])
        self.assertTrue(dates and all(e['type'] == 'date' for e in dates))

    def test_entites_noms_composes(self):
        texte = 'Selon Emmanuel Macron, la Banque de France et la SNCF restent prudentes. Récemment, Mme Anne Hidalgo a parlé.'
        trouvees = {(e['type'], e['text']) for e in moteurs.entites_regles(texte)}
        self.assertIn(('nom_propre', 'Emmanuel Macron'), trouvees)
        self.assertIn(('organisation', 'Banque de France'), trouvees)
        self.assertIn(('organisation', 'SNCF'), trouvees)
        self.assertIn(('personne', 'Anne Hidalgo'), trouvees)
        self.assertNotIn('Selon', {t for _, t in trouvees})
        self.assertNotIn('Récemment', {t for _, t in trouvees})

    def test_embeddings_aucun_puis_api(self):
        with sans_tiers():
            self.assertEqual(moteurs.embeddings(['a', 'b']), ([], 'aucun'))
        s.CONFIG.update(embeddings=True, embedding_model='emb-test')
        reponse = lambda texte: {'model': 'lmstudio:emb-test', 'values': [float(len(texte)), 1.0]}
        with sans_tiers(), patch.object(s, 'embed', side_effect=reponse):
            vecteurs, moteur = moteurs.embeddings(['a', 'bbb'])
        self.assertEqual(moteur, 'api:lmstudio:emb-test')
        self.assertEqual(vecteurs, [[1.0, 1.0], [3.0, 1.0]])

    def test_embeddings_api_en_erreur(self):
        s.CONFIG.update(embeddings=True, embedding_model='emb-test')
        with sans_tiers(), patch.object(s, 'embed', side_effect=s.OperationalError('Connexion API impossible')):
            self.assertEqual(moteurs.embeddings(['a']), ([], 'aucun'))

    def test_reordonner_lexical_puis_cosinus(self):
        passages = ['Le taux de chômage est en hausse en 2024.', 'Les ventes de voitures progressent.', '']
        with sans_tiers():
            scores, moteur = moteurs.reordonner('hausse du chômage en 2024', passages, avec_moteur=True)
        self.assertEqual(moteur, 'lexical')
        self.assertEqual(len(scores), 3)
        self.assertGreater(scores[0], scores[1])
        self.assertTrue(all(0.0 <= x <= 1.0 for x in scores))
        s.CONFIG.update(embeddings=True, embedding_model='emb-test')
        vecteurs = {'q': [1.0, 0.0], 'a': [1.0, 0.1], 'b': [0.0, 1.0]}
        with sans_tiers(), patch.object(s, 'embed', side_effect=lambda t: {'model': 'm', 'values': vecteurs[t]}):
            scores, moteur = moteurs.reordonner('q', ['a', 'b'], avec_moteur=True)
        self.assertEqual(moteur, 'cosinus:api:m')
        self.assertGreater(scores[0], scores[1])
        self.assertEqual(moteurs.reordonner('q', []), [])

    def test_perplexite_absente(self):
        with sans_tiers():
            self.assertIsNone(moteurs.perplexite('Une requête naturelle ?'))

    def test_classer_lexical(self):
        exemples = {
            'etat_courant': ['Le prix actuel est de 10 euros.', 'Le dirigeant actuel est Paul.', 'Le taux en vigueur est de 3 %.'],
            'annonce': ['Le chantier débutera en 2027.', 'La ligne sera ouverte en septembre.', 'Le projet sera lancé bientôt.'],
            'vide': [],
        }
        self.reglage('moteur:classer', 'regles')
        with sans_tiers():
            resultat = moteurs.classer(['Le prix actuel du ticket est de 2 euros.', 'Le tunnel sera ouvert en 2028.'],
                                       ['etat_courant', 'annonce', 'vide'], exemples)
        self.assertEqual([r['label'] for r in resultat], ['etat_courant', 'annonce'])
        self.assertTrue(all(r['engine'] == 'lexical' for r in resultat))
        liste = [('Le prix actuel est de 10 euros.', 'etat_courant'), ('Le chantier débutera en 2027.', 'annonce')]
        with sans_tiers():
            self.assertEqual(moteurs.classer(['Le prix actuel'], ['etat_courant', 'annonce'], liste)[0]['label'], 'etat_courant')

    def test_etat_sans_dependances(self):
        with sans_tiers():
            resultat = moteurs.etat()
        self.assertEqual(set(resultat), set(moteurs.NIVEAUX))
        self.assertEqual(resultat['phrases']['actif'], 'regles')
        self.assertIn('spacy', resultat['phrases']['raison_repli'])
        self.assertEqual(resultat['nli']['actif'], 'llm:lmstudio:test-model')
        self.assertEqual(resultat['embeddings']['actif'], 'aucun')
        self.assertEqual(resultat['perplexite']['actif'], 'aucun')
        self.assertEqual(resultat['classer']['actif'], 'lexical')   # regles avant LLM (relecture M1)
        cles = {'actif', 'disponible', 'modele', 'version', 'raison_repli', 'mode', 'charge'}
        for nom, valeurs in resultat.items():
            # nli : + double_passe (forte | faible | refusee | impossible), relecture M1.
            self.assertEqual(set(valeurs), cles | ({'double_passe'} if nom == 'nli' else set()))
        self.assertEqual(resultat['nli']['double_passe'], 'refusee')

    def test_aucun_import_tiers_au_chargement(self):
        source = Path(moteurs.__file__).read_text(encoding='utf-8')
        entete = source.split('\nMODES = ')[0]
        for paquet in ('spacy', 'gliner', 'transformers', 'torch', 'sentence_transformers', 'setfit', 'numpy', 'wtpsplit'):
            self.assertNotIn('import ' + paquet, entete)


# ---------------------------------------------------------------------------
# NLI par LLM simule
# ---------------------------------------------------------------------------

class NliLlmTests(Base):

    def test_nli_par_llm(self):
        appels = []

        def faux_llm(tache, payload):
            appels.append(payload)
            self.assertEqual(payload['mode'], 'chaine:nli')
            resultats = []
            for paire in payload['paires']:
                label = 'implication' if paire['hypothese'] in paire['premisse'] else 'neutre'
                resultats.append({'id': paire['id'], 'label': label, 'score': 0.9})
            return {'resultats': resultats}
        with sans_tiers(), patch.object(s, 'llm', side_effect=faux_llm):
            resultat = moteurs.nli_lot([('Paris est en France.', 'Paris est en France'), ('Paris est en France.', 'Lyon est grande')] * 5)
            seul = moteurs.nli('A et B.', 'A')
        self.assertEqual(len(resultat), 10)
        self.assertEqual(len(appels), 3)
        self.assertEqual(resultat[0]['label'], 'implication')
        self.assertEqual(resultat[1]['label'], 'neutre')
        self.assertAlmostEqual(sum(resultat[0]['probas'].values()), 1.0)
        self.assertEqual(resultat[0]['engine'], 'llm:lmstudio:test-model')
        self.assertEqual(seul['label'], 'implication')
        self.assertIn('chaine:nli', noyau.modes())

    def test_nli_id_manquant_redemande(self):
        reponses = [
            {'resultats': [{'id': 0, 'label': 'contradiction', 'score': 0.8}]},
            {'resultats': [{'id': 0, 'label': 'neutre', 'score': 0.6}]},
        ]
        with sans_tiers(), patch.object(s, 'llm', side_effect=reponses):
            resultat = moteurs.nli_lot([('a', 'b'), ('c', 'd')])
        self.assertEqual([r['label'] for r in resultat], ['contradiction', 'neutre'])

    def test_nli_sortie_invalide(self):
        with sans_tiers(), patch.object(s, 'llm', return_value={'resultats': [{'id': 0, 'label': 'oui', 'score': 1}]}):
            with self.assertRaises(s.OperationalError):
                moteurs.nli('a', 'b')

    def test_nli_sans_aucun_moteur(self):
        s.CONFIG.update(model='')
        with sans_tiers():
            with self.assertRaises(s.OperationalError):
                moteurs.nli('a', 'b')
        self.reglage('moteur:nli', 'regles')
        s.CONFIG.update(model='test-model')
        with sans_tiers(), patch.object(s, 'llm', return_value={'resultats': [{'id': 0, 'label': 'neutre', 'score': 0.5}]}):
            self.assertEqual(moteurs.nli('a', 'b')['label'], 'neutre')

    def test_classer_par_llm(self):
        self.reglage('moteur:classer', 'llm')

        def faux_llm(tache, payload):
            self.assertEqual(noyau.schema_pour(payload)['properties']['resultats']['items']['properties']['label']['enum'],
                             ['opinion', 'sans_source'])
            return {'resultats': [{'id': t['id'], 'label': 'opinion', 'score': 0.7} for t in payload['textes']]}
        with sans_tiers(), patch.object(s, 'llm', side_effect=faux_llm):
            # Avec des exemples, le centroide lexical passe avant le LLM (relecture M1).
            lexical = moteurs.classer(['Il faut agir.'], ['opinion', 'sans_source'], {'opinion': ['Je pense que oui.']})
            # Sans exemple, seul le LLM peut classer.
            resultat = moteurs.classer(['Il faut agir.'], ['opinion', 'sans_source'], {})
        self.assertEqual(lexical[0]['engine'], 'lexical')
        self.assertEqual(resultat[0]['label'], 'opinion')
        self.assertEqual(resultat[0]['engine'], 'llm:lmstudio:test-model')


# ---------------------------------------------------------------------------
# Moteurs locaux simules (doubles des bibliotheques)
# ---------------------------------------------------------------------------

class FauxTenseur:
    """Tenseur minimal : liste de listes."""

    def __init__(self, valeurs):
        self.valeurs = valeurs
        self.shape = (len(valeurs), len(valeurs[0]) if valeurs and isinstance(valeurs[0], list) else 0)

    def tolist(self):
        return self.valeurs


class FauxSansGradient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def faux_torch():
    module = types.ModuleType('torch')
    module.no_grad = FauxSansGradient

    def softmax(tenseur, dim=-1):
        lignes = []
        for ligne in tenseur.valeurs:
            exps = [math.exp(x) for x in ligne]
            lignes.append([x / sum(exps) for x in exps])
        return FauxTenseur(lignes)
    module.softmax = softmax
    return module


def faux_transformers():
    module = types.ModuleType('transformers')

    class Tokeniseur:
        @classmethod
        def from_pretrained(cls, nom):
            return cls()

        def __call__(self, premieres, secondes=None, **options):
            if secondes is None:
                return {'input_ids': FauxTenseur([[1] * max(2, len(premieres.split()))])}
            return {'paires': list(zip(premieres, secondes))}

    class ModeleNli:
        config = types.SimpleNamespace(id2label={0: 'ENTAILMENT', 1: 'NEUTRAL', 2: 'CONTRADICTION'})

        @classmethod
        def from_pretrained(cls, nom):
            return cls()

        def eval(self):
            return self

        def __call__(self, paires):
            logits = []
            for premisse, hypothese in paires:
                if hypothese in premisse:
                    logits.append([4.0, 0.0, 0.0])
                elif 'pas' in hypothese:
                    logits.append([0.0, 0.0, 4.0])
                else:
                    logits.append([0.0, 4.0, 0.0])
            return types.SimpleNamespace(logits=FauxTenseur(logits))

    class ModeleCausal:
        @classmethod
        def from_pretrained(cls, nom):
            return cls()

        def eval(self):
            return self

        def __call__(self, input_ids, labels=None):
            return types.SimpleNamespace(loss=math.log(42.0))

    module.AutoTokenizer = Tokeniseur
    module.AutoModelForSequenceClassification = ModeleNli
    module.AutoModelForCausalLM = ModeleCausal
    return module


def faux_sentence_transformers():
    module = types.ModuleType('sentence_transformers')

    class SentenceTransformer:
        def __init__(self, nom):
            self.nom = nom

        def encode(self, textes, batch_size=16, normalize_embeddings=True, show_progress_bar=False):
            vecteurs = []
            for texte in textes:
                v = [texte.count(c) + 0.01 for c in 'aeiou']
                norme = math.sqrt(sum(x * x for x in v))
                vecteurs.append(FauxTenseur([x / norme for x in v]))
            return vecteurs

    class CrossEncoder:
        def __init__(self, nom, max_length=512):
            self.nom = nom

        def predict(self, paires):
            return FauxTenseur([3.0 if requete in passage else -3.0 for requete, passage in paires])

    module.SentenceTransformer = SentenceTransformer
    module.CrossEncoder = CrossEncoder
    return module


def faux_gliner():
    module = types.ModuleType('gliner')

    class GLiNER:
        @classmethod
        def from_pretrained(cls, nom):
            return cls()

        def predict_entities(self, texte, etiquettes, threshold=0.5):
            position = texte.find('Paris')
            if position < 0 or 'lieu' not in etiquettes:
                return []
            return [{'start': position, 'end': position + 5, 'text': 'Paris', 'label': 'lieu', 'score': 0.93}]

    module.GLiNER = GLiNER
    return module


def faux_setfit():
    setfit = types.ModuleType('setfit')
    datasets = types.ModuleType('datasets')
    datasets.Dataset = types.SimpleNamespace(from_dict=lambda d: d)

    class Tete:
        classes_ = FauxTenseur([1, 0])

    class SetFitModel:
        model_head = Tete()

        @classmethod
        def from_pretrained(cls, base):
            return cls()

        def predict_proba(self, textes):
            return [FauxTenseur([0.2, 0.8]) if 'sera' in t else FauxTenseur([0.9, 0.1]) for t in textes]

    class Trainer:
        def __init__(self, model, args, train_dataset):
            self.donnees = train_dataset

        def train(self):
            return None

    setfit.SetFitModel = SetFitModel
    setfit.Trainer = Trainer
    setfit.TrainingArguments = lambda **options: options
    return {'setfit': setfit, 'datasets': datasets}


@contextmanager
def doubles(**modules):
    """Installe des modules simules et masque les vrais modeles."""
    with patch.dict(sys.modules, modules), patch.object(moteurs, '_modele_hf_present', lambda depot: None):
        moteurs.reinitialiser()
        yield
    moteurs.reinitialiser()


class MoteursLocauxSimulesTests(Base):

    def test_gliner_et_selection(self):
        with doubles(gliner=faux_gliner()):
            resultat = moteurs.entites('Il vit à Paris depuis 2020.')
            self.assertEqual(resultat[0]['engine'], 'gliner:urchade/gliner_multi-v2.1')
            self.assertEqual(resultat[0]['text'], 'Paris')
            self.reglage('moteur:entites', 'regles')
            resultat = moteurs.entites('Il vit à Paris depuis 2020.')
            self.assertTrue(all(e['engine'] == 'regles' for e in resultat))
            self.assertIn('exclu par le reglage', moteurs.etat()['entites']['raison_repli'])

    def test_gliner_en_echec_de_chargement(self):
        cassé = types.ModuleType('gliner')

        class GLiNER:
            @classmethod
            def from_pretrained(cls, nom):
                raise OSError('modele introuvable hors ligne')
        cassé.GLiNER = GLiNER
        with doubles(gliner=cassé, spacy=None):
            resultat = moteurs.entites('Il vit à Paris.')
            self.assertEqual(moteurs.dernier('entites'), 'regles')
            self.assertTrue(resultat)
            etat = moteurs.etat()['entites']
        self.assertEqual(etat['actif'], 'regles')
        self.assertIn('modele introuvable', etat['raison_repli'])

    def test_nli_local(self):
        with doubles(transformers=faux_transformers(), torch=faux_torch()):
            resultat = moteurs.nli_lot([('Paris est en France.', 'Paris est en France'), ('A.', 'pas A'), ('A.', 'B')])
        self.assertEqual([r['label'] for r in resultat], ['implication', 'contradiction', 'neutre'])
        self.assertTrue(all(r['engine'] == 'transformers:' + moteurs.MODELES['nli'] for r in resultat))
        self.assertGreater(resultat[0]['score'], 0.9)
        self.assertAlmostEqual(sum(resultat[2]['probas'].values()), 1.0)

    def test_nli_reglage_llm_saute_le_local(self):
        self.reglage('moteur:nli', 'llm')
        with doubles(transformers=faux_transformers(), torch=faux_torch()), \
                patch.object(s, 'llm', return_value={'resultats': [{'id': 0, 'label': 'neutre', 'score': 0.5}]}):
            self.assertEqual(moteurs.nli('a', 'b')['engine'], 'llm:lmstudio:test-model')

    def test_embeddings_et_reranker_locaux(self):
        with doubles(sentence_transformers=faux_sentence_transformers()):
            vecteurs, moteur = moteurs.embeddings(['maison', 'avion'])
            scores, moteur_reranker = moteurs.reordonner('taux', ['le taux monte', 'autre chose'], avec_moteur=True)
        self.assertEqual(moteur, 'sentence_transformers:Qwen/Qwen3-Embedding-0.6B')
        self.assertEqual(len(vecteurs), 2)
        self.assertAlmostEqual(moteurs.cosinus(vecteurs[0], vecteurs[0]), 1.0)
        self.assertEqual(moteur_reranker, 'cross_encoder:BAAI/bge-reranker-v2-m3')
        self.assertGreater(scores[0], 0.9)
        self.assertLess(scores[1], 0.1)

    def test_classer_centroide_et_setfit(self):
        exemples = {'annonce': ['Le pont sera ouvert.'], 'etat_courant': ['Le prix est de 3 euros.']}
        with doubles(sentence_transformers=faux_sentence_transformers()):
            resultat = moteurs.classer(['Le pont sera ouvert demain.'], ['annonce', 'etat_courant'], exemples)
        self.assertTrue(resultat[0]['engine'].startswith('centroide:sentence_transformers:'))
        self.assertIn(resultat[0]['label'], ('annonce', 'etat_courant'))
        with doubles(**faux_setfit()):
            resultat = moteurs.classer(['Le pont sera ouvert.', 'Le prix est fixe.'], ['annonce', 'etat_courant'], exemples)
        self.assertEqual([r['label'] for r in resultat], ['annonce', 'etat_courant'])
        self.assertTrue(resultat[0]['engine'].startswith('setfit:'))

    def test_perplexite_locale(self):
        with doubles(transformers=faux_transformers(), torch=faux_torch()):
            self.assertAlmostEqual(moteurs.perplexite('Le périphérique sera-t-il fermé en août 2026 ?'), 42.0)

    def test_sat(self):
        wtpsplit = types.ModuleType('wtpsplit')

        class SaT:
            def __init__(self, nom):
                self.nom = nom

            def split(self, texte):
                morceaux = texte.split('|')
                return [m + '|' if i < len(morceaux) - 1 else m for i, m in enumerate(morceaux)]
        wtpsplit.SaT = SaT
        with doubles(wtpsplit=wtpsplit, spacy=None):
            offsets, moteur = moteurs.phrases('Un. | Deux.', avec_moteur=True)
        self.assertEqual(moteur, 'sat:sat-3l-sm')
        self.assertEqual(offsets, [(0, 5), (6, 11)])


@unittest.skipUnless(moteurs.paquet_present('spacy') and any(moteurs.paquet_present(m) for m in moteurs.REPLIS_SPACY),
                     'spaCy et un modele francais ne sont pas installes')
class SpacyReelTests(Base):

    def test_phrases_entites_analyse(self):
        texte = 'Selon Emmanuel Macron, la Banque de France reste prudente. Le taux atteint 3,5 % en 2024.\nTitre'
        offsets, moteur = moteurs.phrases(texte, avec_moteur=True)
        self.assertTrue(moteur.startswith('spacy:fr_core_news_'))
        self.assertEqual([texte[d:f] for d, f in offsets][-1], 'Titre')
        self.assertEqual(len(offsets), 3)
        resultat = moteurs.entites(texte)
        self.assertIn(('personne', 'Emmanuel Macron'), {(e['type'], e['text']) for e in resultat})
        self.assertIn(('pourcentage', '3,5 %'), {(e['type'], e['text']) for e in resultat})
        doc = moteurs.analyse(texte)
        self.assertTrue(any(t.dep_ for t in doc))
        self.assertTrue(moteurs.etat()['phrases']['charge'])


if __name__ == '__main__':
    unittest.main()
