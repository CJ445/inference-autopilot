from term_screen import VScreen


def test_text_is_placed_at_cursor_positions():
    s = VScreen(3, 20)
    s.feed(b"\x1b[1;1HHello\x1b[2;5HWorld")
    assert s.lines() == ["Hello", "    World", ""]


def test_a_partial_update_overwrites_only_the_changed_cells():
    s = VScreen(2, 30)
    s.feed(b"\x1b[1;1HBudgets ? done")
    s.feed(b"\x1b[1;10H\xe2\x9c\x93")           # only the changed cell is rewritten
    assert s.lines()[0] == "Budgets \u2713 done" or s.lines()[0] == "Budgets ?\u2713done"
    assert "?" not in s.lines()[0].replace("Budgets ?", "") 


def test_styles_clears_and_osc_are_ignored_and_split_sequences_survive():
    s = VScreen(2, 20)
    s.feed(b"\x1b[2J\x1b[1;1H\x1b[38;5;2mOK\x1b[0m\x1b]0;title\x07")
    s.feed(b"\x1b[2;1")
    s.feed(b"HNext")
    assert s.lines() == ["OK", "Next"]
    s.feed(b"\x1b[2J")
    assert s.lines() == ["", ""]


def test_the_stream_can_hide_text_the_screen_shows():
    stream = b"\x1b[1;1HIdentity ?\x1b[1;10H\xe2\x9c\x93"   # '?' then a lone changed cell
    s = VScreen(1, 30)
    s.feed(stream)
    assert "Identity \u2713" in s.text()
    assert "Identity \u2713" not in stream.decode()
