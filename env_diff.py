#!/usr/bin/env python3
"""env-diff: keep .env and .env.example in sync, catch mistakes before deploy.

Subcommands:
  diff      show drift between .env and .env.example
  audit     sanity checks: unfilled values, duplicates, malformed lines,
            leaked real-looking secrets inside .env.example
  template  generate a safe .env.example draft from a real .env

Exit codes: 0 = clean, 1 = issues found, 2 = usage error.
"""
import argparse
import json
import os
import re
import sys

VERSION = "1.0.0"
PROG = "env-diff"

# --------------------------------------------------------------------------
# Parsing (pure)
# --------------------------------------------------------------------------

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

PLACEHOLDER_RE = re.compile(
    r"^(changeme|change[-_ ]?me|todo|fixme|xxx+|none|null|undefined|"
    r"your[-_ ]?(key|secret|token|password|value|api[-_ ]?key)|"
    r"<[^<>]*>|\{\{[^}]*\}\}|\$[A-Za-z_][A-Za-z0-9_]*)$",
    re.IGNORECASE,
)

BOOL_RE = re.compile(r"^(true|false|yes|no|on|off|1|0)$", re.IGNORECASE)


def looks_like_real_value(value):
    """Heuristic: does this look like a real secret rather than a dummy value?

    Intended for scanning .env.example, where values SHOULD be dummy values.
    Deliberately conservative: flags length > 8, not an obvious dummy value,
    not boolean/numeric. A prompt to review, not a guarantee.
    """
    v = value.strip()
    if len(v) <= 8:
        return False
    if PLACEHOLDER_RE.match(v):
        return False
    if BOOL_RE.match(v):
        return False
    if re.fullmatch(r"-?\d+(\.\d+)?", v):
        return False
    if re.fullmatch(r"[a-z]+://\S+", v, re.IGNORECASE):
        # plain scheme-only URLs like sqlite:// or redis://localhost are
        # structural, not secrets
        if re.fullmatch(r"(sqlite|redis|postgres|postgresql|mysql|mongodb)://[^:@]*", v,
                        re.IGNORECASE):
            return False
    return True


def looks_placeholder(value):
    """Heuristic: value still shaped like an unfilled dummy value."""
    return bool(PLACEHOLDER_RE.match(value.strip()))


def _unquote(raw):
    """Split a raw value token into (value, rest). Handles '...', "..."."""
    raw = raw.strip()
    if not raw:
        return "", ""
    quote = raw[0]
    if quote not in ("'", '"'):
        # unquoted: strip inline comment (whitespace + #)
        m = re.search(r"\s+#", raw)
        if m:
            return raw[: m.start()].rstrip(), raw[m.start():]
        return raw, ""
    out = []
    i = 1
    while i < len(raw):
        c = raw[i]
        if c == "\\" and quote == '"' and i + 1 < len(raw):
            nxt = raw[i + 1]
            out.append({"n": "\n", "r": "\r", "t": "\t"}.get(nxt, nxt))
            i += 2
            continue
        if c == quote:
            return "".join(out), raw[i + 1 :]
        out.append(c)
        i += 1
    return None, None  # unterminated quote


def parse_env(text):
    """Parse dotenv text.

    Returns dict with:
      entries:  ordered list of {"key", "value", "line"}
      values:   key -> value (last wins)
      duplicates: list of keys seen more than once
      malformed:  list of {"line", "text"} for lines that failed to parse
    """
    entries, seen, duplicates, malformed = [], {}, [], []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        body = stripped
        if body.lower().startswith("export ") and re.match(
            r"^export\s+[A-Za-z_]", body, re.IGNORECASE
        ):
            body = body[len("export"):].strip()
        if "=" not in body:
            malformed.append({"line": lineno, "text": line})
            continue
        key, raw_val = body.split("=", 1)
        key = key.strip()
        if not KEY_RE.match(key):
            malformed.append({"line": lineno, "text": line})
            continue
        value, rest = _unquote(raw_val)
        if value is None:
            malformed.append({"line": lineno, "text": line})
            continue
        if rest.strip() and not rest.strip().startswith("#"):
            malformed.append({"line": lineno, "text": line})
            continue
        if key in seen:
            duplicates.append({"key": key, "line": lineno})
        seen[key] = value
        entries.append({"key": key, "value": value, "line": lineno})
    return {
        "entries": entries,
        "values": seen,
        "duplicates": duplicates,
        "malformed": malformed,
    }


# --------------------------------------------------------------------------
# Checks (pure)
# --------------------------------------------------------------------------

def diff_envs(env_parsed, example_parsed):
    """Key-level drift between a real .env and its .env.example."""
    env_keys = set(env_parsed["values"])
    ex_keys = set(example_parsed["values"])
    missing_in_env = sorted(ex_keys - env_keys)
    missing_in_example = sorted(env_keys - ex_keys)
    empty_in_env = sorted(
        k for k in (ex_keys & env_keys) if not env_parsed["values"][k].strip()
    )
    return {
        "missing_in_env": missing_in_env,
        "missing_in_example": missing_in_example,
        "empty_in_env": empty_in_env,
    }


def audit_env(env_parsed, example_parsed=None):
    """Sanity problems in a real .env (and optionally its example)."""
    ex_values = example_parsed["values"] if example_parsed else {}
    unfilled = sorted(
        k
        for k, v in env_parsed["values"].items()
        if looks_placeholder(v) or (k in ex_values and v == ex_values[k] and looks_placeholder(ex_values[k]))
    )
    problems = {
        "unfilled_values": unfilled,
        "duplicate_keys": sorted({d["key"] for d in env_parsed["duplicates"]}),
        "malformed_lines": env_parsed["malformed"],
    }
    if example_parsed is not None:
        problems["leaked_secrets_in_example"] = sorted(
            k for k, v in example_parsed["values"].items() if looks_like_real_value(v)
        )
    return problems


def make_template(env_parsed):
    """Build a safe .env.example draft: real values -> <key> markers."""
    lines = []
    for key, value in env_parsed["values"].items():
        if looks_placeholder(value) or not value.strip():
            lines.append(f"{key}={value}")
        elif BOOL_RE.match(value.strip()) or re.fullmatch(r"-?\d+(\.\d+)?", value.strip()):
            lines.append(f"{key}={value.strip()}")  # harmless, keep
        else:
            lines.append(f"{key}=<{key.lower()}>")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

def _colors():
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return {"red": "", "yellow": "", "green": "", "bold": "", "reset": ""}
    return {
        "red": "\033[31m", "yellow": "\033[33m", "green": "\033[32m",
        "bold": "\033[1m", "reset": "\033[0m",
    }


def _table(title, rows, color_key, c):
    out = [f"{c['bold']}{title}{c['reset']}"]
    if not rows:
        out.append(f"  {c['green']}none{c['reset']}")
    else:
        for r in rows:
            out.append(f"  {c[color_key]}\u2022{c['reset']} {r}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _read_source(path):
    """Read env text from a file path, or stdin when path is '-'."""
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        print(f"{PROG}: cannot read '{path}': {e.strerror or e}", file=sys.stderr)
        sys.exit(2)


def _emit(data, as_json, human):
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        print(human)


def cmd_diff(args):
    env_text = _read_source(args.env_file)
    example_text = _read_source(args.example_file)
    env_p = parse_env(env_text)
    ex_p = parse_env(example_text)
    if env_p["malformed"] or ex_p["malformed"]:
        bad = [f"line {m['line']}: {m['text']}" for m in env_p["malformed"] + ex_p["malformed"]]
        print(f"{PROG}: malformed lines, refusing to diff:", file=sys.stderr)
        for b in bad:
            print(f"  {b}", file=sys.stderr)
        return 1
    d = diff_envs(env_p, ex_p)
    issues = len(d["missing_in_env"]) + len(d["missing_in_example"]) + len(d["empty_in_env"])
    _emit(d, args.json, _human_diff(d, args.env_file, args.example_file))
    return 1 if issues else 0


def _human_diff(d, env_file, example_file):
    c = _colors()
    parts = [
        f"{c['bold']}env-diff diff{c['reset']}  {env_file}  vs  {example_file}",
        _table(f"Missing in {env_file} ({len(d['missing_in_env'])})",
               d["missing_in_env"], "red", c),
        _table(f"Not documented in {example_file} ({len(d['missing_in_example'])})",
               d["missing_in_example"], "yellow", c),
        _table(f"Empty values in {env_file} ({len(d['empty_in_env'])})",
               d["empty_in_env"], "yellow", c),
    ]
    return "\n".join(parts)


def cmd_audit(args):
    env_text = _read_source(args.env_file)
    env_p = parse_env(env_text)
    ex_p = parse_env(_read_source(args.example_file)) if args.example_file else None
    a = audit_env(env_p, ex_p)
    issues = (len(a["unfilled_values"]) + len(a["duplicate_keys"])
              + len(a["malformed_lines"]) + len(a.get("leaked_secrets_in_example", [])))
    _emit(a, args.json, _human_audit(a, args.env_file))
    return 1 if issues else 0


def _human_audit(a, env_file):
    c = _colors()
    parts = [
        f"{c['bold']}env-diff audit{c['reset']}  {env_file}",
        _table(f"Unfilled values ({len(a['unfilled_values'])})",
               a["unfilled_values"], "red", c),
        _table(f"Duplicate keys ({len(a['duplicate_keys'])})",
               a["duplicate_keys"], "yellow", c),
        _table(f"Malformed lines ({len(a['malformed_lines'])})",
               [f"line {m['line']}: {m['text']}" for m in a["malformed_lines"]], "red", c),
    ]
    if "leaked_secrets_in_example" in a:
        parts.append(_table(
            f"Real-looking secrets in .env.example ({len(a['leaked_secrets_in_example'])})",
            a["leaked_secrets_in_example"], "red", c))
    return "\n".join(parts)


def cmd_template(args):
    env_text = _read_source(args.env_file)
    env_p = parse_env(env_text)
    if env_p["malformed"]:
        print(f"{PROG}: malformed lines in {args.env_file}, refusing to generate:",
              file=sys.stderr)
        for m in env_p["malformed"]:
            print(f"  line {m['line']}: {m['text']}", file=sys.stderr)
        return 1
    draft = make_template(env_p)
    if args.write:
        try:
            with open(args.write, "w", encoding="utf-8") as f:
                f.write(draft)
        except OSError as e:
            print(f"{PROG}: cannot write '{args.write}': {e.strerror or e}",
                  file=sys.stderr)
            return 1
        print(f"wrote {args.write} ({len(env_p['values'])} keys)")
    else:
        print(draft, end="")
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Keep .env and .env.example in sync; catch unfilled "
                    "values and secret mistakes before deploy.",
    )
    p.add_argument("--version", action="version", version=f"{PROG} {VERSION}")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    d = sub.add_parser("diff", help="show drift between .env and .env.example")
    d.add_argument("env_file", help=".env file to check (use - for stdin)")
    d.add_argument("example_file", help=".env.example file to compare against")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.set_defaults(func=cmd_diff)

    a = sub.add_parser("audit", help="sanity checks on a .env file")
    a.add_argument("env_file", help=".env file to audit (use - for stdin)")
    a.add_argument("--example", dest="example_file", default=None,
                   help="also leak-scan this .env.example for real-looking secrets")
    a.add_argument("--json", action="store_true", help="machine-readable output")
    a.set_defaults(func=cmd_audit)

    t = sub.add_parser("template",
                       help="generate a safe .env.example draft from a .env")
    t.add_argument("env_file", help=".env file to redact (use - for stdin)")
    t.add_argument("--write", metavar="PATH", default=None,
                   help="write the draft to PATH (default: print to stdout)")
    t.set_defaults(func=cmd_template)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
