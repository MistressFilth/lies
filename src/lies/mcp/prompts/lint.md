# Lint the wiki (v$version)

Deterministic + LLM health-check; with `--fix`, apply the
repair agent's plan.

## Walkthrough

1. Run `lies lint --name <wiki>`. Composes the deterministic
   shell (`_build_lint_report`) with the linter sub-agent's
   structured `LintReport`.
2. Output: `<wiki>/wiki/lint-report.md` plus a log entry on
   `wiki/log.md`.
3. To apply fixes: `lies lint --fix`. The repair agent emits
   a `RepairPlan`; the orchestrator routes it through
   `WikiMemoryService.apply_repair_plan` with the same flock
   + atomic-commit envelope as memory plans.
4. Categories the agent never auto-fixes (`safe_to_fix=False`)
   stay in the report verbatim.

## Pitfalls

- `lint --fix` while another sync is in flight → `WikiLockBusy`.
  Wait or pass `--force`.
- The linter sub-agent is fail-soft; mechanical findings
  always reach the repair agent, even on LLM failure.
