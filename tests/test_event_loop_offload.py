"""Slow read tools must not stall the server.

The MCP SDK calls a synchronous tool body directly inside the request coroutine
(func_metadata.call_fn_with_arg_validation: `return fn(**arguments)`), so anything slow
in a tool stops the whole server answering. Measured on the reference server on
2026-09-02: reading one three-page scanned PDF ran OCR for 24 seconds and /health timed
out four times in a row -- the same shape as the 2026-08-28 outage, where back-to-back
full-vault searches blocked /health for about 25 seconds.

The slow read paths therefore run in a worker thread. Writes deliberately do not: the
event loop serialises them today, and that accidental serialisation is a real safety
property for concurrent edits to the same file.
"""

import asyncio
import inspect
import time

import pytest

from obsidian_vault_mcp import server
from obsidian_vault_mcp.rate_limit import (
    current_auth_principal,
    reset_current_auth_principal,
    reset_rate_limits,
    set_current_auth_principal,
)

SLOW_READS = [
    "vault_read",
    "vault_batch_read",
    "vault_search",
    "vault_semantic_search",
    "vault_analytics_summary",
    "vault_analytics_findings",
]

STAY_ON_THE_LOOP = [
    "vault_write",
    "vault_edit",
    "vault_append",
    "vault_patch",
    "vault_str_replace",
]


@pytest.mark.parametrize("name", SLOW_READS)
def test_slow_reads_are_offloaded(name):
    assert inspect.iscoroutinefunction(getattr(server, name)), (
        f"{name} runs on the event loop; a slow call would stall every other request"
    )


@pytest.mark.parametrize("name", STAY_ON_THE_LOOP)
def test_writes_stay_serialised_on_the_loop(name):
    """Not an oversight: serialisation via the loop is what keeps concurrent edits to
    one file from interleaving. Moving writes into threads needs its own locking."""
    assert not inspect.iscoroutinefunction(getattr(server, name))


def test_offloading_preserves_the_tool_signature():
    """functools.wraps must keep the schema intact, or clients lose the arguments."""
    signature = inspect.signature(server.vault_read)

    assert list(signature.parameters) == ["path"]
    assert server.vault_read.__name__ == "vault_read"


def test_context_survives_the_hop_into_the_worker_thread(vault_dir):
    """The rate limiter and request logging read contextvars. If those did not
    propagate, rate limiting would silently stop applying per token."""
    seen = {}

    def fake_read(path):
        seen["principal"] = current_auth_principal()
        return "{}"

    original = server._vault_read
    server._vault_read = fake_read
    token = set_current_auth_principal("principal-in-the-loop")
    try:
        asyncio.run(server.vault_read("test-note.md"))
    finally:
        reset_current_auth_principal(token)
        server._vault_read = original

    assert seen["principal"] == "principal-in-the-loop"


def test_the_event_loop_keeps_running_during_a_slow_read(vault_dir):
    """The point of the whole change, stated as behaviour rather than as a type check.

    A tool that blocks for 0.4s must not stop other coroutines from being scheduled --
    /health is one of those. Before the offload the ticker below would not advance at
    all while the read was in flight.
    """
    reset_rate_limits()
    block_seconds = 0.4

    reached = []

    def slow_read(path):
        reached.append(path)
        time.sleep(block_seconds)
        return "{}"

    async def scenario():
        ticks = 0
        read = asyncio.create_task(server.vault_read("test-note.md"))
        deadline = time.perf_counter() + block_seconds
        while not read.done() and time.perf_counter() < deadline:
            await asyncio.sleep(0.01)
            ticks += 1
        await read
        return ticks

    # Without a principal the tool returns a rate-limit error before reaching the read,
    # and the test would measure nothing.
    original = server._vault_read
    server._vault_read = slow_read
    token = set_current_auth_principal("loop-test")
    try:
        ticks = asyncio.run(scenario())
    finally:
        reset_current_auth_principal(token)
        server._vault_read = original

    # Guard against passing for the wrong reason: an early return (a rate-limit error,
    # say) would end the wait immediately and leave the tick count meaningless.
    assert reached, "the slow read was never reached; the measurement below means nothing"
    # ~40 ticks are possible in 0.4s; anything above a handful proves the loop ran.
    assert ticks >= 5, f"event loop only advanced {ticks} times during a blocking read"
