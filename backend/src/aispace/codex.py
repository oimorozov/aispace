import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
from pathlib import Path

from .provider import ProviderError


class CodexRPCError(ProviderError):
    def __init__(self, method, code):
        super().__init__(f"Codex не выполнил {method} (код {code})")
        self.method = method
        self.code = code


def codex_command():
    override = os.environ.get("AISPACE_CODEX_COMMAND_JSON")
    if override:
        try:
            command = json.loads(override)
        except ValueError as error:
            raise ProviderError("Некорректный AISPACE_CODEX_COMMAND_JSON") from error
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise ProviderError("AISPACE_CODEX_COMMAND_JSON должен содержать массив аргументов")
        return command
    executable = shutil.which(os.environ.get("AISPACE_CODEX_BIN", "codex"))
    if not executable:
        raise ProviderError("Codex CLI не установлен. Установите Codex на сервере aispace.")
    return [executable, "app-server", "--stdio", "--enable", "goals"]


async def finish_cleanup(coroutine):
    task = asyncio.create_task(coroutine)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    task.result()
    if interrupted:
        raise asyncio.CancelledError


class CodexConnection:
    request_timeout = 30
    stop_timeout = 5
    process_timeout = 2

    def __init__(self, command=None, cwd=None):
        self.command = command
        self.cwd = cwd
        self.process = None
        self.reader = None
        self.stderr_reader = None
        self.pending = {}
        self.events = asyncio.Queue()
        self.active_turns = {}
        self.finished_turns = {}
        self.goals = {}
        self.running_commands = {}
        self.completed_commands = []
        self.turn_changed = asyncio.Event()
        self.next_id = 0
        self.write_lock = asyncio.Lock()
        self.close_lock = asyncio.Lock()
        self.closed = False

    async def open(self):
        environment = os.environ.copy()
        if os.environ.get("AISPACE_CODEX_HOME"):
            codex_home = Path(os.environ["AISPACE_CODEX_HOME"]).expanduser()
            codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
            environment["CODEX_HOME"] = str(codex_home)
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *(self.command or codex_command()),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                cwd=self.cwd,
                start_new_session=os.name == "posix",
                limit=8 * 1024 * 1024,
            )
        )
        try:
            try:
                self.process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                while not spawn.done():
                    try:
                        await asyncio.shield(spawn)
                    except asyncio.CancelledError:
                        pass
                self.process = spawn.result()
                raise
            self.reader = asyncio.create_task(self._read())
            self.stderr_reader = asyncio.create_task(self._drain_stderr())
            await self.request(
                "initialize",
                {
                    "clientInfo": {"name": "aispace", "title": "aispace", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            await finish_cleanup(self.close())
            raise

    async def send(self, message):
        if self.closed or not self.process or self.process.returncode is not None:
            raise ProviderError("Процесс Codex завершился. Запустите задачу ещё раз.")
        async with self.write_lock:
            try:
                self.process.stdin.write((json.dumps(message) + "\n").encode())
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise ProviderError("Соединение с Codex прервалось") from error

    async def request(self, method, params=None, timeout=None):
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params or {}})
            response = await asyncio.wait_for(
                future, self.request_timeout if timeout is None else timeout
            )
            if "error" in response:
                raise CodexRPCError(method, response["error"].get("code", "unknown"))
            return response.get("result", {})
        except TimeoutError as error:
            raise ProviderError(f"Codex не ответил на {method} вовремя") from error
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def _drain_stderr(self):
        while await self.process.stderr.read(65536):
            pass

    async def _read(self):
        error = ProviderError("Процесс Codex завершился до окончания задачи")
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise TypeError
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        future.set_result(message)
                    continue
                if "id" in message:
                    await self._deny_request(message)
                    continue
                method = message.get("method")
                params = message.get("params") or {}
                thread_id = params.get("threadId")
                if method == "turn/started":
                    self.active_turns[thread_id] = params["turn"]["id"]
                    self.turn_changed.set()
                elif method == "turn/completed":
                    turn = params["turn"]
                    self.finished_turns[turn["id"]] = turn["status"]
                    if self.active_turns.get(thread_id) == turn["id"]:
                        self.active_turns.pop(thread_id, None)
                    self.turn_changed.set()
                elif method == "thread/goal/updated":
                    self.goals[thread_id] = params.get("goal")
                elif method == "thread/goal/cleared":
                    self.goals[thread_id] = None
                elif method in {"item/started", "item/completed"}:
                    item = params.get("item", {})
                    if item.get("type") == "commandExecution":
                        if method == "item/started":
                            self.running_commands[item["id"]] = {
                                "thread_id": thread_id,
                                "turn_id": params.get("turnId"),
                            }
                        else:
                            self.running_commands.pop(item["id"], None)
                            self.completed_commands.append(
                                {"id": item["id"], "status": item.get("status")}
                            )
                if method in {
                    "turn/started",
                    "turn/completed",
                    "item/agentMessage/delta",
                    "item/completed",
                    "thread/goal/updated",
                    "thread/goal/cleared",
                    "error",
                }:
                    self.events.put_nowait(message)
        except asyncio.CancelledError:
            return
        except (ValueError, KeyError, TypeError, OSError, ProviderError):
            error = ProviderError("Codex вернул некорректный поток событий")
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.events.put_nowait({"method": "connection/closed", "error": error})
            self.turn_changed.set()

    async def _deny_request(self, message):
        method = message["method"]
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            result = {"decision": "decline"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline", "content": None}
        else:
            await self.send(
                {"id": message["id"], "error": {"code": -32601, "message": "Unsupported request"}}
            )
            return
        await self.send({"id": message["id"], "result": result})

    async def stop(self, thread_id=None):
        threads = set(self.active_turns) | set(self.goals)
        if thread_id:
            threads.add(thread_id)
        try:
            async with asyncio.timeout(self.stop_timeout * 3):
                for current in threads:
                    try:
                        goal = await self.request(
                            "thread/goal/get", {"threadId": current}, self.stop_timeout
                        )
                        if (goal.get("goal") or {}).get("status") == "active":
                            await self.request(
                                "thread/goal/set",
                                {"threadId": current, "status": "paused"},
                                self.stop_timeout,
                            )
                    except ProviderError:
                        pass
                for current, turn_id in list(self.active_turns.items()):
                    try:
                        await self.request(
                            "turn/interrupt",
                            {"threadId": current, "turnId": turn_id},
                            self.stop_timeout,
                        )
                    except ProviderError:
                        pass
                    await self.wait_turn_end(turn_id)
                for current in threads:
                    with contextlib.suppress(ProviderError):
                        await self.request(
                            "thread/backgroundTerminals/clean",
                            {"threadId": current},
                            self.stop_timeout,
                        )
        except TimeoutError:
            pass
        finally:
            await self.close()

    async def wait_turn_end(self, turn_id):
        try:
            async with asyncio.timeout(self.stop_timeout):
                while turn_id not in self.finished_turns:
                    if self.reader and self.reader.done():
                        return
                    self.turn_changed.clear()
                    await self.turn_changed.wait()
        except TimeoutError:
            pass

    async def close(self):
        async with self.close_lock:
            if self.closed:
                return
            self.closed = True
            if self.process:
                if self.process.stdin:
                    self.process.stdin.close()
                if self.process.returncode is None:
                    self._signal(signal.SIGTERM)
                    try:
                        await asyncio.wait_for(self.process.wait(), self.process_timeout)
                    except TimeoutError:
                        self._signal(signal.SIGKILL)
                        await self.process.wait()
                if os.name == "posix":
                    self._signal(signal.SIGKILL)
            for task in (self.reader, self.stderr_reader):
                if task:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (self.reader, self.stderr_reader) if task),
                return_exceptions=True,
            )

    def _signal(self, value):
        with contextlib.suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(self.process.pid, value)
            elif value == signal.SIGTERM:
                self.process.terminate()
            else:
                self.process.kill()


class CodexProvider:
    continuation_timeout = 30
    commands = ("/goal", "/goal pause", "/goal resume", "/goal clear", "/stop")

    def __init__(self, store, directories, connection_factory=CodexConnection):
        self.store = store
        self.directories = directories
        self.connection_factory = connection_factory
        self.auth = None
        self.auth_lock = asyncio.Lock()
        self.running = {}

    async def auth_connection(self):
        async with self.auth_lock:
            if self.auth and (self.auth.closed or self.auth.reader.done()):
                await self.auth.close()
                self.auth = None
            if self.auth is None:
                connection = self.connection_factory()
                await connection.open()
                self.auth = connection
            return self.auth

    async def status(self):
        result = {
            "available": False,
            "authenticated": False,
            "auth_type": None,
            "account_label": None,
            "message": "",
            "capabilities": {"workspace": True, "commands": list(self.commands)},
        }
        try:
            codex_command()
            result["available"] = True
            connection = await self.auth_connection()
            response = await connection.request("account/read", {"refreshToken": False})
            account = response.get("account") or {}
            result["auth_type"] = account.get("type")
            result["authenticated"] = account.get("type") == "chatgpt"
            label = account.get("planType") or "ChatGPT"
            if account.get("email"):
                label = f"{account['email']} · {label}"
            result["account_label"] = label if account else None
            result["message"] = (
                "Подключено через подписку ChatGPT"
                if result["authenticated"]
                else "Войдите через ChatGPT для использования подписки"
            )
        except (ProviderError, OSError) as error:
            result["message"] = (
                str(error) if isinstance(error, ProviderError) else "Не удалось запустить Codex CLI"
            )
        return result

    async def login(self):
        connection = await self.auth_connection()
        response = await connection.request("account/login/start", {"type": "chatgptDeviceCode"})
        return {
            "login_id": response["loginId"],
            "auth_url": response["verificationUrl"],
            "user_code": response["userCode"],
        }

    async def cancel_login(self, login_id):
        connection = await self.auth_connection()
        await connection.request("account/login/cancel", {"loginId": login_id})

    async def logout(self):
        if self.running:
            raise ProviderError("Сначала остановите выполняющиеся тасклеты")
        connection = await self.auth_connection()
        await connection.request("account/logout")

    async def close(self):
        await asyncio.gather(
            *(connection.stop(thread_id) for connection, thread_id in self.running.values()),
            return_exceptions=True,
        )
        if self.auth:
            await self.auth.close()
            self.auth = None

    async def _thread(self, connection, settings, model, messages, tasklet):
        raw_path = tasklet.get("working_directory") or settings.get("working_directory")
        if not raw_path:
            raise ProviderError("Выберите рабочую директорию для Codex")
        cwd = str(self.directories.resolve(raw_path))
        response = await connection.request("account/read", {"refreshToken": False})
        if (response.get("account") or {}).get("type") != "chatgpt":
            raise ProviderError("Войдите через ChatGPT в настройках aispace")
        params = {
            "cwd": cwd,
            "sandbox": settings.get("codex_sandbox", "read-only"),
            "approvalPolicy": "never",
            "modelProvider": "openai",
            "developerInstructions": "\n\n".join(
                message["content"] for message in messages if message["role"] == "system"
            ),
        }
        if model:
            params["model"] = model
        session = self.store.codex_session(tasklet["id"])
        resumed = session is not None and session["cwd"] == cwd
        if resumed:
            params["threadId"] = session["thread_id"]
            response = await connection.request("thread/resume", params)
        else:
            response = await connection.request("thread/start", params)
        thread_id = response["thread"]["id"]
        self.store.save_codex_session(tasklet["id"], thread_id, cwd)
        self.running[tasklet["id"]] = (connection, thread_id)
        return thread_id, cwd, resumed, session is None

    @staticmethod
    def goal_text(goal):
        if not goal:
            return "У этого тасклета нет активной цели. Задайте её командой /goal <цель>."
        statuses = {
            "active": "выполняется",
            "paused": "на паузе",
            "blocked": "заблокирована",
            "usageLimited": "достигнут лимит подписки",
            "budgetLimited": "достигнут бюджет",
            "complete": "выполнена",
        }
        return f"Цель: {goal['objective']}\nСтатус: {statuses.get(goal['status'], goal['status'])}"

    async def stream(self, settings, model, messages, tasklet):
        if tasklet["id"] in self.running:
            raise ProviderError("Этот тасклет уже выполняется")
        connection = self.connection_factory()
        self.running[tasklet["id"]] = (connection, None)
        thread_id = None
        try:
            raw_path = tasklet.get("working_directory") or settings.get("working_directory")
            if not raw_path:
                raise ProviderError("Выберите рабочую директорию для Codex")
            connection.cwd = str(self.directories.resolve(raw_path))
            await connection.open()
            thread_id, cwd, resumed, first_session = await self._thread(
                connection, settings, model, messages, tasklet
            )
            content = messages[-1]["content"].strip()
            goal_mode = False
            if re.match(r"^/([a-z][a-z0-9_-]*)(?:\s|$)", content):
                parts = content.split(maxsplit=1)
                command = parts[0]
                argument = parts[1].strip() if len(parts) > 1 else ""
                if command != "/goal":
                    raise ProviderError(
                        "Эта команда не поддерживается aispace. Доступны /goal, /goal pause, /goal resume и /goal clear."
                    )
                goal = (await connection.request("thread/goal/get", {"threadId": thread_id})).get(
                    "goal"
                )
                if not argument or argument in {"status", "inspect"}:
                    yield self.goal_text(goal)
                    return
                if argument in {"pause", "clear"}:
                    if argument == "clear":
                        await connection.request("thread/goal/clear", {"threadId": thread_id})
                        yield "Цель удалена. История чата сохранена."
                    elif goal:
                        response = await connection.request(
                            "thread/goal/set", {"threadId": thread_id, "status": "paused"}
                        )
                        yield self.goal_text(response.get("goal"))
                    else:
                        yield self.goal_text(None)
                    return
                if argument == "resume":
                    if not goal:
                        raise ProviderError("Сначала задайте цель: /goal <цель>")
                    if goal["status"] == "complete":
                        yield self.goal_text(goal)
                        return
                    content = f"Продолжи работу над целью: {goal['objective']}"
                else:
                    if len(argument) > 4000:
                        raise ProviderError("Цель должна содержать не больше 4000 символов")
                    await connection.request(
                        "thread/goal/set",
                        {"threadId": thread_id, "objective": argument, "status": "paused"},
                    )
                    content = argument
                goal_mode = True
            if not resumed and first_session:
                history = [message for message in messages[:-1] if message["role"] != "system"]
                if history:
                    transcript = "\n\n".join(
                        f"{message['role']}: {message['content']}" for message in history
                    )
                    content = (
                        f"Предыдущая переписка тасклета:\n{transcript}\n\nНовый запрос:\n{content}"
                    )
            response = await connection.request(
                "turn/start",
                {"threadId": thread_id, "input": [{"type": "text", "text": content}]},
            )
            self.store.save_codex_session(tasklet["id"], thread_id, cwd)
            turn_id = response["turn"]["id"]
            if turn_id not in connection.finished_turns:
                connection.active_turns[thread_id] = turn_id
            if goal_mode:
                await connection.request(
                    "thread/goal/set", {"threadId": thread_id, "status": "active"}
                )
            seen_items = {}
            last_item = None
            waiting_continuation = False
            while True:
                try:
                    if waiting_continuation:
                        event = await asyncio.wait_for(
                            connection.events.get(), self.continuation_timeout
                        )
                    else:
                        event = await connection.events.get()
                except TimeoutError as error:
                    raise ProviderError(
                        "Codex не продолжил активную цель. Цель поставлена на паузу; можно продолжить через /goal resume."
                    ) from error
                method = event["method"]
                if method == "connection/closed":
                    raise event["error"]
                params = event.get("params", {})
                if params.get("threadId") != thread_id:
                    continue
                if method == "turn/started":
                    waiting_continuation = False
                elif method == "item/agentMessage/delta":
                    item_id = params.get("itemId", params.get("turnId", "message"))
                    delta = params.get("delta", "")
                    if delta:
                        if last_item is not None and last_item != item_id:
                            yield "\n\n"
                        seen_items[item_id] = seen_items.get(item_id, "") + delta
                        last_item = item_id
                        yield delta
                elif method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        item_id = item["id"]
                        text = item.get("text", "")
                        previous = seen_items.get(item_id, "")
                        if text.startswith(previous) and len(text) > len(previous):
                            if last_item is not None and last_item != item_id:
                                yield "\n\n"
                            yield text[len(previous) :]
                            seen_items[item_id] = text
                            last_item = item_id
                elif method == "turn/completed":
                    status = params["turn"]["status"]
                    if status == "interrupted":
                        raise ProviderError("Выполнение Codex остановлено")
                    if status != "completed":
                        raise ProviderError(
                            "Codex не завершил задачу. Проверьте доступность модели и лимиты подписки."
                        )
                    if not goal_mode:
                        return
                    goal = (
                        await connection.request("thread/goal/get", {"threadId": thread_id})
                    ).get("goal")
                    if goal and goal["status"] == "complete":
                        return
                    if not goal or goal["status"] != "active":
                        raise ProviderError(self.goal_text(goal))
                    waiting_continuation = True
        finally:
            try:
                await finish_cleanup(connection.stop(thread_id))
            finally:
                self.running.pop(tasklet["id"], None)

    async def command(self, tasklet, settings, command):
        if command.strip() not in {
            "/goal",
            "/goal status",
            "/goal inspect",
            "/goal pause",
            "/goal clear",
        }:
            raise ProviderError("Отправьте /goal <цель> или /goal resume через чат тасклета")
        return "".join(
            [
                chunk
                async for chunk in self.stream(
                    settings,
                    tasklet.get("model") or settings.get("model", ""),
                    [{"role": "user", "content": command}],
                    tasklet,
                )
            ]
        )
