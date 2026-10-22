from datetime import datetime, timezone

from app.prompts import CallContext, build_system_prompt
from app.transcript import TranscriptAssembler


def test_basic_turns_in_order():
    a = TranscriptAssembler()
    a.assistant_text("Hi, this is Yogesh's AI assistant.", 100)
    a.turn_complete(900)
    a.caller_text("Hello ", 1500, onset_ms=1200)
    a.caller_text("is he free?", 1800)
    a.assistant_text("He's busy right now.", 2500)
    a.turn_complete(3000)
    segs = a.ordered()
    assert [s.speaker for s in segs] == ["assistant", "caller", "assistant"]
    assert segs[1].text == "Hello is he free?"
    assert segs[1].start_ms == 1200


def test_late_caller_transcription_is_ordered_by_speech_onset():
    """Input transcription often arrives after the model has started answering."""
    a = TranscriptAssembler()
    a.assistant_text("Sure.", 2000)          # model reply text arrives first...
    a.caller_text("Can you call back?", 2100, onset_ms=1000, finished=True)  # ...but caller spoke at 1.0 s
    a.turn_complete(2500)
    assert [s.speaker for s in a.ordered()] == ["caller", "assistant"]


def test_interruption_marks_segment_and_starts_new_one():
    a = TranscriptAssembler()
    a.assistant_text("Let me explain the whole", 100)
    a.caller_text("Wait wait", 600, onset_ms=500)
    a.interrupted(650)
    a.assistant_text("Sure, go ahead.", 1500)
    a.turn_complete(1800)
    segs = a.ordered()
    assert segs[0].interrupted and segs[0].speaker == "assistant"
    assert [s.speaker for s in segs] == ["assistant", "caller", "assistant"]
    assert not segs[2].interrupted


def test_updates_emitted_and_empty_segments_dropped():
    events = []
    a = TranscriptAssembler(on_update=lambda s: events.append((s.seq, s.final)))
    a.caller_text("", 10, finished=True)  # nothing said
    a.caller_text("namaste", 20)
    a.close_all(30)
    assert events == [(0, False), (0, True)]
    assert len(a.ordered()) == 1
    assert a.as_text() == "Caller: namaste"


def test_whitespace_normalized():
    a = TranscriptAssembler()
    a.caller_text("haan  ", 1)
    a.caller_text("  theek hai", 2, finished=True)
    assert a.ordered()[0].text == "haan theek hai"


def test_prompt_contains_mandatory_rules_and_context():
    p = build_system_prompt(
        owner_name="Yogesh", languages="English, Hindi", timezone="Asia/Kolkata", style_rules="Be chill.",
        facts=[{"fact": "Prefers WhatsApp", "contact_name": ""}],
        recent=[{"started_at": datetime(2026, 10, 1, tzinfo=timezone.utc), "direction": "incoming",
                 "title": "Rent", "summary": "Discussed rent due date."}],
        ctx=CallContext("outgoing", "Ravi", "Confirm Saturday meeting", True),
    )
    assert "Yogesh's AI assistant" in p and "recorded" in p
    assert "Never claim or imply you are human" in p
    assert "Confirm Saturday meeting" in p and "Prefers WhatsApp" in p and "Discussed rent" in p
    assert "Be chill." in p
    p2 = build_system_prompt(owner_name="Y", languages="en", timezone="Asia/Kolkata", style_rules="", facts=[],
                             recent=[], ctx=CallContext("incoming", "", "", False))
    assert "recorded" not in p2.split("== HOW TO SPEAK")[0].split("HONESTY")[0]
