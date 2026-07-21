## Change type

Select **all** that apply and fill in the corresponding section(s) below.
Sections whose type is **not** checked may be left as-is.

- [ ] 🆕 New or changed CLI command / flag
- [ ] 🔄 Behavior change (observable difference for users)
- [ ] 🔒 Security-sensitive change (auth, credentials, kubeconfig, secrets)
- [ ] 🔧 Pure refactor / chore (no behavior change)

---

## Summary

Describe the problem and the approach taken.

---

<!-- ✅ Required when "New or changed CLI command / flag" is checked -->
## CLI usage example

> **Required for new/changed commands or flags.**
> Show the command(s) as a user would type them, including representative
> options, and note which documentation page was updated.

```
# before (omit if this is a brand-new command)
skyportalai <old-command> [flags]

# after
skyportalai <new-command> [flags]
```

Docs updated: <!-- path(s) or "N/A – no public docs affected" -->

---

<!-- ✅ Required when "Behavior change" is checked -->
## Before / after

> **Required for behavior changes.**
> Describe what users experienced before and what they experience after.

**Before:** <!-- observable behavior or output -->

**After:** <!-- observable behavior or output -->

Test coverage: <!-- new/updated test file(s) or "covered by existing tests" -->

---

<!-- ✅ Required when "Security-sensitive change" is checked -->
## Security callout

> **Required for auth, credentials, kubeconfig, or secrets handling.**
> Explain the threat model impact of this change.

**What changed in security-sensitive code:**

**Why it is safe / how the risk is mitigated:**

**Credentials or secrets introduced:** None <!-- or describe and justify -->

---

<!-- ✅ Standard checklist – always complete this section -->
## Validation

- [ ] Tests added or updated where behavior changed
- [ ] `poetry run pytest` passes
- [ ] `poetry run ruff check .` passes
- [ ] Public API/configuration changes are documented
- [ ] No credentials or private run data are included

## Related issue

Closes #
