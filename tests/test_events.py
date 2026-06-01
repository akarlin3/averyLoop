"""Tests for averyloop.events — the structured progress event stream.

Asserts the emitter produces the expected ordered events for a stubbed run and
that the non-TTY path stays plain text (no ANSI escapes), so CI / piped runs are
unaffected by the live dashboard.
"""

from __future__ import annotations

import io

from averyloop import events, multi


def test_emit_uses_active_emitter_and_records_order():
    sink = events.RecordingSink()
    with events.Emitter(sink):
        events.emit("run_start", project="p", mode="DRY RUN", max_iterations=2)
        events.emit("iteration_start", iteration=1, max_iterations=2)
        events.emit("phase", iteration=1, n=1, name="audit")
        events.emit("findings", iteration=1, count=3)
        events.emit("iteration_end", iteration=1, overall=8.0, composite=7.5,
                    tests_passed=True, exit=False)
        events.emit("convergence", iteration=1, stop=True, reason="plateau")
        events.emit("run_end", iterations=1)

    assert sink.kinds == [
        "run_start", "iteration_start", "phase", "findings",
        "iteration_end", "convergence", "run_end",
    ]
    # Event payloads are preserved.
    assert sink.events[3].data["count"] == 3
    assert sink.events[4].data["composite"] == 7.5


def test_emit_outside_context_does_not_raise():
    # No active emitter → falls back to the default plain sink, never raises.
    assert events.get_active() is None
    events.emit("message", text="hello")


def test_make_emitter_non_tty_is_plain():
    buf = io.StringIO()  # StringIO.isatty() is False
    em = events.make_emitter(buf)
    assert isinstance(em.sink, events.PlainRenderer)


def test_make_emitter_force_plain():
    em = events.make_emitter(force_plain=True)
    assert isinstance(em.sink, events.PlainRenderer)


def test_plain_renderer_writes_plain_text_no_ansi():
    buf = io.StringIO()
    with events.make_emitter(buf, force_plain=True):
        events.emit("run_start", project="proj", mode="LIVE", max_iterations=3)
        events.emit("iteration_start", iteration=1, max_iterations=3)
        events.emit("phase", iteration=1, n=2, name="audit")
        events.emit("findings", iteration=1, count=2)
        events.emit("iteration_end", iteration=1, overall=9.0, composite=8.1,
                    tests_passed=True, exit=False)
    out = buf.getvalue()
    assert "Improvement Loop [proj]" in out
    assert "ITERATION 1/3" in out
    assert "[2/5] audit" in out
    assert "Found 2 valid finding(s)" in out
    assert "overall=9.0" in out
    # No ANSI escape sequences in the plain path.
    assert "\x1b[" not in out


def test_ensure_emitter_reuses_active():
    sink = events.RecordingSink()
    with events.Emitter(sink) as outer:
        with events.ensure_emitter() as inner:
            assert inner is outer  # nested call reuses the active emitter


def test_ensure_emitter_creates_when_none():
    assert events.get_active() is None
    buf = io.StringIO()
    with events.ensure_emitter(buf, force_plain=True) as em:
        assert events.get_active() is em
    assert events.get_active() is None  # torn down on exit


def test_multi_sweep_emits_queue_events():
    """A stubbed sweep emits sweep/repo lifecycle events in order."""
    sink = events.RecordingSink()

    def _noop_loop(max_iterations, dry_run):
        return [{"branches_merged": [], "exit_condition_met": True,
                 "audit_scores": {"overall": 9.0}}]

    import tempfile
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        specs = [multi.RepoSpec(path=a, name="a"),
                 multi.RepoSpec(path=b, name="b")]
        with events.Emitter(sink):
            multi.run_repos(specs, max_iterations=1, dry_run=True,
                            run_fn=_noop_loop)

    kinds = sink.kinds
    assert kinds[0] == "sweep_start"
    assert kinds.count("repo_start") == 2
    assert kinds.count("repo_end") == 2
    # Each repo_start precedes its repo_end.
    assert kinds.index("repo_start") < kinds.index("repo_end")
