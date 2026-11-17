"""Tests du lot M3 : voisins, qualification des paires, paternite, propagation, statuts temporels."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s
import chaine
from chaine import m3
from chaine import moteurs
from chaine import noyau
from chaine import schema
from chaine import seuils

AUJOURDHUI = '2026-11-13'


class Base(unittest.TestCase):
    """Base temporaire, serveur branche, schema de la chaine initialise."""

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
        m3._PREPARE['base'] = None
        s.CTX.step = None
        self.compteur = 0
        self.nli = {}
        self.appels_llm = []

    def tearDown(self):
        s.CONFIG.clear()
        s.CONFIG.update(self.config)
        s.STOP.clear()
        moteurs.reinitialiser()
        noyau.vider_reglages()
        seuils._vider_cache()
        self.tmp.cleanup()

    # --- Fabrique de donnees -------------------------------------------------

    def page(self, titre='Page', url=None, texte='', published='', versions=1):
        """Cree une page et ses versions ; renvoie la liste des version_id."""
        self.compteur += 1
        page_id = 'p%d' % self.compteur
        s.query('INSERT INTO pages(id,url,title) VALUES(?,?,?)', (page_id, url or 'https://site.example/%s/' % page_id, titre))
        ids = []
        for numero in range(versions):
            version_id = '%s-v%d' % (page_id, numero)
            s.query('INSERT INTO versions(id,page_id,text,created,published,hash) VALUES(?,?,?,?,?,?)',
                    (version_id, page_id, texte, s.now(), published, version_id))
            ids.append(version_id)
        return ids

    def unite(self, version_id, texte, ancre=None, **colonnes):
        """Cree une unite ; renvoie son id."""
        self.compteur += 1
        uid = colonnes.pop('id', 'u%d' % self.compteur)
        valeurs = {'id': uid, 'version_id': version_id, 'text': texte, 'quote': texte, 'span_start': 0,
                   'span_end': len(texte), 'anchor_date': ancre, 'anchor_kind': 'publication' if ancre else 'inconnue',
                   'temporal_status': 'a_jour', 'extractor': 'test', 'created': s.now()}
        valeurs.update(colonnes)
        noms = ','.join(valeurs)
        s.query('INSERT INTO units(' + noms + ') VALUES(' + ','.join('?' for _ in valeurs) + ')', tuple(valeurs.values()))
        return uid

    def groupe(self, group_id, *unites):
        """Cree un groupe et ses membres."""
        s.query('INSERT INTO unit_groups(id,label,created) VALUES(?,?,?)', (group_id, group_id, s.now()))
        for unite in unites:
            s.query('INSERT INTO unit_group_members(group_id,unit_id) VALUES(?,?)', (group_id, unite))

    def source(self, unite, url=None, entite=None, kind='lien_externe', domaine=None, inherited_from=None):
        """Ajoute une source propre (ou heritee) a une unite."""
        self.compteur += 1
        s.query('INSERT INTO sources(id,unit_id,url,domain,cited_entity,kind,scope,confidence,inherited_from,created) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('s%d' % self.compteur, unite, url, domaine, entite, kind, 'phrase', 0.9, inherited_from, s.now()))

    def fait(self, unite, sujet, predicat, objet, portee=None, periode=None):
        """Ajoute un fait (triplet) a une unite."""
        self.compteur += 1
        s.query('INSERT INTO facts(id,unit_id,subject_id,subject,predicate,object,scope,period,origin,status,created) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                ('f%d' % self.compteur, unite, 'ent:' + sujet.lower(), sujet, predicat, objet, portee, periode, 'explicite', 'valide', s.now()))

    # --- Doubles des moteurs -----------------------------------------------

    def nli_double(self, paires):
        """NLI simule : ``self.nli[(premisse, hypothese)] = (label, score)``, neutre sinon."""
        resultat = []
        for premisse, hypothese in paires:
            label, score = self.nli.get((premisse, hypothese), ('neutre', 0.9))
            reste = (1.0 - score) / 2.0
            probas = {'implication': reste, 'contradiction': reste, 'neutre': reste}
            probas[label] = score
            resultat.append({'label': label, 'score': score, 'probas': probas, 'engine': 'test:nli'})
        return resultat

    def regle(self, a, b, label, score=0.95):
        """Fixe le resultat NLI de la paire de textes (a, b)."""
        self.nli[(a, b)] = (label, score)

    def statut(self, unite):
        return s.query('SELECT temporal_status FROM units WHERE id=?', (unite,), True)['temporal_status']

    def qualifier(self, a, b):
        with patch.object(moteurs, 'nli_lot', self.nli_double):
            return m3.qualifier(a, b)


class QualificationTests(Base):
    """Etape 2 : chaque ligne du tableau du document."""

    def deux_pages(self, texte_a, texte_b, ancre_a='2025-01-10', ancre_b='2025-02-01'):
        va = self.page('A')[0]
        vb = self.page('B')[0]
        return self.unite(va, texte_a, ancre_a), self.unite(vb, texte_b, ancre_b)

    def test_implication_mutuelle_equivalence_meme_groupe(self):
        ta = 'Renault emploie 100 000 salaries.'
        tb = 'Le groupe Renault compte 100 000 salaries.'
        a, b = self.deux_pages(ta, tb)
        self.regle(ta, tb, 'implication')
        self.regle(tb, ta, 'implication')
        resultat = self.qualifier(a, b)
        self.assertEqual(resultat['relation'], 'equivalence')
        groupes = s.query('SELECT group_id FROM unit_group_members WHERE unit_id IN (?,?)', (a, b))
        self.assertEqual(len(groupes), 2)
        self.assertEqual(len({g['group_id'] for g in groupes}), 1)
        ligne = s.query('SELECT * FROM unit_relations WHERE a=? AND b=?', (a, b), True)
        self.assertEqual(ligne['relation'], 'equivalence')
        self.assertTrue(ligne['run_id'])

    def test_implication_un_sens_precision(self):
        ta = 'Renault emploie 100 000 salaries, dont 40 000 en France.'
        tb = 'Renault emploie 100 000 salaries.'
        a, b = self.deux_pages(ta, tb)
        self.regle(ta, tb, 'implication')
        resultat = self.qualifier(a, b)
        self.assertEqual(resultat['relation'], 'precision')
        self.assertEqual(resultat['direction'], a)

    def test_piege_pdg_succession_pas_contradiction(self):
        ta = 'Le PDG de Renault est Alice Martin.'
        tb = 'Le PDG de Renault est Bruno Durand.'
        a, b = self.deux_pages(ta, tb, '2023-03-01', '2025-03-01')
        self.regle(ta, tb, 'contradiction')
        self.regle(tb, ta, 'contradiction')
        resultat = self.qualifier(a, b)
        self.assertEqual(resultat['relation'], 'remplacement')
        self.assertEqual(resultat['direction'], b)
        self.assertEqual(self.statut(a), 'obsolete_historique')
        self.assertEqual(self.statut(b), 'a_jour')
        self.assertEqual(m3.statuts_temporels(AUJOURDHUI)['obsolete_historique'], 1)
        self.assertEqual(self.statut(a), 'obsolete_historique')

    def test_piege_pdg_avec_faits_et_ordre_inverse(self):
        ta = 'Bruno Durand dirige X depuis 2025.'
        tb = 'Le PDG de X est Alice Martin.'
        a, b = self.deux_pages(ta, tb, '2025-03-01', '2023-03-01')
        self.fait(a, 'X', 'a_pour_dirigeant', 'Bruno Durand')
        self.fait(b, 'X', 'a_pour_dirigeant', 'Alice Martin')
        self.regle(ta, tb, 'contradiction')
        resultat = self.qualifier(a, b)
        self.assertEqual(resultat['relation'], 'remplacement')
        self.assertEqual(resultat['direction'], a)
        self.assertEqual(self.statut(b), 'obsolete_historique')

    def test_contradiction_meme_periode_conflit(self):
        ta = 'Le PDG de Renault est Alice Martin.'
        tb = 'Le PDG de Renault est Bruno Durand.'
        a, b = self.deux_pages(ta, tb, '2025-01-10', '2025-02-01')
        self.regle(ta, tb, 'contradiction')
        resultat = self.qualifier(a, b)
        self.assertEqual(resultat['relation'], 'conflit')
        self.assertEqual(self.statut(a), 'remise_en_question')
        self.assertEqual(self.statut(b), 'remise_en_question')

    def test_contradiction_chronologie_indeterminable_conflit(self):
        ta = 'Le PDG de Renault est Alice Martin.'
        tb = 'Le PDG de Renault est Bruno Durand.'
        a, b = self.deux_pages(ta, tb, None, '2025-02-01')
        self.regle(ta, tb, 'contradiction')
        self.assertEqual(self.qualifier(a, b)['relation'], 'conflit')

    def test_cadres_differents_periode_faux_conflit(self):
        ta = 'En 2023, le taux de chomage en France etait de 7,1 %.'
        tb = 'En 2024, le taux de chomage en France etait de 7,4 %.'
        a, b = self.deux_pages(ta, tb)
        self.regle(ta, tb, 'contradiction')
        resultat = self.qualifier(a, b)
        self.assertEqual(resultat['relation'], 'faux_conflit')
        self.assertEqual(self.statut(a), 'a_jour')
        self.assertEqual(self.statut(b), 'a_jour')

    def test_cadres_differents_portee_faux_conflit(self):
        ta = 'Le taux de chomage est de 7,4 %.'
        tb = 'Le taux de chomage est de 6,1 %.'
        a, b = self.deux_pages(ta, tb, '2023-01-01', '2025-01-01')
        self.fait(a, 'Chomage', 'taux', '7,4 %', portee='France')
        self.fait(b, 'Chomage', 'taux', '6,1 %', portee='Bretagne')
        self.regle(ta, tb, 'contradiction')
        self.assertEqual(self.qualifier(a, b)['relation'], 'faux_conflit')

    def test_depuis_n_est_pas_une_periode(self):
        cadre = m3.cadre({'id': None, 'text': 'Bruno Durand est PDG de Renault depuis 2025.'})
        self.assertIsNone(cadre['periode'])
        cadre = m3.cadre({'id': None, 'text': 'En 2023, Renault employait 100 000 salaries.'})
        self.assertEqual(cadre['periode'], '2023')

    def test_neutre_sans_rapport(self):
        a, b = self.deux_pages('Renault a son siege a Boulogne.', 'Le ciel est bleu.')
        self.assertEqual(self.qualifier(a, b)['relation'], 'sans_rapport')

    def test_seuil_volatilite_du_predicat(self):
        ta = 'Le PDG de Renault est Alice Martin.'
        tb = 'Le PDG de Renault est Bruno Durand.'
        a, b = self.deux_pages(ta, tb, '2023-03-01', '2025-03-01')
        self.fait(a, 'Renault', 'dirigeant', 'Alice Martin')
        self.fait(b, 'Renault', 'dirigeant', 'Bruno Durand')
        self.regle(ta, tb, 'contradiction')
        faux = types.ModuleType('chaine.predicats')
        faux.get = lambda nom: {'nom': nom, 'volatilite_jours': 3650}
        with patch.dict(sys.modules, {'chaine.predicats': faux}):
            with patch.object(chaine, 'predicats', faux, create=True):
                self.assertEqual(m3.seuil_recence('dirigeant'), (1825, 'volatilite:dirigeant'))
                resultat = self.qualifier(a, b)
        self.assertEqual(resultat['relation'], 'conflit')
        self.assertEqual(resultat['reason']['seuil_recence_jours'], 1825)
        # Sans module predicats : seuil fixe de 180 jours, la succession est reconnue.
        self.assertEqual(m3.seuil_recence('inconnu')[1], 'fixe')

    def test_zone_grise_llm_question_fermee(self):
        ta = 'Le PDG de Renault est Alice Martin.'
        tb = 'Le PDG de Renault est Bruno Durand.'
        a, b = self.deux_pages(ta, tb, '2023-03-01', '2025-03-01')
        self.regle(ta, tb, 'contradiction', 0.5)
        self.regle(tb, ta, 'neutre', 0.6)

        def llm(tache, payload):
            self.appels_llm.append(payload)
            return {'meme_cadre': 'oui', 'plus_recente': 'b'}
        with patch.object(s, 'llm', llm):
            resultat = self.qualifier(a, b)
        self.assertEqual(len(self.appels_llm), 1)
        self.assertEqual(self.appels_llm[0]['mode'], 'chaine:m3_cadre')
        self.assertEqual(resultat['relation'], 'remplacement')
        self.assertEqual(resultat['direction'], b)

    def test_zone_grise_llm_cadres_differents_et_divergence(self):
        ta = 'Le PDG de Renault est Alice Martin.'
        tb = 'Le PDG de Renault est Bruno Durand.'
        a, b = self.deux_pages(ta, tb, '2023-03-01', '2025-03-01')
        self.regle(ta, tb, 'contradiction', 0.5)
        with patch.object(s, 'llm', lambda t, p: {'meme_cadre': 'non', 'plus_recente': 'indeterminable'}):
            self.assertEqual(self.qualifier(a, b)['relation'], 'faux_conflit')
        with patch.object(s, 'llm', lambda t, p: {'meme_cadre': 'oui', 'plus_recente': 'a'}):
            self.assertEqual(self.qualifier(a, b)['relation'], 'conflit')

    def test_zone_grise_llm_non_conforme_ou_absent(self):
        ta = 'Le PDG de Renault est Alice Martin.'
        tb = 'Le PDG de Renault est Bruno Durand.'
        a, b = self.deux_pages(ta, tb, '2023-03-01', '2025-03-01')
        self.regle(ta, tb, 'contradiction', 0.5)
        with patch.object(s, 'llm', lambda t, p: {'meme_cadre': 'peut-etre', 'plus_recente': 'a'}):
            resultat = self.qualifier(a, b)
        self.assertEqual(resultat['relation'], 'sans_rapport')
        self.assertIn('LLM indisponible', resultat['reason']['regle'])

    def test_llm_jamais_hors_zone_grise(self):
        ta = 'Le PDG de Renault est Alice Martin.'
        tb = 'Le PDG de Renault est Bruno Durand.'
        a, b = self.deux_pages(ta, tb, '2023-03-01', '2025-03-01')
        self.regle(ta, tb, 'contradiction', 0.95)

        def interdit(tache, payload):
            raise AssertionError('LLM appele hors zone grise')
        with patch.object(s, 'llm', interdit):
            self.assertEqual(self.qualifier(a, b)['relation'], 'remplacement')

    def test_fusion_de_deux_groupes(self):
        ta = 'Renault emploie 100 000 salaries.'
        tb = 'Le groupe Renault compte 100 000 salaries.'
        a, b = self.deux_pages(ta, tb)
        va = s.query('SELECT version_id FROM units WHERE id=?', (a,), True)['version_id']
        c = self.unite(va, 'Autre phrase du groupe de a.')
        self.groupe('ga', a, c)
        self.groupe('gb', b)
        self.regle(ta, tb, 'implication')
        self.regle(tb, ta, 'implication')
        self.assertEqual(self.qualifier(a, b)['groupe'], 'ga')
        self.assertEqual(s.query('SELECT count(*) n FROM unit_group_members WHERE group_id=?', ('ga',), True)['n'], 3)
        self.assertEqual(s.query('SELECT merged_into FROM unit_groups WHERE id=?', ('gb',), True)['merged_into'], 'ga')


class VoisinsTests(Base):
    """Etape 1 : exclusion de la page d'origine et filtres."""

    def test_exclut_toutes_les_versions_de_la_page_d_origine(self):
        v1, v2 = self.page('Origine', versions=2)
        autre = self.page('Autre')[0]
        loin = self.page('Loin')[0]
        phrase = 'Le boulevard peripherique parisien sera ferme du 1er au 31 aout 2026 pour travaux.'
        u1 = self.unite(v1, phrase)
        u1b = self.unite(v1, 'Le boulevard peripherique parisien sera ferme en aout 2026.')
        u2 = self.unite(v2, phrase)
        u3 = self.unite(autre, 'Fermeture du boulevard peripherique parisien en aout 2026 pour travaux.')
        self.unite(loin, 'La poule pond des oeufs.')
        trouves = m3.voisins(u1)
        ids = [v['unit_id'] for v in trouves]
        self.assertIn(u3, ids)
        self.assertNotIn(u1, ids)
        self.assertNotIn(u1b, ids)
        self.assertNotIn(u2, ids)
        page_autre = s.query('SELECT page_id FROM versions WHERE id=?', (autre,), True)['page_id']
        self.assertEqual({v['page_id'] for v in trouves}, {page_autre})
        self.assertIn('lexical', trouves[0]['filtres'])

    def test_filtres_structurel_thematique_et_dense(self):
        v1 = self.page('Origine')[0]
        v2 = self.page('Voisine')[0]
        v3 = self.page('Hors sujet')[0]
        u1 = self.unite(v1, 'Renault emploie 100 000 salaries.', vector=json.dumps([1.0, 0.0, 0.0]))
        u2 = self.unite(v2, 'Renault compte 100 000 employes.', vector=json.dumps([0.95, 0.1, 0.0]))
        u3 = self.unite(v3, 'Renault emploie 100 000 salaries.', vector=json.dumps([0.96, 0.05, 0.0]))
        u4 = self.unite(v2, 'Peugeot emploie 100 000 salaries.', vector=json.dumps([0.97, 0.0, 0.0]))
        s.query("INSERT INTO entities(id,name,type) VALUES('e1','Renault','organisation')")
        s.query("INSERT INTO entities(id,name,type) VALUES('e2','Peugeot','organisation')")
        for unite, entite in ((u1, 'e1'), (u2, 'e1'), (u3, 'e1'), (u4, 'e2')):
            s.query('INSERT INTO unit_entities(unit_id,entity_id,"start","end",role) VALUES(?,?,0,7,?)', (unite, entite, 'sujet'))
        s.query("INSERT INTO topics(id,label,method) VALUES('t1','automobile','fcm')")
        s.query("INSERT INTO topics(id,label,method) VALUES('t2','autre','fcm')")
        s.query("INSERT INTO topic_membership VALUES('t1','version',?,0.8)", (v1,))
        s.query("INSERT INTO topic_membership VALUES('t1','version',?,0.6)", (v2,))
        s.query("INSERT INTO topic_membership VALUES('t2','version',?,0.9)", (v3,))
        with patch.object(moteurs, 'reordonner', lambda r, p, avec_moteur=False: ([0.9] * len(p), 'test') if avec_moteur else [0.9] * len(p)):
            trouves = m3.voisins(u1)
        self.assertEqual([v['unit_id'] for v in trouves], [u2])
        self.assertEqual(trouves[0]['filtres'], ['structurel', 'thematique', 'dense'])


class PaterniteTests(Base):
    """Etape 3 : ordre lexicographique strict."""

    def deux_membres(self, centralites=(0.5, 0.5), dates=('2024-01-01', '2024-01-01')):
        va = self.page('A', published=dates[0])[0]
        vb = self.page('B', published=dates[1])[0]
        a = self.unite(va, 'Renault emploie 100 000 salaries.', centrality=centralites[0])
        b = self.unite(vb, 'Renault emploie 100 000 salaries.', centrality=centralites[1])
        self.groupe('g', a, b)
        return a, b

    def test_critere_1_sujet_principal(self):
        a, b = self.deux_membres((0.3, 0.9))
        self.source(a, 'https://insee.fr/a')
        ligne = m3.paternite('g')
        self.assertEqual((ligne['parent_unit_id'], ligne['criterion']), (b, 'sujet_principal'))
        self.assertEqual(ligne['detail']['centralite'][b], 0.9)

    def test_critere_2_nombre_de_sources_par_domaine(self):
        a, b = self.deux_membres((0.50, 0.53))
        self.source(a, 'https://insee.fr/a')
        self.source(a, 'https://insee.fr/b')
        self.source(a, 'https://www.insee.fr/c')
        self.source(b, 'https://insee.fr/a')
        self.source(b, entite='Banque de France', kind='source_nommee')
        self.source(a, 'https://lemonde.fr/x', inherited_from='zzz')
        ligne = m3.paternite('g')
        self.assertEqual((ligne['parent_unit_id'], ligne['criterion']), (b, 'nombre_sources'))
        self.assertEqual(ligne['detail']['sources'], {a: 1, b: 2})

    def test_critere_3_date_de_publication(self):
        a, b = self.deux_membres((0.5, 0.5), ('2024-01-01', '2022-05-01'))
        ligne = m3.paternite('g')
        self.assertEqual((ligne['parent_unit_id'], ligne['criterion']), (b, 'date_publication'))

    def test_egalite_persistante_puis_arbitrage_humain_preserve(self):
        a, b = self.deux_membres()
        ligne = m3.paternite('g')
        self.assertEqual(ligne['criterion'], 'a_arbitrer')
        self.assertIsNone(ligne['parent_unit_id'])
        self.assertEqual(ligne['detail']['candidats'], sorted([a, b]))
        m3.arbitrer('g', a)
        s.query('UPDATE units SET centrality=0.99 WHERE id=?', (b,))
        m3.paternite('g', forcer=True)
        with patch.object(moteurs, 'nli_lot', side_effect=s.OperationalError('pas de NLI')):
            m3.executer(forcer=True, aujourdhui=AUJOURDHUI)
        ligne = m3.paternite_de('g')
        self.assertEqual((ligne['parent_unit_id'], ligne['criterion'], ligne['human']), (a, 'arbitrage_humain', 1))
        with self.assertRaises(ValueError):
            m3.arbitrer('g', 'inconnue')

    def test_date_inconnue_reste_a_arbitrer(self):
        self.deux_membres((0.5, 0.5), ('2024-01-01', ''))
        self.assertEqual(m3.paternite('g')['criterion'], 'a_arbitrer')

    def test_groupe_a_un_membre_sans_appel_modele(self):
        va = self.page('A')[0]
        a = self.unite(va, 'Phrase unique.')
        self.groupe('seul', a)
        with patch.object(moteurs, 'nli_lot', side_effect=AssertionError('modele')), \
                patch.object(moteurs, 'embeddings', side_effect=AssertionError('modele')), \
                patch.object(s, 'llm', side_effect=AssertionError('modele')):
            ligne = m3.paternite('seul')
        self.assertEqual(ligne['parent_unit_id'], a)
        self.assertEqual(ligne['detail']['trivial'], 'membre_unique')

    def test_meme_page_decide_sans_arbitrage(self):
        va = self.page('A')[0]
        a = self.unite(va, 'Renault emploie 100 000 salaries.', centrality=0.5, span_start=50)
        b = self.unite(va, 'Renault emploie 100 000 salaries.', centrality=0.5, span_start=10)
        self.groupe('g', a, b)
        ligne = m3.paternite('g')
        self.assertEqual((ligne['parent_unit_id'], ligne['criterion']), (b, 'sujet_principal'))
        self.assertTrue(ligne['detail']['page_unique'])

    def test_centralite_titre_hn_et_dependances(self):
        texte = 'Renault en 2026\n\nLe constructeur poursuit sa transformation industrielle et commerciale en Europe et ailleurs dans le monde.\n\nEFFECTIFS\n\nTexte.'
        va = self.page('Renault en 2026', texte=texte)[0]
        a = self.unite(va, 'Renault emploie 100 000 salaries.')
        b = self.unite(va, 'La poule pond des oeufs.')
        c = self.unite(va, 'Donc Renault reste un grand employeur.')
        s.query("INSERT INTO claim_types(unit_id,type,confidence,rule,depends_on) VALUES(?,?,?,?,?)",
                (c, 'interpretation', 0.9, 'test', json.dumps([a])))
        m3.centralites([va])
        valeurs = {l['id']: l['centrality'] for l in s.query('SELECT id, centrality FROM units')}
        self.assertGreater(valeurs[a], valeurs[b])
        self.assertAlmostEqual(valeurs[a] - valeurs[c], 0.3 / 3, places=2)


class PropagationTests(Base):
    """Etape 4 : propagation dans les deux sens, idempotence, recommandation de lien."""

    def construire(self):
        vp = self.page('Parent', url='https://site.example/parent/')[0]
        v1 = self.page('Enfant 1')[0]
        v2 = self.page('Enfant 2')[0]
        p = self.unite(vp, 'Renault emploie 100 000 salaries.', centrality=0.9)
        c1 = self.unite(v1, 'Renault emploie 100 000 salaries, dont 40 000 en France.', centrality=0.2)
        c2 = self.unite(v2, 'Renault compte 100 000 salaries.', centrality=0.2, source_manquante=1)
        self.groupe('g', p, c1, c2)
        self.source(p, 'https://insee.fr/emploi')
        self.source(c1, 'https://lesechos.fr/renault')
        self.source(c1, 'https://site.example/parent/', kind='lien_interne')
        s.query("INSERT INTO unit_relations(a,b,relation,direction) VALUES(?,?,'precision',?)", (c1, p, c1))
        m3.paternite('g')
        return p, c1, c2

    def sources(self, unite):
        return s.query('SELECT url, inherited_from, contribution FROM sources WHERE unit_id=? ORDER BY url', (unite,))

    def test_propagation_deux_sens_et_idempotence(self):
        p, c1, c2 = self.construire()
        premier = m3.propager('g')
        parent = {(x['url'], x['inherited_from'], x['contribution']) for x in self.sources(p)}
        self.assertIn(('https://lesechos.fr/renault', c1, 'precise'), parent)
        self.assertIn(('https://insee.fr/emploi', None, None), parent)
        enfant1 = {(x['url'], x['inherited_from'], x['contribution']) for x in self.sources(c1)}
        self.assertIn(('https://insee.fr/emploi', p, 'heritee'), enfant1)
        self.assertEqual(sum(1 for x in enfant1 if x[0] == 'https://lesechos.fr/renault'), 1)
        enfant2 = {(x['url'], x['inherited_from'], x['contribution']) for x in self.sources(c2)}
        self.assertIn(('https://insee.fr/emploi', p, 'heritee'), enfant2)
        self.assertIn(('https://lesechos.fr/renault', p, 'heritee'), enfant2)
        total = s.query('SELECT count(*) n FROM sources', (), True)['n']
        second = m3.propager('g')
        self.assertEqual(s.query('SELECT count(*) n FROM sources', (), True)['n'], total)
        self.assertEqual(second['vers_parent'] + second['vers_enfants'] + second['supprimees'], 0)
        self.assertGreater(premier['vers_enfants'], 0)
        # Les sources heritees ne comptent pas pour la paternite.
        self.assertEqual(m3.paternite('g')['parent_unit_id'], p)

    def test_enfant_moins_precis_n_apporte_rien(self):
        p, c1, c2 = self.construire()
        s.query("UPDATE unit_relations SET direction=? WHERE a=?", (p, c1))
        m3.propager('g')
        self.assertNotIn('https://lesechos.fr/renault', [x['url'] for x in self.sources(p)])

    def test_recommandation_de_lien(self):
        p, c1, c2 = self.construire()
        m3.propager('g')
        m3.propager('g')
        recos = s.query('SELECT * FROM link_recommendations')
        self.assertEqual(len(recos), 1)
        reco = recos[0]
        self.assertEqual(reco['unit_id'], c2)
        self.assertEqual(reco['anchor'], 'Renault compte 100 000 salaries.')
        self.assertEqual(json.loads(reco['reason'])['motif'], 'source_manquante')
        self.assertEqual(reco['to_version'], s.query('SELECT version_id FROM units WHERE id=?', (p,), True)['version_id'])
        self.assertEqual(reco['status'], 'proposee')

    def test_arbitrage_retire_les_propagations_perimees(self):
        p, c1, c2 = self.construire()
        m3.propager('g')
        m3.arbitrer('g', c2)
        heritees_de_p = s.query("SELECT count(*) n FROM sources WHERE inherited_from=? AND contribution='heritee'", (p,), True)['n']
        self.assertEqual(heritees_de_p, 0)
        self.assertTrue(s.query("SELECT count(*) n FROM sources WHERE inherited_from=? AND contribution='heritee'", (c2,), True)['n'] > 0)
        # L'ancien parent devient un enfant : ses sources remontent vers le nouveau parent.
        self.assertIn(('https://insee.fr/emploi', p, 'equivalente'),
                      {(x['url'], x['inherited_from'], x['contribution']) for x in self.sources(c2)})
        self.assertEqual({r['unit_id'] for r in s.query('SELECT unit_id FROM link_recommendations')}, {p, c1})


class StatutsTests(Base):
    """Etape 5 : perimee_par_date (exemple du peripherique)."""

    def test_peripherique_aout_2026_perime_au_13_novembre(self):
        v = self.page('Travaux')[0]
        texte = 'Le boulevard peripherique parisien sera ferme du 1er au 31 août 2026.'
        a = self.unite(v, texte, obs_class='annonce', expires='2026-08-31')
        b = self.unite(v, texte, obs_class='annonce')
        c = self.unite(v, 'Le peripherique mesure 35 km.', obs_class='immuable')
        effectifs = m3.statuts_temporels(AUJOURDHUI)
        self.assertEqual(self.statut(a), 'perimee_par_date')
        self.assertEqual(self.statut(b), 'perimee_par_date')
        self.assertEqual(self.statut(c), 'a_jour')
        self.assertEqual(effectifs['perimee_par_date'], 2)
        m3.statuts_temporels('2026-07-01')
        self.assertEqual(self.statut(a), 'a_jour')
        self.assertEqual(self.statut(b), 'a_jour')


class ExecuterTests(Base):
    """Execution complete sur des groupes migres, sans moteur NLI."""

    def test_executer_sans_nli_fait_paternite_propagation_statuts(self):
        va = self.page('A', published='2023-01-01')[0]
        vb = self.page('B', published='2024-01-01')[0]
        a = self.unite(va, 'Renault emploie 100 000 salaries.')
        b = self.unite(vb, 'Renault emploie 100 000 salaries.')
        c = self.unite(vb, 'Phrase seule.')
        self.groupe('g1', a, b)
        self.groupe('g2', c)
        self.source(b, 'https://insee.fr/x')
        with patch.object(moteurs, 'nli_lot', side_effect=s.OperationalError('Aucun moteur disponible pour nli')):
            compteurs = m3.executer(aujourdhui=AUJOURDHUI)
        self.assertIn('nli_indisponible', compteurs['qualification'])
        self.assertEqual(compteurs['paternite']['groupes'], 2)
        self.assertEqual(compteurs['paternite']['triviaux'], 1)
        self.assertEqual(compteurs['paternite']['multi_pages'], 1)
        self.assertEqual(m3.paternite_de('g1')['parent_unit_id'], b)
        self.assertEqual(m3.paternite_de('g1')['criterion'], 'nombre_sources')
        self.assertEqual(s.query('SELECT count(*) n FROM sources WHERE inherited_from=?', (b,), True)['n'], 1)
        self.assertEqual(len(s.query('SELECT * FROM link_recommendations')), 1)
        etapes = {l['step']: l['status'] for l in s.query('SELECT step, status FROM step_runs')}
        self.assertEqual(etapes['m3.paternite'], 'terminee')
        self.assertTrue(all(l['run_id'] for l in s.query('SELECT run_id FROM paternity')))
        # Reprise : rien de refait sans forcer.
        with patch.object(moteurs, 'nli_lot', side_effect=s.OperationalError('Aucun moteur disponible pour nli')):
            reprise = m3.executer(aujourdhui=AUJOURDHUI)
        self.assertEqual(reprise['paternite']['deja_faits'], 2)
        self.assertEqual(reprise['propagation']['vers_enfants'], 0)
        self.assertIsNotNone(s.query('SELECT frame FROM unit_groups WHERE id=?', ('g1',), True)['frame'])

    def test_executer_qualifie_les_voisins(self):
        va = self.page('A')[0]
        vb = self.page('B')[0]
        ta = 'Le PDG de Renault est Alice Martin depuis la nomination du conseil.'
        tb = 'Le PDG de Renault est Bruno Durand depuis la nomination du conseil.'
        a = self.unite(va, ta, '2023-03-01')
        b = self.unite(vb, tb, '2025-03-01')
        self.regle(ta, tb, 'contradiction')
        self.regle(tb, ta, 'contradiction')
        with patch.object(moteurs, 'nli_lot', self.nli_double):
            compteurs = m3.executer(aujourdhui=AUJOURDHUI)
        self.assertEqual(compteurs['qualification']['relations'], {'remplacement': 1})
        self.assertEqual(self.statut(a), 'obsolete_historique')


if __name__ == '__main__':
    unittest.main()
