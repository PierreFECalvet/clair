import threading
import time
import unittest
from unittest.mock import patch
import test_pipeline

s=test_pipeline.s

class WatchdogTests(unittest.TestCase):
    def setUp(self):
        test_pipeline.PipelineTests.setUp(self)
        s.JOB.update(status='idle')
    def tearDown(self):
        s.MONITOR_CANCEL.set()
        test_pipeline.PipelineTests.tearDown(self)

    def test_invalid_response_retried_then_completed(self):
        attempts=[]
        def work():
            attempts.append(1)
            if len(attempts)==1:raise s.RetryableDedupError('Réponse invalide')
        with patch.object(s,'WATCH_INTERVAL',.03):
            finished=s.start_job('deduplication',work)
            self.assertTrue(finished.wait(2))
        self.assertEqual(len(attempts),2)
        self.assertEqual(s.JOB['status'],'completed')
        self.assertEqual(s.JOB['auto_retries'],1)
        self.assertIn('full list completed',s.JOB['message'])
        self.assertFalse(s.JOB['watchdog_active'])

    def test_budget_error_not_retried(self):
        attempts=[]
        def work():attempts.append(1);raise s.BudgetError('Plafond atteint')
        with patch.object(s,'WATCH_INTERVAL',.01):
            self.assertTrue(s.start_job('verify',work).wait(2));time.sleep(.04)
        self.assertEqual(attempts,[1]);self.assertEqual(s.JOB['status'],'paused')
        self.assertNotIn('full list completed',s.JOB['message'])

    def test_manual_stop_cancels_scheduled_retry(self):
        attempts=[]
        def work():attempts.append(1);raise s.RetryableDedupError('Réponse invalide')
        with patch.object(s,'WATCH_INTERVAL',.1):
            s.start_job('deduplication',work)
            deadline=time.monotonic()+2
            while not s.JOB.get('retry_pending') and time.monotonic()<deadline:time.sleep(.002)
            self.assertTrue(s.JOB['retry_pending']);s.stop_job();time.sleep(.15)
        self.assertEqual(attempts,[1]);self.assertEqual(s.JOB['status'],'paused')
        self.assertFalse(s.JOB['watchdog_active'])

    def test_monitor_does_not_duplicate_active_worker(self):
        release=threading.Event();attempts=[]
        def work():attempts.append(1);release.wait(2)
        with patch.object(s,'WATCH_INTERVAL',.02):
            finished=s.start_job('deduplication',work)
            try:
                time.sleep(.09);self.assertEqual(attempts,[1]);self.assertEqual(s.JOB['auto_retries'],0)
            finally:release.set();self.assertTrue(finished.wait(2))

    def test_persistent_failure_does_not_claim_completion(self):
        calls=[]
        def work():calls.append(1);raise s.RetryableDedupError('Toujours invalide')
        with patch.object(s,'WATCH_INTERVAL',.02):
            s.start_job('deduplication',work)
            deadline=time.monotonic()+2
            while len(calls)<3 and time.monotonic()<deadline:time.sleep(.005)
            self.assertGreaterEqual(len(calls),3)
            self.assertIsNone(s.JOB.get('list_completed_at'))
            s.stop_job();time.sleep(.05)

