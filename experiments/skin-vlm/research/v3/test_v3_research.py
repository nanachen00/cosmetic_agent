import json
import math
import unittest

import numpy as np
from PIL import Image

from research.v3.facial_research import (build_qwen_evidence, controlled_variants, delta_e_2000,
    detection_metrics, iou, multiview_consistency, personal_baseline, robust_roi_lab,
    srgb_to_lab)
from research.v3.error_analysis import paired_audit, choose_validation_candidate
from research.v3.gpu_monitor import summarize


class ColourTests(unittest.TestCase):
    def test_srgb_reference_endpoints(self):
        self.assertAlmostEqual(srgb_to_lab((255,255,255))[0],100,places=4)
        self.assertAlmostEqual(srgb_to_lab((0,0,0))[0],0,places=4)
    def test_ciede2000_reference_pair(self):
        self.assertAlmostEqual(delta_e_2000((50,2.6772,-79.7751),(50,0,-82.7485)),2.0425,places=4)
    def test_explicit_roi_required_and_usable(self):
        im=Image.new("RGB",(20,20),(180,100,90));mask=np.ones((20,20),dtype=bool)
        self.assertEqual(robust_roi_lab(im,mask)["usable_pixels"],400)
        with self.assertRaises(ValueError): robust_roi_lab(im,np.ones((5,5),dtype=bool))
    def test_control_matrix_is_predeclared(self):
        names=set(controlled_variants(Image.new("RGB",(16,16),(100,100,100))))
        self.assertEqual(names,{"clean","exposure_ev_minus_1","exposure_ev_plus_1","white_balance_warm",
                                "white_balance_cool","blur_radius_2","directional_shadow","warm_plus_shadow"})


class DetectionTests(unittest.TestCase):
    def test_iou(self):
        self.assertEqual(iou((0,0,10,10),(0,0,10,10)),1)
        self.assertEqual(iou((0,0,10,10),(20,20,30,30)),0)
    def test_perfect_ap(self):
        m=detection_metrics({"a":[[0,0,10,10]]},{"a":[{"box":[0,0,10,10],"score":.9}]})
        self.assertEqual(m["ap"],1);self.assertEqual(m["count_mae"],0)
    def test_false_positive_affects_precision_and_count(self):
        m=detection_metrics({"a":[[0,0,10,10]]},{"a":[{"box":[0,0,10,10],"score":.9},{"box":[20,20,30,30],"score":.8}]})
        self.assertEqual(m["precision"],.5);self.assertEqual(m["count_mae"],1)


class MultiViewAndBaselineTests(unittest.TestCase):
    def test_three_views_required(self):
        with self.assertRaises(ValueError): multiview_consistency({"front":[],"left":[]})
    def test_repetition(self):
        f={"tag":"acne_like_bumps","region":"left_cheek","evidence":"visible"}
        r=multiview_consistency({"front":[f],"left":[f],"right":[]})
        self.assertEqual(r["findings"][0]["status"],"repeated")
    def records(self):
        return [{"subject_id":"s","device_id":"d","capture_profile":"p","region":"left_cheek",
                 "captured_date":str(i),"lesion_count":i,"redness_a_star":10+i} for i in (1,2,3)]
    def test_baseline_needs_three_dates(self):
        current=dict(self.records()[0],captured_date="4",lesion_count=4,redness_a_star=14)
        self.assertEqual(personal_baseline(self.records()[:2],current)["status"],"insufficient_history")
        self.assertEqual(personal_baseline(self.records(),current)["status"],"relative_change_only")
    def test_qwen_packet_cannot_serialize_nan(self):
        with self.assertRaises(ValueError): build_qwen_evidence({"x":math.nan},{},{},{})


class ErrorAnalysisTests(unittest.TestCase):
    def test_detects_conservative_shift(self):
        base=[{"id":"p1","group_id":"g1","label":True,"prediction":True},
              {"id":"p2","group_id":"g2","label":True,"prediction":True},
              {"id":"n1","group_id":"g3","label":False,"prediction":True},
              {"id":"n2","group_id":"g4","label":False,"prediction":False}]
        new=[{"id":"p1","group_id":"g1","label":True,"prediction":True},
             {"id":"p2","group_id":"g2","label":True,"prediction":False},
             {"id":"n1","group_id":"g3","label":False,"prediction":False},
             {"id":"n2","group_id":"g4","label":False,"prediction":False}]
        self.assertEqual(paired_audit(base,new)["failure_pattern"],"candidate_became_too_conservative")
    def test_selection_refuses_test_metrics(self):
        with self.assertRaises(ValueError): choose_validation_candidate([{"name":"x","split":"test","metrics":{}}])
    def test_selection_enforces_recall_floor(self):
        result=choose_validation_candidate([{"name":"x","split":"validation","metrics":{
            "valid_json_rate":1,"precision":.95,"recall":.7,"macro_f1":.9}}])
        self.assertIsNone(result["selected"])
    def test_gpu_summary_preserves_vendor_metric_names(self):
        rows=[{"elapsed_seconds":0,"device_cli":{"metrics":{"card0.GPU use (%)":10,"card0.Temperature":50}}},
              {"elapsed_seconds":2,"device_cli":{"metrics":{"card0.GPU use (%)":90,"card0.Temperature":70}}}]
        result=summarize(rows,["python3","train.py"],0)
        self.assertEqual(result["samples"],2)
        self.assertEqual(result["telemetry_series"]["card0.GPU use (%)"]["mean"],50)


if __name__=="__main__": unittest.main()

