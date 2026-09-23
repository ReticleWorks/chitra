"""Harness-neutral lane adapters: one interface over tmux lanes and orb lanes.

The contract lives in ``chitra.adapter.contract``; plugs resolve through
``chitra.adapter.registry``. This package deliberately re-exports nothing —
``dispatch`` and ``dispatchd`` import the contract submodule directly, and
plugs import the transport modules, so eager re-exports here would cycle.
"""
