# polyphony-chitra-monitord@.service example (instance template)

Fleet-style deployments run one isolated monitor instance per lane namespace
instead of the shared single-instance unit. This is an *example* template:
Chitra itself ships no `polyphony-*` units; the fleet control plane owns the
rendered copies and per-instance drop-ins.

```ini
[Unit]
Description=Polyphony Chitra monitord (%i isolated composed monitor)
Documentation=https://github.com/ReticleWorks/chitra
After=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
StateDirectory=polyphony-chitra-%i
StateDirectoryMode=0750
Environment=CHITRA_STATE_DIR=/var/lib/polyphony-chitra-%i
# Shadow mode stays on until an operator explicitly turns it off per instance.
ExecStart=/usr/bin/python3 -m chitra.monitord --state-dir ${CHITRA_STATE_DIR}
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Enable one instance with `systemctl enable --now polyphony-chitra-monitord@<instance>.service`.
The instance name must match the other `polyphony-chitra-*@<instance>` units so
every daemon of one declaration shares the same state root.
