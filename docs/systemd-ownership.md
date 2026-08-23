# Systemd unit ownership inventory

`packaging/systemd/ownership.json` records the core Chitra daemon units and
their deployment variants. It covers the four package daemons and the lane
template owned by Chitra, the ten Chitra package units shipped by Fleet, the
concrete and templated Chitra daemons kept by Polyphony, and the one
byte-identical rate-limit template and timer mirrored by Fleet and Polyphony.
Auxiliary adapter, board-publishing, and unrelated host units are outside this
inventory.

The Chitra package tests verify the Chitra-owned hashes in every checkout.
They do not assume that Fleet or Polyphony is checked out beside Chitra. To
run the cross-repository check, point the test at explicit repository roots:

```bash
CHITRA_OWNERSHIP_FLEET_ROOT=/path/to/fleet-repo \
CHITRA_OWNERSHIP_POLYPHONY_ROOT=/path/to/polyphony \
python -m pytest tests/test_systemd_units.py -k ownership
```

The cross-repository test checks every declared external hash, the required
instance tokens, and byte equality for the shared rate-limit files. It skips
when either root is absent so normal package CI remains hermetic. A release or
consolidation job that claims cross-repository ownership verification must set
both variables.
