#!/usr/bin/env python3
"""Repeat the frozen five-text runtime comparison against a loopback BGE API."""
import argparse
import datetime
import hashlib
import json
import math
from pathlib import Path
import urllib.parse
import urllib.request


def cosine(left, right):
    return sum(a * b for a, b in zip(left, right)) / (
        math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--protocol', required=True, type=Path)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--base-url', default='http://127.0.0.1:8027')
    args = parser.parse_args()
    url = urllib.parse.urlsplit(args.base_url)
    if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or url.username or url.password or url.path not in {'', '/'} or url.query or url.fragment):
        raise SystemExit('Only a loopback BGE API origin is accepted')
    protocol = json.loads(args.protocol.read_text())
    baseline = json.loads(args.baseline.read_text())
    protocol_sha = hashlib.sha256(args.protocol.read_bytes()).hexdigest()
    if baseline['protocol_sha256'] != protocol_sha or [row['text'] for row in baseline['rows']] != protocol['corpus']:
        raise SystemExit('Frozen baseline/protocol differs')
    with urllib.request.urlopen(args.base_url.rstrip('/') + '/healthz', timeout=5) as response:
        health = json.load(response)
    if health.get('ready') is not True or health.get('status') != 'ready' or health.get('revision') != protocol['source_model_revision'] or health.get('dimension') != protocol['expected_dimension']:
        raise SystemExit('Candidate health/model revision/dimension differs')

    def post(texts):
        request = urllib.request.Request(args.base_url.rstrip('/') + '/v1/embeddings',
            data=json.dumps({'model': protocol['request_model'], 'input': texts}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.load(response)
        rows = payload['data']
        if len(rows) != len(texts) or [row['index'] for row in rows] != list(range(len(texts))):
            raise ValueError('BGE batch cardinality/order differs')
        values = [row['embedding'] for row in rows]
        if any(len(row) != protocol['expected_dimension'] or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in row) for row in values):
            raise ValueError('Invalid BGE vector')
        return values

    singles = [post([text])[0] for text in protocol['corpus']]
    batch = post(protocol['corpus'])
    rows = []
    for index, (single, batched, old) in enumerate(zip(singles, batch, baseline['rows'])):
        single_cosine = cosine(single, old['embedding'])
        batch_cosine = cosine(batched, old['embedding'])
        pair_cosine = cosine(single, batched)
        unit_error = max(abs(math.sqrt(sum(v * v for v in vector)) - 1) for vector in (single, batched))
        rows.append(dict(index=index, cosine_to_old=single_cosine, batch_cosine_to_old=batch_cosine,
            batch_vs_single_cosine=pair_cosine, maximum_unit_norm_error=unit_error,
            passed=min(single_cosine, batch_cosine, pair_cosine) >= protocol['minimum_cosine_to_old']
                and unit_error <= protocol['maximum_unit_norm_error']))
    report = dict(checked_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        endpoint=args.base_url, protocol_sha256=protocol_sha, corpus_sha256=protocol['corpus_sha256'],
        dimension=protocol['expected_dimension'], model_revision=protocol['source_model_revision'],
        rows=rows, batch_returned_embeddings=len(batch), passed=all(row['passed'] for row in rows),
        scope='Frozen runtime numerical parity only; retrieval relevance and GraphRAG require separate checks')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
