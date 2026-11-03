import unittest
from benchmark import score, bootstrap, overlap

class MetricContracts(unittest.TestCase):
    def setUp(self):
        self.rows=[{'group_id':'a','lesion_present':True},{'group_id':'a','lesion_present':False},{'group_id':'b','lesion_present':True},{'group_id':'b','lesion_present':False}]
    def predictions(self,values):return [{'prediction':v,'seconds':1} for v in values]
    def test_perfect_predictions(self):
        result=score(self.rows,self.predictions([True,False,True,False]))
        self.assertEqual(result['macro_f1'],1);self.assertEqual(result['tp'],2);self.assertEqual(result['tn'],2)
    def test_always_positive_cannot_win_balanced_task(self):
        result=score(self.rows,self.predictions([True]*4))
        self.assertAlmostEqual(result['macro_f1'],1/3);self.assertEqual(result['positive_precision'],.5);self.assertEqual(result['balanced_accuracy'],.5)
    def test_invalid_outputs_not_discarded(self):
        result=score(self.rows,self.predictions([None]*4))
        self.assertEqual(result['macro_f1'],0);self.assertEqual(result['accuracy'],0);self.assertEqual(result['valid_json_rate'],0);self.assertEqual(result['n'],4)
    def test_paired_cluster_bootstrap_same_model_zero(self):
        predictions=self.predictions([True,False,False,True]);ci=bootstrap(self.rows,predictions,predictions,n=100)
        self.assertEqual(ci['lower_95'],0);self.assertEqual(ci['upper_95'],0)
    def test_negative_crop_rejects_partial_intersection(self):
        self.assertTrue(overlap((0,0,10,10),(9,9,20,20)))
        self.assertFalse(overlap((0,0,10,10),(11,11,20,20)))

if __name__=='__main__':unittest.main()
