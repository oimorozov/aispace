import asyncio
import re
from contextlib import suppress

from fastapi import HTTPException

from .models import Pipeline
from .provider import ProviderError
from .storage import new_id, now


class Events:
    def __init__(self):
        self.subscribers = set()

    def subscribe(self):
        queue = asyncio.Queue(maxsize=256)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.subscribers.discard(queue)

    def publish(self, event, data):
        for queue in tuple(self.subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait((event, data))


class Runtime:
    def __init__(self, store, provider, codex=None, directories=None):
        self.store = store
        self.provider = provider
        self.codex = codex
        self.directories = directories
        self.events = Events()
        self.lock = asyncio.Lock()
        self.worker = None
        self.worker_workspace_id = None

    @property
    def active(self):
        workspace = (
            self.store.workspace_info(self.worker_workspace_id)
            if self.worker_workspace_id
            else None
        )
        return workspace is not None and workspace["pipeline"]["status"] in {"running", "stopping"}

    @property
    def active_workspace_id(self):
        return self.worker_workspace_id if self.active else None

    def require_workspace(self, workspace_id=None):
        workspace_id = workspace_id or self.store.default_workspace_id
        workspace = self.store.workspace_info(workspace_id) if workspace_id else None
        if workspace is None:
            raise HTTPException(404, "Пространство не найдено")
        return workspace

    def ensure_idle(self, workspace_id=None):
        if workspace_id is not None:
            self.require_workspace(workspace_id)
        if self.active and (workspace_id is None or workspace_id == self.worker_workspace_id):
            workspace = self.require_workspace(self.worker_workspace_id)
            raise HTTPException(
                409,
                f"Сейчас выполняется пространство «{workspace['name']}». Сначала остановите его пайплайн.",
            )

    def require_tasklet(self, tasklet_id, workspace_id=None):
        workspace_id = self.require_workspace(workspace_id)["id"]
        tasklet = self.store.tasklet(tasklet_id, workspace_id=workspace_id)
        if tasklet is None:
            raise HTTPException(404, "Тасклет не найден")
        return tasklet

    def require_edge(self, edge_id, workspace_id=None):
        workspace_id = self.require_workspace(workspace_id)["id"]
        edge = next(
            (edge for edge in self.store.edges(workspace_id) if edge["id"] == edge_id), None
        )
        if edge is None:
            raise HTTPException(404, "Связь не найдена")
        return edge

    def changed(self, workspace_id=None):
        workspace_id = workspace_id or self.store.default_workspace_id
        workspace = self.store.workspace(workspace_id) if workspace_id else None
        if workspace:
            self.events.publish("workspace", workspace)
        self.events.publish("workspaces", self.store.workspaces())

    def invalidate(self, roots, exclude=None, workspace_id=None):
        workspace_id = self.require_workspace(workspace_id)["id"]
        for tasklet_id in self.descendants(roots, workspace_id):
            if not exclude or tasklet_id not in exclude:
                self.store.update_tasklet(
                    tasklet_id, {"status": "idle", "error": None}, workspace_id=workspace_id
                )

    def descendants(self, roots, workspace_id):
        children = {}
        for edge in self.store.edges(workspace_id):
            children.setdefault(edge["source"], []).append(edge["target"])
        visited = set()
        pending = list(roots)
        while pending:
            tasklet_id = pending.pop()
            if tasklet_id in visited:
                continue
            visited.add(tasklet_id)
            pending.extend(children.get(tasklet_id, []))
        return visited

    def validate_edge(self, source, target, workspace_id=None):
        workspace_id = self.require_workspace(workspace_id)["id"]
        self.require_tasklet(source, workspace_id)
        self.require_tasklet(target, workspace_id)
        edges = self.store.edges(workspace_id)
        if any(edge["source"] == source and edge["target"] == target for edge in edges):
            raise HTTPException(409, "Такая связь уже существует")
        visited = set()
        pending = [target]
        children = {}
        for edge in edges:
            children.setdefault(edge["source"], []).append(edge["target"])
        while pending:
            current = pending.pop()
            if current == source:
                raise HTTPException(
                    422, "Связь создаёт цикл. Пайплайн должен быть направленным графом без циклов."
                )
            if current not in visited:
                visited.add(current)
                pending.extend(children.get(current, []))

    @staticmethod
    def normalize_command(content):
        if re.match(r"^/goal(?:\s|$)", content):
            return f"/goal {content[5:].strip()}".rstrip()
        return content

    @staticmethod
    def validate_command(content, mode):
        match = re.match(r"^/([a-z][a-z0-9_-]*)(?:\s|$)", content)
        if not match:
            return
        name = match[1]
        if mode != "codex":
            raise HTTPException(422, "Команды исполнителя доступны в режиме ChatGPT через Codex")
        if name not in {"goal", "stop"}:
            raise HTTPException(
                422, f"Команда /{name} пока не поддерживается. Доступны /goal и /stop"
            )
        if name == "stop" and content != "/stop":
            raise HTTPException(422, "Используйте /stop без аргументов")
        if name == "goal":
            value = content[5:].strip()
            if len(value) > 4000:
                raise HTTPException(422, "Цель должна содержать не более 4000 символов")

    async def send_message(self, tasklet_id, content, workspace_id=None):
        content = self.normalize_command(content)
        async with self.lock:
            workspace_id = self.require_workspace(workspace_id)["id"]
            tasklet = self.require_tasklet(tasklet_id, workspace_id)
            settings = self.store.settings(private=True, workspace_id=workspace_id)
            self.validate_command(content, settings["execution_mode"])
            own_active = self.active_workspace_id == workspace_id
            stop_requested = content == "/stop" or (content == "/goal pause" and own_active)
            if not stop_requested and content in {
                "/goal",
                "/goal status",
                "/goal inspect",
                "/goal pause",
                "/goal clear",
            }:
                self.ensure_idle()
                try:
                    result = await self.codex.command(tasklet, settings, content)
                except ProviderError as error:
                    raise HTTPException(422, str(error)) from error
                self.record_command(tasklet_id, content, result, workspace_id)
                return self.store.pipeline(workspace_id)
        if stop_requested:
            result = await self.stop(workspace_id)
            async with self.lock:
                if self.store.workspace_info(workspace_id) and self.store.tasklet(
                    tasklet_id, workspace_id=workspace_id
                ):
                    self.record_command(
                        tasklet_id,
                        content,
                        "Пайплайн остановлен. Активные цели приостановлены."
                        if own_active
                        else "В этом пространстве нет активного пайплайна.",
                        workspace_id,
                    )
            return result
        return await self.start(
            [tasklet_id], (tasklet_id, content), workspace_id, reset_context=False
        )

    def record_command(self, tasklet_id, content, result, workspace_id=None):
        workspace_id = self.require_workspace(workspace_id)["id"]
        for role, value in (("user", content), ("assistant", result)):
            message = self.store.create_message(
                tasklet_id, role, value, None, workspace_id=workspace_id
            )
            self.events.publish("message", message)

    async def restart(self, tasklet_id, workspace_id=None):
        return await self.start([tasklet_id], workspace_id=workspace_id, exact=True)

    async def start(
        self, selected=None, followup=None, workspace_id=None, *, reset_context=True, exact=False
    ):
        async with self.lock:
            workspace_id = self.require_workspace(workspace_id)["id"]
            self.ensure_idle()
            all_tasks = {tasklet["id"]: tasklet for tasklet in self.store.tasklets(workspace_id)}
            if selected is None:
                selected = list(all_tasks)
            if not selected:
                raise HTTPException(422, "Создайте хотя бы один тасклет для запуска")
            for tasklet_id in selected:
                self.require_tasklet(tasklet_id, workspace_id)
            chosen = set(selected)
            edges = self.store.edges(workspace_id)
            parents = {tasklet_id: set() for tasklet_id in all_tasks}
            for edge in edges:
                parents[edge["target"]].add(edge["source"])
            pending = list(chosen)
            ancestors = set()
            while pending:
                current = pending.pop()
                for parent in parents[current]:
                    if exact:
                        if parent not in ancestors:
                            ancestors.add(parent)
                            pending.append(parent)
                    elif parent not in chosen and all_tasks[parent]["status"] != "completed":
                        chosen.add(parent)
                        pending.append(parent)
            incomplete = [
                all_tasks[parent]["title"]
                for parent in sorted(ancestors)
                if all_tasks[parent]["status"] != "completed"
            ]
            if incomplete:
                names = ", ".join(f"«{title}»" for title in incomplete)
                raise HTTPException(
                    409,
                    f"Сначала завершите зависимости: {names}. Запустите их отдельно или весь пайплайн.",
                )
            settings = self.store.settings(private=True, workspace_id=workspace_id)
            codex_mode = settings["execution_mode"] == "codex"
            if codex_mode:
                status = await self.codex.status()
                if not status["authenticated"]:
                    raise HTTPException(422, status["message"])
            elif not settings["api_key"]:
                raise HTTPException(422, "Добавьте API-ключ в настройках")
            for tasklet_id in chosen:
                tasklet = all_tasks[tasklet_id]
                if codex_mode:
                    self.directories.resolve(
                        tasklet["working_directory"] or settings["working_directory"]
                    )
                elif not (tasklet["model"] or settings["model"]):
                    raise HTTPException(
                        422, f"Выберите модель для тасклета «{tasklet['title']}» или в настройках"
                    )
                if not tasklet["prompt"].strip() and not (followup and tasklet_id == followup[0]):
                    raise HTTPException(422, f"Добавьте промпт тасклету «{tasklet['title']}»")
                content = (
                    followup[1] if followup and tasklet_id == followup[0] else tasklet["prompt"]
                )
                content = self.normalize_command(content)
                self.validate_command(content, settings["execution_mode"])
                if reset_context and content == "/goal resume":
                    raise HTTPException(
                        422,
                        "Новый запуск начинает чистый чат. Отправьте /goal resume в существующий "
                        "чат или укажите /goal с новой целью в промпте.",
                    )
                if content in {
                    "/goal",
                    "/goal status",
                    "/goal inspect",
                    "/goal pause",
                    "/goal clear",
                    "/stop",
                }:
                    raise HTTPException(
                        422,
                        "Отправьте эту команду в чат. В промпте укажите задачу или /goal с целью.",
                    )
            pipeline = Pipeline(
                id=new_id(), status="running", started_at=now(), total=len(chosen)
            ).model_dump()
            self.store.accept_run(
                pipeline,
                chosen,
                self.descendants(chosen, workspace_id),
                reset_context=reset_context,
                workspace_id=workspace_id,
            )
            self.worker_workspace_id = workspace_id
            if reset_context:
                self.events.publish("chat_reset", {
                    "workspace_id": workspace_id,
                    "tasklet_ids": sorted(chosen),
                    "conversation_id": pipeline["id"],
                })
            self.changed(workspace_id)
            self.worker = asyncio.create_task(
                self.run(pipeline, chosen, parents, settings, followup, workspace_id)
            )
            return pipeline

    async def execute(self, tasklet_id, pipeline_id, settings, followup, workspace_id):
        tasklet = self.store.update_tasklet(
            tasklet_id,
            {"status": "running", "error": None, "last_output": ""},
            workspace_id=workspace_id,
        )
        self.changed(workspace_id)
        history = self.store.messages(tasklet_id, workspace_id=workspace_id)
        content = followup[1] if followup and followup[0] == tasklet_id else tasklet["prompt"]
        user = self.store.create_message(
            tasklet_id, "user", content, pipeline_id, workspace_id=workspace_id
        )
        self.events.publish("message", user)
        messages = []
        context = settings["workspace_context"].strip()
        if context:
            messages.append({"role": "system", "content": context})
        if followup and followup[0] == tasklet_id and not history and tasklet["prompt"]:
            messages.append({"role": "system", "content": tasklet["prompt"]})
        for edge in self.store.edges(workspace_id):
            if edge["target"] == tasklet_id and edge["pass_context"]:
                predecessor = self.store.tasklet(edge["source"], workspace_id=workspace_id)
                if predecessor["last_output"]:
                    messages.append(
                        {
                            "role": "system",
                            "content": f"Результат задачи «{predecessor['title']}»:\n{predecessor['last_output']}",
                        }
                    )
        messages.extend(
            {"role": message["role"], "content": message["content"]}
            for message in history
            if message["content"]
        )
        messages.append({"role": "user", "content": content})
        assistant = None
        output = ""
        try:
            model = tasklet["model"] or settings["model"]
            stream = (
                self.codex.stream(settings, model, messages, tasklet)
                if settings["execution_mode"] == "codex"
                else self.provider.stream(settings, model, messages)
            )
            async for chunk in stream:
                output += chunk
                if assistant is None:
                    assistant = self.store.create_message(
                        tasklet_id, "assistant", output, pipeline_id, workspace_id=workspace_id
                    )
                else:
                    assistant = self.store.update_message(
                        assistant, output, workspace_id=workspace_id
                    )
                self.store.update_tasklet(
                    tasklet_id, {"last_output": output}, workspace_id=workspace_id
                )
                self.events.publish("message", assistant)
            if not output.strip():
                raise ProviderError("Модель завершила запрос без текстового ответа")
            self.store.update_tasklet(
                tasklet_id, {"status": "completed", "error": None}, workspace_id=workspace_id
            )
        except asyncio.CancelledError:
            self.store.update_tasklet(
                tasklet_id,
                {"status": "cancelled", "error": "Остановлено пользователем"},
                workspace_id=workspace_id,
            )
            raise
        except ProviderError as error:
            self.store.update_tasklet(
                tasklet_id, {"status": "failed", "error": str(error)}, workspace_id=workspace_id
            )
        except Exception:
            self.store.update_tasklet(
                tasklet_id,
                {"status": "failed", "error": "Не удалось выполнить задачу"},
                workspace_id=workspace_id,
            )
        finally:
            self.changed(workspace_id)

    async def run(self, pipeline, chosen, parents, settings, followup, workspace_id):
        pending = set(chosen)
        running = {}
        stopped = False
        failure = None
        try:
            while pending or running:
                for tasklet_id in sorted(pending):
                    statuses = [
                        self.store.tasklet(parent, workspace_id=workspace_id)["status"]
                        for parent in parents[tasklet_id]
                    ]
                    if any(status in {"failed", "blocked", "cancelled"} for status in statuses):
                        self.store.update_tasklet(
                            tasklet_id,
                            {"status": "blocked", "error": "Зависимость не выполнена успешно"},
                            workspace_id=workspace_id,
                        )
                        pending.remove(tasklet_id)
                        self.changed(workspace_id)
                for tasklet_id in sorted(pending):
                    if len(running) >= settings["max_parallel"]:
                        break
                    if all(
                        self.store.tasklet(parent, workspace_id=workspace_id)["status"]
                        == "completed"
                        for parent in parents[tasklet_id]
                    ):
                        task = asyncio.create_task(
                            self.execute(
                                tasklet_id, pipeline["id"], settings, followup, workspace_id
                            )
                        )
                        running[task] = tasklet_id
                        pending.remove(tasklet_id)
                if running:
                    done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        del running[task]
                        task.result()
                    pipeline["completed"] = sum(
                        self.store.tasklet(tasklet_id, workspace_id=workspace_id)["status"]
                        == "completed"
                        for tasklet_id in chosen
                    )
                    self.store.save_pipeline(pipeline, workspace_id=workspace_id)
                    self.changed(workspace_id)
                elif pending:
                    for tasklet_id in pending:
                        self.store.update_tasklet(
                            tasklet_id,
                            {"status": "blocked", "error": "Не удалось разрешить зависимости"},
                            workspace_id=workspace_id,
                        )
                    pending.clear()
        except asyncio.CancelledError:
            stopped = True
        except Exception:
            failure = "Ошибка выполнения пайплайна"
        finally:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            for tasklet_id in chosen:
                if self.store.tasklet(tasklet_id, workspace_id=workspace_id)["status"] in {
                    "queued",
                    "running",
                }:
                    self.store.update_tasklet(
                        tasklet_id,
                        {
                            "status": "cancelled",
                            "error": "Остановлено пользователем"
                            if stopped
                            else "Выполнение прервано",
                        },
                        workspace_id=workspace_id,
                    )
            statuses = [
                self.store.tasklet(tasklet_id, workspace_id=workspace_id)["status"]
                for tasklet_id in chosen
            ]
            status = (
                "cancelled"
                if stopped
                else "completed"
                if all(value == "completed" for value in statuses)
                else "failed"
            )
            pipeline.update(
                status=status,
                completed=statuses.count("completed"),
                finished_at=now(),
                error=failure
                or ("Не все тасклеты выполнены успешно" if status == "failed" else None),
            )
            self.store.save_pipeline(pipeline, workspace_id=workspace_id)
            self.changed(workspace_id)
        return pipeline

    async def stop(self, workspace_id=None):
        workspace_id = workspace_id or self.active_workspace_id or self.store.default_workspace_id
        if workspace_id is None:
            return Pipeline().model_dump()
        async with self.lock:
            self.require_workspace(workspace_id)
            worker = self.worker
            if not self.active or self.worker_workspace_id != workspace_id:
                return self.store.pipeline(workspace_id)
            pipeline = self.store.pipeline(workspace_id)
            already_stopping = pipeline["status"] == "stopping"
            pipeline["status"] = "stopping"
            self.store.save_pipeline(pipeline, workspace_id=workspace_id)
            self.changed(workspace_id)
            if not already_stopping:
                worker.cancel()
        completed = None
        with suppress(asyncio.CancelledError):
            completed = await worker
        async with self.lock:
            result = completed or {**pipeline, "status": "cancelled", "finished_at": now()}
            if self.store.workspace_info(workspace_id) is None:
                return result
            current = self.store.pipeline(workspace_id)
            if current["id"] != pipeline["id"]:
                return result
            if current["status"] == "stopping":
                pipeline.update(status="cancelled", finished_at=now())
                self.store.save_pipeline(pipeline, workspace_id=workspace_id)
                for tasklet in self.store.tasklets(workspace_id):
                    if tasklet["status"] in {"queued", "running"}:
                        self.store.update_tasklet(
                            tasklet["id"],
                            {"status": "cancelled", "error": "Остановлено пользователем"},
                            workspace_id=workspace_id,
                        )
                self.changed(workspace_id)
            return self.store.pipeline(workspace_id)
