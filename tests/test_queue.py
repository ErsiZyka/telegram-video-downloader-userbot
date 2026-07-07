import json
import os
import tempfile
import pytest
from bot.queue import DownloadQueue


@pytest.fixture
def tmp_queue():
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "queue.json")
    yield DownloadQueue(filepath=fp), fp
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def test_empty_queue_is_empty(tmp_queue):
    q, _ = tmp_queue
    assert q.is_empty() is True
    assert q.peek() is None
    assert q.items == []


def test_add_returns_position(tmp_queue):
    q, _ = tmp_queue
    pos1 = q.add("https://a", "720", "A")
    assert pos1 == 1
    pos2 = q.add("https://b", "max", "B")
    assert pos2 == 2
    assert q.is_empty() is False
    assert len(q.items) == 2


def test_peek_does_not_remove(tmp_queue):
    q, _ = tmp_queue
    q.add("https://a", "720", "A")
    q.add("https://b", "max", "B")
    first = q.peek()
    assert first["url"] == "https://a"
    assert len(q.items) == 2  # NOT removed
    assert q.peek()["url"] == "https://a"  # still there


def test_remove_by_url(tmp_queue):
    q, _ = tmp_queue
    q.add("https://a", "720", "A")
    q.add("https://b", "max", "B")
    assert q.remove("https://a") is True
    assert q.peek()["url"] == "https://b"
    assert q.remove("https://nonexistent") is False


def test_move_front(tmp_queue):
    q, _ = tmp_queue
    q.add("https://a", "720", "A")
    q.add("https://b", "max", "B")
    q.add("https://c", "360", "C")
    assert q.move_front("https://c") is True
    assert q.items[0]["url"] == "https://c"
    assert q.items[1]["url"] == "https://a"
    assert q.move_front("https://nonexistent") is False
    # move_front on already-first is no-op but True
    assert q.move_front("https://c") is True
    assert q.items[0]["url"] == "https://c"


def test_clear(tmp_queue):
    q, _ = tmp_queue
    q.add("https://a", "720", "A")
    q.add("https://b", "max", "B")
    n = q.clear()
    assert n == 2
    assert q.is_empty() is True


def test_persistence(tmp_queue):
    q, fp = tmp_queue
    q.add("https://a", "720", "A")
    q.add("https://b", "max", "B")
    q.move_front("https://b")
    # reload from same file
    q2 = DownloadQueue(filepath=fp)
    assert [i["url"] for i in q2.items] == ["https://b", "https://a"]


def test_corrupt_file_treated_as_empty(tmp_queue):
    q, fp = tmp_queue
    with open(fp, "w") as f:
        f.write("NOT JSON{{{")
    q2 = DownloadQueue(filepath=fp)
    assert q2.is_empty() is True


def test_non_list_json_treated_as_empty(tmp_queue):
    q, fp = tmp_queue
    with open(fp, "w") as f:
        json.dump({"not": "a list"}, f)
    q2 = DownloadQueue(filepath=fp)
    assert q2.is_empty() is True
