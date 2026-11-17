import contextlib,hashlib,io,json,random,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from PIL import Image,ImageOps
from research.clean_expansion import image_features,duplicate_clusters,take_holdout,apply_manual_groups,main
from research.appearance_decisions import parse,score,class_weights,KEYS
from research.download_skincon import target_for


class ImageIntegrity(unittest.TestCase):
    def make(self,root,name='a.png',size=240):
        image=Image.fromarray(np.random.default_rng(123).integers(0,256,(size,size,3),dtype=np.uint8))
        path=root/name;image.save(path);return path

    def test_changed_source_bytes_cannot_pass_cleaning(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.make(Path(directory));digest=hashlib.sha256(path.read_bytes()).hexdigest();path.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'checksum'):image_features(path,digest)

    def test_small_protected_image_is_still_deduplicated_but_never_upscaled_for_training(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.make(Path(directory),size=100);digest=hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError,'no_upscale'):image_features(path,digest)
            self.assertEqual(image_features(path,digest,allow_small=True)['width'],100)

    def test_mirror_of_protected_image_stays_in_same_cluster(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);a=self.make(root);b=root/'mirror.png'
            with Image.open(a) as source:ImageOps.mirror(source).save(b)
            items=[dict(id='protected',role='protected',**image_features(a,hashlib.sha256(a.read_bytes()).hexdigest())),
                   dict(id='new',role='new',**image_features(b,hashlib.sha256(b.read_bytes()).hexdigest()))]
            clusters,pairs=duplicate_clusters(items)
            self.assertEqual(len(clusters),1);self.assertEqual(pairs[0]['reason'],'perceptual_or_mirrored')

    def test_manual_group_rejects_changed_or_missing_source_bytes(self):
        record=dict(id='a',sha256='a'*64)
        document=dict(verified_patient_identities=False,groups=[dict(id='capture',reason='Conservative review',
            members=[dict(id='a',sha256='a'*64),dict(id='b',sha256='b'*64)])])
        with self.assertRaisesRegex(ValueError,'missing or checksum changed'):apply_manual_groups([record],document)
        record=dict(id='a',sha256='c'*64)
        with self.assertRaisesRegex(ValueError,'missing or checksum changed'):apply_manual_groups([record,dict(id='b',sha256='b'*64)],document)

    def test_full_cleaning_excludes_protected_images_and_freezes_covered_new_holdouts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);tags=list(KEYS[1:]);manifests={};checks=[]
            for role,count,start in [('existing',20,0),('new',200,20),('protected',4,0)]:
                records=[]
                for n in range(start,start+count):
                    digest=hashlib.sha256(str(n).encode()).hexdigest()
                    records.append(dict(id=str(n),image=str(root/(str(n)+'.png')),image_sha256=digest,
                        source='unit-test-source',license='unit-test-fixture',usage_permitted=True,subject_id_verified=False,
                        label_origin='source_expert_derived',target=target_for([tag for i,tag in enumerate(tags) if n&(1<<i)])))
                    checks.append(dict(sha256=digest,complete=True,policy_version=2,condition_token_hits=[]))
                path=root/(role+'.jsonl');path.write_text(''.join(json.dumps(row)+'\n' for row in records));manifests[role]=path
            ocr=root/'ocr.jsonl';ocr.write_text(''.join(json.dumps(row)+'\n' for row in checks))
            manual=root/'manual-groups.json'
            manual.write_text(json.dumps(dict(verified_patient_identities=False,groups=[dict(id='capture-context',reason='Conservative photo-series review',
                members=[dict(id=str(n),sha256=hashlib.sha256(str(n).encode()).hexdigest()) for n in (0,20)])])))
            def features(path,expected,**kwargs):
                rng=random.Random(str(path));return dict(sha256=expected,normalized_rgb_sha256=expected,
                    dhash64=rng.getrandbits(64),mirrored_dhash64=rng.getrandbits(64),phash63=rng.getrandbits(63),mirrored_phash63=rng.getrandbits(63),quality_flags=[])
            out=root/'cleaned';args=['clean','--existing',str(manifests['existing']),'--new',str(manifests['new']),
                '--protect',str(manifests['protected']),'--ocr',str(ocr),'--manual-groups',str(manual),'--output',str(out)]
            with patch('sys.argv',args),patch('research.clean_expansion.require_reviewed_labels'),patch('research.clean_expansion.image_features',side_effect=features),contextlib.redirect_stdout(io.StringIO()):main()
            info=json.loads((out/'split_info.json').read_text())
            self.assertEqual(info['status_counts']['protected'],4)
            self.assertEqual(info['reason_counts']['matches_prior_holdout'],5)
            for split in ('val','test'):
                rows=[json.loads(line) for line in (out/(split+'.jsonl')).read_text().splitlines()]
                self.assertTrue(all(row['fresh_source_image'] for row in rows))
                self.assertTrue(all(count>=12 for count in info['counts'][split]['tags'].values()))
            with patch('sys.argv',args):
                with self.assertRaises(FileExistsError):main()


class DecisionContracts(unittest.TestCase):
    def test_invalid_output_is_not_rewarded_as_correct_object_rejection(self):
        row={'target':dict(image_scope='non_skin',observations=[])}
        result=score([row],[{'decision':None}])
        self.assertEqual(result['valid_json_rate'],0)
        self.assertEqual(result['non_skin_rejection_rate'],0)
        self.assertEqual(result['by_tag']['redness']['fp'],1)

    def test_non_skin_cannot_produce_positive_appearance_tags(self):
        value={key:False for key in KEYS};value['redness']=True
        with self.assertRaisesRegex(ValueError,'Non-skin'):parse(json.dumps(value))

    def test_no_string_booleans_or_diagnostic_fields(self):
        value={key:False for key in KEYS};value['flaking']='false'
        with self.assertRaises(ValueError):parse(json.dumps(value))
        value={key:False for key in KEYS};value['diagnosis']='anything'
        with self.assertRaises(ValueError):parse(json.dumps(value))

    def test_class_weights_require_both_skin_and_object_coverage(self):
        with self.assertRaisesRegex(ValueError,'lacks both'):class_weights([{'target':target_for(['redness'])}],True)


if __name__=='__main__':unittest.main()
