import asyncio
import json
from pathlib import Path

import panel


def _registry(tmp_path):
    (tmp_path / "scripts.json").write_text(json.dumps({"scripts": [{
        "id": "telegram-voice",
        "name": "TelegramVoice",
        "description": "Crea note vocali Telegram",
        "icon": "🎙️",
        "service": "telegramvoice",
        "path": "/home/ersi/TelegramVoice",
    }]}), encoding="utf-8")


def test_managed_scripts_reads_registry_and_reports_service_state(tmp_path, monkeypatch):
    _registry(tmp_path)
    monkeypatch.setattr(panel, "SCRIPTS_DIR", Path(tmp_path), raising=False)
    monkeypatch.setattr(panel, "_run", lambda *_args, **_kwargs: {"out": "active\n", "err": "", "code": 0})

    assert panel._managed_scripts() == [{
        "id": "telegram-voice",
        "name": "TelegramVoice",
        "description": "Crea note vocali Telegram",
        "icon": "🎙️",
        "service": "telegramvoice",
        "path": "/home/ersi/TelegramVoice",
        "active": True,
    }]


def test_script_action_starts_a_registered_service(tmp_path, monkeypatch):
    _registry(tmp_path)
    monkeypatch.setattr(panel, "SCRIPTS_DIR", Path(tmp_path), raising=False)
    calls = []
    monkeypatch.setattr(panel, "_run", lambda cmd, **_kwargs: calls.append(cmd) or {"out": "", "err": "", "code": 0})

    class FakeRequest:
        async def json(self):
            return {"id": "telegram-voice", "action": "start"}

    response = asyncio.run(panel.script_action(FakeRequest()))

    assert response == {"out": "", "err": "", "code": 0}
    assert calls == [["sudo", "systemctl", "start", "telegramvoice"]]


def test_managed_scripts_lists_new_project_directories_automatically(tmp_path, monkeypatch):
    (tmp_path / "voice-lab").mkdir()
    monkeypatch.setattr(panel, "SCRIPTS_DIR", Path(tmp_path), raising=False)

    assert panel._managed_scripts() == [{
        "id": "voice-lab",
        "name": "Voice Lab",
        "description": "Da configurare",
        "icon": "📁",
        "service": "",
        "path": str(tmp_path / "voice-lab"),
        "active": False,
    }]
