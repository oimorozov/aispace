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

    @property
    def active(self):
        return self.store.pipeline()["status"] in {"running", "stopping"}

    def ensure_idle(self):
        if self.active:
            raise HTTPException(409, "Сначала остановите текущий пайплайн")

    def require_tasklet(self, tasklet_id):
        tasklet = self.store.tasklet(tasklet_id)
        if tasklet is None:
            raise HTTPException(404, "Тасклет не найден")
        return tasklet

    def require_edge(self, edge_id):
        edge = next((edge for edge in self.store.edges() if edge["id"] == edge_id), None)
        if edge is None:
            raise HTTPException(404, "Связь не найдена")
        return edge

    def changed(self):
        self.events.publish("workspace", self.store.workspace())

    def invalidate(self, roots, exclude=None):
        children = {}
        for edge in self.store.edges():
            children.setdefault(edge["source"], []).append(edge["target"])
        visited = set()
        pending = list(roots)
        while pending:
            tasklet_id = pending.pop()
            if tasklet_id in visited:
                continue
            visited.add(tasklet_id)
            pending.extend(children.get(tasklet_id, []))
            if not exclude or tasklet_id not in exclude:
                self.store.update_tasklet(tasklet_id, {"status": "idle", "error": None})

    def validate_edge(self, source, target):
        self.require_tasklet(source)
        self.require_tasklet(target)
        edges = self.store.edges()
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

    async def send_message(self, tasklet_id, content):
        content = self.normalize_command(content)
        tasklet = self.require_tasklet(tasklet_id)
        settings = self.store.settings(private=True)
        self.validate_command(content, settings["execution_mode"])
        if content == "/stop" or (content == "/goal pause" and self.active):
            result = await self.stop()
            self.record_command(
                tasklet_id, content, "Пайплайн остановлен. Активные цели приостановлены."
            )
            return result
        if content in {"/goal", "/goal status", "/goal inspect", "/goal pause", "/goal clear"}:
            async with self.lock:
                self.ensure_idle()
                try:
                    result = await self.codex.command(tasklet, settings, content)
                except ProviderError as error:
                    raise HTTPException(422, str(error)) from error
                self.record_command(tasklet_id, content, result)
                return self.store.pipeline()
        return await self.start([tasklet_id], (tasklet_id, content))

    def record_command(self, tasklet_id, content, result):
        for role, value in (("user", content), ("assistant", result)):
            message = self.store.create_message(tasklet_id, role, value, None)
            self.events.publish("message", message)

    async def start(self, selected=None, followup=None):
        async with self.lock:
            self.ensure_idle()
            all_tasks = {tasklet["id"]: tasklet for tasklet in self.store.tasklets()}
            if selected is None:
                selected = list(all_tasks)
            if not selected:
                raise HTTPException(422, "Создайте хотя бы один тасклет для запуска")
            for tasklet_id in selected:
                self.require_tasklet(tasklet_id)
            chosen = set(selected)
            edges = self.store.edges()
            parents = {tasklet_id: set() for tasklet_id in all_tasks}
            for edge in edges:
                parents[edge["target"]].add(edge["source"])
            pending = list(chosen)
            while pending:
                current = pending.pop()
                for parent in parents[current]:
                    if parent not in chosen and all_tasks[parent]["status"] != "completed":
                        chosen.add(parent)
                        pending.append(parent)
            settings = self.store.settings(private=True)
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
            self.invalidate(chosen, exclude=chosen)
            self.store.save_pipeline(pipeline)
            for tasklet_id in chosen:
                self.store.update_tasklet(tasklet_id, {"status": "queued", "error": None})
            self.changed()
            self.worker = asyncio.create_task(
                self.run(pipeline, chosen, parents, settings, followup)
            )
            return pipeline

    async def execute(self, tasklet_id, pipeline_id, settings, followup):
        tasklet = self.store.update_tasklet(
            tasklet_id, {"status": "running", "error": None, "last_output": ""}
        )
        self.changed()
        history = self.store.messages(tasklet_id)
        content = followup[1] if followup and followup[0] == tasklet_id else tasklet["prompt"]
        user = self.store.create_message(tasklet_id, "user", content, pipeline_id)
        self.events.publish("message", user)
        messages = []
        context = settings["workspace_context"].strip()
        if context:
            messages.append({"role": "system", "content": context})
        if followup and followup[0] == tasklet_id and not history and tasklet["prompt"]:
            messages.append({"role": "system", "content": tasklet["prompt"]})
        for edge in self.store.edges():
            if edge["target"] == tasklet_id and edge["pass_context"]:
                predecessor = self.store.tasklet(edge["source"])
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
                        tasklet_id, "assistant", output, pipeline_id
                    )
                else:
                    assistant = self.store.update_message(assistant, output)
                self.store.update_tasklet(tasklet_id, {"last_output": output})
                self.events.publish("message", assistant)
            if not output.strip():
                raise ProviderError("Модель завершила запрос без текстового ответа")
            self.store.update_tasklet(tasklet_id, {"status": "completed", "error": None})
        except asyncio.CancelledError:
            self.store.update_tasklet(
                tasklet_id, {"status": "cancelled", "error": "Остановлено пользователем"}
            )
            raise
        except ProviderError as error:
            self.store.update_tasklet(tasklet_id, {"status": "failed", "error": str(error)})
        except Exception:
            self.store.update_tasklet(
                tasklet_id, {"status": "failed", "error": "Не удалось выполнить задачу"}
            )
        finally:
            self.changed()

    async def run(self, pipeline, chosen, parents, settings, followup):
        pending = set(chosen)
        running = {}
        stopped = False
        failure = None
        try:
            while pending or running:
                for tasklet_id in sorted(pending):
                    statuses = [
                        self.store.tasklet(parent)["status"] for parent in parents[tasklet_id]
                    ]
                    if any(status in {"failed", "blocked", "cancelled"} for status in statuses):
                        self.store.update_tasklet(
                            tasklet_id,
                            {"status": "blocked", "error": "Зависимость не выполнена успешно"},
                        )
                        pending.remove(tasklet_id)
                        self.changed()
                for tasklet_id in sorted(pending):
                    if len(running) >= settings["max_parallel"]:
                        break
                    if all(
                        self.store.tasklet(parent)["status"] == "completed"
                        for parent in parents[tasklet_id]
                    ):
                        task = asyncio.create_task(
                            self.execute(tasklet_id, pipeline["id"], settings, followup)
                        )
                        running[task] = tasklet_id
                        pending.remove(tasklet_id)
                if running:
                    done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        del running[task]
                        task.result()
                    pipeline["completed"] = sum(
                        self.store.tasklet(tasklet_id)["status"] == "completed"
                        for tasklet_id in chosen
                    )
                    self.store.save_pipeline(pipeline)
                    self.changed()
                elif pending:
                    for tasklet_id in pending:
                        self.store.update_tasklet(
                            tasklet_id,
                            {"status": "blocked", "error": "Не удалось разрешить зависимости"},
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
                if self.store.tasklet(tasklet_id)["status"] in {"queued", "running"}:
                    self.store.update_tasklet(
                        tasklet_id,
                        {
                            "status": "cancelled",
                            "error": "Остановлено пользователем"
                            if stopped
                            else "Выполнение прервано",
                        },
                    )
            statuses = [self.store.tasklet(tasklet_id)["status"] for tasklet_id in chosen]
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
            self.store.save_pipeline(pipeline)
            self.changed()

    async def stop(self):
        async with self.lock:
            worker = self.worker
            if not self.active or worker is None:
                return self.store.pipeline()
            pipeline = self.store.pipeline()
            already_stopping = pipeline["status"] == "stopping"
            pipeline["status"] = "stopping"
            self.store.save_pipeline(pipeline)
            self.changed()
            if not already_stopping:
                worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        async with self.lock:
            if self.store.pipeline()["status"] == "stopping":
                pipeline.update(status="cancelled", finished_at=now())
                self.store.save_pipeline(pipeline)
                for tasklet in self.store.tasklets():
                    if tasklet["status"] in {"queued", "running"}:
                        self.store.update_tasklet(
                            tasklet["id"],
                            {"status": "cancelled", "error": "Остановлено пользователем"},
                        )
                self.changed()
            return self.store.pipeline()
