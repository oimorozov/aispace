import asyncio
import contextlib
import os
import platform
import shutil
from pathlib import Path

from fastapi import HTTPException

CHOOSE_FOLDER_SCRIPT = """on run argv
    try
        set startingFolder to POSIX file (item 1 of argv)
        set selectedFolder to choose folder with prompt "Выберите рабочую папку для aispace" default location startingFolder
        return POSIX path of selectedFolder
    on error number -128
        return ""
    end try
end run"""


class Directories:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("AISPACE_WORKSPACE_ROOT") or "/").resolve()
        self.picker_lock = asyncio.Lock()

    @staticmethod
    def capabilities():
        current_platform = platform.system().lower()
        return {
            "native_picker": current_platform == "darwin" and shutil.which("osascript") is not None,
            "platform": current_platform,
        }

    async def choose(self, value=None):
        if not self.capabilities()["native_picker"]:
            raise HTTPException(501, "Системный выбор папки доступен при запуске aispace на macOS")
        if self.picker_lock.locked():
            raise HTTPException(409, "Окно выбора папки уже открыто")
        home = Path.home().resolve()
        starting_path = self.resolve(
            value or str(home if home.is_relative_to(self.root) else self.root)
        )
        async with self.picker_lock:
            process = None
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    shutil.which("osascript"),
                    "-e",
                    CHOOSE_FOLDER_SCRIPT,
                    "--",
                    str(starting_path),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            )
            try:
                try:
                    process = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    while not spawn.done():
                        with contextlib.suppress(asyncio.CancelledError):
                            await asyncio.shield(spawn)
                    process = spawn.result()
                    raise
                output, _ = await process.communicate()
                if process.returncode:
                    raise HTTPException(503, "Не удалось открыть системное окно выбора папки")
                selected = output.decode("utf-8").removesuffix("\n")
                return {"path": str(self.resolve(selected)) if selected else None}
            except (OSError, UnicodeError) as error:
                raise HTTPException(
                    503, "Не удалось открыть системное окно выбора папки"
                ) from error
            finally:
                if process and process.returncode is None:
                    cleanup = asyncio.create_task(self.close_picker(process))
                    while not cleanup.done():
                        with contextlib.suppress(asyncio.CancelledError):
                            await asyncio.shield(cleanup)
                    cleanup.result()

    @staticmethod
    async def close_picker(process):
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    def resolve(self, value):
        if not value:
            raise HTTPException(422, "Выберите рабочую папку проекта в настройках или тасклете")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise HTTPException(422, "Укажите абсолютный путь к рабочей папке")
        try:
            path = candidate.resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise HTTPException(422, "Рабочая папка не найдена или недоступна backend") from error
        if not path.is_relative_to(self.root):
            raise HTTPException(
                422, "Папка находится за пределами настроенного корня файловой системы"
            )
        if not path.is_dir():
            raise HTTPException(422, "Рабочая папка не найдена или недоступна backend")
        return path

    def browse(self, value=None):
        path = self.resolve(value or str(self.root))
        try:
            entries = []
            for child in sorted(path.iterdir(), key=lambda item: item.name.casefold()):
                try:
                    if not child.is_dir():
                        continue
                    self.resolve(str(child))
                except (HTTPException, OSError, RuntimeError):
                    continue
                entries.append({"name": child.name, "path": str(child)})
        except OSError as error:
            raise HTTPException(422, "Нет доступа к содержимому папки") from error
        return {
            "path": str(path),
            "parent": str(path.parent) if path != self.root else None,
            "entries": entries,
            "roots": [str(self.root)],
        }
