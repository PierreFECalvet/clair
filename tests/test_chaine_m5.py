"""Tests du lot M5 : criticite, appels Ibou (cache, 429, 503, seuil, plafond), circularite, filtre temporel,
passages, verdicts, reecriture. Ibou, HTTP, NLI, reranker et LLM sont simules ; aucun appel reseau."""
import json
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as s
from chaine import m4
from chaine import m5
from chaine import moteurs
from chaine import noyau
from chaine import simhash
from test_chaine_m4 import Base, JOUR, PERIPH

QUESTION = 'Qui dirige X en 2026 ?'


def reponse(code, corps, entetes=None):
    """Reponse HTTP simulee pour ``m5._http``."""
    brut = corps if isinstance(corps, bytes) else json.dumps(corps).encode()
    return code, brut, dict(entetes or {})


def resultats(*urls):
    """Corps Ibou /passage."""
    return {'results': [{'url': u, 'title': 'Titre ' + u, 'passages': [{'text': 'Passage Ibou de ' + u}]} for u in urls]}


def html(texte, publiee=None):
    """Page HTML simulee, avec date JSON-LD."""
    ld = ('<script type="application/ld+json">{"@type":"NewsArticle","datePublished":"' + publiee + '"}</script>'
          if publiee else '')
    corps = ''.join('<p>' + p + '</p>' for p in texte.split('\n\n'))
    return '<html><head><title>Page</title>' + ld + '</head><body>' + corps + '</body></html>'


class M5Base(Base):
    def setUp(self):
        super().setUp()
        m5._ETAT['pages'].clear()
        m5._ETAT['site'] = {'cle': None, 'empreintes': {}}
        m5._GSC_CLICS['cle'] = None
        self.attentes = []
        self.attente = patch.object(m5, '_attendre', side_effect=self.attentes.append)
        self.attente.start()
        self.solde = patch.object(m5, '_solde', return_value=1000)
        self.solde.start()
        self.reordonner = patch.object(moteurs, 'reordonner', side_effect=lambda q, ps, avec_moteur=False: [0.9] * len(ps))
        self.reordonner.start()
        # Semantic selection is tested separately; legacy verdict tests isolate aggregation.
        def selection(preuve, fait):
            nli = moteurs.nli(preuve['text'], fait['phrase'])
            position = {'implication': 'soutient', 'contradiction': 'contredit'}.get(nli['label'], 'hors_sujet')
            return {'type': 'selection', 'version': m5.VERSION, 'fact_id': fait['id'],
                    'unit_id': fait['unit_id'], 'affirmation': fait['phrase'], 'position': position,
                    'citation': preuve['text'], 'motif': 'selection simulee',
                    'retenue': position in ('soutient', 'contredit')}
        self.selection = patch.object(m5, 'selectionner_preuve', side_effect=selection)
        self.selection.start()
        noyau.definir_reglage('site:domaines', 'site-audite.fr')

    def tearDown(self):
        self.selection.stop()
        self.attente.stop()
        self.solde.stop()
        self.reordonner.stop()
        super().tearDown()

    def budget(self, plafond):
        identifiant = s.uid()
        s.query('INSERT INTO budgets(id,cap,used,created) VALUES(?,?,0,?)', (identifiant, plafond, s.now()))
        return identifiant

    def utilise(self, budget):
        return s.query('SELECT used FROM budgets WHERE id=?', (budget,), True)['used']

    def pdg(self, critique=1, texte=QUESTION):
        """Unite « Le PDG de X est B. » (2025) avec sa requete ouverte M4."""
        version = self.page('https://site-audite.fr/x', 'Le PDG de X est B.\n\nSuite.')
        self.entite('EX', 'X', 'organisation')
        # Critique au sens de l'etape 1 : centrale, en tete de page, remise en question par M3.
        unite = self.unite(version, 'Le PDG de X est B.', groupe='g1', anchor_date='2025-03-01', anchor_kind='phrase',
                           obs_class='etat_courant', critical=critique, centrality=1.0 if critique else 0.0, span_start=0,
                           temporal_status='remise_en_question' if critique else 'a_jour')
        self.fait(unite, 'X', 'a_pour_dirigeant', 'B', 'EX', verbalization='X a pour dirigeant B')
        m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        requete = s.query("SELECT * FROM queries WHERE kind='ouverte'", one=True)
        if texte != requete['text']:
            s.query('UPDATE queries SET text=? WHERE id=?', (texte, requete['id']))
        return unite, requete['id']

    def preuve(self, groupe, url, texte, date_page, query_id='q', relevance=0.9, poids=0.6):
        identifiant = s.uid()
        s.query('INSERT INTO evidence(id,claim_id,url,title,text,external_chunk,created,query_id,group_id,domain,page_date,'
                'passage_start,relevance,kept,weight) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)',
                (identifiant, groupe, url, 'T', texte, '', s.now(), query_id, groupe, m5.domaine(url), date_page, 0,
                 relevance, poids))
        return identifiant


def nli_par_mots(regles):
    """NLI simule : premiere regle (mot du passage, mot de l'hypothese) -> label."""
    def fonction(paires):
        sortie = []
        for premisse, hypothese in paires:
            label = 'neutre'
            for mot_premisse, mot_hypothese, valeur in regles:
                if mot_premisse in premisse and (mot_hypothese is None or mot_hypothese in hypothese):
                    label = valeur
                    break
            sortie.append({'label': label, 'score': 0.95, 'probas': {}, 'engine': 'test'})
        return sortie
    return fonction


class CriticiteTests(M5Base):
    def test_score_et_file_plus_tard(self):
        perimee = self.peripherique(critique=None)
        version = self.page('https://site-audite.fr/b', 'Intro longue de la page avec un point.\n\nAutre paragraphe.\n\n'
                            'Paragraphe lointain : X compte 200 salariés.')
        banale = self.unite(version, 'X compte 200 salariés.', span_start=80, anchor_date='2026-10-01', anchor_kind='phrase')
        scores = m5.evaluer_criticite([perimee, banale], JOUR, ecrire=True)
        self.assertEqual(scores[perimee]['critical'], 1)
        self.assertEqual(scores[banale]['critical'], 0)
        self.assertEqual(scores[perimee]['composantes']['statut'], 1.0)
        self.assertEqual(scores[perimee]['composantes']['classe'], 1.0)
        s.query('UPDATE units SET critical=NULL')
        noyau.definir_reglage(m4.REGLAGE_REPLI, '1')  # « X compte 200 salaries » n'a pas de fait : repli reactive
        m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        compteurs = m5.criticite(JOUR)
        self.assertEqual(compteurs['plus_tard'], 1)
        statuts = {r['text'][:3]: r['status'] for r in s.query("SELECT text, status FROM queries WHERE kind='ouverte'")}
        self.assertEqual(sorted(statuts.values()), ['plus_tard', 'prete'])

    def test_audience_gsc_et_centralite(self):
        version = self.page('https://site-audite.fr/c', 'Texte')
        unite = self.unite(version, 'X compte 200 salariés.', centrality=0.9)
        noyau.definir_reglage('gsc:clics', json.dumps({'https://site-audite.fr/c': 5000, 'https://site-audite.fr/z': 10}))
        score = m5.evaluer_criticite([unite], JOUR, ecrire=False)[unite]
        self.assertAlmostEqual(score['composantes']['centralite'], 0.9)
        self.assertAlmostEqual(score['composantes']['audience'], 1.0)


class AppelerTests(M5Base):
    def test_cache_par_empreinte_ne_paie_pas_deux_fois(self):
        _, requete = self.pdg()
        budget = self.budget(30)
        with patch.object(m5, '_http', return_value=reponse(200, resultats('https://a.fr/1'), {'X-Units-Remaining': '900'})) as http:
            premier = m5.appeler(requete, budget)
            s.query("UPDATE queries SET status='prete' WHERE id=?", (requete,))
            second = m5.appeler(requete, budget)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.utilise(budget), 3)
        self.assertEqual(second['statut'], 'cache')
        ligne = s.query('SELECT * FROM ibou_calls WHERE id=?', (second['call_id'],), True)
        self.assertEqual(ligne['cached_from'], premier['call_id'])
        self.assertEqual(ligne['units'], 0)
        origine = s.query('SELECT * FROM ibou_calls WHERE id=?', (premier['call_id'],), True)
        self.assertEqual(origine['units_remaining'], 900)
        self.assertEqual(json.loads(origine['headers'])['X-Units-Remaining'], '900')
        self.assertEqual(json.loads(origine['response'])['results'][0]['url'], 'https://a.fr/1')
        self.assertEqual(origine['max_results'], 10)
        journal = s.query("SELECT * FROM api_log WHERE service='ibou' ORDER BY created")
        self.assertEqual(len(journal), 2)
        self.assertEqual([l['units'] for l in journal], [3, 0])

    def test_corps_envoye_et_max_results(self):
        _, requete = self.pdg()
        budget = self.budget(30)
        with patch.object(m5, '_http', return_value=reponse(200, resultats())) as http:
            m5.appeler(requete, budget, max_results=50)
        methode, url, corps, entetes = http.call_args[0]
        self.assertEqual(url, 'https://platform.ibou.com/api/v1/passage')
        self.assertEqual(corps, {'query': QUESTION, 'max_results': 20})
        self.assertEqual(entetes, {'X-API-Key': 'test-only'})

    def test_429_solde_epuise_arrete_la_file(self):
        _, requete = self.pdg()
        budget = self.budget(30)
        corps = {'error': {'code': 'insufficient_units', 'message': 'Not enough units left on this key'}}
        with patch.object(m5, '_http', return_value=reponse(429, corps)) as http:
            with self.assertRaises(m5.ArretFile):
                m5.appeler(requete, budget)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.utilise(budget), 0)
        self.assertEqual(s.query('SELECT status FROM ibou_calls', one=True)['status'], 'solde_epuise')

    def test_429_debit_pause_puis_nouvel_essai(self):
        _, requete = self.pdg()
        budget = self.budget(30)
        sequence = [reponse(429, {'error': 'Rate limit: too many requests'}, {'Retry-After': '7'}),
                    reponse(200, resultats('https://a.fr/1'))]
        with patch.object(m5, '_http', side_effect=sequence):
            resultat = m5.appeler(requete, budget)
        self.assertEqual(resultat['statut'], 'success')
        self.assertIn(7.0, self.attentes)
        self.assertEqual(self.utilise(budget), 3)
        self.assertEqual([l['status'] for l in s.query('SELECT status FROM ibou_calls ORDER BY created, rowid')],
                         ['debit', 'success'])

    def test_503_respecte_retry_after(self):
        _, requete = self.pdg()
        budget = self.budget(30)
        sequence = [reponse(503, b'Service Unavailable', {'Retry-After': '5'}),
                    reponse(503, b'Service Unavailable', {'Retry-After': '999'}),
                    reponse(200, resultats())]
        with patch.object(m5, '_http', side_effect=sequence):
            m5.appeler(requete, budget)
        self.assertIn(5.0, self.attentes)
        self.assertIn(120.0, self.attentes)
        self.assertEqual(self.utilise(budget), 3)   # les 503 sont rembourses (relecture, mineur)

    def test_arret_sous_le_seuil_unites_restantes(self):
        _, requete = self.pdg()
        budget = self.budget(30)
        s.query("INSERT INTO ibou_calls(id,endpoint,status,units_remaining,created) VALUES('ancien','passage','success',12,?)",
                (s.now(),))
        with patch.object(m5, '_http') as http:
            with self.assertRaises(m5.ArretFile):
                m5.appeler(requete, budget)
        self.assertEqual(http.call_count, 0)
        self.assertEqual(self.utilise(budget), 0)

    def test_ancienne_requete_migree_jamais_envoyee(self):
        s.query("INSERT INTO queries(id,text,kind,template,status) VALUES('old','question ancienne ?','ouverte',"
                "'ancien-pipeline','prete')")
        with patch.object(m5, '_http') as http:
            with self.assertRaises(s.OperationalError):
                m5.appeler('old', self.budget(30))
        self.assertEqual(http.call_count, 0)

    def test_retry_after_date_http(self):
        self.assertEqual(m5.delai_retry_after('Wed, 21 Oct 2015 07:28:00 GMT'), 0.0)
        self.assertEqual(m5.delai_retry_after(None), 2.0)
        self.assertEqual(m5.nature_429(b'{"error":{"message":"Solde d\'unit\\u00e9s \\u00e9puis\\u00e9"}}'), 'solde')
        self.assertEqual(m5.nature_429(b'{"error":"too many requests"}'), 'debit')


class FiltrerTests(M5Base):
    def documents(self, *elements):
        return [dict({'domaine': m5.domaine(e['url']), 'rang': i, 'passages_ibou': [], 'titre': '', 'call_id': None},
                     **e) for i, e in enumerate(elements)]

    def test_circularite_site_quasi_copie_et_reprise(self):
        texte_page = 'Travaux\n\n' + PERIPH + '\n\n' + ' '.join('Phrase de contexte numéro %d sur la circulation.' % i
                                                                 for i in range(40))
        version = self.page('https://site-audite.fr/peripherique', texte_page)
        unite = self.unite(version, PERIPH, groupe='gp', temporal_status='perimee_par_date')
        copie = texte_page.replace('numéro 3 ', 'numéro trois ')
        reprise = 'Dépêche. ' + PERIPH + ' Fin de la dépêche, autres nouvelles du jour sans rapport.'
        docs = self.documents(
            {'url': 'https://www.site-audite.fr/autre', 'texte': 'Page interne du site.', 'date': '2026-09-01'},
            {'url': 'https://copieur.com/a', 'texte': copie, 'date': '2026-09-01'},
            {'url': 'https://agregateur1.com/a', 'texte': reprise, 'date': '2026-09-01'},
            {'url': 'https://agregateur2.com/a', 'texte': reprise + ' Publicité.', 'date': '2026-09-02'},
            {'url': 'https://independant.fr/a', 'texte': 'Le périphérique parisien a été fermé en août 2026.', 'date': '2026-09-03'},
        )
        annotes = {d['url']: d for d in m5.filtrer(docs, 'gp', JOUR)}
        self.assertEqual(annotes['https://www.site-audite.fr/autre']['raison'], 'site_audite')
        self.assertEqual(annotes['https://copieur.com/a']['raison'], 'quasi_copie_site')
        gardees = [u for u in ('https://agregateur1.com/a', 'https://agregateur2.com/a') if annotes[u]['kept']]
        self.assertEqual(len(gardees), 1)
        ecartee = [u for u in ('https://agregateur1.com/a', 'https://agregateur2.com/a') if not annotes[u]['kept']][0]
        self.assertEqual(annotes[ecartee]['raison'], 'reprise')
        self.assertTrue(annotes['https://independant.fr/a']['kept'])
        self.assertIsNotNone(unite)

    def test_filtre_temporel_bilan_d_evenement(self):
        unite = self.peripherique(critique=1)
        m4.requetes(aujourdhui=JOUR, reformuler_llm=False)
        groupe = s.query('SELECT group_id FROM unit_group_members WHERE unit_id=?', (unite,), True)['group_id']
        docs = self.documents(
            {'url': 'https://avant.fr/a', 'texte': 'Le périphérique parisien sera fermé en août.', 'date': '2026-06-10'},
            {'url': 'https://apres.fr/a', 'texte': 'Le périphérique parisien a été fermé en août.', 'date': '2026-09-02'},
            {'url': 'https://pendant.fr/a', 'texte': 'Le périphérique parisien est fermé ce mois-ci.', 'date': '2026-08-12'},
            {'url': 'https://sansdate.fr/a', 'texte': 'Le périphérique parisien a été fermé.', 'date': None},
        )
        annotes = {d['url']: d for d in m5.filtrer(docs, groupe, JOUR)}
        self.assertEqual(annotes['https://avant.fr/a']['raison'], 'anterieure_periode')
        self.assertTrue(annotes['https://apres.fr/a']['kept'])
        self.assertTrue(annotes['https://pendant.fr/a']['kept'])
        self.assertTrue(annotes['https://sansdate.fr/a']['kept'])
        self.assertLess(annotes['https://sansdate.fr/a']['poids'], annotes['https://apres.fr/a']['poids'])

    def test_poids_officiel_et_primaire(self):
        self.assertEqual(m5.poids_source('insee.fr'), 1.0)
        self.assertEqual(m5.poids_source('www.paris.fr'), 1.0)
        self.assertEqual(m5.poids_source('renault.com', 'Renault'), 0.9)
        self.assertEqual(m5.poids_source('blog.example.com', 'Renault'), 0.6)

    def test_simhash(self):
        a = simhash.empreinte('le périphérique parisien sera fermé en août pour des travaux de rénovation importants')
        b = simhash.empreinte('le périphérique parisien sera fermé en août pour des travaux de rénovation importantes')
        c = simhash.empreinte('la poule pond des oeufs tous les jours dans le poulailler de la ferme voisine')
        self.assertLess(simhash.distance(a, b), simhash.distance(a, c))
        self.assertEqual(simhash.depuis_hexa(simhash.hexa(a)), a)
        self.assertIsNone(simhash.empreinte(''))


class PassagesTests(M5Base):
    def test_page_entiere_examinee_et_hors_cadre_ecarte(self):
        unite, requete = self.pdg()
        remplissage = '\n\n'.join('Paragraphe de remplissage numéro %d sans rapport avec le sujet traité ici.' % i
                                  for i in range(400))
        texte = remplissage + '\n\nEn 2026, X est dirigé par B, a confirmé le conseil.'
        self.assertGreater(len(texte), 24000)
        docs = [{'url': 'https://journal.fr/a', 'domaine': 'journal.fr', 'texte': texte, 'date': '2026-10-01', 'kept': True,
                 'poids': 0.6, 'passages_ibou': [], 'titre': '', 'call_id': None},
                {'url': 'https://autre.fr/b', 'domaine': 'autre.fr', 'texte': 'En 2019, Y était dirigé par Z.',
                 'date': '2026-10-01', 'kept': True, 'poids': 0.6, 'passages_ibou': [], 'titre': '', 'call_id': None}]
        retenus = m5.passages(docs, requete, 'g1', aujourdhui=JOUR)
        textes = [r['text'] for r in retenus]
        self.assertTrue(any('dirigé par B' in t for t in textes))
        self.assertFalse(any('Y était dirigé' in t for t in textes))
        ecarte = s.query("SELECT reject_reason FROM evidence WHERE url='https://autre.fr/b'", one=True)
        self.assertTrue(ecarte['reject_reason'].startswith('hors_cadre'))

    def test_decouper_couvre_tout(self):
        texte = 'Court.\n\nAutre court.\n\n' + 'Une phrase assez longue pour remplir. ' * 80
        morceaux = m5.decouper(texte)
        couverts = ''.join(texte[a:b] for a, b in morceaux)
        self.assertEqual(couverts.replace(' ', '').replace('\n', ''), texte.replace(' ', '').replace('\n', ''))


class VerdictTests(M5Base):
    def test_confirmee_deux_domaines(self):
        unite, _ = self.pdg()
        self.preuve('g1', 'https://a.fr/1', 'X est dirigé par B depuis 2025.', '2026-09-01')
        self.preuve('g1', 'https://b.fr/1', 'B dirige toujours X.', '2026-10-01')
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('B', None, 'implication')])):
            decision = m5.verdict('g1', JOUR)
            bilan = m5.reecrire('g1', JOUR)
        self.assertEqual(decision['verdict'], 'confirmee')
        ligne = s.query('SELECT status, verdict, verified_at FROM units WHERE id=?', (unite,), True)
        self.assertEqual((ligne['status'], ligne['verdict'], ligne['verified_at']), ('sourcee', 'confirmee', '2026-11-13'))
        sources = s.query("SELECT * FROM sources WHERE unit_id=? AND kind='verification'", (unite,))
        self.assertEqual(len(sources), 2)
        self.assertEqual({x['source_date'] for x in sources}, {'2026-09-01', '2026-10-01'})
        self.assertTrue(all(x['passage'] for x in sources))
        self.assertEqual(bilan['unites'], 1)

    def test_source_plus_recente_qui_contredit_empeche_la_confirmation(self):
        self.pdg()
        self.preuve('g1', 'https://a.fr/1', 'X est dirigé par B.', '2026-09-01')
        self.preuve('g1', 'https://b.fr/1', 'B dirige X.', '2026-09-15')
        self.preuve('g1', 'https://c.fr/1', 'X est désormais dirigé par Claire Martin.', None)
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('Claire', None, 'contradiction'),
                                                                        ('B', None, 'implication')])):
            decision = m5.verdict('g1', JOUR)
        self.assertEqual(decision['verdict'], 'conflictuelle')

    def test_corrigee_valeur_coherente_et_historique(self):
        unite, _ = self.pdg()
        passage_1 = 'Depuis octobre 2026, X est dirigé par Claire Martin.'
        passage_2 = 'Claire Martin a pris la tête de X.'
        self.preuve('g1', 'https://a.fr/1', passage_1, '2026-10-05')
        self.preuve('g1', 'https://b.fr/1', passage_2, '2026-10-20')
        regles = [('Claire', 'B', 'contradiction'), ('Claire', 'Claire', 'implication')]

        def llm(tache, payload):
            self.assertEqual(payload['mode'], 'chaine:m5_valeur')
            return {'trouve': True, 'valeur': 'Claire Martin', 'citation': 'Claire Martin'}
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots(regles)), patch.object(s, 'llm', side_effect=llm):
            decision = m5.verdict('g1', JOUR)
            m5.reecrire('g1', JOUR)
        self.assertEqual(decision['verdict'], 'corrigee')
        self.assertEqual({c['valeur'] for c in decision['corrections']}, {'Claire Martin'})
        ancien = s.query("SELECT * FROM facts WHERE unit_id=? AND object='B'", (unite,), True)
        nouveau = s.query("SELECT * FROM facts WHERE unit_id=? AND object='Claire Martin'", (unite,), True)
        self.assertEqual(ancien['status'], 'historique')
        self.assertEqual(ancien['replaced_by'], nouveau['id'])
        self.assertEqual(nouveau['origin'], 'externe')
        self.assertIsNotNone(nouveau['span_start'])
        self.assertEqual(s.query('SELECT verdict, status FROM units WHERE id=?', (unite,), True)['verdict'], 'corrigee')
        self.assertTrue(s.query("SELECT * FROM sources WHERE unit_id=? AND contribution='correction'", (unite,)))

    def test_correction_propagee_au_fait_identique_d_une_reprise(self):
        unite, _ = self.pdg()
        autre = self.page('https://site-audite.fr/reprise', 'Selon nous, le PDG de X est B.')
        reprise = self.unite(autre, 'Le PDG de X est B.', groupe='g1', obs_class='etat_courant', span_start=11)
        self.fait(reprise, 'X', 'a_pour_dirigeant', 'B', 'EX', verbalization='X a pour dirigeant B')
        self.preuve('g1', 'https://a.fr/1', 'Depuis octobre 2026, X est dirigé par Claire Martin.', '2026-10-05')
        self.preuve('g1', 'https://b.fr/1', 'Claire Martin a pris la tête de X.', '2026-10-20')
        regles = [('Claire', 'B', 'contradiction'), ('Claire', 'Claire', 'implication')]
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots(regles)), \
                patch.object(s, 'llm', return_value={'trouve': True, 'valeur': 'Claire Martin', 'citation': 'Claire Martin'}):
            m5.verdict('g1', JOUR)
            m5.reecrire('g1', JOUR)
        for identifiant in (unite, reprise):
            ancien = s.query("SELECT * FROM facts WHERE unit_id=? AND object='B'", (identifiant,), True)
            nouveau = s.query("SELECT * FROM facts WHERE unit_id=? AND object='Claire Martin'", (identifiant,), True)
            self.assertEqual(ancien['status'], 'historique')
            self.assertEqual(ancien['replaced_by'], nouveau['id'])
            self.assertEqual(nouveau['origin'], 'externe')

    def test_valeur_non_citee_dans_le_passage_refusee(self):
        self.pdg()
        self.preuve('g1', 'https://a.fr/1', 'X a changé de dirigeant en octobre 2026.', '2026-10-05')
        self.preuve('g1', 'https://b.fr/1', 'Nouveau dirigeant chez X.', '2026-10-20')
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('X', None, 'contradiction')])), \
                patch.object(s, 'llm', return_value={'trouve': True, 'valeur': 'Claire Martin', 'citation': 'Claire Martin'}):
            decision = m5.verdict('g1', JOUR)
        self.assertEqual(decision['verdict'], 'conflictuelle')
        self.assertEqual(decision['corrections'], [])

    def test_conflictuelle_valeurs_differentes(self):
        self.pdg()
        self.preuve('g1', 'https://a.fr/1', 'X est dirigé par Claire Martin.', '2026-10-05')
        self.preuve('g1', 'https://b.fr/1', 'X est dirigé par Paul Durand.', '2026-10-20')
        valeurs = iter(['Claire Martin', 'Paul Durand'])

        def llm(tache, payload):
            valeur = next(valeurs)
            return {'trouve': True, 'valeur': valeur, 'citation': valeur}
        regles = [('Claire', 'B', 'contradiction'), ('Paul', 'B', 'contradiction'), ('Claire', 'Claire', 'implication'),
                  ('Paul', 'Paul', 'implication')]
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots(regles)), patch.object(s, 'llm', side_effect=llm):
            decision = m5.verdict('g1', JOUR)
            m5.reecrire('g1', JOUR)
        self.assertEqual(decision['verdict'], 'conflictuelle')
        self.assertEqual(s.query("SELECT count(*) n FROM facts WHERE origin='externe'", one=True)['n'], 0)

    def test_insuffisante(self):
        self.pdg()
        self.preuve('g1', 'https://a.fr/1', 'X est dirigé par B.', '2026-09-01')
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('B', None, 'implication')])):
            decision = m5.verdict('g1', JOUR)
        self.assertEqual(decision['verdict'], 'insuffisante')
        self.preuve('g1', 'https://a.fr/2', 'Le conseil de X se réunit.', '2026-09-01')
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('B', None, 'implication')])):
            decision = m5.verdict('g1', JOUR)
        self.assertEqual(decision['verdict'], 'insuffisante')

    def test_completee_inconnue_resolue(self):
        unite, _ = self.pdg()
        # Predicat de la liste fermee : sans lui, la valeur resolue reste une hypothese (relecture M8).
        self.hypothese(unite, 'information_inconnue', 'date de nomination', 'date_de_debut')
        self.preuve('g1', 'https://a.fr/1', 'B dirige X depuis sa nomination le 3 mars 2025.', '2026-09-01')
        self.preuve('g1', 'https://b.fr/1', 'X est dirigé par B.', '2026-10-01')

        def llm(tache, payload):
            if 'nomination' in payload['passage']:
                return {'trouve': True, 'valeur': '3 mars 2025', 'citation': 'nomination le 3 mars 2025'}
            return {'trouve': False, 'valeur': '', 'citation': ''}
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('B', None, 'implication')])), \
                patch.object(s, 'llm', side_effect=llm):
            decision = m5.verdict('g1', JOUR)
            m5.reecrire('g1', JOUR)
        self.assertEqual(decision['verdict'], 'completee')
        hypothese = s.query('SELECT resolved_fact_id FROM hypotheses WHERE unit_id=?', (unite,), True)
        nouveau = s.query('SELECT * FROM facts WHERE id=?', (hypothese['resolved_fact_id'],), True)
        self.assertEqual(nouveau['object'], '3 mars 2025')
        self.assertEqual(nouveau['origin'], 'externe')
        self.assertEqual(s.query('SELECT status FROM units WHERE id=?', (unite,), True)['status'], 'sourcee')

    def test_propagation_aux_enfants_selon_la_paternite(self):
        parent, _ = self.pdg()
        version = self.page('https://site-audite.fr/y', 'Autre page.')
        enfant = self.unite(version, 'B est le PDG de X.', groupe='g1', anchor_date='2025-06-01', anchor_kind='phrase')
        s.query("INSERT INTO paternity(group_id,parent_unit_id,criterion) VALUES('g1',?,'sujet_principal')", (parent,))
        self.preuve('g1', 'https://a.fr/1', 'X est dirigé par B.', '2026-09-01')
        self.preuve('g1', 'https://b.fr/1', 'B dirige X.', '2026-10-01')
        with patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('B', None, 'implication')])):
            m5.verdict('g1', JOUR)
            m5.reecrire('g1', JOUR)
        propres = s.query("SELECT * FROM sources WHERE unit_id=? AND kind='verification'", (parent,))
        heritees = s.query("SELECT * FROM sources WHERE unit_id=? AND kind='verification'", (enfant,))
        self.assertEqual(len(propres), 2)
        self.assertEqual(len(heritees), 2)
        self.assertTrue(all(h['inherited_from'] in {p['id'] for p in propres} for h in heritees))
        self.assertTrue(all(p['inherited_from'] is None for p in propres))
        self.assertEqual(s.query('SELECT status FROM units WHERE id=?', (enfant,), True)['status'], 'sourcee')

    def test_taux_obsolescence_par_page(self):
        version = self.page('https://site-audite.fr/t', 'Texte')
        self.unite(version, 'A.', verdict='corrigee')
        self.unite(version, 'B.', temporal_status='perimee_par_date')
        self.unite(version, 'C.')
        self.unite(version, 'D.', obs_class='immuable')
        taux = m5.taux_obsolescence([version])[version]
        self.assertEqual((taux['obsoletes'], taux['verifiables']), (2, 3))


class ExecuterTests(M5Base):
    def pages(self, contenus):
        return patch.object(m5, '_telecharger', side_effect=lambda url: contenus[url])

    def test_circuit_complet_controle_non_envoye_si_confirmee(self):
        unite, requete = self.pdg(critique=1)
        controle = s.query("SELECT * FROM queries WHERE kind='controle'", one=True)
        self.assertIsNotNone(controle)
        contenus = {
            'https://a.fr/1': html('Actualité.\n\nEn 2026, X est dirigé par B, a confirmé le conseil.', '2026-09-01'),
            'https://b.fr/1': html('B dirige X depuis 2025 et prépare 2026.', '2026-10-01'),
        }
        budget = self.budget(30)
        with patch.object(m5, '_http', return_value=reponse(200, resultats(*contenus), {'X-Units-Remaining': '800'})) as http, \
                self.pages(contenus), \
                patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('dirig', 'B', 'implication')])):
            compteurs = m5.executer(budget, aujourdhui=JOUR)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(compteurs['verdicts'], {'confirmee': 1})
        self.assertEqual(compteurs['unites_ibou'], 3)
        self.assertEqual(self.utilise(budget), 3)
        self.assertEqual(s.query('SELECT status FROM units WHERE id=?', (unite,), True)['status'], 'sourcee')
        self.assertEqual(s.query('SELECT status FROM queries WHERE id=?', (controle['id'],), True)['status'], 'prete')
        preuve = s.query("SELECT * FROM evidence WHERE kept=1 AND url='https://a.fr/1'", one=True)
        self.assertEqual(preuve['page_date'], '2026-09-01')
        self.assertEqual(preuve['nli'], 'implication')
        etape = s.query("SELECT * FROM step_runs WHERE step='m5.verification'", one=True)
        self.assertEqual(etape['status'], 'terminee')
        self.assertEqual(etape['ibou_units'], 3)

    def test_controle_envoye_si_premiere_insuffisante(self):
        unite, requete = self.pdg(critique=1)
        contenus = {'https://a.fr/1': html('X est dirigé par B en 2026.', '2026-09-01')}
        corps = resultats(*contenus)
        with patch.object(m5, '_http', return_value=reponse(200, corps)) as http, self.pages(contenus), \
                patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([('dirig', 'B', 'implication')])), \
                patch.object(s, 'llm', side_effect=s.OperationalError('LLM indisponible')):
            compteurs = m5.executer(self.budget(30), aujourdhui=JOUR)
        envoyees = [c[0][2]['query'] for c in http.call_args_list]
        self.assertEqual(envoyees[0], QUESTION)
        self.assertEqual(envoyees[1], 'Est-ce que X a pour dirigeant B en 2026 ?')
        self.assertEqual(compteurs['controles'], 1)
        self.assertEqual(s.query('SELECT verdict FROM units WHERE id=?', (unite,), True)['verdict'], 'insuffisante')

    def test_plafond_jamais_depasse(self):
        version = self.page('https://site-audite.fr/m', 'Texte')
        for i in range(4):
            unite = self.unite(version, 'Le site %d compte %d000 visiteurs.' % (i, i + 1), critical=1)
            s.query("INSERT INTO queries(id,group_id,text,kind,template,frame,status,critical,priority) "
                    "VALUES(?,?,?,'ouverte','etat_courant:generique','{}','prete',1,1)",
                    ('q%d' % i, unite, 'Combien de visiteurs compte le site numéro %d en 2026 ?' % i))
            s.query('INSERT INTO query_units(query_id,unit_id) VALUES(?,?)', ('q%d' % i, unite))
        budget = self.budget(7)
        with patch.object(m5, '_http', return_value=reponse(200, resultats())) as http, \
                patch.object(m5, 'criticite', return_value={}), \
                patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([])), \
                patch.object(s, 'llm', side_effect=s.OperationalError('LLM indisponible')):
            compteurs = m5.executer(budget, aujourdhui=JOUR)
        self.assertEqual(compteurs['arret'], 'plafond')
        self.assertLessEqual(self.utilise(budget), 7)
        self.assertEqual(self.utilise(budget), 6)
        self.assertEqual(http.call_count, 2)

    def test_requete_de_repli_jamais_envoyee(self):
        version = self.page('https://site-audite.fr/m', 'Texte')
        unite = self.unite(version, 'Google utilise plus de 100 signaux bruts.', critical=1)
        s.query("INSERT INTO queries(id,group_id,text,kind,template,frame,status,critical,priority) "
                "VALUES('qr',?,'Est-ce que Google utilise plus de 100 signaux bruts ?','ouverte','unite:fermee','{}','prete',1,1)",
                (unite,))
        s.query("INSERT INTO query_units(query_id,unit_id) VALUES('qr',?)", (unite,))
        budget = self.budget(30)
        with patch.object(m5, '_http') as http, patch.object(m5, 'criticite', return_value={}):
            with self.assertRaises(s.OperationalError):
                m5.appeler('qr', budget)
            compteurs = m5.executer(budget, aujourdhui=JOUR, tout=True)
        self.assertEqual(http.call_count, 0)
        self.assertEqual(compteurs['requetes'], 0)
        self.assertEqual(self.utilise(budget), 0)
        self.assertFalse(s.query('SELECT * FROM ibou_calls'))
        # Modifiee par un humain : ce n'est plus l'affirmation telle quelle, elle devient envoyable.
        s.query("UPDATE queries SET edited=1, text='Combien de signaux bruts Google utilise-t-il en 2026 ?' WHERE id='qr'")
        self.assertTrue(m5.envoyable_repli(s.query("SELECT * FROM queries WHERE id='qr'", one=True)))

    def test_file_arretee_proprement_sous_le_seuil(self):
        version = self.page('https://site-audite.fr/m', 'Texte')
        for i in range(3):
            unite = self.unite(version, 'Le site %d compte %d000 visiteurs.' % (i, i + 1), critical=1)
            s.query("INSERT INTO queries(id,group_id,text,kind,template,frame,status,critical,priority) "
                    "VALUES(?,?,?,'ouverte','etat_courant:generique','{}','prete',1,1)",
                    ('q%d' % i, unite, 'Combien de visiteurs compte le site numéro %d en 2026 ?' % i))
            s.query('INSERT INTO query_units(query_id,unit_id) VALUES(?,?)', ('q%d' % i, unite))
        with patch.object(m5, '_http', return_value=reponse(200, resultats(), {'X-Units-Remaining': '25'})) as http, \
                patch.object(m5, 'criticite', return_value={}), \
                patch.object(moteurs, 'nli_lot', side_effect=nli_par_mots([])), \
                patch.object(s, 'llm', side_effect=s.OperationalError('LLM indisponible')):
            compteurs = m5.executer(self.budget(100), aujourdhui=JOUR)
        self.assertEqual(http.call_count, 1)
        self.assertTrue(compteurs['arret'].startswith('unites restantes'))
        with patch.object(m5, '_solde', return_value=5), patch.object(m5, '_http') as http:
            compteurs = m5.executer(self.budget(100), aujourdhui=JOUR, tout=True)
        self.assertEqual(http.call_count, 0)
        self.assertTrue(compteurs['arret'].startswith('solde'))


if __name__ == '__main__':
    unittest.main()
