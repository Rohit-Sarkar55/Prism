import pytest

from app.providers import MidStreamError, ProviderError
from app.providers.fake import FakeAdapter

MESSAGES = [{"role": "user", "content": "hi"}]


async def test_fake_chat_reports_usage_and_records_calls():
    fake = FakeAdapter("alpha", reply="one two three", prompt_tokens=4)
    result = await fake.chat("alpha-small", MESSAGES)
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (4, 3)
    assert fake.calls == [("chat", "alpha-small")]


async def test_fake_fails_the_scripted_number_of_times_then_recovers():
    fake = FakeAdapter("alpha", fail_times=2)
    for _ in range(2):
        with pytest.raises(ProviderError) as exc:
            await fake.chat("alpha-small", MESSAGES)
        assert exc.value.transient
    assert (await fake.chat("alpha-small", MESSAGES)).provider == "alpha"


async def test_fake_stream_ends_with_usage_chunk():
    fake = FakeAdapter("alpha", reply="a b c")
    chunks = [c async for c in fake.stream("alpha-small", MESSAGES)]
    assert len(chunks) == 4 and chunks[-1].usage == fake.usage


async def test_fake_stream_can_die_midway():
    fake = FakeAdapter("alpha", reply="a b c d", fail_stream_after=2)
    got = []
    with pytest.raises(MidStreamError):
        async for chunk in fake.stream("alpha-small", MESSAGES):
            got.append(chunk)
    assert len(got) == 2