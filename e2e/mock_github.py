import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
requests = []
configuration = {}


def issue(number, repository="demo/project"):
    repo_id = {"demo/project": 100, "demo/other": 200, "demo/private": 300}[repository]
    result = {
        "id": repo_id * 1000 + number,
        "number": number,
        "title": "Вторая страница" if number == 22 else f"Задача {number}",
        "body": None
        if number == 23
        else f"## Полное описание #{number}\n\n**Markdown** и ссылка #1 не задают зависимость.\n\n"
        + "Содержимое задачи.\n" * 30,
        "state": "closed" if number == 23 else "open",
        "state_reason": "completed" if number == 23 else None,
        "updated_at": "2026-09-19T12:00:00Z",
        "labels": [{"name": "feature", "color": "7465a5"}],
        "repository_url": f"https://api.github.com/repos/{repository}",
    }
    if number == 13:
        result["pull_request"] = {
            "url": "https://api.github.com/repos/demo/project/pulls/13"
        }
    return result


@app.get("/audit")
async def audit():
    return {"requests": requests}


@app.post("/fixture")
async def fixture(request: Request):
    configuration.clear()
    configuration.update(await request.json())
    requests.clear()
    return {"ok": True}


@app.api_route(
    "/repos/{owner}/{repo}{suffix:path}", methods=["GET", "POST", "PATCH", "DELETE"]
)
async def github(request: Request, owner: str, repo: str, suffix: str):
    name = f"{owner}/{repo}"
    authorized = request.headers.get("authorization") == "Bearer github-e2e-token"
    requests.append(
        {
            "method": request.method,
            "path": request.url.path,
            "page": request.query_params.get("page"),
            "authenticated": authorized,
        }
    )
    if request.method != "GET":
        return JSONResponse({"message": "writes forbidden"}, status_code=405)
    if configuration.get("rate_remaining", 0):
        configuration["rate_remaining"] -= 1
        return JSONResponse(
            {"message": "rate limit"}, status_code=429, headers={"Retry-After": "1"}
        )
    if configuration.get("failure"):
        return JSONResponse(
            {"message": "github-e2e-token"}, status_code=configuration["failure"]
        )
    if name not in {"demo/project", "demo/other", "demo/private"} or (
        name == "demo/private" and not authorized
    ):
        return JSONResponse({"message": "not found"}, status_code=404)
    if not suffix:
        return {
            "id": {"demo/project": 100, "demo/other": 200, "demo/private": 300}[name],
            "full_name": name,
            "private": name == "demo/private",
        }
    page = request.query_params.get("page", "1")
    next_link = {"Link": f'<{str(request.url).split("?")[0]}?page=2>; rel="next"'}
    if suffix == "/issues":
        return JSONResponse(
            [
                issue(number, name)
                for number in (range(1, 21) if page == "1" else range(21, 24))
            ],
            headers=next_link if page == "1" else {},
        )
    if suffix.endswith("/dependencies/blocked_by"):
        number = int(suffix.split("/")[2])
        if configuration.get("dependencies_failure"):
            return JSONResponse({"message": "no permission"}, status_code=403)
        if number == 22:
            return JSONResponse(
                [issue(1)] if page == "1" else [issue(5, "demo/other")],
                headers=next_link if page == "1" else {},
            )
        return []
    if suffix.startswith("/issues/"):
        number = int(suffix.split("/")[2])
        if number == configuration.get("missing_issue"):
            return JSONResponse({"message": "missing issue"}, status_code=404)
        return issue(number, name)
    return JSONResponse({}, status_code=404)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["AISPACE_GITHUB_MOCK_PORT"]))
