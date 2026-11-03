import copy,tempfile,unittest
from pathlib import Path
from core import validate,envelope
from prepare_data import assert_no_leakage
from evaluate import score

def observation(scope='face',tags=('redness',)):
    return {'image_scope':scope,'observations':[{'tag':t,'region':'cheek','evidence':'局部可見泛紅'} for t in tags],
            'limitations':['光線可能影響外觀'],'follow_up_question':''}

class Contracts(unittest.TestCase):
    def test_cannot_add_diagnosis(self):
        v=observation();v['diagnosis']='rosacea'
        with self.assertRaises(ValueError): validate(v)
    def test_unknown_tag_rejected(self):
        with self.assertRaises(ValueError): validate(observation(tags=('hydration_80_percent',)))
    def test_non_skin_cannot_have_findings(self):
        with self.assertRaises(ValueError): validate(observation('non_skin'))
    def test_no_autosave(self):
        r=envelope(observation(),{});self.assertFalse(r['eligible_for_auto_save']);self.assertIsNone(r['severity'])
    def test_invalid_output_counts_as_missed_positive(self):
        m=score([{'target':observation()}],[{'status':'invalid_model_output'}])
        self.assertEqual(m['by_tag']['redness']['fn'],1);self.assertEqual(m['valid_json_rate'],0)
    def test_leakage_by_subject_and_image(self):
        with tempfile.TemporaryDirectory() as d:
            a=Path(d)/'a';b=Path(d)/'b';a.write_bytes(b'a');b.write_bytes(b'a')
            r={'id':'a','subject_id':'s1','image':str(a)};s={'id':'b','subject_id':'s2','image':str(b)}
            with self.assertRaises(ValueError): assert_no_leakage([r],[s])
            b.write_bytes(b'b');s['subject_id']='s1'
            with self.assertRaises(ValueError): assert_no_leakage([r],[s])

if __name__=='__main__': unittest.main()
