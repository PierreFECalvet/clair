"""Fournisseur OpenRouter : appels simulés, aucune requête réseau."""
import json
import threading
import unittest
from urllib.request import Request,urlopen
from unittest.mock import patch
import test_pipeline
import chaine
from chaine import noyau,moteurs

s=test_pipeline.s

class OpenRouterTests(unittest.TestCase):
    setUp=test_pipeline.PipelineTests.setUp
    tearDown=test_pipeline.PipelineTests.tearDown

    def configure(self):
        chaine.brancher(s);s.OPENROUTER_SANS_SCHEMA.clear()
        s.CONFIG.update(provider='openrouter',model='fournisseur/modele:free',openrouter_key='sk-or-v1-cle-de-test-0123456789')
        self.addCleanup(lambda:s.CONFIG.update(provider='openai',model='',openrouter_key=''))

    def answer(self,content='{"ok": true}',**extra):
        return ({'choices':[{'message':{'content':content},'finish_reason':'stop',**extra}],'usage':{'prompt_tokens':42,'completion_tokens':5}},{})

    def test_call_uses_strict_schema_key_header_and_logs_tokens(self):
        self.configure()
        with patch.object(s,'http_json',return_value=self.answer()) as http:
            self.assertEqual(s.llm('Test',{'test':'x'}),{'ok':True})
        url,body,headers=http.call_args.args
        self.assertEqual(url,'https://openrouter.ai/api/v1/chat/completions')
        self.assertEqual(body['model'],'fournisseur/modele:free');self.assertEqual(body['response_format']['type'],'json_schema')
        self.assertTrue(body['response_format']['json_schema']['strict'])
        self.assertEqual(headers['Authorization'],'Bearer sk-or-v1-cle-de-test-0123456789')
        row=s.query('SELECT * FROM api_log',one=True)
        self.assertEqual((row['service'],row['status'],row['tokens_in'],row['tokens_out']),('openrouter','success',42,5))
        self.assertNotIn('cle-de-test',json.dumps(row))

    def test_model_without_structured_outputs_falls_back_to_json_mode_once(self):
        self.configure()
        refus=s.OperationalError('OpenRouter — HTTP 400 : response_format non accepté');refus.code=400
        with patch.object(s,'http_json',side_effect=[refus,self.answer(),self.answer()]) as http:
            s.llm('Test',{'test':'x'});s.llm('Test',{'test':'x'})
        kinds=[c.args[1]['response_format']['type'] for c in http.call_args_list]
        self.assertEqual(kinds,['json_schema','json_object','json_object'])

    def test_rate_limit_is_not_retried_as_json_mode(self):
        self.configure()
        limite=s.OperationalError('OpenRouter — HTTP 429 : limite de requêtes');limite.code=429
        with patch.object(s,'http_json',side_effect=limite) as http:
            with self.assertRaises(s.OperationalError):s.llm('Test',{'test':'x'})
        self.assertEqual(http.call_count,1)
        self.assertEqual(s.query('SELECT http_status FROM api_log',one=True)['http_status'],429)

    def test_truncated_or_failed_choice_is_an_error(self):
        self.configure()
        with patch.object(s,'http_json',return_value=self.answer('{"ok": tr',finish_reason='length')):
            with self.assertRaises(s.OperationalError):s.llm('Test',{'test':'x'})
        with patch.object(s,'http_json',return_value=self.answer('',error={'code':502,'message':'hébergeur indisponible'})):
            with self.assertRaisesRegex(s.OperationalError,'hébergeur indisponible'):s.llm('Test',{'test':'x'})

    def test_missing_key_stops_before_any_request(self):
        self.configure();s.CONFIG['openrouter_key']=''
        with patch.object(s,'http_json') as http:
            with self.assertRaisesRegex(s.OperationalError,'OpenRouter'):s.llm('Test',{'test':'x'})
            http.assert_not_called()

    def test_openrouter_is_a_third_party_llm_without_embeddings(self):
        self.configure()
        self.assertEqual(noyau.type_fournisseur(),'tiers');self.assertIsNotNone(noyau.raison_llm_local())
        with patch.object(s,'http_json') as http:
            self.assertIsNone(s.embed('texte'));http.assert_not_called()
        self.assertFalse(moteurs._api_embeddings_disponible()[0])

    def test_http_config_listing_and_no_key_leak(self):
        chaine.brancher(s);s.JOB['status']='idle'
        self.addCleanup(lambda:s.CONFIG.update(provider='openai',model='',openrouter_key=''))
        server=s.ThreadingHTTPServer(('127.0.0.1',0),s.Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        def post(path,data):
            request=Request(base+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json','X-Session-Token':s.TOKEN})
            with urlopen(request,timeout=5) as response:return json.loads(response.read())
        modeles={'data':[{'id':'a/payant','supported_parameters':['structured_outputs']},{'id':'b/libre:free','supported_parameters':['structured_outputs']},{'id':'c/sans:free','supported_parameters':[]}]}
        quota={'data':{'free_model_daily_requests':{'used':3,'limit':50,'remaining':47}}}
        try:
            post('/api/config',{'provider':'openrouter','model':'b/libre:free','openrouter_key':'sk-or-v1-cle-de-test-0123456789'})
            self.assertEqual(s.CONFIG['openrouter_key'],'sk-or-v1-cle-de-test-0123456789')
            post('/api/config',{'provider':'openrouter'});self.assertTrue(s.CONFIG['openrouter_key'])
            with patch.object(s,'http_json',side_effect=[(modeles,{}),(quota,{})]):
                listing=post('/api/test',{'target':'llm'})
            self.assertEqual(listing['models'][0],'b/libre:free');self.assertIn('47',listing['message']);self.assertEqual(len(listing['models']),3)
            with urlopen(base+'/api/pulse',timeout=5) as response:body=response.read().decode()
            self.assertNotIn('cle-de-test',body);self.assertTrue(json.loads(body)['config']['openrouter_key_set'])
            post('/api/clear-keys',{});self.assertEqual(s.CONFIG['openrouter_key'],'')
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
