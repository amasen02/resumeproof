# Demo guide

Run:

```powershell
resumeproof demo --output-dir demo-output
```

Expected observations:

1. The first child process exits with code 86 after committing one idempotent effect.
2. The journal still says `running`; the restarted process calls the cooperating tool with the same key.
3. The tool returns the original receipt with `replayed: true`; repeated resume leaves its effect count at one.
4. A second child crashes after a non-idempotent effect. Restart records `unknown` and does not call that tool again.
5. The two workflows therefore produce two total simulated effects: one safe example and one deliberately ambiguous example.

`receipt.json` is the machine-readable result. `timeline.txt` is a readable replay. Use `sqlite3` or any SQLite browser to inspect the two databases independently. The command requires a new or empty output directory and refuses to overwrite any prior evidence. Choose a different directory for each run.

The crash is real process termination, while the effect is intentionally local and simulated. Exit code 86 is ResumeProof’s injection marker, not an operating-system failure classification.
