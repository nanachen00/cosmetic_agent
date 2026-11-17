import hashlib,json,tempfile,unittest
from pathlib import Path
from research.v3.finalize_cycle3 import locked_rows


class LockedTestIntegrity(unittest.TestCase):
    def fixture(self,root):
        for name in ('train','validation','test'):
            image=root/(name+'.png');image.write_bytes(name.encode())
            row=dict(id=name,group_id=name,image=str(image),sha256=hashlib.sha256(image.read_bytes()).hexdigest(),lesion_present=True)
            (root/(name+'.jsonl')).write_text(json.dumps(row)+'\n')
        return {'counts':{'test':{'sha256':hashlib.sha256((root/'test.jsonl').read_bytes()).hexdigest()}}}

    def test_changed_test_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);meta=self.fixture(root)
            (root/'test.png').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'image changed'):locked_rows(root,meta)

    def test_historical_group_cannot_enter_new_test(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);meta=self.fixture(root);meta['forbidden_locked_test_groups']=['test']
            with self.assertRaisesRegex(ValueError,'overlaps'):locked_rows(root,meta)

    def test_valid_locked_data_and_modified_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);meta=self.fixture(root)
            self.assertEqual(locked_rows(root,meta)[0]['id'],'test')
            with (root/'test.jsonl').open('a') as stream:stream.write('\n')
            with self.assertRaisesRegex(ValueError,'manifest changed'):locked_rows(root,meta)


if __name__=='__main__':unittest.main()
