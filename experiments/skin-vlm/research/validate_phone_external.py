"""Frozen public-photo functional validation, separate from clinical accuracy.

SCIN appearance labels are not expert gold. This runner tests response contracts,
privacy fields and objective image-quality rejection, never phenotype accuracy.
Prepared JPEGs use Pillow, not the browser's canvas implementation.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import http.cookiejar
import json
import secrets
import statistics
import sys
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import validate


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    temporary = path.with_suffix(path.suffix + '.partial')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def prepare(args):
    from PIL import Image, ImageOps, ImageStat, __version__ as pillow_version

    if args.output.exists():
        raise FileExistsError('Use a fresh frozen cohort directory')
    metadata = json.loads(args.split_info.read_text())
    excluded = set()
    inputs = {}
    for split in ('train', 'val', 'test'):
        path = getattr(args, split)
        if sha(path) != metadata['manifest_sha256'][split]:
            raise ValueError('Frozen split changed: ' + split)
        inputs[split] = sha(path)
        excluded.update(json.loads(line)['image_sha256'] for line in path.read_text().splitlines() if line.strip())
    source_audit = [json.loads(line) for line in args.source_audit.read_text().splitlines() if line.strip()]
    accepted_clusters = {row['cluster_id'] for row in source_audit if row['status'] == 'accepted'}
    by_id = {row['id']: row for row in source_audit if row['role'] == 'review_pending'}
    index = json.loads(args.index.read_text())
    notes = json.loads(args.quality_notes.read_text())
    license_path = Path(index[0]['image']).parents[2] / 'SCIN-LICENSE.txt'
    license_data = license_path.read_bytes()
    clear = [row[0] for row in notes['rows'] if row[4] == 'face' and not row[3]][:10]
    if len(clear) != 10:
        raise ValueError('Need ten quality-reviewed face candidates')
    partial = args.output.with_name(args.output.name + '.partial')
    if partial.exists():
        raise FileExistsError('Incomplete cohort output already exists')
    (partial / 'images').mkdir(parents=True)
    candidates = []
    for n, entry in enumerate(index):
        original = Path(entry['image'])
        if sha(original) != entry['sha256'] or entry['sha256'] in excluded:
            raise ValueError('Changed or previously used source image')
        audit = by_id[entry['id']]
        if audit['cluster_id'] in accepted_clusters:
            raise ValueError('Source perceptual cluster overlaps the training study')
        with Image.open(original) as raw:
            image = ImageOps.exif_transpose(raw).convert('RGB')
            ratio = min(1., 1280 / max(image.size))
            if ratio < 1:
                image = image.resize(tuple(round(v * ratio) for v in image.size), Image.Resampling.LANCZOS)
            target = partial / 'images' / ('%03d.jpg' % n)
            image.save(target, format='JPEG', quality=90)
        with Image.open(target) as image:
            stats = ImageStat.Stat(image.convert('L'))
            mean, contrast = stats.mean[0], stats.stddev[0]
            reasons = []
            if min(image.size) < 224: reasons.append('too_small')
            if mean < 30: reasons.append('too_dark')
            if mean > 235: reasons.append('too_bright')
            if contrast < 5: reasons.append('low_contrast')
            dimensions = list(image.size)
        prepared_sha = sha(target)
        if prepared_sha in excluded:
            raise ValueError('Prepared JPEG overlaps a frozen split')
        candidates.append(dict(id=entry['id'],review_index=n,original_image=str(original),
            original_sha256=entry['sha256'],prepared_image='images/' + target.name,
            prepared_sha256=prepared_sha,prepared_dimensions=dimensions,
            prepared_bytes=target.stat().st_size,brightness=round(mean, 4),contrast=round(contrast, 4),
            source_cluster=audit['cluster_id'],expected_heuristic_retake=bool(reasons),
            heuristic_reasons=reasons,appearance_gold=None))
    poor = [row['review_index'] for row in candidates if row['expected_heuristic_retake'] and row['review_index'] not in clear][:2]
    if len(poor) != 2:
        raise ValueError('Need two distinct sources that remain objectively poor after JPEG preprocessing')
    chosen = [row for row in candidates if row['review_index'] in clear + poor]
    if len({row['source_cluster'] for row in chosen}) != 12:
        raise ValueError('External sources are not twelve distinct source-image clusters')
    for row in candidates:
        if row not in chosen:
            (partial / row['prepared_image']).unlink()
    cohort = dict(version=1,source='https://github.com/google-research-datasets/scin',
        license='SCIN Data Use License',source_image_groups_not_verified_people=True,
        independent_phone_device_ids_available=False,expert_appearance_gold_available=False,
        selection='First ten AI quality-reviewed face candidates with no quality flags, plus first two other sources meeting objective post-JPEG retake thresholds; no model predictions or phenotype labels select cases.',
        preprocessing=dict(engine='Pillow',version=pillow_version,operations=['EXIF transpose','RGB','long side <=1280 without upscaling','JPEG quality 90'],browser_canvas_equivalence_claimed=False),
        excluded_frozen_split_sha256=inputs,original_and_prepared_image_hash_overlap=0,
        source_perceptual_cluster_overlap=0,source_index_sha256=sha(args.index),
        source_quality_notes_sha256=sha(args.quality_notes),source_audit_sha256=sha(args.source_audit),
        license_sha256=hashlib.sha256(license_data).hexdigest(),
        appearance_accuracy_measured=False,clinical_validation=False,rows=chosen)
    (partial / 'SCIN-LICENSE.txt').write_bytes(license_data)
    write(partial / 'cohort.json', cohort)
    (partial / 'cohort.sha256').write_text(sha(partial / 'cohort.json') + '\n')
    partial.rename(args.output)
    print(json.dumps({'cohort':str(args.output),'images':len(chosen),'objective_retake_cases':len(poor),'cohort_sha256':sha(args.output/'cohort.json')}))


def endpoint(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'http' or parsed.hostname not in {'localhost','127.0.0.1'} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'','/'}:
        raise ValueError('Only an explicitly forwarded local HTTP endpoint is allowed')
    return value.rstrip('/')


def call(opener, url, body=None, origin=None):
    headers = {'Accept':'application/json'}
    if body is not None: headers['Content-Type'] = 'application/json'
    if origin: headers['Origin'] = origin
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    start = time.monotonic()
    try:
        response = opener.open(request, timeout=100)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024: raise ValueError('Oversized response')
        try: payload = json.loads(raw)
        except ValueError: payload = {'error':'non_json_response'}
        return response.status, payload, round(time.monotonic()-start, 4)


@contextmanager
def website_member(base, backend_root, auth_report):
    """Disposable local fixture; never invokes registration, SMTP or OAuth."""
    from dotenv import dotenv_values
    import psycopg
    from psycopg.rows import dict_row
    sys.path.insert(0, str(backend_root))
    from database.postgres.repositories.password_auth import PostgresPasswordAuthRepository
    database_url = dotenv_values(backend_root / '.env').get('DATABASE_URL', '')
    parsed = urllib.parse.urlsplit(database_url)
    if parsed.scheme not in {'postgres','postgresql'} or parsed.hostname not in {'localhost','127.0.0.1'}:
        raise ValueError('Fixture creation is restricted to the configured local PostgreSQL')
    @contextmanager
    def connection():
        with psycopg.connect(database_url, connect_timeout=5, row_factory=dict_row) as conn:
            yield conn
    username = 'phone_' + uuid4().hex[:12]
    password = secrets.token_urlsafe(24) + 'Aa1'
    account = None
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    auth_report.update(scope='Disposable verified-email local DB fixture; real website password login/session; no SMTP, registration or OAuth verification', fixture_removed=False)
    try:
        account = PostgresPasswordAuthRepository(connection_factory=connection).register(
            username=username, verified_email=username+'@example.invalid', password=password)
        status, _, _ = call(opener, base+'/api/auth/password/login', {'username':username,'password':password},base)
        auth_report['login_status'] = status
        auth_report['cookie_received'] = bool(list(jar))
        if status != 200 or not list(jar): raise RuntimeError('Fixture login failed')
        status, session, _ = call(opener, base+'/api/auth/session')
        identity = session.get('account',session)
        auth_report.update(session_status=status,session_identity_matches=str(identity.get('user_id'))==str(account.user_id))
        if status != 200 or not auth_report['session_identity_matches']: raise RuntimeError('Fixture session mismatch')
        yield opener
    finally:
        jar.clear()
        if account is not None:
            with connection() as conn:
                count = conn.execute('DELETE FROM users WHERE id=%s AND EXISTS (SELECT 1 FROM usernames WHERE user_id=users.id AND username=%s)',(account.user_id,username)).rowcount
            auth_report['fixture_removed'] = count == 1
            if count != 1: raise RuntimeError('Fixture cleanup was not confirmed')


def response_checks(payload, expected_retake):
    def leaked(value):
        if isinstance(value,dict):
            return bool(set(value)&{'raw_output','image_sha256','image_base64','session_token','password'}) or any(leaked(v) for v in value.values())
        return isinstance(value,list) and any(leaked(v) for v in value)
    flags = dict(no_private_debug_fields=not leaked(payload),
        confirmation_required=payload.get('requires_user_confirmation') is True,
        auto_save_disabled=payload.get('eligible_for_auto_save') is False,
        severity_not_inferred=payload.get('severity') is None,schema_valid=False)
    if payload.get('status') in {'needs_confirmation','needs_retake'}:
        try: validate(payload['observation']); flags['schema_valid']=True
        except (KeyError,TypeError,ValueError): pass
    flags['expected_quality_retake'] = (payload.get('status')=='needs_retake' and flags['schema_valid'] and not payload['observation']['observations']) if expected_retake else None
    return flags


def run(args):
    if args.output.exists(): raise FileExistsError('Use a fresh evaluation output')
    base = endpoint(args.endpoint)
    manifest = args.cohort / 'cohort.json'
    if sha(manifest) != (args.cohort / 'cohort.sha256').read_text().strip():
        raise ValueError('Frozen cohort manifest changed')
    cohort = json.loads(manifest.read_text())
    if cohort.get('expert_appearance_gold_available') is not False or cohort.get('appearance_accuracy_measured') is not False:
        raise ValueError('This runner is functional validation without expert appearance gold')
    report = dict(scope='External public-source functional validation; no phenotype/clinical/device/person accuracy estimate',
        transport=args.transport,endpoint=base,cohort_sha256=sha(manifest),minimum_start_interval_seconds=args.interval,
        appearance_accuracy_measured=False,clinical_validation=False,real_device_capture_tested=False,
        credentials_or_images_recorded_in_report=False,cases=[],auth={},semantic_medical_safety_adjudicated=False)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    try:
        opener = urllib.request.build_opener()
        if args.transport == 'website':
            noauth, _, _ = call(opener,base+'/api/skin-photo',{},base)
            badorigin, _, _ = call(opener,base+'/api/skin-photo',{},'http://invalid.example')
            report['auth'].update(no_auth_status=noauth,bad_origin_status=badorigin)
            if noauth != 401 or badorigin != 403: raise RuntimeError('Website photo auth/origin checks failed')
            context = website_member(base,args.backend_root,report['auth'])
        else:
            @contextmanager
            def direct(): yield opener
            context = direct()
        previous_start = 0.
        with context as opener:
            for row in cohort['rows']:
                image = args.cohort / row['prepared_image']
                if sha(image) != row['prepared_sha256'] or sha(row['original_image']) != row['original_sha256']:
                    raise ValueError('Frozen external photo bytes changed')
                time.sleep(max(0., args.interval-(time.monotonic()-previous_start)))
                previous_start = time.monotonic()
                status,payload,seconds = call(opener,base+('/api/skin-photo' if args.transport=='website' else '/analyze'),
                    dict(image_base64=base64.b64encode(image.read_bytes()).decode(),note=''),base if args.transport=='website' else None)
                checks = response_checks(payload,row['expected_heuristic_retake']) if status==200 else None
                report['cases'].append(dict(id=row['id'],original_sha256=row['original_sha256'],prepared_sha256=row['prepared_sha256'],
                    expected_heuristic_retake=row['expected_heuristic_retake'],http_status=status,seconds=seconds,result=payload,checks=checks))
                write(args.output,report)
                print(json.dumps({'completed':len(report['cases']),'total':len(cohort['rows']),'http':status,'result_status':payload.get('status'),'seconds':seconds}),flush=True)
        cases=report['cases'];ok=[r for r in cases if r['http_status']==200]
        report['summary']=dict(images=len(cases),http_200=len(ok),usable_schema_responses=sum(r['checks']['schema_valid'] for r in ok),
            invalid_model_outputs=sum(r['result'].get('status')=='invalid_model_output' for r in ok),
            quality_retake_cases=sum(r['expected_heuristic_retake'] for r in cases),
            quality_retakes_correct=sum(r['checks']['expected_quality_retake'] is True for r in ok),
            confirmation_auto_save_privacy_contract_pass=all(all(r['checks'][key] for key in ('no_private_debug_fields','confirmation_required','auto_save_disabled','severity_not_inferred')) for r in ok) and len(ok)==len(cases),
            median_seconds=statistics.median(r['seconds'] for r in cases),maximum_seconds=max(r['seconds'] for r in cases),
            clinical_quality_gate_passed=False)
    except Exception as error:
        report['execution_error_type']=type(error).__name__
        raise
    finally: write(args.output,report)
    print(json.dumps(report['summary'],indent=2))


def main():
    p=argparse.ArgumentParser();commands=p.add_subparsers(dest='command',required=True)
    q=commands.add_parser('prepare')
    for name in ('index','quality-notes','split-info','source-audit','train','val','test','output'):q.add_argument('--'+name,type=Path,required=True)
    q=commands.add_parser('run');q.add_argument('--cohort',type=Path,required=True);q.add_argument('--output',type=Path,required=True)
    q.add_argument('--endpoint',required=True);q.add_argument('--transport',choices=['website','private-api'],required=True)
    q.add_argument('--backend-root',type=Path,default=ROOT.parents[1]/'cosmetic-agent');q.add_argument('--interval',type=float,default=16.)
    args=p.parse_args()
    if args.command=='run' and (args.interval<0 or args.transport=='website' and args.interval<16):p.error('Website validation requires >=16 seconds between starts')
    prepare(args) if args.command=='prepare' else run(args)


if __name__=='__main__':
    try: main()
    except Exception as error: raise SystemExit('External validation stopped: '+type(error).__name__)
