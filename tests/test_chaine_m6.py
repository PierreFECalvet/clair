"""Tests du lot M6 : perimetre, substitution, reecriture, controle, cartes, decisions, apprentissage."""
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s
import chaine
from chaine import m6
from chaine import moteurs
from chaine import noyau
from chaine import schema
from chaine import seuils

AUJOURDHUI = '2026-11-13'


def jetons(texte):
    return set(re.findall(r'\w+', texte.lower()))


def faux_nli(paires):
    """NLI simule : implication si tous les mots de l'hypothese figurent dans la premisse."""
    resultat = []
    for premisse, hypothese in paires:
        ok = jetons(hypothese) <= jetons(premisse)
        label = 'implication' if ok else 'neutre'
        resultat.append({'label': label, 'score': 0.95, 'probas': {'implication': 0.95 if ok else 0.05, 'neutre': 0.05 if ok else 0.95, 'contradiction': 0.0}, 'engine': 'faux'})
    return resultat


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
        s.CTX.step = None
        self.nli = patch.object(moteurs, 'nli_lot', side_effect=faux_nli)
        self.nli.start()
        # Configuration simulee declaree : un NLI local distinct du LLM (double passe forte, relecture M1).
        self.nli_prevu = patch.object(moteurs, 'nli_prevu', return_value='test:nli')
        self.nli_prevu.start()
        self.compteur = 0

    def tearDown(self):
        self.nli.stop()
        self.nli_prevu.stop()
        s.CONFIG.clear()
        s.CONFIG.update(self.config)
        noyau.vider_reglages()
        seuils._vider_cache()
        self.tmp.cleanup()

    def page(self, url, texte, page_type=None):
        vid = s.add_content(url, 'Titre ' + url, texte, '2026-01-01')
        if page_type:
            s.query('UPDATE versions SET page_type=? WHERE id=?', (page_type, vid))
        return vid

    def unite(self, vid, phrase, **champs):
        self.compteur += 1
        texte = s.query('SELECT text FROM versions WHERE id=?', (vid,), True)['text']
        debut = texte.index(phrase)
        valeurs = dict(id=f'u{self.compteur}', version_id=vid, text=phrase, quote=phrase, span_start=debut, span_end=debut + len(phrase),
                       anchor_date='2024-03-01', anchor_kind='phrase', status='a_verifier', verdict=None, temporal_status='a_jour',
                       critical=0, critical_score=None, excluded=0, created=s.now())
        valeurs.update(champs)
        s.query('INSERT INTO units(' + ','.join(valeurs) + ') VALUES(' + ','.join('?' for _ in valeurs) + ')', tuple(valeurs.values()))
        return valeurs['id']

    def fait(self, unit_id, ident, objet, **champs):
        valeurs = dict(id=ident, unit_id=unit_id, subject='Acme', predicate='pdg', object=objet, object_norm=objet, object_type='nom',
                       origin='explicite', status='actif', created=s.now())
        valeurs.update(champs)
        s.query('INSERT INTO facts(' + ','.join(valeurs) + ') VALUES(' + ','.join('?' for _ in valeurs) + ')', tuple(valeurs.values()))

    def pdg(self, url='https://a.test/pdg', page_type=None, texte=None):
        """Page avec une unite corrigee : le PDG passe d'Alice Martin a Bob Durand."""
        phrase = 'Le PDG de Acme est Alice Martin depuis 2024.'
        vid = self.page(url, texte or (phrase + '\n\nUn autre paragraphe sans rapport.'), page_type)
        uid = self.unite(vid, phrase, verdict='corrigee')
        position = phrase.index('Alice Martin')
        base = s.query('SELECT span_start FROM units WHERE id=?', (uid,), True)['span_start']
        self.fait(uid, uid + 'f1', 'Alice Martin', span_start=base + position, span_end=base + position + 12, verbalization='Alice Martin Acme PDG',
                  replaced_by=uid + 'f2')
        self.fait(uid, uid + 'f2', 'Bob Durand', origin='externe', verbalization='Bob Durand Acme PDG')
        depuis = phrase.index('2024')
        self.fait(uid, uid + 'f3', '2024', predicate='depuis', object_type='date', span_start=base + depuis, span_end=base + depuis + 4,
                  verbalization='Acme 2024')
        return vid, uid

    def llm(self, reponses):
        suite = iter(reponses)
        return patch.object(s, 'llm', side_effect=lambda tache, payload: {'phrase': next(suite)})


class FormatsTests(unittest.TestCase):
    def test_date_au_format_de_l_ancienne(self):
        self.assertEqual(m6.formater_date('2026-11-13', '1er août 2026'), '13 novembre 2026')
        self.assertEqual(m6.formater_date('2026-08-01', '13 novembre 2026'), '1er août 2026')
        self.assertEqual(m6.formater_date('2026-11', 'août 2026'), 'novembre 2026')
        self.assertEqual(m6.formater_date('2027', 'en 2026'), 'en 2027')
        self.assertEqual(m6.formater_date('2026-11-13', '01/08/2026'), '13/11/2026')

    def test_nombre_separateurs_francais(self):
        self.assertEqual(m6.formater_nombre('12500', '9000').replace(' ', ' '), '12 500')
        self.assertEqual(m6.formater_nombre('12.5', '8,2 %'), '12,5 %')
        self.assertEqual(m6.formater_nombre('250', '1 200 salariés'), '250 salariés')
        self.assertEqual(m6.formater_nombre('1250000', '1 200 000').replace(' ', ' '), '1 250 000')

    def test_substitution_date_et_chiffre(self):
        phrase = 'X compte 1 salarié depuis le 1er août 2026.'
        a, b = phrase.index('1 sal'), phrase.index('1er août 2026')
        resultat = m6.substituer(phrase, [{'debut': a, 'fin': a + 1, 'type': 'nombre', 'norm': '250', 'brut': '250'},
                                          {'debut': b, 'fin': b + 13, 'type': 'date', 'norm': '2026-11-13', 'brut': '13 novembre 2026'}])
        self.assertEqual(resultat, 'X compte 250 salariés depuis le 13 novembre 2026.')

    def test_accords_simples(self):
        def sub(phrase, ancien, nouveau):
            a = phrase.index(ancien)
            return m6.substituer(phrase, [{'debut': a, 'fin': a + len(ancien), 'type': 'nom', 'brut': nouveau}])
        self.assertEqual(sub('Le chef de Paris est ici.', 'Paris', 'le Sénat'), 'Le chef du Sénat est ici.')
        self.assertEqual(sub('Il vient de Lyon.', 'Lyon', 'Orléans'), "Il vient d'Orléans.")
        self.assertEqual(sub("Il vient d'Orléans.", 'Orléans', 'Lyon'), 'Il vient de Lyon.')
        self.assertEqual(sub('Il va à Paris.', 'Paris', 'les Antilles'), 'Il va aux Antilles.')

    def test_substitution_refuse_un_span_incoherent(self):
        self.assertIsNone(m6.substituer('abc', [{'debut': 2, 'fin': 9, 'type': 'nom', 'brut': 'x'}]))

    def test_diff_au_mot_pres_accents_et_ponctuation(self):
        avant, apres = "L'été est chaud, très chaud.", "L'Été est chaud ; très chaud."
        segments = m6.diff_mots(avant, apres)
        ops = [(x['op'], x['texte'].strip()) for x in segments if x['op'] != 'egal']
        self.assertIn(('supprime', 'été'), ops)
        self.assertIn(('ajoute', 'Été'), ops)
        self.assertIn(('supprime', ','), ops)
        self.assertIn(('ajoute', ';'), ops)
        self.assertEqual(''.join(x.get('avant', x['texte']) for x in segments if x['op'] in ('egal', 'supprime')), avant)
        self.assertEqual(''.join(x['texte'] for x in segments if x['op'] in ('egal', 'ajoute')), apres)

    def test_diff_identique_et_vide(self):
        self.assertEqual([x['op'] for x in m6.diff_mots('Un texte.', 'Un texte.')], ['egal'])
        self.assertEqual(m6.diff_mots('', ''), [])
        self.assertEqual(m6.ratio_difference('un deux trois quatre', 'un deux trois cinq'), 0.25)


class PerimetreTests(Base):
    def test_exclusions_historique_et_opinion(self):
        vid = self.page('https://a.test/p', 'Phrase un. Phrase deux. Phrase trois. Phrase quatre.')
        u1 = self.unite(vid, 'Phrase un.', verdict='corrigee')
        u2 = self.unite(vid, 'Phrase deux.', verdict='corrigee', status='historique')
        u3 = self.unite(vid, 'Phrase trois.', verdict='corrigee', sourcing='opinion')
        u4 = self.unite(vid, 'Phrase quatre.', temporal_status='perimee_par_date')
        s.query("INSERT INTO claim_types(unit_id,type) VALUES(?,?)", (u4, 'opinion'))
        candidats, exclus = m6.perimetre(AUJOURDHUI, avec_exclusions=True)
        self.assertEqual([c['unit_id'] for c in candidats], [u1])
        self.assertEqual({e['unit_id']: e['raison'] for e in exclus}, {u2: 'historique', u3: 'opinion', u4: 'opinion'})

    def test_types_de_proposition(self):
        vid = self.page('https://a.test/p', 'Une. Deux. Trois. Quatre. Cinq.')
        self.unite(vid, 'Une.', verdict='corrigee')
        self.unite(vid, 'Deux.', temporal_status='perimee_par_date')
        self.unite(vid, 'Trois.', verdict='completee', critical=1)
        self.unite(vid, 'Quatre.', verdict='completee', critical=0)
        c5 = self.unite(vid, 'Cinq.', verdict='confirmee', source_manquante=1)
        s.query("INSERT INTO sources(id,unit_id,kind,url,domain) VALUES('s1',?,?,?,?)", (c5, 'verification', 'https://insee.fr/x', 'insee.fr'))
        types = sorted(c['kind'] for c in m6.perimetre(AUJOURDHUI))
        self.assertEqual(types, ['completee', 'corrigee', 'perimee_par_date', 'source_ajoutee'])

    def test_depeche_donne_une_mention_pas_une_reecriture(self):
        vid, uid = self.pdg('https://a.test/depeche', page_type='actualité')
        candidat = m6.perimetre(AUJOURDHUI)[0]
        self.assertEqual((candidat['kind'], candidat['mode'], candidat['motif_origine']), ('mention_maj', 'mention', 'corrigee'))

    def test_seule_la_derniere_version_est_examinee(self):
        vid, uid = self.pdg()
        s.add_content('https://a.test/pdg', 'T', 'Nouveau texte entierement different.', '2026-02-01')
        self.assertEqual(m6.perimetre(AUJOURDHUI), [])

    def test_lien_interne_depuis_link_recommendations(self):
        vid = self.page('https://a.test/p', 'Voir la notion de hostAge pour le détail.')
        cible = self.page('https://a.test/hostage', 'hostAge expliqué.')
        uid = self.unite(vid, 'Voir la notion de hostAge pour le détail.')
        s.query('INSERT INTO link_recommendations(id,unit_id,from_version,to_version,anchor,reason,status,created) VALUES(?,?,?,?,?,?,?,?)', ('l1', uid, vid, cible, 'hostAge', 'page parente', 'proposee', s.now()))
        candidat = m6.perimetre(AUJOURDHUI)[0]
        self.assertEqual((candidat['kind'], candidat['ref_id']), ('lien_interne', 'l1'))
        proposition = m6.proposer(uid, AUJOURDHUI)
        self.assertEqual(proposition['after'], 'Voir la notion de [hostAge](https://a.test/hostage) pour le détail.')
        self.assertEqual(proposition['status'], 'proposee')


class ProposerTests(Base):
    def test_substitution_sans_llm(self):
        vid, uid = self.pdg()
        with patch.object(s, 'llm', side_effect=AssertionError('LLM interdit')):
            proposition = m6.proposer(uid, AUJOURDHUI)
        self.assertEqual(proposition['after'], 'Le PDG de Acme est Bob Durand depuis 2024.')
        self.assertEqual((proposition['method'], proposition['status']), ('substitution', 'proposee'))
        self.assertEqual(len(proposition['facts_changed']), 1)
        self.assertEqual(len(proposition['facts_kept']), 1)
        self.assertTrue(proposition['checks']['ok'])

    def test_substitution_de_date_et_de_chiffre_en_base(self):
        phrase = 'Acme compte 200 salariés depuis le 1er août 2023.'
        vid = self.page('https://a.test/n', phrase)
        uid = self.unite(vid, phrase, verdict='corrigee')
        base = s.query('SELECT span_start FROM units WHERE id=?', (uid,), True)['span_start']
        a, b = phrase.index('200'), phrase.index('1er août 2023')
        self.fait(uid, 'fa', '200', object_type='nombre', span_start=base + a, span_end=base + a + 3, verbalization='Acme 200 salariés', replaced_by='fa2')
        self.fait(uid, 'fa2', '250', object_type='nombre', origin='externe', verbalization='Acme 250 salariés')
        self.fait(uid, 'fb', '1er août 2023', object_type='date', span_start=base + b, span_end=base + b + 13, verbalization='Acme depuis 1er août 2023',
                  replaced_by='fb2')
        self.fait(uid, 'fb2', '13 novembre 2026', object_type='date', object_norm='2026-11-13', origin='externe', verbalization='Acme depuis 13 novembre 2026')
        proposition = m6.proposer(uid, AUJOURDHUI)
        self.assertEqual(proposition['after'], 'Acme compte 250 salariés depuis le 13 novembre 2026.')
        self.assertEqual(proposition['method'], 'substitution')

    def test_reecriture_simulee_acceptee(self):
        phrase = 'Le périphérique sera immobilisé du 1er au 31 août 2026.'
        vid = self.page('https://a.test/p', phrase)
        uid = self.unite(vid, phrase, temporal_status='perimee_par_date')
        self.fait(uid, 'p1', 'du 1er au 31 août 2026', subject='périphérique', predicate='immobilise', verbalization='périphérique immobilisé du 1er au 31 août 2026')
        with self.llm(['Le périphérique a été immobilisé du 1er au 31 août 2026.']):
            proposition = m6.proposer(uid, AUJOURDHUI)
        self.assertEqual(proposition['status'], 'proposee')
        self.assertEqual(proposition['method'], 'reecriture_llm')
        self.assertEqual(proposition['kind'], 'perimee_par_date')

    def test_reecriture_refusee_par_nli_perte_d_information(self):
        phrase = 'Le périphérique sera immobilisé du 1er au 31 août 2026.'
        vid = self.page('https://a.test/p', phrase)
        uid = self.unite(vid, phrase, temporal_status='perimee_par_date')
        self.fait(uid, 'p1', 'du 1er au 31 août 2026', verbalization='périphérique immobilisé du 1er au 31 août 2026')
        with self.llm(['Le périphérique a été immobilisé.', 'Le périphérique a été immobilisé cet été.']):
            proposition = m6.proposer(uid, AUJOURDHUI)
        self.assertEqual(proposition['status'], 'a_rediger')
        self.assertEqual(proposition['attempts'], 2)
        codes = [p['code'] for t in proposition['checks']['tentatives'] for p in t['problemes']]
        self.assertIn('information_perdue', codes)

    def test_seconde_tentative_rattrape_la_premiere(self):
        phrase = 'Le périphérique sera immobilisé du 1er au 31 août 2026.'
        vid = self.page('https://a.test/p', phrase)
        uid = self.unite(vid, phrase, temporal_status='perimee_par_date')
        self.fait(uid, 'p1', 'du 1er au 31 août 2026', verbalization='périphérique immobilisé du 1er au 31 août 2026')
        with self.llm(['Le périphérique a été immobilisé.', 'Le périphérique a été immobilisé du 1er au 31 août 2026.']):
            proposition = m6.proposer(uid, AUJOURDHUI)
        self.assertEqual((proposition['status'], proposition['attempts']), ('proposee', 2))

    def test_budget_de_difference(self):
        vid, uid = self.pdg()
        seuils.definir('m6.diff_ratio_max', 0.05)
        with self.llm(['Le PDG de Acme est Bob Durand depuis 2024.']):
            proposition = m6.proposer(uid, AUJOURDHUI)
        self.assertEqual(proposition['status'], 'a_rediger')
        codes = [p['code'] for t in proposition['checks']['tentatives'] for p in t['problemes']]
        self.assertIn('budget_difference', codes)

    def test_mention_de_mise_a_jour_pour_une_depeche(self):
        vid, uid = self.pdg('https://a.test/depeche', page_type='depeche')
        proposition = m6.proposer(uid, AUJOURDHUI)
        self.assertEqual(proposition['kind'], 'mention_maj')
        self.assertEqual(proposition['after'], 'Mise à jour du 13 novembre 2026 : Le PDG de Acme est Bob Durand depuis 2024.')
        self.assertEqual(proposition['status'], 'proposee')
        self.assertEqual(proposition['before'], 'Le PDG de Acme est Alice Martin depuis 2024.')

    def test_sans_faits_connus_a_rediger(self):
        vid = self.page('https://a.test/p', 'Une phrase sans fait.')
        uid = self.unite(vid, 'Une phrase sans fait.', verdict='corrigee')
        with patch.object(s, 'llm', side_effect=AssertionError('LLM interdit')):
            proposition = m6.proposer(uid, AUJOURDHUI)
        self.assertEqual(proposition['status'], 'a_rediger')

    def test_source_ajoutee_laisse_la_phrase_inchangee(self):
        vid = self.page('https://a.test/p', 'Le taux est de 12 %.')
        uid = self.unite(vid, 'Le taux est de 12 %.', verdict='confirmee', source_manquante=1)
        s.query("INSERT INTO sources(id,unit_id,kind,url,domain,source_date) VALUES('s1',?,?,?,?,?)", (uid, 'verification', 'https://www.insee.fr/x', 'insee.fr', '2026-05-01'))
        proposition = m6.proposer(uid, AUJOURDHUI)
        self.assertEqual(proposition['after'], 'Le taux est de 12 %. ([insee.fr](https://www.insee.fr/x))')
        self.assertEqual(proposition['status'], 'proposee')

    def test_controler_detecte_un_fait_nouveau_non_implique(self):
        resultat = m6.controler({'kind': 'corrigee', 'before': 'Le PDG est Alice.', 'after': 'Le PDG est Carole.',
                                 'facts_changed': [{'verbalisation': 'Bob PDG', 'nouveau_fait_id': 'x'}], 'facts_kept': []})
        self.assertFalse(resultat['ok'])
        self.assertEqual(resultat['problemes'][0]['code'], 'fait_nouveau_non_implique')

    def test_executer_reprenable_et_compteurs(self):
        vid, uid = self.pdg()
        premiere = m6.executer(aujourdhui=AUJOURDHUI)
        self.assertEqual((premiere['candidats'], premiere['proposees'], premiere['substitutions']), (1, 1, 1))
        seconde = m6.executer(aujourdhui=AUJOURDHUI)
        self.assertEqual((seconde['deja'], seconde['proposees']), (1, 0))
        self.assertEqual(s.query('SELECT count(*) n FROM proposals', one=True)['n'], 1)
        forcee = m6.executer(forcer=True, aujourdhui=AUJOURDHUI)
        self.assertEqual(forcee['proposees'], 1)
        self.assertEqual(s.query('SELECT count(*) n FROM proposals', one=True)['n'], 1)
        run = s.query("SELECT * FROM step_runs WHERE step='m6.proposer' ORDER BY started DESC", one=True)
        self.assertEqual(run['status'], 'terminee')
        self.assertIsNotNone(s.query('SELECT run_id FROM proposals', one=True)['run_id'])


class RevueTests(Base):
    def setUp(self):
        super().setUp()
        self.vid, self.uid = self.pdg()
        self.prop = m6.proposer(self.uid, AUJOURDHUI)

    def texte_courant(self, url='https://a.test/pdg'):
        return s.query('SELECT v.text FROM versions v JOIN pages p ON p.id=v.page_id WHERE p.url=? ORDER BY v.created DESC, v.rowid DESC', (url,), True)['text']

    def test_carte_contient_tout_le_necessaire(self):
        s.query("INSERT INTO sources(id,unit_id,kind,url,domain,source_date,passage,contribution) VALUES('s1',?,?,?,?,?,?,?)",
                (self.uid, 'verification', 'https://acme.test/pr', 'acme.test', '2026-10-01', 'Bob Durand est nommé PDG.', 'soutient'))
        carte = m6.carte(self.prop['id'])
        contexte = carte['contexte']
        self.assertIn('Un autre paragraphe', s.query('SELECT text FROM versions WHERE id=?', (self.vid,), True)['text'])
        self.assertEqual(contexte['paragraphe'][contexte['phrase_debut']:contexte['phrase_fin']], 'Le PDG de Acme est Alice Martin depuis 2024.')
        self.assertNotIn('Un autre paragraphe', contexte['paragraphe'])
        self.assertEqual(carte['motif']['code'], 'corrigee')
        self.assertEqual(carte['motif']['ancrage']['date'], '2024-03-01')
        self.assertEqual(carte['motif']['ancrage']['date_fr'], '1er mars 2024')
        self.assertEqual({x['op'] for x in carte['avant_apres']['diff']}, {'egal', 'supprime', 'ajoute'})
        self.assertEqual(carte['page']['url'], 'https://a.test/pdg')
        self.assertTrue(carte['controle']['ok'])

    def test_cartes_triees_par_criticite_puis_page(self):
        autre = self.pdg('https://a.test/aaa')[1]
        tiers = self.pdg('https://a.test/zzz')[1]
        s.query('UPDATE units SET critical_score=0.9 WHERE id=?', (tiers,))
        s.query('UPDATE units SET critical_score=0.5 WHERE id=?', (autre,))
        s.query('UPDATE units SET critical_score=0.5 WHERE id=?', (self.uid,))
        m6.executer(aujourdhui=AUJOURDHUI)
        urls = [c['page']['url'] for c in m6.cartes()['cartes']]
        self.assertEqual(urls, ['https://a.test/zzz', 'https://a.test/aaa', 'https://a.test/pdg'])
        self.assertEqual(m6.cartes('proposee')['total'], 3)
        self.assertEqual(m6.cartes('rejetee')['total'], 0)

    def test_accepter_cree_une_nouvelle_version_locale(self):
        resultat = m6.decider(self.prop['id'], 'accepter')
        self.assertEqual(resultat['status'], 'appliquee')
        self.assertIn('Bob Durand', self.texte_courant())
        self.assertEqual(s.query('SELECT count(*) n FROM versions', one=True)['n'], 2)
        self.assertIn('Alice Martin', s.query('SELECT text FROM versions WHERE id=?', (self.vid,), True)['text'])
        ligne = s.query('SELECT * FROM proposals WHERE id=?', (self.prop['id'],), True)
        self.assertEqual((ligne['decision'], ligne['applied_version']), ('accepter', resultat['version_id']))
        with self.assertRaises(ValueError):
            m6.decider(self.prop['id'], 'rejeter')

    def test_modifier_recontrole_le_texte(self):
        refus = m6.decider(self.prop['id'], 'modifier', texte='Le PDG de Acme est Carole Petit depuis 2024.')
        self.assertFalse(refus['ok'])
        self.assertEqual(refus['status'], 'proposee')
        self.assertIn('Alice Martin', self.texte_courant())
        accepte = m6.decider(self.prop['id'], 'modifier', texte='Le PDG de Acme est désormais Bob Durand depuis 2024.', note='reformulé')
        self.assertEqual(accepte['status'], 'modifiee')
        self.assertIn('est désormais Bob Durand', self.texte_courant())
        ligne = s.query('SELECT * FROM proposals WHERE id=?', (self.prop['id'],), True)
        self.assertEqual((ligne['method'], ligne['decision']), ('humain', 'modifier'))

    def test_modifier_force(self):
        sortie = m6.decider(self.prop['id'], 'modifier', texte='Le PDG de Acme est Carole Petit depuis 2024.', forcer=True)
        self.assertEqual(sortie['status'], 'modifiee')

    def test_rejeter_archiver_reporter(self):
        autre = self.pdg('https://a.test/b')[1]
        troisieme = self.pdg('https://a.test/c')[1]
        p2 = m6.proposer(autre, AUJOURDHUI)
        p3 = m6.proposer(troisieme, AUJOURDHUI)
        self.assertEqual(m6.decider(self.prop['id'], 'rejeter', note='non')['status'], 'rejetee')
        self.assertEqual(m6.decider(p2['id'], 'reporter')['status'], 'reportee')
        archive = m6.decider(p3['id'], 'archive')
        self.assertEqual(archive['status'], 'archive')
        self.assertEqual(s.query('SELECT status FROM units WHERE id=?', (troisieme,), True)['status'], 'historique')
        self.assertEqual(s.query('SELECT decision_note FROM proposals WHERE id=?', (p3['id'],), True)['decision_note'], 'à date du 1er mars 2024')
        self.assertEqual(m6.decider(p2['id'], 'accepter')['status'], 'appliquee')

    def test_une_phrase_a_rediger_ne_s_accepte_pas(self):
        vid = self.page('https://a.test/x', 'Phrase vide de faits.')
        uid = self.unite(vid, 'Phrase vide de faits.', verdict='corrigee')
        p = m6.proposer(uid, AUJOURDHUI)
        with self.assertRaises(ValueError):
            m6.decider(p['id'], 'accepter')
        self.assertEqual(m6.decider(p['id'], 'modifier', texte='Phrase réécrite à la main.', forcer=True)['status'], 'modifiee')

    def test_propagation_aux_enfants(self):
        enfant1 = self.pdg('https://a.test/e1')[1]
        enfant2 = self.pdg('https://a.test/e2')[1]
        m6.executer(aujourdhui=AUJOURDHUI)
        s.query("INSERT INTO unit_groups(id,frame) VALUES('g1','{}')")
        for u in (self.uid, enfant1, enfant2):
            s.query("INSERT INTO unit_group_members VALUES('g1',?)", (u,))
        s.query("INSERT INTO paternity(group_id,parent_unit_id,criterion) VALUES('g1',?,'date_publication')", (self.uid,))
        carte = m6.carte(self.prop['id'])
        self.assertTrue(carte['paternite']['est_parente'])
        self.assertEqual(len(carte['paternite']['enfants']), 2)
        resultat = m6.decider(self.prop['id'], 'accepter', propager=True)
        self.assertEqual(len(resultat['propagees']), 2)
        for url in ('https://a.test/e1', 'https://a.test/e2'):
            self.assertIn('Bob Durand', self.texte_courant(url))
        self.assertEqual(s.query("SELECT count(*) n FROM proposals WHERE status='appliquee'", one=True)['n'], 3)

    def test_propagation_rejet_seulement_aux_enfants_du_parent(self):
        enfant = self.pdg('https://a.test/e1')[1]
        m6.proposer(enfant, AUJOURDHUI)
        s.query("INSERT INTO unit_groups(id,frame) VALUES('g1','{}')")
        for u in (self.uid, enfant):
            s.query("INSERT INTO unit_group_members VALUES('g1',?)", (u,))
        s.query("INSERT INTO paternity(group_id,parent_unit_id,criterion) VALUES('g1',?,'date_publication')", (enfant,))
        resultat = m6.decider(self.prop['id'], 'rejeter', propager=True)
        self.assertEqual(resultat['propagees'], [])

    def test_mention_appliquee_apres_le_paragraphe(self):
        vid, uid = self.pdg('https://a.test/depeche', page_type='depeche')
        p = m6.proposer(uid, AUJOURDHUI)
        m6.decider(p['id'], 'accepter')
        texte = self.texte_courant('https://a.test/depeche')
        self.assertIn('Alice Martin depuis 2024.\n\nMise à jour du 13 novembre 2026 : Le PDG de Acme est Bob Durand depuis 2024.\n\nUn autre', texte)
        nouvelle = s.query('SELECT page_type FROM versions v JOIN pages p ON p.id=v.page_id WHERE p.url=? ORDER BY v.created DESC, v.rowid DESC',
                           ('https://a.test/depeche',), True)
        self.assertEqual(nouvelle['page_type'], 'depeche')

    def test_apprentissage(self):
        autres = [self.pdg(f'https://a.test/x{i}')[1] for i in range(3)]
        propositions = [m6.proposer(u, AUJOURDHUI) for u in autres]
        m6.decider(self.prop['id'], 'accepter')
        m6.decider(propositions[0]['id'], 'accepter')
        m6.decider(propositions[1]['id'], 'modifier', texte='Le PDG de Acme est désormais Bob Durand depuis 2024.')
        m6.decider(propositions[2]['id'], 'rejeter')
        stats = m6.apprentissage()
        self.assertEqual(stats['total_decisions'], 4)
        self.assertEqual((stats['acceptees'], stats['modifiees'], stats['rejetees']), (2, 1, 1))
        self.assertEqual(stats['taux_sans_modification'], 0.5)
        self.assertEqual(stats['par_motif']['corrigee']['total'], 4)
        self.assertEqual(stats['par_methode']['substitution']['acceptees'], 2)
        self.assertEqual(stats['diff_ratio']['acceptees']['n'], 2)
        self.assertEqual(stats['diff_ratio']['rejetees']['n'], 1)


if __name__ == '__main__':
    unittest.main()
