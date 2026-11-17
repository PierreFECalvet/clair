import json
import unittest
import threading
from urllib.request import Request,urlopen
from unittest.mock import patch
import test_pipeline

s=test_pipeline.s

class ScreeningTests(unittest.TestCase):
    setUp=test_pipeline.PipelineTests.setUp
    tearDown=test_pipeline.PipelineTests.tearDown
    claim=test_pipeline.PipelineTests.claim
    evidence=test_pipeline.PipelineTests.evidence

    def attach(self,claim,text=None):
        vid=s.add_content('texte:lot:article','Article technique',text or claim['text'])
        chunk=s.query('SELECT id FROM chunks WHERE version_id=?',(vid,),True)['id']
        s.query('INSERT INTO occurrences VALUES(?,?,?,?,?)',(s.uid(),claim['id'],chunk,claim['text'],'[]'))
        return vid

    def decision(self,route='search',q='Googlebot exécute-t-il JavaScript lors du rendu ?'):
        return {'route':route,'reason':'Choix contextualisé','standalone_claim':'Googlebot exécute JavaScript.',
                'search_query':q,'source_urls':[],'content_ids':[]}

    def verdict(self,status='validated',relation='supports',same=True):
        return {'status':status,'reason':'Preuve examinée','correction':'Correction du même fait' if status=='correct' else '',
                'same_subject':same,'relation':relation,'citations':[]}

    def test_local_title_never_calls_ibou_even_when_inconclusive(self):
        c=self.claim('Le deuxième contenu porte le titre « Entonnoir ».');self.attach(c)
        with patch.object(s,'llm',side_effect=[self.decision('local'),self.verdict('review','insufficient')]),patch.object(s,'ibou') as ibou,patch.object(s,'http_json') as network:
            s.verify(100)
            ibou.assert_not_called();network.assert_not_called()
        self.assertEqual(s.query('SELECT route FROM screenings',one=True)['route'],'local')
        self.assertEqual(s.estimate()['maximum'],0)

    def test_local_validation_uses_document_citation(self):
        c=self.claim('Le titre est « Entonnoir ».');vid=self.attach(c)
        def fake(task,payload):
            if payload.get('mode')=='screen':return self.decision('local')
            result=self.verdict();result['citations']=[{'evidence_id':'content:'+vid,'quote':c['text']}];return result
        with patch.object(s,'llm',side_effect=fake),patch.object(s,'ibou') as ibou:
            s.verify(0);ibou.assert_not_called()
        self.assertEqual(s.query('SELECT status FROM claims',one=True)['status'],'validated')

    def test_metadata_cannot_be_sent_to_search_by_bad_route(self):
        c=self.claim('Le texte entier contient 600 mots.');self.attach(c)
        with patch.object(s,'llm',side_effect=[self.decision(),self.verdict('review','insufficient')]),patch.object(s,'ibou') as ibou:
            s.verify(100);ibou.assert_not_called()
        self.assertEqual(s.query('SELECT route FROM screenings',one=True)['route'],'local')

    def test_cited_source_sufficient_zero_ibou_units(self):
        c=self.claim('Googlebot exécute JavaScript.');self.attach(c,c['text']+'\n\nSources\nGoogle documentation https://docs.example/js')
        def fake(task,payload):
            if payload.get('mode')=='screen':return self.decision('sources')
            result=self.verdict();result['citations']=[{'evidence_id':payload['evidence'][0]['id'],'quote':c['text']}];return result
        with patch.object(s,'llm',side_effect=fake),patch.object(s,'fetch_article_source',return_value={'status':'ok','text':c['text']}),patch.object(s,'ibou') as ibou,patch.object(s,'http_json') as network:
            s.verify(100);ibou.assert_not_called();network.assert_not_called()
        self.assertEqual(s.query('SELECT route FROM screenings',one=True)['route'],'sources')
        self.assertEqual(s.query('SELECT status FROM claims',one=True)['status'],'validated')

    def test_unavailable_source_and_zero_budget_stop_before_ibou(self):
        c=self.claim();self.attach(c,c['text']+'\n\nSources\nhttps://docs.example/a')
        with patch.object(s,'llm',return_value=self.decision('sources')),patch.object(s,'fetch_article_source',return_value={'status':'unavailable','text':'','error':'PDF indisponible'}),patch.object(s,'ibou') as ibou,patch.object(s,'http_json') as network:
            s.verify(0);ibou.assert_not_called();network.assert_not_called()
        self.assertEqual(s.estimate()['maximum'],3)
        result=json.loads(s.query('SELECT result FROM screenings',one=True)['result'])
        self.assertEqual(result['source_report'][0]['error'],'PDF indisponible')

    def test_ambiguous_or_nonfactual_claim_never_searches(self):
        for route in ('review','skip'):
            c=self.claim('Description ambiguë '+route)
            def fake(task,payload):
                if payload.get('mode')=='deduplicate':return {'matches':[{'id':c['id'],'same':False,'reason':'Éléments distincts'} for c in payload['candidates']]}
                return self.decision(route)
            with patch.object(s,'llm',side_effect=fake),patch.object(s,'ibou') as ibou:
                s.verify(100);ibou.assert_not_called()
        self.assertEqual(s.estimate()['search'],0)

    def test_missing_search_question_is_not_authorized(self):
        self.claim()
        with patch.object(s,'llm',return_value=self.decision(q='')),patch.object(s,'ibou') as ibou:
            s.verify(100);ibou.assert_not_called()
        self.assertEqual(s.query('SELECT route FROM screenings',one=True)['route'],'review')

    def test_only_one_search_no_page_escalation(self):
        self.claim()
        with patch.object(s,'llm',return_value=self.decision()),patch.object(s,'http_json',return_value=({'remaining':100},{})),patch.object(s,'ibou',return_value={'results':[]}) as ibou:
            s.verify(100)
        self.assertEqual(ibou.call_count,1);self.assertEqual(ibou.call_args.args[:2],('passage','Googlebot exécute-t-il JavaScript lors du rendu ?'))
        self.assertEqual(s.query('SELECT status FROM claims',one=True)['status'],'review')

    def test_screening_persists_and_does_not_repay_llm_on_resume(self):
        self.claim()
        with patch.object(s,'llm',return_value=self.decision('skip')) as llm:
            s.prescreen();s.init();s.prescreen();self.assertEqual(llm.call_count,1)

    def test_new_occurrence_invalidates_screening(self):
        c=self.claim()
        with patch.object(s,'llm',return_value=self.decision('skip')) as llm:
            s.prescreen();self.attach(c);s.prescreen();self.assertEqual(llm.call_count,2)

    def test_old_bad_verdict_rechecked_history_preserved(self):
        c=self.claim();s.save_verdict(c,{'status':'correct','reason':'Rolling Stones','correction':'Mauvais titre'})
        with patch.object(s,'llm',return_value=self.decision('review')):s.prescreen()
        self.assertEqual(s.query('SELECT status,correction FROM claims',one=True),{'status':'review','correction':''})
        self.assertEqual(len(s.query('SELECT * FROM checks')),2)

    def test_manual_verdict_preserved(self):
        c=self.claim();s.save_verdict(c,{'status':'validated','reason':'Décision humaine : preuve locale','correction':''})
        with patch.object(s,'llm') as llm:s.prescreen();llm.assert_not_called()

    def test_literal_quote_from_wrong_subject_cannot_correct(self):
        c=self.claim('Le deuxième article se nomme Entonnoir.');eid=self.evidence(c['id'],'Les Rolling Stones annoncent un concert.')
        result=self.verdict('correct','unrelated',False);result['citations']=[{'evidence_id':eid,'quote':'Les Rolling Stones annoncent un concert.'}]
        with patch.object(s,'llm',return_value=result):self.assertEqual(s.evaluate(c)['status'],'review')

    def test_absence_of_mention_cannot_be_a_correction(self):
        c=self.claim('Le contenu comporte 4762 mots.');eid=self.evidence(c['id'],'Lecture : 24 minutes.')
        result=self.verdict('correct','insufficient');result['citations']=[{'evidence_id':eid,'quote':'Lecture : 24 minutes.'}]
        with patch.object(s,'llm',return_value=result):self.assertEqual(s.evaluate(c)['correction'],'')

    def test_stale_correction_blocked_without_screening(self):
        c=self.claim();s.save_verdict(c,{'status':'correct','reason':'Ancien verdict','correction':'Faux remplacement'})
        with patch.object(s,'llm') as llm:s.correct();llm.assert_not_called()
        self.assertEqual(s.query('SELECT * FROM patches'),[])

    def test_batch_limit_leaves_other_claims_untouched(self):
        for i in range(3):self.claim('Fait '+str(i))
        with patch.object(s,'llm',return_value=self.decision('skip')) as llm:
            s.prescreen(2);self.assertEqual(llm.call_count,2)
            self.assertEqual(s.estimate()['unscreened'],1)
            s.prescreen(2);self.assertEqual(llm.call_count,3)

    def test_unresolved_ordinal_search_blocked(self):
        self.claim()
        with patch.object(s,'llm',return_value=self.decision(q='deuxième contenu titre SEO')),patch.object(s,'ibou') as ibou:
            s.verify(100);ibou.assert_not_called()
        self.assertEqual(s.query('SELECT route FROM screenings',one=True)['route'],'review')

    def test_http_interface_and_prescreen_endpoint(self):
        server=s.ThreadingHTTPServer(('127.0.0.1',0),s.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            for path in ('/','/screening.js','/review.js','/api/state'):
                with urlopen(base+path,timeout=5) as response:
                    self.assertEqual(response.status,200);body=response.read().decode()
                    if path=='/':self.assertIn('/screening.js',body)
                    if path=='/api/state':self.assertIn('unscreened',json.loads(body)['estimate'])
            s.JOB['status']='idle'
            request=Request(base+'/api/prescreen',data=b'{"limit":50}',headers={'Content-Type':'application/json','X-Session-Token':s.TOKEN})
            with patch.object(s,'start_job') as job:
                with urlopen(request,timeout=5) as response:self.assertEqual(response.status,200)
                self.assertEqual(job.call_args.args[0],'screening')
        finally:server.shutdown();server.server_close();thread.join()
