from __future__ import annotations

import json
import re
from pathlib import Path

HOST_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
    r"\.neo4j\.io"
)

class RoutingPolicyError(RuntimeError):
    pass

def validate_exact_host(host: str) -> str:
    if not isinstance(host, str):
        raise RoutingPolicyError("NEO4J_ROUTING_HOST_NOT_STRING")
    if host != host.lower() or "*" in host or "://" in host or "/" in host:
        raise RoutingPolicyError("NEO4J_ROUTING_HOST_NOT_EXACT_LOWERCASE")
    if not HOST_RE.fullmatch(host):
        raise RoutingPolicyError("NEO4J_ROUTING_HOST_INVALID")
    return host

def load_declared_hosts(path: Path) -> tuple[list[str], dict]:
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    if obj.get("gate") != 8 or obj.get("wildcards_allowed") is not False:
        raise RoutingPolicyError("NEO4J_ROUTING_DECLARATION_INVALID")
    if obj.get("port") != 7687:
        raise RoutingPolicyError("NEO4J_ROUTING_PORT_CHANGED")
    observations = obj.get("observations")
    if not isinstance(observations, list) or not observations:
        raise RoutingPolicyError("NEO4J_ROUTING_OBSERVATIONS_EMPTY")
    hosts = []
    for row in observations:
        if not isinstance(row, dict):
            raise RoutingPolicyError("NEO4J_ROUTING_OBSERVATION_INVALID")
        host = validate_exact_host(row.get("host"))
        if host in hosts:
            raise RoutingPolicyError("NEO4J_ROUTING_HOST_DUPLICATE")
        hosts.append(host)
    return hosts, obj

def merge_exact_hosts(cfg: dict, declaration_path: Path) -> dict:
    declared, declaration = load_declared_hosts(declaration_path)
    existing = cfg.get("neo4j_routing_hosts", [])
    if not isinstance(existing, list):
        raise RoutingPolicyError("NEO4J_ROUTING_CONFIG_NOT_LIST")
    validated_existing = []
    for host in existing:
        host = validate_exact_host(host)
        if host not in validated_existing:
            validated_existing.append(host)
    effective = list(validated_existing)
    added = []
    for host in declared:
        if host not in effective:
            effective.append(host)
            added.append(host)
    if len(effective) > 16:
        raise RoutingPolicyError("NEO4J_ROUTING_HOST_LIMIT_EXCEEDED")
    cfg["neo4j_routing_hosts"] = effective
    return {
        "gate": 8,
        "port": 7687,
        "exact_only": True,
        "wildcards_allowed": False,
        "baseline_hosts": validated_existing,
        "declared_hosts": declared,
        "added_hosts": added,
        "effective_hosts": effective,
        "observation_execution_ids": sorted({
            str(row.get("observed_execution"))
            for row in declaration.get("observations", [])
            if row.get("observed_execution")
        }),
        "production_source_modified": False,
        "shared_service_modified": False,
    }

def verify_backend_policy(policy: dict, expected_hosts: list[str]) -> dict:
    host_ports = policy.get("host_ports", [])
    normalized = set()
    for row in host_ports:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        normalized.add((str(row[0]).lower(), int(row[1])))
    missing = [
        host for host in expected_hosts
        if (validate_exact_host(host), 7687) not in normalized
    ]
    return {
        "pass": not missing,
        "expected_exact_hosts": list(expected_hosts),
        "port": 7687,
        "missing_hosts": missing,
        "effective_host_port_count": len(normalized),
        "wildcard_used": False,
    }
