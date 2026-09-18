import os
from pathlib import Path

from fastapi import HTTPException


class Directories:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("AISPACE_WORKSPACE_ROOT") or "/").resolve()
        self.host_root = os.environ.get("AISPACE_HOST_MOUNT")

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
        if (
            self.host_root
            and candidate.is_relative_to(self.host_root)
            and not path.is_relative_to(self.host_root)
        ):
            raise HTTPException(
                422, "Ссылка ведёт за пределы папок компьютера. Выберите целевую папку напрямую."
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
