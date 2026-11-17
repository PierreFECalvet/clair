from unittest.mock import patch
import json
from test_chaine_m5 import M5Base, m5, moteurs, s, JOUR, nli_par_mots


class CacheTests(M5Base):
    def setUp(self):
        super().setUp()
        m5._ANALYSES.clear()

    def test_reprise_sans_reselection_et_attribution_conservee(self):
        self.pdg()
        for host in ('a', 'b'):
            self.preuve('g1', 'https://' + host + '.fr/1', 'B dirige X.', '2026-10-01')
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('B', None, 'implication')])):
            premier = m5.verdict('g1', JOUR)
            self.assertEqual(m5.selectionner_preuve.call_count, 1)
            m5._ANALYSES.clear()  # Simulate restart: reload from the existing evidence journal.
            second = m5.verdict('g1', JOUR)
            self.assertEqual(m5.selectionner_preuve.call_count, 1)
        self.assertEqual(premier['verdict'], 'confirmee')
        self.assertEqual(second['verdict'], 'confirmee')
        self.assertEqual(set(second['soutiens']), {'a.fr', 'b.fr'})

    def test_invalidation_par_contenu_et_modele(self):
        preuve = {'text': 'B dirige X.'}
        fait = {'id': 'f', 'unit_id': 'u', 'phrase': 'B dirige X.'}
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('B', None, 'implication')])):
            self.assertFalse(m5._selection_reutilisable(preuve, fait)[1])
            self.assertTrue(m5._selection_reutilisable(preuve, fait)[1])
            self.assertFalse(m5._selection_reutilisable({'text': 'B dirige X depuis 2025.'}, fait)[1])
            with patch.dict(s.CONFIG, {'model': 'autre-modele'}):
                self.assertFalse(m5._selection_reutilisable(preuve, fait)[1])
            nouveau = dict(fait, fait={'scope': 'France'})
            self.assertFalse(m5._selection_reutilisable(preuve, nouveau)[1])

    def test_erreur_non_memorisee(self):
        with patch.object(m5, 'selectionner_preuve', return_value={'position': 'inexploitable', 'retenue': False}) as call:
            for _ in range(2):
                m5._selection_reutilisable({'text': 'texte'}, {'id': 'f', 'unit_id': 'u', 'phrase': 'fait'})
            self.assertEqual(call.call_count, 2)

    def test_nli_dedoublonne(self):
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('B', None, 'implication')])) as call:
            paires = [('B dirige X.', 'B dirige X.')] * 2
            self.assertEqual(len(m5._nli_reutilisable(paires)), 2)
            m5._nli_reutilisable(paires)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(len(call.call_args.args[0]), 1)

    def test_embeddings_reutilises(self):
        moteurs._CACHE_EMBEDDINGS_API.clear()
        with patch.object(s, 'embed', return_value={'model': 'test', 'values': [1., 0.]}) as call:
            moteurs._embeddings_api(['texte', 'texte'])
            moteurs._embeddings_api(['texte'])
            self.assertEqual(call.call_count, 1)
            with patch.dict(s.CONFIG, {'embedding_model': 'autre'}):
                moteurs._embeddings_api(['texte'])
            self.assertEqual(call.call_count, 2)

    def test_restauration_journal_complet_uniquement(self):
        with patch.dict(s.CONFIG, {'provider': 'openai', 'model': 'test-restauration'}):
            s.query('INSERT INTO step_runs(id,step,started,prompt) VALUES(?,?,?,?)',
                    ('r-cache', 'm5.verification', s.now(), 'm5-v2-preuves-par-fait'))
            for passage in ('B dirige X.', 'B dirige X. ' * 100):
                payload = {'mode': 'chaine:m5_selection', 'fait': 'B dirige X.', 'sujet': 'X',
                           'relation': 'a_pour_dirigeant', 'periode': None, 'perimetre': None, 'passage': passage}
                s.query('INSERT INTO api_log(id,service,model,status,request,response,step) VALUES(?,?,?,?,?,?,?)',
                        (s.uid(), 'openai', 'test-restauration', 'success', s.llm_preview(payload),
                         json.dumps({'position': 'soutient', 'citation': 'B dirige X.', 'motif': 'explicite'}), 'r-cache'))
            self.assertEqual(m5._restaurer_analyses(), 1)
            _, reutilisee = m5._selection_reutilisable({'text': 'B dirige X.'},
                {'id': 'f', 'unit_id': 'u', 'phrase': 'B dirige X.', 'sujet': 'X', 'predicat': 'a_pour_dirigeant'})
            self.assertTrue(reutilisee)
