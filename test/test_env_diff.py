import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import env_diff

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
CLI = [sys.executable, os.path.join(ROOT, "env_diff.py")]


def run_cli(*args, input_text=None, env=None):
    full_env = dict(os.environ)
    full_env["NO_COLOR"] = "1"
    if env:
        full_env.update(env)
    return subprocess.run(
        CLI + list(args),
        input=input_text,
        capture_output=True,
        text=True,
        env=full_env,
    )


class TestParse(unittest.TestCase):
    def test_basic_quoted_unquoted(self):
        p = env_diff.parse_env('A=1\nB="two"\nC=\'three\'\n')
        self.assertEqual(p["values"], {"A": "1", "B": "two", "C": "three"})
        self.assertEqual(p["malformed"], [])

    def test_comments_and_export(self):
        p = env_diff.parse_env("# comment\nexport D=4\n\nE=5 # inline\n")
        self.assertEqual(p["values"], {"D": "4", "E": "5"})

    def test_hash_inside_quotes_kept(self):
        p = env_diff.parse_env('F="a#b"\nG=a#b\n')
        self.assertEqual(p["values"]["F"], "a#b")
        self.assertEqual(p["values"]["G"], "a#b")

    def test_escaped_double_quotes(self):
        p = env_diff.parse_env('H="say \\"hi\\"\\n"\n')
        self.assertEqual(p["values"]["H"], 'say "hi"\n')

    def test_malformed_and_duplicates(self):
        p = env_diff.parse_env("NOEQUALS\n1BAD=x\nOK=1\nOK=2\nK=\"unterminated\n")
        self.assertEqual(len(p["malformed"]), 3)
        self.assertEqual(len(p["duplicates"]), 1)
        self.assertEqual(p["duplicates"][0]["key"], "OK")
        self.assertEqual(p["values"]["OK"], "2")  # last wins

    def test_empty_input(self):
        p = env_diff.parse_env("")
        self.assertEqual(p["values"], {})
        self.assertEqual(p["malformed"], [])

    def test_value_with_equals(self):
        p = env_diff.parse_env("URL=postgres://u:p@h/db?x=1&y=2\n")
        self.assertEqual(p["values"]["URL"], "postgres://u:p@h/db?x=1&y=2")


class TestDiff(unittest.TestCase):
    def test_drift_categories(self):
        env = env_diff.parse_env("A=1\nB=2\nC=\n")
        ex = env_diff.parse_env("A=\nC=\nD=\n")
        d = env_diff.diff_envs(env, ex)
        self.assertEqual(d["missing_in_env"], ["D"])
        self.assertEqual(d["missing_in_example"], ["B"])
        self.assertEqual(d["empty_in_env"], ["C"])


class TestAudit(unittest.TestCase):
    def test_unfilled_values(self):
        env = env_diff.parse_env(
            "SIGNUP_MSG=changeme\nCALLBACK_URL=<paste-token>\nNICK=xxx\nREAL=abc123xyz\n"
        )
        a = env_diff.audit_env(env)
        self.assertEqual(
            sorted(a["unfilled_values"]), ["CALLBACK_URL", "NICK", "SIGNUP_MSG"]
        )

    def test_value_copied_from_placeholder_example(self):
        env = env_diff.parse_env("SIGNUP_MSG=changeme\n")
        ex = env_diff.parse_env("SIGNUP_MSG=changeme\n")
        a = env_diff.audit_env(env, ex)
        self.assertIn("SIGNUP_MSG", a["unfilled_values"])

    def test_leak_scan(self):
        ex = env_diff.parse_env(
            "NOTIFY_URL=https://hooks.example.com/abcdef123456\n"
            "DEBUG=false\nPORT=8000\nNOTE=<api-key>\n"
        )
        a = env_diff.audit_env(env_diff.parse_env("NOTIFY_URL=x\n"), ex)
        self.assertEqual(a["leaked_secrets_in_example"], ["NOTIFY_URL"])

    def test_leak_scan_ignores_harmless(self):
        ex = env_diff.parse_env("DEBUG=false\nPORT=8000\nDB=sqlite:///app.db\n")
        a = env_diff.audit_env(env_diff.parse_env("DEBUG=false\n"), ex)
        self.assertEqual(a["leaked_secrets_in_example"], [])


class TestTemplate(unittest.TestCase):
    def test_redacts_and_keeps_shape(self):
        env = env_diff.parse_env(
            "SERVICE_NAME=acme-prod\nDEBUG=false\nPORT=8000\nEMPTY=\nNOTE=changeme\n"
        )
        t = env_diff.make_template(env)
        self.assertIn("SERVICE_NAME=<service_name>", t)
        self.assertIn("DEBUG=false", t)
        self.assertIn("PORT=8000", t)
        self.assertIn("EMPTY=", t)
        self.assertIn("NOTE=changeme", t)
        self.assertNotIn("acme-prod", t)


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.path.join(self.tmp.name, ".env")
        self.ex = os.path.join(self.tmp.name, ".env.example")
        with open(self.env, "w") as f:
            f.write("APP_ENV=production\nSIGNUP_MSG=changeme\nPORT=8000\n")
        with open(self.ex, "w") as f:
            f.write("APP_ENV=\nSIGNUP_MSG=\nPORT=\nDB_URL=\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_e2e_diff_reports_drift(self):
        r = run_cli("diff", self.env, self.ex)
        self.assertEqual(r.returncode, 1)
        self.assertIn("DB_URL", r.stdout)
        self.assertIn("Missing in", r.stdout)

    def test_e2e_diff_clean_json(self):
        with open(self.ex, "w") as f:
            f.write("APP_ENV=\nSIGNUP_MSG=\nPORT=\n")
        r = run_cli("diff", self.env, self.ex, "--json")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertEqual(data["missing_in_env"], [])
        self.assertEqual(data["empty_in_env"], [])

    def test_e2e_audit_finds_placeholder(self):
        r = run_cli("audit", self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SIGNUP_MSG", r.stdout)

    def test_e2e_template_dry_run_stdout(self):
        r = run_cli("template", self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("APP_ENV=<app_env>", r.stdout)

    def test_e2e_template_write(self):
        out = os.path.join(self.tmp.name, "gen.example")
        r = run_cli("template", self.env, "--write", out)
        self.assertEqual(r.returncode, 0)
        self.assertIn("wrote", r.stdout)
        self.assertIn("APP_ENV=<app_env>", open(out).read())

    def test_e2e_stdin(self):
        r = run_cli("audit", "-", input_text="A=changeme\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("A", r.stdout)

    def test_missing_file_exit_2(self):
        r = run_cli("diff", "/nope/.env", self.ex)
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot read", r.stderr)

    def test_bad_subcommand_exit_2(self):
        r = run_cli("frobnicate")
        self.assertEqual(r.returncode, 2)

    def test_version_and_help(self):
        self.assertEqual(run_cli("--version").returncode, 0)
        self.assertIn("env-diff", run_cli("--version").stdout)
        for sub in ("diff", "audit", "template"):
            r = run_cli(sub, "--help")
            self.assertEqual(r.returncode, 0)
            self.assertIn("usage", r.stdout.lower())

    def test_no_color_honored(self):
        r = run_cli("audit", self.env)
        self.assertNotIn("\033[", r.stdout)


if __name__ == "__main__":
    unittest.main()
