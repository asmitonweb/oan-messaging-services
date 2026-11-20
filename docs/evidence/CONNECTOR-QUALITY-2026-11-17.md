# Quality report: oan_messaging_connector (17 November 2026)

Commit checked: `oan_messaging_connector` `develop` at `b190093`.

The connector now meets the checks the OAN Frappe apps run in CI (the shared Linters
workflow of `oan_auth_service`), and calls that workflow from its own
`.github/workflows/linters.yml`.

## Results

| Check | Configuration | Result |
|---|---|---|
| pre-commit, all files | Same hooks as `oan_auth_service`: file hygiene, ruff import sort, ruff lint, ruff format, prettier (Markdown), gitleaks | **Pass** |
| Ruff | Settings of `oan_grievance_service` (line length 110, tabs) | **Pass** (vendored client excluded; see below) |
| Semgrep | Frappe `semgrep-rules` (81a6e3d), `r/python.lang.correctness`, shared OAN rules (`oan_auth_service/.semgrep/rules.yml`); tests included | **0 findings** (before: 11) |
| pip-audit | `pip-audit --desc on .` | **No known vulnerabilities** |
| Tests | Frappe 16.56.0, Python 3.14.10, MariaDB (`bench run-tests --app oan_messaging_connector`) | **34 passed** |
| Vendored client | `vendor/` against `oan_messaging_services` `examples/` | Identical apart from the documented relative imports |

## What changed

| Before | After |
|---|---|
| Four queries written as SQL strings with an f-string table name (`frappe-sql-format-injection`, `oan-db-sql-formatted-string`) | Frappe query builder. Workers claim one due row at a time with `FOR UPDATE SKIP LOCKED`; a row is processed at most once per run |
| Three `frappe.db.commit()` calls flagged by `frappe-manual-commit` | The purge leaves the commit to the scheduler. Dispatch and polling keep one commit per row (it releases the row lock and records the OAN outcome before the next row), in one helper marked `nosemgrep` with the reason. Test clean-up commits are marked the same way. |
| `get_doc` per claimed row | One column read per claimed row, in the same locking query |
| Test clean-up with a raw `delete` | `frappe.db.delete` with a filter |
| No lint configuration; mixed spaces and tabs | House `pyproject.toml` ruff settings, `.pre-commit-config.yaml`, `.editorconfig`, `.semgrepignore`; code formatted with tabs |
| No CI | `linters.yml` calling the shared workflow (conventional commit titles, pre-commit, Semgrep, pip-audit) |

A new test checks that both sweeps claim rows with `FOR UPDATE SKIP LOCKED ... LIMIT 1`.

## Not done

- **SonarQube** was run later the same day: quality gate passed, 0 bugs, 0 vulnerabilities,
  A ratings (see `INTEGRATION-KITS-2026-11-17.md`).
- **The shared workflow on GitHub.** It runs on pull requests; none has been opened yet.
- **Several workers at once.** Covered by the query check above, not by a test with two
  real database connections.
- **The Credit end-to-end trial** was not re-run after the refactor: the trial's fake
  8028 is stopped. The unit tests ran the new queries against MariaDB.
