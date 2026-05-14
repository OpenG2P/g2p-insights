# `insight-onboard` — registry onboarding CLI

Automates the steps in the [onboarding runbook](onboarding-new-registry.md):
inspects a registry's schema, classifies columns by PII risk, proposes safe
views + materialised views, and applies the result (creates the views, the
`insight_ai` role with grants, the operational catalog YAML, and a Helm
values overlay).

Three commands: `scan`, `apply`, `audit`. The `scan` and `audit` steps are
read-only; `apply` defaults to interactive confirmation and supports
`--dry-run` so you always see the SQL before anything runs.

---

## Why this exists

Onboarding a new registry by hand involves: classifying every column, writing
view DDL, creating roles and grants, drafting a catalog YAML, and verifying
no PII leaked through. With 20+ tables × 30+ columns each it's mechanical
work where mistakes leak data. The CLI turns it into:

1. `scan` — produces a reviewable **proposal.yaml**
2. *human reviews the YAML in their editor*
3. `apply proposal.yaml` — generates and runs the DDL, writes the catalog
   and Helm overlay
4. `audit` — confirms no PII is visible to the new role

About 30 min of human time, vs half a day per registry by hand.

---

## Install

The tool ships as a Python package in `backend/scripts/insight_onboard`. It
runs in the same venv as the backend:

```bash
cd /Users/puneet/Documents/OpenG2P/repos/g2p-insight/backend
source .venv/bin/activate            # the venv from dev_run.sh setup
pip install -e .                     # gives us psycopg, pyyaml, anthropic, openai
```

Now you can run:

```bash
python -m scripts.insight_onboard --help
```

---

## End-to-end workflow

### 1. Scan

Connect as a DBA who can read every table. The tool only does `SELECT`.

```bash
python -m scripts.insight_onboard scan \
    --registry-db postgresql://dba:***@registry-host:5432/farmer_registry \
    --out /tmp/proposal.yaml
```

Output:

```
[scan] connecting to postgresql://dba:***@registry-host:5432/farmer_registry
[scan] found 23 tables
[scan]   inspecting g2p_register_individuals
[scan]   inspecting g2p_register_households
[scan]   ...
[scan] wrote /tmp/proposal.yaml
[scan] summary:
         5 analytical tables
         18 skipped tables
         17 PII columns identified (will be dropped)
         0 columns need human review (`pii: '?'`)
         2 materialized views suggested

Next: review /tmp/proposal.yaml, then run `insight-onboard apply ...`
```

Inspect `/tmp/proposal.yaml` in your editor. Every column has a
classification (`pii` / `quasi` / `safe`), a confidence (`high` / `medium`
/ `low`), and a reason. Edit anything that looks wrong.

#### What the classifier does

Three layers, each only handles what the previous didn't:

| Layer | Method | What it catches |
|-------|--------|----------------|
| 1. Token-set matching on column name | `gps_latitude` → tokens `{gps, latitude}` → both PII keywords | Most well-named columns |
| 2. Sample-value shape inspection | Reads 20 distinct values; phone-shaped → `pii`, low cardinality + text → `safe` enum, free-text → suspect | Generically-named columns |
| 3. (Optional) LLM-assisted | Sends column metadata + value SHAPES (never raw values) to the configured LLM, gets back a classification + reasoning | The 5-15% the heuristics couldn't classify |

Layer 3 is opt-in via `--use-llm`:

```bash
python -m scripts.insight_onboard scan --use-llm \
    --registry-db ... --out /tmp/proposal.yaml
```

This uses the same `INSIGHT_LLM__*` env vars as the backend, so whatever LLM
you've configured for production (Claude / OpenRouter Qwen / on-prem vLLM)
is what classifies. Cost: ~$0.10 per registry, one-time.

### 2. Review the proposal

The YAML has every table and column with a proposed classification and the
reason. Reviewers do three things:

1. **Override any wrong classification** — set `pii:` to the correct value
   and `keep:` to `true`/`false`.
2. **Resolve any `pii: '?'` entries** — these are columns the heuristics
   couldn't classify. Read the `reason`, decide, and replace `?` with a
   real value.
3. **Adjust the proposed materialized views** — edit the `join:` clauses,
   add/remove indexes, change the refresh schedule.

You can also adjust:

- `deployment.country_code` and `default_currency` (required)
- `deployment.k_anonymity` (raise per regulatory requirement)
- Move tables from `skipped` to `analytical_tables` if the heuristic was
  too aggressive about excluding them

`apply` will refuse to run until every `?` is resolved and
`country_code` is set.

### 3. Apply — dry-run first

Always do a dry-run first to see the DDL:

```bash
python -m scripts.insight_onboard apply \
    --registry-db postgresql://dba:***@registry-host:5432/farmer_registry \
    --dry-run \
    --catalog-out /tmp/catalog.yaml \
    --helm-out /tmp/values-onboard.yaml \
    /tmp/proposal.yaml
```

Output: the full SQL that would execute, followed by the generated catalog
and Helm overlay. **No DB changes.**

### 4. Apply — for real

Drop `--dry-run`. The tool will print the SQL again and ask for explicit
confirmation:

```
[apply] about to execute the following SQL:
   ... full SQL block ...
Proceed? (yes/no): yes
[apply] DDL executed
[apply] PII-leak audit: 0 leaks ✓
[apply] wrote catalog → /tmp/catalog.yaml
[apply] wrote Helm values overlay → /tmp/values-onboard.yaml

[apply] done. Save this `insight_ai` password into your K8s secret:
  Ak83mVxQp9Lz7rNwT4hYbCf2sD6jKvEu

  kubectl create secret generic g2p-insight-registry-creds \
      --from-literal=insight-ai-password='Ak83mVxQp9Lz7rNwT4hYbCf2sD6jKvEu'
```

The password is generated fresh each run (32 random chars). Save it
immediately — there's no way to recover it later. If you want a specific
password, pass `--role-password`.

### 5. Audit — verify no PII leaked

The audit is a separate command in case you ever want to re-run it later
(e.g., after a DDL change to the registry):

```bash
python -m scripts.insight_onboard audit \
    --registry-db postgresql://dba:***@registry-host:5432/farmer_registry \
    --proposal /tmp/proposal.yaml
```

It connects, `SET ROLE insight_ai`, lists every column visible, and diffs
against the proposal's `pii_columns_in_source`. Output:

```
audit: 0 leaks ✓
```

…or, if anything's wrong:

```
AUDIT FAILED — 2 PII columns visible to insight_ai:
  - g2p_register_individuals.full_name
  - g2p_register_individuals.primary_phone
```

`apply` runs the audit automatically at the end and refuses to succeed if
there are leaks. The standalone `audit` command is for re-checks.

---

## What gets created

| Artifact | Location | What for |
|----------|----------|---------|
| Safe views (`v_*_safe`) | Registry Postgres, `public` schema | One per analytical table; drops PII columns |
| Materialised views (`mv_*`) | Registry Postgres, `public` schema | Pre-joined / pre-aggregated for fast reads |
| `insight_ai` Postgres role | Registry Postgres | `SELECT` only on safe views; explicit `REVOKE` on PII tables; statement timeout 30s |
| `catalog.yaml` | Wherever `--catalog-out` points (default `catalog.yaml`) | Ship this into the K8s pod via Helm (`insightBackend.appCatalog.schemaYaml`) |
| `values-onboard.yaml` | Wherever `--helm-out` points | Helm values overlay scaffolding — fill in registryDBHost, hostname, etc. |
| `insight_ai` password (stdout) | Console | Put into a K8s secret yourself; the tool does NOT write it anywhere |

---

## Heuristics — keyword sets

The token-based classification in [heuristics.py](../backend/scripts/insight_onboard/heuristics.py)
covers most OpenG2P registries out of the box. Currently:

- **PII keywords**: name, fullname, surname, phone, mobile, email, address,
  street, gps, lat/lng, latitude/longitude, fayda, aadhaar, ssn, passport,
  dob, birthdate, iban, mpesa, rid, password, secret
- **Multi-token PII**: date+birth, national+id, functional+id, registry+id,
  contact+person, contact+number, account+number, exact+age, api+key
- **Quasi-identifier keywords**: age, gender, sex, marital, ethnicity,
  religion, language, disability, orphanhood, plw, displacement, refugee,
  idp, kebele, village, occupation, employment
- **Safe keywords**: region, zone, woreda, district, pmt, income,
  livelihood, livestock, headship, status, score, created/updated/registered
- **Multi-token safe**: age+bracket, age+group, pmt+score, monthly+income,
  household+size, headship+type

To extend per-country (e.g., to detect a country-specific national-ID
column name), edit `heuristics.py` and add to `_PII_KEYWORDS` or
`_PII_MULTI_TOKEN`. A future enhancement: load these from
`backend/catalog/pii_recognizers.yaml` so they're per-deployment.

---

## Tables auto-skipped

The scanner skips tables that match these patterns (because they're
operational / metadata, not analytical):

| Pattern | Reason |
|---------|--------|
| `^g2p_intake_form_` | Raw intake submissions, pre-validation |
| `^dedup(_|lication_)` | Deduplication tracking, operational |
| `_queue$` | Background-job queue |
| `^g2p_attributes?$` / `^g2p_attribute_values$` | Schema metadata |
| `^g2p_input_mechanisms?$` | Input-channel registry |
| `^g2p_intake_form_definitions$` | Form-definition metadata |
| `^data_models?$` | Schema definitions |
| `^alembic_version$` | Migration state |
| `_history$` | Mirror of base; raises PII surface unnecessarily |
| `_log$` / `_logs$` / `_audit$` | Audit/log tables |

To force a "skipped" table back in, move it to `analytical_tables` in the
proposal YAML before `apply`.

---

## What this tool deliberately does NOT do

- **Modify your raw registry tables** — never. It only `CREATE`s views and
  the `insight_ai` role.
- **Decide policy** — k-anonymity threshold, regulatory classifications
  (`pii` vs `quasi` for borderline columns) are operator decisions.
- **Store the role password** — printed once to stdout; you put it in your
  K8s secret. We don't want it persisted anywhere by the tool.
- **Configure the LLM provider** — uses your existing `INSIGHT_LLM__*` env
  vars; if you haven't set them and pass `--use-llm`, classification falls
  back to "unclear" for ambiguous cases.
- **Deploy Helm** — the tool writes a Helm overlay file; the human runs
  `helm upgrade` with it.

---

## Limitations / future work

| Limitation | Workaround | Roadmap |
|------------|-----------|---------|
| Doesn't sample VALUES across multiple rows for the LLM (only shape summaries) | Set `pii` manually in proposal | Optional — would require more careful PII filtering of sampled values before sending to LLM |
| FK-based MV suggestion is simple (just individuals × households) | Edit `materialized_views:` in the proposal | Could be smarter — detect richer join patterns |
| No undo for `apply` | Manually `DROP VIEW`/`DROP ROLE`; or run `apply` again with a different proposal | Could add an `unapply` command from a recorded `applied.yaml` |
| Heuristic keyword set isn't per-country | Edit `heuristics.py` | Load keyword sets from `pii_recognizers.yaml` so they're per-deployment |
| Audit only covers PII listed in proposal | Run `audit` periodically as part of CI | Add CI workflow that runs audit after every deploy |

---

## See also

- [onboarding-new-registry.md](onboarding-new-registry.md) — the hand-execution runbook this tool automates
- [README §5 — Schema catalog](../README.md#5-schema-catalog--generation-and-structure) — format of the catalog YAML the tool writes
- [README §8 — PII protection](../README.md#8-pii-protection) — the layered model the tool enforces
