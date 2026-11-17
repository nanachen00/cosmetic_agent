"""CPU policy checks; no images, GPU, or sealed input contents are read."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from research.app_cycle4_policy import (CONFIGS, SEEDS, TAGS, gate, choose_recipe,
    stability_report, protocol_fingerprint, assert_fingerprint_matches, assert_no_sealed_inputs)


def metrics(f1=.85, json_rate=1., object_rate=1.):
    return dict(valid_json_rate=json_rate, non_skin_correct_rejection_rate=object_rate,
                by_tag={tag:dict(precision=f1, recall=f1, f1=f1) for tag in TAGS})


class AppCycle4Policy(unittest.TestCase):
    def test_fixed_grid_and_api_are_json_serializable(self):
        self.assertEqual([row['variant'] for row in CONFIGS], list('ABCD'))
        self.assertEqual(SEEDS, (20261116,20261117,20261118))
        self.assertEqual([row['aux_weight'] for row in CONFIGS],[0.,.25,.25,.25])
        self.assertEqual([row['rank'] for row in CONFIGS],[8,8,8,16])
        json.dumps({'configs':CONFIGS,'seeds':SEEDS,'gate':gate(metrics())},allow_nan=False)

    def test_eligible_precedes_higher_macro_with_invalid_json(self):
        bad=dict(name='higher-but-invalid',validation=metrics(.96,json_rate=.97),parameters=1)
        good=dict(name='eligible',validation=metrics(.83),parameters=100)
        result=choose_recipe([bad,good])
        self.assertEqual(result['selected']['name'],'eligible')
        self.assertFalse(result['automatic_deployment'])

    def test_missing_object_coverage_is_not_a_pass(self):
        value=metrics();del value['non_skin_correct_rejection_rate']
        self.assertFalse(gate(value)['eligible'])
        self.assertIn('object_rejection_missing_or_invalid',gate(value)['failures'])
        for bad in [None,float('nan'),True]:
            self.assertFalse(gate(dict(value,non_skin_correct_rejection_rate=bad))['eligible'])

    def test_none_eligible_is_diagnostic_only_and_cannot_open_test(self):
        bad=dict(name='diagnostic',validation=metrics(.95,json_rate=.5))
        result=choose_recipe([bad])
        self.assertIsNone(result['selected']);self.assertEqual(result['diagnostic']['name'],'diagnostic')
        self.assertFalse(result['eligible']);self.assertFalse(result['automatic_deployment']);self.assertFalse(result['test_evaluated'])

    def test_tag_failure_and_inflated_macro_are_rejected(self):
        value=metrics();value['by_tag']['flaking']['recall']=.79
        self.assertFalse(gate(value)['eligible'])
        value=metrics();value['macro_f1']=.99
        self.assertIn('macro_f1_inconsistent_with_by_tag',gate(value)['failures'])

    def test_three_fixed_seeds_nonregression_and_median_gain(self):
        rows=[dict(seed=seed,validation=metrics(value)) for seed,value in zip(SEEDS,[.82,.82,.83])]
        result=stability_report(metrics(.8),rows)
        self.assertTrue(result['eligible']);self.assertEqual(result['median_validation_gain'],.02)
        self.assertFalse(result['test_evaluated']);json.dumps(result,allow_nan=False)
        rows[1]['validation']=metrics(.82,json_rate=.97)
        self.assertFalse(stability_report(metrics(.8),rows)['eligible'])
        rows[1]['validation']=metrics(.82)
        self.assertFalse(stability_report(metrics(.84),rows)['eligible'])
        self.assertFalse(stability_report(metrics(.8),rows[:2])['eligible'])
        rows[1]['seed']=SEEDS[0]
        self.assertFalse(stability_report(metrics(.8),rows)['eligible'])

    def test_runtime_change_or_tampered_fingerprint_refuses_reuse(self):
        protocol={'runtime':{'torch':'2.13.0+rocm7.1'},'configs':CONFIGS,'data_sha256':'frozen'}
        stored=copy.deepcopy(protocol);stored['fingerprint']=protocol_fingerprint(protocol)
        self.assertEqual(assert_fingerprint_matches(stored,protocol),stored['fingerprint'])
        changed=copy.deepcopy(protocol);changed['runtime']['torch']='2.9.1+rocm6.4'
        with self.assertRaisesRegex(ValueError,'runtime changed'):
            assert_fingerprint_matches(stored,changed)
        stored['fingerprint']='tampered'
        with self.assertRaisesRegex(ValueError,'inconsistent'):
            assert_fingerprint_matches(stored,protocol)

    def test_precomputed_fingerprints_require_strict_64hex(self):
        protocol={'runtime':{'torch':'2.13.0+rocm7.1'}}
        digest=protocol_fingerprint(protocol)
        self.assertEqual(assert_fingerprint_matches(digest,digest),digest)
        self.assertEqual(assert_fingerprint_matches(digest.upper(),digest),digest)
        self.assertEqual(assert_fingerprint_matches(dict(protocol,fingerprint=digest),digest),digest)
        with self.assertRaisesRegex(ValueError,'runtime changed'):
            assert_fingerprint_matches(digest,'0'*64)
        for invalid in ['x'*64,digest[:-1],digest+'0',' '+digest]:
            with self.subTest(invalid_length=len(invalid)),self.assertRaisesRegex(ValueError,'64hex'):
                assert_fingerprint_matches(digest,invalid)
            with self.subTest(stored_length=len(invalid)),self.assertRaisesRegex(ValueError,'64hex'):
                assert_fingerprint_matches(invalid,digest)

    def test_input_basename_only_guard_never_reads_sealed_files(self):
        with patch('builtins.open',side_effect=AssertionError('No file reads allowed')),patch.object(Path,'read_text',side_effect=AssertionError('No file reads allowed')):
            self.assertFalse(assert_no_sealed_inputs('/data/test-directory/train.jsonl','/reports/test-output/val.jsonl')['sealed_test_inputs'])
            for bad in ['test.jsonl','clean-test.jsonl','OLD48_TEST.JSONL']:
                with self.subTest(bad=bad),self.assertRaisesRegex(ValueError,'Sealed test'):
                    assert_no_sealed_inputs('train.jsonl',bad)
            for train,val in [(None,'val.jsonl'),('train.jsonl',None),('','val.jsonl')]:
                with self.assertRaisesRegex(ValueError,'Both train'):
                    assert_no_sealed_inputs(train,val)


if __name__=='__main__':unittest.main()
