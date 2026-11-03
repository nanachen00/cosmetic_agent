from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import bench
import runtime_capture as runtime
import gate2_audit
from gate8_neo4j_routing import verify_backend_policy


class Gate8Experiment(bench.Experiment):
    """Use one immutable harness/backend capsule across A/C/B."""

    def __init__(self, *args, gate8_root: Path, kev_url: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.gate8_root = Path(gate8_root)
        self.gate8_arm = "A"
        self.kev_url = kev_url

    def set_gate8_phase(self, arm: str, phase: str, phase_cfg: dict):
        if arm not in {"A", "B", "C"}:
            raise bench.Refuse("Unknown Gate8 arm")
        if phase not in {"auth", "smoke", "formal"}:
            raise bench.Refuse("Unknown Gate8 phase")
        self.gate8_arm = arm
        self.phase = f"{phase}-{arm}"
        self.cfg = copy.deepcopy(phase_cfg)
        self.run = self.root_run / "arms" / arm / phase
        self.run.mkdir(parents=True, exist_ok=True)
        self.raw = self.state / "raw-logs" / arm / phase
        self.cfg["snapshot_id"] = self.snapshot_id
        bench.atomic_json(self.run / "run-config.json", self.cfg)
        bench.atomic_json(
            self.run / "gate8-arm.json",
            {
                "gate": 8,
                "arm": arm,
                "phase": phase,
                "snapshot_id": self.snapshot_id,
                "treatment": {
                    "A": "current_blink",
                    "B": "parser_plus_kev",
                    "C": "parser_only",
                }[arm],
            },
        )
        self.invariants()
        status = runtime.runtime_changed(bench.read_json(self.capsule / "runtime-metadata.json"))
        bench.atomic_json(self.run / "runtime-observation.json", status)
        if status["status"] != "NO_OBSERVED_RUNTIME_METADATA_DRIFT":
            raise bench.Refuse("SHARED_RUNTIME_CHANGED: Gate8 stops without rollback")

    def guarded_launch(self, name, python, spec, env, cwd, port, log, evidence):
        if name == "backend":
            expected = list(self.cfg.get("neo4j_routing_hosts", []))
            proof = verify_backend_policy(spec.get("policy") or {}, expected)
            bench.atomic_json(Path(evidence) / "gate8-neo4j-egress-binding.json", {
                "gate": 8,
                "arm": self.gate8_arm,
                "phase": self.phase,
                **proof,
            })
            if not proof["pass"]:
                raise bench.Refuse("GATE8_NEO4J_EXACT_ROUTING_POLICY_NOT_BOUND")

        if name == "gateway":
            argv = list(spec.get("service_argv") or [])
            try:
                idx = argv.index("--spec")
                gcfg = Path(argv[idx + 1])
            except Exception as exc:
                raise bench.Refuse("Gate8 could not locate gateway spec") from exc
            data = bench.read_json(gcfg)
            hroot = Path(bench.HERE)
            decision_audit = Path(data["audit_log"]).with_name("gate8-intent-decisions.jsonl")
            if getattr(self, "_gate2_decision_audit", None) is not None:
                raise bench.Refuse("GATE2_PREVIOUS_DECISION_AUDIT_NOT_CLOSED")
            scenario_name = Path(evidence).parent.name
            phase_name = self.run.name
            audit_session = uuid.uuid4().hex
            gate2_audit.open_cell(
                decision_audit, session_id=audit_session, arm=self.gate8_arm,
                phase=phase_name, scenario=scenario_name, provider_port=int(self.cfg["providers"][0]),
            )
            self._gate2_decision_audit = {
                "path": decision_audit, "session_id": audit_session,
                "arm": self.gate8_arm, "phase": phase_name,
                "scenario": scenario_name, "provider_port": int(self.cfg["providers"][0]),
            }
            data["gate8"] = {
                "arm": self.gate8_arm,
                "parser_file": str(hroot / "qualified_parser.py"),
                "policy_file": str(hroot / "selected_policy.json"),
                "questions_file": str(hroot / "gate8_questions.json"),
                "decision_audit": str(decision_audit),
                "decision_audit_session": audit_session,
                "kev_url": self.kev_url if self.gate8_arm == "B" else None,
                "kev_timeout_seconds": 10.0,
            }
            bench.atomic_json(gcfg, data)
            if self.gate8_arm == "B":
                local_ports = list(spec["policy"].get("local_ports") or [])
                if 8230 not in local_ports:
                    local_ports.append(8230)
                spec["policy"]["local_ports"] = local_ports
                llm_ports = list(spec["policy"].get("llm_ports") or [])
                if 8230 not in llm_ports:
                    llm_ports.append(8230)
                spec["policy"]["llm_ports"] = llm_ports
            bench.atomic_json(
                Path(evidence) / "gate8-treatment-binding.json",
                {
                    "gate": 8,
                    "arm": self.gate8_arm,
                    "gateway_spec": str(gcfg),
                    "kev_port_permitted": self.gate8_arm == "B",
                    "source_patch": False,
                    "integration": "model_http_boundary",
                },
            )
        return super().guarded_launch(name, python, spec, env, cwd, port, log, evidence)

    def _sanitize_gate8_raw(self):
        if not self.raw.is_dir():
            return
        for p in self.raw.rglob("*.log"):
            q = self.run / "service-logs" / p.relative_to(self.raw)
            q.parent.mkdir(parents=True, exist_ok=True)
            with p.open("r", encoding="utf-8", errors="replace") as src, q.open("w", encoding="utf-8") as dst:
                for line in src:
                    dst.write(bench.redact(line))

    def release_cell(self):
        for name in ("backend", "gateway", "proxy", "redis"):
            bench.pc.stop_owned(self.j, self.jfile, name, self.cfg["stop_timeout_seconds"])
        self._sanitize_gate8_raw()
        audit_state = getattr(self, "_gate2_decision_audit", None)
        if audit_state is not None:
            gate2_audit.close_cell(
                audit_state["path"], session_id=audit_state["session_id"],
                arm=audit_state["arm"], phase=audit_state["phase"],
                scenario=audit_state["scenario"], provider_port=audit_state["provider_port"],
            )
            self._gate2_decision_audit = None
        self.invariants()
        if getattr(self, "_open_cell", None):
            out, token = self._open_cell
            bench.atomic_json(out / "deployment-window.json", self.observer.end(token))
            self._open_cell = None
