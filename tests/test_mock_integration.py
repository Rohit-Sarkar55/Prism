"""Contract test against the real provided mock_provider.py (a subprocess on a free port).

This is the closest thing to the live demo: the adapter meets the actual failure injection.
"""

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from app.providers import ProviderError
from app.providers.openai_compat import OpenAICompatAdapter

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mock_provider.py"
KEY = "mock-key-alpha"
MESSAGES = [{"role": "user", "content": "What is a message queue?"}]

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="scripts/mock_provider.py not found")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mock_url():
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--port", str(port), "--name", "alpha", "--api-key", KEY],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail("mock provider did not start")
    yield base
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(autouse=True)
def healthy_mock(mock_url):
    """Reset failure injection before every test."""
    httpx.post(f"{mock_url}/admin/config", json={"mode": "ok", "fail_rate": 0, "latency_ms": 0})


async def set_mock(base: str, **config) -> None:
    async with httpx.AsyncClient() as client:
        await client.post(f"{base}/admin/config", json=config)


def adapter_for(base: str, *, timeout_s: float = 2.0, key: str = KEY) -> OpenAICompatAdapter:
    return OpenAICompatAdapter("alpha", f"{base}/v1", key, timeout_s=timeout_s)


async def test_chat_against_the_mock(mock_url):
    adapter = adapter_for(mock_url)
    result = await adapter.chat("alpha-small", MESSAGES)
    assert result.usage.prompt_tokens > 0 and result.usage.completion_tokens > 0
    assert result.body["choices"][0]["message"]["content"]
    await adapter.aclose()


async def test_stream_against_the_mock_is_progressive_and_reports_usage(mock_url):
    adapter = adapter_for(mock_url)
    arrival, chunks = [], []
    async for chunk in adapter.stream("alpha-large", MESSAGES):
        arrival.append(time.monotonic())
        chunks.append(chunk)
    assert len(chunks) >= 3
    assert chunks[-1].usage is not None and chunks[-1].usage.completion_tokens > 0
    assert arrival[-1] - arrival[0] > 0.05  # the mock paces tokens, so arrival is spread out
    await adapter.aclose()


async def test_provider_down_is_a_transient_server_error(mock_url):
    await set_mock(mock_url, mode="down")
    adapter = adapter_for(mock_url)
    with pytest.raises(ProviderError) as exc:
        await adapter.chat("alpha-small", MESSAGES)
    assert (exc.value.kind, exc.value.status, exc.value.transient) == ("server_error", 503, True)
    await adapter.aclose()


async def test_rate_limited_provider_reports_retry_after(mock_url):
    await set_mock(mock_url, mode="rate_limited")
    adapter = adapter_for(mock_url)
    with pytest.raises(ProviderError) as exc:
        await adapter.chat("alpha-small", MESSAGES)
    assert (exc.value.kind, exc.value.retry_after_s) == ("rate_limited", 5.0)
    await adapter.aclose()


async def test_slow_provider_times_out_instead_of_hanging(mock_url):
    await set_mock(mock_url, latency_ms=2000)
    adapter = adapter_for(mock_url, timeout_s=0.4)
    started = time.monotonic()
    with pytest.raises(ProviderError) as exc:
        await adapter.chat("alpha-small", MESSAGES)
    assert exc.value.kind == "timeout"
    assert time.monotonic() - started < 1.5  # gave up near the timeout, not after 2s
    await adapter.aclose()


async def test_dead_provider_is_a_connect_error():
    adapter = adapter_for(f"http://127.0.0.1:{free_port()}", timeout_s=0.5)  # nothing listens there
    with pytest.raises(ProviderError) as exc:
        await adapter.chat("alpha-small", MESSAGES)
    assert (exc.value.kind, exc.value.transient) == ("connect", True)
    await adapter.aclose()


async def test_unknown_model_and_bad_key_are_permanent_client_errors(mock_url):
    adapter = adapter_for(mock_url)
    with pytest.raises(ProviderError) as unknown:
        await adapter.chat("alpha-gigantic", MESSAGES)
    assert (unknown.value.kind, unknown.value.status, unknown.value.transient) == ("client_error", 404, False)

    bad_key = adapter_for(mock_url, key="wrong-key")
    with pytest.raises(ProviderError) as unauthorized:
        await bad_key.chat("alpha-small", MESSAGES)
    assert unauthorized.value.status == 401 and "wrong-key" not in str(unauthorized.value)
    await adapter.aclose()
    await bad_key.aclose()