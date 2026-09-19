import asyncio
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import HTTPException

from .storage import new_id, now


def repository_name(value):
    value = value.strip().rstrip("/")
    if "://" in value:
        try:
            parts = urlsplit(value)
        except ValueError as error:
            raise HTTPException(
                422, "Укажите owner/repo или https://github.com/owner/repo"
            ) from error
        if (
            parts.scheme != "https"
            or parts.netloc.lower() != "github.com"
            or parts.query
            or parts.fragment
        ):
            raise HTTPException(422, "Укажите owner/repo или https://github.com/owner/repo")
        value = parts.path.removeprefix("/")
    value = value.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value):
        raise HTTPException(422, "Укажите owner/repo или https://github.com/owner/repo")
    owner, repo = value.split("/")
    if owner.endswith("-") or repo in {".", ".."}:
        raise HTTPException(422, "Недопустимое имя репозитория GitHub")
    return value


class GitHubError(HTTPException):
    def __init__(self, status_code, detail, retry_after=None):
        super().__init__(
            status_code,
            detail,
            headers={"Retry-After": str(retry_after)} if retry_after is not None else None,
        )
        self.retry_after = retry_after


class GitHubClient:
    def __init__(self, store, transport=None, base_url="https://api.github.com"):
        self.store = store
        self.client = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(15),
            follow_redirects=False,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"},
        )
        self.retry_at = 0

    async def close(self):
        await self.client.aclose()

    async def _get(self, path, params=None):
        wait = max(0, int(self.retry_at - time.time() + 0.999))
        if wait:
            raise GitHubError(429, f"Лимит GitHub. Повторите запрос через {wait} сек.", wait)
        token = self.store.github_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = await self.client.get(path, params=params, headers=headers)
        except httpx.TimeoutException as error:
            raise GitHubError(504, "GitHub не ответил вовремя. Повторите загрузку.") from error
        except httpx.RequestError as error:
            raise GitHubError(502, "Не удалось связаться с GitHub. Повторите загрузку.") from error
        if response.status_code == 401:
            raise GitHubError(401, "GitHub отклонил токен. Замените его в настройках GitHub.")
        limited = response.status_code == 429 or (
            response.status_code == 403
            and (
                response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in response.headers
                or "rate limit" in response.text.lower()
            )
        )
        if limited:
            try:
                wait = max(1, int(response.headers.get("retry-after", "0")))
                if response.headers.get("x-ratelimit-remaining") == "0":
                    wait = max(
                        wait, int(response.headers.get("x-ratelimit-reset", "0")) - int(time.time())
                    )
            except ValueError:
                wait = 60
            if wait == 1 and "retry-after" not in response.headers:
                wait = 60
            self.retry_at = time.time() + wait
            raise GitHubError(429, f"Лимит GitHub. Повторите запрос через {wait} сек.", wait)
        if response.status_code == 403:
            raise GitHubError(
                403, "Нет доступа GitHub. Проверьте разрешение Issues: read для этого репозитория."
            )
        if response.status_code == 404:
            raise GitHubError(
                404, "Репозиторий или issue не найдены либо недоступны этому подключению GitHub."
            )
        if response.status_code in {301, 302, 307, 308}:
            raise GitHubError(422, "Репозиторий перемещён. Укажите его текущий адрес на GitHub.")
        if not response.is_success:
            raise GitHubError(502, "GitHub не смог выполнить запрос. Повторите загрузку.")
        try:
            return response.json(), response
        except ValueError as error:
            raise GitHubError(502, "GitHub вернул некорректный ответ.") from error

    def _next_page(self, response, path, page):
        link = response.links.get("next", {}).get("url")
        if not link:
            return None
        url = urlsplit(link)
        base = urlsplit(str(self.client.base_url))
        values = parse_qs(url.query)
        try:
            next_page = int(values.get("page", [""])[0])
        except ValueError as error:
            raise GitHubError(502, "Не удалось прочитать следующую страницу GitHub.") from error
        if (
            (url.scheme, url.netloc) != (base.scheme, base.netloc)
            or url.path != path
            or next_page <= page
            or next_page > 1000
        ):
            raise GitHubError(
                502, "GitHub вернул неподдерживаемую пагинацию. Набор не загружен полностью."
            )
        return next_page

    async def _pages(self, path, params=None):
        page = 1
        while page:
            data, response = await self._get(
                path, {**(params or {}), "per_page": 100, "page": page}
            )
            if not isinstance(data, list):
                raise GitHubError(502, "GitHub вернул некорректный список issues.")
            yield data
            page = self._next_page(response, path, page)

    async def repository(self, value):
        name = repository_name(value)
        data, _ = await self._get(f"/repos/{name}")
        try:
            canonical = repository_name(data["full_name"])
            repository_id = int(data["id"])
            if repository_id <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise GitHubError(502, "GitHub вернул некорректный репозиторий.") from error
        return {
            "id": repository_id,
            "full_name": canonical,
            "url": f"https://github.com/{canonical}",
            "private": bool(data.get("private")),
        }

    @staticmethod
    def issue(data, repository):
        try:
            number = int(data["number"])
            issue_id = int(data["id"])
            if number <= 0 or issue_id <= 0 or data["state"] not in {"open", "closed"}:
                raise ValueError
            return {
                "id": issue_id,
                "number": number,
                "url": f"https://github.com/{repository}/issues/{number}",
                "title": data["title"],
                "body": data.get("body") or "",
                "state": data["state"],
                "state_reason": data.get("state_reason"),
                "labels": [
                    {"name": item["name"], "color": item.get("color", "")}
                    if isinstance(item, dict)
                    else {"name": item, "color": ""}
                    for item in data.get("labels", [])
                ],
                "updated_at": data["updated_at"],
            }
        except (KeyError, TypeError, ValueError) as error:
            raise GitHubError(502, "GitHub вернул некорректную issue.") from error

    async def list_issues(self, value):
        repository = await self.repository(value)
        issues = {}
        async for page in self._pages(
            f"/repos/{repository['full_name']}/issues",
            {"state": "all", "sort": "created", "direction": "asc"},
        ):
            for item in page:
                if "pull_request" not in item:
                    issue = self.issue(item, repository["full_name"])
                    issues[issue["id"]] = issue
        return {"repository": repository, "issues": list(issues.values())}

    async def dependencies(self, repository, number):
        dependencies = {}
        try:
            async for page in self._pages(
                f"/repos/{repository}/issues/{number}/dependencies/blocked_by"
            ):
                for item in page:
                    parts = urlsplit(item.get("repository_url", ""))
                    if (
                        parts.scheme != "https"
                        or parts.netloc != "api.github.com"
                        or not parts.path.startswith("/repos/")
                    ):
                        raise GitHubError(502, "GitHub вернул некорректную зависимость.")
                    name = repository_name(parts.path.removeprefix("/repos/"))
                    issue = self.issue(item, name)
                    if "pull_request" in item:
                        raise GitHubError(
                            502, "GitHub вернул pull request вместо зависимости issue."
                        )
                    dependencies[issue["id"]] = {
                        key: issue[key] for key in ("id", "number", "url", "title", "state")
                    }
                    dependencies[issue["id"]]["repository"] = name
        except HTTPException as error:
            return {
                "status": "unavailable",
                "blocked_by": list(dependencies.values()),
                "error": error.detail,
            }
        return {"status": "complete", "blocked_by": list(dependencies.values()), "error": None}

    async def selection(self, value, repository_id, selected, *, persist=True):
        repository = await self.repository(value)
        if repository["id"] != repository_id:
            raise GitHubError(409, "Репозиторий изменился. Загрузите его issues заново.")
        issues = []
        seen = set()
        for item in selected:
            if item.id in seen:
                continue
            data, _ = await self._get(f"/repos/{repository['full_name']}/issues/{item.number}")
            if (
                "pull_request" in data
                or data.get("id") != item.id
                or data.get("number") != item.number
            ):
                raise GitHubError(
                    422, "Выбор содержит чужую issue или pull request. Загрузите список заново."
                )
            expected = f"https://api.github.com/repos/{repository['full_name']}"
            if data.get("repository_url", "").lower() != expected.lower():
                raise GitHubError(422, "Выбранная issue не принадлежит этому репозиторию.")
            issue = self.issue(data, repository["full_name"])
            issue["dependencies"] = await self.dependencies(repository["full_name"], item.number)
            issues.append(issue)
            seen.add(item.id)
        snapshot = {"id": new_id(), "repository": repository, "issues": issues, "created_at": now()}
        if persist:
            self.store.save_github_selection(snapshot)
        return snapshot

    async def bounded(self, operation):
        try:
            async with asyncio.timeout(120):
                return await operation
        except TimeoutError as error:
            raise GitHubError(
                504,
                "Загрузка GitHub заняла слишком много времени. Выбор сохранён в форме; повторите запрос.",
            ) from error
