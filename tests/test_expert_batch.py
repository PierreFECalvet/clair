import json
import unittest
from unittest.mock import patch
import test_expert

s=test_expert.s

class ExpertBatchTests(unittest.TestCase):
    setUp=test_expert.ExpertTests.setUp
    tearDown=test_expert.ExpertTests.tearDown
    claim=test_expert.ExpertTests.claim

    def test_selection_validated_before_creating_batch(self):
        c=self.claim()
        for ids in ([],None,[c['id'],'missing'],[123]):
            with self.assertRaises(ValueError):s.create_expert_batch(ids)
        self.assertEqual(s.query('SELECT * FROM expert_batches'),[])
        self.assertEqual(s.query('SELECT * FROM expert_batch_items'),[])

    def test_duplicate_selection_ids_only_processed_once(self):
        a=self.claim('Fait A');b=self.claim('Fait B');other=self.claim('Autre fait')
        bid=s.create_expert_batch([a['id'],b['id'],a['id']]);calls=[]
        def run(cid,**kwargs):
            self.assertEqual(s.JOB['stage'],'expert_batch');self.assertEqual(s.JOB['total'],2)
            calls.append(cid);return 'review-'+cid
        with patch.object(s,'expert_review',side_effect=run),patch.object(s,'ibou') as ibou:
            s.run_expert_batch(bid);ibou.assert_not_called()
        self.assertEqual(calls,[a['id'],b['id']]);self.assertNotIn(other['id'],calls)
        self.assertEqual(s.JOB['done'],2)
        self.assertEqual(s.latest_expert_batch()['status'],'completed')
        self.assertEqual(s.query('SELECT * FROM patches'),[])

    def test_error_then_restart_resumes_only_unfinished_items_with_original_model(self):
        a=self.claim('A');b=self.claim('B');bid=s.create_expert_batch([a['id'],b['id']])
        def fail(cid,**kwargs):
            if cid==b['id']:raise s.OperationalError('Connexion interrompue')
            return 'review-a'
        with patch.object(s,'expert_review',side_effect=fail):
            with self.assertRaises(s.OperationalError):s.run_expert_batch(bid)
        self.assertEqual(s.latest_expert_batch()['done'],1)
        self.assertEqual(s.latest_expert_batch()['status'],'paused')
        s.init();s.CONFIG['review_model']='different-model'
        with patch.object(s,'expert_review',return_value='review-b') as review:
            s.run_expert_batch(bid)
        review.assert_called_once_with(b['id'],batch_id=bid,model='chosen-advanced-model')
        self.assertEqual(s.latest_expert_batch()['done'],2)
        self.assertEqual(s.latest_expert_batch()['error'],'')

    def test_real_review_and_batch_checkpoint_saved_together(self):
        a=self.claim('A');b=self.claim('B');bid=s.create_expert_batch([a['id'],b['id']])
        def result(*args,**kwargs):
            s.STOP.set()
            return {'contextualized_claim':'A','verification_prompt':'Vérifier A','assessment':'Sans preuve',
                    'points':[],'proposed_correction':'','correction_supported':False,'same_subject':True,
                    'correction_citations':[],'remaining_questions':[],'search_queries':[]}
        with patch.object(s,'llm',side_effect=result):
            with self.assertRaises(s.OperationalError):s.run_expert_batch(bid)
        self.assertEqual(len(s.query('SELECT * FROM expert_reviews')),1)
        item=s.query('SELECT * FROM expert_batch_items WHERE claim_id=?',(a['id'],),True)
        self.assertEqual(item['status'],'completed')
        self.assertEqual(item['review_id'],s.query('SELECT id FROM expert_reviews',one=True)['id'])
        self.assertEqual(s.latest_expert_batch()['done'],1)
        self.assertEqual(s.latest_expert_batch()['status'],'paused')

    def test_pending_batch_protects_ids_from_deduplication(self):
        a=self.claim('A');b=self.claim('A identique');s.create_expert_batch([b['id']])
        self.assertFalse(s.merge_claims(a['id'],b['id'],'Même information'))

    def test_completed_batch_can_be_resumed_without_new_calls(self):
        c=self.claim();bid=s.create_expert_batch([c['id']])
        with patch.object(s,'expert_review',return_value='report') as review:
            s.run_expert_batch(bid);s.run_expert_batch(bid)
            self.assertEqual(review.call_count,1)

    def test_initialization_marks_interrupted_batch_as_paused(self):
        c=self.claim();bid=s.create_expert_batch([c['id']])
        s.query("UPDATE expert_batches SET status='running' WHERE id=?",(bid,));s.init()
        self.assertEqual(s.latest_expert_batch()['status'],'paused')
