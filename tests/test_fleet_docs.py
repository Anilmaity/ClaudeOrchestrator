import pytest

import fleet


@pytest.fixture
def docs(tmp_path, monkeypatch):
    """Point DOCS_ROOT at a temp dir and define one known agent."""
    monkeypatch.setattr(fleet, "DOCS_ROOT", tmp_path / "fleet_docs")
    monkeypatch.setattr(
        fleet, "AGENTS",
        [{"name": "alice", "role": "", "project_dir": str(tmp_path)}],
    )
    return fleet


def test_safe_filename_strips_path(docs):
    assert docs._safe_filename("a/b/c.txt") == "c.txt"
    assert docs._safe_filename("..\\..\\evil.md") == "evil.md"
    assert docs._safe_filename("plain.pdf") == "plain.pdf"


@pytest.mark.parametrize("bad", ["", ".", "..", "   ", "a/..", "x/", "a\x00b"])
def test_safe_filename_rejects_traversal(docs, bad):
    with pytest.raises(ValueError):
        docs._safe_filename(bad)


def test_save_and_list_shared(docs):
    meta = docs.save_doc("shared", None, "notes.md", b"hello")
    assert meta["name"] == "notes.md"
    assert meta["size"] == 5
    files = docs.list_docs("shared")
    assert [f["name"] for f in files] == ["notes.md"]
    assert files[0]["path"].endswith("notes.md")
    assert "modified" in files[0]


def test_save_and_list_agent(docs):
    docs.save_doc("agent", "alice", "spec.txt", b"abc")
    files = docs.list_docs("agent", "alice")
    assert [f["name"] for f in files] == ["spec.txt"]


def test_unknown_agent_rejected(docs):
    with pytest.raises(ValueError):
        docs.save_doc("agent", "bob", "x.txt", b"x")


def test_bad_scope_rejected(docs):
    with pytest.raises(ValueError):
        docs.list_docs("nope")


def test_too_large_rejected(docs, monkeypatch):
    monkeypatch.setattr(fleet, "MAX_DOC_BYTES", 4)
    with pytest.raises(ValueError):
        docs.save_doc("shared", None, "big.bin", b"12345")


def test_delete_roundtrip(docs):
    docs.save_doc("shared", None, "gone.txt", b"x")
    assert docs.delete_doc("shared", None, "gone.txt") is True
    assert docs.delete_doc("shared", None, "gone.txt") is False
    assert docs.list_docs("shared") == []
