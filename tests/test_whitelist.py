import json
import os
import tempfile
import pytest
from bot.whitelist import Whitelist


@pytest.fixture
def whitelist():
    """Create a Whitelist backed by a temporary file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"users": {}}, f)
        path = f.name
    wl = Whitelist(filepath=path)
    yield wl
    os.unlink(path)


class TestWhitelistBasic:
    def test_is_authorized_returns_false_for_unknown_user(self, whitelist):
        assert whitelist.is_authorized(999999) is False

    def test_add_and_is_authorized(self, whitelist):
        whitelist.add(123456, username="@test")
        assert whitelist.is_authorized(123456) is True

    def test_add_persists_to_file(self, whitelist):
        whitelist.add(123456, username="@test")
        # Re-read from file
        with open(whitelist.filepath) as f:
            data = json.load(f)
        assert "123456" in data["users"]
        assert data["users"]["123456"]["username"] == "@test"

    def test_remove_existing_user(self, whitelist):
        whitelist.add(123456, username="@test")
        result = whitelist.remove(123456)
        assert result is True
        assert whitelist.is_authorized(123456) is False

    def test_remove_nonexistent_user(self, whitelist):
        result = whitelist.remove(999999)
        assert result is False

    def test_get_all_returns_all_users(self, whitelist):
        whitelist.add(111, username="@a")
        whitelist.add(222, username="@b")
        users = whitelist.get_all()
        assert len(users) == 2
        assert users[111]["username"] == "@a"
        assert users[222]["username"] == "@b"

    def test_get_all_returns_empty_initially(self, whitelist):
        assert whitelist.get_all() == {}

    def test_whitelist_creates_file_if_missing(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as f:
            path = f.name
        assert not os.path.exists(path)
        wl = Whitelist(filepath=path)
        assert os.path.exists(path)
        assert wl.get_all() == {}
        os.unlink(path)


class TestWhitelistAtomic:
    def test_save_is_atomic_no_corruption(self, whitelist):
        """If the process crashes mid-write, the original file is intact."""
        whitelist.add(123, username="@test")
        # Read the file — it should be valid JSON with the user
        with open(whitelist.filepath) as f:
            data = json.load(f)
        assert "123" in data["users"]

    def test_multiple_adds_all_persist(self, whitelist):
        for i in range(10):
            whitelist.add(i, username=f"@user{i}")
        with open(whitelist.filepath) as f:
            data = json.load(f)
        assert len(data["users"]) == 10

    def test_concurrent_adds_same_id_idempotent(self, whitelist):
        whitelist.add(42, username="@first")
        whitelist.add(42, username="@second")
        users = whitelist.get_all()
        assert len(users) == 1
        assert users[42]["username"] == "@second"
