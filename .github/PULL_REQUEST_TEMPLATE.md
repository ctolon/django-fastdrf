## Summary

What this changes for users, and which DRF behaviour it keeps.

## Checklist

- [ ] The pull request targets `dev`.
- [ ] Tests were written first; output, input or query changes have a test
      that compares with plain DRF, including invalid input and custom hooks.
- [ ] Query counts and shared state are covered by tests where they change.
- [ ] `ruff check`, `ruff format --check` and the test suite pass.
- [ ] No Django or DRF class is replaced or patched, and the change is opt-in.
- [ ] `docs/` and the unreleased section of `CHANGELOG.md` describe any
      user-visible change.

For a performance change, include the workload, the environment and repeated
measurements before and after.
