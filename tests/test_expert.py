import json
import unittest
from unittest.mock import patch
import test_pipeline

s=test_pipeline.s

class ExpertTests(unittest.TestCase):
    def setUp(self):
        test_pipeline.PipelineTests.setUp(self)
        s.CONFIG.update(review_model='chosen-advanced-model',openai_key='test-only',provider='lmstudio',model='local-basic')
    tearDown=test_pipeline.PipelineTests.tearDown
    claim=test_pipeline.PipelineTests.claim
    evidence=test_pipeline.PipelineTests.evidence

    def attach(self,c):
        v=s.add_content('texte:lot:expert','Google leaks de 2024',c['text']+'\nContexte historique de la documentation.')
        chunk=s.query('SELECT id FROM chunks WHERE version_id=?',(v,),True)['id']
        s.query('INSERT INTO occurrences VALUES(?,?,?,?,?)',(s.uid(),c['id'],chunk,c['text'],'[]'));return v

    def result(self,eid='e',quote='Documentation du champ hostAge.'):
        citations=[{'evidence_id':eid,'quote':quote}]
        return {'contextualized_claim':'La fuite des Google leaks de 2024 documente hostAge.',
                'verification_prompt':'Vérifie la documentation, la priorité relative et le sens du mot preuve.',
                'assessment':'La priorité relative exige un examen historique, distinct du déploiement.',
                'points':[{'claim':'hostAge est documenté','verdict':'supported','reason':'Documentation interne','citations':citations}],
                'proposed_correction':'La documentation décrit hostAge.','same_subject':True,'correction_supported':True,
                'correction_citations':citations,'remaining_questions':[],'search_queries':[]}

    def test_openai_override_does_not_modify_default_model(self):
        response={'output':[{'content':[{'type':'output_text','text':'{"ok":true}'}]}]}
        before=dict(s.CONFIG)
        with patch.object(s,'http_json',return_value=(response,{})) as request:
            s.llm('test',{},provider='openai',model='chosen-advanced-model')
        self.assertEqual(request.call_args.args[0],'https://api.openai.com/v1/responses')
        self.assertEqual(request.call_args.args[1]['model'],'chosen-advanced-model')
        self.assertEqual(s.CONFIG,before)

    def test_context_proposal_and_sources_sent_without_ibou(self):
        c=self.claim('La fuite de 2024 documente hostAge.');self.attach(c)
        eid=self.evidence(c['id'],'Documentation du champ hostAge.')
        proposal='Une des premières preuves publiques de ces mécanismes.'
        with patch.object(s,'llm',return_value=self.result(eid)) as llm,patch.object(s,'ibou') as ibou:
            rid=s.expert_review(c['id'],'Google leaks 2024',proposal,[]);ibou.assert_not_called()
        payload=llm.call_args.args[1]
        self.assertEqual(payload['user_proposal'],proposal)
        self.assertEqual(payload['documents'][0]['title'],'Google leaks de 2024')
        self.assertEqual(payload['occurrences'][0]['quote'],c['text'])
        self.assertEqual(llm.call_args.kwargs,{'provider':'openai','model':'chosen-advanced-model'})
        self.assertIn('priorité relative',llm.call_args.args[0])
        self.assertEqual(s.query('SELECT status FROM claims',one=True)['status'],'pending')
        self.assertEqual(s.query('SELECT * FROM patches'),[])
        self.assertTrue(json.loads(s.query('SELECT result FROM expert_reviews WHERE id=?',(rid,),True)['result'])['eligible_for_patches'])

    def test_hallucinated_quotes_block_patch_preparation(self):
        c=self.claim();eid=self.evidence(c['id'])
        with patch.object(s,'llm',return_value=self.result(eid,'Citation inventée')):
            rid=s.expert_review(c['id'])
        result=json.loads(s.query('SELECT result FROM expert_reviews',one=True)['result'])
        self.assertFalse(result['eligible_for_patches']);self.assertEqual(result['points'][0]['verdict'],'unestablished')
        with self.assertRaises(ValueError):s.expert_patches(rid)

    def test_wrong_subject_cannot_become_a_patch(self):
        c=self.claim();eid=self.evidence(c['id'],'Documentation du champ hostAge.')
        result=self.result(eid);result['same_subject']=False
        with patch.object(s,'llm',return_value=result):s.expert_review(c['id'])
        self.assertFalse(json.loads(s.query('SELECT result FROM expert_reviews',one=True)['result'])['eligible_for_patches'])

    def test_inaccessible_source_is_reported_not_fabricated(self):
        c=self.claim()
        with patch.object(s,'fetch_article_source',return_value={'status':'unavailable','error':'PDF non pris en charge'}),patch.object(s,'llm',return_value=self.result()):
            s.expert_review(c['id'],urls=['https://source.example/p.pdf'])
        review=s.query('SELECT * FROM expert_reviews',one=True);payload=json.loads(review['input'])
        self.assertEqual(payload['evidence'],[])
        self.assertEqual(payload['source_report'][0]['status'],'unavailable')
        self.assertFalse(json.loads(review['result'])['eligible_for_patches'])

    def test_patches_are_drafts_using_advanced_model_and_replacement_schema(self):
        c=self.claim('La fuite de 2024 documente hostAge.');v=self.attach(c);eid=self.evidence(c['id'],'Documentation du champ hostAge.')
        with patch.object(s,'llm',return_value=self.result(eid)):rid=s.expert_review(c['id'])
        with patch.object(s,'llm',return_value={'replacement':'La documentation décrit hostAge.'}) as llm:
            s.expert_patches(rid);s.expert_patches(rid)
        self.assertEqual(llm.call_count,1)
        self.assertEqual(s.output_schema(llm.call_args.args[1])['required'],['replacement'])
        self.assertEqual(llm.call_args.kwargs['model'],'chosen-advanced-model')
        self.assertEqual(s.query('SELECT status FROM patches',one=True)['status'],'proposed')
        self.assertIn(c['text'],s.query('SELECT text FROM versions WHERE id=?',(v,),True)['text'])

    def test_changed_occurrences_require_new_review(self):
        c=self.claim();eid=self.evidence(c['id'],'Documentation du champ hostAge.')
        with patch.object(s,'llm',return_value=self.result(eid)):rid=s.expert_review(c['id'])
        self.attach(c)
        with self.assertRaises(ValueError):s.expert_patches(rid)

    def test_explicit_model_required(self):
        c=self.claim();s.CONFIG['review_model']=''
        with patch.object(s,'llm') as llm:
            with self.assertRaises(s.OperationalError):s.expert_review(c['id'])
            llm.assert_not_called()

    def test_document_citation_can_establish_fidelity_not_external_truth(self):
        c=self.claim('Le texte présente hostAge.');vid=self.attach(c)
        eid=self.evidence(c['id'],'Documentation du champ hostAge.')
        result=self.result(eid)
        local={'evidence_id':vid,'quote':c['text'],'scope':'context'}
        result['points'].append({'claim':'Le texte mentionne hostAge','scope':'context','verdict':'supported','reason':'Fidélité','citations':[local]})
        result['correction_citations']=[*result['correction_citations'],local]
        with patch.object(s,'llm',return_value=result):s.expert_review(c['id'])
        saved=json.loads(s.query('SELECT result FROM expert_reviews',one=True)['result'])
        self.assertTrue(saved['eligible_for_patches'])
        self.assertEqual(saved['points'][-1]['verdict'],'supported')

    def test_local_document_cannot_prove_external_claim(self):
        c=self.claim('Le texte présente hostAge.');vid=self.attach(c)
        result=self.result(vid,c['text'])
        result['points'][0]['scope']='external'
        result['points'][0]['citations'][0]['scope']='context'
        with patch.object(s,'llm',return_value=result):s.expert_review(c['id'])
        saved=json.loads(s.query('SELECT result FROM expert_reviews',one=True)['result'])
        self.assertFalse(saved['eligible_for_patches'])
        self.assertEqual(saved['points'][0]['verdict'],'unestablished')

    def test_correction_with_only_local_sources_stays_draft(self):
        c=self.claim('Le texte présente hostAge.');vid=self.attach(c)
        result=self.result(vid,c['text'])
        result['points'][0]['scope']='context'
        result['points'][0]['citations'][0]['scope']='context'
        with patch.object(s,'llm',return_value=result):s.expert_review(c['id'])
        self.assertFalse(json.loads(s.query('SELECT result FROM expert_reviews',one=True)['result'])['eligible_for_patches'])
