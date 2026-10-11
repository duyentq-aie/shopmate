"""Run only by tests/test_ci.py in a child pytest (SHOPMATE_PROBE=1), to prove that conftest installs the network guard when SHOPMATE_BLOCK_NETWORK=1.
Not collected by the normal run (the file name does not start with test_)."""

import os
import socket

import pytest


@pytest.mark.skipif(os.environ.get("SHOPMATE_PROBE") != "1", reason="run by tests/test_ci.py only")
def test_probe_a_public_connect_is_refused_by_the_guard():
    with socket.socket() as probe, pytest.raises(OSError, match="SHOPMATE_BLOCK_NETWORK"):
        probe.connect(("8.8.8.8", 53))
