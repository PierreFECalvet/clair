"""Tests des correctifs de la relecture independante (docs/RELECTURE.md).

Chaque classe reprend un ecart reproduit par le relecteur (B1-B4, M1-M9, mineurs) :
le test echouait avant le correctif et passe apres. Base, doubles et corpus :
``tests/test_chaine_integration.py`` (LLM, NLI, embeddings, Ibou simules).
"""
import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_chaine_integration as T
import server as s
from chaine import m1_page
from chaine import m4
from chaine import m5
from chaine import moteurs
from chaine import noyau
from chaine import pipeline
from chaine import seuils

JOUR = T.JOUR


def proba(label, score=0.96):
    """Resultat NLI simule a un label."""
    probas = {'implication': 0.02, 'contradiction': 0.02, 'neutre': 0.02}
    probas[label] = score
    return {'label': label, 'score': score, 'probas': probas, 'engine': 'test:nli'}


def lancer(*etapes):
    """Lance des etapes dans l'ordre ; renvoie le dernier resultat."""
    resultat = None
    for etape in etapes:
        resultat = pipeline.lancer(etape, aujourdhui=JOUR)
    return resultat


# ---------------------------------------------------------------------------
# B1 — piege du PDG par le chemin de la deduplication
# ---------------------------------------------------------------------------

def nli_pdg(paires):
    """NLI : « PDG de X est A » et « ... est B » se contredisent ; le reste par le NLI simule."""
    sortie = []
    for premisse, hypothese in paires:
        p, h = T.plat(premisse), T.plat(hypothese)
        if ('pdg de x est a' in p and 'pdg de x est b' in h) or ('pdg de x est b' in p and 'pdg de x est a' in h):
            sortie.append(proba('contradiction', 0.94))
        else:
            sortie.extend(T.nli_simule([(premisse, hypothese)]))
    return sortie


def faits_pdg(payload):
    """Faits : a_pour_dirigeant(X, A|B)."""
    reponses = []
    for unite in payload['unites']:
        faits = []
        for valeur in ('A', 'B'):
            if 'PDG de X est ' + valeur in unite['texte']:
                faits.append({'sujet': 'X', 'predicat': 'a_pour_dirigeant', 'objet': valeur, 'portee': '', 'periode': '',
                              'origine': 'explicite', 'citation': 'PDG de X est ' + valeur})
        reponses.append({'unite': unite['id'], 'faits': faits, 'inconnues': []})
    return {'unites': reponses}


class B1PiegeDuPdgParLaDedup(T.Base):

    def setUp(self):
        super().setUp()
        original = T.llm_simule
        self.pile.enter_context(patch.object(moteurs, 'nli_lot', side_effect=nli_pdg))
        self.pile.enter_context(patch.object(T, 'llm_simule', side_effect=lambda t, p: faits_pdg(p)
                                             if p.get('mode') == 'chaine:faits' else original(t, p)))

    def test_succession_et_non_conflit(self):
        seuils.definir('dedup.cosinus', 0.4)   # la paire est captee par la deduplication (comme le relecteur)
        s.add_content('https://' + T.SITE + '/x2023', 'X en 2023', 'Gouvernance\n\nLe PDG de X est A.', '2023-01-15')
        s.add_content('https://' + T.SITE + '/x2025', 'X en 2025', 'Gouvernance\n\nLe PDG de X est B.', '2025-03-01')
        resultat = pipeline.tout(budget=0, aujourdhui=JOUR)
        self.assertTrue(resultat['ok'], resultat)
        # La deduplication a bien vu la paire (sinon le test ne prouve rien).
        dedup = {e['etape']: e for e in resultat['etapes']}['m1.sujets']['resultat']['dedup']
        self.assertEqual(dedup['conflit'], 1, dedup)
        ancienne = s.query("SELECT * FROM units WHERE text LIKE '%est A.%'", one=True)
        recente = s.query("SELECT * FROM units WHERE text LIKE '%est B.%'", one=True)
        self.assertEqual(ancienne['temporal_status'], 'obsolete_historique')
        self.assertEqual(recente['temporal_status'], 'a_jour')
        relation = s.query('SELECT * FROM unit_relations', one=True)
        self.assertEqual(relation['relation'], 'remplacement')
        self.assertFalse(str(relation['reason']).startswith('m1_dedup'))
        # Aucune requete de controle ne porte l'ancienne valeur.
        self.assertFalse(s.query("SELECT 1 x FROM queries WHERE text LIKE '% A %' AND status<>'abandonnee'", one=True))

    def test_statuts_temporels_ignorent_un_conflit_non_qualifie_par_m3(self):
        from chaine import m3
        v = s.add_content('https://' + T.SITE + '/y', 'Y', 'Y\n\nLe PDG de Y est C.\n\nLe PDG de Y est D.', '2025-01-01')
        lancer('m1.page', 'm1.unites')
        a, b = sorted(u['id'] for u in s.query('SELECT id FROM units WHERE version_id=?', (v,)))
        s.query("INSERT INTO unit_relations(a,b,relation,reason) VALUES(?,?,'conflit','m1_dedup:nli contradiction=0.94/0.94')",
                (a, b))
        m3.statuts_temporels(JOUR)
        self.assertEqual({u['temporal_status'] for u in s.query('SELECT temporal_status FROM units')}, {'a_jour'})


# ---------------------------------------------------------------------------
# M3 (majeur) — les paires « precision » de la dedup rejoignent un meme groupe
# ---------------------------------------------------------------------------

class M3PrecisionsRegroupees(T.Base):

    def test_precision_de_la_dedup_meme_groupe(self):
        def nli(paires):
            sortie = []
            for premisse, hypothese in paires:
                p, h = T.plat(premisse), T.plat(hypothese)
                if p == h or h in p:
                    sortie.append(proba('implication'))      # controle d'une unite contre son propre texte
                elif 'lyon' in p and 'lyon' not in h and 'ingenieurs' in h:
                    sortie.append(proba('implication'))      # la phrase precise implique la generale
                elif 'ingenieurs' in p and 'ingenieurs' in h:
                    sortie.append(proba('neutre'))
                else:
                    sortie.extend(T.nli_simule([(premisse, hypothese)]))
            return sortie
        self.pile.enter_context(patch.object(moteurs, 'nli_lot', side_effect=nli))
        seuils.definir('dedup.cosinus', 0.3)
        s.add_content('https://' + T.SITE + '/a', 'A', 'Emploi\n\nX emploie des ingénieurs à Lyon depuis 2020.', '2025-01-01')
        s.add_content('https://' + T.SITE + '/b', 'B', 'Emploi\n\nX emploie des ingénieurs.', '2025-02-01')
        self.assertTrue(pipeline.tout(budget=0, aujourdhui=JOUR)['ok'])
        precise = s.query("SELECT id FROM units WHERE text LIKE '%Lyon%'", one=True)['id']
        generale = s.query("SELECT id FROM units WHERE text NOT LIKE '%Lyon%' AND text LIKE '%ingénieurs%'", one=True)['id']
        relation = s.query('SELECT relation FROM unit_relations WHERE (a=? AND b=?) OR (a=? AND b=?)',
                           (precise, generale, generale, precise), True)
        self.assertEqual(relation['relation'], 'precision')
        groupes = {r['group_id'] for r in s.query('SELECT group_id FROM unit_group_members WHERE unit_id IN (?,?)',
                                                   (precise, generale))}
        self.assertEqual(len(groupes), 1)


# ---------------------------------------------------------------------------
# B2 — M4 ne compte que les sources que M2 retient (confiance)
# ---------------------------------------------------------------------------

LIVRET = ('Épargne\n\nLe taux du Livret A fixé par Bercy est actuellement de 3 %.\n\nLa Banque Postale distribue largement '
          'le produit.\n\nSources\n\n[1] Bercy, communiqué du 1er février 2025, https://www.economie.gouv.fr/livret-a\n\n'
          '[2] Rapport annuel, https://www.banque-france.fr/rapport')


class B2SourcesFaibles(T.Base):

    def test_source_de_portee_page_ne_rend_pas_sourcee(self):
        s.add_content('https://' + T.SITE + '/livret', 'Livret A', LIVRET, '2025-02-01')
        lancer('m1.page', 'm1.unites', 'm1.faits', 'm1.sujets', 'm2')
        unite = s.query("SELECT * FROM units WHERE text LIKE 'Le taux du Livret A%'", one=True)
        self.assertEqual(unite['sourcing'], 'sans_source')
        faible = s.query('SELECT max(confidence) c FROM sources WHERE unit_id=?', (unite['id'],), True)['c']
        self.assertLess(faible, seuils.get('m2.confiance_min'))
        lancer('m3', 'm4')
        unite = s.query('SELECT * FROM units WHERE id=?', (unite['id'],), True)
        self.assertEqual(unite['status'], 'a_verifier')
        statut = m4._calculer_statuts([unite['id']])[0]
        self.assertTrue(statut['requete'])
        self.assertEqual(statut['raison'], 'sans source')

    def test_source_de_verification_et_source_forte_comptent(self):
        v = s.add_content('https://' + T.SITE + '/z', 'Z', 'Z\n\nLe taux du Livret A est actuellement de 3 %.', '2025-02-01')
        lancer('m1.page', 'm1.unites')
        unite = s.query('SELECT id FROM units WHERE version_id=?', (v,), True)['id']
        s.query("UPDATE units SET obs_class='etat_courant', sourcing='sans_source' WHERE id=?", (unite,))
        s.query("INSERT INTO sources(id,unit_id,url,kind,scope,confidence) VALUES('s1',?,'https://a.fr','verification','page',0.1)",
                (unite,))
        statut = m4._calculer_statuts([unite])[0]
        self.assertEqual(statut['statut'], 'sourcee')
        s.query("DELETE FROM sources")
        s.query("INSERT INTO sources(id,unit_id,url,kind,scope,confidence) VALUES('s2',?,'https://a.fr','lien_externe','phrase',0.9)",
                (unite,))
        self.assertEqual(m4._calculer_statuts([unite])[0]['statut'], 'sourcee')


# ---------------------------------------------------------------------------
# B3 — etats courants classes immuables
# ---------------------------------------------------------------------------

class B3EtatsCourantsNonImmuables(T.Base):

    def classe(self, phrase, publiee='2025-06-01'):
        v = s.add_content('https://' + T.SITE + '/p' + str(abs(hash(phrase)) % 10 ** 8), 'Page', 'Titre\n\n' + phrase, publiee)
        lancer('m1.page', 'm1.unites')
        return s.query('SELECT obs_class, obs_rule FROM units WHERE version_id=?', (v,), True)

    def test_citation_datee_et_etat_present(self):
        for phrase in ('Selon une note de 2023, Jean Dupont dirige la société X.',
                       'La société X est dirigée par Jean Dupont (communiqué de mai 2024).',
                       'En 2023, le loyer moyen est de 12 euros le mètre carré.'):
            with self.subTest(phrase=phrase):
                ligne = self.classe(phrase)
                self.assertNotEqual(ligne['obs_class'], 'immuable', ligne)

    def test_etat_de_la_connaissance_au_present_negatif_non_immuable(self):
        for phrase in ('La proportion de documents dans chaque tier n’est pas connue.',
                       'Le fonctionnement de Glue n’est pas décrit par la documentation.',
                       'Microsoft n’a pas publié de nombre de requêtes par réponse en 2024.'):
            with self.subTest(phrase=phrase):
                ligne = self.classe(phrase)
                self.assertNotEqual(ligne['obs_class'], 'immuable', ligne)

    def test_evenement_passe_date_reste_immuable(self):
        for phrase in ('Le pont a été inauguré le 3 mars 2019 par le maire.',
                       'La société X a été fondée en 1998 à Lyon.'):
            with self.subTest(phrase=phrase):
                ligne = self.classe(phrase)
                self.assertEqual(ligne['obs_class'], 'immuable', ligne)

    def test_regles_sans_verbe_d_evenement_ne_decident_pas_immuable(self):
        indices = m1_page._indices('Selon une note de 2023, Jean Dupont dirige la société X.', {'present'},
                                   [{'nature': 'absolue', 'resolue': True, 'debut': '2023-01-01', 'fin': '2023-12-31',
                                     'modificateur': ''}], '2025-06-01')
        classe, _, regle = m1_page._regles_classe(indices)
        self.assertEqual(classe, 'etat_courant', regle)
        self.assertEqual(m1_page._temps_regles('la societe x est dirigee par jean dupont'), {'present'})
        self.assertEqual(m1_page._temps_regles('jean dupont est ne a lyon'), {'passe'})

    def test_participe_ete_n_est_pas_la_saison(self):
        from chaine import dates
        self.assertEqual([e['texte'] for e in dates.trouver('Le pont a été inauguré le 3 mars 2019.', '2025-01-01')],
                         ['le 3 mars 2019'])
        self.assertEqual([e['debut'] for e in dates.trouver('Cet été, les travaux reprennent.', '2025-01-01')], ['2025-06-21'])


# ---------------------------------------------------------------------------
# B4 — « corrigee » exige plusieurs domaines independants
# ---------------------------------------------------------------------------

def nli_marques(paires):
    """NLI : SOUTIEN -> implication, CONTRA -> contradiction, « rien » -> neutre, sinon implication."""
    sortie = []
    for premisse, _ in paires:
        if 'SOUTIEN' in premisse:
            sortie.append(proba('implication'))
        elif 'CONTRA' in premisse:
            sortie.append(proba('contradiction'))
        elif 'rien' in premisse:
            sortie.append(proba('neutre'))
        else:
            sortie.append(proba('implication'))
    return sortie


class B4CorrectionMultiDomaines(T.Base):

    def setUp(self):
        super().setUp()
        original = T.llm_simule

        def llm(tache, payload):
            if payload.get('mode') == 'chaine:m5_valeur':
                trouve = re.search(r'dirigé par (\w+)', payload['passage'])
                valeur = trouve.group(1) if trouve else ''
                return {'trouve': bool(trouve), 'valeur': valeur, 'citation': valeur}
            if payload.get('mode') == 'chaine:m5_selection':
                # La selection du contenu suit les memes marqueurs que le NLI du test :
                # ces cas visent l'agregation par domaines, pas la selection elle-meme.
                label = nli_marques([(payload['passage'], payload['fait'])])[0]['label']
                position = {'implication': 'soutient', 'contradiction': 'contredit'}.get(label, 'hors_sujet')
                return {'position': position, 'citation': payload['passage'], 'motif': 'Selection simulee par marqueurs.'}
            return original(tache, payload)
        self.pile.enter_context(patch.object(moteurs, 'nli_lot', side_effect=nli_marques))
        self.pile.enter_context(patch.object(moteurs, 'nli', side_effect=lambda a, b: proba('implication')))
        self.pile.enter_context(patch.object(T, 'llm_simule', side_effect=llm))
        s.add_content('https://' + T.SITE + '/x', 'X', 'Gouvernance\n\nLe PDG de X est B.', '2025-03-01')
        lancer('m1.page', 'm1.unites', 'm1.faits', 'm1.sujets')
        unite = s.query('SELECT id FROM units', one=True)['id']
        self.groupe = s.query('SELECT group_id FROM unit_group_members WHERE unit_id=?', (unite,), True)['group_id']

    def verdict(self, preuves):
        s.query('DELETE FROM evidence')
        for i, (dom, jour, texte) in enumerate(preuves):
            s.query('INSERT INTO evidence(id,claim_id,url,title,text,created,query_id,group_id,domain,page_date,kept,relevance,weight) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,1,0.9,0.6)',
                    ('e' + str(i), self.groupe, 'https://' + dom + '/p' + str(i), dom, texte, s.now(), 'q', self.groupe, dom, jour))
        return m5.verdict(self.groupe, JOUR)

    def test_un_seul_domaine_qui_contredit_ne_corrige_pas(self):
        d = self.verdict([('a.fr', '2026-10-01', 'CONTRA X dirigé par Claire')])
        self.assertEqual(d['verdict'], 'insuffisante', d)
        d = self.verdict([('a.fr', '2026-10-01', 'CONTRA X dirigé par Claire'), ('a.fr', '2026-10-01', 'CONTRA X dirigé par Claire')])
        self.assertEqual(d['verdict'], 'insuffisante', d)
        d = self.verdict([('a.fr', '2026-10-01', 'CONTRA X dirigé par Claire'), ('www.a.fr', '2026-10-02', 'CONTRA X dirigé par Claire')])
        self.assertEqual(d['verdict'], 'insuffisante', d)

    def test_un_domaine_recent_contre_deux_soutiens_est_conflictuel(self):
        d = self.verdict([('a.fr', '2026-09-01', 'SOUTIEN'), ('b.fr', '2026-09-02', 'SOUTIEN'),
                          ('c.fr', '2026-10-02', 'CONTRA X dirigé par Paul')])
        self.assertEqual(d['verdict'], 'conflictuelle', d)

    def test_deux_domaines_coherents_corrigent(self):
        d = self.verdict([('a.fr', '2026-10-01', 'CONTRA X dirigé par Claire'), ('b.fr', '2026-10-02', 'CONTRA X dirigé par Claire')])
        self.assertEqual(d['verdict'], 'corrigee', d)
        d = self.verdict([('a.fr', '2026-10-01', 'CONTRA X dirigé par Claire'), ('b.fr', '2026-10-02', 'CONTRA X dirigé par Paul')])
        self.assertEqual(d['verdict'], 'conflictuelle', d)


# ---------------------------------------------------------------------------
# M4 (majeur) — panne du LLM en option C
# ---------------------------------------------------------------------------

class M4PanneLlm(T.Base):

    def test_panne_llm_option_c_arrete_la_chaine_avec_raison(self):
        def panne(task, payload, provider, model, meta):
            if payload.get('mode') == 'chaine:unites':
                raise s.OperationalError('LM Studio injoignable')
            return T.requete_llm_simulee(task, payload, provider, model, meta)
        self.pile.enter_context(patch.object(s, 'llm_request', side_effect=panne))
        noyau.definir_reglage('m1:unites:option', 'C')
        self.corpus()
        resultat = pipeline.tout(budget=0, aujourdhui=JOUR)
        self.assertFalse(resultat['ok'])
        self.assertEqual(resultat['arret'], 'm1.unites')
        self.assertIn('échec', resultat['raison'])
        self.assertIn('LM Studio injoignable', resultat['raison'])
        run = s.query("SELECT status, note FROM step_runs WHERE step='m1.unites'", one=True)
        self.assertEqual(run['status'], 'erreur')

    def test_m1_sujets_sans_unite_se_saute(self):
        resultat = pipeline.lancer('m1.sujets', aujourdhui=JOUR)
        self.assertTrue(resultat['ok'], resultat)
        self.assertTrue(resultat['resultat']['sujets']['saute'])


# ---------------------------------------------------------------------------
# M6 (majeur) — « source ajoutee » sans exiger source_manquante
# ---------------------------------------------------------------------------

class M6SourceAjoutee(T.Base):

    def test_confirmee_sans_source_recoit_une_proposition(self):
        from chaine import m6
        v = s.add_content('https://' + T.SITE + '/t', 'T', 'Taux\n\nLe taux du Livret A est de 3 %.', '2025-02-01')
        lancer('m1.page', 'm1.unites')
        unite = s.query('SELECT id FROM units WHERE version_id=?', (v,), True)['id']
        s.query("UPDATE units SET verdict='confirmee', status='sourcee', sourcing='sans_source', source_manquante=0 WHERE id=?",
                (unite,))
        s.query("INSERT INTO sources(id,unit_id,url,domain,kind,scope,confidence) VALUES('sv',?,'https://www.economie.gouv.fr/a',"
                "'economie.gouv.fr','verification','page',0.9)", (unite,))
        kinds = {c['unit_id']: c['kind'] for c in m6.perimetre(JOUR)}
        self.assertEqual(kinds.get(unite), 'source_ajoutee')
        s.query("UPDATE units SET sourcing='connaissance_generale' WHERE id=?", (unite,))
        self.assertEqual({c['unit_id']: c['kind'] for c in m6.perimetre(JOUR)}.get(unite), 'source_ajoutee')
        s.query("UPDATE units SET sourcing='sourcee_externe' WHERE id=?", (unite,))
        self.assertNotIn(unite, {c['unit_id'] for c in m6.perimetre(JOUR)})


# ---------------------------------------------------------------------------
# M7 (majeur) — une requete par cadre (sujet, famille, periode), toutes familles
# ---------------------------------------------------------------------------

class M7RequeteParCadre(T.Base):

    def ajouter_fait(self, unite, sujet, predicat, objet, periode=''):
        s.query("INSERT INTO facts(id,unit_id,subject,predicate,object,period,origin,status,created) "
                "VALUES(?,?,?,?,?,?,'explicite','valide',?)", (s.uid(), unite, sujet, predicat, objet, periode, s.now()))

    def test_chaque_cadre_de_l_unite_a_sa_requete_et_les_cadres_communs_sont_partages(self):
        v1 = s.add_content('https://' + T.SITE + '/m1', 'M1', 'X\n\nLa société X, dirigée par B, facture 10 euros '
                           'et le salon Y aura lieu en mars 2027.', '2026-01-10')
        v2 = s.add_content('https://' + T.SITE + '/m2', 'M2', 'X\n\nLe PDG de X est B depuis 2020.', '2026-02-10')
        lancer('m1.page', 'm1.unites')
        u1 = s.query('SELECT id FROM units WHERE version_id=?', (v1,), True)['id']
        u2 = s.query('SELECT id FROM units WHERE version_id=?', (v2,), True)['id']
        s.query("UPDATE units SET obs_class='etat_courant', sourcing='sans_source', status=NULL, temporal_status='a_jour'")
        self.ajouter_fait(u1, 'X', 'a_pour_dirigeant', 'B')
        self.ajouter_fait(u1, 'X', 'prix', '10 euros')
        self.ajouter_fait(u1, 'salon Y', 'aura_lieu_le', 'mars 2027', 'mars 2027')
        self.ajouter_fait(u2, 'X', 'a_pour_dirigeant', 'B')
        m4.requetes(JOUR, unit_ids=[u1, u2], reformuler_llm=False)
        ouvertes = s.query("SELECT q.id, q.text, q.template FROM queries q JOIN query_units qu ON qu.query_id=q.id "
                           "WHERE qu.unit_id=? AND q.kind='ouverte'", (u1,))
        gabarits = sorted(q['template'] for q in ouvertes)
        self.assertEqual(len(ouvertes), 3, gabarits)
        self.assertTrue(any(g.startswith('annonce') for g in gabarits), gabarits)
        self.assertIn('etat_courant:a_pour_dirigeant', gabarits)
        self.assertIn('etat_courant:prix', gabarits)
        dirigeant = [q['id'] for q in ouvertes if q['template'] == 'etat_courant:a_pour_dirigeant'][0]
        membres = {r['unit_id'] for r in s.query('SELECT unit_id FROM query_units WHERE query_id=?', (dirigeant,))}
        self.assertEqual(membres, {u1, u2})


# ---------------------------------------------------------------------------
# M8 (majeur) — faits « externe » : preuve de reference, predicat obligatoire
# ---------------------------------------------------------------------------

import test_chaine_m5 as TM5   # noqa: E402  (base unitaire de M5)


class M8FaitsExternes(TM5.M5Base):

    def test_fait_corrige_porte_sa_preuve_et_un_span_relatif_a_elle(self):
        unite, _ = self.pdg()
        p1 = self.preuve('g1', 'https://a.fr/1', 'Depuis octobre 2026, X est dirigé par Claire Martin.', '2026-10-05')
        self.preuve('g1', 'https://b.fr/1', 'Claire Martin a pris la tête de X.', '2026-10-20')
        regles = [('Claire', 'B', 'contradiction'), ('Claire', 'Claire', 'implication')]
        with patch.object(moteurs, 'nli_lot', side_effect=TM5.nli_par_mots(regles)), \
                patch.object(s, 'llm', return_value={'trouve': True, 'valeur': 'Claire Martin', 'citation': 'Claire Martin'}):
            self.assertEqual(m5.verdict('g1', JOUR)['verdict'], 'corrigee')
            m5.reecrire('g1', JOUR)
        nouveau = s.query("SELECT * FROM facts WHERE unit_id=? AND object='Claire Martin'", (unite,), True)
        self.assertIsNotNone(nouveau['evidence_id'])
        preuve = s.query('SELECT text FROM evidence WHERE id=?', (nouveau['evidence_id'],), True)
        self.assertEqual(preuve['text'][nouveau['span_start']:nouveau['span_end']], 'Claire Martin')
        self.assertIn(nouveau['evidence_id'], {p1} | {r['id'] for r in s.query('SELECT id FROM evidence')})

    def resoudre(self, predicat, attendu='completee'):
        unite, _ = self.pdg()
        self.hypothese(unite, 'information_inconnue', 'date de nomination', predicat)
        self.preuve('g1', 'https://a.fr/1', 'B dirige X depuis sa nomination le 3 mars 2025.', '2026-09-01')
        self.preuve('g1', 'https://b.fr/1', 'X est dirigé par B.', '2026-10-01')

        def llm(tache, payload):
            if 'nomination' in payload['passage']:
                return {'trouve': True, 'valeur': '3 mars 2025', 'citation': 'nomination le 3 mars 2025'}
            return {'trouve': False, 'valeur': '', 'citation': ''}
        with patch.object(moteurs, 'nli_lot', side_effect=TM5.nli_par_mots([('B', None, 'implication')])), \
                patch.object(s, 'llm', side_effect=llm):
            self.assertEqual(m5.verdict('g1', JOUR)['verdict'], attendu)
            m5.reecrire('g1', JOUR)
        return unite

    def test_inconnue_sans_predicat_n_est_pas_resolue(self):
        """Sans predicat de la liste fermee, aucune relation n'est verifiable par NLI :
        la valeur lue n'est ni retenue ni transformee en fait, et le groupe reste confirme.

        Regle du 15/11/2026 (selection des preuves) : un complement doit verifier une
        relation sur le sujet, pas seulement la presence de sa propre citation.
        """
        unite = self.resoudre(None, attendu='confirmee')
        self.assertFalse(s.query("SELECT 1 x FROM facts WHERE origin='externe' AND predicate IS NULL", one=True))
        hypothese = s.query('SELECT * FROM hypotheses WHERE unit_id=?', (unite,), True)
        self.assertIsNone(hypothese['resolved_fact_id'])
        self.assertIsNone(hypothese['resolved_value'])
        # L'inconnue reste ouverte : M4 continue de la demander.
        self.assertNotEqual(m4._inconnues([hypothese]), [])

    def test_inconnue_resolue_avec_predicat_devient_un_fait_ancre(self):
        unite = self.resoudre('date_de_debut')
        hypothese = s.query('SELECT * FROM hypotheses WHERE unit_id=?', (unite,), True)
        fait = s.query('SELECT * FROM facts WHERE id=?', (hypothese['resolved_fact_id'],), True)
        self.assertEqual(fait['predicate'], 'date_de_debut')
        self.assertIsNotNone(fait['evidence_id'])


# ---------------------------------------------------------------------------
# M9 (majeur) — tout est rejouable : date du jour, execution de chaque statut, moteurs reels
# ---------------------------------------------------------------------------

class M9Rejouable(T.Base):

    def test_parametres_colonnes_run_et_moteurs_reels(self):
        self.corpus()
        seuils.definir('m5.critique', 0.0)
        self.assertTrue(pipeline.tout(budget=60, aujourdhui=JOUR)['ok'])
        # Date du jour dans les parametres de toutes les executions qui en dependent.
        for etape in ('m1.obsolescence', 'm1.peremption', 'm3.statuts', 'm4.statuts', 'm4.requetes', 'm5.criticite',
                      'm5.verification', 'm6.proposer'):
            run = s.query('SELECT params FROM step_runs WHERE step=? ORDER BY started DESC LIMIT 1', (etape,), True)
            self.assertIsNotNone(run, etape)
            self.assertEqual(json.loads(run['params'] or '{}').get('aujourdhui'), JOUR, etape)
        # Chaque statut reecrit porte l'execution qui l'a ecrit.
        pdg = self.unite_de(self.v_pdg, 'PDG de X')
        for colonne, etapes in (('status_run', ('m4.statuts', 'm5.verification', 'm2', 'm2.typologie')),
                                ('temporal_run', ('m3.statuts', 'm1.peremption')),
                                ('critical_run', ('m5.criticite', 'm4.requetes')),
                                ('verdict_run', ('m5.verification',))):
            identifiant = pdg[colonne]
            self.assertTrue(identifiant, colonne)
            etape = s.query('SELECT step FROM step_runs WHERE id=?', (identifiant,), True)
            self.assertIn(etape['step'], etapes, colonne)
        # Moteurs reellement utilises par M5 (le NLI simule), pas ceux de moteurs.etat().
        run = s.query("SELECT engines FROM step_runs WHERE step='m5.verification'", one=True)
        moteurs_m5 = json.loads(run['engines'])
        self.assertEqual(moteurs_m5.get('nli'), 'test:nli')
        self.assertEqual(moteurs_m5.get('reordonner'), 'test')


# ---------------------------------------------------------------------------
# M2 (majeur) — l'import par URL passe par M1 etape 1 (acquerir), M2 voit les liens
# ---------------------------------------------------------------------------

PAGE_IMPORT = (
    '<html><head><title>Livret A : le taux</title>'
    '<script type="application/ld+json">{"@type":"NewsArticle","headline":"Livret A","datePublished":"2025-02-01",'
    '"dateModified":"2025-03-15","author":{"@type":"Person","name":"Claire Martin"}}</script></head><body>'
    '<article><h1>Livret A : le taux</h1><p class="reading-time">Temps de lecture : 3 min</p>'
    '<p>Le taux du Livret A est actuellement de 3 %, selon le <a href="https://www.economie.gouv.fr/livret-a">'
    'communiqué du ministère</a>.</p>'
    '<p>Partager sur Facebook</p></article></body></html>')


class M2ImportParAcquerir(T.Base):

    def setUp(self):
        super().setUp()
        import threading
        from urllib.request import Request, urlopen
        self.urlopen, self.Request = urlopen, Request
        self.serveur = s.ThreadingHTTPServer(('127.0.0.1', 0), s.Handler)
        self.fil = threading.Thread(target=self.serveur.serve_forever, daemon=True)
        self.fil.start()
        self.base = 'http://127.0.0.1:' + str(self.serveur.server_port)
        self.pile.enter_context(patch.object(m1_page, '_telecharger', return_value=(PAGE_IMPORT, 'text/html')))

    def tearDown(self):
        self.serveur.shutdown()
        self.serveur.server_close()
        self.fil.join()
        super().tearDown()

    def post(self, chemin, donnees):
        requete = self.Request(self.base + chemin, data=json.dumps(donnees).encode(),
                               headers={'Content-Type': 'application/json', 'X-Session-Token': s.TOKEN})
        with self.urlopen(requete, timeout=20) as reponse:
            return json.loads(reponse.read())

    def test_import_url_json_ld_puis_m2(self):
        version = self.post('/api/import', {'url': 'https://' + T.SITE + '/livret'})['version_id']
        ligne = s.query('SELECT * FROM versions WHERE id=?', (version,), True)
        self.assertEqual(ligne['published'], '2025-02-01')
        self.assertEqual(ligne['updated'], '2025-03-15')
        self.assertEqual(ligne['author'], 'Claire Martin')
        self.assertTrue(ligne['meta'])
        self.assertNotIn('Temps de lecture', ligne['text'])
        self.assertNotIn('Partager', ligne['text'])
        lancer('m1.page', 'm1.unites', 'm1.faits', 'm1.sujets', 'm2')
        unite = s.query("SELECT * FROM units WHERE text LIKE 'Le taux du Livret A%'", one=True)
        self.assertEqual(unite['sourcing'], 'sourcee_externe')
        self.assertTrue(s.query("SELECT 1 x FROM sources WHERE unit_id=? AND url='https://www.economie.gouv.fr/livret-a'",
                                (unite['id'],), True))

    def test_import_texte_colle_garde_la_date_saisie(self):
        version = self.post('/api/import', {'url': 'https://' + T.SITE + '/colle', 'title': 'Collé', 'published': '2024-05-02',
                                            'text': 'Titre\n\nLe taux du Livret A est de 3 %.'})['version_id']
        ligne = s.query('SELECT published, meta FROM versions WHERE id=?', (version,), True)
        self.assertEqual(ligne['published'], '2024-05-02')
        self.assertTrue(ligne['meta'])

    def test_import_groupe_passe_par_acquerir(self):
        identifiant = 'a' * 32
        self.post('/api/import-batch', {'items': [{'id': identifiant, 'title': 'Lot', 'text': 'Titre\n\nPar Jean Dupont\n\n'
                                                   'Le taux du Livret A est de 3 %.'}]})
        for _ in range(100):
            if s.JOB.get('status') != 'running':
                break
            import time
            time.sleep(0.05)
        ligne = s.query("SELECT v.meta, v.author FROM versions v JOIN pages p ON p.id=v.page_id WHERE p.url=?",
                        ('texte:lot:' + identifiant,), True)
        self.assertTrue(ligne['meta'])


# ---------------------------------------------------------------------------
# M5 (majeur) — voisins denses de M3 par produit matriciel (numpy), memes resultats que le repli Python
# ---------------------------------------------------------------------------

class M5VoisinsNumpy(T.Base):

    def test_numpy_sans_cosinus_python_et_resultats_identiques(self):
        from chaine import m3
        textes = ['Le PDG de X est B.', 'X est dirigée par B.', 'Le siège de X est à Lyon.', 'X a son siège à Lyon.',
                  'Le taux du Livret A est de 3 %.', 'Le Livret A rapporte 3 %.']
        for rang, texte in enumerate(textes):
            s.add_content('https://' + T.SITE + '/v' + str(rang), 'Page ' + str(rang), 'Titre\n\n' + texte, '2025-01-0' + str(rang + 1))
        lancer('m1.page', 'm1.unites', 'm1.faits', 'm1.sujets')
        seuils.definir('m3.voisins.cosinus', 0.2)
        ids = sorted(u['id'] for u in s.query('SELECT id FROM units'))
        python = m3.Index()
        python._matrice = False          # repli pur Python (sans numpy)
        attendu = {u: [v['unit_id'] for v in python.voisins(u)] for u in ids}
        self.assertTrue(any(attendu.values()))
        index = m3.Index()
        with patch.object(moteurs, 'cosinus', side_effect=AssertionError('cosinus Python appele')):
            index.preparer_denses(ids)
            obtenu = {u: [v['unit_id'] for v in index.voisins(u)] for u in ids}
        self.assertEqual(obtenu, attendu)


# ---------------------------------------------------------------------------
# M1 (majeur) — double passe reelle, LLM local ou tiers, classement lexical avant LLM
# ---------------------------------------------------------------------------

class M1HierarchieDesMoyens(T.Base):

    def test_fournisseur_tiers_refuse_par_defaut_pour_les_etapes_llm_local(self):
        s.query("DELETE FROM settings WHERE key='m1:unites:option'")   # option automatique
        noyau.vider_reglages()
        s.CONFIG.update(provider='openai', openai_key='cle-de-test', model='gpt-test')
        self.assertEqual(noyau.type_fournisseur(), 'tiers')
        from chaine import m1_unites
        self.assertEqual(m1_unites.choisir_option('auto'), 'A')
        raison = pipeline.prerequis('m1.unites', option='C')
        self.assertIn('tiers', raison)
        self.assertIn('chaine:llm_tiers_autorise', raison)
        self.assertIn('tiers', pipeline.prerequis('m1.faits'))
        with self.assertRaises(s.OperationalError) as erreur:
            noyau.llm('tache', {'mode': 'chaine:nli', 'paires': []})
        self.assertIn('tiers', str(erreur.exception))
        etat = pipeline.etat()
        self.assertEqual(etat['moteurs']['llm']['type'], 'tiers')
        self.assertFalse(etat['moteurs']['llm']['tiers_autorise'])
        self.assertIn('tiers', etat['prerequis']['m1.faits'])
        # Reglage explicite : le LLM tiers est accepte.
        noyau.definir_reglage('chaine:llm_tiers_autorise', '1')
        self.assertEqual(m1_unites.choisir_option('auto'), 'C')
        self.assertIsNone(pipeline.prerequis('m1.faits'))
        self.assertTrue(pipeline.etat()['moteurs']['llm']['tiers_autorise'])

    def test_reglages_api(self):
        from chaine import api
        self.assertFalse(api.reglages()['llm_tiers_autorise'])
        self.assertFalse(api.reglages()['nli_par_llm'])
        api.modifier_reglages({'llm_tiers_autorise': True, 'nli_par_llm': True})
        self.assertTrue(noyau.llm_tiers_autorise())
        self.assertTrue(moteurs.nli_par_llm_autorise())
        self.assertTrue(api.reglages()['nli_par_llm'])
        with self.assertRaises(ValueError):
            api.modifier_reglages({'nli_par_llm': 'oui'})

    def test_lm_studio_est_local(self):
        self.assertEqual(noyau.type_fournisseur(), 'local')
        self.assertIsNone(pipeline.prerequis('m1.faits'))

    def test_nli_par_le_meme_llm_refuse_pour_controler_ses_sorties(self):
        self.pile.enter_context(patch.object(moteurs, 'nli_prevu', return_value=noyau.moteur_llm()))
        raison = pipeline.prerequis('m1.faits')
        self.assertIn('double passe', raison)
        self.assertIn('chaine:nli_par_llm', raison)
        self.assertIn('double passe', pipeline.prerequis('m1.unites', option='C'))
        self.assertIsNone(pipeline.prerequis('m1.unites', option='A'))   # regles : rien de generatif a controler
        self.assertIn('double passe', pipeline.etat()['prerequis']['m1.faits'])
        self.corpus()
        resultat = pipeline.tout(budget=0, aujourdhui=JOUR)
        self.assertFalse(resultat['ok'])
        self.assertEqual(resultat['arret'], 'm1.faits')
        self.assertIn('double passe', resultat['raison'])
        self.assertEqual(s.query('SELECT count(*) n FROM facts', one=True)['n'], 0)

    def test_nli_par_llm_autorise_marque_double_passe_faible(self):
        def nli_llm(paires):
            return [dict(r, engine=noyau.moteur_llm()) for r in T.nli_simule(paires)]
        self.pile.enter_context(patch.object(moteurs, 'nli_prevu', return_value=noyau.moteur_llm()))
        self.pile.enter_context(patch.object(moteurs, 'nli_lot', side_effect=nli_llm))
        noyau.definir_reglage('chaine:nli_par_llm', '1')
        self.assertIsNone(pipeline.prerequis('m1.faits'))
        self.corpus()
        lancer('m1.page', 'm1.unites', 'm1.faits')
        run = s.query("SELECT engines FROM step_runs WHERE step='m1.faits'", one=True)
        self.assertEqual(json.loads(run['engines']).get('double_passe'), 'faible')

    def test_resultat_nli_du_llm_refuse_meme_si_le_moteur_prevu_differait(self):
        def nli_llm(paires):
            return [dict(r, engine=noyau.moteur_llm()) for r in T.nli_simule(paires)]
        self.pile.enter_context(patch.object(moteurs, 'nli_lot', side_effect=nli_llm))
        with self.assertRaises(s.OperationalError):
            moteurs.constater_double_passe(moteurs.nli_lot([('a', 'b')]))

    def test_classer_lexical_avant_llm(self):
        appels = []
        original = T.llm_simule

        def llm(tache, payload):
            appels.append(payload.get('mode'))
            return original(tache, payload)
        self.pile.enter_context(patch.object(T, 'llm_simule', side_effect=llm))
        s.CONFIG.update(embeddings=False)
        self.pile.enter_context(patch.object(moteurs, 'embeddings', return_value=([], 'aucun')))
        sortie = moteurs.classer(['Le prix est de 3 euros.'], ['a', 'b'], {'a': ['prix euros'], 'b': ['naissance auteur']})
        self.assertEqual(sortie[0]['engine'], 'lexical')
        self.assertNotIn('chaine:classer', appels)


# ---------------------------------------------------------------------------
# Mineurs
# ---------------------------------------------------------------------------

class Mineur503SansSurcompte(TM5.M5Base):

    def test_503_rembourse_la_reservation(self):
        _, requete = self.pdg()
        budget = self.budget(30)
        sequence = [TM5.reponse(503, b'Service Unavailable', {'Retry-After': '5'}),
                    TM5.reponse(503, b'Service Unavailable', {'Retry-After': '5'}),
                    TM5.reponse(200, TM5.resultats())]
        with patch.object(m5, '_http', side_effect=sequence):
            m5.appeler(requete, budget)
        self.assertEqual(self.utilise(budget), 3)
        self.assertEqual(s.query('SELECT sum(units) n FROM ibou_calls', one=True)['n'], 3)
        self.assertEqual(s.query("SELECT count(*) n FROM calls WHERE status='success'", one=True)['n'], 1)


class MineurDedupTronquee(T.Base):

    def test_troncature_signalee_comme_incomplete(self):
        self.corpus()
        seuils.definir('dedup.cosinus', 0.1)
        seuils.definir('dedup.nli_paires_max', 1)
        lancer('m1.page', 'm1.unites', 'm1.faits')
        resultat = pipeline.lancer('m1.sujets', aujourdhui=JOUR)
        self.assertGreater(resultat['resultat']['dedup']['tronquees'], 0)
        self.assertFalse(resultat['ok'])
        self.assertTrue(resultat['partiel'])
        self.assertIn('reportée', resultat['raison'])
        # Les relances continuent la deduplication jusqu'au bout (cache des paires deja qualifiees).
        for _ in range(20):
            resultat = pipeline.lancer('m1.sujets', aujourdhui=JOUR)
            if resultat['ok']:
                break
        self.assertTrue(resultat['ok'], resultat)


class MineurAccents(T.Base):

    def test_accents_absents_reperes_par_lexique_et_par_la_source(self):
        requete = 'Le peripherique parisien sera-t-il ferme en aout 2026 ?'
        problemes = ' '.join(m4.regles(requete))
        self.assertIn('peripherique', problemes)
        source = 'La voie gauche du périphérique parisien sera fermée en août 2026.'
        self.assertIn('fermee', ' '.join(m4.regles('La voie gauche sera-t-elle fermee en 2026 ?', source)))
        self.assertEqual(m4.regles('Le périphérique parisien sera-t-il fermé en août 2026 ?', source), [])


class MineurCarteM6VerdictNli(T.Base):

    def test_preuves_de_la_carte_portent_le_label_et_le_score_nli(self):
        self.corpus()
        seuils.definir('m5.critique', 0.0)
        self.assertTrue(pipeline.tout(budget=60, aujourdhui=JOUR)['ok'])
        proposition = s.query("SELECT sources FROM proposals WHERE kind='corrigee'", one=True)
        preuves = json.loads(proposition['sources'])
        self.assertTrue(preuves)
        for preuve in preuves:
            self.assertIn(preuve['verdict_nli'], ('implication', 'contradiction', 'neutre'))
            self.assertIsInstance(preuve['score_nli'], float)
            self.assertIn('contribution', preuve)


class MineurInterpretationsDependantes(T.Base):

    def test_dependances_du_connecteur_et_statut_suivi_persiste(self):
        s.add_content('https://' + T.SITE + '/i', 'I', "Emploi\n\nSelon l'Insee, le taux de chômage s'élève à 7,4 % de la "
                      "population active.\n\nCela signifie que le marché du travail reste tendu.", '2026-02-01')
        lancer('m1.page', 'm1.unites', 'm1.faits', 'm1.sujets', 'm2', 'm3', 'm4')
        insee = s.query("SELECT * FROM units WHERE text LIKE 'Selon%'", one=True)
        interpretation = s.query("SELECT u.*, t.depends_on FROM units u JOIN claim_types t ON t.unit_id=u.id "
                                 "WHERE u.text LIKE 'Cela signifie%'", one=True)
        self.assertEqual(interpretation['status'], 'dependante')
        self.assertEqual(json.loads(interpretation['depends_on']), [insee['id']])
        self.assertEqual(interpretation['status_suivi'], insee['status'])


# ---------------------------------------------------------------------------
# Test manquant : criticite et pertinence de bout en bout (seuil par defaut, reordonnancement non constant)
# ---------------------------------------------------------------------------

class CriticiteEtPertinenceDeBoutEnBout(T.Base):

    def test_critiques_d_abord_hors_sujet_ecarte(self):
        def lexical(requete, passages, avec_moteur=False):
            scores = moteurs.recouvrement_lexical(requete, passages)
            return (scores, 'lexical') if avec_moteur else scores
        contenus = dict(T.CONTENUS_IBOU)
        contenus['https://c.fr/1'] = 'Cuisine.\n\nLa tarte aux pommes se prépare avec une pâte brisée et du beurre.'
        jours = dict(T.DATES_IBOU, **{'https://c.fr/1': '2026-10-10'})

        def ibou(*args, **kwargs):
            corps = {'results': [{'url': u, 'title': 'T', 'passages': [{'text': x}]} for u, x in contenus.items()]}
            return 200, json.dumps(corps).encode(), {'X-Units-Remaining': '900'}
        self.pile.enter_context(patch.object(moteurs, 'reordonner', side_effect=lexical))
        http = self.pile.enter_context(patch.object(m5, '_http', side_effect=ibou))
        self.pile.enter_context(patch.object(m5, '_telecharger', side_effect=lambda u: T.page_html(contenus[u], jours[u])))
        self.corpus()
        self.assertTrue(pipeline.tout(budget=60, aujourdhui=JOUR)['ok'])   # m5.critique par defaut
        periph = self.unite_de(self.v_periph, 'voie gauche')
        pdg = self.unite_de(self.v_pdg, 'PDG de X')
        self.assertEqual(periph['critical'], 1)       # perimee par date, centrale, en tete de page
        self.assertEqual(pdg['critical'], 0)
        requete_pdg = s.query("SELECT * FROM queries WHERE text LIKE 'Qui dirige X%'", one=True)
        self.assertNotEqual(requete_pdg['status'], 'envoyee')    # file « plus tard »
        appels = http.call_count
        self.assertGreater(appels, 0)
        # Pertinence : la page hors sujet n'est jamais retenue.
        self.assertFalse(s.query("SELECT 1 x FROM evidence WHERE url='https://c.fr/1' AND kept=1", one=True))
        # File « plus tard » sur demande explicite.
        self.assertTrue(pipeline.lancer('m5', budget=60, aujourdhui=JOUR, plus_tard=True)['ok'])
        self.assertGreater(http.call_count, appels)
        self.assertEqual(s.query('SELECT status FROM queries WHERE id=?', (requete_pdg['id'],), True)['status'], 'envoyee')
        hors_sujet = s.query("SELECT kept, reject_reason FROM evidence WHERE url='https://c.fr/1' AND query_id=?",
                             (requete_pdg['id'],), True)
        self.assertEqual(hors_sujet['kept'], 0)
        self.assertTrue(hors_sujet['reject_reason'])
        self.assertTrue(s.query("SELECT 1 x FROM evidence WHERE url='https://a.fr/1' AND query_id=? AND kept=1",
                                (requete_pdg['id'],), True))


import test_chaine_fondation as TF   # noqa: E402  (doubles des bibliotheques locales)


class MineurNliFenetres(TF.Base):
    """Premisse plus longue que la fenetre du modele NLI (``nli.jetons_max``) : fenetres puis agregation."""

    def test_premisse_longue_decoupee_en_fenetres(self):
        module = TF.faux_transformers()

        class Modele(module.AutoModelForSequenceClassification):
            """Comme le double de la fondation, mais ne voit que les 512 premiers « jetons » (mots) de la paire."""
            def __call__(self, paires):
                return super().__call__([(' '.join(p.split()[:512 - len(h.split())]), h) for p, h in paires])
        module.AutoModelForSequenceClassification = Modele
        premisse = ' '.join('Le texte continue ici.' for _ in range(200)) + ' Paris est en France.'
        with TF.doubles(transformers=module, torch=TF.faux_torch()):
            resultat = moteurs.nli_lot([(premisse, 'Paris est en France'), ('A.', 'B')])
        self.assertEqual(resultat[0]['label'], 'implication')
        self.assertGreater(resultat[0].get('fenetres', 1), 1)
        self.assertEqual(resultat[1]['label'], 'neutre')
        self.assertNotIn('fenetres', resultat[1])


class MineurTroncaturesSignalees(T.Base):

    def test_encodage_des_pages_signale_les_troncatures(self):
        from chaine import m1_sujets
        seuils.definir('m1.encodage.page_caracteres', 100)
        s.add_content('https://' + T.SITE + '/longue', 'Longue', 'Titre\n\n' + 'Le taux du Livret A est de 3 %. ' * 20, '2025-01-01')
        s.add_content('https://' + T.SITE + '/courte', 'Courte', 'Titre\n\nLe taux est de 3 %.', '2025-01-01')
        lancer('m1.page', 'm1.unites')
        resultat = m1_sujets.encoder('pages')
        self.assertEqual(resultat['tronquees'], 1)
        run = s.query("SELECT note FROM step_runs WHERE step='m1.encodage.pages'", one=True)
        self.assertIn('tronquees=1', run['note'])

    def test_chapo_de_repli_seuil_et_fin_de_phrase(self):
        from chaine import m3
        self.assertIn('m3.centralite.chapo_caracteres', seuils.DEFAUTS)
        seuils.definir('m3.centralite.chapo_caracteres', 150)
        texte = 'Titre\n\n' + ' '.join('Phrase numéro %d du premier paragraphe de la page.' % i for i in range(10))
        v = s.add_content('https://' + T.SITE + '/chapo', 'Chapo', texte, '2025-01-01')
        lancer('m1.page')
        version = s.query('SELECT v.*, p.title FROM versions v JOIN pages p ON p.id=v.page_id WHERE v.id=?', (v,), True)
        s.query('UPDATE versions SET lede=NULL WHERE id=?', (v,))
        version['lede'] = None
        _, chapo, _ = m3._structure_page(version)
        self.assertLessEqual(len(chapo), 150)
        self.assertTrue(chapo.endswith('.'), chapo)


class MineurScoresDesRegles(T.Base):

    def test_scores_des_regles_reglables(self):
        indices = m1_page._indices('Jean Dupont dirige la société X.', {'present'}, [], '2025-06-01')
        self.assertEqual(m1_page._regles_classe(indices)[2], 'lexique:fonction_present')
        seuils.definir('m1.regle.lexique.fonction_present', 0.3)
        self.assertEqual(m1_page._regles_classe(indices)[1], 0.45)   # temps:present l'emporte desormais
        self.assertIn('m2.interpretation.bonus_connecteur', seuils.DEFAUTS)


if __name__ == '__main__':
    unittest.main()
