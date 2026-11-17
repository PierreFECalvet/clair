from types import SimpleNamespace
from unittest.mock import patch
from test_chaine_m5 import M5Base, m5, s, moteurs, JOUR, html, reponse, resultats, nli_par_mots


class TranchesTests(M5Base):
    def verifier(self, soutien_des_le_debut):
        _, qid = self.pdg()
        requete = s.query('SELECT * FROM queries WHERE id=?', (qid,), True)
        urls = ['https://source%d.fr/page' % i for i in range(20)]
        contenus = {url: html('B dirige X en 2026.' if (i >= 10 or soutien_des_le_debut)
                             else 'Le conseil de X se reunit en 2026.', '2026-10-01')
                    for i, url in enumerate(urls)}
        compteurs = dict(appels=0, unites_ibou=0, cache=0, elargies=0)
        budget = self.budget(3)
        with patch.object(m5, '_http', return_value=reponse(200, resultats(*urls))) as http, \
             patch.object(m5, '_telecharger', side_effect=lambda url: contenus[url]) as pages, \
             patch.object(m5, 'filtrer', side_effect=lambda docs, *args: [dict(d, kept=True, poids=0.6) for d in docs]), \
             patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('B dirige', None, 'implication')])):
            decisions = m5._chercher_par_tranches(requete, budget, SimpleNamespace(id=None), JOUR, {}, compteurs)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(http.call_args.args[2]['max_results'], 20)
        self.assertEqual(self.utilise(budget), 3)
        self.assertEqual(compteurs['appels'], 1)
        self.assertEqual(decisions['g1'], 'confirmee')
        return pages.call_count, compteurs

    def test_premiers_suffisants_aucun_telechargement_de_la_reserve(self):
        pages, compteurs = self.verifier(True)
        self.assertEqual(pages, 10)
        self.assertEqual(compteurs['elargies'], 0)

    def test_reserve_analysee_sans_second_appel_ni_double_depense(self):
        pages, compteurs = self.verifier(False)
        self.assertEqual(pages, 20)
        self.assertEqual(compteurs['elargies'], 1)

    def test_pas_de_reserve_si_ibou_renvoie_moins_de_dix(self):
        compteurs = dict(appels=0, unites_ibou=0, cache=0, elargies=0)
        with patch.object(m5, '_appel_compte', return_value={'call_id': 'c', 'resultats': 7}) as appel, \
             patch.object(m5, '_traiter_appel', return_value={'g': 'insuffisante'}) as traiter:
            m5._chercher_par_tranches({'id': 'q'}, 'b', None, JOUR, {}, compteurs)
        self.assertEqual(appel.call_count, 1)
        self.assertEqual(traiter.call_count, 1)
        self.assertEqual(compteurs['elargies'], 0)

    def test_seuls_les_groupes_insuffisants_sont_elargis(self):
        compteurs = dict(appels=0, unites_ibou=0, cache=0, elargies=0)
        with patch.object(m5, '_appel_compte', return_value={'call_id': 'c', 'resultats': 20}), \
             patch.object(m5, '_traiter_appel', side_effect=[{'a': 'confirmee', 'b': 'insuffisante'},
                                                          {'b': 'confirmee'}]) as traiter:
            d = m5._chercher_par_tranches({'id': 'q'}, 'b', None, JOUR, {}, compteurs)
        self.assertEqual(traiter.call_args.kwargs['groupes'], ['b'])
        self.assertEqual(traiter.call_args.kwargs['debut'], 10)
        self.assertEqual(d, {'a': 'confirmee', 'b': 'confirmee'})

    def test_filtre_voit_les_deux_tranches(self):
        docs = [{'rang': i, 'kept': True, 'url': str(i)} for i in range(20)]
        with patch.object(m5, 'recuperer', return_value=docs), \
             patch.object(m5, 'contexte_groupe', return_value={}), \
             patch.object(m5, 'filtrer', return_value=docs) as filtre, \
             patch.object(m5, 'passages') as passages, \
             patch.object(m5, 'verdict', return_value={'verdict': 'insuffisante'}):
            m5._traiter_appel({'call_id': 'c'}, {'id': 'q'}, SimpleNamespace(id=None), JOUR, {},
                             debut=10, fin=20, groupes=['g'])
        self.assertEqual(len(filtre.call_args.args[0]), 20)
        self.assertEqual([d['rang'] for d in passages.call_args.args[0]], list(range(10, 20)))
