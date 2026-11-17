"""Content selection and atomic coverage; no network or paid model calls."""
import json
from unittest.mock import patch
from test_chaine_m5 import M5Base, m5, s, moteurs, JOUR, nli_par_mots


class SelectionTests(M5Base):
    def test_selection_et_nli_en_desaccord_bloquent(self):
        self.pdg()
        for host in ('a', 'b'):
            self.preuve('g1', 'https://' + host + '.fr/1', 'B dirige X.', '2026-10-01')
        def selection(preuve, fait):
            return {'type': 'selection', 'fact_id': fait['id'], 'unit_id': fait['unit_id'],
                    'position': 'contredit', 'citation': preuve['text'], 'motif': 'Erreur simulee', 'retenue': True}
        with patch.object(m5, 'selectionner_preuve', side_effect=selection), patch.object(
                moteurs, 'nli_lot', side_effect=nli_par_mots([('B', None, 'implication')])):
            decision = m5.verdict('g1', JOUR)
        self.assertEqual(decision['verdict'], 'conflictuelle')
        self.assertTrue(all(p['nli'] == 'neutre' for p in s.query('SELECT nli FROM evidence')))

    def test_citation_inventee_refusee(self):
        self.selection.stop()
        fait = {'id': 'f', 'unit_id': 'u', 'phrase': 'Le brevet expire en 2026.'}
        with patch.object(m5.noyau, 'llm', return_value={
                'position': 'soutient', 'citation': 'Expiration en 2026', 'motif': 'date'}):
            r = m5.selectionner_preuve({'text': 'Un brevet concerne une invention.'}, fait)
        self.assertFalse(r['retenue'])
        self.assertEqual(r['position'], 'inexploitable')

    def test_generalite_non_retenue(self):
        self.selection.stop()
        with patch.object(m5.noyau, 'llm', return_value={
                'position': 'partiel', 'citation': '', 'motif': 'Aucune date pour US8924380.'}):
            r = m5.selectionner_preuve({'text': 'Les brevets ont une duree limitee.'},
                                      {'id': 'f', 'unit_id': 'u', 'phrase': 'US8924380 expire en 2026.'})
        self.assertFalse(r['retenue'])
        self.assertEqual(r['position'], 'partiel')

    def test_indisponibilite_ne_valide_pas(self):
        self.selection.stop()
        with patch.object(m5.noyau, 'llm', side_effect=RuntimeError('hors ligne')):
            r = m5.selectionner_preuve({'text': 'X est dirige par B.'},
                                      {'id': 'f', 'unit_id': 'u', 'phrase': 'X est dirige par B.'})
        self.assertFalse(r['retenue'])

    def test_citation_exacte_conservee(self):
        self.selection.stop()
        with patch.object(m5.noyau, 'llm', return_value={
                'position': 'soutient', 'citation': 'X a pour dirigeant B.', 'motif': 'Relation explicite.'}):
            r = m5.selectionner_preuve({'text': 'Introduction. X a pour dirigeant B. Suite.'},
                                      {'id': 'f', 'unit_id': 'u', 'phrase': 'X a pour dirigeant B.'})
        self.assertTrue(r['retenue'])
        self.assertEqual(r['span'], [14, 35])

    def test_un_fait_non_couvert_bloque_confirmation_du_groupe(self):
        unite, _ = self.pdg()
        self.fait(unite, 'X', 'date_de_debut', '2020', 'EX', verbalization='X a ete cree en 2020')
        for host in ('a', 'b'):
            self.preuve('g1', 'https://' + host + '.fr/1', 'B dirige X.', '2026-10-01')
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('B', 'dirigeant', 'implication')])):
            decision = m5.verdict('g1', JOUR)
            m5.reecrire('g1', JOUR)
        self.assertEqual(decision['verdict'], 'insuffisante')
        self.assertEqual(len(decision['faits']), 2)
        self.assertEqual(s.query("SELECT count(*) n FROM sources WHERE kind='verification'", one=True)['n'], 0)
        journal = json.loads(s.query('SELECT extracted FROM evidence LIMIT 1', one=True)['extracted'])
        self.assertEqual(len([e for e in journal if e['type'] == 'selection']), 2)

    def test_deux_faits_deux_soutiens_chacun(self):
        unite, _ = self.pdg()
        self.fait(unite, 'X', 'date_de_debut', '2020', 'EX', verbalization='X a ete cree en 2020')
        for host in ('a', 'b'):
            self.preuve('g1', 'https://' + host + '.fr/1', 'B dirige X, cree en 2020.', '2026-10-01')
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('B', None, 'implication')])):
            decision = m5.verdict('g1', JOUR)
        self.assertEqual(decision['verdict'], 'confirmee')
        self.assertTrue(all(d['verdict'] == 'confirmee' for d in decision['faits']))

    def test_contradiction_meme_date_non_ignoree(self):
        self.pdg()
        for host in ('a', 'b'):
            self.preuve('g1', 'https://' + host + '.fr/1', 'B dirige X.', '2026-10-01')
        self.preuve('g1', 'https://c.fr/1', 'Claire dirige X.', '2026-10-01')
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([
                ('Claire', None, 'contradiction'), ('B', None, 'implication')])):
            decision = m5.verdict('g1', JOUR)
        self.assertEqual(decision['verdict'], 'conflictuelle')

    def test_source_se_contredisant_bloque(self):
        self.pdg()
        for host in ('a', 'b'):
            self.preuve('g1', 'https://' + host + '.fr/1', 'B dirige X.', '2026-10-01')
        self.preuve('g1', 'https://a.fr/2', 'Claire dirige X.', '2026-10-01')
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([
                ('Claire', None, 'contradiction'), ('B', None, 'implication')])):
            decision = m5.verdict('g1', JOUR)
        self.assertEqual(decision['verdict'], 'conflictuelle')
