"""Tests du lot M2 : sources explicites, typologie des affirmations, source manquante."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server as s
import chaine
from chaine import lexiques
from chaine import m2
from chaine import moteurs
from chaine import noyau
from chaine import schema
from chaine import seuils

AUJOURDHUI = '2026-11-13'
URL_PAGE = 'https://pierrecalvet.fr/moteurs/rendu/'


class BaseM2(unittest.TestCase):
    """Base temporaire, moteurs a regles (deterministes), aucun appel reseau."""

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
        m2.vider_cache()
        s.CTX.step = None
        for nom in ('phrases', 'entites', 'classer'):
            noyau.definir_reglage('moteur:' + nom, 'regles')

    def tearDown(self):
        s.CONFIG.clear()
        s.CONFIG.update(self.config)
        s.STOP.clear()
        moteurs.reinitialiser()
        noyau.vider_reglages()
        seuils._vider_cache()
        m2.vider_cache()
        self.tmp.cleanup()

    # --- aides --------------------------------------------------------------

    def page(self, corps, url=URL_PAGE, titre='Le rendu JavaScript', publie='2026-03-02', xml=None, page_type=None, meta=None):
        """Cree une page et sa version ; le texte suit le format du corpus (titre, URL, corps)."""
        texte = titre + '\n\n' + url + '\n\n' + corps
        page_id = s.uid()
        version_id = s.uid()
        s.query('INSERT INTO pages(id,url,title) VALUES(?,?,?)', (page_id, 'texte:lot:' + page_id, 'slug-' + page_id))
        s.query('INSERT INTO versions(id,page_id,text,created,published,hash,xml,page_type,meta) VALUES(?,?,?,?,?,?,?,?,?)',
                (version_id, page_id, texte, s.now(), publie, page_id, xml, page_type, meta))
        return version_id

    def unite(self, version_id, citation, texte=None, classe='etat_courant', **extra):
        """Cree une unite dont le span est la citation dans le texte de la version."""
        corps = s.query('SELECT text FROM versions WHERE id=?', (version_id,), True)['text']
        debut = corps.index(citation)
        identifiant = s.uid()
        s.query('INSERT INTO units(id,version_id,text,quote,span_start,span_end,obs_class,status,extractor,created) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (identifiant, version_id, texte or citation, citation, debut, debut + len(citation), classe,
                 extra.get('status', 'a_verifier'), 'test', s.now()))
        return identifiant

    def ligne(self, unit_id):
        return s.query('SELECT * FROM units WHERE id=?', (unit_id,), True)

    def type(self, unit_id):
        return s.query('SELECT * FROM claim_types WHERE unit_id=?', (unit_id,), True)

    def sources(self, unit_id, contribution='m2'):
        return s.query('SELECT * FROM sources WHERE unit_id=? AND contribution=? ORDER BY scope, kind', (unit_id, contribution))

    def executer(self, version_id, **options):
        options.setdefault('avec_llm', False)
        return m2.executer([version_id], aujourdhui=AUJOURDHUI, **options)


class SourcesNommeesTests(BaseM2):
    def test_selon_l_insee_dans_la_phrase(self):
        v = self.page("Selon l'Insee, le taux de chômage s'est établi à 7,4 % au deuxième trimestre. Le texte s'arrête là.")
        u = self.unite(v, "Selon l'Insee, le taux de chômage s'est établi à 7,4 % au deuxième trimestre.")
        m2.sources_explicites(v)
        lignes = self.sources(u)
        self.assertEqual(len(lignes), 1)
        self.assertEqual(lignes[0]['kind'], 'source_nommee')
        self.assertEqual(lignes[0]['scope'], 'phrase')
        self.assertEqual(lignes[0]['cited_entity'], 'Insee')
        self.assertAlmostEqual(lignes[0]['confidence'], seuils.get('m2.confiance.phrase'))
        self.executer(v)
        self.assertEqual(self.ligne(u)['sourcing'], 'sourcee_externe')
        self.assertIsNone(self.type(u))

    def test_portee_phrase_puis_paragraphe(self):
        v = self.page("Selon l'Insee, le chômage atteint 7,4 %. Il a baissé de 0,2 point depuis janvier.\n\nUn autre paragraphe sans aucune source.")
        premiere = self.unite(v, "Selon l'Insee, le chômage atteint 7,4 %.")
        seconde = self.unite(v, 'Il a baissé de 0,2 point depuis janvier.')
        autre = self.unite(v, 'Un autre paragraphe sans aucune source.')
        m2.sources_explicites(v)
        a = self.sources(premiere)
        b = self.sources(seconde)
        self.assertEqual([x['scope'] for x in a], ['phrase'])
        self.assertEqual([x['scope'] for x in b], ['paragraphe'])
        self.assertAlmostEqual(b[0]['confidence'], seuils.get('m2.confiance.paragraphe'))
        self.assertLess(b[0]['confidence'], a[0]['confidence'])
        self.assertEqual(self.sources(autre), [])

    def test_verbe_d_attribution_et_sujet(self):
        v = self.page("L'Insee a annoncé que le chômage baissait. Nous le savons.")
        u = self.unite(v, "L'Insee a annoncé que le chômage baissait.")
        m2.sources_explicites(v)
        lignes = self.sources(u)
        self.assertEqual([(x['kind'], x['scope'], x['cited_entity']) for x in lignes], [('source_nommee', 'phrase', 'Insee')])

    def test_d_apres_adverbial_n_est_pas_une_attribution(self):
        v = self.page("Le brevet est suivi l'année d'après par un autre texte que Matt Cutts commente.")
        u = self.unite(v, "Le brevet est suivi l'année d'après par un autre texte que Matt Cutts commente.")
        m2.sources_explicites(v)
        self.assertEqual(self.sources(u), [])

    def test_le_nom_du_site_n_est_pas_une_source(self):
        v = self.page("Selon pierrecalvet.fr, le rendu est différé.")
        u = self.unite(v, 'Selon pierrecalvet.fr, le rendu est différé.')
        m2.sources_explicites(v)
        self.assertEqual([x for x in self.sources(u) if x['kind'] == 'source_nommee'], [])

    def test_marqueur_sans_entite_ne_donne_rien(self):
        v = self.page("Selon la documentation, le rendu est différé. D'après cela, tout est dit.")
        u = self.unite(v, 'Selon la documentation, le rendu est différé.')
        m2.sources_explicites(v)
        self.assertEqual(self.sources(u), [])


class LiensTests(BaseM2):
    def test_lien_interne_et_externe_et_page_elle_meme(self):
        corps = ("Le rendu est décrit par Google https://developers.google.com/search/docs/rendu pour les pages dynamiques.\n\n"
                 "La série détaille le crawl, voir https://pierrecalvet.fr/moteurs/crawl/ pour le détail.\n\n"
                 "Cette phrase renvoie à elle-même (https://pierrecalvet.fr/moteurs/rendu/) sans autre source.")
        v = self.page(corps)
        externe = self.unite(v, 'Le rendu est décrit par Google https://developers.google.com/search/docs/rendu pour les pages dynamiques.')
        interne = self.unite(v, 'La série détaille le crawl, voir https://pierrecalvet.fr/moteurs/crawl/ pour le détail.')
        soi = self.unite(v, 'Cette phrase renvoie à elle-même (https://pierrecalvet.fr/moteurs/rendu/) sans autre source.')
        m2.sources_explicites(v)
        self.assertIn(('lien_externe', 'phrase'), [(x['kind'], x['scope']) for x in self.sources(externe)])
        self.assertEqual([(x['kind'], x['scope'], x['domain']) for x in self.sources(interne)],
                         [('lien_interne', 'phrase', 'pierrecalvet.fr')])
        self.assertEqual(self.sources(soi), [])
        self.executer(v)
        self.assertEqual(self.ligne(externe)['sourcing'], 'sourcee_externe')
        self.assertEqual(self.ligne(interne)['sourcing'], 'sourcee_interne')
        self.assertEqual(self.ligne(soi)['sourcing'], 'sans_source')

    def test_lien_au_paragraphe(self):
        v = self.page("Le robot lit le HTML avant le rendu. La source est ici : https://developers.google.com/search/docs/crawling-indexing/javascript")
        u = self.unite(v, 'Le robot lit le HTML avant le rendu.')
        m2.sources_explicites(v)
        lignes = self.sources(u)
        self.assertEqual([(x['kind'], x['scope']) for x in lignes], [('lien_externe', 'paragraphe')])

    def test_ref_target_du_xml_trafilatura(self):
        xml = ('<doc><main><p>Le rapport <ref target="https://www.insee.fr/fr/statistiques/123">annuel de l\'Insee</ref> '
               'et la <ref target="/moteurs/crawl/">page sur le crawl</ref> décrivent la méthode.</p></main></doc>')
        v = self.page("Le rapport annuel de l'Insee et la page sur le crawl décrivent la méthode.", xml=xml)
        u = self.unite(v, "Le rapport annuel de l'Insee et la page sur le crawl décrivent la méthode.")
        m2.sources_explicites(v)
        genres = sorted((x['kind'], x['domain']) for x in self.sources(u) if x['url'])
        self.assertEqual(genres, [('lien_externe', 'insee.fr'), ('lien_interne', 'pierrecalvet.fr')])

    def test_lignes_migrees_reclassees_avec_le_site(self):
        """La migration ne connait pas le site : M2 reclasse le domaine et ignore le lien vers la page elle-meme."""
        v = self.page("Phrase un sans lien dans le texte.\n\nPhrase deux sans lien dans le texte.")
        a = self.unite(v, 'Phrase un sans lien dans le texte.')
        b = self.unite(v, 'Phrase deux sans lien dans le texte.')
        for unite, url in ((a, 'https://pierrecalvet.fr/moteurs/crawl/'), (b, URL_PAGE)):
            s.query('INSERT INTO sources(id,unit_id,url,domain,kind,scope,confidence,contribution) VALUES(?,?,?,?,?,?,?,?)',
                    (s.uid(), unite, url, 'pierrecalvet.fr', 'lien_externe', 'paragraphe', 0.6, 'migration'))
        self.executer(v)
        self.assertEqual(self.ligne(a)['sourcing'], 'sourcee_interne')
        self.assertEqual(self.ligne(b)['sourcing'], 'sans_source')
        self.assertEqual(len(self.sources(a, 'migration')), 1)


class AppareilDeCitationTests(BaseM2):
    CORPS = ("Googlebot exécute le JavaScript des pages [1] et conserve le résultat dans son cache.\n\n"
             "Le délai médian de rendu est de dix secondes [Documenté : Vercel, juillet 2024] selon la mesure de 2024.\n\n"
             "Le graphique montre le délai par centile.\nSource : MERJ et Vercel, 2024\n\n"
             "Une phrase du corps qui cite Cloudflare sans lien.\n\n"
             "Sources\n\n1. Google Search Central, « JavaScript SEO basics », documentation. https://developers.google.com/search/docs/js\n"
             "2. Cloudflare, « Perplexity is using stealth crawlers », août 2025. https://blog.cloudflare.com/perplexity\n")

    def test_note_numerotee_resolue_dans_le_catalogue(self):
        v = self.page(self.CORPS)
        u = self.unite(v, 'Googlebot exécute le JavaScript des pages [1] et conserve le résultat dans son cache.')
        m2.sources_explicites(v)
        lignes = [x for x in self.sources(u) if x['kind'] == 'note']
        self.assertEqual(len(lignes), 1)
        self.assertEqual(lignes[0]['scope'], 'phrase')
        self.assertEqual(lignes[0]['domain'], 'developers.google.com')
        self.assertEqual(lignes[0]['cited_entity'], 'Google Search Central')

    def test_marqueur_epistemique_avec_source(self):
        v = self.page(self.CORPS)
        u = self.unite(v, 'Le délai médian de rendu est de dix secondes [Documenté : Vercel, juillet 2024] selon la mesure de 2024.')
        m2.sources_explicites(v)
        lignes = self.sources(u)
        self.assertIn(('note', 'phrase', 'Vercel, juillet 2024'), [(x['kind'], x['scope'], x['cited_entity']) for x in lignes])

    def test_ligne_source_de_legende_rattachee_au_paragraphe(self):
        v = self.page(self.CORPS)
        u = self.unite(v, 'Le graphique montre le délai par centile.')
        m2.sources_explicites(v)
        lignes = [x for x in self.sources(u) if x['kind'] == 'note']
        self.assertEqual([(x['scope'], x['cited_entity']) for x in lignes], [('paragraphe', 'MERJ et Vercel, 2024')])

    def test_catalogue_rapproche_a_la_page_sans_sourcer(self):
        v = self.page(self.CORPS)
        u = self.unite(v, 'Une phrase du corps qui cite Cloudflare sans lien.')
        m2.sources_explicites(v)
        lignes = self.sources(u)
        self.assertEqual([(x['scope'], x['cited_entity']) for x in lignes], [('page', 'Cloudflare')])
        self.assertLess(lignes[0]['confidence'], seuils.get('m2.confiance_min'))
        self.executer(v)
        self.assertEqual(self.ligne(u)['sourcing'], 'sans_source')

    def test_encadre_sources_est_un_catalogue_pas_une_affirmation_sourcee_par_les_autres(self):
        v = self.page(self.CORPS)
        entree = self.unite(v, 'Cloudflare, « Perplexity is using stealth crawlers », août 2025. https://blog.cloudflare.com/perplexity')
        m2.sources_explicites(v)
        self.assertEqual([(x['kind'], x['scope']) for x in self.sources(entree)], [('lien_externe', 'phrase')])


class TypologieTests(BaseM2):
    def typer_un(self, corps, citation, texte=None, **options):
        v = self.page(corps, **options.pop('page', {}))
        u = self.unite(v, citation, texte=texte, **options)
        self.executer(v)
        return u

    def test_information_source_premiere_personne_methodologique(self):
        u = self.typer_un("Nous avons interrogé 1 200 responsables de site entre mars et mai. Le reste suit.",
                          'Nous avons interrogé 1 200 responsables de site entre mars et mai.')
        t = self.type(u)
        self.assertEqual(t['type'], 'information_source')
        self.assertTrue(t['rule'].startswith('m2.info_source.'))
        ligne = self.ligne(u)
        self.assertEqual(ligne['sourcing'], 'auto_sourcee')
        self.assertEqual(ligne['sourcing_rule'], t['rule'])
        self.assertEqual(ligne['status'], 'sourcee')
        # datee par la date de publication
        self.assertEqual((ligne['anchor_date'], ligne['anchor_kind']), ('2026-03-02', 'publication'))

    def test_information_source_chiffres_de_l_auteur(self):
        u = self.typer_un("Dans notre échantillon, 38 % des pages sont rendues en moins de dix secondes.",
                          'Dans notre échantillon, 38 % des pages sont rendues en moins de dix secondes.')
        self.assertEqual(self.type(u)['rule'], 'm2.info_source.chiffres_auteur')

    def test_information_source_par_type_de_page_schema_org(self):
        meta = json.dumps({'@type': 'ScholarlyArticle', 'headline': 'Une étude'})
        u = self.typer_un("Le taux de rendu atteint 91 % sur les pages mesurées.", 'Le taux de rendu atteint 91 % sur les pages mesurées.',
                          page={'meta': meta})
        t = self.type(u)
        self.assertEqual(t['type'], 'information_source')
        self.assertEqual(t['rule'], 'm2.info_source.page_type:etude')
        self.assertEqual(s.query('SELECT page_type FROM versions', one=True)['page_type'], 'etude')

    def test_type_de_page_par_rubrique_dans_l_url(self):
        u = self.typer_un("Le taux de rendu atteint 91 % sur les pages mesurées.", 'Le taux de rendu atteint 91 % sur les pages mesurées.',
                          page={'url': 'https://exemple.fr/etudes/rendu-2026/'})
        self.assertEqual(self.type(u)['rule'], 'm2.info_source.page_type:etude')

    def test_type_de_page_non_exige_si_reglage_page_seule_faux(self):
        noyau.definir_reglage('seuil:m2.information_source.page_seule', 'false')
        seuils._vider_cache()
        meta = json.dumps({'@type': 'Report'})
        u = self.typer_un("Le taux de rendu atteint 91 % sur les pages mesurées.", 'Le taux de rendu atteint 91 % sur les pages mesurées.',
                          page={'meta': meta})
        self.assertEqual(self.type(u)['type'], 'sans_source')

    def test_page_de_classifieur_de_repli_ne_decide_jamais_seule(self):
        u = self.typer_un("Le taux de rendu atteint 91 % sur les pages mesurées.", 'Le taux de rendu atteint 91 % sur les pages mesurées.',
                          page={'titre': 'Enquête 2026 : résultats de l\'étude auprès de 1 200 professionnels'})
        self.assertEqual(self.type(u)['type'], 'sans_source')
        self.assertIsNone(s.query('SELECT page_type FROM versions', one=True)['page_type'])

    def test_opinion_modalites_evaluatives(self):
        u = self.typer_un("À mon avis, il faut revoir entièrement cette approche, qui est franchement décevante.",
                          'À mon avis, il faut revoir entièrement cette approche, qui est franchement décevante.')
        t = self.type(u)
        self.assertEqual(t['type'], 'opinion')
        self.assertEqual(self.ligne(u)['sourcing'], 'opinion')
        self.assertEqual(self.ligne(u)['status'], 'non_verifiable')

    def test_opinion_conditionnel_de_souhait_et_adjectif(self):
        u = self.typer_un("On aimerait que les moteurs soient plus transparents, ce serait vraiment formidable.",
                          'On aimerait que les moteurs soient plus transparents, ce serait vraiment formidable.')
        self.assertEqual(self.type(u)['type'], 'opinion')

    def test_il_faut_seul_ne_suffit_pas(self):
        u = self.typer_un("Pour retirer une page de l'index, il faut un noindex sur une page laissée accessible.",
                          "Pour retirer une page de l'index, il faut un noindex sur une page laissée accessible.")
        self.assertEqual(self.type(u)['type'], 'sans_source')

    def test_opinion_par_rubrique_tribune(self):
        v = self.page("Les moteurs de recherche changent de visage.\n\nLe classement a évolué en 2025 selon 12 % des sondés.",
                      url='https://exemple.fr/tribunes/avenir-du-seo/')
        simple = self.unite(v, 'Les moteurs de recherche changent de visage.')
        chiffree = self.unite(v, 'Le classement a évolué en 2025 selon 12 % des sondés.')
        self.executer(v)
        self.assertEqual(self.type(simple)['rule'], 'm2.opinion.page:tribune')
        self.assertNotEqual(self.ligne(chiffree)['sourcing'], 'opinion')

    def test_interpretation_connecteur_et_nli_confirme(self):
        corps = ("Le robot lit le HTML avant tout rendu [Confirmé : Google, doc]. Les liens du HTML servi sont lus avant le rendu [Confirmé : Google, doc].\n\n"
                 "Le rendu ne change donc pas la découverte des liens présents dans le HTML servi.")
        v = self.page(corps)
        a = self.unite(v, 'Le robot lit le HTML avant tout rendu [Confirmé : Google, doc].')
        b = self.unite(v, 'Les liens du HTML servi sont lus avant le rendu [Confirmé : Google, doc].')
        c = self.unite(v, 'Le rendu ne change donc pas la découverte des liens présents dans le HTML servi.')

        def faux_nli(paires):
            return [{'label': 'implication', 'score': 0.93, 'engine': 'test', 'probas': {}} for _ in paires]
        with patch.object(moteurs, 'nli_lot', side_effect=faux_nli) as nli:
            with patch.object(noyau, 'llm', side_effect=AssertionError('pas de LLM direct')):
                m2.executer([v], aujourdhui=AUJOURDHUI, avec_llm=True)
        self.assertTrue(nli.called)
        t = self.type(c)
        self.assertEqual(t['type'], 'interpretation')
        self.assertEqual(t['rule'], 'm2.interpretation.nli')
        self.assertEqual(sorted(json.loads(t['depends_on'])), sorted([a, b]))
        self.assertEqual(self.ligne(c)['sourcing'], 'dependante')
        self.assertEqual(self.ligne(c)['status'], 'dependante')

    def test_interpretation_connecteur_sans_nli(self):
        u = self.typer_un("Le cache ne relit pas les en-têtes, ce qui montre que l'ancien script peut rester actif.",
                          "Le cache ne relit pas les en-têtes, ce qui montre que l'ancien script peut rester actif.")
        t = self.type(u)
        # Aucune autre unite dont dependre : une « interpretation » ne suivrait aucun statut et ne serait jamais
        # verifiee ; l'unite reste dans la cascade (relecture, mineur « depends_on vide »).
        self.assertNotEqual(t['type'], 'interpretation')
        v = self.page("Le cache ne relit pas les en-têtes.\n\nCela montre que l'ancien script peut rester actif.")
        premiere = self.unite(v, 'Le cache ne relit pas les en-têtes.')
        seconde = self.unite(v, "Cela montre que l'ancien script peut rester actif.")
        m2.executer([v], aujourdhui=AUJOURDHUI)
        t = self.type(seconde)
        self.assertEqual(t['type'], 'interpretation')
        self.assertEqual(t['rule'], 'm2.interpretation.connecteur')
        self.assertEqual(json.loads(t['depends_on']), [premiere])

    def test_nli_non_confirme_decourage_un_connecteur_faible(self):
        corps = ("Le robot lit le HTML avant tout rendu [Confirmé : Google, doc]. Les liens sont lus avant le rendu [Confirmé : Google, doc].\n\n"
                 "Le site est donc rapide sur le HTML des liens.")
        v = self.page(corps)
        self.unite(v, 'Le robot lit le HTML avant tout rendu [Confirmé : Google, doc].')
        self.unite(v, 'Les liens sont lus avant le rendu [Confirmé : Google, doc].')
        c = self.unite(v, 'Le site est donc rapide sur le HTML des liens.')
        neutre = lambda paires: [{'label': 'neutre', 'score': 0.9, 'engine': 'test', 'probas': {}} for _ in paires]
        with patch.object(moteurs, 'nli_lot', side_effect=neutre):
            m2.executer([v], aujourdhui=AUJOURDHUI, avec_llm=True)
        self.assertEqual(self.type(c)['type'], 'sans_source')

    def test_borne_du_test_nli(self):
        phrases = ['Le robot lit le HTML numero %d avant tout rendu des liens [Confirmé : Google, doc].' % i for i in range(12)]
        corps = ' '.join(phrases) + '\n\nLe rendu des liens du HTML ne change donc pas le robot lisant le HTML avant tout rendu.'
        v = self.page(corps)
        for phrase in phrases:
            self.unite(v, phrase)
        cible = self.unite(v, 'Le rendu des liens du HTML ne change donc pas le robot lisant le HTML avant tout rendu.')
        recu = []

        def espion(paires):
            recu.extend(paires)
            return [{'label': 'neutre', 'score': 0.5, 'engine': 'test', 'probas': {}} for _ in paires]
        with patch.object(moteurs, 'nli_lot', side_effect=espion):
            m2.executer([v], aujourdhui=AUJOURDHUI, avec_llm=True)
        self.assertLessEqual(len(recu), int(seuils.get('m2.interpretation.paires_max')))
        self.assertGreater(len(recu), 0)
        self.assertIsNotNone(self.ligne(cible)['sourcing'])

    def test_connaissance_generale_par_regles(self):
        u = self.typer_un("Un moteur de recherche est un logiciel qui indexe des pages et répond à des requêtes.",
                          'Un moteur de recherche est un logiciel qui indexe des pages et répond à des requêtes.')
        t = self.type(u)
        self.assertEqual(t['type'], 'connaissance_generale')
        self.assertEqual(t['rule'], 'm2.generale.regles')
        self.assertEqual(self.ligne(u)['sourcing'], 'connaissance_generale')
        self.assertEqual(self.ligne(u)['status'], 'a_verifier_faible')

    def test_cas_ambigu_la_poule_tranche_par_le_llm_simule(self):
        v = self.page("La poule pond des œufs.")
        u = self.unite(v, 'La poule pond des œufs.')
        appels = []

        def faux_llm(tache, payload):
            appels.append((tache, payload))
            return {'resultats': [{'id': item['id'], 'general': True, 'confiance': 0.9} for item in payload['items']]}
        with patch.object(noyau, 'llm', side_effect=faux_llm):
            rapport = m2.executer([v], aujourdhui=AUJOURDHUI, avec_llm=True)
        self.assertEqual(len(appels), 1)
        payload = appels[0][1]
        self.assertEqual(payload['mode'], 'chaine:m2.generale')
        self.assertEqual(payload['question'], lexiques.QUESTION_FERMEE)
        self.assertEqual(payload['items'], [{'id': 0, 'texte': 'La poule pond des œufs.'}])
        t = self.type(u)
        self.assertEqual((t['type'], t['rule']), ('connaissance_generale', 'm2.generale.llm'))
        self.assertEqual(rapport['llm_appels'], 1)
        self.assertEqual(rapport['par_sourcing'].get('connaissance_generale'), 1)

    def test_cas_ambigu_llm_dit_non(self):
        v = self.page("La poule pond des œufs.")
        u = self.unite(v, 'La poule pond des œufs.')
        faux = lambda tache, payload: {'resultats': [{'id': i['id'], 'general': False, 'confiance': 0.8} for i in payload['items']]}
        with patch.object(noyau, 'llm', side_effect=faux):
            m2.executer([v], aujourdhui=AUJOURDHUI, avec_llm=True)
        t = self.type(u)
        self.assertEqual((t['type'], t['rule']), ('sans_source', 'm2.sans_source.llm_non_general'))

    def test_pas_de_llm_si_interdit_ou_si_specifique(self):
        v = self.page("La poule pond des œufs.\n\nLa poule pond 300 œufs par an depuis 2024.")
        a = self.unite(v, 'La poule pond des œufs.')
        b = self.unite(v, 'La poule pond 300 œufs par an depuis 2024.')
        with patch.object(noyau, 'llm', side_effect=AssertionError('LLM interdit')):
            m2.executer([v], aujourdhui=AUJOURDHUI, avec_llm=False)
        self.assertEqual(self.type(a)['type'], 'sans_source')
        self.assertEqual(self.type(b)['type'], 'sans_source')
        # avec LLM autorise, la phrase chiffree et datee n'est jamais soumise (veto des specificites)
        soumis = []

        def faux(tache, payload):
            soumis.extend(i['texte'] for i in payload['items'])
            return {'resultats': [{'id': i['id'], 'general': True, 'confiance': 0.99} for i in payload['items']]}
        with patch.object(noyau, 'llm', side_effect=faux):
            m2.executer([v], aujourdhui=AUJOURDHUI, avec_llm=True, forcer=True)
        self.assertEqual(soumis, ['La poule pond des œufs.'])
        self.assertEqual(self.type(b)['type'], 'sans_source')

    def test_llm_en_echec_retombe_sur_sans_source(self):
        v = self.page("La poule pond des œufs.")
        u = self.unite(v, 'La poule pond des œufs.')
        with patch.object(noyau, 'llm', side_effect=s.OperationalError('modele absent')):
            rapport = m2.executer([v], aujourdhui=AUJOURDHUI, avec_llm=True)
        self.assertEqual(self.type(u)['type'], 'sans_source')
        self.assertEqual(rapport['llm_echecs'], 1)

    def test_sans_source_par_defaut_etat_courant(self):
        u = self.typer_un("Le périphérique parisien est limité à 50 km/h depuis août 2026.",
                          'Le périphérique parisien est limité à 50 km/h depuis août 2026.')
        t = self.type(u)
        self.assertEqual(t['type'], 'sans_source')
        self.assertEqual(t['rule'], 'm2.sans_source.defaut')
        self.assertGreater(t['confidence'], seuils.get('m2.sans_source.confiance_base'))
        self.assertEqual(self.ligne(u)['sourcing'], 'sans_source')

    def test_cascade_dans_l_ordre_la_source_prime_sur_le_type(self):
        u = self.typer_un("Selon l'Insee, il faut se méfier de ce chiffre franchement décevant, donc 7,4 % est trompeur.",
                          "Selon l'Insee, il faut se méfier de ce chiffre franchement décevant, donc 7,4 % est trompeur.")
        self.assertEqual(self.ligne(u)['sourcing'], 'sourcee_externe')
        self.assertIsNone(self.type(u))

    def test_information_source_prime_sur_opinion(self):
        u = self.typer_un("Nous avons testé ces pages et nous trouvons les résultats franchement décevants.",
                          'Nous avons testé ces pages et nous trouvons les résultats franchement décevants.')
        self.assertEqual(self.type(u)['type'], 'information_source')

    def test_statut_existant_avec_verdict_protege(self):
        v = self.page("À mon avis, il faut revoir entièrement cette approche franchement décevante.")
        u = self.unite(v, 'À mon avis, il faut revoir entièrement cette approche franchement décevante.', status='sourcee')
        s.query("UPDATE units SET verdict='confirmee' WHERE id=?", (u,))
        self.executer(v)
        self.assertEqual(self.ligne(u)['status'], 'sourcee')
        self.assertEqual(self.ligne(u)['sourcing'], 'opinion')

    def test_unites_non_obsolescibles_ignorees(self):
        v = self.page("Le rendu existe depuis longtemps.\n\nLe rendu n'a pas de date.")
        immuable = self.unite(v, 'Le rendu existe depuis longtemps.', classe='immuable')
        inconnu = self.unite(v, "Le rendu n'a pas de date.", classe='inconnu')
        self.executer(v)
        self.assertIsNone(self.ligne(immuable)['sourcing'])
        self.assertIsNotNone(self.ligne(inconnu)['sourcing'])


class SourceManquanteTests(BaseM2):
    def groupe(self, *unites):
        groupe = s.uid()
        s.query('INSERT INTO unit_groups(id,label,created) VALUES(?,?,?)', (groupe, 'g', s.now()))
        for unite in unites:
            s.query('INSERT INTO unit_group_members(group_id,unit_id) VALUES(?,?)', (groupe, unite))

    def test_drapeau_source_manquante(self):
        phrase = "Le périphérique parisien est limité à 50 km/h depuis août 2026."
        va = self.page(phrase, url='https://pierrecalvet.fr/a/')
        vb = self.page(phrase + " Selon la Ville de Paris, c'est officiel.", url='https://pierrecalvet.fr/b/')
        vc = self.page(phrase, url='https://pierrecalvet.fr/c/')
        a = self.unite(va, phrase)
        b = self.unite(vb, phrase)
        c = self.unite(vc, phrase)
        self.groupe(a, b, c)
        rapport = m2.executer(None, aujourdhui=AUJOURDHUI, avec_llm=False)
        self.assertEqual(self.ligne(a)['sourcing'], 'sans_source')
        self.assertEqual(self.ligne(a)['source_manquante'], 1)
        self.assertEqual(self.ligne(c)['source_manquante'], 1)
        self.assertEqual(rapport['manquantes']['signalees'], 2)
        # b n'est pas sans source : pas de drapeau
        self.assertEqual(self.ligne(b)['source_manquante'], 0)

    def test_equivalent_sur_la_meme_page_ne_signale_pas(self):
        v = self.page("Phrase jumelle un sans source.\n\nPhrase jumelle deux sans source.")
        a = self.unite(v, 'Phrase jumelle un sans source.')
        b = self.unite(v, 'Phrase jumelle deux sans source.')
        self.groupe(a, b)
        self.executer(v)
        self.assertEqual(self.ligne(a)['source_manquante'], 0)
        self.assertEqual(self.ligne(b)['source_manquante'], 0)

    def test_drapeau_remis_a_zero_si_le_groupe_disparait(self):
        phrase = 'Le seuil vaut quarante pour cent en 2026.'
        va = self.page(phrase, url='https://pierrecalvet.fr/a/')
        vb = self.page(phrase, url='https://pierrecalvet.fr/b/')
        a = self.unite(va, phrase)
        b = self.unite(vb, phrase)
        self.groupe(a, b)
        m2.executer(None, aujourdhui=AUJOURDHUI, avec_llm=False)
        self.assertEqual(self.ligne(a)['source_manquante'], 1)
        s.query('DELETE FROM unit_group_members')
        m2.signaler_manquantes()
        self.assertEqual(self.ligne(a)['source_manquante'], 0)


class ExecutionTests(BaseM2):
    def test_executer_reprise_forcer_et_journal(self):
        v = self.page("Selon l'Insee, le chômage atteint 7,4 %.\n\nLe périphérique est limité à 50 km/h depuis août 2026.")
        a = self.unite(v, "Selon l'Insee, le chômage atteint 7,4 %.")
        b = self.unite(v, 'Le périphérique est limité à 50 km/h depuis août 2026.')
        premier = self.executer(v)
        self.assertEqual(premier['unites'], 2)
        self.assertEqual(premier['par_sourcing'], {'sourcee_externe': 1, 'sans_source': 1})
        run = s.query("SELECT * FROM step_runs WHERE step='m2'", one=True)
        self.assertEqual(run['status'], 'terminee')
        self.assertEqual(run['pages'], 1)
        self.assertEqual(self.type(b)['run_id'], run['id'])
        self.assertEqual(self.sources(a)[0]['run_id'], run['id'])
        deuxieme = self.executer(v)
        self.assertEqual(deuxieme['unites'], 0)
        self.assertEqual(deuxieme['sources'], 0)
        troisieme = self.executer(v, forcer=True)
        self.assertEqual(troisieme['unites'], 2)
        self.assertEqual(len(self.sources(a)), 1)
        self.assertEqual(s.JOB['stage'], 'm2.manquantes')

    def test_typer_calcule_les_sources_manquantes_et_est_idempotent(self):
        v = self.page("Selon l'Insee, le chômage atteint 7,4 %.")
        u = self.unite(v, "Selon l'Insee, le chômage atteint 7,4 %.")
        m2.typer([u], avec_llm=False)
        m2.typer([u], avec_llm=False)
        self.assertEqual(self.ligne(u)['sourcing'], 'sourcee_externe')
        self.assertEqual(len(self.sources(u)), 1)

    def test_arret_demande_interrompt_proprement(self):
        v = self.page("Phrase une sans source.\n\nPhrase deux sans source.")
        self.unite(v, 'Phrase une sans source.')
        s.STOP.set()
        with self.assertRaises(s.OperationalError):
            self.executer(v)
        s.STOP.clear()
        self.assertEqual(s.query("SELECT status FROM step_runs WHERE step='m2'", one=True)['status'], 'interrompue')

    def test_aucune_unite_ne_leve_pas(self):
        rapport = m2.executer(None, avec_llm=False)
        self.assertEqual(rapport['versions'], 0)
        self.assertEqual(rapport['manquantes']['signalees'], 0)

    def test_lexiques_a_plat_conservent_les_offsets(self):
        texte = "Selon l’Insee : « œuvre » Éléphant ÉCOLE !"
        self.assertEqual(len(lexiques.plat(texte)), len(texte))
        self.assertTrue(lexiques.plat(texte).startswith("selon l'insee"))


if __name__ == '__main__':
    unittest.main()
