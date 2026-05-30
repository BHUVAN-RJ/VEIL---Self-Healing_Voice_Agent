"""Tests for Hinglish spacing fixes."""

from text_spacing import append_stream_chunk, fix_hinglish_spacing, space_before_chunk


def test_space_after_comma():
    assert append_stream_chunk("Achha,", "kitna") == "Achha, kitna"


def test_subword_tokens_are_not_shattered():
    # Real LLM streams emit sub-word tokens; leading spaces mark word starts.
    chunks = [
        "Ach", "ha", " Pri", "ya", " ji", ",", " bata", "o",
        " k", "ya", " ba", "at", " hai", "?",
    ]
    buf = ""
    for chunk in chunks:
        buf = append_stream_chunk(buf, chunk)
    assert buf == "Achha Priya ji, batao kya baat hai?"


def test_does_not_double_space_normal_text():
    normal = "Achha Priya ji, batao kya baat hai?"
    assert fix_hinglish_spacing(normal) == normal


def test_fix_glued_string_only():
    glued = "Theekhaisamajhgayi,dhanyavaad"
    out = fix_hinglish_spacing(glued)
    assert " " in out
    assert out != glued


def test_no_extra_spaces_on_already_spaced():
    text = "Haan ji, sahi hai, wahi branch hai."
    assert fix_hinglish_spacing(text) == text


def test_space_before_chunk():
    assert space_before_chunk(",", "kitna") == " kitna"
    assert space_before_chunk(" ", "kitna") == "kitna"
