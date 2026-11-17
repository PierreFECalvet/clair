"""Tests du lot m1_unites : predicats, unites (M1 etape 4), faits atomiques (M1 etape 5)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s
import chaine
from chaine import m1_unites as m
from chaine import moteurs
from chaine import noyau
from chaine import predicats
from chaine import schema
from chaine import seuils

PERIPH = ('Travaux d’été sur le périphérique\n\n'
          'La voie gauche du périphérique parisien sera immobilisée du 1er au 31 août 2026. '
          'Les automobilistes sont invités à anticiper leurs trajets.\n')
UNITE_PERIPH = 'La voie gauche du périphérique parisien sera immobilisée du 1er au 31 août 2026.'
REFUS = {'appartient', 'gere', 'proprietaire', 'invente'}


def spacy_disponible():
    """Vrai si spaCy et un modele francais sont installes."""
    return moteurs.paquet_present('spacy') and bool(moteurs._modeles_spacy_installes())


def nli_simule(paires):
    """NLI double : implication sauf mot refuse, negation ajoutee ou nombre absent de la premisse."""
    resultats = []
    for premisse, hypothese in paires:
        mots = set(m._RE_MOT.findall(m.cle(hypothese)))
        negation = 'pas' in mots and 'pas' not in set(m._RE_MOT.findall(m.cle(premisse)))
        refuse = bool(mots & REFUS) or negation or not m._nombres(hypothese) <= m._nombres(premisse)
        label = 'neutre' if refuse else 'implication'
        probas = {'implication': 0.05 if refuse else 0.95, 'contradiction': 0.05, 'neutre': 0.9 if refuse else 0.0}
        resultats.append({'label': label, 'score': max(probas.values()), 'probas': probas, 'engine': 'nli:simule'})
    return resultats


def fait(sujet, predicat, objet, origine='explicite', citation='', periode='', portee=''):
    """Fait tel que le renvoie le LLM contraint."""
    return {'sujet': sujet, 'predicat': predicat, 'objet': objet, 'portee': portee, 'periode': periode,
            'origine': origine, 'citation': citation}


def llm_faits_periph(task, payload, provider=None, model=None):
    """LLM simule : l'exemple du peripherique (faits explicites, F3/F4 externes, inconnues, erreurs)."""
    sortie = []
    for unite in payload['unites']:
        if 'voie gauche' not in unite['texte']:
            sortie.append({'unite': unite['id'], 'faits': [], 'inconnues': []})
            continue
        sortie.append({'unite': unite['id'], 'inconnues': [
            {'sujet': 'périphérique parisien', 'predicat': 'sera_ferme', 'question': 'Quelles voies et quelles dates exactes ?'}],
            'faits': [
                fait('La voie gauche', 'fait_partie_de', 'périphérique parisien', citation='La voie gauche du périphérique parisien'),
                fait('La voie gauche du périphérique parisien', 'sera_ferme', 'du 1er au 31 août 2026',
                     citation='sera immobilisée du 1er au 31 août 2026'),
                fait('La voie gauche du périphérique parisien', 'date_de_debut', '1er août 2026', origine='inference_contextuelle',
                     citation='du 1er au 31 août 2026'),
                fait('La voie gauche du périphérique parisien', 'date_de_fin', '31 août 2026', origine='inference_contextuelle',
                     citation='du 1er au 31 août 2026'),
                # F3 et F4 du document : connaissance externe -> hypotheses
                fait('périphérique parisien', 'est_gere_par', 'Ville de Paris', origine='connaissance_externe'),
                fait('périphérique parisien', 'a_pour_proprietaire', 'Ville de Paris', origine='connaissance_externe'),
                # Meme connaissance externe presentee a tort comme explicite : entite absente du span
                fait('périphérique parisien', 'est_gere_par', 'Ville de Paris', citation='périphérique parisien'),
                # Entites presentes mais non impliquees : rejet NLI
                fait('La voie gauche', 'a_pour_proprietaire', 'périphérique parisien', citation='La voie gauche du périphérique parisien'),
            ]})
    return {'unites': sortie}


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
        # Configuration simulee declaree : un NLI local distinct du LLM (double passe forte, relecture M1).
        self.nli_prevu = patch.object(moteurs, 'nli_prevu', return_value='test:nli')
        self.nli_prevu.start()

    def tearDown(self):
        self.nli_prevu.stop()
        s.CONFIG.clear()
        s.CONFIG.update(self.config)
        s.STOP.clear()
        moteurs.reinitialiser()
        noyau.vider_reglages()
        seuils._vider_cache()
        self.tmp.cleanup()

    def version(self, texte=PERIPH, titre='Périphérique', publie='2026-06-15'):
        return s.add_content('https://exemple.test/' + s.uid(), titre, texte, publie)

    def unite_migree(self, version_id, texte, citation):
        version = s.query('SELECT text FROM versions WHERE id=?', (version_id,), True)['text']
        debut = version.find(citation)
        ident = s.uid()
        s.query('INSERT INTO units(id, version_id, text, quote, span_start, span_end, extractor, created) VALUES(?,?,?,?,?,?,?,?)',
                (ident, version_id, texte, citation, debut if debut >= 0 else None,
                 debut + len(citation) if debut >= 0 else None, 'migration', s.now()))
        return ident


# ---------------------------------------------------------------------------
# Predicats
# ---------------------------------------------------------------------------

class PredicatsTests(unittest.TestCase):

    def test_liste_fermee_valide(self):
        liste = predicats.tous()
        self.assertTrue(30 <= len(liste) <= 60)
        self.assertEqual(predicats.valider(liste), [])
        for p in liste:
            self.assertEqual(p['nom'], p['nom'].encode('ascii', 'ignore').decode())
            self.assertIn(p['classe'], predicats.CLASSES)
            self.assertIn(p['gabarit'], predicats.GABARITS)
            if p['classe'] == 'immuable':
                self.assertIsNone(p['volatilite_jours'])

    def test_acces(self):
        self.assertEqual(predicats.classe('a_pour_dirigeant'), 'etat_courant')
        self.assertEqual(predicats.classe('date_de_debut'), 'annonce')
        self.assertEqual(predicats.classe('a_pour_inventeur'), 'immuable')
        self.assertTrue(predicats.critique('prix'))
        self.assertTrue(predicats.critique('date_de_fin'))
        self.assertFalse(predicats.critique('utilise'))
        self.assertIsNone(predicats.get('predicat_libre'))
        self.assertIsNone(predicats.classe('predicat_libre'))
        copie = predicats.get('prix')
        copie['classe'] = 'immuable'
        self.assertEqual(predicats.classe('prix'), 'etat_courant')
        self.assertEqual(predicats.verbaliser('fait_partie_de', 'la voie gauche', 'le périphérique parisien'),
                         'La voie gauche fait partie de le périphérique parisien')
        with self.assertRaises(KeyError):
            predicats.verbaliser('inconnu', 'a', 'b')

    def test_valider_signale_les_erreurs(self):
        mauvais = [{'nom': 'Prix Libre', 'libelle': '', 'classe': 'eternel', 'volatilite_jours': -1, 'type_objet': 'x',
                    'gabarit': 'y', 'critique': 'oui', 'verbalisation': '{sujet}'}]
        problemes = predicats.valider(mauvais)
        self.assertTrue(any('classe' in p for p in problemes))
        self.assertTrue(any('verbalisation' in p for p in problemes))
        self.assertTrue(any(p.startswith('avertissement') for p in problemes))

    def test_fichier_modifiable_et_recharge(self):
        with tempfile.TemporaryDirectory() as dossier:
            chemin = Path(dossier) / 'p.json'
            liste = predicats.tous()[:30]
            chemin.write_text(json.dumps({'version': 'essai', 'predicats': liste}), encoding='utf-8')
            with patch.dict(os.environ, {'FC_PREDICATS': str(chemin)}):
                self.assertEqual(predicats.recharger(), 30)
                self.assertEqual(predicats.version(), 'essai')
                liste[0] = dict(liste[0], classe='inconnue')
                chemin.write_text(json.dumps({'predicats': liste}), encoding='utf-8')
                with self.assertRaises(predicats.PredicatsInvalides):
                    predicats.recharger()
        predicats.recharger()
        self.assertGreaterEqual(len(predicats.noms()), 30)


# ---------------------------------------------------------------------------
# Etape 4 : unites
# ---------------------------------------------------------------------------

class UnitesTests(Base):

    def verifier_spans(self, version_id):
        texte = s.query('SELECT text FROM versions WHERE id=?', (version_id,), True)['text']
        lignes = s.query('SELECT * FROM units WHERE version_id=?', (version_id,))
        for u in lignes:
            self.assertEqual(texte[u['span_start']:u['span_end']], u['quote'])
            self.assertTrue(u['run_id'])
        return lignes

    @unittest.skipUnless(spacy_disponible(), 'spaCy francais absent')
    def test_option_a_spacy_coordination_relative(self):
        v = self.version('Bilan.\n\nGoogle, qui a lancé le Knowledge Graph en 2012, utilise des entités et il les relie '
                         'aux requêtes. ' + UNITE_PERIPH + '\n')
        with patch.object(moteurs, 'nli_lot', side_effect=nli_simule):
            r = m.unites(v, option='A')
        textes = {u['text'] for u in self.verifier_spans(v)}
        self.assertIn('Google utilise des entités.', textes)
        self.assertIn('Google a lancé le Knowledge Graph en 2012.', textes)
        self.assertIn('Google les relie aux requêtes.', textes)
        self.assertIn(UNITE_PERIPH, textes)
        self.assertTrue(r['extracteur'].startswith('A:spacy'))
        unite = s.query('SELECT * FROM units WHERE text=?', (UNITE_PERIPH,), True)
        self.assertEqual(unite['anchor_date'], '2026-08-31')
        self.assertEqual(unite['anchor_kind'], 'phrase')
        run = s.query('SELECT * FROM step_runs WHERE id=?', (unite['run_id'],), True)
        self.assertEqual(run['step'], 'm1.unites')
        self.assertEqual(run['status'], 'terminee')

    def test_option_a_repli_sans_spacy(self):
        v = self.version('Titre\n\nNavBoost segmente les clics par locale et par appareil, mais il ne généralise pas '
                         'aux reformulations. Le canonical agit sur l’indexation, tandis que robots.txt agit sur l’exploration.\n')
        with patch.object(moteurs, 'analyse', return_value=None), patch.object(moteurs, 'nli_lot', side_effect=nli_simule):
            r = m.unites(v, option='A')
        textes = [u['text'] for u in self.verifier_spans(v)]
        self.assertEqual(r['extracteur'], 'A:regles')
        self.assertIn('Il ne généralise pas aux reformulations.', textes)
        self.assertIn('Robots.txt agit sur l’exploration.', textes)
        self.assertEqual(len(textes), 4)

    def test_option_a_sans_nli_garde_les_unites_copiees(self):
        v = self.version()
        with patch.object(moteurs, 'analyse', return_value=None), \
                patch.object(moteurs, 'nli_lot', side_effect=s.OperationalError('aucun moteur NLI')):
            r = m.unites(v, option='A')
        self.assertEqual(r['unites'], 2)
        self.assertEqual(r['nli_indisponible'], 2)

    def test_option_c_ancrage_et_nli(self):
        v = self.version()

        def llm(task, payload, provider=None, model=None):
            self.assertEqual(payload['mode'], 'chaine:unites')
            self.assertEqual(payload['prompt'], m.PROMPT_UNITES)
            i = payload['passages'][0]['id']
            return {'unites': [
                {'passage': i, 'texte': UNITE_PERIPH, 'citation': UNITE_PERIPH.rstrip('.'), 'sujet': 'voie gauche',
                 'cadre_temporel': 'août 2026'},
                {'passage': i, 'texte': 'La voie gauche est fermée.', 'citation': 'texte inventé', 'sujet': '', 'cadre_temporel': ''},
                {'passage': i, 'texte': 'La voie gauche du périphérique parisien sera immobilisée par la préfecture de police.',
                 'citation': 'La voie gauche du périphérique parisien sera immobilisée', 'sujet': '', 'cadre_temporel': ''},
                {'passage': i, 'texte': 'La voie gauche du périphérique parisien sera immobilisée en 2027.',
                 'citation': 'La voie gauche du périphérique parisien sera immobilisée', 'sujet': '', 'cadre_temporel': ''},
                {'passage': i, 'texte': 'Les automobilistes ne sont pas invités à anticiper leurs trajets.',
                 'citation': 'Les automobilistes sont invités à anticiper leurs trajets', 'sujet': '', 'cadre_temporel': ''},
                {'passage': 99, 'texte': 'X.', 'citation': 'X', 'sujet': '', 'cadre_temporel': ''},
            ]}
        with patch.object(s, 'llm', side_effect=llm), patch.object(moteurs, 'nli_lot', side_effect=nli_simule):
            r = m.unites(v, option='C')
        self.assertEqual(r['unites'], 1)
        self.assertEqual(r['rejet_span'], 1)
        self.assertEqual(r['rejet_mots_ajoutes'], 1)
        self.assertEqual(r['rejet_nombre_absent'], 1)
        self.assertEqual(r['rejet_nli'], 1)
        self.assertEqual(r['passage_inconnu'], 1)
        unite = self.verifier_spans(v)[0]
        self.assertEqual(unite['text'], UNITE_PERIPH)
        self.assertTrue(unite['extractor'].startswith('C:llm:'))
        self.assertAlmostEqual(unite['nli_score'], 0.95)

    def test_option_c_nli_obligatoire(self):
        v = self.version()
        sortie = {'unites': [{'passage': 0, 'texte': UNITE_PERIPH, 'citation': UNITE_PERIPH, 'sujet': '', 'cadre_temporel': ''}]}
        with patch.object(s, 'llm', return_value=sortie), \
                patch.object(moteurs, 'nli_lot', side_effect=s.OperationalError('aucun moteur NLI')):
            with self.assertRaises(s.OperationalError):
                m.unites(v, option='C')
        self.assertEqual(s.query('SELECT COUNT(*) n FROM units', one=True)['n'], 0)

    def test_version_migree_non_reextraite_sauf_forcer(self):
        v = self.version()
        migree = self.unite_migree(v, UNITE_PERIPH, UNITE_PERIPH)
        with patch.object(moteurs, 'analyse', return_value=None), patch.object(moteurs, 'nli_lot', side_effect=nli_simule):
            r = m.unites(v, option='A')
            self.assertEqual(r['versions_ignorees'], 1)
            self.assertEqual(r['unites'], 0)
            r = m.unites(v, option='A', forcer=True)
            self.assertEqual(r['unites'], 2)
            r = m.unites(v, option='A', forcer=True)
            self.assertEqual(r['unites'], 2)
        self.assertEqual(s.query('SELECT COUNT(*) n FROM units', one=True)['n'], 3)
        self.assertIsNotNone(s.query('SELECT id FROM units WHERE id=?', (migree,), True))

    def test_option_d_reservee_au_jeu_de_reference(self):
        v = self.version()
        with self.assertRaises(ValueError):
            m.unites(v, option='D')
        sortie = {'unites': [{'passage': 0, 'texte': UNITE_PERIPH, 'citation': UNITE_PERIPH, 'sujet': '', 'cadre_temporel': ''}]}
        with patch.object(s, 'llm', return_value=sortie) as appel, patch.object(moteurs, 'nli_lot', side_effect=nli_simule):
            r = m.unites(v, option='D', reference=True)
        self.assertEqual(len(r['reference']), 1)
        self.assertEqual(appel.call_args.kwargs['provider'], 'openai')
        self.assertEqual(s.query('SELECT COUNT(*) n FROM units', one=True)['n'], 0)

    def test_option_b_non_installee(self):
        v = self.version()
        with patch.dict(sys.modules, {'transformers': None}):
            with self.assertRaises(s.OperationalError):
                m.unites(v, option='B')

    def test_chunks_structurels_du_lot_m1_page(self):
        v = self.version()
        texte = s.query('SELECT text FROM versions WHERE id=?', (v,), True)['text']
        s.query('DELETE FROM chunks WHERE version_id=?', (v,))
        debut = texte.find('La voie')
        s.query('INSERT INTO chunks(id, version_id, position, start, text, kind, section, context) VALUES(?,?,?,?,?,?,?,?)',
                ('c1', v, 0, 0, 'Travaux d’été sur le périphérique.', 'titre', '', ''))
        s.query('INSERT INTO chunks(id, version_id, position, start, text, kind, section, context) VALUES(?,?,?,?,?,?,?,?)',
                ('c2', v, 1, debut, UNITE_PERIPH, 'paragraphe', 'Travaux',
                 json.dumps({'titre': 'Périphérique', 'section': 'Travaux', 'date': '2026-06-15'})))
        liste = m.passages(v)
        self.assertEqual([p['chunk_id'] for p in liste], ['c2'])
        self.assertIn('Section : Travaux', liste[0]['context'])
        with patch.object(moteurs, 'analyse', return_value=None), patch.object(moteurs, 'nli_lot', side_effect=nli_simule):
            m.unites(v, option='A')
        self.assertEqual(s.query('SELECT chunk_id FROM units', one=True)['chunk_id'], 'c2')

    def test_choisir_option(self):
        self.assertEqual(m.choisir_option('auto'), 'C')
        s.CONFIG['model'] = ''
        self.assertEqual(m.choisir_option('auto'), 'A')
        noyau.definir_reglage('m1:unites:option', 'C')
        self.assertEqual(m.choisir_option('auto'), 'C')
        with self.assertRaises(ValueError):
            m.choisir_option('Z')


# ---------------------------------------------------------------------------
# Etape 5 : faits
# ---------------------------------------------------------------------------

class FaitsTests(Base):

    def periph(self):
        v = self.version()
        return v, self.unite_migree(v, UNITE_PERIPH, UNITE_PERIPH)

    def test_exemple_du_peripherique(self):
        v, u = self.periph()
        with patch.object(s, 'llm', side_effect=llm_faits_periph), patch.object(moteurs, 'nli_lot', side_effect=nli_simule), \
                patch.object(moteurs, 'entites', side_effect=lambda t, types=None: moteurs.entites_regles(t)):
            r = m.faits([u])
        faits = s.query('SELECT * FROM facts WHERE unit_id=?', (u,))
        predicats_faits = {f['predicate'] for f in faits}
        self.assertEqual(predicats_faits, {'fait_partie_de', 'sera_ferme', 'date_de_debut', 'date_de_fin'})
        # F3 / F4 : gestion et propriete du peripherique -> hypotheses, jamais facts
        self.assertNotIn('est_gere_par', predicats_faits)
        self.assertNotIn('a_pour_proprietaire', predicats_faits)
        hyp = s.query('SELECT * FROM hypotheses WHERE unit_id=?', (u,))
        externes = {(h['predicate'], h['object']) for h in hyp if h['kind'] == 'connaissance_externe'}
        self.assertEqual(externes, {('est_gere_par', 'Ville de Paris'), ('a_pour_proprietaire', 'Ville de Paris')})
        self.assertEqual(len([h for h in hyp if h['kind'] == 'information_inconnue']), 1)
        rejets = {h['reason'].split(' ')[0]: h for h in hyp if h['kind'] == 'fait_rejete'}
        self.assertEqual(set(rejets), {'entite_absente_du_span', 'nli_sous_seuil'})
        self.assertEqual(rejets['entite_absente_du_span']['object'], 'Ville de Paris')
        self.assertEqual(rejets['nli_sous_seuil']['predicate'], 'a_pour_proprietaire')
        self.assertEqual(r['faits'], 4)
        self.assertEqual(r['faits_rejetes'], 2)
        self.assertEqual(r['connaissance_externe'], 2)
        # Ancrage, normalisation, origine, verbalisation
        texte = s.query('SELECT text FROM versions WHERE id=?', (v,), True)['text']
        for f in faits:
            self.assertIn(f['origin'], ('explicite', 'inference_contextuelle'))
            self.assertTrue(texte[f['span_start']:f['span_end']])
            self.assertGreaterEqual(f['nli_score'], 0.8)
        debut = next(f for f in faits if f['predicate'] == 'date_de_debut')
        self.assertEqual(debut['object_norm'], '2026-08-01')
        self.assertEqual(texte[debut['span_start']:debut['span_end']], 'du 1er au 31 août 2026')
        partie = next(f for f in faits if f['predicate'] == 'fait_partie_de')
        self.assertEqual(partie['verbalization'], 'La voie gauche fait partie de périphérique parisien.')
        self.assertTrue(partie['subject_id'])
        self.assertTrue(s.query('SELECT facts_run_id FROM units WHERE id=?', (u,), True)['facts_run_id'])

    def test_unite_migree_reprise_et_forcer(self):
        _, u = self.periph()
        with patch.object(s, 'llm', side_effect=llm_faits_periph) as appel, \
                patch.object(moteurs, 'nli_lot', side_effect=nli_simule):
            m.faits([u])
            r = m.faits([u])
            self.assertEqual(r['unites_ignorees'], 1)
            self.assertEqual(appel.call_count, 1)
            m.faits([u], forcer=True)
        self.assertEqual(s.query('SELECT COUNT(*) n FROM facts', one=True)['n'], 4)

    def test_unite_sans_span(self):
        v = self.version()
        u = self.unite_migree(v, UNITE_PERIPH, 'citation perdue')
        with patch.object(s, 'llm', side_effect=llm_faits_periph), patch.object(moteurs, 'nli_lot', side_effect=nli_simule):
            r = m.faits([u])
        self.assertEqual(r['faits'], 0)
        self.assertEqual(r['rejets'].get('unite_sans_span'), 6)

    def test_garde_fou_direct(self):
        unite = {'text': UNITE_PERIPH, 'quote': UNITE_PERIPH}
        with patch.object(moteurs, 'nli_lot', side_effect=nli_simule):
            ok = m.garde_fou({'sujet': 'La voie gauche', 'predicat': 'fait_partie_de', 'objet': 'périphérique parisien'}, unite)
            nli = m.garde_fou({'sujet': 'La voie gauche', 'predicat': 'a_pour_proprietaire', 'objet': 'périphérique parisien'}, unite)
            absente = m.garde_fou({'sujet': 'Le pont de Bercy', 'predicat': 'fait_partie_de', 'objet': 'périphérique parisien'}, unite)
            date = m.garde_fou({'sujet': 'La voie gauche', 'predicat': 'date_de_fin', 'objet': '30 septembre 2027'}, unite)
            hors = m.garde_fou({'sujet': 'La voie gauche', 'predicat': 'est_fermee_par', 'objet': 'x'}, unite)
        self.assertTrue(ok['ok'])
        self.assertEqual(ok['verbalisation'], 'La voie gauche fait partie de périphérique parisien.')
        self.assertEqual(nli['raison'], 'nli_sous_seuil')
        self.assertLess(nli['nli_score'], 0.8)
        self.assertEqual(absente['raison'], 'entite_absente_du_span')
        self.assertEqual(absente['entites_absentes'], ['Le pont de Bercy'])
        self.assertEqual(date['raison'], 'entite_absente_du_span')
        self.assertEqual(hors['raison'], 'predicat_hors_liste')

    def test_verbaliser_avec_periode(self):
        texte = m.verbaliser({'sujet': 'X', 'predicat': 'a_pour_dirigeant', 'objet': 'A', 'periode': 'en 2024'})
        self.assertEqual(texte, 'X a pour dirigeant A, en 2024.')

    def test_schema_llm_limite_aux_predicats_fermes(self):
        definition = noyau.schema_pour({'mode': 'chaine:faits'})
        sortie = {'unites': [{'unite': 0, 'inconnues': [], 'faits': [fait('X', 'est_le_patron_de', 'Y')]}]}
        self.assertTrue(noyau.valider(sortie, definition))
        sortie['unites'][0]['faits'][0]['predicat'] = 'a_pour_dirigeant'
        self.assertEqual(noyau.valider(sortie, definition), [])
        sortie['unites'][0]['faits'][0]['origine'] = 'information_inconnue'
        self.assertTrue(noyau.valider(sortie, definition))

    def test_pdg_deux_versions_et_canonicalisation(self):
        v = self.version('Direction.\n\nLe PDG de Exemplia est Anne Martin. Le PDG de la société Exemplia est Bruno Petit.\n')
        u1 = self.unite_migree(v, 'Le PDG de Exemplia est Anne Martin.', 'Le PDG de Exemplia est Anne Martin')
        u2 = self.unite_migree(v, 'Le PDG de la société Exemplia est Bruno Petit.', 'Le PDG de la société Exemplia est Bruno Petit')

        def llm(task, payload, provider=None, model=None):
            sortie = []
            for unite in payload['unites']:
                personne = 'Anne Martin' if 'Anne' in unite['texte'] else 'Bruno Petit'
                sujet = 'Exemplia' if 'Anne' in unite['texte'] else 'exemplia'
                sortie.append({'unite': unite['id'], 'inconnues': [],
                               'faits': [fait(sujet, 'a_pour_dirigeant', personne, citation=unite['citation'])]})
            return {'unites': sortie}
        with patch.object(s, 'llm', side_effect=llm), patch.object(moteurs, 'nli_lot', side_effect=nli_simule), \
                patch.object(moteurs, 'entites', side_effect=lambda t, types=None: moteurs.entites_regles(t)):
            r = m.faits([u1, u2])
        faits = s.query('SELECT * FROM facts ORDER BY object')
        self.assertEqual([f['object'] for f in faits], ['Anne Martin', 'Bruno Petit'])
        self.assertEqual(faits[0]['subject_id'], faits[1]['subject_id'])
        self.assertEqual(r['faits'], 2)
        self.assertGreater(r['mentions'], 0)

    def test_resolveur_alias(self):
        resolveur = m.Resolveur()
        a = resolveur.resoudre('la Ville de Paris', 'collectivite')
        self.assertEqual(resolveur.resoudre('Ville de Paris'), a)
        self.assertEqual(resolveur.resoudre('ville de paris'), a)
        self.assertEqual(resolveur.resoudre('Ville de Pariss'), a)
        self.assertNotEqual(resolveur.resoudre('Google'), a)
        with noyau.connexion() as base:
            resolveur.ecrire(base)
        self.assertEqual(s.query('SELECT COUNT(*) n FROM entities', one=True)['n'], 2)
        self.assertEqual(m.Resolveur().resoudre('Ville de Pariss'), a)

    def test_sans_extracteur(self):
        _, u = self.periph()
        s.CONFIG['model'] = ''
        with patch.object(moteurs, 'paquet_present', return_value=False):
            r = m.faits([u])
        self.assertEqual(r['unites_sans_extracteur'], 1)
        self.assertIsNone(s.query('SELECT facts_run_id FROM units WHERE id=?', (u,), True)['facts_run_id'])


class ExecuterTests(Base):

    def test_executer_bout_en_bout(self):
        v1 = self.version()
        v2 = self.version('Autre.\n\nLe brevet US8924380 a pour seul inventeur Ross Koningstein.\n', titre='Brevet')
        UnitesTests.unite_migree(self, v2, 'Le brevet US8924380 a pour seul inventeur Ross Koningstein.',
                                 'Le brevet US8924380 a pour seul inventeur Ross Koningstein')
        with patch.object(s, 'llm', side_effect=llm_faits_periph), patch.object(moteurs, 'nli_lot', side_effect=nli_simule), \
                patch.object(moteurs, 'analyse', return_value=None):
            r = m.executer(option='A')
            self.assertEqual(r['unites']['versions'], 1)
            self.assertEqual(r['unites']['versions_ignorees'], 1)
            self.assertEqual(r['faits']['unites'], 3)
            r2 = m.executer(option='A')
        self.assertEqual(r2['faits']['unites'], 0)
        self.assertEqual(r2['faits']['unites_ignorees'], 3)
        etapes = {l['step'] for l in s.query('SELECT step FROM step_runs')}
        self.assertEqual(etapes, {'m1.unites', 'm1.faits'})
        self.assertGreater(s.query('SELECT COUNT(*) n FROM facts', one=True)['n'], 0)
        self.assertEqual(s.JOB.get('stage'), 'm1.faits')

    def test_executer_unit_ids(self):
        v = self.version()
        u = UnitesTests.unite_migree(self, v, UNITE_PERIPH, UNITE_PERIPH)
        with patch.object(s, 'llm', side_effect=llm_faits_periph), patch.object(moteurs, 'nli_lot', side_effect=nli_simule):
            r = m.executer(unit_ids=[u])
        self.assertNotIn('unites', r)
        self.assertEqual(r['faits']['faits'], 4)

    def test_arret_demande(self):
        v = self.version()
        u = UnitesTests.unite_migree(self, v, UNITE_PERIPH, UNITE_PERIPH)
        s.STOP.set()
        with self.assertRaises(s.OperationalError):
            m.faits([u])
        run = s.query("SELECT status FROM step_runs WHERE step='m1.faits'", one=True)
        self.assertEqual(run['status'], 'interrompue')


if __name__ == '__main__':
    unittest.main()
