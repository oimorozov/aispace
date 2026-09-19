import asyncio

import pytest
from fastapi import HTTPException

from aispace import directories as module
from aispace.directories import CHOOSE_FOLDER_SCRIPT, Directories


class PickerProcess:
    def __init__(self, output=b"\n", returncode=0, pending=False):
        self.output = output
        self.final_returncode = returncode
        self.returncode = None
        self.pending = pending
        self.started = asyncio.Event()
        self.terminated = False

    async def communicate(self):
        self.started.set()
        if self.pending:
            await asyncio.Event().wait()
        self.returncode = self.final_returncode
        return self.output, b"private diagnostic text"

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    async def wait(self):
        return self.returncode


@pytest.fixture
def picker(monkeypatch):
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.shutil, "which", lambda executable: "/usr/bin/osascript")
    calls = []
    process = PickerProcess()

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    return process, calls


async def test_choose_returns_canonical_selected_directory(tmp_path, picker):
    process, calls = picker
    destination = tmp_path / "project"
    destination.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(destination, target_is_directory=True)
    process.output = f"{alias}/\n".encode()
    directories = Directories(tmp_path)
    assert directories.capabilities() == {"native_picker": True, "platform": "darwin"}
    assert await directories.choose(str(tmp_path)) == {"path": str(destination.resolve())}
    assert calls[0][0] == (
        "/usr/bin/osascript",
        "-e",
        CHOOSE_FOLDER_SCRIPT,
        "--",
        str(tmp_path),
    )


async def test_choose_cancellation_returns_null(tmp_path, picker):
    assert await Directories(tmp_path).choose() == {"path": None}


@pytest.mark.parametrize("system,executable", [("Linux", "/usr/bin/osascript"), ("Darwin", None)])
async def test_choose_unavailable_without_native_macos_picker(
    tmp_path, monkeypatch, system, executable
):
    monkeypatch.setattr(module.platform, "system", lambda: system)
    monkeypatch.setattr(module.shutil, "which", lambda _: executable)
    directories = Directories(tmp_path)
    assert directories.capabilities()["native_picker"] is False
    with pytest.raises(HTTPException) as error:
        await directories.choose()
    assert error.value.status_code == 501


async def test_choose_paths_are_arguments_and_preserve_special_characters(tmp_path, picker):
    process, calls = picker
    special = tmp_path / "quote\" apostrophe' $(`id`)\nfolder "
    special.mkdir()
    process.output = (str(special) + "/\n").encode()
    assert await Directories(tmp_path).choose(str(special)) == {"path": str(special)}
    args, options = calls[0]
    assert args[-1] == str(special)
    assert args[-2] == "--"
    assert args[2] == CHOOSE_FOLDER_SCRIPT
    assert str(special) not in CHOOSE_FOLDER_SCRIPT
    assert "shell" not in options
    assert "on error number -128" in CHOOSE_FOLDER_SCRIPT


async def test_choose_process_error_does_not_expose_diagnostics(tmp_path, picker):
    process, _ = picker
    process.final_returncode = 1
    with pytest.raises(HTTPException) as error:
        await Directories(tmp_path).choose()
    assert error.value.status_code == 503
    assert "private diagnostic text" not in error.value.detail


async def test_choose_validates_selected_directory(tmp_path, picker):
    process, _ = picker
    process.output = f"{tmp_path}/missing/\n".encode()
    with pytest.raises(HTTPException) as error:
        await Directories(tmp_path).choose()
    assert error.value.status_code == 422


async def test_cancelled_request_closes_dialog_and_rejects_duplicate_picker(tmp_path, picker):
    process, calls = picker
    process.pending = True
    directories = Directories(tmp_path)
    task = asyncio.create_task(directories.choose())
    await process.started.wait()
    with pytest.raises(HTTPException) as error:
        await directories.choose()
    assert error.value.status_code == 409
    assert len(calls) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated
    assert not directories.picker_lock.locked()


def test_native_filesystem_ignores_obsolete_docker_host_mount(tmp_path, monkeypatch):
    alias = tmp_path / "disk-alias"
    alias.symlink_to("/", target_is_directory=True)
    monkeypatch.setenv("AISPACE_HOST_MOUNT", str(tmp_path))
    assert Directories("/").resolve(str(alias)) == module.Path("/")
