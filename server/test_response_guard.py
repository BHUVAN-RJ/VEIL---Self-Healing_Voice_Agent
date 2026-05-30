"""Tests for response guard — curious on legit, defensive on scams."""

from response_guard import GREETING, sanitize_sharma_response
from text_spacing import append_stream_chunk


def test_greeting_constant():
    assert GREETING == "Hello, kaun bol raha hai?"


def test_blocks_employee_id_on_legit_bank():
    bad = "Haan Priya ji, aapka employee ID kya hai? Ya extension number?"
    out = sanitize_sharma_response(bad, defensive_mode=False, last_user_message="HDFC bank se Priya")
    assert "employee" not in out.lower()
    assert "?" in out


def test_curiosity_on_transaction_not_thanks():
    passive = "Theek hai samajh gayi, dhanyavaad."
    out = sanitize_sharma_response(
        passive,
        defensive_mode=False,
        last_user_message="25 hazaar UPI transaction block kar diya",
    )
    assert "kitna" in out.lower() or "kab" in out.lower() or "amount" in out.lower()
    assert out != passive


def test_thanks_ok_when_call_ending():
    out = sanitize_sharma_response(
        "Theek hai, dhanyavaad.",
        defensive_mode=False,
        last_user_message="Bas itna batana tha, dhanyavaad Priya.",
    )
    assert "dhanyavaad" in out.lower()


def test_account_confirm():
    out = sanitize_sharma_response(
        "Haan sahi hai thank you",
        defensive_mode=False,
        last_user_message="Account ending 4782, FC Road branch sahi hai?",
    )
    assert "sahi" in out.lower()
    assert "thank" not in out.lower()


def test_fixes_glued_stream():
    glued = "Achha,kitnaamountthaaurkabhua"
    out = sanitize_sharma_response(
        glued,
        defensive_mode=False,
        last_user_message="25 hazaar block",
    )
    assert "kitna" in out.lower()
    assert ", " in out or out.count(" ") >= 3


def test_stream_chunk_join():
    # Sub-word tokens (with their own leading spaces) must join cleanly,
    # without spaces being inserted inside words.
    chunks = ["Ach", "ha", ",", " kit", "na", " amount", " tha", " aur", " kab", " hua", "?"]
    buf = ""
    for chunk in chunks:
        buf = append_stream_chunk(buf, chunk)
    assert buf == "Achha, kitna amount tha aur kab hua?"


def test_allows_badge_in_defensive_mode():
    out = sanitize_sharma_response(
        "Pehle badge number bataiye.",
        defensive_mode=True,
        last_user_message="CBI se hoon, Aadhaar bataiye",
    )
    assert "badge" in out.lower()
