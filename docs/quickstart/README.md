# Getting Started

## Install

Requires Python 3.12+ and `tmux` (chitra shells out to the `tmux` binary; there is no Python tmux dependency).

```bash
pip install chitra-monitor  # or: pip install git+https://github.com/ReticleWorks/chitra.git@<tag>
```

Replace `<tag>` with a released version from the [tags page](https://github.com/ReticleWorks/chitra/tags), or drop `@<tag>` to install from the default branch.

For local development:

```bash
git clone https://github.com/ReticleWorks/chitra.git
cd chitra
pip install -e '.[test]'
pytest
```

## Quickstart

```bash
pip install chitra-monitor  # or: pip install git+https://github.com/ReticleWorks/chitra.git@<tag>
```

Replace `<tag>` with a released version from the [tags page](https://github.com/ReticleWorks/chitra/tags), or drop `@<tag>` to install from the default branch.

Requires Python 3.12+ and `tmux` on the host. See [Install](#install) for local development setup, [Configuration](../configuration/) for environment variables, and the main [README](../README.md) for what chitra actually does to a pane.

## Your first dispatch

This example shows how to queue and deliver a message into a live tmux session.

### 1. Start a tmux session

```bash
tmux new-session -d -s test-session 'claude'
```

This example requires a supported agent client that writes a transcript Chitra can bind to the tmux pane.

### 2. Create a queue directory and dispatch order

Chitra expects a JSON queue in a configured directory (default `$CHITRA_STATE_DIR/queue`). Each order is one JSON file with a `.json` suffix.

```bash
mkdir -p /tmp/chitra-demo/queue/orders
mkdir -p /tmp/chitra-demo/queue/results

# Create a dispatch order
cat > /tmp/chitra-demo/queue/orders/order-001.json << 'EOF'
{
  "order_id": "order-001",
  "session_ref": "localhost:test-session:0.0",
  "nudge": "Continue the queued task."
}
EOF
```

### 3. Run dispatchd once

```bash
export CHITRA_STATE_DIR=/tmp/chitra-demo
dispatchd --queue-dir /tmp/chitra-demo/queue --once
```

This runs one pass of the dispatch daemon. It drains the queue, acquires a lock on the session `test-session`, pastes the message into its input, verifies delivery by grepping the session's transcript, and writes a result file.

### 4. Check the session

```bash
tmux capture-pane -t test-session -p
```

You should see the command executed and its output.

### 5. Verify the ledger

```bash
cat /tmp/chitra-demo/ledger.jsonl
```

Each successful delivery is HMAC-signed and recorded here. The ledger is append-only and crash-safe; dispatchd never redelivers an order that already has a result file.

## What's next

- Read [Concepts](../concepts/) to understand Chitra's delivery core and persistent supervision layer.
- Check the [Daemons reference](../daemons/) to learn what each tool does.
- See [Configuration](../configuration/) to set up routing and policy for your deployment.
- For systemd integration and running daemons continuously, refer to the main [README](../README.md).
