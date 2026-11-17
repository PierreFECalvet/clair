"""Tests du lot m1_sujets : embeddings, sujets (FCM) et deduplication des unites.

Aucun appel reseau : embeddings, NLI et LLM sont simules.
"""
import hashlib
import json
import math
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s
import chaine
from chaine import m1_sujets as m1
from chaine import moteurs
from chaine import noyau
from chaine import schema
from chaine import seuils

AUJOURDHUI = '2026-11-13'


def blobs(centres, par_groupe, dimension, bruit, graine=3):
    """Nuages gaussiens autour de centres aleatoires de norme 1 ; renvoie (points, etiquettes)."""
    hasard = random.Random(graine)
    points = []
    etiquettes = []
    for index in range(centres):
        centre = [hasard.gauss(0, 1) for _ in range(dimension)]
        norme = math.sqrt(sum(x * x for x in centre))
        centre = [x / norme for x in centre]
        for _ in range(par_groupe):
            points.append([x + hasard.gauss(0, bruit) for x in centre])
            etiquettes.append(index)
    return points, etiquettes


def probas(implication=0.0, contradiction=0.0):
    """Probabilites NLI completes."""
    return {'implication': implication, 'contradiction': contradiction,
            'neutre': max(0.0, 1.0 - implication - contradiction)}


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

    # --- fabrique de donnees -------------------------------------------------
    def page(self, titre, texte='Texte de la page.', vecteur=None):
        page_id = s.uid()
        version_id = s.uid()
        s.query('INSERT INTO pages(id,url,title) VALUES(?,?,?)', (page_id, 'https://exemple.test/' + page_id, titre))
        s.query('INSERT INTO versions(id,page_id,text,created,vector) VALUES(?,?,?,?,?)',
                (version_id, page_id, texte, s.now(), json.dumps(vecteur) if vecteur else None))
        return version_id

    def unite(self, version_id, texte, vecteur=None, groupe_legacy=False, identifiant=None):
        unite_id = identifiant or s.uid()
        s.query('INSERT INTO units(id,version_id,text,vector,excluded,extractor,legacy_claim_id,created) VALUES(?,?,?,?,0,?,?,?)',
                (unite_id, version_id, texte, json.dumps(vecteur) if vecteur else None,
                 'migration' if groupe_legacy else 'test', unite_id if groupe_legacy else None, s.now()))
        if groupe_legacy:
            s.query('INSERT INTO unit_groups(id,frame,label,legacy_claim_id,run_id,created) VALUES(?,NULL,?,?,?,?)',
                    (unite_id, texte[:300], unite_id, 'migration', s.now()))
            s.query('INSERT INTO unit_group_members(group_id,unit_id) VALUES(?,?)', (unite_id, unite_id))
        return unite_id

    def fait(self, unite_id, sujet_id, predicat, valeur, periode=None):
        s.query('INSERT INTO facts(id,unit_id,subject_id,subject,predicate,object,object_norm,period,created) VALUES(?,?,?,?,?,?,?,?,?)',
                (s.uid(), unite_id, sujet_id, sujet_id, predicat, valeur, valeur, periode, s.now()))

    def sujet_manuel(self, version_ids, methode='fcm', label='sujet'):
        """Un sujet qui contient toutes les unites des versions donnees."""
        topic = s.uid()
        s.query('INSERT INTO topics(id,label,method,centroid,run_id) VALUES(?,?,?,?,?)', (topic, label, methode, None, 'test'))
        for version_id in version_ids:
            s.query("INSERT INTO topic_membership(topic_id,target_kind,target_id,degree) VALUES(?,'version',?,1.0)", (topic, version_id))
        m1._heriter(methode)
        return topic

    def nombre(self, table, where='1=1'):
        return s.query('SELECT count(*) n FROM ' + table + ' WHERE ' + where, (), True)['n']


# ---------------------------------------------------------------------------
# Etape 7 : algorithmes
# ---------------------------------------------------------------------------

class AlgorithmesTests(Base):

    def test_fcm_appartenances_somment_a_un(self):
        points, _ = blobs(3, 20, 6, 0.05)
        U, V, J, fait = m1.fcm(points, 3, m=1.3, graine=1)
        for ligne in U:
            self.assertAlmostEqual(sum(ligne), 1.0, places=9)
            self.assertTrue(all(0.0 <= x <= 1.0 for x in ligne))
        self.assertEqual(len(V), 3)
        self.assertGreater(fait, 0)

    def test_fcm_refuse_m_inferieur_ou_egal_a_un(self):
        with self.assertRaises(ValueError):
            m1.fcm([[0.0, 1.0], [1.0, 0.0], [1.0, 1.0]], 2, m=1.0)

    def test_fcm_m_proche_de_un_sans_debordement(self):
        points, _ = blobs(3, 15, 5, 0.05)
        U, _, _, _ = m1.fcm(points, 3, m=1.05, graine=2)
        self.assertTrue(all(math.isfinite(x) for ligne in U for x in ligne))
        self.assertTrue(all(abs(sum(ligne) - 1.0) < 1e-9 for ligne in U))

    def test_indices_de_validite_retrouvent_le_bon_c(self):
        points, etiquettes = blobs(4, 25, 30, 0.12)
        calcul = m1.clusteriser(points, dimensions=10, m=1.3, c_min=2, c_max=8)
        self.assertEqual(calcul['c'], 4)
        # les deux indices sont fournis pour chaque c essaye
        self.assertEqual([ligne['c'] for ligne in calcul['indices']], list(range(2, 9)))
        for ligne in calcul['indices']:
            self.assertGreater(ligne['xie_beni'], 0.0)
            self.assertLessEqual(ligne['coefficient_partition'], 1.0)
        meilleur_xb = min(calcul['indices'], key=lambda ligne: ligne['xie_beni'])
        self.assertEqual(meilleur_xb['c'], 4)
        # chaque nuage tombe dans un sujet distinct, avec un degre dominant
        dominants = [max(range(4), key=lambda j: ligne[j]) for ligne in calcul['U']]
        for index in range(4):
            groupe = {d for d, e in zip(dominants, etiquettes) if e == index}
            self.assertEqual(len(groupe), 1)

    def test_choisir_c_combine_les_deux_indices(self):
        resultats = [
            {'c': 2, 'xie_beni': 0.30, 'pc_modifie': 0.60},
            {'c': 3, 'xie_beni': 0.05, 'pc_modifie': 0.90},
            {'c': 4, 'xie_beni': 0.20, 'pc_modifie': 0.70},
        ]
        self.assertEqual(m1.choisir_c(resultats), 3)

    def test_reduction_evite_la_degenerescence_en_haute_dimension(self):
        # 3 sujets, 600 dimensions, bruit fort : sans reduction les distances s'uniformisent.
        points, _ = blobs(3, 30, 600, 0.06, graine=11)
        U_brut, _, _, _ = m1.fcm(points, 3, m=1.5, graine=1)
        brut = sum(max(ligne) for ligne in U_brut) / len(U_brut)
        calcul = m1.clusteriser(points, c=3, dimensions=10, m=1.5)
        reduit = sum(max(ligne) for ligne in calcul['U']) / len(calcul['U'])
        self.assertLess(brut, 0.55, 'sans reduction, les degres doivent tendre vers 1/c')
        self.assertGreater(reduit, 0.95, 'apres reduction, les degres redeviennent tranches')

    def test_pca_numpy_dimensions_et_variance(self):
        points, _ = blobs(3, 20, 40, 0.05)
        Z, infos = m1.reduire(points, 10, 'pca')
        self.assertEqual(infos['methode'], 'pca')
        self.assertEqual(len(Z), 60)
        self.assertEqual(len(Z[0]), 10)
        self.assertGreater(infos['variance'], 0.9)

    def test_repli_pur_python_sans_numpy(self):
        points, _ = blobs(3, 12, 20, 0.05)
        with patch.object(m1, '_np', lambda: None):
            Z, infos = m1.reduire(points, 6, 'pca')
            self.assertEqual(infos['methode'], 'pca_python')
            self.assertLessEqual(len(Z[0]), 6)
            calcul = m1.clusteriser(points, dimensions=6, c_min=2, c_max=5)
        self.assertEqual(calcul['moteur'], 'python')
        self.assertEqual(calcul['c'], 3)
        for ligne in calcul['U']:
            self.assertAlmostEqual(sum(ligne), 1.0, places=9)

    def test_repli_pur_python_refuse_les_gros_volumes(self):
        points, _ = blobs(2, 5, 4, 0.1)
        with patch.object(m1, '_np', lambda: None):
            noyau.definir_reglage('seuil:m1.python.pages_max', '5')
            with self.assertRaises(s.OperationalError):
                m1.reduire(points, 3, 'pca')

    def test_affectation_au_dela_du_seuil_souvent_multiple(self):
        U = [[0.7, 0.25, 0.05], [0.9, 0.05, 0.05], [0.34, 0.33, 0.33]]
        retenues = m1.affecter(U, 0.2)
        self.assertEqual([j for j, _ in retenues[0]], [0, 1])
        self.assertEqual([j for j, _ in retenues[1]], [0])
        self.assertEqual(len(retenues[2]), 3)
        # aucun degre au-dessus du seuil : on garde le plus fort
        self.assertEqual([j for j, _ in m1.affecter([[0.15, 0.17, 0.16, 0.16, 0.18, 0.18]], 0.2)[0]], [4])

    def test_libelle_deterministe_ascii_sans_llm(self):
        textes = ['peripherique parisien travaux vitesse peripherique', 'peripherique parisien vitesse limitee',
                  'cotisations retraite regime retraite cotisations', 'regime retraite cotisations age']
        appartenances = [[(0, 1.0)], [(0, 1.0)], [(1, 1.0)], [(1, 1.0)]]
        etiquettes = m1.libelles(textes, appartenances, 2)
        self.assertEqual(etiquettes, m1.libelles(textes, appartenances, 2))
        self.assertIn('peripherique', etiquettes[0])
        self.assertIn('retraite', etiquettes[1])
        for libelle in etiquettes:
            self.assertTrue(libelle.isascii())


# ---------------------------------------------------------------------------
# Etape 7 : encodage et sujets en base
# ---------------------------------------------------------------------------

def faux_embeddings(textes):
    """Embeddings deterministes (hachage de mots) pour les tests."""
    resultat = []
    for texte in textes:
        vecteur = [0.0] * 16
        for mot in texte.lower().split():
            empreinte = hashlib.md5(mot.encode()).digest()
            for index in range(16):
                vecteur[index] += (empreinte[index] - 127.5) / 127.5
        norme = math.sqrt(sum(x * x for x in vecteur)) or 1.0
        resultat.append([x / norme for x in vecteur])
    return resultat, 'test:hachage'


class EncodageTests(Base):

    def test_encode_avec_contexte_par_lots_et_reprise(self):
        version = self.page('Le periph parisien', 'Texte de la page sur le periph.')
        for index in range(7):
            self.unite(version, 'Unite numero %d.' % index)
        vus = []

        def faux(textes):
            vus.append(list(textes))
            return faux_embeddings(textes)

        noyau.definir_reglage('seuil:m1.encodage.taille_lot', '3')
        with patch.object(moteurs, 'embeddings', faux):
            premier = m1.encoder('unites')
        self.assertEqual(premier['encodes'], 7)
        self.assertEqual(premier['lots'], 3)
        self.assertEqual([len(lot) for lot in vus], [3, 3, 1])
        self.assertTrue(all(texte.startswith('Le periph parisien\n') for lot in vus for texte in lot))
        self.assertEqual(self.nombre('units', "vector IS NOT NULL AND vector!=''"), 7)
        self.assertEqual(len(json.loads(s.query('SELECT vector FROM units LIMIT 1', (), True)['vector'])), 16)
        # reprise : plus rien a encoder
        with patch.object(moteurs, 'embeddings', faux):
            second = m1.encoder('unites')
        self.assertEqual(second['encodes'], 0)
        self.assertEqual(second['deja_encodes'], 7)
        # forcer re-encode tout
        with patch.object(moteurs, 'embeddings', faux):
            troisieme = m1.encoder('unites', forcer=True)
        self.assertEqual(troisieme['encodes'], 7)

    def test_reprise_apres_interruption(self):
        version = self.page('Titre')
        for index in range(6):
            self.unite(version, 'Affirmation %d.' % index)
        noyau.definir_reglage('seuil:m1.encodage.taille_lot', '2')
        appels = {'n': 0}

        def tombe(textes):
            appels['n'] += 1
            if appels['n'] == 2:
                s.STOP.set()
            return faux_embeddings(textes)

        with patch.object(moteurs, 'embeddings', tombe):
            with self.assertRaises(s.OperationalError):
                m1.encoder('unites')
        s.STOP.clear()
        fait = self.nombre('units', "vector IS NOT NULL AND vector!=''")
        self.assertEqual(fait, 4)
        with patch.object(moteurs, 'embeddings', faux_embeddings):
            suite = m1.encoder('unites')
        self.assertEqual(suite['encodes'], 2)

    def test_encode_les_pages_avec_titre_et_chapo(self):
        version = self.page('Retraite 2026', 'Le texte complet de la page.')
        s.query('UPDATE versions SET lede=? WHERE id=?', ('Chapo de la page.', version))
        self.unite(version, 'Une unite.')
        vus = []
        with patch.object(moteurs, 'embeddings', lambda textes: (vus.extend(textes), faux_embeddings(textes))[1]):
            resultat = m1.encoder('pages')
        self.assertEqual(resultat['encodes'], 1)
        self.assertTrue(vus[0].startswith('Retraite 2026\nChapo de la page.'))
        self.assertIsNotNone(s.query('SELECT vector FROM versions WHERE id=?', (version,), True)['vector'])

    def test_sans_moteur_d_embeddings_erreur_claire(self):
        version = self.page('Titre')
        self.unite(version, 'Affirmation.')
        with patch.object(moteurs, 'embeddings', lambda textes: ([], 'aucun')):
            with self.assertRaises(s.OperationalError) as erreur:
                m1.encoder('unites')
        self.assertIn('embeddings', str(erreur.exception))

    def test_cibles_inconnues(self):
        with self.assertRaises(ValueError):
            m1.encoder('chunks')


class SujetsTests(Base):

    def pages_synthetiques(self, sujets=3, par_sujet=8, dimension=32, bruit=0.08):
        points, etiquettes = blobs(sujets, par_sujet, dimension, bruit, graine=5)
        versions = []
        for index, (point, etiquette) in enumerate(zip(points, etiquettes)):
            version = self.page('Page %d sujet%d' % (index, etiquette), 'motsujet%d ' % etiquette * 12 + 'commun', point)
            versions.append(version)
            self.unite(version, 'Affirmation de la page %d.' % index, [0.1] * 4)
            self.unite(version, 'Deuxieme affirmation de la page %d.' % index, [0.2] * 4)
        return versions, etiquettes

    def test_sujets_fcm_stockage_heritage_et_libelles(self):
        versions, etiquettes = self.pages_synthetiques()
        resultat = m1.sujets('fcm')
        self.assertEqual(resultat['c'], 3)
        self.assertEqual(resultat['sujets'], 3)
        self.assertEqual(self.nombre('topics', "method='fcm'"), 3)
        # chaque page appartient a au moins un sujet ; une page est rattachee au sujet de son nuage
        par_version = {}
        for ligne in s.query("SELECT target_id, topic_id, degree FROM topic_membership WHERE target_kind='version'"):
            par_version.setdefault(ligne['target_id'], []).append((ligne['topic_id'], ligne['degree']))
            self.assertGreaterEqual(ligne['degree'], 0.2)
            self.assertLessEqual(ligne['degree'], 1.0 + 1e-9)
        self.assertEqual(len(par_version), len(versions))
        dominant = {v: max(lignes, key=lambda t: t[1])[0] for v, lignes in par_version.items()}
        for etiquette in range(3):
            self.assertEqual(len({dominant[v] for v, e in zip(versions, etiquettes) if e == etiquette}), 1)
        # les unites heritent des sujets de leur page, avec le meme degre
        self.assertEqual(self.nombre('topic_membership', "target_kind='unit'"),
                         sum(2 * len(lignes) for lignes in par_version.values()))
        unite = s.query('SELECT id, version_id FROM units LIMIT 1', (), True)
        herites = {ligne['topic_id']: ligne['degree'] for ligne in s.query(
            "SELECT topic_id, degree FROM topic_membership WHERE target_kind='unit' AND target_id=?", (unite['id'],))}
        self.assertEqual(herites, dict(par_version[unite['version_id']]))
        # libelles deterministes, ASCII, termes distinctifs
        libelles = [ligne['label'] for ligne in s.query("SELECT label FROM topics WHERE method='fcm' ORDER BY label")]
        self.assertTrue(all(libelle.isascii() for libelle in libelles))
        self.assertTrue(all('motsujet' in libelle for libelle in libelles))
        # centroides dans l'espace d'origine
        centroide = json.loads(s.query("SELECT centroid FROM topics WHERE method='fcm' LIMIT 1", (), True)['centroid'])
        self.assertEqual(len(centroide), 32)
        # run_id et ligne step_runs
        self.assertEqual(self.nombre('step_runs', "step='m1.sujets' AND status='terminee'"), 1)
        self.assertEqual(self.nombre('topics', 'run_id IS NULL'), 0)

    def test_sujets_reprise_sans_recalcul_puis_forcer(self):
        self.pages_synthetiques(sujets=2, par_sujet=6)
        premier = m1.sujets('fcm')
        ids_avant = {ligne['id'] for ligne in s.query('SELECT id FROM topics')}
        with patch.object(m1, 'clusteriser', side_effect=AssertionError('ne doit pas recalculer')):
            second = m1.sujets('fcm')
        self.assertTrue(second['saute'])
        self.assertEqual(ids_avant, {ligne['id'] for ligne in s.query('SELECT id FROM topics')})
        forcee = m1.sujets('fcm', forcer=True)
        self.assertFalse(forcee['saute'])
        self.assertEqual(self.nombre('topics'), forcee['sujets'])
        self.assertEqual(premier['sujets'], forcee['sujets'])

    def test_une_page_peut_appartenir_a_plusieurs_sujets(self):
        # une page placee entre deux nuages avec un m eleve : appartenance multiple
        points, _ = blobs(2, 10, 24, 0.05, graine=9)
        milieu = [(a + b) / 2 for a, b in zip(points[0], points[-1])]
        points.append(milieu)
        for index, point in enumerate(points):
            self.page('Page %d' % index, 'texte %d' % index, point)
        for version in s.query('SELECT id FROM versions'):
            self.unite(version['id'], 'Affirmation.', [0.1] * 4)
        noyau.definir_reglage('seuil:m1.fcm.m', '1.5')
        noyau.definir_reglage('seuil:m1.fcm.c_min', '2')
        noyau.definir_reglage('seuil:m1.fcm.c_max', '2')
        m1.sujets('fcm')
        nb = s.query("SELECT target_id, count(*) n FROM topic_membership WHERE target_kind='version' GROUP BY target_id")
        self.assertTrue(any(ligne['n'] >= 2 for ligne in nb))

    def test_hdbscan_indisponible_message_clair(self):
        self.pages_synthetiques(sujets=2, par_sujet=6)
        with patch.object(m1.importlib, 'import_module', side_effect=ImportError('hdbscan')):
            with self.assertRaises(s.OperationalError) as erreur:
                m1.sujets('hdbscan')
        self.assertIn('hdbscan', str(erreur.exception))

    def test_trop_peu_de_pages_un_seul_sujet(self):
        for index in range(3):
            version = self.page('P%d' % index, 'mot%d texte' % index, [1.0, 0.0, float(index)])
            self.unite(version, 'Affirmation.', [0.1] * 4)
        resultat = m1.sujets('fcm')
        self.assertEqual(resultat['sujets'], 1)
        self.assertEqual(self.nombre('topic_membership', "target_kind='unit'"), 3)

    def test_sujets_sans_pages_encodees(self):
        # Rien a encoder (aucune unite) : etape sautee proprement (relecture M4).
        self.assertTrue(m1.sujets('fcm')['saute'])
        # Des pages avec unites mais non encodees : erreur explicite.
        version = self.page('P', 'texte')
        s.query('UPDATE versions SET vector=NULL WHERE id=?', (version,))
        self.unite(version, 'Affirmation.')
        with self.assertRaises(s.OperationalError):
            m1.sujets('fcm')


# ---------------------------------------------------------------------------
# Etape 8 : deduplication
# ---------------------------------------------------------------------------

def nli_scripte(table, appels=None):
    """Double de ``moteurs.nli_lot`` : ``table[(premisse, hypothese)] = probas`` ; neutre sinon."""
    def nli_lot(paires):
        if appels is not None:
            appels.append(list(paires))
        return [dict(label='neutre', score=1.0, probas=table.get(paire, probas()), engine='test:nli') for paire in paires]
    return nli_lot


class DedupTests(Base):

    def deux_unites_proches(self, texte_a='Le PDG de Acme est Jean Martin.', texte_b='Jean Martin dirige Acme.', legacy=True):
        va = self.page('Page A')
        vb = self.page('Page B')
        a = self.unite(va, texte_a, [1.0, 0.0, 0.0, 0.0], groupe_legacy=legacy, identifiant='ua')
        b = self.unite(vb, texte_b, [0.99, 0.05, 0.0, 0.0], groupe_legacy=legacy, identifiant='ub')
        self.sujet_manuel([va, vb])
        return a, b, texte_a, texte_b

    def relation(self, a='ua', b='ub'):
        return s.query('SELECT * FROM unit_relations WHERE a=? AND b=?', (a, b), True)

    def test_doublons_exacts_sur_triplets_sans_modele(self):
        va, vb, vc = self.page('A'), self.page('B'), self.page('C')
        a = self.unite(va, 'Acme a pour dirigeant Jean Martin.', groupe_legacy=True, identifiant='u1')
        b = self.unite(vb, 'Jean Martin est le PDG de Acme.', groupe_legacy=True, identifiant='u2')
        autre = self.unite(vc, 'Acme a pour dirigeant Paul Durand.', groupe_legacy=True, identifiant='u3')
        for unite, valeur in ((a, 'jean martin'), (b, 'jean martin'), (autre, 'paul durand')):
            self.fait(unite, 'ent_acme', 'dirigeant', valeur)
        with patch.object(moteurs, 'nli_lot', side_effect=AssertionError('aucun NLI pour un doublon exact')):
            resultat = m1.dedoublonner()
        self.assertEqual(resultat['classes_exactes'], 1)
        relation = self.relation('u1', 'u2')
        self.assertEqual((relation['relation'], relation['engine'], relation['direction']), ('equivalence', 'regles', 'mutuelle'))
        self.assertIsNone(self.relation('u1', 'u3'))
        groupe = s.query("SELECT group_id FROM unit_group_members WHERE unit_id='u1'", (), True)['group_id']
        self.assertEqual(s.query("SELECT group_id FROM unit_group_members WHERE unit_id='u2'", (), True)['group_id'], groupe)
        self.assertNotEqual(s.query("SELECT group_id FROM unit_group_members WHERE unit_id='u3'", (), True)['group_id'], groupe)

    def test_periodes_differentes_ne_sont_pas_des_doublons_exacts(self):
        va, vb = self.page('A'), self.page('B')
        a = self.unite(va, 'Acme a 200 salaries en 2023.', identifiant='ua')
        b = self.unite(vb, 'Acme emploie 200 personnes en 2024.', identifiant='ub')
        self.fait(a, 'ent_acme', 'effectif', '200', '2023')
        self.fait(b, 'ent_acme', 'effectif', '200', '2024')
        resultat = m1.dedoublonner()
        self.assertEqual(resultat['classes_exactes'], 0)
        self.assertIsNone(self.relation())

    def test_texte_identique_apres_normalisation(self):
        va, vb = self.page('A'), self.page('B')
        self.unite(va, 'Le périphérique est limité à 50 km/h.', identifiant='ua')
        self.unite(vb, 'le peripherique est limite a 50 km/h', identifiant='ub')
        resultat = m1.dedoublonner()
        self.assertEqual(resultat['classes_exactes'], 1)
        self.assertEqual(self.relation()['relation'], 'equivalence')

    def test_issue_equivalence_par_implication_mutuelle(self):
        a, b, ta, tb = self.deux_unites_proches()
        table = {(ta, tb): probas(0.95), (tb, ta): probas(0.93)}
        with patch.object(moteurs, 'nli_lot', nli_scripte(table)):
            resultat = m1.dedoublonner()
        relation = self.relation()
        self.assertEqual((relation['relation'], relation['direction']), ('equivalence', 'mutuelle'))
        self.assertAlmostEqual(relation['nli_ab'], 0.95)
        self.assertEqual(resultat['equivalence'], 1)
        self.assertEqual(resultat['groupes_fusionnes'], 1)

    def test_issue_precision_dans_un_seul_sens(self):
        a, b, ta, tb = self.deux_unites_proches('Le PDG de Acme, Jean Martin, a ete nomme en 2020.', 'Jean Martin dirige Acme.')
        table = {(ta, tb): probas(0.92), (tb, ta): probas(0.10)}
        with patch.object(moteurs, 'nli_lot', nli_scripte(table)):
            resultat = m1.dedoublonner()
        relation = self.relation()
        self.assertEqual((relation['relation'], relation['direction']), ('precision', 'a_precise_b'))
        self.assertEqual(resultat['precision'], 1)
        # une precision ne fusionne pas les groupes
        self.assertEqual(resultat['groupes_fusionnes'], 0)
        self.assertEqual(self.nombre('unit_groups', 'merged_into IS NOT NULL'), 0)

    def test_issue_contradiction_transmise_a_m3_sans_trancher(self):
        a, b, ta, tb = self.deux_unites_proches('Le PDG de Acme est Jean Martin.', 'Le PDG de Acme est Paul Durand.')
        table = {(ta, tb): probas(0.0, 0.96), (tb, ta): probas(0.0, 0.95)}
        with patch.object(moteurs, 'nli_lot', nli_scripte(table)):
            resultat = m1.dedoublonner()
        relation = self.relation()
        self.assertEqual(relation['relation'], 'conflit')
        self.assertIn('transmis_m3', relation['reason'])
        self.assertEqual(resultat['conflit'], 1)
        self.assertEqual(resultat['groupes_fusionnes'], 0)
        self.assertEqual(self.nombre('unit_groups', 'merged_into IS NOT NULL'), 0)

    def test_issue_sans_rapport(self):
        a, b, ta, tb = self.deux_unites_proches('Paris est en France.', 'Le chat dort sur le canape.')
        with patch.object(moteurs, 'nli_lot', nli_scripte({})):
            resultat = m1.dedoublonner()
        self.assertEqual(self.relation()['relation'], 'sans_rapport')
        self.assertEqual(resultat['sans_rapport'], 1)

    def test_classement_des_probabilites(self):
        self.assertEqual(m1.qualifier_probas(probas(0.9), probas(0.9)), ('equivalence', 'mutuelle'))
        self.assertEqual(m1.qualifier_probas(probas(0.9), probas(0.1)), ('precision', 'a_precise_b'))
        self.assertEqual(m1.qualifier_probas(probas(0.1), probas(0.9)), ('precision', 'b_precise_a'))
        self.assertEqual(m1.qualifier_probas(probas(0, 0.9), probas(0.1, 0.1))[0], 'conflit')
        self.assertEqual(m1.qualifier_probas(probas(0.1), probas(0.2))[0], 'sans_rapport')
        self.assertEqual(m1.qualifier_probas(probas(0.6), probas(0.1))[0], 'zone_grise')
        self.assertEqual(m1.qualifier_probas(probas(0.9), probas(0.6))[0], 'zone_grise')
        self.assertEqual(m1.qualifier_probas(probas(0.9), probas(0.0, 0.9))[0], 'zone_grise')

    def test_zone_grise_tranchee_par_le_llm_simule(self):
        a, b, ta, tb = self.deux_unites_proches()
        table = {(ta, tb): probas(0.60), (tb, ta): probas(0.55)}
        envois = []

        def faux_llm(tache, payload):
            envois.append(payload)
            return {'resultats': [{'id': p['id'], 'relation': 'equivalence'} for p in payload['paires']]}

        with patch.object(moteurs, 'nli_lot', nli_scripte(table)), patch.object(s, 'llm', faux_llm):
            resultat = m1.dedoublonner()
        self.assertEqual(len(envois), 1)
        self.assertEqual(envois[0]['mode'], 'chaine:dedup_paire')
        self.assertEqual(resultat['zone_grise'], 1)
        self.assertEqual(resultat['llm_tranchees'], 1)
        relation = self.relation()
        self.assertEqual(relation['relation'], 'equivalence')
        self.assertTrue(relation['engine'].startswith('llm:'))
        self.assertEqual(resultat['groupes_fusionnes'], 1)

    def test_le_llm_ne_voit_pas_les_paires_hors_zone_grise(self):
        a, b, ta, tb = self.deux_unites_proches()
        table = {(ta, tb): probas(0.95), (tb, ta): probas(0.95)}
        with patch.object(moteurs, 'nli_lot', nli_scripte(table)), patch.object(s, 'llm', side_effect=AssertionError('LLM interdit')):
            m1.dedoublonner()
        self.assertEqual(self.relation()['relation'], 'equivalence')

    def test_llm_hors_schema_rejete_et_paire_non_tranchee(self):
        a, b, ta, tb = self.deux_unites_proches()
        table = {(ta, tb): probas(0.6), (tb, ta): probas(0.5)}
        with patch.object(moteurs, 'nli_lot', nli_scripte(table)), \
                patch.object(s, 'llm', lambda tache, payload: {'resultats': [{'id': 0, 'relation': 'peut-etre'}]}):
            resultat = m1.dedoublonner()
        self.assertEqual(resultat['non_tranchees'], 1)
        self.assertIsNone(self.relation())

    def test_zone_grise_tranchee_conserve_la_decision_en_cache(self):
        a, b, ta, tb = self.deux_unites_proches()
        table = {(ta, tb): probas(0.6), (tb, ta): probas(0.5)}
        compteur = {'llm': 0}

        def faux_llm(tache, payload):
            compteur['llm'] += 1
            return {'resultats': [{'id': p['id'], 'relation': 'sans_rapport'} for p in payload['paires']]}

        with patch.object(moteurs, 'nli_lot', nli_scripte(table)), patch.object(s, 'llm', faux_llm):
            m1.dedoublonner()
            m1.dedoublonner()
        self.assertEqual(compteur['llm'], 1)

    def test_les_doublons_ne_sont_pas_supprimes(self):
        a, b, ta, tb = self.deux_unites_proches()
        avant_unites = self.nombre('units')
        avant_membres = {(r['group_id'], r['unit_id']) for r in s.query('SELECT * FROM unit_group_members')}
        avant_groupes = {r['id'] for r in s.query('SELECT id FROM unit_groups')}
        table = {(ta, tb): probas(0.95), (tb, ta): probas(0.95)}
        with patch.object(moteurs, 'nli_lot', nli_scripte(table)):
            m1.dedoublonner()
        self.assertEqual(self.nombre('units'), avant_unites)
        # tous les identifiants de groupes existent encore ; aucun membre perdu
        apres_groupes = {r['id'] for r in s.query('SELECT id FROM unit_groups')}
        self.assertTrue(avant_groupes <= apres_groupes)
        membres = {r['unit_id'] for r in s.query('SELECT unit_id FROM unit_group_members')}
        self.assertTrue({u for _, u in avant_membres} <= membres)
        # les deux unites sont dans le meme groupe, le groupe absorbe renvoie vers le survivant
        groupes = {r['unit_id']: r['group_id'] for r in s.query('SELECT * FROM unit_group_members')}
        self.assertEqual(groupes['ua'], groupes['ub'])
        absorbe = s.query('SELECT id, merged_into, legacy_claim_id FROM unit_groups WHERE merged_into IS NOT NULL', (), True)
        self.assertEqual(absorbe['merged_into'], groupes['ua'])
        self.assertTrue(absorbe['legacy_claim_id'])
        self.assertEqual(self.nombre('unit_group_members', "group_id='" + absorbe['id'] + "'"), 0)

    def test_groupes_migres_etendus_sans_casser_les_identifiants(self):
        va, vb = self.page('A'), self.page('B')
        # groupe migre a deux membres + une unite recente sans groupe
        self.unite(va, 'Texte une.', [1.0, 0.0, 0.0, 0.0], groupe_legacy=True, identifiant='g1')
        self.unite(va, 'Texte une bis.', [0.0, 1.0, 0.0, 0.0], identifiant='g1b')
        s.query("INSERT INTO unit_group_members(group_id,unit_id) VALUES('g1','g1b')")
        nouvelle = self.unite(vb, 'Texte une bis bis.', [0.995, 0.02, 0.0, 0.0], identifiant='zz')
        self.sujet_manuel([va, vb])
        table = {('Texte une.', 'Texte une bis bis.'): probas(0.95), ('Texte une bis bis.', 'Texte une.'): probas(0.95)}
        with patch.object(moteurs, 'nli_lot', nli_scripte(table)):
            resultat = m1.dedoublonner()
        self.assertEqual(resultat['groupes_crees'], 1)
        groupes = {r['unit_id']: r['group_id'] for r in s.query('SELECT * FROM unit_group_members')}
        self.assertEqual(groupes['g1'], 'g1')
        self.assertEqual(groupes['g1b'], 'g1')
        self.assertEqual(groupes['zz'], 'g1', 'la nouvelle unite rejoint le groupe migre, qui survit')
        self.assertEqual(self.nombre('unit_groups', "id='g1' AND merged_into IS NULL"), 1)

    def test_fusion_de_deux_groupes_migres_garde_les_deux_identifiants(self):
        a, b, ta, tb = self.deux_unites_proches()
        table = {(ta, tb): probas(0.95), (tb, ta): probas(0.95)}
        with patch.object(moteurs, 'nli_lot', nli_scripte(table)):
            m1.dedoublonner()
        ids = {r['id'] for r in s.query('SELECT id FROM unit_groups')}
        self.assertEqual(ids, {'ua', 'ub'})
        self.assertEqual(s.query("SELECT merged_into FROM unit_groups WHERE id='ub'", (), True)['merged_into'], 'ua')
        self.assertEqual(self.nombre('unit_group_members', "group_id='ua'"), 2)

    def test_chaque_unite_a_un_groupe_et_relance_idempotente(self):
        va = self.page('A')
        for index in range(3):
            self.unite(va, 'Unite distincte numero %d sur autre chose.' % index, [float(index == i) for i in range(4)])
        m1.dedoublonner()
        self.assertEqual(self.nombre('unit_group_members'), 3)
        self.assertEqual(self.nombre('unit_groups'), 3)
        etat = (self.nombre('unit_groups'), self.nombre('unit_group_members'), self.nombre('unit_relations'))
        m1.dedoublonner()
        self.assertEqual(etat, (self.nombre('unit_groups'), self.nombre('unit_group_members'), self.nombre('unit_relations')))

    def test_candidats_a_l_interieur_d_un_meme_sujet_seulement(self):
        va, vb = self.page('A'), self.page('B')
        self.unite(va, 'Phrase une.', [1.0, 0.0, 0.0, 0.0], identifiant='ua')
        self.unite(vb, 'Phrase une bis.', [1.0, 0.0, 0.0, 0.0], identifiant='ub')
        self.sujet_manuel([va], label='sujet1')
        self.sujet_manuel([vb], label='sujet2')
        paires, stats = m1.candidats_semantiques('fcm')
        self.assertEqual(paires, [])
        self.assertEqual(stats['portees'], 2)
        # un unite appartenant aux deux sujets suffit a creer la paire
        s.query("INSERT INTO topic_membership(topic_id,target_kind,target_id,degree) "
                "SELECT topic_id,'unit','ub',0.5 FROM topic_membership WHERE target_kind='unit' AND target_id='ua'")
        paires, _ = m1.candidats_semantiques('fcm')
        self.assertEqual([(a, b) for a, b, _ in paires], [('ua', 'ub')])

    def test_seuil_cosinus_et_voisins_max(self):
        v = self.page('A')
        for index, vecteur in enumerate(([1.0, 0.0], [0.99, 0.14], [0.7, 0.7], [0.0, 1.0])):
            self.unite(v, 'Unite %d.' % index, vecteur, identifiant='u%d' % index)
        self.sujet_manuel([v])
        paires, _ = m1.candidats_semantiques('fcm', k=3, seuil=0.95)
        self.assertEqual({(a, b) for a, b, _ in paires}, {('u0', 'u1')})
        paires, _ = m1.candidats_semantiques('fcm', k=1, seuil=0.0)
        self.assertLessEqual(len(paires), 4)
        # chaque unite n'a au plus qu'un voisin : au plus 4 paires, toutes de cosinus le plus fort
        self.assertEqual(paires[0][:2], ('u0', 'u1'))

    def test_sans_sujet_portee_unique(self):
        va, vb = self.page('A'), self.page('B')
        self.unite(va, 'Phrase une.', [1.0, 0.0, 0.0, 0.0], identifiant='ua')
        self.unite(vb, 'Phrase une bis.', [1.0, 0.0, 0.0, 0.0], identifiant='ub')
        paires, stats = m1.candidats_semantiques('fcm')
        self.assertFalse(stats['avec_sujets'])
        self.assertEqual(len(paires), 1)

    def test_nli_borne_et_annonce_avant_execution(self):
        va = self.page('A')
        for index in range(6):
            self.unite(va, 'Variante numero %d de la meme idee.' % index, [1.0, 0.01 * index, 0.0, 0.0], identifiant='u%d' % index)
        self.sujet_manuel([va])
        noyau.definir_reglage('seuil:dedup.nli_paires_max', '3')
        noyau.definir_reglage('seuil:dedup.voisins_max', '5')
        messages = []
        appels = []

        def espion(paires):
            messages.append(s.JOB['message'])
            appels.append(len(paires))
            return [dict(label='neutre', score=1.0, probas=probas(), engine='test:nli') for _ in paires]

        with patch.object(moteurs, 'nli_lot', espion):
            premier = m1.dedoublonner()
        self.assertEqual(premier['paires_nli'], 3)
        self.assertEqual(premier['appels_nli'], 6)
        self.assertGreater(premier['tronquees'], 0)
        self.assertEqual(sum(appels), 6)
        # annonce du nombre d'appels NLI avant le premier appel
        self.assertIn('6 appels NLI', messages[0])
        self.assertIn('reportees', messages[0])
        # relance : continue la ou on s'etait arrete, sans repayer les 3 premieres paires
        deja = premier['paires_nli']
        with patch.object(moteurs, 'nli_lot', espion):
            second = m1.dedoublonner()
        self.assertEqual(second['deja_qualifiees'], deja)
        self.assertEqual(second['paires_nli'], 3)

    def test_cache_pas_de_second_paiement_et_forcer(self):
        a, b, ta, tb = self.deux_unites_proches()
        appels = []
        table = {(ta, tb): probas(0.1), (tb, ta): probas(0.1)}
        with patch.object(moteurs, 'nli_lot', nli_scripte(table, appels)):
            premier = m1.dedoublonner()
            second = m1.dedoublonner()
            force = m1.dedoublonner(forcer=True)
        self.assertEqual(premier['paires_nli'], 1)
        self.assertEqual(second['paires_nli'], 0)
        self.assertEqual(second['deja_qualifiees'], 1)
        self.assertEqual(force['paires_nli'], 1)
        self.assertEqual(len(appels), 2)
        self.assertEqual(self.relation()['relation'], 'sans_rapport')

    def test_cache_invalide_quand_le_texte_change(self):
        a, b, ta, tb = self.deux_unites_proches()
        appels = []
        with patch.object(moteurs, 'nli_lot', nli_scripte({}, appels)):
            m1.dedoublonner()
            s.query("UPDATE units SET text='Texte modifie.' WHERE id='ub'")
            second = m1.dedoublonner()
        self.assertEqual(second['paires_nli'], 1)
        self.assertEqual(len(appels), 2)

    def test_relation_d_un_autre_module_n_est_pas_ecrasee(self):
        a, b, ta, tb = self.deux_unites_proches()
        s.query("INSERT INTO unit_relations(a,b,relation,direction,nli_ab,nli_ba,engine,reason,run_id,created) "
                "VALUES('ua','ub','faux_conflit','',0,0,'m3','arbitrage humain','x',?)", (s.now(),))
        with patch.object(moteurs, 'nli_lot', nli_scripte({(ta, tb): probas(0.95), (tb, ta): probas(0.95)})):
            m1.dedoublonner()
        self.assertEqual(self.relation()['relation'], 'faux_conflit')

    def test_paires_deja_dans_le_meme_groupe_sont_ignorees(self):
        a, b, ta, tb = self.deux_unites_proches()
        s.query("INSERT INTO unit_group_members(group_id,unit_id) VALUES('ua','ub')")
        s.query("DELETE FROM unit_group_members WHERE group_id='ub'")
        with patch.object(moteurs, 'nli_lot', side_effect=AssertionError('deja groupees')):
            resultat = m1.dedoublonner()
        self.assertEqual(resultat['paires_nli'], 0)

    def test_executer_enchaine_les_etapes(self):
        points, etiquettes = blobs(2, 6, 12, 0.05, graine=21)
        for index, point in enumerate(points):
            version = self.page('Page %d' % index, 'sujet%d texte %s' % (etiquettes[index], 'mot' * 3))
            self.unite(version, 'Affirmation de la page %d sur sujet%d.' % (index, etiquettes[index]))
        with patch.object(moteurs, 'embeddings', faux_embeddings), patch.object(moteurs, 'nli_lot', nli_scripte({})):
            resultat = m1.executer()
        self.assertEqual(resultat['encodage_pages']['encodes'], 12)
        self.assertEqual(resultat['encodage_unites']['encodes'], 12)
        self.assertGreaterEqual(resultat['sujets']['sujets'], 1)
        self.assertEqual(resultat['dedup']['unites'], 12)
        # seconde passe : tout est repris du cache et des vecteurs
        with patch.object(moteurs, 'embeddings', side_effect=AssertionError('deja encodes')), \
                patch.object(moteurs, 'nli_lot', side_effect=AssertionError('deja qualifiees')):
            second = m1.executer()
        self.assertEqual(second['encodage_unites']['encodes'], 0)
        self.assertTrue(second['sujets']['saute'])
        self.assertEqual(second['dedup']['paires_nli'], 0)


class VoisinsTests(Base):

    def test_numpy_par_blocs_equivaut_a_python(self):
        points, _ = blobs(3, 15, 8, 0.2, graine=4)
        matrice = m1._normaliser(points)
        reference = m1._voisins_python([list(map(float, ligne)) for ligne in matrice], 5, 0.7)
        par_blocs = m1._voisins_numpy(matrice, 5, 0.7, 7)
        self.assertEqual(set(reference), set(par_blocs))
        for cle, valeur in reference.items():
            self.assertAlmostEqual(valeur, par_blocs[cle], places=4)


if __name__ == '__main__':
    unittest.main()
