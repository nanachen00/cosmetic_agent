"""shadow_stack.py: pure logic and refusal paths (no PostgreSQL / Redis / uvicorn needed; runs on any OS)."""
import io, json, os, sys, tempfile, unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import shadow_stack as S  # noqa: E402

PROD_ENV = """# production-like .env (fake values)
DATABASE_URL=postgresql://blinkai:PGSECRET123@127.0.0.1:5432/blinkai
REDIS_URL=redis://:REDISSECRET456@127.0.0.1:6389/0
export OPENAI_API_KEY="sk-OPENAISECRET789"
NEO4J_PASSWORD='neo4jSECRET000'
SMTP_HOST=smtp.example.com   # inline comment
SMTP_PASSWORD=mailSECRET111
ADMIN_USER_IDS=11111111-2222-3333-4444-555555555555
TERRA_MODEL=gpt-5.6-terra
EMPTY_ONE=
NEO4J_URI=   # [REQUIRED] Neo4j Aura
"""
SECRETS = ("PGSECRET123", "REDISSECRET456", "OPENAISECRET789", "neo4jSECRET000", "mailSECRET111")


class Pure(unittest.TestCase):
    def test_parse_dotenv(self):
        e = S.parse_dotenv(PROD_ENV)
        self.assertEqual(e["OPENAI_API_KEY"], "sk-OPENAISECRET789")
        self.assertEqual(e["NEO4J_PASSWORD"], "neo4jSECRET000")
        self.assertEqual(e["SMTP_HOST"], "smtp.example.com")
        self.assertEqual((e["EMPTY_ONE"], e["NEO4J_URI"]), ("", ""))

    def test_db_url_rewrite_and_guards(self):
        u = S.shadow_db_url("postgresql://blinkai:pw@127.0.0.1:5432/blinkai", "blinkai_shadow_sys1")
        self.assertEqual(u, "postgresql://blinkai:pw@127.0.0.1:5432/blinkai_shadow_sys1")
        for bad in ("blinkai", "prod", "blinkai_shadow_X;drop", "blinkai_shadow_"):
            with self.assertRaises(SystemExit): S.shadow_db_url("postgresql://u:p@h/blinkai", bad)
        with self.assertRaises(SystemExit): S.shadow_db_url("redis://h/0", "blinkai_shadow_sys1")
        self.assertNotIn("pw", S.redact_url(u))

    def test_ports(self):
        S.check_ports(8210, 6380)
        for b, r in ((8010, 6380), (8210, 6389), (8200, 6380), (9000, 6380), (8210, 5432)):
            with self.assertRaises(SystemExit): S.check_ports(b, r)

    def test_compose_env_isolates_and_blanks(self):
        prod = S.parse_dotenv(PROD_ENV)
        env, over = S.compose_env(prod, db_url=S.shadow_db_url(prod["DATABASE_URL"], "blinkai_shadow_sys1"),
                                  redis_url="redis://:x@127.0.0.1:6380/0", release="shadow-sys1-test", control="/c.json", prom_dir="/p")
        self.assertNotEqual(env["DATABASE_URL"], prod["DATABASE_URL"]); self.assertTrue(env["DATABASE_URL"].endswith("/blinkai_shadow_sys1"))
        self.assertEqual(env["REDIS_URL"], "redis://:x@127.0.0.1:6380/0")
        for k in ("SMTP_HOST", "SMTP_PASSWORD", "ADMIN_USER_IDS", "FIRECRAWL_API_KEY"): self.assertEqual(env[k], "", k)
        self.assertEqual((env["APP_ENV"], env["SYS1_DISPATCH_CONTROL"], env["OTEL_TRACING_ENABLED"]), ("shadow", "/c.json", "0"))
        self.assertEqual(env["OPENAI_API_KEY"], prod["OPENAI_API_KEY"])          # shared service kept (needs ack)
        with self.assertRaises(SystemExit):
            S.compose_env(prod, db_url=prod["DATABASE_URL"], redis_url="r", release="x", control="c", prom_dir="p")
        with self.assertRaises(SystemExit):
            S.compose_env(prod, db_url="postgresql://u@h/blinkai_shadow_x", redis_url=prod["REDIS_URL"], release="x", control="c", prom_dir="p")

    def test_env_report_never_prints_secret_values(self):
        prod = S.parse_dotenv(PROD_ENV)
        env, over = S.compose_env(prod, db_url=S.shadow_db_url(prod["DATABASE_URL"], "blinkai_shadow_sys1"),
                                  redis_url="redis://:GENERATED999@127.0.0.1:6380/0", release="r", control="c", prom_dir="p")
        text = "\n".join(S.env_report(prod, env, over))
        for sec in SECRETS + ("GENERATED999",): self.assertNotIn(sec, text)
        self.assertIn("覆寫", text); self.assertIn("已填", text)


class Cli(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp()); self.prod = self.t / "prod"; (self.prod / "database/postgres/migrations").mkdir(parents=True)
        (self.prod / ".env").write_text(PROD_ENV, encoding="utf-8")
        (self.prod / "database/postgres/migrations/0001_reconstructed_schema.sql").write_text("-- schema\n")
        self.shadow = self.t / "shadow" / "cosmetic-agent"; self.shadow.mkdir(parents=True)
        (self.shadow / ".shadow-copy").write_text("{}"); (self.shadow / ".sys1-shadow-overlay").write_text("{}")
        self.ctl = self.t / "control.json"; self.ctl.write_text("{}")
        import shadowpatch                                   # prod root via the JSON file (env list is ':'-separated, not Windows-safe)
        self.SP, self.saved = shadowpatch, shadowpatch.PROD_FILE
        shadowpatch.PROD_FILE = self.t / "prod_roots.json"; shadowpatch.PROD_FILE.write_text(json.dumps({"prod_roots": [str(self.prod)]}))
        self.base = ["--prod-root", str(self.prod), "--shadow-root", str(self.shadow), "--control", str(self.ctl), "--state-dir", str(self.t / "state")]

    def tearDown(self): self.SP.PROD_FILE = self.saved

    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            try: rc = S.main(self.base + list(args))
            except SystemExit as e: rc = e
        return rc, buf.getvalue()

    def test_up_without_ack_refuses_before_side_effects(self):
        rc, _ = self.run_cli("up")
        self.assertIsInstance(rc, SystemExit); self.assertIn("--ack-shared-data-services", str(rc))
        self.assertFalse((self.t / "state").exists())

    def test_plan_reports_and_hides_secrets(self):
        rc, out = self.run_cli("plan")
        self.assertIn("[OK] shadow root (G1/G2)", out); self.assertIn("[OK] overlay applied", out)
        for sec in SECRETS: self.assertNotIn(sec, out)

    def test_env_check_hides_secrets(self):
        rc, out = self.run_cli("env-check")
        self.assertEqual(rc, 0); self.assertIn("DATABASE_URL", out); self.assertIn("blinkai_shadow_sys1", out)
        for sec in SECRETS: self.assertNotIn(sec, out)

    def test_shadow_root_inside_production_is_refused(self):
        inside = self.prod / "shadow"; inside.mkdir(); (inside / ".shadow-copy").write_text("{}"); (inside / ".sys1-shadow-overlay").write_text("{}")
        self.base[3] = str(inside)
        rc, out = self.run_cli("plan")
        self.assertEqual(rc, 3); self.assertIn("[NG] shadow root (G1/G2)", out)

    def test_missing_overlay_marker_is_reported(self):
        (self.shadow / ".sys1-shadow-overlay").unlink()
        rc, out = self.run_cli("plan")
        self.assertEqual(rc, 3); self.assertIn("[NG] overlay applied", out)


if __name__ == "__main__":
    unittest.main()
