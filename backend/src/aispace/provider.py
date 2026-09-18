import json

import httpx


class ProviderError(Exception):
    pass


class ChatProvider:
    def __init__(self, transport=None):
        self.transport = transport

    def client(self, settings):
        return httpx.AsyncClient(
            transport=self.transport,
            timeout=httpx.Timeout(120, connect=15),
            headers={"Authorization": f"Bearer {settings['api_key']}"},
        )

    @staticmethod
    def error_message(status):
        if status in {401, 403}:
            return "Провайдер отклонил API-ключ или доступ к модели"
        if status == 429:
            return "Превышен лимит запросов или баланс API"
        if status == 404:
            return "Проверьте адрес API и название модели: ресурс не найден"
        return f"Провайдер вернул ошибку HTTP {status}"

    async def test(self, settings):
        try:
            async with self.client(settings) as client:
                response = await client.get(f"{settings['base_url']}/models")
                if response.status_code >= 400:
                    return {"ok": False, "message": self.error_message(response.status_code)}
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    return {"ok": False, "message": "Адрес не вернул совместимый список моделей"}
                if settings["model"] and not any(
                    model.get("id") == settings["model"]
                    for model in payload["data"]
                    if isinstance(model, dict)
                ):
                    return {
                        "ok": True,
                        "message": "Подключение работает. Выбранная модель отсутствует в списке провайдера; её доступность не проверена.",
                    }
                return {"ok": True, "message": "Подключение работает. Генерация не запускалась."}
        except (httpx.HTTPError, ValueError):
            return {
                "ok": False,
                "message": "Не удалось получить список моделей. Проверьте адрес API и соединение.",
            }

    async def stream(self, settings, model, messages):
        try:
            async with (
                self.client(settings) as client,
                client.stream(
                    "POST",
                    f"{settings['base_url']}/chat/completions",
                    json={"model": model, "messages": messages, "stream": True},
                ) as response,
            ):
                if response.status_code >= 400:
                    raise ProviderError(self.error_message(response.status_code))
                finished = False
                event_lines = []
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        event_lines.append(line[5:].lstrip())
                    elif not line and event_lines:
                        data = "\n".join(event_lines)
                        event_lines.clear()
                        if data == "[DONE]":
                            finished = True
                            break
                        try:
                            chunk = json.loads(data)
                            if chunk.get("error"):
                                raise ProviderError(
                                    "Провайдер сообщил об ошибке во время генерации"
                                )
                            for choice in chunk.get("choices", []):
                                if choice.get("index", 0) != 0:
                                    continue
                                reason = choice.get("finish_reason")
                                if reason == "length":
                                    raise ProviderError(
                                        "Ответ достиг ограничения длины и не завершён. Уточните или сократите задачу."
                                    )
                                if reason == "content_filter":
                                    raise ProviderError(
                                        "Провайдер остановил ответ из-за фильтра содержимого"
                                    )
                                if reason not in {None, "stop"}:
                                    raise ProviderError(
                                        "Модель запросила неподдерживаемое действие вместо текстового ответа"
                                    )
                                delta = choice.get("delta", {})
                                if delta.get("refusal"):
                                    raise ProviderError(
                                        "Модель отказалась выполнять задачу. Измените промпт."
                                    )
                                content = delta.get("content")
                                if content:
                                    if not isinstance(content, str):
                                        raise ValueError("Invalid content")
                                    yield content
                        except (ValueError, TypeError, AttributeError) as error:
                            raise ProviderError(
                                "Провайдер вернул некорректный поток ответа"
                            ) from error
                if not finished:
                    raise ProviderError(
                        "Соединение прервалось до завершения ответа. Частичный ответ сохранён."
                    )
        except httpx.TimeoutException as error:
            raise ProviderError(
                "Провайдер не ответил вовремя. Попробуйте запустить задачу снова."
            ) from error
        except httpx.HTTPError as error:
            raise ProviderError(
                "Ошибка соединения с провайдером. Проверьте адрес API и подключение."
            ) from error
