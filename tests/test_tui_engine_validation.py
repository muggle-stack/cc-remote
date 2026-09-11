"""Invalid ENGINE configuration fails before connecting or opening the UI."""

import pytest

from cc_remote import tui, tui_app


@pytest.mark.parametrize("engine", ["CODEX", "foo", "codex "])
@pytest.mark.parametrize("line", [False, True])
def test_invalid_engine_fails_before_client_start(monkeypatch, capsys, engine, line):
    monkeypatch.setenv("ENGINE", engine)
    monkeypatch.setattr(tui.sys, "argv", ["tui", *(["--line-mode"] if line else [])])
    with pytest.raises(SystemExit) as error:
        tui.main()
    assert error.value.code == 2
    assert "ENGINE must be" in capsys.readouterr().err


def test_explicit_engine_overrides_invalid_environment(monkeypatch):
    monkeypatch.setenv("ENGINE", "typo")
    monkeypatch.setattr(tui.sys, "argv", ["tui", "--demo", "--engine", "codex"])
    clients = []
    monkeypatch.setattr(tui_app, "run_workspace", lambda c, **kw: clients.append(c))
    tui.main()
    assert clients[0].engine == "codex"
