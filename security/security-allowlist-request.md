# Endpoint-security allowlist request — supporting evidence

Backing detail for `ticket-body.txt`. Same facts, plus the diagnostic reasoning that led
to them.

Host: macOS, Apple silicon (arm64)
Requester: `sbrahman@ciena.com`
Workspace: `~/OneDrive - Ciena Corporation/Desktop/bp_codebases/`

## The blocking product — confirmed

**ThreatLocker**, running application control in **default-deny** mode.

Confirmed 2026-09-01 from the agent's own configuration at
`/Library/Application Support/ThreatLocker/AgentConfiguration.plist`:

```
"products" => {
    "applicationControl" => true
    "defaultDeny"        => true
    "ringfencing"        => true
    ...
}
```

System extension: `com.threatlocker.app.agent (6.7.6.2022)`, team ID `MSY54GN4KF`,
`[activated enabled]`.

ThreatLocker has already inventoried the blocked file — its baseline database
(`/Library/Application Support/ThreatLocker/Baseline.db`, table `FileData`) contains:

```
/Users/sbrahman/.local/share/uv/python/cpython-3.12.10-macos-aarch64-none/bin/python3.12
62D77CE55BDCEF80C23CF388998CFF228600B6849D7AA1DE7B9FDA68F0B8EFEC
```

which is exactly the binary that gets killed. It is known to the agent; it simply is not on
the approved list.

**This means the correct remedy is a ThreatLocker application approval, not a security
policy exception.** Default-deny is working as designed — the toolchain just needs adding
to the allowlist.

Other security products also present on the host, none of which show default-deny
behaviour: CyberArk EPM, CrowdStrike Falcon Sensor, Zscaler, Jamf.

### Why this explains every symptom

Default-deny allowlisting decides by file hash, which accounts for observations that
looked strange when the cause was assumed to be malware detection:

| Observation | Explanation under default-deny |
|---|---|
| Copying a blocked `.so` to `/tmp` didn't help | the hash is unchanged, so it's still not on the list |
| Ad-hoc re-signing didn't help | re-signing doesn't add it to the allowlist |
| A trivial C extension compiled locally was refused | a brand-new binary is by definition not on the list |
| Three `rg` copies work, two don't | the working ones were captured during baselining; the newer ones never were |
| No Gatekeeper quarantine `xattr` | correct — this was never Gatekeeper |

---
Agent: **ThreatLocker** application control, default-deny mode (see above)

## Two distinct symptoms

| Symptom | What you see | Affects |
|---|---|---|
| A. Process kill | `Killed: 9`, shell exit code `137`, no output; agent popup naming the binary | whole Python interpreters, and some other native binaries |
| B. Mapping denial | `dlopen(...): mmap(size=...) failed with errno=1`; `lipo: can't map input file (Operation not permitted)` | compiled `.so` extension modules inside Python wheels |

Both are content/hash-based, **not** path-, size-, or architecture-based. See §4.

---

## 1. Blocked interpreters (symptom A)

All re-verified 2026-09-01 — every one exits `137` with no output.

| Path | Version / use | SHA-256 |
|---|---|---|
| `~/.local/share/uv/python/cpython-3.12.10-macos-aarch64-none/bin/python3.12` | 3.12.10, uv-managed | `62d77ce55bdcef80c23cf388998cff228600b6849d7aa1de7b9fda68f0b8efec` |
| `~/.local/share/uv/python/cpython-3.10.17-macos-aarch64-none/bin/python3.10` | 3.10.17, uv-managed | `39cd91cd01bc2958770d73a623917ba706bd6e855dfd68f94e1a555a2637713f` |
| `~/miniconda3/bin/python3.12` | conda base (target of `bin/python3`) | `417e7a965656c4755969ba8d855759addbe4bc0610f83c4ea51322d02c4cfef1` |
| `~/miniconda3/envs/bpa_ai_sdk_py_3_10/bin/python3.10` | BPA AI SDK env | `36bfabe84b467c7191f50b7862169ae5e8f6e20ee9acbcb9f180af69d844425b` |
| `~/miniconda3/envs/gen_ai_py3_12/bin/python3.12` | generative-AI workstream env | `97bf69cad281b7ee8d276bc2b1b9ae1e0d48f9b3566cf11d65ae335bd1691a02` |
| `~/miniconda3/envs/py313/bin/python3.13` | 3.13 | `72f0cbd6ed040cb7957dc0301333bb7e23b630179758f25f327c3d9e67a2326e` |

Not blocked, for contrast:

| Path | Version | Note |
|---|---|---|
| `/usr/bin/python3` | 3.9.6 | Apple-signed; too old for current project code |
| `/usr/local/bin/python3` | 3.14.7 | python.org build |

### Why venv paths are not the thing to allowlist

A project virtualenv is only a **symlink** to the real interpreter, so the venv appears
blocked while the actual offending file lives elsewhere. Every uv-provisioned environment
in the workspace is therefore dead — verified 2026-09-01, all five exit `137`:

| Repo venv (relative to workspace root) | Resolves to |
|---|---|
| `BP-AGENTS/bp-knowledge-retriever-agent/.venv/bin/python` | uv cpython-3.12.10 |
| `BP-AGENTS/bp-supervisor-agent/.venv/bin/python` | uv cpython-3.12.10 |
| `bpa_langflow/.venv/bin/python` | uv cpython-3.12.10 |
| `bpa_ai_sdk/bpa-gen-ai/.venv/bin/python` | uv cpython-3.12.10 |
| `bpa_ai_sdk/bpa-base/.venv/bin/python` | uv cpython-3.10.17 |

The exclusion has to target the symlink **destination** — the two uv interpreters in the
table above — not these venv paths.

## 2. Blocked tooling (symptom A)

Verified 2026-09-01: only **two** of the five bundled `ripgrep` copies are blocked.

| Path | SHA-256 | Status |
|---|---|---|
| `~/.cache/pkg/copilot-runtime-copilot-darwin-arm64-9711ba36337fb291/copilot-darwin-arm64/ripgrep/bin/darwin-arm64/rg` | `c8e8fb805e044b038926419d06ba09286800c5ec8cf116448b856d33bd55cca1` | **SIGKILL** |
| `~/.cache/pkg/copilot-runtime-copilot-darwin-arm64-55d38403543c9b25/copilot-darwin-arm64/ripgrep/bin/darwin-arm64/rg` | `eabe1d5ad6eee013f09170e8bb0a7cd5377c4865a2ed8afe155bfd68caf58a1c` | **SIGKILL** |
| `~/.cache/pkg/28bcfff…/rg`, `~/.cache/pkg/7cfc3fe…/rg`, `~/.cache/pkg/fec9cb0…/rg` | — | OK |

These are the code-search binaries shipped with GitHub Copilot CLI; the blocked ones cause
`ripgrep terminated by signal SIGKILL` in the editor. That three functionally identical
siblings run fine is a useful data point — it rules out a policy against ripgrep as such.

## 3. Blocked compiled wheels (symptom B)

Location pattern: `<repo>/.venv/lib/python3.X/site-packages/…` under the workspace root.
Representative files sampled from `BP-AGENTS/bp-knowledge-retriever-agent` — the same
modules recur in every BPA environment:

| Module | SHA-256 | Why it matters |
|---|---|---|
| `pydantic_core/_pydantic_core.cpython-312-darwin.so` | `e7816581490beecc139cce55406632ce3d3dbdf71e4557f41549a23ba3f8ff5f` | pydantic is a hard dependency of langflow, FastAPI and the LangChain stack — none of them import when this is unmappable |
| `lxml/etree.cpython-312-darwin.so` | `32f7679e697dbc320827bf77d9471ea318163dd1c2c8079d5d6dd2338c029215` | XML handling |
| `websockets/speedups.cpython-312-darwin.so` | `393cb0dd87b31b4b72e9479a18d8b5187c270020f407f881d9a6b44c10542b44` | — |

`numpy` is affected the same way and underpins the data-science / ML work.

A live, on-demand reproducer currently present on the machine:

```
websockets/speedups.cpython-314-darwin.so
SHA256: 00c0fd47be6fef57c5f488ffd0def66745862b9cad4c77b21dde82cbdbf7bd9b
ImportError: dlopen(...): tried: '...' (mmap(size=0xC8B0) failed with errno=1)
```

**Hash exclusions alone will not hold here.** These filenames and hashes change on every
`pip install` / `uv sync` / dependency bump. Directory exclusions are required.

## 4. Why this is an allowlist gap, not a detection

Under default-deny the verdict is "is this hash on the approved list?", not "is this file
malicious". The table above (§"The blocking product") explains each diagnostic result on
those terms. Summarised:

| Test | Result |
|---|---|
| Copy a blocked `.so` to `/tmp` and load it | still refused → hash unchanged, still not listed |
| Ad-hoc re-sign (`codesign -f -s -`) | no change → re-signing doesn't add it to the list |
| Compile a trivial C extension locally, then load it | also refused → a new binary is never on the list |
| Check for quarantine `xattr` | none present → not Gatekeeper |
| Load other native modules in the same directory | `uvloop`, `httptools`, `markupsafe._speedups`, `yaml._yaml`, `_sqlite3`, `_ssl` all fine → these were baselined |
| Compare identical copies of one tool | different verdicts (§2) → only the baselined copies are approved |

All affected files are unmodified official releases (python.org, Anaconda, PyPI, GitHub
Copilot CLI). The hashes above can be checked against VirusTotal. **No malicious behaviour
is alleged or suspected by the agent** — the files are simply absent from the approved
application list.

## 5. The request, in three tiers

1. **Hash approvals** for the binaries in §1 and §2.
2. **Path / folder approvals**, so routine dependency installs don't re-block under new
   hashes:
   - `~/.local/share/uv/python/**`
   - `~/miniconda3/**`
   - `~/.cache/pkg/**`
   - `~/OneDrive - Ciena Corporation/Desktop/bp_codebases/**/.venv/lib/python3.*/site-packages/**`
3. **A standard developer-workstation policy**, if Ciena operates one. ThreatLocker
   supports per-group policies, so moving this host into an engineering computer group
   that already has the standard toolchain approved would beat per-machine approvals.
   Tiers 1 and 2 otherwise need re-filing at each toolchain upgrade.

The ticket also asks the reviewer to share Ciena's approved interpreter and toolchain
locations, if such a list exists, so the workstation can be conformed to it rather than
excepted from it.

## 6. Separate item, different owner

Docker Desktop starts but refuses to build or run: *"Sign in to continue using Docker
Desktop. Membership in the [ciena] organization is required"*, enforced via
`/Library/Application Support/com.docker.docker/registry.json`. That's a Docker
licensing / org-membership matter, not an endpoint-security exclusion — but it's worth
mentioning in the ticket because it removes the obvious containerised workaround.

## 7. Regenerating the evidence

Hashes go stale on every toolchain upgrade, so regenerate rather than trusting the tables:

```bash
bash collect_blocked_binaries.sh
```

`STATUS` column: `SIGKILL` = killed by the agent, `OK` = runs, `rc=N` = some other failure.
