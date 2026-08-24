# Governed Tophand moving seam

`tools/run_cross_repo_moving_seam.py` runs the accepted Chitra contract against
Adapter and Fleet source checkouts. It never imports an installed package and
does not contact a host, provider, credential store, or network endpoint.

The harness covers:

- authenticated close and the exact structured prior owner;
- a fresh-process same-session resume with a rotated process token;
- a Fleet reply lost after its durable resume and reopen markers exist;
- fresh Adapter recovery from the durable pending operation;
- post-resume status and send with one stop and one physical resume.

Run against the in-progress candidates:

```sh
python3 tools/run_cross_repo_moving_seam.py \
  --adapter-root /private/tmp/adapter-governed-resume-20260824 \
  --fleet-root /private/tmp/fleet-final-composition-20260824
```

When exact tips are accepted, replace the roots and pin both revisions:

```sh
python3 tools/run_cross_repo_moving_seam.py \
  --adapter-root /path/to/adapter-worktree \
  --adapter-revision <accepted-adapter-commit> \
  --fleet-root /path/to/fleet-worktree \
  --fleet-revision <accepted-fleet-commit> \
  --require-clean-candidates
```

The first output line is a JSON source manifest. It records each checkout HEAD,
dirty state, and tracked-diff digest so a provisional moving-seam result cannot
be mistaken for an exact-tip result.
