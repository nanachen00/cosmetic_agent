import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import TAGS
from research.train_app_comparison import candidate_grid,main,summarize


class SelectionTests(unittest.TestCase):
    def test_visual_study_changes_only_declared_modules_and_epochs(self):
        grid=candidate_grid('visual-merger')
        self.assertEqual([row['epochs'] for row in grid],[1,2])
        self.assertTrue(all(row['modules']=='language_attention_mlp_visual_merger' for row in grid))
        with self.assertRaises(ValueError):candidate_grid('unknown')

    def metrics(self):
        return {'valid_json_rate':1.,'by_tag':{tag:{'precision':1.,'recall':1.,'f1':1.} for tag in TAGS}}

    def test_high_macro_cannot_hide_one_tag_missing_most_positives(self):
        metrics=self.metrics()
        metrics['by_tag']['flaking']={'precision':1.,'recall':.2,'f1':1/3}
        result=summarize(metrics)
        self.assertGreater(result['macro_f1'],.8)
        self.assertFalse(result['eligible'])
        self.assertIn('flaking:recall_below_0.80',result['failures'])

    def test_invalid_json_rate_is_a_separate_gate(self):
        metrics=self.metrics();metrics['valid_json_rate']=.97
        self.assertFalse(summarize(metrics)['eligible'])

    def test_unsupported_empty_tag_metrics_stop_comparison(self):
        metrics=self.metrics();metrics['by_tag']['redness']['f1']=None
        with self.assertRaises(ValueError):summarize(metrics)

    def test_controller_completes_workflow_and_resumes_without_rerunning_children(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);data=root/'data';model=root/'model';data.mkdir();model.mkdir()
            hashes={}
            for split in ('train','val','test'):
                path=data/(split+'.jsonl');path.write_text('{}\n')
                hashes[split]=hashlib.sha256(path.read_bytes()).hexdigest()
            (data/'split_info.json').write_text(json.dumps({'manifest_sha256':hashes}))
            (model/'artifact-metadata.json').write_text('{"verified":true}')
            rows=[{'label_origin':'human_verified','target':{'observations':[{'tag':tag} for tag in TAGS]}},
                  {'label_origin':'human_verified','target':{'observations':[]}}]
            commands=[]
            def child(command,**kwargs):
                self.assertTrue(all(isinstance(value,str) for value in command))
                commands.append(command)
                if Path(command[2]).name=='train_lora.py':
                    out=Path(command[command.index('--out')+1]);out.mkdir(parents=True)
                    (out/'adapter_model.safetensors').write_bytes(b'explicit-unit-test-fixture')
                    (out/'training_summary.json').write_text('{"trainable_parameters":100}')
                else:
                    out=Path(command[command.index('--output')+1]);out.parent.mkdir(parents=True,exist_ok=True)
                    metrics=self.metrics()
                    if '--adapter' not in command:
                        for values in metrics['by_tag'].values():values.update(precision=.5,recall=.5,f1=.5)
                    out.write_text(json.dumps({'metrics':metrics}))
            args=['compare','--data',str(data),'--model',str(model),'--output',str(root/'checkpoints'),
                  '--report',str(root/'reports'),'--backup-root',str(root/'backups')]
            with patch('sys.argv',args),patch('research.train_app_comparison.require_persistent_mount'), \
                 patch('research.train_app_comparison.load_rows',return_value=rows), \
                 patch('research.train_app_comparison.subprocess.run',side_effect=child),contextlib.redirect_stdout(io.StringIO()):
                main()
                result=json.loads((root/'reports/selection.json').read_text())
                self.assertEqual(result['selected'],'app-r8-lr5e-5')
                self.assertTrue(result['test_evaluated'])
                count=len(commands);main();self.assertEqual(len(commands),count)


if __name__=='__main__':unittest.main()
