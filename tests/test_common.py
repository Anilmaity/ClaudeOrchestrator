import re
import common

def test_name_re_accepts_valid_names():
    assert common.NAME_RE.match("web-1")
    assert common.NAME_RE.match("Agent_2.test")

def test_name_re_rejects_invalid_names():
    assert not common.NAME_RE.match("-leading")
    assert not common.NAME_RE.match("has space")
    assert not common.NAME_RE.match("bad/slash")

def test_now_is_utc_iso_z():
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", common._now())

def test_markers_present():
    assert common.DONE_MARKER == "worker-done"
    assert any("interrupt" in m for m in common.BUSY_MARKERS)
    assert any("shortcuts" in m or "shift+tab" in m for m in common.READY_MARKERS)
