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
save, and re-run. The LLM is used for the *conversation*, never for generating
the SQL — object names, table lists, and comment metadata come from captured
facts, not from a model.

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

No password goes in the config file. Resolution order:

1. OCI Vault secret (`[de] secret_ocid`)
2. `OCI_DB_PASSWORD_<USER>` environment variable
3. `OCI_DB_PASSWORD` environment variable
4. Interactive prompt

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
`ALTER USER ACME_CORP GRANT CONNECT THROUGH DS_USER;`.

Leave `target_schema` blank to connect directly as `db_user` instead.

---

## The menu

**Pre-flight check** — three independent checks over the proxy connection:
DS provisioning, DE provisioning, and target-schema configuration. Each failure
prints the exact statement that fixes it. Read the symbols carefully:

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
- *Import from Word doc* — a two-column `Field | Value` table. Any `COMMENT ON`
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
  spec_builder.py   Captured facts → normalised spec dict
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

examples/     CSV project template, sample NL2SQL comment files
templates/    LLM system prompt and codegen prompt
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
| Role grants don't satisfy NL2SQL | `SELECT ANY TABLE` is silently ignored. Every source table needs an explicit `GRANT SELECT ON <table> TO <user>`. |
| `UPDATE_PROFILE` not available | On ADB versions lacking it, the profile-attribute option drops and recreates the profile. If recreate fails, use Rebuild to restore from the saved spec. |
| Generated smoke tests | The emitted verification block uses `SYS_GUID()` for the conversation id and fails with *Invalid value for conversation id*. The interactive test runner uses `CREATE_CONVERSATION` correctly — test there. |
| `pyqGetHostAce` is ADMIN-only | The schema pre-flight EPE ACL check always shows ⚠ over a proxy connection. Verify as ADMIN. |
| Tool history has no conversation id | `USER_AI_AGENT_TOOL_HISTORY` has no `conversation_id` column, so the test runner correlates invocations using a 30-second time window. |
| Model truncation | Some fast/non-reasoning models cut off long structured output regardless of `max_tokens`, which breaks PL/SQL generation. Prefer a reasoning/instruct model and keep `max_tokens` at 8000+. |

---

## Security notes

- No secrets in the repo: `agent_builder_config.ini`, `*.runtime.ini`, wallets,
  `logs/`, and `projects/` are all gitignored.
- `logs/` and `projects/` capture live session transcripts, real schema and table
  names, and query results. Check before you ever commit or share them.
- `[de] oml_password` exists for OML4Py custom tools whose token-refresh PL/SQL
  needs the schema password inline. **Leave it blank** — the builder then prompts
  for it (masked) and never writes it to disk.
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

## License

No license is granted yet — add one before relying on this in your own work.
