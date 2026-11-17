"""Tests de l'export Excel et du protocole d'evaluation (lot M6)."""
import copy
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s
import chaine
from chaine import evaluation
from chaine import export
from chaine import noyau
from chaine import schema
from chaine import seuils

REFERENCE = Path(__file__).resolve().parents[1] / 'docs' / 'reference_exemple.json'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        s.DB = Path(self.tmp.name) / 'test.sqlite3'
        s.init()
        s.STOP.clear()
        self.config = dict(s.CONFIG)
        s.CONFIG.update(embeddings=False, ibou_key='test-only', model='test-model', provider='lmstudio', openai_key='')
        chaine.brancher(s)
        schema.init()
        noyau.vider_reglages()
        seuils._vider_cache()
        self.reference = json.loads(REFERENCE.read_text(encoding='utf-8'))

    def tearDown(self):
        s.CONFIG.clear()
        s.CONFIG.update(self.config)
        noyau.vider_reglages()
        seuils._vider_cache()
        self.tmp.cleanup()

    def installer_pages(self):
        """Cree en base les deux pages du jeu de reference (memes identifiants et textes)."""
        for page in self.reference['pages']:
            pid = 'p-' + page['version_id']
            s.query('INSERT INTO pages VALUES(?,?,?)', (pid, page['url'], page['url']))
            s.query('INSERT INTO versions(id,page_id,text,created,published,hash) VALUES(?,?,?,?,?,?)',
                    (page['version_id'], pid, page['texte'], s.now(), '2026-01-01', page['version_id']))

    def unite(self, ident, classe, extra=None, **champs):
        for page in self.reference['pages']:
            for u in page['unites']:
                if u['id'] == ident:
                    valeurs = dict(id=ident, version_id=page['version_id'], text=page['texte'][u['span'][0]:u['span'][1]], quote=page['texte'][u['span'][0]:u['span'][1]],
                                   span_start=u['span'][0], span_end=u['span'][1], obs_class=classe, status='a_verifier', temporal_status='a_jour', excluded=0)
                    valeurs.update(champs)
                    s.query('INSERT INTO units(' + ','.join(valeurs) + ') VALUES(' + ','.join('?' for _ in valeurs) + ')', tuple(valeurs.values()))
                    return u
        raise KeyError(ident)

    def fait(self, ident, unite, debut, fin, **champs):
        valeurs = dict(id=ident, unit_id=unite, subject='x', predicate='p', object='o', span_start=debut, span_end=fin, status='actif')
        valeurs.update(champs)
        s.query('INSERT INTO facts(' + ','.join(valeurs) + ') VALUES(' + ','.join('?' for _ in valeurs) + ')', tuple(valeurs.values()))

    def jeu_complet(self):
        """Base avec des predictions volontairement imparfaites (voir les valeurs attendues dans les tests)."""
        self.installer_pages()
        gold = {u['id']: u for page in self.reference['pages'] for u in page['unites']}
        classes = {'ex-u1': 'annonce', 'ex-u2': 'etat_courant', 'ex-u3': 'annonce', 'ex-u4': 'usage'}
        for ident, classe in classes.items():
            self.unite(ident, classe)
        for ident in ('ex-u1', 'ex-u2', 'ex-u3'):
            debut, fin = gold[ident]['faits'][0]['span']
            self.fait('f-' + ident, ident, debut, fin, nli_score=0.95)
        self.fait('f-extra', 'ex-u3', 0, 5, nli_score=0.2)
        for ident in ('ex-u1', 'ex-u2', 'ex-u3', 'ex-u4'):
            s.query('INSERT INTO claim_types(unit_id,type) VALUES(?,?)', (ident, 'information_source'))
        s.query("INSERT INTO unit_groups(id,frame) VALUES('g1','{}'),('g2','{}')")
        for groupe, unites in (('g1', ('ex-u1', 'ex-u4')), ('g2', ('ex-u2', 'ex-u3'))):
            for u in unites:
                s.query('INSERT INTO unit_group_members VALUES(?,?)', (groupe, u))
        s.query("INSERT INTO paternity(group_id,parent_unit_id,criterion) VALUES('g2','ex-u3','date_publication')")
        s.query("INSERT INTO unit_relations(a,b,relation) VALUES('ex-u2','ex-u3','conflit'),('ex-u1','ex-u4','conflit')")
        self.reference['paires'].append({'a': 'ex-u1', 'b': 'ex-u4', 'relation': 'equivalence'})


def mesure(lignes, fragment):
    trouvees = [ligne for ligne in lignes if fragment in ligne['mesure']]
    assert len(trouvees) == 1, (fragment, [ligne['mesure'] for ligne in lignes])
    return trouvees[0]


class ReferenceTests(Base):
    def test_exemple_valide_et_chargeable(self):
        self.assertEqual(evaluation.valider(self.reference), [])
        charge = evaluation.charger(str(REFERENCE))
        self.assertEqual(len(charge['pages']), 2)
        self.assertEqual(evaluation.reference_courante()['nom'], charge['nom'])

    def test_un_fait_sans_span_est_refuse(self):
        mauvais = copy.deepcopy(self.reference)
        mauvais['pages'][0]['unites'][0]['faits'][0].pop('span')
        self.assertTrue(evaluation.valider(mauvais))
        with self.assertRaises(ValueError):
            evaluation.charger(mauvais)

    def test_relation_et_classe_inconnues(self):
        mauvais = copy.deepcopy(self.reference)
        mauvais['paires'][0]['relation'] = 'bizarre'
        mauvais['pages'][0]['unites'][0]['classe'] = 'xx'
        self.assertEqual(len(evaluation.valider(mauvais)), 2)

    def test_gabarit_prerempli_a_relire(self):
        self.jeu_complet()
        modele = evaluation.gabarit(['v-ex-1', 'v-ex-2'])
        self.assertEqual([len(p['unites']) for p in modele['pages']], [2, 2])
        premiere = modele['pages'][0]['unites'][0]
        self.assertEqual((premiere['classe'], premiere['relu']), ('annonce', False))
        self.assertEqual(len(premiere['faits']), 1)
        groupe = [g for g in modele['groupes'] if g['id'] == 'g2'][0]
        self.assertEqual(groupe['page_parente'], 'v-ex-2')
        self.assertEqual({p['relation'] for p in modele['paires']}, {'contradiction'})
        self.assertEqual(evaluation.valider(dict(modele, groupes=[g for g in modele['groupes'] if g['page_parente']])), [])


class MesuresTests(Base):
    def lancer(self):
        evaluation.charger(self.reference)
        return evaluation.mesurer()

    def test_faits_precision_et_rappel(self):
        self.jeu_complet()
        lignes = self.lancer()
        precision = mesure(lignes, 'Précision des faits (appariés')
        self.assertEqual((precision['valeur'], precision['seuil'], precision['ok']), (0.75, 0.98, False))
        rappel = mesure(lignes, 'Rappel par rapport aux faits annotés')
        self.assertEqual((rappel['valeur'], rappel['seuil'], rappel['ok']), (1.0, 0.8, True))
        nli = mesure(lignes, 'NLI stocké')
        self.assertEqual(nli['valeur'], 0.75)

    def test_f1_classes(self):
        self.jeu_complet()
        ligne = mesure(self.lancer(), 'classes d\'obsolescence')
        self.assertAlmostEqual(ligne['valeur'], 0.7778, places=4)
        self.assertFalse(ligne['ok'])

    def test_dedup_precision_et_rappel(self):
        self.jeu_complet()
        lignes = self.lancer()
        self.assertEqual(mesure(lignes, 'Précision sur les paires')['valeur'], 0.5)
        rappel = mesure(lignes, 'Rappel sur les paires')
        self.assertEqual((rappel['valeur'], rappel['ok']), (1.0, True))

    def test_typologie_par_type(self):
        self.jeu_complet()
        lignes = self.lancer()
        info = mesure(lignes, 'F1 du type information_source')
        self.assertAlmostEqual(info['valeur'], 0.8571, places=4)
        self.assertTrue(info['ok'])
        opinion = mesure(lignes, 'F1 du type opinion (prioritaire)')
        self.assertEqual((opinion['valeur'], opinion['ok']), (0.0, False))

    def test_paternite(self):
        self.jeu_complet()
        ligne = mesure(self.lancer(), 'bonne page parente')
        self.assertEqual((ligne['valeur'], ligne['ok']), (1.0, True))
        s.query("UPDATE paternity SET parent_unit_id='ex-u2'")
        self.assertEqual(mesure(evaluation.mesurer(), 'bonne page parente')['valeur'], 0.0)

    def test_faux_conflits(self):
        self.jeu_complet()
        ligne = mesure(self.lancer(), 'Faux conflits')
        self.assertEqual((ligne['valeur'], ligne['seuil'], ligne['ok']), (0.5, 0.10, False))

    def test_appariement_par_recouvrement_de_span(self):
        self.installer_pages()
        u = self.reference['pages'][0]['unites'][0]
        decale = dict(id='autre-id')
        self.unite('ex-u1', 'annonce', id=decale['id'], span_start=u['span'][0] + 2, span_end=u['span'][1] - 2)
        evaluation.charger(self.reference)
        ligne = mesure(evaluation.mesurer(), 'classes d\'obsolescence')
        self.assertEqual(ligne['valeur'], 1.0)

    def test_m6_part_acceptee_sans_modification(self):
        self.installer_pages()
        evaluation.charger(self.reference)
        self.assertIsNone(mesure(evaluation.mesurer(), 'acceptée sans modification')['valeur'])
        from chaine import m6
        m6.assurer_schema()
        for i, (statut, decision) in enumerate([('appliquee', 'accepter'), ('appliquee', 'accepter'), ('appliquee', 'accepter'),
                                                ('modifiee', 'modifier'), ('rejetee', 'rejeter')]):
            s.query('INSERT INTO proposals(id,kind,"before","after",status,decision) VALUES(?,?,?,?,?,?)', (f'pr{i}', 'corrigee', 'a', 'b', statut, decision))
        ligne = mesure(evaluation.mesurer(), 'acceptée sans modification')
        self.assertEqual((ligne['valeur'], ligne['seuil'], ligne['ok']), (0.6, 0.70, False))

    def test_couts_par_page_depuis_step_runs(self):
        self.installer_pages()
        s.query("INSERT INTO step_runs(id,step,status,pages,local_seconds,tokens_in,tokens_out,ibou_units) VALUES('r1','m6.proposer','terminee',4,20.0,400,80,12)")
        lignes = [ligne for ligne in evaluation.mesurer() if ligne['methodologie'] == 'Coût']
        self.assertEqual(mesure(lignes, 'Temps local par page (s) · m6.proposer')['valeur'], 5.0)
        self.assertEqual(mesure(lignes, 'Tokens LLM en entrée par page · m6.proposer')['valeur'], 100.0)
        self.assertEqual(mesure(lignes, 'Unités Ibou par page · m6.proposer')['valeur'], 3.0)
        self.assertIsNone(lignes[0]['ok'])

    def test_mesures_humaines_a_saisir(self):
        lignes = evaluation.mesurer()
        note = mesure(lignes, 'Note humaine')
        accord = mesure(lignes, 'Accord avec une vérification')
        self.assertTrue(note['a_saisir'] and accord['a_saisir'])
        self.assertIsNone(note['valeur'])
        for valeur in (5, 4, 4):
            evaluation.saisir('m4.note_humaine', valeur)
        for ok in (True, True, True, True, True, True, True, True, True, False):
            evaluation.saisir('m5.accord', ok, 'confirmee')
        lignes = evaluation.mesurer()
        note, accord = mesure(lignes, 'Note humaine'), mesure(lignes, 'Accord avec une vérification')
        self.assertEqual((note['valeur'], note['ok'], note['a_saisir']), (4.3333, True, False))
        self.assertEqual((accord['valeur'], accord['ok']), (0.9, True))
        with self.assertRaises(ValueError):
            evaluation.saisir('m4.note_humaine', 9)
        with self.assertRaises(ValueError):
            evaluation.saisir('inconnue', 1)

    def test_sans_reference(self):
        lignes = evaluation.mesurer()
        self.assertTrue(any('Aucun jeu de référence' in ligne['mesure'] for ligne in lignes))


class ExcelTests(Base):
    def preparer(self):
        self.jeu_complet()
        from chaine import m6
        m6.assurer_schema()
        s.query("INSERT INTO sources(id,unit_id,kind,url,domain,source_date) VALUES('s1','ex-u1','verification','https://x.test/a','x.test','2026-10-01')")
        s.query('INSERT INTO proposals(id,unit_id,version_id,kind,"before","after",status,decision,decision_note,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('pr1', 'ex-u1', 'v-ex-1', 'perimee_par_date', 'avant\x07', 'après', 'appliquee', 'accepter', 'ok', '2026-11-13'))
        s.query("UPDATE units SET verdict='corrigee' WHERE id='ex-u2'")

    def test_xlsx_valide_zip_et_xml(self):
        self.preparer()
        octets = export.excel()
        with zipfile.ZipFile(io.BytesIO(octets)) as archive:
            self.assertIsNone(archive.testzip())
            noms = archive.namelist()
            for nom in noms:
                ElementTree.fromstring(archive.read(nom))
        self.assertIn('xl/worksheets/sheet3.xml', noms)
        self.assertNotIn('xl/worksheets/sheet4.xml', noms)

    def test_lecture_avec_openpyxl(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest('openpyxl absent : validation zip/XML seulement')
        self.preparer()
        classeur = openpyxl.load_workbook(io.BytesIO(export.excel()))
        self.assertEqual(classeur.sheetnames[0], 'Synthese')
        self.assertEqual(len(classeur.sheetnames), 3)
        synthese = list(classeur['Synthese'].iter_rows(values_only=True))
        self.assertEqual(synthese[0][:5], ('URL', 'Titre', 'Unités', "Taux d'obsolescence", 'Propositions'))
        lignes = {ligne[0]: ligne for ligne in synthese[1:]}
        depeche = lignes['https://exemple.test/depeche']
        self.assertEqual(depeche[2], 2)
        self.assertEqual(depeche[3], 0.5)
        self.assertEqual(depeche[4], 1)
        en_tete = synthese[0]
        self.assertEqual(depeche[en_tete.index('appliquee')], 1)
        feuille = classeur[classeur.sheetnames[1]]
        lignes_page = list(feuille.iter_rows(values_only=True))
        self.assertEqual(lignes_page[0][0], 'Unité')
        premiere = lignes_page[1]
        self.assertEqual(premiere[0], 'ex-u1')
        self.assertEqual(premiere[12:14], ('perimee_par_date', 'appliquee'))
        self.assertEqual(premiere[14], 'avant')
        self.assertIn('https://x.test/a', premiere[11])
        self.assertEqual(premiere[17], 'accepter')

    def test_noms_d_onglets_uniques_et_valides(self):
        noms = set()
        for position, url in enumerate(['https://a.test/x/[a]:b?c', 'https://a.test/y/[a]:b?c', 'https://a.test/' + 'z' * 80, 'https://a.test/' + 'z' * 80 + '2']):
            nom = export._nom_feuille(url, position + 1, noms)
            self.assertLessEqual(len(nom), 31)
            self.assertFalse(set('[]:*?/\\') & set(nom))
        self.assertEqual(len(noms), 4)

    def test_taux_d_obsolescence(self):
        unites = [{'verdict': 'corrigee'}, {'temporal_status': 'perimee_par_date'}, {}, {'status': 'historique', 'verdict': 'corrigee'}, {'excluded': 1}]
        self.assertAlmostEqual(export.taux_obsolescence(unites), 2 / 3)
        self.assertIsNone(export.taux_obsolescence([]))

    def test_base_vide(self):
        octets = export.excel()
        with zipfile.ZipFile(io.BytesIO(octets)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn('xl/worksheets/sheet1.xml', archive.namelist())


if __name__ == '__main__':
    unittest.main()
