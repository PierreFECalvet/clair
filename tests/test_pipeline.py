import unittest, tempfile, threading, json, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server as s

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        s.DB=Path(self.tmp.name)/'test.sqlite3';s.init();s.STOP.clear()
        s.CONFIG.update(embeddings=False,ibou_key='test-only',model='test-model')
    def tearDown(self): self.tmp.cleanup()
    def claim(self,text='Paris est en France.',period=''):
        cid=s.uid();s.query('INSERT INTO claims(id,text,period,created) VALUES(?,?,?,?)',(cid,text,period,s.now()))
        return s.query('SELECT * FROM claims WHERE id=?',(cid,),True)
    def evidence(self,cid,text='Paris est en France.',url='https://source.example/fact'):
        eid=s.uid();s.query('INSERT INTO evidence(id,claim_id,url,title,text,external_chunk,created) VALUES(?,?,?,?,?,?,?)',(eid,cid,url,'Source',text,'0',s.now()));return eid
    def test_chunks_preserve_all_positions(self):
        text=('Une phrase de test.\n'*1300);covered=set()
        for start,part in s.chunks(text):
            self.assertEqual(text[start:start+len(part)],part);covered.update(range(start,start+len(part)))
        self.assertEqual(len(covered),len(text))
    def test_import_is_idempotent_and_versioned(self):
        a=s.add_content('https://example.org/a','A','Ancien texte','2020')
        self.assertEqual(a,s.add_content('https://example.org/a','A','Ancien texte','2020'))
        b=s.add_content('https://example.org/a','A','Nouveau texte','2024')
        self.assertNotEqual(a,b);self.assertEqual(len(s.query('SELECT * FROM pages')),1)
        self.assertEqual(len(s.query('SELECT * FROM versions')),2)
    def test_extract_dedup_and_preserve_dates(self):
        s.add_content('https://example.org/a','A','X a 200 salariés en 2023. X a 250 salariés en 2024.')
        s.add_content('https://example.org/b','B','X a 200 salariés en 2023.')
        def fake(task,payload):
            items=[{'text':'X a 200 salariés en 2023.','quote':'X a 200 salariés en 2023.','period':'2023','sources':[]}]
            if '250' in payload['chunk']:items.append({'text':'X a 250 salariés en 2024.','quote':'X a 250 salariés en 2024.','period':'2024','sources':[]})
            return {'claims':items}
        with patch.object(s,'llm',side_effect=fake):s.prepare();s.prepare()
        self.assertEqual(len(s.query('SELECT * FROM claims')),2)
        self.assertEqual(len(s.query('SELECT * FROM occurrences')),3)
        self.assertEqual(s.estimate()['initial'],0)
        self.assertEqual(s.estimate()['unscreened'],2)
    def test_invalid_quote_leaves_chunk_resumable(self):
        s.add_content('','A','Texte réel.')
        with patch.object(s,'llm',return_value={'claims':[{'text':'Autre','quote':'inventé','period':''}]}):
            with self.assertRaises(s.OperationalError):s.prepare()
        self.assertEqual(s.estimate()['unprepared'],1)
    def test_atomic_budget_never_exceeded(self):
        s.query('INSERT INTO budgets VALUES(?,?,?,?)',('b',9,0,s.now()));results=[]
        def run():
            try:s.reserve('b','passage','q');results.append(True)
            except s.BudgetError:results.append(False)
        threads=[threading.Thread(target=run) for _ in range(12)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(sum(results),3);self.assertEqual(s.query('SELECT used FROM budgets',one=True)['used'],9)
    def test_ibou_cache_avoids_second_charge(self):
        s.query('INSERT INTO budgets VALUES(?,?,?,?)',('b',3,0,s.now()))
        with patch.object(s,'http_json',return_value=({'results':[]},{})) as request:
            s.ibou('passage','question','b');s.ibou('passage','question','b')
            self.assertEqual(request.call_count,1)
        self.assertEqual(s.query('SELECT used FROM budgets',one=True)['used'],3)
    def test_credit_failure_refunded_and_not_false(self):
        c=self.claim();s.query('INSERT INTO budgets VALUES(?,?,?,?)',('b',3,0,s.now()))
        error=s.OperationalError('crédits épuisés');error.code=402
        with patch.object(s,'http_json',side_effect=error):
            with self.assertRaises(s.OperationalError):s.ibou('passage','q','b')
        self.assertEqual(s.query('SELECT used FROM budgets',one=True)['used'],0)
        self.assertEqual(s.query('SELECT status FROM claims WHERE id=?',(c['id'],),True)['status'],'pending')
    def test_timeout_budget_remains_reserved(self):
        s.query('INSERT INTO budgets VALUES(?,?,?,?)',('b',3,0,s.now()))
        with patch.object(s,'http_json',side_effect=s.OperationalError('timeout')):
            with self.assertRaises(s.OperationalError):s.ibou('passage','q','b')
        self.assertEqual(s.query('SELECT used FROM budgets',one=True)['used'],3)
        self.assertEqual(s.query('SELECT status FROM calls',one=True)['status'],'uncertain')
    def test_hallucinated_citation_cannot_validate(self):
        c=self.claim();eid=self.evidence(c['id'])
        with patch.object(s,'llm',return_value={'status':'validated','reason':'oui','citations':[{'evidence_id':eid,'quote':'Inventé'}]}):
            self.assertEqual(s.evaluate(c)['status'],'review')
    def test_valid_evidence_citation(self):
        c=self.claim();eid=self.evidence(c['id'])
        with patch.object(s,'llm',return_value={'status':'validated','same_subject':True,'relation':'supports','reason':'Conforme','citations':[{'evidence_id':eid,'quote':'Paris est en France.'}]}):
            self.assertEqual(s.evaluate(c)['status'],'validated')
    def test_self_source_not_used(self):
        c=self.claim();v=s.add_content('https://source.example/a','A',c['text']);chunk=s.query('SELECT id FROM chunks',one=True)['id']
        s.query('INSERT INTO occurrences VALUES(?,?,?,?,?)',(s.uid(),c['id'],chunk,c['text'],'[]'));self.evidence(c['id'])
        with patch.object(s,'llm') as llm:self.assertEqual(s.evaluate(c)['status'],'review');llm.assert_not_called()
    def test_patch_creates_version_without_overwriting(self):
        v=s.add_content('https://example.org/a','A','Texte ancien.');c=self.claim();pid=s.uid()
        s.query('INSERT INTO patches VALUES(?,?,?,?,?,?,?)',(pid,c['id'],v,'ancien','corrigé','proposed',s.now()))
        new=s.apply_patch_version(pid)
        self.assertEqual(s.query('SELECT text FROM versions WHERE id=?',(v,),True)['text'],'Texte ancien.')
        self.assertEqual(s.query('SELECT text,prepared FROM versions WHERE id=?',(new,),True),{'text':'Texte corrigé.','prepared':0})
    def test_state_never_exposes_keys(self):
        s.CONFIG['openai_key']='secret-value-not-for-client'
        self.assertNotIn('secret-value-not-for-client',json.dumps(s.state()))
    def test_private_source_rejected(self):
        with self.assertRaises(ValueError):s.public_url('http://127.0.0.1/a')
        with self.assertRaises(ValueError):s.public_url('file:///etc/passwd')
    def test_full_correction_and_reverification_cycle(self):
        version=s.add_content('https://audited.example/a','A','Paris est en Allemagne.')
        def fake_llm(task,payload):
            if payload.get('mode')=='deduplicate':return {'matches':[{'id':c['id'],'same':False,'reason':'Faits contradictoires'} for c in payload['candidates']]}
            if payload.get('mode')=='screen':return {'route':'search','reason':'Fait géographique externe','standalone_claim':payload['claim'],'search_query':'Dans quel pays se trouve Paris ?','source_urls':[],'content_ids':[]}
            if 'Extrais' in task:
                return {'claims':[{'text':payload['chunk'],'quote':payload['chunk'],'period':'','sources':[]}]}
            if 'Compare' in task:
                evidence=payload['evidence'][0]
                return {'status':'correct' if 'Allemagne' in payload['claim'] else 'validated',
                        'reason':'Preuve consultée','correction':'Paris est en France.','same_subject':True,'relation':'contradicts' if 'Allemagne' in payload['claim'] else 'supports',
                        'citations':[{'evidence_id':evidence['id'],'quote':'Paris est en France.'}]}
            if 'Réécris' in task:return {'replacement':'Paris est en France.'}
            raise AssertionError(task)
        def fake_http(url,body=None,headers=None,**kw):
            if url.endswith('/units'):return {'remaining':100},{}
            return {'results':[{'url':'https://independent.example/paris','title':'Paris','passage':{'chunk_id':7,'text':'Paris est en France.'}}]},{}
        with patch.object(s,'llm',side_effect=fake_llm),patch.object(s,'http_json',side_effect=fake_http):
            s.prepare();s.verify(11);self.assertEqual(s.query('SELECT status FROM claims',one=True)['status'],'correct')
            s.correct();proposal=s.query('SELECT * FROM patches',one=True);self.assertIsNotNone(proposal)
            new=s.apply_patch_version(proposal['id']);s.prepare();s.verify(11)
        self.assertNotEqual(new,version)
        self.assertEqual(s.query('SELECT status FROM claims WHERE text=?',('Paris est en France.',),True)['status'],'validated')
        self.assertEqual(s.query('SELECT text FROM versions WHERE id=?',(version,),True)['text'],'Paris est en Allemagne.')
    def test_openai_gateway_uses_responses_without_storage(self):
        s.CONFIG.update(provider='openai',model='chosen-model',openai_key='test-secret')
        result={'output':[{'type':'message','content':[{'type':'output_text','text':'{"ok":true}'}]}]}
        with patch.object(s,'http_json',return_value=(result,{})) as call:
            self.assertEqual(s.llm('Test',{'text':'Donnée'}),{'ok':True})
            self.assertEqual(call.call_args.args[0],'https://api.openai.com/v1/responses')
            self.assertFalse(call.call_args.args[1]['store'])
            self.assertEqual(call.call_args.args[1]['model'],'chosen-model')
            self.assertEqual(call.call_args.args[1]['text']['format']['type'],'json_schema')
            self.assertTrue(call.call_args.args[1]['text']['format']['strict'])
            self.assertEqual(call.call_args.args[1]['input'][0]['role'],'user')
    def test_lmstudio_gateway_uses_local_chat(self):
        s.CONFIG.update(provider='lmstudio',lm_url='http://127.0.0.1:1234/v1',model='local-model')
        with patch.object(s,'http_json',return_value=({'choices':[{'message':{'content':'{"ok":true}'}}]},{})) as call:
            self.assertEqual(s.llm('Test',{}),{'ok':True})
            self.assertEqual(call.call_args.args[0],'http://127.0.0.1:1234/v1/chat/completions')
    def test_bulk_import_preserves_separate_contents_and_retries(self):
        items=[{'id':f'{i:032x}','title':f'Article {i}','text':f'Texte numéro {i}.\nDeuxième paragraphe.'} for i in range(25)]
        cleaned=s.validate_text_batch(items)
        s.import_many(cleaned);s.import_many(cleaned)
        self.assertEqual(len(s.query('SELECT * FROM pages')),25)
        self.assertEqual(len(s.query('SELECT * FROM versions')),25)
        self.assertEqual(len(s.query('SELECT * FROM chunks')),25)
        self.assertEqual(s.JOB['done'],25)
    def test_run_all_order_and_cap(self):
        calls=[]
        with patch.object(s,'preflight',side_effect=lambda:calls.append(('test',))),patch.object(s,'prepare',side_effect=lambda:calls.append(('prepare',))),patch.object(s,'deduplicate',side_effect=lambda:calls.append(('dedup',))),patch.object(s,'prescreen',side_effect=lambda:calls.append(('screen',))),patch.object(s,'estimate',return_value={'claims':2,'search':2,'initial':6,'expected':6,'maximum':6}),patch.object(s,'verify',side_effect=lambda cap,prepared=False:calls.append(('verify',cap))),patch.object(s,'correct',side_effect=lambda:calls.append(('correct',))):
            s.run_all(10)
        self.assertEqual(calls,[('test',),('prepare',),('dedup',),('screen',),('verify',10),('correct',)])
        self.assertIn('Plafond autorisé : 10',s.query("SELECT message FROM events WHERE message LIKE 'Estimation%'",one=True)['message'])
    def test_run_all_stops_on_preflight_failure(self):
        with patch.object(s,'preflight',side_effect=s.OperationalError('modèle indisponible')),patch.object(s,'prepare') as prepare,patch.object(s,'verify') as verify:
            with self.assertRaises(s.OperationalError):s.run_all(100)
            prepare.assert_not_called();verify.assert_not_called()
    def test_real_preflight_tests_generation_and_embeddings(self):
        s.CONFIG['embeddings']=True
        with patch.object(s,'llm',return_value={'ok':True}) as llm,patch.object(s,'embed',return_value={'values':[1]}) as embed:
            s.preflight();llm.assert_called_once();embed.assert_called_once()
    def test_error_diagnostics_include_context_but_not_secret(self):
        import io
        from urllib.error import HTTPError
        s.CONFIG['openai_key']='secret-api-value'
        body=json.dumps({'error':{'message':'Unsupported format json_object. secret-api-value','param':'text.format','code':'unsupported_value'}}).encode()
        error=HTTPError('https://api.openai.com/v1/responses',400,'bad',{},io.BytesIO(body))
        with patch.object(s,'urlopen',side_effect=error):
            with self.assertRaises(s.OperationalError) as caught:s.http_json('https://api.openai.com/v1/responses',{'model':'gpt-6-luna'})
        message=str(caught.exception)
        self.assertIn('gpt-6-luna',message);self.assertIn('text.format',message);self.assertIn('unsupported_value',message)
        self.assertNotIn('secret-api-value',message)
    def test_extraction_schema_requires_traceable_quotes(self):
        schema=s.output_schema({'chunk':'Texte'})
        item=schema['properties']['claims']['items']
        self.assertEqual(set(item['required']),{'text','quote','period','sources'})
        self.assertFalse(item['additionalProperties'])
    def test_bulk_rejects_invalid_batch_before_writing(self):
        items=[{'id':'a'*32,'title':'Bon','text':'Texte valide'},{'id':'b'*32,'title':'Vide','text':'  '}]
        with self.assertRaises(ValueError):s.validate_text_batch(items)
        self.assertEqual(s.query('SELECT count(*) n FROM pages',one=True)['n'],0)
        with self.assertRaises(ValueError):s.validate_text_batch([{'id':'a'*32,'title':'Long','text':'x'*300001}])
        with self.assertRaises(ValueError):s.validate_text_batch([items[0]]*101)
        with self.assertRaises(ValueError):s.validate_text_batch([items[0],items[0]])
    def test_bulk_pause_then_resume_without_duplicate(self):
        items=s.validate_text_batch([{'id':f'{i:032x}','title':f'Article {i}','text':f'Texte {i}'} for i in range(3)])
        s.add_content('texte:lot:'+items[0]['id'],items[0]['title'],items[0]['text']);s.STOP.set()
        with self.assertRaises(s.OperationalError):s.import_many(items)
        self.assertEqual(len(s.query('SELECT * FROM versions')),1)
        s.STOP.clear();s.import_many(items)
        self.assertEqual(len(s.query('SELECT * FROM versions')),3)
    def test_budget_pause_keeps_unfinished_claim_pending(self):
        c=self.claim()
        def request(url,*a,**kw):return ({'remaining':100}, {}) if url.endswith('/units') else ({'results':[]},{})
        with patch.object(s,'http_json',side_effect=request),patch.object(s,'llm',return_value={'route':'search','reason':'Fait externe','standalone_claim':c['text'],'search_query':'Dans quel pays se trouve Paris ?','source_urls':[],'content_ids':[]}):
            with self.assertRaises(s.BudgetError):s.verify(2)
        self.assertEqual(s.query('SELECT status FROM claims WHERE id=?',(c['id'],),True)['status'],'pending')
    def test_quote_typography_reconciled_to_original(self):
        text='L’entreprise\u00a0compte\n  200 salariés.'
        quote="L'entreprise compte 200 salariés."
        self.assertEqual(s.source_quote(quote,text),text)
        self.assertIsNone(s.source_quote("L'entreprise compte 250 salariés.",text))
        self.assertIsNone(s.source_quote("L'entreprise ne compte pas 200 salariés.",text))
    def test_bad_chunk_does_not_block_good_chunk(self):
        s.add_content('https://example.org/bad','Bad','Texte source A.')
        s.add_content('https://example.org/good','Good','Texte source B.')
        def fake(task,payload):return {'claims':[{'text':'Fait','quote':'inventé' if payload['title']=='Bad' else 'Texte source B.','period':'','sources':[]}]}
        with patch.object(s,'llm',side_effect=fake):
            with self.assertRaises(s.OperationalError):s.prepare()
        self.assertEqual(s.query('SELECT count(*) n FROM chunks WHERE prepared=1',one=True)['n'],1)
        self.assertEqual(len(s.state()['issues']),1)
        self.assertEqual(len(s.query('SELECT * FROM claims')),1)
    def test_manual_draft_repair_is_consumed_without_reextracting(self):
        s.add_content('','Article','Texte exact.')
        chunk=s.query('SELECT * FROM chunks',one=True)
        clean=s.validate_extraction({'claims':[{'text':'Un fait','quote':'Texte exact.','period':'','sources':[]}]},chunk['text'])
        s.save_draft(chunk['id'],clean,'','ready')
        with patch.object(s,'llm') as llm:s.prepare(chunk['id']);llm.assert_not_called()
        self.assertEqual(s.query('SELECT prepared FROM chunks',one=True)['prepared'],1)
        self.assertEqual(s.query('SELECT * FROM extraction_drafts'),[])
    def test_exclusion_is_reversible_and_controls_budget_and_verification(self):
        c=self.claim();s.set_claim_selection([c['id']],True)
        self.assertEqual(s.estimate()['initial'],0)
        with patch.object(s,'http_json',return_value=({'remaining':100},{})),patch.object(s,'ibou') as ibou,patch.object(s,'llm') as llm:
            s.verify(10);s.correct();ibou.assert_not_called();llm.assert_not_called()
        self.assertEqual(s.query('SELECT status FROM claims',one=True)['status'],'pending')
        s.set_claim_selection([c['id']],False);self.assertEqual(s.estimate()['initial'],0);self.assertEqual(s.estimate()['unscreened'],1)
    def test_metadata_detection_is_conservative(self):
        self.assertTrue(s.metadata_reason('Le texte entier contient 600 mots.'))
        self.assertTrue(s.metadata_reason('Le contenu est classé dans la catégorie « SEO ».'))
        self.assertFalse(s.metadata_reason('La société emploie 600 personnes en 2024.'))
    def test_selection_gate_prevents_paid_search(self):
        estimation={'unprepared':1,'claims':2,'initial':6,'expected':14,'maximum':22}
        with patch.object(s,'preflight'),patch.object(s,'prepare'),patch.object(s,'estimate',return_value=estimation),patch.object(s,'verify') as verify:
            with self.assertRaises(s.OperationalError):s.run_all(100,select_claims=True)
            verify.assert_not_called()
    def test_footer_catalog_excludes_body_url_and_retains_references(self):
        text='Article\nhttps://audit.example/a\nTexte.\n\nSources\n1. Cutts, sculpting, 2009. https://source.example/sculpting\n2. Brevet US1234567. https://patents.example/US1234567'
        catalog=s.article_sources(text)
        self.assertEqual(len(catalog),2)
        self.assertIn('Cutts',catalog[0]['reference'])
        self.assertEqual(s.relevant_sources(catalog,'Selon [Documenté : brevet US1234567]',1)[0]['url'],'https://patents.example/US1234567')
    def test_repair_fetches_footer_source_but_keeps_local_quote(self):
        text='Le score existe [Confirmé : Cutts, sculpting].\n'+'x '*2700+'\nSources\n1. Cutts, sculpting. https://source.example/sculpting'
        s.add_content('https://audited.example/a','A',text)
        chunk=s.query('SELECT * FROM chunks ORDER BY position LIMIT 1',one=True)
        self.assertNotIn('https://source.example/sculpting',chunk['text'])
        def fake(task,payload):
            if 'previous' not in payload:return {'claims':[{'text':'Le score existe.','quote':'Citation reformulée','period':'','sources':[]}]}
            self.assertEqual(payload['linked_source_content'][0]['status'],'ok')
            self.assertIn('Preuve externe',payload['linked_source_content'][0]['text'])
            return {'claims':[{'text':'Le score existe.','quote':'Le score existe [Confirmé : Cutts, sculpting].','period':'','sources':['https://source.example/sculpting']}]}
        with patch.object(s,'llm',side_effect=fake),patch.object(s,'fetch_page',return_value='Preuve externe') as fetch:
            s.prepare(chunk['id']);fetch.assert_called_once()
        occurrence=s.query('SELECT * FROM occurrences',one=True)
        self.assertEqual(json.loads(occurrence['sources']),['https://source.example/sculpting'])
        self.assertEqual(s.query('SELECT status FROM claims',one=True)['status'],'pending')
    def test_external_quote_cannot_replace_article_quote(self):
        with self.assertRaises(ValueError):s.validate_extraction({'claims':[{'text':'Fait','quote':'Preuve externe','period':''}]},'Texte de l’article.')
    def test_source_cache_and_unavailability(self):
        with patch.object(s,'fetch_page',return_value='Preuve') as fetch:
            s.fetch_article_source('https://source.example/a');s.fetch_article_source('https://source.example/a');fetch.assert_called_once()
        with patch.object(s,'fetch_page',side_effect=ValueError('PDF non pris en charge')):
            result=s.fetch_article_source('https://source.example/b.pdf')
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['text'],'')

if __name__=='__main__':unittest.main()
