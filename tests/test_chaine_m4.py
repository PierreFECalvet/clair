"""Tests du lot M4 : statuts finaux, cadres, gabarits, controle de l'aspect humain, reformulation, modification."""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s
import chaine
from chaine import m4
from chaine import moteurs
from chaine import noyau
from chaine import schema
from chaine import seuils

JOUR = date(2026, 11, 13)
PERIPH = ('La voie gauche du périphérique parisien sera immobilisée du 1er au 31 août 2026 entre la porte Maillot '
          'et la porte de la Chapelle pour des travaux de rénovation.')
QUESTION_PERIPH = 'Le périphérique parisien a-t-il été fermé ou modifié en août 2026 ? Quelles voies et quelles dates ?'


class Base(unittest.TestCase):
    """Base temporaire initialisee par le serveur puis par la chaine (comme le lot fondation)."""

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
        m4._GSC['cle'] = None
        self.perplexite = patch.object(moteurs, 'perplexite', return_value=None)
        self.perplexite.start()
        # Configuration simulee declaree : un NLI local distinct du LLM (double passe forte, relecture M1).
        self.nli_prevu = patch.object(moteurs, 'nli_prevu', return_value='test:nli')
        self.nli_prevu.start()

    def tearDown(self):
        self.nli_prevu.stop()
        self.perplexite.stop()
        s.CONFIG.clear()
        s.CONFIG.update(self.config)
        moteurs.reinitialiser()
        noyau.vider_reglages()
        seuils._vider_cache()
        self.tmp.cleanup()

    # --- fabriques -------------------------------------------------------
    def page(self, url, texte, publiee='2026-05-02', titre='Titre'):
        page_id = s.uid()
        version_id = s.uid()
        s.query('INSERT INTO pages(id,url,title) VALUES(?,?,?)', (page_id, url, titre))
        s.query('INSERT INTO versions(id,page_id,text,created,published,hash) VALUES(?,?,?,?,?,?)',
                (version_id, page_id, texte, s.now(), publiee, s.uid()))
        return version_id

    def unite(self, version_id, texte, groupe=None, **champs):
        identifiant = champs.pop('id', None) or s.uid()
        valeurs = {'id': identifiant, 'version_id': version_id, 'text': texte, 'quote': texte, 'status': 'a_verifier',
                   'temporal_status': 'a_jour', 'excluded': 0, 'created': s.now()}
        valeurs.update(champs)
        s.query('INSERT INTO units(' + ','.join(valeurs) + ') VALUES(' + ','.join('?' for _ in valeurs) + ')',
                tuple(valeurs.values()))
        groupe = groupe or identifiant
        s.query('INSERT OR IGNORE INTO unit_groups(id,label,created) VALUES(?,?,?)', (groupe, texte[:80], s.now()))
        s.query('INSERT OR IGNORE INTO unit_group_members(group_id,unit_id) VALUES(?,?)', (groupe, identifiant))
        return identifiant

    def fait(self, unit_id, sujet, predicat, objet, subject_id=None, **champs):
        identifiant = s.uid()
        valeurs = {'id': identifiant, 'unit_id': unit_id, 'subject': sujet, 'subject_id': subject_id, 'predicate': predicat,
                   'object': objet, 'origin': 'explicite', 'status': 'valide', 'created': s.now()}
        valeurs.update(champs)
        s.query('INSERT INTO facts(' + ','.join(valeurs) + ') VALUES(' + ','.join('?' for _ in valeurs) + ')',
                tuple(valeurs.values()))
        return identifiant

    def entite(self, identifiant, nom, type_entite):
        s.query('INSERT INTO entities(id,name,type,created) VALUES(?,?,?,?)', (identifiant, nom, type_entite, s.now()))

    def hypothese(self, unit_id, kind, texte, predicat=None):
        s.query('INSERT INTO hypotheses(id,unit_id,kind,text,predicate,created) VALUES(?,?,?,?,?,?)',
                (s.uid(), unit_id, kind, texte, predicat, s.now()))

    def requetes_de(self, unit_id):
        return s.query('SELECT q.* FROM queries q JOIN query_units qu ON qu.query_id=q.id WHERE qu.unit_id=? '
                       "AND COALESCE(q.template,'')!='ancien-pipeline' ORDER BY q.kind DESC", (unit_id,))

    def peripherique(self, critique=0, temporel='perimee_par_date', hypotheses=True):
        """L'exemple du document : huit faits sur la voie gauche du peripherique parisien."""
        version = self.page('https://site-audite.fr/peripherique', 'Travaux\n\n' + PERIPH)
        unite = self.unite(version, PERIPH, temporal_status=temporel, obs_class='annonce', anchor_date='2026-08-01',
                           anchor_kind='phrase', expires='2026-08-31', critical=critique)
        self.entite('E1', 'périphérique parisien', 'infrastructure')
        self.entite('E2', 'voie gauche', 'infrastructure')
        self.fait(unite, 'voie gauche', 'fait_partie_de', 'périphérique parisien', 'E2')
        self.fait(unite, 'voie gauche', 'sera_ferme', 'du 1er au 31 août 2026', 'E2')
        self.fait(unite, 'voie gauche', 'date_de_debut', '2026-08-01', 'E2')
        self.fait(unite, 'voie gauche', 'date_de_fin', '2026-08-31', 'E2')
        self.fait(unite, 'E1', 'est_situe_a', 'Paris', 'E1')
        self.fait(unite, 'immobilisation', 'a_pour_objectif', 'des travaux de rénovation')
        self.fait(unite, 'voie gauche', 'statut', 'immobilisée', 'E2')
        self.fait(unite, 'E1', 'a_pour_limite', '50 km/h', 'E1')
        if hypotheses:
            self.hypothese(unite, 'information_inconnue', 'voies concernées')
            self.hypothese(unite, 'information_inconnue', 'dates précises')
            self.hypothese(unite, 'connaissance_externe', 'Le périphérique est géré par la Ville de Paris', 'est_gere_par')
        return unite


class StatutsTests(Base):
    def test_tableau_des_statuts_ligne_par_ligne(self):
        version = self.page('https://site-audite.fr/a', 'Texte')
        opinion = self.unite(version, 'Il faut fermer le périphérique.', sourcing='opinion')
        sourcee = self.unite(version, "Le chômage est de 7,3 % selon l'Insee.")
        s.query("INSERT INTO sources(id,unit_id,cited_entity,kind,scope,confidence) VALUES('s1',?,'Insee','source_nommee','phrase',0.9)",
                (sourcee,))
        sans_source = self.unite(version, 'X compte 200 salariés.')
        historique = self.unite(version, 'Le PDG de X est A.', temporal_status='obsolete_historique')
        conflit = self.unite(version, 'Le PDG de Y est C.', temporal_status='remise_en_question')
        perimee = self.unite(version, 'Le salon aura lieu en mars 2026.', temporal_status='perimee_par_date')
        poule = self.unite(version, 'La poule pond des œufs.')
        s.query("INSERT INTO claim_types(unit_id,type,confidence) VALUES(?,'connaissance_generale',0.9)", (poule,))
        interpretation = self.unite(version, 'Donc X recrute.')
        s.query("INSERT INTO claim_types(unit_id,type,confidence,depends_on) VALUES(?,'interpretation',0.9,?)",
                (interpretation, json.dumps([sans_source, sourcee])))
        immuable = self.unite(version, 'Victor Hugo a écrit Les Misérables.', obs_class='immuable')
        lignes = {l['unit_id']: l for l in m4.statuts()}
        self.assertEqual(lignes[opinion]['statut'], 'non_verifiable')
        self.assertFalse(lignes[opinion]['requete'])
        self.assertEqual(lignes[sourcee]['statut'], 'sourcee')
        self.assertFalse(lignes[sourcee]['requete'])
        self.assertEqual(lignes[sans_source]['statut'], 'a_verifier')
        self.assertTrue(lignes[sans_source]['requete'])
        self.assertEqual(lignes[historique]['statut'], 'historique')
        self.assertFalse(lignes[historique]['requete'])
        self.assertEqual(lignes[conflit]['statut'], 'a_verifier')
        self.assertEqual(lignes[perimee]['statut'], 'a_verifier')
        self.assertEqual(lignes[poule]['statut'], 'a_verifier_faible')
        self.assertTrue(lignes[poule]['requete'])
        self.assertAlmostEqual(lignes[poule]['priorite'], seuils.get('m4.priorite.connaissance_generale'))
        self.assertEqual(lignes[interpretation]['statut'], 'dependante')
        self.assertFalse(lignes[interpretation]['requete'])
        self.assertEqual(lignes[interpretation]['suit'], 'a_verifier')
        self.assertIsNone(lignes[immuable]['statut'])
        self.assertEqual(s.query('SELECT status FROM units WHERE id=?', (sourcee,), True)['status'], 'sourcee')

    def test_sourcee_mais_source_plus_ancienne_que_la_periode(self):
        version = self.page('https://site-audite.fr/b', 'Texte')
        unite = self.unite(version, "Le taux de chômage est de 7,3 % en 2026 selon l'Insee.", anchor_date='2026-01-01',
                           anchor_kind='phrase')
        self.fait(unite, 'taux de chômage', 'taux', '7,3 %', period='2026')
        s.query("INSERT INTO sources(id,unit_id,url,domain,kind,scope,source_date) VALUES('s2',?,'https://insee.fr/x',"
                "'insee.fr','lien_externe','phrase','2024-05-01')", (unite,))
        ligne = m4.statuts([unite])[0]
        self.assertEqual(ligne['statut'], 'sourcee')
        self.assertTrue(ligne['requete'])
        self.assertIn('plus ancienne', ligne['raison'])

    def test_verdicts_m5_sortent_de_la_file(self):
        version = self.page('https://site-audite.fr/c', 'Texte')
        confirmee = self.unite(version, 'X compte 200 salariés.', verdict='confirmee')
        corrigee = self.unite(version, 'X compte 300 salariés.', verdict='corrigee')
        lignes = {l['unit_id']: l for l in m4.statuts()}
        self.assertEqual(lignes[confirmee]['statut'], 'sourcee')
        self.assertEqual(lignes[corrigee]['statut'], 'a_verifier')
        self.assertFalse(lignes[corrigee]['requete'])


class GabaritsTests(Base):
    def test_annonce_echue_au_passe_avec_inconnues(self):
        cadre = {'sujet': 'périphérique parisien', 'famille': 'fermeture', 'inconnues': ['voies', 'dates'],
                 'periode': m4.periode_elargie('2026-08-01', '2026-08-31', 'jour')}
        self.assertEqual(m4.gabarit(cadre, 'annonce', JOUR), QUESTION_PERIPH)

    def test_annonce_non_echue_au_futur(self):
        cadre = {'sujet': 'périphérique parisien', 'famille': 'fermeture', 'inconnues': [],
                 'periode': m4.periode_elargie('2026-08-01', '2026-08-31', 'jour')}
        self.assertEqual(m4.gabarit(cadre, 'annonce', date(2026, 5, 13)),
                         'Le périphérique parisien sera-t-il concerné par des fermetures en août 2026 ?')

    def test_etat_courant_question_ouverte(self):
        self.assertEqual(m4.gabarit({'sujet': 'X', 'predicat': 'a_pour_dirigeant'}, 'etat_courant', JOUR),
                         'Qui dirige X en 2026 ?')
        self.assertEqual(m4.gabarit({'sujet': 'abonnement Navigo', 'predicat': 'prix'}, 'etat_courant', JOUR),
                         "Quel est le prix de l'abonnement Navigo en 2026 ?")

    def test_connaissance_generale_question_fermee(self):
        self.assertEqual(m4.gabarit({'texte': 'La poule pond des œufs.'}, 'connaissance_generale', JOUR),
                         "Est-ce qu'une poule pond des œufs ?")

    def test_accords_et_contractions(self):
        self.assertEqual(m4.gabarit({'sujet': 'ligne 14', 'famille': 'fermeture', 'perimee': True,
                                     'periode': m4.periode_elargie('2026-07-01', '2026-07-15', 'jour')}, 'annonce', JOUR),
                         'La ligne 14 a-t-elle été fermée ou modifiée en juillet 2026 ?')
        self.assertEqual(m4.periode_elargie('2026-07-20', '2026-08-10', 'jour')['libelle'], 'entre juillet et août 2026')
        self.assertEqual(m4.de(m4.groupe_nominal('périphérique')), 'du périphérique')
        self.assertEqual(m4.a(m4.groupe_nominal('entreprises')), 'aux entreprises')


class RequetesTests(Base):
    def test_huit_faits_du_peripherique_une_seule_requete(self):
        unite = self.peripherique(critique=0)
        compteurs = m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        requetes = self.requetes_de(unite)
        self.assertEqual(len(s.query('SELECT * FROM facts WHERE unit_id=?', (unite,))), 8)
        self.assertEqual(compteurs['cadres'], 1)
        self.assertEqual(len(requetes), 1)
        self.assertEqual(requetes[0]['text'], QUESTION_PERIPH)
        self.assertEqual(requetes[0]['kind'], 'ouverte')
        self.assertEqual(requetes[0]['status'], 'prete')
        self.assertEqual(requetes[0]['template'], 'annonce:passe:fermeture')
        cadre = json.loads(requetes[0]['frame'])
        self.assertEqual(cadre['sujet'], 'périphérique parisien')
        self.assertIsNone(cadre['lieu'])
        self.assertNotIn('E1', requetes[0]['text'])

    def test_deux_pages_meme_cadre_une_requete(self):
        premiere = self.peripherique(critique=0)
        version = self.page('https://site-audite.fr/autre', 'Autre')
        seconde = self.unite(version, 'Le périphérique parisien sera fermé en août 2026.', temporal_status='perimee_par_date',
                             critical=0)
        self.fait(seconde, 'périphérique parisien', 'sera_ferme', 'en août 2026', 'E1')
        m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        ouvertes = s.query("SELECT * FROM queries WHERE kind='ouverte'")
        self.assertEqual(len(ouvertes), 1)
        couvertes = {l['unit_id'] for l in s.query('SELECT unit_id FROM query_units WHERE query_id=?', (ouvertes[0]['id'],))}
        self.assertEqual(couvertes, {premiere, seconde})

    def test_unite_critique_requete_ouverte_puis_controle(self):
        unite = self.peripherique(critique=1)
        m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        par_type = {r['kind']: r for r in self.requetes_de(unite)}
        self.assertEqual(par_type['ouverte']['text'], QUESTION_PERIPH)
        self.assertEqual(par_type['controle']['text'],
                         'Le périphérique parisien a-t-il été fermé du 1er au 31 août 2026 ?')

    def test_criticite_calculee_si_absente(self):
        unite = self.peripherique(critique=None)
        m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        ligne = s.query('SELECT critical, critical_score FROM units WHERE id=?', (unite,), True)
        self.assertIsNotNone(ligne['critical'])
        self.assertIsNotNone(ligne['critical_score'])

    def test_pdg_succession_question_ouverte_sans_valeur(self):
        version = self.page('https://site-audite.fr/x', 'Texte')
        self.entite('EX', 'X', 'organisation')
        ancienne = self.unite(version, 'Le PDG de X est A.', anchor_date='2023-03-01', anchor_kind='phrase',
                              temporal_status='obsolete_historique', critical=1)
        recente = self.unite(version, 'Le PDG de X est B.', anchor_date='2025-03-01', anchor_kind='phrase', critical=1,
                             obs_class='etat_courant')
        self.fait(ancienne, 'X', 'a_pour_dirigeant', 'A', 'EX')
        self.fait(recente, 'X', 'a_pour_dirigeant', 'B', 'EX')
        m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        self.assertEqual(self.requetes_de(ancienne), [])
        par_type = {r['kind']: r for r in self.requetes_de(recente)}
        self.assertEqual(par_type['ouverte']['text'], 'Qui dirige X en 2026 ?')
        self.assertNotIn(' B ', ' ' + par_type['ouverte']['text'] + ' ')
        self.assertEqual(par_type['controle']['text'], 'Est-ce que X a pour dirigeant B en 2026 ?')

    def test_poule_priorite_minimale(self):
        version = self.page('https://site-audite.fr/p', 'Texte')
        poule = self.unite(version, 'La poule pond des œufs.', critical=0)
        s.query("INSERT INTO claim_types(unit_id,type,confidence) VALUES(?,'connaissance_generale',0.9)", (poule,))
        m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        requete = self.requetes_de(poule)[0]
        self.assertEqual(requete['text'], "Est-ce qu'une poule pond des œufs ?")
        self.assertAlmostEqual(requete['priority'], seuils.get('m4.priorite.connaissance_generale'))

    def test_unite_migree_sans_fait_et_ancienne_requete_abandonnee(self):
        version = self.page('https://site-audite.fr/m', 'Texte')
        unite = self.unite(version, 'Google utilise plus de 100 signaux bruts.', groupe='claimA', extractor='migration',
                           critical=0)
        s.query("INSERT INTO queries(id,group_id,text,kind,template,status) VALUES('old','claimA',"
                "'google signaux bruts nombre classement','ouverte','ancien-pipeline','prete')")
        s.query("INSERT INTO query_units(query_id,unit_id) VALUES('old',?)", (unite,))
        with patch.object(moteurs, 'entites', return_value=[{'text': 'Google', 'type': 'organisation', 'start': 0, 'end': 6}]):
            compteurs = m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        self.assertEqual(compteurs['anciennes_abandonnees'], 1)
        self.assertEqual(s.query("SELECT status FROM queries WHERE id='old'", one=True)['status'], 'abandonnee')
        # Ni fait ni cadre : aucune requete envoyable (jamais l'affirmation telle quelle), unite en attente de M1 faits.
        self.assertEqual(compteurs['en_attente_faits'], 1)
        self.assertEqual(self.requetes_de(unite), [])
        # Repli reactive explicitement par reglage : la question fermee sur l'unite revient.
        noyau.definir_reglage(m4.REGLAGE_REPLI, '1')
        with patch.object(moteurs, 'entites', return_value=[{'text': 'Google', 'type': 'organisation', 'start': 0, 'end': 6}]):
            compteurs = m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        self.assertEqual(compteurs['en_attente_faits'], 0)
        requete = self.requetes_de(unite)[0]
        self.assertEqual(requete['text'], 'Est-ce que Google utilise plus de 100 signaux bruts ?')
        self.assertEqual(requete['template'], 'unite:fermee')
        self.assertEqual(json.loads(requete['frame'])['sujet'], 'Google')
        # Desactive a nouveau : la requete de repli non envoyee est abandonnee.
        noyau.definir_reglage(m4.REGLAGE_REPLI, None)
        with patch.object(moteurs, 'entites', return_value=[{'text': 'Google', 'type': 'organisation', 'start': 0, 'end': 6}]):
            compteurs = m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        self.assertEqual(compteurs['replis_abandonnes'], 1)
        self.assertEqual(s.query('SELECT status FROM queries WHERE id=?', (requete['id'],), True)['status'], 'abandonnee')

    def test_reprise_ne_recalcule_pas_et_respecte_les_modifications(self):
        unite = self.peripherique(critique=0)
        m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        requete = self.requetes_de(unite)[0]
        resultat = m4.modifier(requete['id'], 'Le périphérique parisien a-t-il été fermé en août 2026 ?')
        self.assertTrue(resultat['ok'])
        compteurs = m4.requetes(aujourdhui=JOUR, reformuler_llm=False, forcer=True)
        self.assertEqual(compteurs['conservees'], 1)
        self.assertEqual(self.requetes_de(unite)[0]['text'], 'Le périphérique parisien a-t-il été fermé en août 2026 ?')


class ControleTests(Base):
    def problemes(self, texte):
        return m4.controler(texte)['problemes']

    def test_exemples_du_document_passent(self):
        for texte in (QUESTION_PERIPH, 'Qui dirige X en 2026 ?', "Est-ce qu'une poule pond des œufs ?",
                      'Le périphérique parisien sera-t-il concerné par des fermetures en août 2026 ?'):
            self.assertEqual(self.problemes(texte), [], texte)

    def test_regles_bloquantes(self):
        self.assertTrue(any('caracteres' in p for p in self.problemes('Le périphérique est-il fermé ? ' + 'é' * 200)))
        self.assertTrue(any('mots' in p for p in self.problemes('Périphérique fermé ?')))
        self.assertTrue(any('mots' in p for p in self.problemes(' '.join(['mot'] * 31) + ' ?')))
        self.assertTrue(any('identifiant' in p for p in self.problemes('Est-ce que E1 a été fermée en août 2026 ?')))
        self.assertTrue(any('identifiant' in p for p in self.problemes('Qui est le a_pour_dirigeant de X en 2026 ?')))
        self.assertTrue(any('identifiant' in p for p in self.problemes('Où est la page 3f9a2b7c1d4e5f60 en 2026 ?')))
        self.assertTrue(any('JSON' in p for p in self.problemes('Qui dirige {"sujet": "X"} en 2026 ?')))
        self.assertTrue(any('questions' in p for p in self.problemes('Qui dirige X ? Depuis quand ? Avec qui ?')))
        self.assertTrue(any('accents' in p for p in self.problemes('Le peripherique a-t-il ete ferme en aout 2026 ?')))
        self.assertTrue(any('ISO' in p for p in self.problemes('Le périphérique a-t-il été fermé le 2026-08-01 ?')))
        self.assertEqual(self.problemes('Les codes de région suivent-ils la norme ISO 3166-1 ?'), [])

    def test_terme_de_la_source_n_est_pas_un_identifiant(self):
        question = 'Est-ce que CETR obtient un F1 moyen de 94 % ?'
        self.assertTrue(any('identifiant' in p for p in m4.controler(question)['problemes']))
        self.assertTrue(m4.controler(question, source='CETR obtient un F1 moyen de 94 %.')['ok'])
        self.assertTrue(m4.controler('Que signifie unavailable_after pour Google ?', source='unavailable_after')['ok'])
        self.assertFalse(m4.controler('Que signifie unavailable_after pour Google ?')['ok'])

    def test_question_fermee_minuscule_sauf_nom_propre(self):
        self.assertEqual(m4.question_fermee('Placé dans des tests, le diff signale une erreur.', noms_propres=[]),
                         'Est-ce que placé dans des tests, le diff signale une erreur ?')
        self.assertEqual(m4.question_fermee('Yandex répartit son index.', noms_propres=['Yandex']),
                         'Est-ce que Yandex répartit son index ?')
        self.assertEqual(m4.question_fermee('PageRank reste un signal.', noms_propres=[]),
                         'Est-ce que PageRank reste un signal ?')
        self.assertEqual(m4.question_fermee('[Hypothèse] Le prior pèserait peu.', noms_propres=[]),
                         'Est-ce que le prior pèserait peu ?')

    def test_score_de_naturel_perplexite(self):
        with patch.object(moteurs, 'perplexite', return_value=50.0):
            resultat = m4.controler('Qui dirige X en 2026 ?')
        self.assertTrue(resultat['ok'])
        self.assertEqual(resultat['score'], 1.0)
        with patch.object(moteurs, 'perplexite', return_value=5000.0):
            resultat = m4.controler('Qui dirige X en 2026 ?')
        self.assertFalse(resultat['ok'])
        self.assertTrue(any('naturel' in p for p in resultat['problemes']))

    def test_score_de_naturel_export_gsc(self):
        noyau.definir_reglage('gsc:requetes', json.dumps(['fermeture périphérique parisien août 2026', 'travaux périphérique']))
        resultat = m4.controler('Le périphérique parisien a-t-il été fermé en août 2026 ?')
        self.assertIn('similarite_gsc', resultat['composantes'])
        self.assertTrue(resultat['ok'])
        sans_rapport = m4.controler('Combien de calories contient une banane mûre ?')
        self.assertFalse(sans_rapport['ok'])

    def test_reformulation_unique_puis_revue(self):
        version = self.page('https://site-audite.fr/r', 'Texte')
        texte = ('Selon les auteurs, le moteur de recherche Google combine plusieurs centaines de signaux de classement '
                 'issus de systèmes distincts, réglés par des ingénieurs au-dessus des sorties de modèles appris, '
                 'et cette combinaison change à chaque mise à jour majeure.')
        unite = self.unite(version, texte, critical=0)
        noyau.definir_reglage(m4.REGLAGE_REPLI, '1')  # unite sans fait : le gabarit de repli doit etre reactive
        appels = []

        def llm_ok(tache, payload):
            appels.append(payload)
            return {'requete': 'Comment Google combine-t-il ses signaux de classement en 2026 ?'}
        with patch.object(moteurs, 'entites', return_value=[{'text': 'Google', 'type': 'organisation', 'start': 0, 'end': 6}]), \
                patch.object(s, 'llm', side_effect=llm_ok):
            m4.requetes(aujourdhui=JOUR)
        requete = self.requetes_de(unite)[0]
        self.assertEqual(len(appels), 1)
        self.assertEqual(appels[0]['mode'], 'chaine:m4_reformulation')
        self.assertEqual(requete['status'], 'prete')
        self.assertEqual(requete['text'], 'Comment Google combine-t-il ses signaux de classement en 2026 ?')
        self.assertEqual(requete['attempts'], 1)

    def test_second_echec_gabarit_brut_a_revoir(self):
        version = self.page('https://site-audite.fr/r2', 'Texte')
        texte = 'Le moteur ' + ' '.join(['Google'] * 40) + ' utilise des signaux.'
        unite = self.unite(version, texte, critical=0)
        noyau.definir_reglage(m4.REGLAGE_REPLI, '1')  # unite sans fait : le gabarit de repli doit etre reactive
        with patch.object(moteurs, 'entites', return_value=[{'text': 'Google', 'type': 'organisation', 'start': 0, 'end': 6}]), \
                patch.object(s, 'llm', return_value={'requete': 'Quels signaux utilise-t-il ?'}):
            m4.requetes(aujourdhui=JOUR)
        requete = self.requetes_de(unite)[0]
        self.assertEqual(requete['status'], 'a_revoir')
        self.assertEqual(requete['text'], m4.question_fermee(texte))
        checks = json.loads(requete['checks'])
        self.assertTrue(checks['brut_conserve'])
        self.assertIn('ne nomme plus le sujet', ' '.join(checks['reformulation_rejetee']['problemes']))

    def test_reformulation_ne_doit_pas_presupposer_la_valeur(self):
        cadre = {'sujet': 'X', 'valeur': 'Jean Dupont', 'classe': 'etat_courant'}
        with patch.object(s, 'llm', return_value={'requete': 'Jean Dupont dirige-t-il X en 2026 ?'}):
            resultat = m4.reformuler('Qui dirige X 2026-01-01 ?', ['date au format ISO'], cadre, 'ouverte')
        self.assertTrue(resultat['echec'])


class ModifierTests(Base):
    def test_modifier_applique_les_controles_et_le_sujet(self):
        unite = self.peripherique(critique=0)
        m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        requete = self.requetes_de(unite)[0]
        refus = m4.modifier(requete['id'], 'La voie a-t-elle été fermée en août 2026 ?')
        self.assertFalse(refus['ok'])
        self.assertTrue(any('sujet' in p for p in refus['problemes']))
        refus = m4.modifier(requete['id'], 'Le peripherique a-t-il ete ferme en aout 2026 ?')
        self.assertFalse(refus['ok'])
        accepte = m4.modifier(requete['id'], 'Quelles fermetures du périphérique parisien en août 2026 ?')
        self.assertTrue(accepte['ok'])
        ligne = s.query('SELECT * FROM queries WHERE id=?', (requete['id'],), True)
        self.assertEqual(ligne['edited'], 1)
        self.assertEqual(ligne['status'], 'prete')


if __name__ == '__main__':
    unittest.main()
