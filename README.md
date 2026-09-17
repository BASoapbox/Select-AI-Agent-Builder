# Select AI Agent Builder

An interactive Python CLI for building, managing, and testing **Oracle Select AI Agent**
stacks on Autonomous Database — without hand-writing PL/SQL against
`DBMS_CLOUD_AI_AGENT`.

It covers the whole lifecycle: provisioning pre-flight checks, a guided build
conversation, **deterministic** PL/SQL generation, execution against ADB, and a
post-build management console for listing, editing, rebuilding, and testing what
you deployed.

All examples throughout the code and config use a fictional `ACME_CORP` schema.

> **Status:** working tool, shared as-is. It talks to a live Autonomous Database
> and a live OCI tenancy, and the Admin Setup menu issues real `GRANT` and IAM
> statements. Read what it prints before you confirm anything.

---

## What it produces

One run generates and executes a complete agent stack:

```
NL2SQL profile  ──┐
RAG profile ──────┤
vector index  ────┼──▶  SQL tool + RAG tool + N custom tools  ──▶  agent  ──▶  task  ──▶  team
COMMENT ON …  ────┘
```

Everything is emitted by `core/sql_builder.py` as plain PL/SQL you can read,
save, and re-run. The model runs the *conversation*; it does not write the
provisioning code. Object names, table lists, and comment metadata come from
captured facts, so the same spec produces a byte-identical script every time.

Two qualifications worth stating plainly:

- If `sql_builder` fails, the tool falls back to asking the model for the script,
  and prints a warning when it does. Read that output more carefully.
- The agent you deploy still uses a model at run time. NL2SQL writes queries, and
  query results and tool output are sent to the model to compose answers. What the
  generator controls is what the agent is allowed to reach, not whether a model
  sees your data.

---

## Requirements

| | |
|---|---|
| Python | 3.10+ |
| Database | Oracle Autonomous Database (ADW/ATP) with Select AI |
| Driver | `python-oracledb` in **thick** mode → needs Oracle Instant Client |
| Cloud | OCI tenancy with GenAI service, Object Storage, IAM |
| Auth | ADB wallet + `~/.oci/config` profile |

```bash
pip install -r requirements.txt
```

---

## Setup

**1. Create your config**

```bash
cp agent_builder_config.ini.template agent_builder_config.ini
```

Fill in your region, compartment OCID, database user, TNS alias, wallet
directory, and Instant Client path. Every field is documented inline.

`agent_builder_config.ini` is `.gitignore`d — it holds your tenancy OCIDs and
local paths. Keep it that way.

**2. Supply the database password out-of-band**

No password goes in the config file. Resolution order, per user:

1. OCI Vault secret mapped to that user — `[secrets] <USER> = <secret OCID>`
2. `OCI_DB_PASSWORD_<USER>` environment variable
3. `OCI_DB_PASSWORD` environment variable
4. Interactive prompt

`[de] secret_ocid` is different: it holds the *target schema's* password, used to
create the OML credential, and is only used as a login password when the
connecting user is that schema. A Vault lookup that fails is not fatal — the
reason is printed and resolution continues. Vault lookups need the `oci` SDK in
the interpreter you launch with.

**3. Run**

```bash
python agent_builder.py
```

The tool connects at startup, detects whether you are a DE or a DS, and builds
the menu accordingly.

---

## Two entry points

| Entry point | Who it's for |
|---|---|
| `agent_builder.py` | Full mode — everything, including the Admin Setup menu (IAM dynamic groups and policies, schema bootstrap, Resource Principal, EPE ACL, proxy grants) |
| `agent_builder_ds_only.py` | Restricted mode — pre-flight, build, review & manage, tools. No admin operations. |

In `agent_builder.py` the Admin Setup menu appears only when **both** gates pass:
a `[de]` section exists in the config, *and* the connected user actually holds
`COMMENT ANY TABLE` in the database. Config alone does not unlock it.

---

## Connecting: proxy authentication

The builder is designed so that agent objects land in a shared schema while
people authenticate as themselves:

```
[database]
db_user       = DS_USER      ← you; your password authenticates the session
target_schema = ACME_CORP    ← agent objects are created here
```

This connects as `DS_USER[ACME_CORP]`. The session runs with `ACME_CORP`'s
identity and privileges, the audit trail records `DS_USER`, and `ACME_CORP`'s
password is never needed by anyone. It requires a one-time
`ALTER USER ACME_CORP GRANT CONNECT THROUGH DS_USER;`, and
`ALTER USER ACME_CORP DEFAULT ROLE ALL;` — without the second, a proxied session
starts with no roles enabled and privileges held through a role disappear.

DS and DE logins are deliberately separate accounts. `sql/SA_06_ds_user.sql` and
`sql/SA_06_de_user.sql` (run as ADMIN) create each one with its grants and proxy
connect. The DE login is named in `[de] de_schema`.

Leave `target_schema` blank to connect directly as `db_user` instead.

---

## The menu

**Pre-flight check** — three independent checks over the proxy connection:
DS provisioning (as `[database] db_user`), DE provisioning (as `[de] de_schema`,
falling back to `db_user` with a warning), and target-schema configuration. Each
failure prints the exact statement that fixes it.

The checks perform the operation rather than read the privilege views, which
mislead in several ways: source-table access is a real `SELECT`, and package
`EXECUTE` is proved by compiling a call that never runs. Read the symbols
carefully:

| | |
|---|---|
| ✓ | Confirmed working |
| ✗ | Confirmed missing — fix shown inline |
| ⚠ | **Inconclusive**, not failed — usually a dictionary view that returns nothing useful inside a proxied session, or an ADMIN-only function. Verify as ADMIN before chasing it. |

**Build** — three ways in:

- *Conversational* — a 7-step guided interview (project & schema → data sources →
  source details → object names → optional analysis tools → agent role → task & team).
  Steps 4, 5 and 7 are collected by the application, not the LLM, so object names
  are never silently renamed.
- *Import from CSV* — see `examples/project_template.csv`.
- *Import from Word doc* — a two-column `Field | Value` table; start from
  `examples/sample_agent_spec.docx`. Any `COMMENT ON`
  SQL blocks in the doc are parsed and stored as pre-approved NL2SQL comments.
  Choosing "Proceed" jumps straight to the final step without re-prompting.

**Review & manage** — list objects, view detail plus tool invocation history,
edit a tool description / agent role / task instruction in place, change a single
profile attribute, manage NL2SQL comments, delete, rebuild the whole stack from
the saved spec, or run an interactive multi-turn test conversation against a team.

**Tools** — browse OCI GenAI models and switch the active one; create an Object
Storage bucket and upload RAG documents.

**Admin Setup** (DE only) — IAM dynamic group and policy, schema creation,
Resource Principal, package/role grants, `pyqAppendHostAce` EPE ACL, Vault
credential, and DS/DE proxy grants. Every destructive step prints the SQL and
asks before executing; `--dry-run` shows the statements without running them.
It grants to DS and DE users but does not create them — use `sql/SA_06_*.sql`.

---

## Layout

```
agent_builder.py                    Full entry point (DE + DS)
agent_builder_ds_only.py            Restricted DS entry point
agent_builder_config.ini.template   Copy to agent_builder_config.ini

core/
  config.py         Two-file config loader (user file + runtime overlay)
  db.py             ADB connect (wallet + proxy), execute, query helpers
  llm.py            OCI GenAI chat completions
  oci_clients.py    OCI SDK client factory
  spec_builder.py   Captured facts → normalized spec dict
  spec_parser.py    Extracts the JSON spec block from model output
  spec_validator.py Validates the spec's shape
  sql_builder.py    Deterministic PL/SQL generator — no LLM involved
  state.py          Project JSON save / load / list / resume, run logs

modules/
  preflight.py         Combined DE-setup verification
  preflight_dsde.py    DS and DE user provisioning checks
  preflight_schema.py  Target-schema bootstrap checks
  conversation.py      The 7-step guided build loop
  docx_import.py       Word doc config importer + COMMENT ON parser
  project_import.py    CSV and shared import logic
  comments.py          NL2SQL comment management
  review.py            All post-build review & manage operations
  object_storage.py    Bucket creation + RAG document upload
  list_models.py       OCI GenAI model browser
  grant_check.py       Grant audit
  debug_menu.py        Debug toggles and diagnostics
  check_config.py      Config file validator

sql/          SA_06_ds_user.sql / SA_06_de_user.sql — create the DS and DE logins
examples/     Sanitized sample Word spec, CSV project template, NL2SQL comment files
docs/         ACME_AI_Chat_Implementation_Guide.docx (other contents gitignored)
templates/    LLM system prompt and the fallback codegen prompt
uploads/      Your own spec documents (contents gitignored)
projects/     Saved project specs and per-project logs (gitignored)
logs/         Per-session runtime logs (gitignored)
```

---

## Configuration precedence

Two files, deliberately:

- `agent_builder_config.ini` — yours, hand-edited, never written by the tool.
- `agent_builder_config.runtime.ini` — machine-written overlay for in-session
  picks (model selection, RAG upload location, debug toggle).

The runtime file is **wiped at process start**, so nothing you pick interactively
survives the session. To make a change permanent, edit the user config.

For an LLM profile attribute, the resolution order is:

```
project spec  →  runtime.ini  →  config.ini  →  built-in fallback
```

A project whose Word doc sets an explicit `LLM Chat Model` wins over everything,
because that lands in the project spec.

---

## Known limitations

| | |
|---|---|
| Roles must be enabled | NL2SQL honors table privileges granted through a role, but only when the role is enabled in the session. A newly granted role is not a default role: run `ALTER USER <schema> DEFAULT ROLE ALL`. |
| Attribute edits drop and recreate | The profile-attribute option drops and recreates the profile. `DBMS_CLOUD_AI.SET_ATTRIBUTE` exists and would change it in place; the tool does not use it yet. If recreate fails, use Rebuild to restore from the saved spec. |
| `pyqGetHostAce` is ADMIN-only | The schema pre-flight EPE ACL check always shows ⚠ over a proxy connection. Verify as ADMIN. |
| Tool history has no conversation id | `USER_AI_AGENT_TOOL_HISTORY` has no `conversation_id` column, so the test runner correlates invocations using a 30-second time window. |
| Model truncation | Some fast models cut off long structured output regardless of `max_tokens`. That only affects script generation if it falls back to the model — `sql_builder` is not affected. |

---

## Security notes

- No secrets in the repo: `agent_builder_config.ini`, `*.runtime.ini`, wallets,
  `logs/`, `projects/`, `uploads/` and working files in `docs/` are all gitignored.
  A filled-in spec document carries your ADB hostname, Vault OCIDs and Object
  Storage namespace — keep it in `uploads/`.
- `logs/` and `projects/` capture live session transcripts, real schema and table
  names, and query results. Check before you ever commit or share them.
- OML4Py tool bodies should read the schema password from OCI Vault at run time,
  as the Python tool in `examples/sample_agent_spec.docx` does. The builder only
  asks for `[de] oml_password` when a tool body still contains a
  `<SET … PASSWORD HERE>` placeholder. **Leave it blank** — it then prompts
  (masked) and never writes the value to disk.
- The Admin Setup menu executes real DDL and creates real IAM resources. Use
  `--dry-run` first.

---

## CLI

```bash
python agent_builder.py                          # interactive menu
python agent_builder.py --config other.ini       # alternate config
python agent_builder.py --option 1               # run menu option 1 directly
python agent_builder.py --dry-run                # admin actions print, don't execute
```

---

## Further reading

- `docs/ACME_AI_Chat_Implementation_Guide.docx` — the full build this tool automates,
  with every sample question run against a deployed agent
- [Building an AI Financial Analyst with Oracle Select AI Agent](https://basoapbox.com/blog/select-ai-agent-part1)
  — a five-part series; Part 5 covers this builder

---

## License

Universal Permissive License (UPL), Version 1.0 — see `LICENSE`.
