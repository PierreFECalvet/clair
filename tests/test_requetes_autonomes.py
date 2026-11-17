"""Regressions issues des requetes relues le 15 novembre 2026."""
import unittest
from unittest.mock import patch

from test_chaine_m4 import Base, JOUR
from chaine import m4, noyau


class Autonomie(Base):
    def test_article_ne_decoupe_pas_les_ni_les_noms_propres(self):
        self.assertEqual(m4.de(m4.groupe_nominal('les bornes')), 'des bornes')
        self.assertEqual(m4.groupe_nominal('Lehman')['texte'], 'Lehman')
        self.assertEqual(m4.groupe_nominal('LaMDA')['texte'], 'LaMDA')

    def test_statut_generique_n_est_plus_pret(self):
        cadre = {'sujet': 'Google', 'predicat': 'statut', 'classe': 'etat_courant',
                 'texte': 'Google ne confirme pas utiliser SimHash.', 'valeur': 'non confirme'}
        _, controle, statut, essais = m4._preparer_requete(
            'Quel est le statut de Google en 2026 ?', cadre, 'ouverte', False)
        self.assertEqual(statut, 'a_revoir')
        self.assertTrue(any('objet' in p or 'generique' in p for p in controle['problemes']))

    def test_fonction_n_est_pas_un_evenement(self):
        cadre = {'predicat': 'a_pour_objectif', 'texte': 'hostAge sert a freiner le spam recent.'}
        self.assertTrue(m4.problemes_autonomie('Le hostAge a-t-il tenu ses objectifs en 2024 ?', cadre, True))

    def test_date_historique_non_transposee(self):
        cadre = {'texte': 'En 1998, Google utilise des barrels.', 'predicat': 'utilise'}
        self.assertTrue(any('periode' in p for p in m4.problemes_autonomie(
            'Sur quoi repose Google en 2026 ?', cadre, True)))

    def test_references_et_fragments_refuses(self):
        self.assertTrue(m4.problemes_autonomie('Que fait ce mécanisme ?'))
        self.assertTrue(m4.problemes_autonomie('Quels sont les effets du s bornes ?'))
        self.assertTrue(m4.problemes_autonomie('Comment Google traite les liens'))

    def test_homographes_et_noms_techniques(self):
        source = 'À Paris, la méthode publiée emploie pagerank_ns et PRE_DOC, selon le schéma.'
        question = 'Comment Google a-t-il documenté pagerank_ns et PRE_DOC ?'
        self.assertEqual(m4.regles(question, source), [])
        self.assertEqual(m4.regles('Quel vocabulaire schema.org décrit-il pour les pages web ?', source), [])
        self.assertTrue(m4.regles('Comment Google emploie-t-il code_invente ?', source))

    def test_reformulation_recoit_le_contexte_et_resout_le_sujet(self):
        cadre = {'sujet': 'le mécanisme', 'texte': 'Le mécanisme utilise des annotations.',
                 'predicat': 'utilise', 'classe': 'etat_courant', 'valeur': 'annotations',
                 'contextes': [{'titre': 'Versions linguistiques',
                                'passage': 'Google documente hreflang et les versions linguistiques.'}]}
        with patch.object(noyau, 'llm', return_value={
                'requete': 'Comment Google traite-t-il les versions linguistiques avec hreflang ?'}) as appel:
            resultat = m4.reformuler('Sur quoi repose le mécanisme ?', ['sujet implicite'], cadre)
        self.assertFalse(resultat['echec'])
        payload = appel.call_args.args[1]
        self.assertEqual(payload['affirmation'], cadre['texte'])
        self.assertEqual(payload['contextes'], cadre['contextes'])
        self.assertEqual(payload['relation']['predicat'], 'utilise')

    def test_sujet_precis_ne_peut_pas_etre_remplace(self):
        self.assertFalse(m4.sujet_contextualise_present('Comment Bing classe-t-il les documents ?',
            {'sujet': 'Google', 'contextes': [{'passage': 'Bing classe des documents.'}]}))

    def test_chargement_du_passage_et_fusion(self):
        texte = 'Le système hreflang de Google regroupe les versions linguistiques. Les règles sont publiques.'
        version = self.page('https://example.test/hreflang', texte, titre='Hreflang')
        uid = self.unite(version, 'Les règles sont publiques.', span_start=66, span_end=len(texte))
        unites, *_ = m4._charger_contexte([uid])
        self.assertIn('hreflang', unites[uid]['_contexte']['passage'])
        self.assertEqual(unites[uid]['_contexte']['titre'], 'Hreflang')


if __name__ == '__main__':
    unittest.main()
