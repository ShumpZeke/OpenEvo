"""
A model that answers in a reasoning channel and leaves `content` empty.

Measured on a local Qwen3.5-27B through Ollama's OpenAI-compatible endpoint:

    request   max_tokens 400
    response  content   ""
              reasoning "The user wants me to reply with..."
              usage     completion_tokens 16
    broker    reasoning_tokens: 0

Three things were wrong at once, and the third is why the first two mattered.

**It is not truncation.** A 400-token budget finished after 16. The model chose
to think and then stop, so the escalation path for `finish_reason=length` --
which exists precisely for reasoning models -- never fires and would not help if
it did.

**The broker could not see it.** `reasoning_tokens` read only OpenAI's
`usage.completion_tokens_details.reasoning_tokens`. Ollama returns the text in
`message.reasoning` and no count, so the one number that would have explained an
empty answer reported zero. An operator looking at that log sees a model that
returned nothing, having apparently done nothing.

**There was no way to switch it off.** `reasoning_effort: "none"` fixes it --
same request returned "READY", and latency fell from 8031 ms to 1695 ms because
the discarded thinking block was never generated -- but the adapter had no way
for a provider to add a parameter to its own requests.
"""

import pytest

from oe_max.providers.base import ChatResult, Outcome


def _result(message: dict, usage: dict | None = None) -> ChatResult:
    return ChatResult(
        Outcome.OK, "ollama", "qwen-evo-text:latest", 1000.0,
        status_code=200,
        body={"choices": [{"message": message, "finish_reason": "stop"}],
              "usage": usage or {}},
    )


class TestSeeingTheReasoning:
    def test_reasoning_text_is_found_where_ollama_puts_it(self):
        r = _result({"role": "assistant", "content": "", "reasoning": "thinking..."})

        assert r.reasoning_text == "thinking..."

    @pytest.mark.parametrize("field", ["reasoning", "reasoning_content", "thinking"])
    def test_the_field_name_varies_by_provider(self, field):
        """
        Three spellings are in the wild for the same idea. Reading only one of
        them is how this went unnoticed.
        """
        r = _result({"role": "assistant", "content": "", field: "thought"})

        assert r.reasoning_text == "thought"

    def test_a_reported_token_count_is_preferred_over_an_estimate(self):
        """A provider that counts is more trustworthy than our arithmetic."""
        r = _result(
            {"role": "assistant", "content": "hi", "reasoning": "x" * 400},
            {"completion_tokens_details": {"reasoning_tokens": 37}},
        )

        assert r.reasoning_tokens == 37
        assert r.reasoning_tokens_are_estimated is False

    def test_an_uncounted_reasoning_block_still_reports_non_zero(self):
        """
        The actual defect. Zero was indistinguishable from "no thinking
        happened", which is the opposite of what occurred.
        """
        r = _result({"role": "assistant", "content": "", "reasoning": "x" * 400})

        assert r.reasoning_tokens > 0
        assert r.reasoning_tokens_are_estimated is True

    def test_no_reasoning_is_still_zero(self):
        r = _result({"role": "assistant", "content": "the answer"})

        assert r.reasoning_tokens == 0
        assert r.reasoning_tokens_are_estimated is False


class TestTheEmptyAnswer:
    def test_empty_content_with_reasoning_is_named(self):
        """
        Not a transport failure, not a refusal, not truncation: the request
        succeeded and the visible answer is empty. It needs its own name or it
        gets diagnosed as one of the other three.
        """
        r = _result({"role": "assistant", "content": "", "reasoning": "thought"})

        assert r.answered_only_in_reasoning is True

    def test_whitespace_only_content_counts_as_empty(self):
        r = _result({"role": "assistant", "content": "  \n ", "reasoning": "thought"})

        assert r.answered_only_in_reasoning is True

    def test_a_real_answer_alongside_reasoning_is_not_the_failure(self):
        """
        Thinking is not itself a problem. A model that thinks *and* answers has
        done nothing wrong, and flagging it would make the signal useless.
        """
        r = _result({"role": "assistant", "content": "READY", "reasoning": "thought"})

        assert r.answered_only_in_reasoning is False

    def test_an_empty_answer_with_no_reasoning_is_a_different_fault(self):
        """
        Also broken, but for another reason -- and pointing at the reasoning
        channel would send someone the wrong way.
        """
        r = _result({"role": "assistant", "content": ""})

        assert r.answered_only_in_reasoning is False

    def test_the_log_carries_all_three_facts(self):
        """
        The log is what an operator reads at 2am. It has to say the answer was
        empty, that thinking happened, and that the count is an estimate.
        """
        log = _result({"role": "assistant", "content": "",
                       "reasoning": "x" * 200}).to_log()

        assert log["answered_only_in_reasoning"] is True
        assert log["reasoning_tokens"] > 0
        assert log["reasoning_tokens_estimated"] is True


class TestSwitchingItOff:
    def test_local_providers_ask_for_no_reasoning_by_default(self, monkeypatch):
        monkeypatch.delenv("OE_MAX_LOCAL_REASONING", raising=False)
        from oe_max.providers.local import build_local_providers

        for name, adapter in build_local_providers().items():
            assert getattr(adapter, "extra_body", {}).get("reasoning_effort") == "none", name

    def test_the_default_can_be_raised_for_roles_that_want_thinking(self, monkeypatch):
        """
        A judge sometimes should think. A diff generator that thinks instead of
        answering produces nothing to apply, which is why the default is off
        rather than absent.
        """
        monkeypatch.setenv("OE_MAX_LOCAL_REASONING", "medium")
        from oe_max.providers.local import build_local_providers

        adapter = build_local_providers()["ollama"]

        assert adapter.extra_body["reasoning_effort"] == "medium"

    def test_default_means_send_nothing(self, monkeypatch):
        """
        An explicit way to let the server decide, for a provider that rejects
        the parameter rather than ignoring it.
        """
        monkeypatch.setenv("OE_MAX_LOCAL_REASONING", "default")
        from oe_max.providers.local import build_local_providers

        adapter = build_local_providers()["ollama"]

        assert not getattr(adapter, "extra_body", None)

    def test_a_per_request_parameter_still_wins(self):
        """
        `extra_body` is a provider-level default, not an override. A caller that
        names a value means it.
        """
        import inspect

        from oe_max.providers import base

        source = inspect.getsource(base.ProviderAdapter.chat)
        extra = source.index("extra_body")
        params = source.index('params.items()')

        assert extra < params, (
            "per-request params must be applied after extra_body, or a caller "
            "cannot override a provider default")
