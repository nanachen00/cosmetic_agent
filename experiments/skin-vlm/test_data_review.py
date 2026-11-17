import unittest

from core import validate
from data_review import group_source_images, require_reviewed_labels
from research.download_skincon import MAPPING, appearance_tags, target_for


class DataReviewTests(unittest.TestCase):
    def test_pending_scin_labels_never_become_training_gold(self):
        with self.assertRaisesRegex(ValueError,'pending/AI'):
            require_reviewed_labels([{'label_origin':'appearance_review_pending'}],True)

    def test_expert_opt_in_does_not_accept_missing_provenance(self):
        with self.assertRaisesRegex(ValueError,'provenance'):
            require_reviewed_labels([{'label_origin':'source_expert_derived'}],True)

    def test_unknown_clinical_concepts_are_not_treated_as_negatives(self):
        row={key:'0' for values in MAPPING.values() for key in values}
        row.update({'Do not consider this image':'0','Scale':'unknown'})
        with self.assertRaisesRegex(ValueError,'Unknown source'):
            appearance_tags(row)

    def test_source_exclusion_is_preserved(self):
        with self.assertRaisesRegex(ValueError,'excluded'):
            appearance_tags({'Do not consider this image':'1'})

    def test_mapped_appearance_contract_never_invents_location_or_diagnosis(self):
        target=validate(target_for(list(MAPPING)))
        self.assertEqual({finding['region'] for finding in target['observations']},{'unknown'})
        self.assertNotIn('diagnosis',target)

    def test_near_duplicates_grouped_without_fabricating_person_ids(self):
        rows=[{'rgb_sha256':'a','dhash64':0,'source_image_md5':'a'},
              {'rgb_sha256':'b','dhash64':1,'source_image_md5':'b'},
              {'rgb_sha256':'c','dhash64':2**64-1,'source_image_md5':'c'}]
        grouped=group_source_images(rows)
        self.assertEqual(grouped[0]['subject_id'],grouped[1]['subject_id'])
        self.assertNotEqual(grouped[0]['subject_id'],grouped[2]['subject_id'])
        self.assertTrue(all(row['subject_id_verified'] is False for row in grouped))


if __name__=='__main__':unittest.main()
