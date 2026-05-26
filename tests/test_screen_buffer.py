from screen_buffer import ScreenBuffer

def test_renders_plain_text():
    sb = ScreenBuffer(cols=20, rows=4)
    sb.feed(b"hello\r\nworld\r\n")
    text = sb.text()
    assert "hello" in text
    assert "world" in text

def test_text_lines_limit():
    sb = ScreenBuffer(cols=20, rows=6)
    sb.feed(b"a\r\nb\r\nc\r\nd\r\n")
    # last 2 non-empty-ish lines requested
    assert sb.text(lines=2).count("\n") <= 1

def test_overwrite_via_carriage_return():
    sb = ScreenBuffer(cols=20, rows=3)
    sb.feed(b"AAAAA\rBB")          # CR returns cursor; BB overwrites AA
    assert sb.text().splitlines()[0].startswith("BBAAA")

def test_detects_marker_substring():
    sb = ScreenBuffer(cols=40, rows=3)
    sb.feed(b"\r\n? for shortcuts\r\n")
    assert sb.contains_any(("for shortcuts",))
    assert not sb.contains_any(("nonexistent",))
