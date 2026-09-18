import json

import httpx
import pytest

from aispace.provider import ChatProvider, ProviderError

SETTINGS = {"api_key": "secret-test-key", "base_url": "https://test.example/v1", "model": "model"}


def event(payload):
    return f"data: {json.dumps(payload)}\n\n"


def chunk(content="", finish=None, refusal=None):
    return {
        "choices": [
            {"index": 0, "delta": {"content": content, "refusal": refusal}, "finish_reason": finish}
        ]
    }


async def collect(provider):
    return "".join(
        [
            part
            async for part in provider.stream(
                SETTINGS, "model", [{"role": "user", "content": "Hello"}]
            )
        ]
    )


async def test_actual_request_headers_body_and_successful_sse_parser():
    async def handler(request):
        assert request.url == "https://test.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer secret-test-key"
        assert json.loads(request.content) == {
            "model": "model",
            "stream": True,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        payload = event(chunk("Привет")) + event(chunk(" мир", "stop")) + "data: [DONE]\n\n"
        return httpx.Response(200, text=payload, headers={"content-type": "text/event-stream"})

    assert await collect(ChatProvider(httpx.MockTransport(handler))) == "Привет мир"


@pytest.mark.parametrize(
    "payload,match",
    [
        (event(chunk("Partial")), "прервалось"),
        (
            event(chunk("Partial")) + event(chunk(finish="length")) + "data: [DONE]\n\n",
            "ограничения длины",
        ),
        (event(chunk(finish="content_filter")) + "data: [DONE]\n\n", "фильтра"),
        (event(chunk(refusal="I cannot")) + "data: [DONE]\n\n", "отказалась"),
        ("data: broken\n\n", "некорректный"),
        (event({"error": {"message": "private details"}}), "ошибке во время"),
    ],
)
async def test_incomplete_refused_and_malformed_streams_fail(payload, match):
    provider = ChatProvider(httpx.MockTransport(lambda request: httpx.Response(200, text=payload)))
    with pytest.raises(ProviderError, match=match):
        await collect(provider)


async def test_http_errors_do_not_expose_provider_body_or_key():
    provider = ChatProvider(
        httpx.MockTransport(lambda request: httpx.Response(401, text="secret-test-key"))
    )
    with pytest.raises(ProviderError) as error:
        await collect(provider)
    assert "secret-test-key" not in str(error.value)
    assert "API-ключ" in str(error.value)


async def test_settings_test_only_requests_models():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "model"}]})

    result = await ChatProvider(httpx.MockTransport(handler)).test(SETTINGS)
    assert result["ok"] is True
    assert "Генерация не запускалась" in result["message"]
