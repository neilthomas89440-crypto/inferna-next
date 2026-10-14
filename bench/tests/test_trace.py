"""Trace recording and replay scheduling."""

from __future__ import annotations

from bench.trace import TraceWriter, read_trace, replay_schedule


def test_trace_roundtrip_preserves_order_and_bodies(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    with TraceWriter(path) as writer:
        writer.write("POST", "/v1/chat/completions", {"model": "m", "messages": []}, ts=100.0)
        writer.write("POST", "/v1/chat/completions", {"model": "m", "messages": []}, ts=101.5)

    entries = read_trace(path)

    assert [entry.ts for entry in entries] == [100.0, 101.5]
    assert all(entry.path == "/v1/chat/completions" for entry in entries)
    assert entries[0].body["model"] == "m"


def test_replay_schedule_preserves_inter_arrival_times(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    with TraceWriter(path) as writer:
        writer.write("POST", "/v1/chat/completions", {}, ts=10.0)
        writer.write("POST", "/v1/chat/completions", {}, ts=10.5)
        writer.write("POST", "/v1/chat/completions", {}, ts=13.0)

    delays = list(replay_schedule(read_trace(path)))

    assert delays[0] == 0.0
    assert delays[1:] == [0.5, 2.5]


def test_replay_schedule_ignores_backwards_timestamps(tmp_path) -> None:
    path = tmp_path / "trace.jsonl"
    with TraceWriter(path) as writer:
        writer.write("POST", "/v1/chat/completions", {}, ts=20.0)
        writer.write("POST", "/v1/chat/completions", {}, ts=19.0)  # clock skew

    assert list(replay_schedule(read_trace(path))) == [0.0, 0.0]
