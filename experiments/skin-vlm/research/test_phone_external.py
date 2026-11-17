import json,tempfile,unittest
from pathlib import Path
from research.validate_phone_external import endpoint,response_checks,run
from core import envelope
from types import SimpleNamespace


class ExternalPhoneContracts(unittest.TestCase):
    def observation(self,scope='uncertain'):
        return dict(image_scope=scope,observations=[],limitations=['影像品質限制判讀。'],follow_up_question='請提供清楚照片。')

    def test_safely_rejected_model_output_is_not_usable_accuracy(self):
        result=response_checks(dict(status='invalid_model_output',error='Unsupported tag/region',requires_user_confirmation=True,eligible_for_auto_save=False),False)
        self.assertTrue(result['auto_save_disabled'])
        self.assertFalse(result['schema_valid'])

    def test_technical_retake_needs_valid_empty_observation(self):
        payload=envelope(self.observation(),{})
        self.assertTrue(response_checks(payload,True)['expected_quality_retake'])
        payload['observation']['observations']=[dict(tag='redness',region='cheek',evidence='可見偏紅')]
        self.assertFalse(response_checks(payload,True)['expected_quality_retake'])

    def test_debug_fields_are_detected_at_any_depth(self):
        payload=envelope(self.observation(),{})
        payload['debug']={'image_base64':'do-not-export'}
        self.assertFalse(response_checks(payload,False)['no_private_debug_fields'])

    def test_photos_cannot_be_sent_to_external_or_credential_urls(self):
        for url in ['https://example.com','http://127.0.0.1.evil.example','http://secret@localhost:8025','http://localhost:8025/?token=x']:
            with self.subTest(url=url),self.assertRaises(ValueError):endpoint(url)
        self.assertEqual(endpoint('http://127.0.0.1:8025/'),'http://127.0.0.1:8025')

    def test_changed_frozen_manifest_stops_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'cohort.json').write_text(json.dumps({'changed':True}))
            (root/'cohort.sha256').write_text('0'*64)
            args=SimpleNamespace(cohort=root,output=root/'report.json',endpoint='http://127.0.0.1:8025',transport='private-api')
            with self.assertRaisesRegex(ValueError,'Frozen cohort'):run(args)
            self.assertFalse((root/'report.json').exists())


if __name__=='__main__':unittest.main()
