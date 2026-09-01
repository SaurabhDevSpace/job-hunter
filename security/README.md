# Endpoint-security allowlist request

Material for asking Ciena IT/InfoSec to unblock the Python developer toolchain that the
host endpoint agent is killing on this workstation.

## Files

| File | What it is |
|---|---|
| `logicgate-spe-form.md` | **If you're filling the LogicGate SPE form**, start here — field-by-field answers for record `Exception-1220`. |
| `ticket-body.txt` | The underlying request, sections 1–9. Attach this to the form, or send it directly if a non-SPE channel turns out to be correct. |
| `security-allowlist-request.md` | Supporting evidence — the same facts plus the diagnostic reasoning. |
| `collect_blocked_binaries.sh` | Re-tests each binary and prints current SHA-256 values. |

> **Blocking product confirmed: ThreatLocker**, application control in default-deny mode.
> That means this is a routine **allowlist addition**, not a policy exception — the agent
> is working as designed, and no policy needs to bend. Don't complete the LogicGate SPE
> form until whoever owns ThreatLocker approvals confirms it's needed;
> `logicgate-spe-form.md` has the message to send them.

## Before you file

Hashes rotate on every toolchain upgrade, so regenerate them rather than trusting the
tables in the documents:

```bash
bash collect_blocked_binaries.sh
```

`STATUS`: `SIGKILL` = killed by the agent, `OK` = runs fine, `rc=N` = some other failure.
Paste the refreshed values into `ticket-body.txt` before sending.

## The short version

Two symptoms, one likely cause — per-file reputation scoring:

- **Interpreters are SIGKILLed** (`Killed: 9`, exit 137). Affects the uv-managed Pythons
  (3.12.10, 3.10.17) and the miniconda environments including `bpa_ai_sdk_py_3_10` and
  `gen_ai_py3_12`.
- **Compiled `.so` files can't be mapped** (`mmap(size=...) failed with errno=1`). Affects
  `pydantic_core`, `numpy` and `lxml` — so langflow, FastAPI and the LangChain stack fail
  to import.

`bpa_langflow`, both `BP-AGENTS` repos and both `bpa_ai_sdk` sub-projects have venvs that
symlink straight to the blocked interpreters, so none of them can be run. Since a venv is
just a symlink, the exclusion must target the symlink **destination**, not the venv.

The ask has three tiers: hash exclusions for the named binaries; **directory** exclusions
so `pip install` / `uv sync` doesn't re-trigger the block under new hashes; and a standard
developer-workstation policy profile if one exists — the only durable fix, since hash and
path exceptions need re-filing at every upgrade.

The ticket also asks IT to share Ciena's approved interpreter/toolchain locations, if such
a list is published — conforming to the standard layout beats requesting exceptions to it.

## Also worth raising

Docker Desktop starts but refuses to build or run without `ciena` org sign-in. Different
owner (licensing, not endpoint security), but it removes the obvious containerised
workaround, so the ticket mentions it in section 8.
