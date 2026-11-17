"""Suivi des étapes, journal des appels API et préparation des requêtes Ibou. API simulées."""
import json
import threading
import unittest
from urllib.request import Request,urlopen
from unittest.mock import patch
import test_pipeline
import test_screening

s=test_pipeline.s

class SuiviTests(unittest.TestCase):
    setUp=test_pipeline.PipelineTests.setUp
    tearDown=test_pipeline.PipelineTests.tearDown
    claim=test_pipeline.PipelineTests.claim
    attach=test_screening.ScreeningTests.attach
    decision=test_screening.ScreeningTests.decision
    verdict=test_screening.ScreeningTests.verdict

    def openai(self,payload=None,usage=None):
        return ({'status':'completed','usage':usage or {'input_tokens':120,'output_tokens':8},
                 'output':[{'content':[{'type':'output_text','text':json.dumps(payload or {'ok':True})}]}]},{})

    def test_llm_call_is_logged_with_tokens_and_without_key(self):
        s.CONFIG.update(provider='openai',openai_key='sk-secret-value-123456')
        with patch.object(s,'http_json',return_value=self.openai()):
            with s.scope('claim-1'):s.llm('Test',{'test':'x','note':'Bearer sk-secret-value-123456'})
        row=s.query('SELECT * FROM api_log',one=True)
        self.assertEqual((row['service'],row['operation'],row['status'],row['claim_id']),('openai','test','success','claim-1'))
        self.assertEqual((row['tokens_in'],row['tokens_out']),(120,8))
        self.assertNotIn('sk-secret-value-123456',row['request']);self.assertIsNotNone(row['duration_ms'])

    def test_llm_error_is_logged_and_still_raised(self):
        s.CONFIG.update(provider='openai',openai_key='k')
        error=s.OperationalError('OpenAI — HTTP 429 : limite');error.code=429
        with patch.object(s,'http_json',side_effect=error):
            with self.assertRaises(s.OperationalError):s.llm('Test',{'test':'x'})
        row=s.query('SELECT status,http_status,error FROM api_log',one=True)
        self.assertEqual((row['status'],row['http_status']),('error',429));self.assertIn('429',row['error'])

    def test_ibou_success_cache_and_failure_are_all_in_history(self):
        c=self.claim();s.query('INSERT INTO budgets VALUES(?,?,?,?)',('b',6,0,s.now()))
        answer={'results':[{'url':'https://source.example/a','title':'A','passage':{'chunk_id':1,'text':'Googlebot exécute JavaScript lors du rendu.'}}]}
        with patch.object(s,'http_json',return_value=(answer,{})):
            with s.scope(c['id']):s.ibou('passage','Googlebot rendu JavaScript','b');s.ibou('passage','Googlebot rendu JavaScript','b')
        with patch.object(s,'http_json',side_effect=s.OperationalError('timeout')):
            with self.assertRaises(s.OperationalError):s.ibou('passage','autre requête','b')
        call=s.query("SELECT * FROM calls WHERE status='success'",one=True)
        self.assertEqual((call['claim_id'],call['budget_id'],call['results']),(c['id'],'b',1))
        logs=s.query("SELECT status,units FROM api_log WHERE service='ibou' ORDER BY created,rowid")
        self.assertEqual([(l['status'],l['units']) for l in logs],[('success',3),('cache',0),('uncertain',3)])
        listing=s.history({'kind':['ibou']})
        self.assertEqual(listing['total'],3)
        hit=next(r for r in listing['rows'] if r['status']=='success')
        self.assertEqual((hit['result_count'],hit['claim_text'],hit['hosts']),(1,c['text'],['source.example']))
        detail=s.history_detail(hit['id'])
        self.assertEqual(detail['ibou']['results'][0]['host'],'source.example');self.assertEqual(detail['claim']['id'],c['id'])

    def test_old_calls_are_backfilled_once(self):
        s.query("INSERT INTO calls(id,endpoint,query,units,status,response,created) VALUES('old1','passage','ancienne requête',3,'success','{}',?)",(s.now(),))
        s.init();s.init()
        rows=s.query("SELECT request,call_id FROM api_log WHERE call_id='old1'")
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['request'],'ancienne requête')

    QUESTION='Googlebot exécute-t-il JavaScript lors du rendu des pages ?'

    def legacy(self,sources_ok=True,long_source=False):
        """Décision telle que l’ancien pipeline l’enregistrait : revue, sans requête, motif trompeur."""
        c=self.claim('Googlebot exécute JavaScript.');self.attach(c)
        with patch.object(s,'llm',return_value=self.decision('review')):s.prescreen()
        url='https://docs.example/js'
        s.query('INSERT OR REPLACE INTO source_fetches VALUES(?,?,?,?,?)',(url,'x'*(30000 if long_source else 500),'ok','',s.now()))
        result={'status':'review','reason':'r','standalone_claim':'Googlebot exécute JavaScript.','source_report':[{'url':url,'status':'ok' if sources_ok else 'unavailable','error':''}]}
        s.query("UPDATE screenings SET search_query='',reason='Fait externe. Sujet ou requête non résolu : aucune recherche autorisée.',result=?",(s.dump(result),))
        s.query("UPDATE claims SET status='review',reason='Fait externe. Sujet ou requête non résolu : aucune recherche autorisée.'")
        return c

    def test_legacy_rows_get_exact_blocker(self):
        self.legacy();self.assertEqual(s.state()['claims'][0]['blocker'],'no_query')
        self.assertEqual(s.progress()['screening']['blockers'],{'no_query':1})
        s.query("UPDATE screenings SET result=replace(result,'\"status\": \"ok\"','\"status\": \"unavailable\"')")
        self.assertEqual(s.state()['claims'][0]['blocker'],'sources_down')

    def test_query_rules(self):
        claim='Googlebot exécute JavaScript.'
        self.assertEqual(s.query_problems(self.QUESTION,claim),[])
        self.assertTrue(s.query_problems('Googlebot rendu JavaScript documentation',claim))
        self.assertTrue(s.query_problems('Googlebot exécute JavaScript ?',claim))
        self.assertTrue(s.query_problems('Quel est le titre du deuxième contenu de la série ?',claim))
        self.assertTrue(s.query_problems('Quelle est la capitale de la Mongolie aujourd’hui ?',claim))
        self.assertTrue(s.query_problems('',claim))

    def test_inconclusive_sources_use_valid_proposed_question(self):
        c=self.claim('Googlebot exécute JavaScript.');self.attach(c,c['text']+'\n\nSources\nGoogle documentation https://docs.example/js')
        calls=[]
        def fake(task,payload):
            calls.append(payload.get('mode'))
            if payload.get('mode')=='screen':return self.decision('sources',q=self.QUESTION)
            return self.verdict('review','insufficient')
        with patch.object(s,'llm',side_effect=fake),patch.object(s,'fetch_article_source',return_value={'status':'ok','text':'Autre sujet.'}):s.prescreen()
        row=s.query('SELECT route,search_query,result FROM screenings',one=True)
        self.assertEqual((row['route'],row['search_query']),('search',self.QUESTION));self.assertNotIn('query',calls)
        self.assertEqual(json.loads(row['result'])['query_version'],s.QUERY_VERSION)

    def test_bad_proposed_query_is_rewritten_then_checked(self):
        self.claim('Googlebot exécute JavaScript.')
        def fake(task,payload):
            if payload.get('mode')=='screen':return self.decision(q='Googlebot JavaScript documentation officielle')
            self.assertTrue(payload['problems']);return {'searchable':True,'query':self.QUESTION,'reason':'Fait documenté.'}
        with patch.object(s,'llm',side_effect=fake):s.prescreen()
        self.assertEqual(s.query('SELECT search_query FROM screenings',one=True)['search_query'],self.QUESTION)

    def test_two_invalid_questions_block_search(self):
        self.claim('Googlebot exécute JavaScript.')
        def fake(task,payload):
            if payload.get('mode')=='screen':return self.decision(q='')
            return {'searchable':True,'query':'Googlebot exécute JavaScript.','reason':''}
        with patch.object(s,'llm',side_effect=fake) as llm,patch.object(s,'ibou') as ibou:s.verify(100);ibou.assert_not_called()
        self.assertEqual(llm.call_count,3);self.assertEqual(s.state()['claims'][0]['blocker'],'bad_query')

    def test_prepare_queries_authorizes_search_without_ibou(self):
        c=self.legacy()
        with patch.object(s,'llm',return_value={'searchable':True,'query':self.QUESTION,'reason':'Fait documenté.'}) as llm,patch.object(s,'ibou') as ibou:
            s.prepare_queries([c['id']]);s.prepare_queries([c['id']])
        self.assertEqual(llm.call_count,1);ibou.assert_not_called()
        row=s.query('SELECT route,search_query FROM screenings',one=True)
        self.assertEqual((row['route'],row['search_query']),('search',self.QUESTION))
        self.assertEqual(s.query('SELECT status FROM claims',one=True)['status'],'pending')
        self.assertIsNotNone(s.screening_current(s.query('SELECT * FROM claims',one=True)))
        self.assertEqual(s.estimate()['search'],1);self.assertEqual(s.progress()['search']['old_queries'],0)

    def test_prepare_queries_keeps_unsearchable_claims_in_review(self):
        c=self.legacy()
        with patch.object(s,'llm',return_value={'searchable':False,'query':'','reason':'Affirmation d’inexistence.'}):s.prepare_queries([c['id']])
        self.assertEqual(s.query('SELECT status FROM claims',one=True)['status'],'review')
        self.assertEqual(s.state()['claims'][0]['blocker'],'not_searchable')

    def test_prepare_queries_never_touches_human_decisions(self):
        c=self.legacy()
        s.save_verdict(c,{'status':'review','reason':'Décision humaine : à garder','correction':'','actor':'human'})
        with patch.object(s,'llm') as llm:s.prepare_queries([c['id']]);llm.assert_not_called()

    def test_old_pending_queries_are_rewritten_as_questions(self):
        c=self.claim('Googlebot exécute JavaScript.')
        with patch.object(s,'llm',return_value=self.decision(q=self.QUESTION)):s.prescreen()
        s.query("UPDATE screenings SET search_query='Googlebot JavaScript rendering',result=replace(result,?,'ancien')",(s.QUERY_VERSION,))
        self.assertEqual(s.progress()['search']['old_queries'],1)
        with patch.object(s,'llm',return_value={'searchable':True,'query':self.QUESTION,'reason':''}):s.catch_up()
        self.assertEqual(s.query('SELECT search_query FROM screenings',one=True)['search_query'],self.QUESTION)
        self.assertEqual(s.progress()['search']['old_queries'],0)

    def test_catch_up_requeues_claims_judged_on_a_cut_source(self):
        c=self.legacy(long_source=True)
        with patch.object(s,'llm') as llm:s.catch_up();llm.assert_not_called()
        self.assertIsNone(s.query('SELECT * FROM screenings',one=True))
        self.assertEqual(s.query('SELECT status FROM claims',one=True)['status'],'pending');self.assertEqual(s.estimate()['unscreened'],1)

    def test_catch_up_writes_question_when_source_was_fully_read(self):
        self.legacy()
        with patch.object(s,'llm',return_value={'searchable':True,'query':self.QUESTION,'reason':''}):s.catch_up()
        self.assertEqual(s.query('SELECT route FROM screenings',one=True)['route'],'search')

    def test_search_query_edit_is_checked_against_the_claim(self):
        c=self.claim('Googlebot exécute JavaScript.')
        with patch.object(s,'llm',return_value=self.decision(q=self.QUESTION)):s.prescreen()
        s.set_search_query(c['id'],'  Googlebot interprète-t-il le JavaScript des pages ?  ')
        self.assertEqual(s.query('SELECT search_query FROM screenings',one=True)['search_query'],'Googlebot interprète-t-il le JavaScript des pages ?')
        self.assertIsNotNone(s.screening_current(s.query('SELECT * FROM claims',one=True)))
        for bad in ('x','Quelle est la capitale de la Mongolie aujourd’hui ?','Googlebot exécute JavaScript.'):
            with self.assertRaises(ValueError):s.set_search_query(c['id'],bad)
        s.save_verdict(c,{'status':'review','reason':'vu','correction':''})
        with self.assertRaises(ValueError):s.set_search_query(c['id'],self.QUESTION)

    def source(self,cid,text,url='https://source.example/long'):
        eid=s.uid();s.query('INSERT INTO evidence(id,claim_id,url,title,text,external_chunk,created) VALUES(?,?,?,?,?,?,?)',(eid,cid,url,'Source',text,'',s.now()));return eid

    def test_short_sources_are_sent_whole(self):
        passes,total=s.evidence_passes([{'id':'a','url':'u','text':'x'*30000}],'x')
        self.assertEqual((len(passes),len(passes[0][0]['text']),total),(1,30000,30000))

    def test_windows_cover_every_character(self):
        text=('Phrase de remplissage sans rapport. '*40+'\n')*300
        spans=s.evidence_windows(text)
        self.assertEqual(''.join(text[a:b] for a,b in spans),text)
        passes,total=s.evidence_passes([{'id':'a','url':'u','text':text}],'rien')
        self.assertGreater(len(passes),1);self.assertEqual(sum(len(e['text'].replace('\n[…]\n','')) for p in passes for e in p),len(text))

    def test_proof_beyond_old_limit_is_found_first(self):
        c=self.claim('Googlebot exécute JavaScript.');filler='Paragraphe sans rapport sur la météo et les marées.\n'*5000
        proof='Googlebot exécute JavaScript lors du rendu des pages.'
        eid=self.source(c['id'],filler+proof+'\n'+filler)
        self.assertGreater(len(filler),200000)
        seen=[]
        def fake(task,payload):
            text=payload['evidence'][0]['text'];seen.append(proof in text)
            if proof in text:return {'status':'validated','reason':'Étayé','correction':'','same_subject':True,'relation':'supports','outdated':False,'citations':[{'evidence_id':eid,'quote':proof}]}
            return self.verdict('review','insufficient')
        with patch.object(s,'llm',side_effect=fake):result=s.evaluate(c)
        self.assertEqual((result['status'],seen),('validated',[True]))
        self.assertTrue(result['coverage']['complete']);self.assertLess(result['coverage']['read'],result['coverage']['total'])

    def test_inconclusive_long_source_is_read_to_the_end(self):
        c=self.claim('Googlebot exécute JavaScript.');self.source(c['id'],'Paragraphe sans rapport sur la météo.\n'*8000)
        with patch.object(s,'llm',return_value=self.verdict('review','insufficient')) as llm:result=s.evaluate(c)
        self.assertEqual(llm.call_count,result['coverage']['passes']);self.assertGreater(llm.call_count,1)
        self.assertTrue(result['coverage']['complete']);self.assertEqual(result['coverage']['read'],result['coverage']['total'])

    def test_unread_remainder_is_stated_in_the_reason(self):
        c=self.claim('Googlebot exécute JavaScript.');self.source(c['id'],'Paragraphe sans rapport sur la météo.\n'*40000)
        with patch.object(s,'llm',return_value=self.verdict('review','insufficient')) as llm:result=s.evaluate(c)
        self.assertEqual(llm.call_count,s.EVIDENCE_PASSES);self.assertFalse(result['coverage']['complete'])
        self.assertIn('Lecture incomplète',result['reason'])

    def test_outdated_flag_requires_a_supported_correction(self):
        c=self.claim('Le seuil est de 500 pages.');eid=self.source(c['id'],'Le seuil est désormais de 1000 pages.')
        answer={'status':'correct','reason':'Seuil relevé','correction':'Le seuil est de 1000 pages.','same_subject':True,'relation':'contradicts','outdated':True,'citations':[{'evidence_id':eid,'quote':'Le seuil est désormais de 1000 pages.'}]}
        with patch.object(s,'llm',return_value=answer):result=s.evaluate(c)
        self.assertTrue(result['outdated']);self.assertTrue(result['reason'].startswith('Affirmation dépassée.'))
        with patch.object(s,'llm',return_value={**answer,'status':'review'}):self.assertFalse(s.evaluate(c)['outdated'])

    def test_prescreen_total_is_remaining_work(self):
        for i in range(4):self.claim(f'Fait numéro {i}.')
        with patch.object(s,'llm',return_value=self.decision('skip')):
            s.prescreen(1);self.assertEqual((s.JOB['done'],s.JOB['total']),(1,1))
            totals=[]
            def spy(claim):totals.append(s.JOB['total']);return original(claim)
            original=s.screen_claim
            with patch.object(s,'screen_claim',side_effect=spy):s.prescreen()
        self.assertEqual(totals,[3,3,3])

    def test_job_trail_rate_and_run_history(self):
        s.JOB.update(status='idle')
        def work():
            s.JOB.update(stage='extraction',done=0,total=4);s.JOB.update(done=4)
            s.JOB.update(stage='screening',done=0,total=2);s.JOB['done']=2
        cancel=s.start_job('all',work);cancel.wait(2)
        stages=[t['stage'] for t in s.JOB['trail']]
        self.assertEqual(stages[-2:],['extraction','screening'])
        self.assertTrue(all(t.get('ended') for t in s.JOB['trail']))
        self.assertEqual(s.JOB['trail'][-2]['done'],4)
        run=s.query('SELECT * FROM job_runs',one=True)
        self.assertEqual((run['kind'],run['status']),('all','completed'));self.assertIn('extraction',run['stages'])
        view=s.job_view();self.assertNotIn('rate_base',view);self.assertIn('eta_seconds',view)

    def test_interrupted_run_is_closed_at_restart(self):
        s.query("INSERT INTO job_runs VALUES('r','all',?,NULL,'running','','[]')",(s.now(),))
        s.init()
        self.assertEqual(s.query('SELECT status FROM job_runs',one=True)['status'],'interrupted')

    def test_http_pulse_history_and_new_assets(self):
        (s.ROOT/'app'/'suivi.js').stat();self.claim()
        server=s.ThreadingHTTPServer(('127.0.0.1',0),s.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            for path in ('/suivi.js','/suivi.css','/api/pulse','/api/history?kind=ibou&q=x'):
                with urlopen(base+path,timeout=5) as response:
                    self.assertEqual(response.status,200);body=response.read().decode()
                    if path=='/api/pulse':
                        data=json.loads(body);self.assertEqual(data['progress']['claims']['total'],1)
                        self.assertNotIn('claims',data);self.assertNotIn('openai_key',data['config'])
            with urlopen(base+'/',timeout=5) as response:self.assertIn('/suivi.js',response.read().decode())
            s.JOB['status']='idle'
            request=Request(base+'/api/catch-up',data=b'{}',headers={'Content-Type':'application/json','X-Session-Token':s.TOKEN})
            with patch.object(s,'start_job') as job:
                with urlopen(request,timeout=5) as response:self.assertEqual(response.status,200)
                self.assertEqual(job.call_args.args[0],'catchup')
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
