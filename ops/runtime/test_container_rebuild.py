import base64,hashlib,importlib.util,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('rebuild_verifier',Path(__file__).with_name('verify-container-rebuild.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class RebuildIntegrity(unittest.TestCase):
    def test_mount_rejects_plain_folder_wrong_volume_and_read_only(self):
        valid=dict(target='/mnt/goodagents',source='expected-nfs',fstype='nfs4',options='rw,relatime')
        for changes in [dict(target='/'),dict(source='other-nfs'),dict(fstype='overlay'),dict(options='ro,relatime')]:
            value=dict(valid,**changes)
            with patch.object(module.subprocess,'check_output',return_value=json.dumps({'filesystems':[value]})):
                with self.assertRaises(RuntimeError):module.require_mount('expected-nfs')
        with patch.object(module.subprocess,'check_output',return_value=json.dumps({'filesystems':[valid]})):
            self.assertEqual(module.require_mount('expected-nfs')['source'],'expected-nfs')

    def test_same_container_and_process_restart_cannot_pass(self):
        before=dict(pod_uid='a',container_id='c',hostname='old')
        with self.assertRaisesRegex(ValueError,'did not change'):module.replacement_kind(before,dict(before,hostname='new',pid=123))
        self.assertEqual(module.replacement_kind(before,dict(before,container_id='d')),'container_recreated_within_same_pod')
        self.assertEqual(module.replacement_kind(before,dict(pod_uid='b',container_id='e')),'pod_recreated')
        with self.assertRaisesRegex(ValueError,'unavailable'):module.replacement_kind({},dict(container_id='f'))

    def test_only_public_identity_fields_are_retained(self):
        claims={'kubernetes.io':{'pod':{'name':'example','uid':'uid'},'namespace':'ns'},'credential':'must-not-retain'}
        payload=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        result=module.identity_claims('header.'+payload+'.signature')
        self.assertEqual(result,dict(pod_uid='uid',pod_name='example',namespace='ns'))
        self.assertNotIn('must-not-retain',json.dumps(result))

    def test_changed_ssh_key_requires_matching_external_attestation(self):
        old={'ssh_host_ed25519_key.pub':dict(fingerprint='SHA256:old')};new={'ssh_host_ed25519_key.pub':dict(fingerprint='SHA256:new')}
        with self.assertRaisesRegex(ValueError,'out-of-band'):module.check_host_keys(old,new)
        with self.assertRaises(ValueError):module.check_host_keys(old,new,dict(method='school_console',fingerprints={'wrong':'SHA256:new'},verified_at='now'))
        evidence=dict(method='school_console',fingerprints={'ssh_host_ed25519_key.pub':'SHA256:new'},verified_at='now')
        self.assertIn('out_of_band',module.check_host_keys(old,new,evidence))
        self.assertEqual(module.check_host_keys(old,{'ssh_host_ed25519_key.pub':dict(fingerprint='SHA256:old',public_file_sha256='comment changed')}),'unchanged')

    def test_restore_corruption_and_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'adapter.safetensors';path.write_bytes(b'original')
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            result=module.hash_records([(path,digest,'restored')]);self.assertEqual(result[str(path)]['sha256'],digest)
            path.write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):module.hash_records([(path,digest,'restored')])
            for value in ['../secret','/etc/shadow','.env']:
                with self.assertRaises(ValueError):module.member(root,value)

    def test_hf_snapshot_symlinks_can_only_reach_persistent_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);root=base/'persistent';folder=root/'snapshot';folder.mkdir(parents=True)
            blob=root/'blobs'/'hash';blob.parent.mkdir();blob.write_bytes(b'weights')
            (folder/'model.bin').symlink_to(blob)
            outside=base/'private';outside.write_bytes(b'private');(folder/'bad.bin').symlink_to(outside)
            with patch.object(module,'ROOT',root):
                self.assertEqual(module.model_member(folder,'model.bin').resolve(),blob.resolve())
                with self.assertRaises(ValueError):module.model_member(folder,'bad.bin')

    def test_post_rebuild_package_drift_is_not_accepted(self):
        before=dict(python='3.11',packages=[dict(name='torch',version='2.13.0+rocm7.1')])
        self.assertTrue(module.same_packages(before,before)['python_and_package_versions_match'])
        for after in [dict(before,python='3.12'),dict(before,packages=[]),dict(before,packages=[dict(name='torch',version='2.9.1+rocm6.4')])]:
            with self.assertRaisesRegex(ValueError,'versions differ'):module.same_packages(before,after)

    def test_canonical_managed_command_must_bind_source_python_and_port(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';python=root/'env/bin/python'
            row=dict(source=str(source),python=str(python),port=8025,adapter=None,command=[str(python),'-u',str(source/'photo_service.py')])
            with patch.object(module,'ROOT',root):
                self.assertEqual(module.photo_record(row),(source,python))
                for changes in [dict(port=8026),dict(port='8025'),dict(adapter='research'),dict(command=[str(python),'other_service.py']),dict(source='relative')]:
                    with self.assertRaises(ValueError):module.photo_record(dict(row,**changes))

    def test_active_pointer_must_agree_with_live_secure_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);active=root/'outputs/runtime/photo-model.active.json';active.parent.mkdir(parents=True)
            python=root/'env/bin/python';python.parent.mkdir(parents=True)
            metadata=python.parent.parent/'runtime-ready.json';lock='a'*64
            ready=dict(profile='secure-rocm71',native_lock_sha256=lock);metadata.write_text(json.dumps(ready))
            runtime=dict(source=str(root/'source'),python=str(python))
            value=dict(runtime,port=8025,instance='photo-model',profile='secure-rocm71',native_lock_sha256=lock)
            with patch.object(module,'ROOT',root):
                self.assertIsNone(module.active_configuration(runtime))
                with self.assertRaises(FileNotFoundError):module.active_configuration(runtime,True)
                active.write_text(json.dumps(value));self.assertEqual(module.active_configuration(runtime,True),active)
                for changes in [dict(source=str(root/'oldsource')),dict(python=str(root/'oldenv/bin/python')),dict(port=8026),dict(instance='candidate'),dict(profile='legacy-rocm64'),dict(native_lock_sha256='b'*64)]:
                    active.write_text(json.dumps(dict(value,**changes)))
                    with self.assertRaises(ValueError):module.active_configuration(runtime,True)
                active.write_text(json.dumps(value));metadata.write_text(json.dumps(dict(ready,profile='legacy-rocm64')))
                with self.assertRaises(ValueError):module.active_configuration(runtime,True)

    def test_every_child_python_scrubs_import_and_preload_overrides(self):
        poisoned={name:'unsafe' for name in ['PYTHONPATH','PYTHONHOME','LD_PRELOAD','LD_LIBRARY_PATH']}
        with patch.dict(os.environ,poisoned),patch.object(module.subprocess,'run') as run:
            run.return_value.stdout='{}';module.package_inventory(Path('/persistent/python'))
            env=run.call_args.kwargs['env']
            for name in poisoned:self.assertNotIn(name,env)
            self.assertEqual(env['PYTHONNOUSERSITE'],'1')
            self.assertEqual(env['TRANSFORMERS_OFFLINE'],'1')

    def test_bge_final_gate_binds_formal_service_active_source_and_both_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir();python=root/'env/bin/python'
            names=['bge_secure_service.py','bounded_http.py','bge-m3.secure.manifest.json','requirements-bge-linux-py311.lock']
            for name in names:(source/name).write_text(name)
            manifest=source/'source-manifest.json';manifest.write_text(json.dumps({name:module.sha(source/name) for name in names}))
            value=dict(source=str(source),python=str(python),service='blink-managed-bge-v1',instance='bge-model',port=8002,
                       command=[str(python),'-u',str(source/'bge_secure_service.py'),'--port','8002'])
            metadata=dict(profile='secure-rocm71',native_lock_sha256='a'*64,python_lock_sha256=module.sha(source/'requirements-bge-linux-py311.lock'))
            active=dict({name:value[name] for name in ['source','python','instance','port']},**metadata,source_manifest_sha256=module.sha(manifest))
            with patch.object(module,'ROOT',root):
                self.assertEqual(module.bge_configuration(value,active,metadata,manifest),(source,python))
                for changes in [dict(port=8027),dict(instance='bge-model-candidate'),dict(service='unmanaged'),dict(command=['unrelated'])]:
                    with self.assertRaises(ValueError):module.bge_configuration(dict(value,**changes),active,metadata,manifest)
                for changes in [dict(source=str(root/'old')),dict(python=str(root/'oldenv/bin/python')),dict(profile='legacy-rocm64'),dict(native_lock_sha256='b'*64),dict(python_lock_sha256='b'*64),dict(source_manifest_sha256='b'*64)]:
                    with self.assertRaises(ValueError):module.bge_configuration(value,dict(active,**changes),metadata,manifest)
                extra=source/'unexpected.py';extra.write_text('unreviewed')
                with self.assertRaisesRegex(ValueError,'Unreviewed files'):module.bge_configuration(value,active,metadata,manifest)
                extra.unlink()
                (source/'bounded_http.py').write_text('modified')
                with self.assertRaisesRegex(ValueError,'source checksum'):module.bge_configuration(value,active,metadata,manifest)

    def test_missing_bge_active_pointer_cannot_satisfy_final_gate(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(module,'ROOT',Path(directory)):
            self.assertIsNone(module.bge_runtime_info())
            with self.assertRaisesRegex(FileNotFoundError,'formal BGE'):module.bge_runtime_info(True)

    def test_public_source_filter_includes_conf_but_never_secret_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'src';source.mkdir();(source/'secrets').mkdir()
            for name in ['runtime.conf','module.py','commands.json','.env','.env.production','private.env','secrets/private.conf']:
                (source/name).write_text('fixture')
            with patch.object(module,'ROOT',root):
                found={str(path.relative_to(source)) for path in module.files_under(source,module.PUBLIC_SOURCE_SUFFIXES)}
            self.assertEqual(found,{'runtime.conf','module.py','commands.json'})

if __name__=='__main__':unittest.main()
