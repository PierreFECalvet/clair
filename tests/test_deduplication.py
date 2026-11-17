import json
import unittest
from unittest.mock import patch
import test_pipeline

s=test_pipeline.s

class DeduplicationTests(unittest.TestCase):
    setUp=test_pipeline.PipelineTests.setUp
    tearDown=test_pipeline.PipelineTests.tearDown
    claim=test_pipeline.PipelineTests.claim
    evidence=test_pipeline.PipelineTests.evidence

    def pair(self,period_a='',period_b=''):
        return self.claim('Googlebot exécute le JavaScript des pages.',period_a),self.claim('Le JavaScript des pages est exécuté par Googlebot.',period_b)

    def answer(self,same=True):
        return lambda task,payload:{'matches':[{'id':c['id'],'same':same,'reason':'Même fait' if same else 'Périmètres distincts'} for c in payload['candidates']]}

    def test_semantic_merge_without_embeddings_across_articles(self):
        a,b=self.pair()
        for i,c in enumerate((a,b)):
            vid=s.add_content('texte:lot:'+str(i),'Article '+str(i),c['text'])
            chunk=s.query('SELECT id FROM chunks WHERE version_id=?',(vid,),True)['id']
            s.query('INSERT INTO occurrences VALUES(?,?,?,?,?)',(s.uid(),c['id'],chunk,c['text'],'[]'))
            self.evidence(c['id'])
            s.save_verdict(c,{'status':'review','reason':'Ancien contrôle','correction':''})
        with patch.object(s,'llm',side_effect=self.answer()) as llm,patch.object(s,'ibou') as ibou:
            s.deduplicate();self.assertEqual(llm.call_count,1);ibou.assert_not_called()
        self.assertEqual(len(s.query('SELECT * FROM claims')),1)
        self.assertEqual(len(s.query('SELECT * FROM occurrences')),2)
        self.assertEqual(len(s.query('SELECT * FROM evidence')),2)
        self.assertEqual(len(s.query('SELECT * FROM checks')),3)
        archive=s.query('SELECT * FROM claim_merges',one=True)
        self.assertEqual(len(json.loads(archive['snapshot'])['claims']),2)
        self.assertEqual(s.query('SELECT status FROM claims',one=True)['status'],'pending')

    def test_rejected_pair_is_cached_on_resume(self):
        self.pair()
        with patch.object(s,'llm',side_effect=self.answer(False)) as llm:
            s.deduplicate();s.init();s.deduplicate();self.assertEqual(llm.call_count,1)
        self.assertEqual(len(s.query('SELECT * FROM claims')),2)

    def test_years_different_never_merge_even_if_model_would_agree(self):
        self.pair('2023','2024')
        with patch.object(s,'llm',side_effect=self.answer()) as llm:s.deduplicate();llm.assert_not_called()
        self.assertEqual(len(s.query('SELECT * FROM claims')),2)

    def test_equivalent_period_labels_can_reach_llm(self):
        self.pair('2024','en 2024')
        with patch.object(s,'llm',side_effect=self.answer()) as llm:s.deduplicate();self.assertEqual(llm.call_count,1)
        self.assertEqual(len(s.query('SELECT * FROM claims')),1)

    def test_manual_decisions_and_exclusion_boundaries_protected(self):
        a,b=self.pair();s.set_claim_selection([b['id']],True)
        with patch.object(s,'llm') as llm:s.deduplicate();llm.assert_not_called()
        s.set_claim_selection([b['id']],False)
        s.save_verdict(a,{'status':'validated','reason':'Décision humaine : preuve','correction':''})
        with patch.object(s,'llm') as llm:s.deduplicate();llm.assert_not_called()
        self.assertFalse(s.merge_claims(a['id'],b['id'],'Même fait'))

    def test_duplicate_occurrence_combines_sources(self):
        a,b=self.pair()
        for c,url in ((a,'https://source.example/a'),(b,'https://source.example/b')):
            s.query('INSERT INTO occurrences VALUES(?,?,?,?,?)',(s.uid(),c['id'],'chunk','citation',json.dumps([url])))
        self.assertTrue(s.merge_claims(a['id'],b['id'],'Même fait'))
        rows=s.query('SELECT * FROM occurrences');self.assertEqual(len(rows),1)
        self.assertEqual(set(json.loads(rows[0]['sources'])),{'https://source.example/a','https://source.example/b'})

    def test_merge_invalidates_screening_and_proposed_patches(self):
        a,b=self.pair()
        for c in (a,b):
            s.query('INSERT INTO screenings VALUES(?,?,?,?,?,?,?)',(c['id'],'hash','search','raison','query','{}',s.now()))
        s.query('INSERT INTO patches VALUES(?,?,?,?,?,?,?)',('p',b['id'],'v','old','new','proposed',s.now()))
        s.merge_claims(a['id'],b['id'],'Même fait')
        self.assertEqual(s.query('SELECT * FROM screenings'),[])
        self.assertEqual(s.query('SELECT status,claim_id FROM patches',one=True),{'status':'conflict','claim_id':a['id']})

    def test_invalid_llm_answer_cannot_merge(self):
        self.pair()
        with patch.object(s,'llm',return_value={'matches':[{'id':'invented','same':True,'reason':'oui'}]}):
            with self.assertRaises(s.OperationalError):s.deduplicate()
        self.assertEqual(len(s.query('SELECT * FROM claims')),2)
        self.assertEqual(s.query('SELECT * FROM claim_merges'),[])

    def test_retry_splits_failed_batch_into_single_candidates(self):
        a,b=self.pair();self.claim('Googlebot exécute du JavaScript pour les pages.')
        sizes=[]
        def fake(task,payload):
            sizes.append(len(payload['candidates']))
            if len(sizes)==1:return {'matches':[]}
            return self.answer(False)(task,payload)
        with patch.object(s,'llm',side_effect=fake):
            with self.assertRaises(s.RetryableDedupError):s.deduplicate()
            self.assertEqual(sizes,[2]);s.deduplicate()
        self.assertTrue(all(size==1 for size in sizes[1:]))
        self.assertEqual(len(s.query('SELECT * FROM claims')),3)
        self.assertIsNotNone(s.JOB['list_completed_at'])
        self.assertTrue(any('full list completed' in r['message'] for r in s.query('SELECT message FROM events')))

    def test_pause_before_processing_preserves_data(self):
        self.pair();s.STOP.set()
        with patch.object(s,'llm') as llm:
            with self.assertRaises(s.OperationalError):s.deduplicate()
            llm.assert_not_called()
        self.assertEqual(len(s.query('SELECT * FROM claims')),2)

    def test_prescreen_entry_runs_dedup_first(self):
        calls=[]
        with patch.object(s,'deduplicate',side_effect=lambda:calls.append('dedup')),patch.object(s,'prescreen',side_effect=lambda limit:calls.append(limit)):
            s.screen_with_dedup(50)
        self.assertEqual(calls,['dedup',50])
