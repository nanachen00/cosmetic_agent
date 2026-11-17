"""CPU-only generated-token diagnostics; unchanged generation contract."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import torch
from skin_vlm import SkinVLM


class Batch(dict):
    def to(self,device):return self


class Diagnostics(unittest.TestCase):
    def engine(self,text=None,tokens=None):
        text=text or json.dumps(dict(image_scope='skin_closeup',observations=[],limitations=['光線會影響觀察。'],follow_up_question=''))
        calls=[]
        def generate(**kwargs):calls.append(kwargs);return torch.tensor([[1,2]+(tokens if tokens is not None else [3,4,9])])
        engine=SkinVLM.__new__(SkinVLM);engine.torch=torch
        engine.processor=SimpleNamespace(apply_chat_template=lambda *args,**kwargs:Batch(input_ids=torch.tensor([[1,2]])),
            batch_decode=lambda *args,**kwargs:[text],tokenizer=SimpleNamespace(eos_token_id=9))
        engine.model=SimpleNamespace(device='cpu',generate=generate,generation_config=SimpleNamespace(eos_token_id=[9,10]))
        return engine,calls

    def test_numeric_private_diagnostics_track_eos_budget_and_original_arguments(self):
        engine,calls=self.engine()
        with patch('skin_vlm.open_image_bytes',return_value=(object(),{'reasons':[]})), \
                patch('torch.cuda.is_available',return_value=True),patch('torch.cuda.synchronize') as sync, \
                patch('skin_vlm.time.perf_counter',side_effect=[0.,61.2,62.4,63.]):
            result=engine.analyze_bytes(b'private image','private note')
        d=engine.last_diagnostics
        self.assertEqual(d['generated_tokens'],3);self.assertTrue(d['eos_seen']);self.assertTrue(d['eos_at_end'])
        self.assertTrue(d['time_budget_reached']);self.assertFalse(d['token_cap_reached']);self.assertTrue(d['valid_output'])
        self.assertEqual(d['generate_seconds'],61.2);self.assertEqual(d['synchronize_seconds'],1.2)
        self.assertEqual((calls[0]['max_new_tokens'],calls[0]['max_time'],calls[0]['do_sample'],calls[0]['repetition_penalty']),(400,60,False,1.05))
        self.assertNotIn('generated_tokens',result);sync.assert_called_once()
        self.assertTrue(all(type(value) in {bool,int,float} for value in d.values()))

    def test_invalid_output_without_eos_and_token_cap_are_observable(self):
        engine,_=self.engine(text='not JSON',tokens=[3]*400)
        with patch('skin_vlm.open_image_bytes',return_value=(object(),{'reasons':[]})),patch('torch.cuda.is_available',return_value=False):
            result=engine.analyze_bytes(b'fixture')
        self.assertEqual(result['status'],'invalid_model_output');self.assertFalse(engine.last_diagnostics['valid_output'])
        self.assertTrue(engine.last_diagnostics['token_cap_reached']);self.assertFalse(engine.last_diagnostics['eos_seen'])

    def test_quality_rejected_call_clears_previous_generation_diagnostics(self):
        engine,_=self.engine();engine.last_diagnostics={'generated_tokens':400,'valid_output':True}
        with patch('skin_vlm.open_image_bytes',return_value=(object(),{'reasons':['low_contrast']})):
            result=engine.analyze_bytes(b'fixture')
        self.assertEqual(result['status'],'needs_retake');self.assertEqual(engine.last_diagnostics['generated_tokens'],0)
        self.assertEqual(engine.last_diagnostics['generate_seconds'],0.);self.assertFalse(engine.last_diagnostics['valid_output'])


if __name__=='__main__':unittest.main()
