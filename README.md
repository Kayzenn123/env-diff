# env-diff

> Keep `.env` and `.env.example` in sync; catch unfilled values and secret mistakes before deploy. Daily Tools #13.

## Why

`.env.example` files rot silently: new keys land in `.env` and never get documented, and deploys go out with `SIGNUP_MSG=changeme` still unfilled. `env-diff` is a zero-dependency, single-file Python CLI that gates this in pre-commit or CI. Unlike `FerrowAI/env-example-diff` (a TypeScript *library* with no CLI) it runs anywhere Python exists with no build step, and unlike `LucasBringsken/dotenv-diff` (an N-file matrix comparer) it understands `.env` vs `.env.example` parity semantics — including the reverse direction nobody else checks: dummy values left *unfilled in the real `.env`*, plus a leak-scan that flags real-looking secrets accidentally committed into `.env.example`.

## Install

```bash
git clone https://github.com/Kayzenn123/env-diff.git
cd env-diff
# no dependencies — Python 3.9+ standard library only
```

Requires: Python >= 3.9

## Usage

```bash
python3 env_diff.py diff .env .env.example
python3 env_diff.py audit .env --example .env.example
python3 env_diff.py template .env            # dry-run: prints draft to stdout
python3 env_diff.py template .env --write .env.example
```

| Command | What it does |
| --- | --- |
| `diff ENV EXAMPLE` | Key drift: missing in `.env`, undocumented in example, empty values. Exit 1 on drift. |
| `audit ENV [--example EX]` | Unfilled dummy values, duplicate keys, malformed lines, real-looking secrets in the example. Exit 1 on findings. |
| `template ENV [--write PATH]` | Generate a safe `.env.example` draft (real values become `<key>`). Prints to stdout unless `--write` is given. |

Global: `--version`, `--json` (on `diff`/`audit` for machine output), `-` reads from stdin, `NO_COLOR=1` disables color. Exit codes: `0` clean, `1` issues found, `2` usage error.

## Example output

Command:

```bash
python3 env_diff.py diff .env .env.example
```

Output (real, copied from an actual run):

```text
env-diff diff  .env  vs  .env.example
Missing in .env (1)
  • LOG_LEVEL
Not documented in .env.example (3)
  • CACHE_TTL
  • SIGNUP_MSG
  • WEBHOOK_PATH
Empty values in .env (0)
  none
```

Command:

```bash
python3 env_diff.py audit .env --example .env.example
```

Output (real, copied from an actual run):

```text
env-diff audit  .env
Unfilled values (1)
  • SIGNUP_MSG
Duplicate keys (1)
  • PORT
Malformed lines (1)
  • line 5: MALFORMED LINE HERE
Real-looking secrets in .env.example (1)
  • NOTIFY_URL
```

Command:

```bash
python3 env_diff.py template .env
```

Output (real, copied from an actual run):

```text
APP_ENV=<app_env>
PORT=8000
DEBUG=false
DB_HOST=<db_host>
ADMIN_EMAIL=<admin_email>
```

## How it works

A strict dotenv parser (`parse_env`) handles `export` prefixes, single/double quotes with escapes, inline comments, and flags duplicate keys and malformed lines. Pure functions then compute: key-set drift (`diff_envs`), dummy-value/leak heuristics (`audit_env`), and a redacting generator (`make_template`) that keeps harmless booleans/numbers and turns everything else into `<key>` markers. CLI parsing and output formatting are kept separate from the logic, so every check is unit-testable.

## Limitations

- The leak heuristic (`looks_like_real_value`: length > 8, not an obvious dummy value, not boolean/numeric) is a prompt to review, not a secret scanner — short secrets can be missed and long benign values can be flagged.
- Multi-line values and shell variable expansion (`$OTHER_VAR`) are treated as literal text, not expanded.
- Duplicate keys resolve last-wins (reported by `audit`, not silently).
- `diff`/`audit` refuse to run when either file has malformed lines — fix them first.

## Development

```bash
python3 -m unittest discover -s test -q
```

23 tests (parser, drift, audit heuristics, template, and end-to-end CLI runs), green twice in a row.

## License

MIT

---
Ringkasan (Bahasa Indonesia): env-diff adalah CLI Python satu file tanpa dependensi untuk menjaga `.env` dan `.env.example` tetap sinkron. Tiga subcommand: `diff` (deteksi key yang hilang/tidak terdokumentasi), `audit` (dummy values belum diisi, key duplikat, baris rusak, dan secret asli yang bocor di `.env.example`), serta `template` (membuat draf `.env.example` yang aman dari `.env` asli). Cocok dipasang sebagai gate pre-commit/CI dengan exit code bermakna dan output `--json`. Batasan utama: deteksi secret memakai heuristik sederhana, bukan pemindai secret sungguhan.
