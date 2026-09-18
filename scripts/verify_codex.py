import asyncio
import contextlib
import json
import tempfile
from pathlib import Path
from uuid import uuid4

from aispace.codex import CodexConnection, CodexProvider, codex_command
from aispace.directories import Directories
from aispace.models import TaskletCreate
from aispace.provider import ChatProvider
from aispace.runtime import Runtime
from aispace.storage import Store


async def wait_for(predicate, timeout=90):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


async def main():
    with tempfile.TemporaryDirectory(prefix="aispace-codex-live-") as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        nonce = f"AISPACE_{uuid4().hex}"
        (workspace / "CONTEXT.txt").write_text(nonce)
        store = Store(root / "data")
        directories = Directories(workspace)
        connections = []

        def connection_factory():
            connection = CodexConnection(
                [*codex_command(), "-c", 'model_reasoning_effort="low"']
            )
            connections.append(connection)
            return connection

        codex = CodexProvider(store, directories, connection_factory=connection_factory)
        runtime = Runtime(store, ChatProvider(), codex, directories)
        store.save_settings(
            {
                "execution_mode": "codex",
                "working_directory": str(workspace),
                "codex_sandbox": "read-only",
                "model": "",
                "max_parallel": 2,
            }
        )
        report = {}
        try:
            status = await codex.status()
            assert status["authenticated"], status["message"]
            report["chatgpt_authenticated"] = True
            tasklet = store.create_tasklet(
                TaskletCreate(
                    title="Codex workspace verification",
                    prompt="Прочитай файл CONTEXT.txt в рабочей папке и ответь только его содержимым. Не изменяй файлы.",
                ).model_dump()
            )
            await runtime.start([tasklet["id"]])
            await asyncio.wait_for(asyncio.shield(runtime.worker), 120)
            result = store.tasklet(tasklet["id"])
            assert result["status"] == "completed", result["error"]
            assert nonce in result["last_output"], (
                "Codex did not read the selected workspace"
            )
            session = store.codex_session(tasklet["id"])
            report["selected_directory_read"] = True
            await runtime.send_message(
                tasklet["id"],
                "Повтори значение из своего предыдущего ответа без чтения файлов и без инструментов.",
            )
            await asyncio.wait_for(asyncio.shield(runtime.worker), 120)
            assert nonce in store.tasklet(tasklet["id"])["last_output"]
            assert (
                store.codex_session(tasklet["id"])["thread_id"] == session["thread_id"]
            )
            report["conversation_resumed"] = True

            goal_task = store.create_tasklet(
                TaskletCreate(
                    title="Codex native goal verification",
                    prompt="/goal Проверь, что в CONTEXT.txt есть непустое значение. Сначала прочитай файл инструментом, затем отметь цель выполненной и сообщи значение. Не изменяй файлы.",
                ).model_dump()
            )
            await runtime.start([goal_task["id"]])
            await asyncio.wait_for(asyncio.shield(runtime.worker), 120)
            goal_result = store.tasklet(goal_task["id"])
            assert goal_result["status"] == "completed", goal_result["error"]
            assert nonce in goal_result["last_output"]
            inspected = await codex.command(
                goal_task, store.settings(private=True), "/goal"
            )
            assert "выполнена" in inspected, inspected
            report["native_goal_completed"] = True

            slow = []
            for index in range(2):
                prompt = "Сейчас запусти команду python3 -c 'import time; time.sleep(60)' и дождись её окончания. Не меняй файлы. Не заменяй команду более коротким ожиданием."
                if index == 0:
                    prompt = "/goal " + prompt
                slow.append(
                    store.create_tasklet(
                        TaskletCreate(
                            title=f"Stop probe {index}", prompt=prompt
                        ).model_dump()
                    )
                )
            waiting = store.create_tasklet(
                TaskletCreate(
                    title="Must not start", prompt="Ответь NEVER"
                ).model_dump()
            )
            for item in slow:
                store.create_edge(
                    {
                        "source": item["id"],
                        "target": waiting["id"],
                        "pass_context": False,
                    }
                )
            await runtime.start([item["id"] for item in slow] + [waiting["id"]])

            def commands_running():
                return all(
                    item["id"] in codex.running
                    and codex.running[item["id"]][0].running_commands
                    for item in slow
                )

            await wait_for(commands_running)
            active_connections = [codex.running[item["id"]][0] for item in slow]
            result = await asyncio.wait_for(runtime.stop(), 25)
            assert result["status"] == "cancelled"
            assert not codex.running
            for connection in active_connections:
                assert connection.process.returncode is not None
                assert "interrupted" in connection.finished_turns.values(), (
                    connection.finished_turns
                )
            assert store.codex_session(waiting["id"]) is None
            inspected = await codex.command(
                slow[0], store.settings(private=True), "/goal"
            )
            assert "на паузе" in inspected, inspected
            report["two_native_turns_interrupted"] = True
            report["goal_paused_on_stop"] = True
            report["dependent_not_started"] = True
            report["codex_processes_stopped"] = True
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        finally:
            await runtime.stop()
            if codex.auth:
                for tasklet in store.tasklets():
                    session = store.codex_session(tasklet["id"])
                    if session:
                        with contextlib.suppress(Exception):
                            await codex.auth.request(
                                "thread/archive",
                                {"threadId": session["thread_id"]},
                                timeout=5,
                            )
            await codex.close()
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
