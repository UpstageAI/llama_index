import json
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Union,
    cast,
    get_args,
    runtime_checkable,
)

import httpx
import tiktoken
from llama_index.core.base.llms.generic_utils import (
    achat_to_completion_decorator,
    acompletion_to_chat_decorator,
    astream_chat_to_completion_decorator,
    astream_completion_to_chat_decorator,
    chat_to_completion_decorator,
    completion_to_chat_decorator,
    stream_chat_to_completion_decorator,
    stream_completion_to_chat_decorator,
)
from llama_index.core.base.llms.types import (
    ChatMessage,
    ChatResponse,
    ChatResponseAsyncGen,
    ChatResponseGen,
    CompletionResponse,
    CompletionResponseAsyncGen,
    CompletionResponseGen,
    LLMMetadata,
    MessageRole,
)
from llama_index.core.bridge.pydantic import Field, PrivateAttr
from llama_index.core.callbacks import CallbackManager
from llama_index.core.constants import (
    DEFAULT_TEMPERATURE,
)
from llama_index.core.llms.callbacks import (
    llm_chat_callback,
    llm_completion_callback,
)
from llama_index.core.llms.function_calling import FunctionCallingLLM
from llama_index.core.llms.llm import ToolSelection
from llama_index.core.types import BaseOutputParser, PydanticProgramMode
from llama_index.llms.openai.utils import (
    OpenAIToolCall,
    create_retry_decorator,
    from_openai_completion_logprobs,
    from_openai_message,
    from_openai_token_logprobs,
    is_chat_model,
    is_function_calling_model,
    openai_modelname_to_contextsize,
    resolve_openai_credentials,
    to_openai_message_dicts,
)

from openai import AsyncOpenAI, AzureOpenAI
from openai import OpenAI as SyncOpenAI
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    ChoiceDelta,
    ChoiceDeltaToolCall,
)

if TYPE_CHECKING:
    from llama_index.core.chat_engine.types import AgentChatResponse
    from llama_index.core.tools.types import BaseTool

DEFAULT_OPENAI_MODEL = "gpt-3.5-turbo"

llm_retry_decorator = create_retry_decorator(
    max_retries=6,
    random_exponential=True,
    stop_after_delay_seconds=60,
    min_seconds=1,
    max_seconds=20,
)


@runtime_checkable
class Tokenizer(Protocol):
    """Tokenizers support an encode function that returns a list of ints."""

    def encode(self, text: str) -> List[int]:
        ...


def force_single_tool_call(response: ChatResponse) -> None:
    tool_calls = response.message.additional_kwargs.get("tool_calls", [])
    if len(tool_calls) > 1:
        response.message.additional_kwargs["tool_calls"] = [tool_calls[0]]


class OpenAI(FunctionCallingLLM):
    """
    OpenAI LLM.

    Examples:
        `pip install llama-index-llms-openai`

        ```python
        import os
        import openai

        os.environ["OPENAI_API_KEY"] = "sk-..."
        openai.api_key = os.environ["OPENAI_API_KEY"]

        from llama_index.llms.openai import OpenAI

        llm = OpenAI(model="gpt-3.5-turbo")

        stream = llm.stream("Hi, write a short story")

        for r in stream:
            print(r.delta, end="")
        ```
    """

    model: str = Field(
        default=DEFAULT_OPENAI_MODEL, description="The OpenAI model to use."
    )
    temperature: float = Field(
        default=DEFAULT_TEMPERATURE,
        description="The temperature to use during generation.",
        gte=0.0,
        lte=1.0,
    )
    max_tokens: Optional[int] = Field(
        description="The maximum number of tokens to generate.",
        gt=0,
    )
    logprobs: Optional[bool] = Field(
        description="Whether to return logprobs per token."
    )
    top_logprobs: int = Field(
        description="The number of top token log probs to return.",
        default=0,
        gte=0,
        lte=20,
    )
    additional_kwargs: Dict[str, Any] = Field(
        default_factory=dict, description="Additional kwargs for the OpenAI API."
    )
    max_retries: int = Field(
        default=3,
        description="The maximum number of API retries.",
        gte=0,
    )
    timeout: float = Field(
        default=60.0,
        description="The timeout, in seconds, for API requests.",
        gte=0,
    )
    default_headers: Dict[str, str] = Field(
        default=None, description="The default headers for API requests."
    )
    reuse_client: bool = Field(
        default=True,
        description=(
            "Reuse the OpenAI client between requests. When doing anything with large "
            "volumes of async API calls, setting this to false can improve stability."
        ),
    )

    api_key: str = Field(default=None, description="The OpenAI API key.")
    api_base: str = Field(description="The base URL for OpenAI API.")
    api_version: str = Field(description="The API version for OpenAI API.")

    _client: Optional[SyncOpenAI] = PrivateAttr()
    _aclient: Optional[AsyncOpenAI] = PrivateAttr()
    _http_client: Optional[httpx.Client] = PrivateAttr()

    def __init__(
        self,
        model: str = DEFAULT_OPENAI_MODEL,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: Optional[int] = None,
        additional_kwargs: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        timeout: float = 60.0,
        reuse_client: bool = True,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        api_version: Optional[str] = None,
        callback_manager: Optional[CallbackManager] = None,
        default_headers: Optional[Dict[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
        # base class
        system_prompt: Optional[str] = None,
        messages_to_prompt: Optional[Callable[[Sequence[ChatMessage]], str]] = None,
        completion_to_prompt: Optional[Callable[[str], str]] = None,
        pydantic_program_mode: PydanticProgramMode = PydanticProgramMode.DEFAULT,
        output_parser: Optional[BaseOutputParser] = None,
        **kwargs: Any,
    ) -> None:
        additional_kwargs = additional_kwargs or {}

        api_key, api_base, api_version = resolve_openai_credentials(
            api_key=api_key,
            api_base=api_base,
            api_version=api_version,
        )

        super().__init__(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            additional_kwargs=additional_kwargs,
            max_retries=max_retries,
            callback_manager=callback_manager,
            api_key=api_key,
            api_version=api_version,
            api_base=api_base,
            timeout=timeout,
            reuse_client=reuse_client,
            default_headers=default_headers,
            system_prompt=system_prompt,
            messages_to_prompt=messages_to_prompt,
            completion_to_prompt=completion_to_prompt,
            pydantic_program_mode=pydantic_program_mode,
            output_parser=output_parser,
            **kwargs,
        )

        self._client = None
        self._aclient = None
        self._http_client = http_client

    def _get_client(self) -> SyncOpenAI:
        if not self.reuse_client:
            return SyncOpenAI(**self._get_credential_kwargs())

        if self._client is None:
            self._client = SyncOpenAI(**self._get_credential_kwargs())
        return self._client

    def _get_aclient(self) -> AsyncOpenAI:
        if not self.reuse_client:
            return AsyncOpenAI(**self._get_credential_kwargs())

        if self._aclient is None:
            self._aclient = AsyncOpenAI(**self._get_credential_kwargs())
        return self._aclient

    def _get_model_name(self) -> str:
        model_name = self.model
        if "ft-" in model_name:  # legacy fine-tuning
            model_name = model_name.split(":")[0]
        elif model_name.startswith("ft:"):
            model_name = model_name.split(":")[1]
        return model_name

    def _is_azure_client(self) -> bool:
        return isinstance(self._get_client(), AzureOpenAI)

    @classmethod
    def class_name(cls) -> str:
        return "openai_llm"

    @property
    def _tokenizer(self) -> Optional[Tokenizer]:
        """
        Get a tokenizer for this model, or None if a tokenizing method is unknown.

        OpenAI can do this using the tiktoken package, subclasses may not have
        this convenience.
        """
        return tiktoken.encoding_for_model(self._get_model_name())

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(
            context_window=openai_modelname_to_contextsize(self._get_model_name()),
            num_output=self.max_tokens or -1,
            is_chat_model=is_chat_model(model=self._get_model_name()),
            is_function_calling_model=is_function_calling_model(
                model=self._get_model_name()
            ),
            model_name=self.model,
        )

    @llm_chat_callback()
    def chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        if self._use_chat_completions(kwargs):
            chat_fn = self._chat
        else:
            chat_fn = completion_to_chat_decorator(self._complete)
        return chat_fn(messages, **kwargs)

    @llm_chat_callback()
    def stream_chat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponseGen:
        if self._use_chat_completions(kwargs):
            stream_chat_fn = self._stream_chat
        else:
            stream_chat_fn = stream_completion_to_chat_decorator(self._stream_complete)
        return stream_chat_fn(messages, **kwargs)

    @llm_completion_callback()
    def complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponse:
        if self._use_chat_completions(kwargs):
            complete_fn = chat_to_completion_decorator(self._chat)
        else:
            complete_fn = self._complete
        return complete_fn(prompt, **kwargs)

    @llm_completion_callback()
    def stream_complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponseGen:
        if self._use_chat_completions(kwargs):
            stream_complete_fn = stream_chat_to_completion_decorator(self._stream_chat)
        else:
            stream_complete_fn = self._stream_complete
        return stream_complete_fn(prompt, **kwargs)

    def _use_chat_completions(self, kwargs: Dict[str, Any]) -> bool:
        if "use_chat_completions" in kwargs:
            return kwargs["use_chat_completions"]
        return self.metadata.is_chat_model

    def _get_credential_kwargs(self) -> Dict[str, Any]:
        return {
            "api_key": self.api_key,
            "base_url": self.api_base,
            "max_retries": self.max_retries,
            "timeout": self.timeout,
            "default_headers": self.default_headers,
            "http_client": self._http_client,
        }

    def _get_model_kwargs(self, **kwargs: Any) -> Dict[str, Any]:
        base_kwargs = {"model": self.model, "temperature": self.temperature, **kwargs}
        if self.max_tokens is not None:
            # If max_tokens is None, don't include in the payload:
            # https://platform.openai.com/docs/api-reference/chat
            # https://platform.openai.com/docs/api-reference/completions
            base_kwargs["max_tokens"] = self.max_tokens
        if self.logprobs is not None and self.logprobs is True:
            if self.metadata.is_chat_model:
                base_kwargs["logprobs"] = self.logprobs
                base_kwargs["top_logprobs"] = self.top_logprobs
            else:
                base_kwargs["logprobs"] = self.top_logprobs  # int in this case
        return {**base_kwargs, **self.additional_kwargs}

    @llm_retry_decorator
    def _chat(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResponse:
        client = self._get_client()
        message_dicts = to_openai_message_dicts(messages)

        if self.reuse_client:
            response = client.chat.completions.create(
                messages=message_dicts,
                stream=False,
                **self._get_model_kwargs(**kwargs),
            )
        else:
            with client:
                response = client.chat.completions.create(
                    messages=message_dicts,
                    stream=False,
                    **self._get_model_kwargs(**kwargs),
                )

        openai_message = response.choices[0].message
        message = from_openai_message(openai_message)
        openai_token_logprobs = response.choices[0].logprobs
        logprobs = None
        if openai_token_logprobs and openai_token_logprobs.content:
            logprobs = from_openai_token_logprobs(openai_token_logprobs.content)

        return ChatResponse(
            message=message,
            raw=response,
            logprobs=logprobs,
            additional_kwargs=self._get_response_token_counts(response),
        )

    def _update_tool_calls(
        self,
        tool_calls: List[ChoiceDeltaToolCall],
        tool_calls_delta: Optional[List[ChoiceDeltaToolCall]],
    ) -> List[ChoiceDeltaToolCall]:
        """
        Use the tool_calls_delta objects received from openai stream chunks
        to update the running tool_calls object.

        Args:
            tool_calls (List[ChoiceDeltaToolCall]): the list of tool calls
            tool_calls_delta (ChoiceDeltaToolCall): the delta to update tool_calls

        Returns:
            List[ChoiceDeltaToolCall]: the updated tool calls
        """
        # openai provides chunks consisting of tool_call deltas one tool at a time
        if tool_calls_delta is None:
            return tool_calls

        tc_delta = tool_calls_delta[0]

        if len(tool_calls) == 0:
            tool_calls.append(tc_delta)
        else:
            # we need to either update latest tool_call or start a
            # new tool_call (i.e., multiple tools in this turn) and
            # accumulate that new tool_call with future delta chunks
            t = tool_calls[-1]
            if t.index != tc_delta.index:
                # the start of a new tool call, so append to our running tool_calls list
                tool_calls.append(tc_delta)
            else:
                # not the start of a new tool call, so update last item of tool_calls

                # validations to get passed by mypy
                assert t.function is not None
                assert tc_delta.function is not None
                assert t.function.arguments is not None
                assert t.function.name is not None
                assert t.id is not None

                t.function.arguments += tc_delta.function.arguments or ""
                t.function.name += tc_delta.function.name or ""
                t.id += tc_delta.id or ""
        return tool_calls

    @llm_retry_decorator
    def _stream_chat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponseGen:
        client = self._get_client()
        message_dicts = to_openai_message_dicts(messages)

        def gen() -> ChatResponseGen:
            content = ""
            tool_calls: List[ChoiceDeltaToolCall] = []

            is_function = False
            for response in client.chat.completions.create(
                messages=message_dicts,
                stream=True,
                **self._get_model_kwargs(**kwargs),
            ):
                response = cast(ChatCompletionChunk, response)
                if len(response.choices) > 0:
                    delta = response.choices[0].delta
                else:
                    if self._is_azure_client():
                        continue
                    else:
                        delta = ChoiceDelta()

                # check if this chunk is the start of a function call
                if delta.tool_calls:
                    is_function = True

                # update using deltas
                role = delta.role or MessageRole.ASSISTANT
                content_delta = delta.content or ""
                content += content_delta

                additional_kwargs = {}
                if is_function:
                    tool_calls = self._update_tool_calls(tool_calls, delta.tool_calls)
                    additional_kwargs["tool_calls"] = tool_calls

                yield ChatResponse(
                    message=ChatMessage(
                        role=role,
                        content=content,
                        additional_kwargs=additional_kwargs,
                    ),
                    delta=content_delta,
                    raw=response,
                    additional_kwargs=self._get_response_token_counts(response),
                )

        return gen()

    @llm_retry_decorator
    def _complete(self, prompt: str, **kwargs: Any) -> CompletionResponse:
        client = self._get_client()
        all_kwargs = self._get_model_kwargs(**kwargs)
        self._update_max_tokens(all_kwargs, prompt)

        if self.reuse_client:
            response = client.completions.create(
                prompt=prompt,
                stream=False,
                **all_kwargs,
            )
        else:
            with client:
                response = client.completions.create(
                    prompt=prompt,
                    stream=False,
                    **all_kwargs,
                )
        text = response.choices[0].text

        openai_completion_logprobs = response.choices[0].logprobs
        logprobs = None
        if openai_completion_logprobs:
            logprobs = from_openai_completion_logprobs(openai_completion_logprobs)

        return CompletionResponse(
            text=text,
            raw=response,
            logprobs=logprobs,
            additional_kwargs=self._get_response_token_counts(response),
        )

    @llm_retry_decorator
    def _stream_complete(self, prompt: str, **kwargs: Any) -> CompletionResponseGen:
        client = self._get_client()
        all_kwargs = self._get_model_kwargs(**kwargs)
        self._update_max_tokens(all_kwargs, prompt)

        def gen() -> CompletionResponseGen:
            text = ""
            for response in client.completions.create(
                prompt=prompt,
                stream=True,
                **all_kwargs,
            ):
                if len(response.choices) > 0:
                    delta = response.choices[0].text
                else:
                    delta = ""
                text += delta
                yield CompletionResponse(
                    delta=delta,
                    text=text,
                    raw=response,
                    additional_kwargs=self._get_response_token_counts(response),
                )

        return gen()

    def _update_max_tokens(self, all_kwargs: Dict[str, Any], prompt: str) -> None:
        """Infer max_tokens for the payload, if possible."""
        if self.max_tokens is not None or self._tokenizer is None:
            return
        # NOTE: non-chat completion endpoint requires max_tokens to be set
        num_tokens = len(self._tokenizer.encode(prompt))
        max_tokens = self.metadata.context_window - num_tokens
        if max_tokens <= 0:
            raise ValueError(
                f"The prompt has {num_tokens} tokens, which is too long for"
                " the model. Please use a prompt that fits within"
                f" {self.metadata.context_window} tokens."
            )
        all_kwargs["max_tokens"] = max_tokens

    def _get_response_token_counts(self, raw_response: Any) -> dict:
        """Get the token usage reported by the response."""
        if not isinstance(raw_response, dict):
            return {}

        usage = raw_response.get("usage", {})
        # NOTE: other model providers that use the OpenAI client may not report usage
        if usage is None:
            return {}

        return {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }

    # ===== Async Endpoints =====
    @llm_chat_callback()
    async def achat(
        self,
        messages: Sequence[ChatMessage],
        **kwargs: Any,
    ) -> ChatResponse:
        achat_fn: Callable[..., Awaitable[ChatResponse]]
        if self._use_chat_completions(kwargs):
            achat_fn = self._achat
        else:
            achat_fn = acompletion_to_chat_decorator(self._acomplete)
        return await achat_fn(messages, **kwargs)

    @llm_chat_callback()
    async def astream_chat(
        self,
        messages: Sequence[ChatMessage],
        **kwargs: Any,
    ) -> ChatResponseAsyncGen:
        astream_chat_fn: Callable[..., Awaitable[ChatResponseAsyncGen]]
        if self._use_chat_completions(kwargs):
            astream_chat_fn = self._astream_chat
        else:
            astream_chat_fn = astream_completion_to_chat_decorator(
                self._astream_complete
            )
        return await astream_chat_fn(messages, **kwargs)

    @llm_completion_callback()
    async def acomplete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponse:
        if self._use_chat_completions(kwargs):
            acomplete_fn = achat_to_completion_decorator(self._achat)
        else:
            acomplete_fn = self._acomplete
        return await acomplete_fn(prompt, **kwargs)

    @llm_completion_callback()
    async def astream_complete(
        self, prompt: str, formatted: bool = False, **kwargs: Any
    ) -> CompletionResponseAsyncGen:
        if self._use_chat_completions(kwargs):
            astream_complete_fn = astream_chat_to_completion_decorator(
                self._astream_chat
            )
        else:
            astream_complete_fn = self._astream_complete
        return await astream_complete_fn(prompt, **kwargs)

    @llm_retry_decorator
    async def _achat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponse:
        aclient = self._get_aclient()
        message_dicts = to_openai_message_dicts(messages)

        if self.reuse_client:
            response = await aclient.chat.completions.create(
                messages=message_dicts, stream=False, **self._get_model_kwargs(**kwargs)
            )
        else:
            async with aclient:
                response = await aclient.chat.completions.create(
                    messages=message_dicts,
                    stream=False,
                    **self._get_model_kwargs(**kwargs),
                )

        openai_message = response.choices[0].message
        message = from_openai_message(openai_message)
        openai_token_logprobs = response.choices[0].logprobs
        logprobs = None
        if openai_token_logprobs and openai_token_logprobs.content:
            logprobs = from_openai_token_logprobs(openai_token_logprobs.content)

        return ChatResponse(
            message=message,
            raw=response,
            logprobs=logprobs,
            additional_kwargs=self._get_response_token_counts(response),
        )

    @llm_retry_decorator
    async def _astream_chat(
        self, messages: Sequence[ChatMessage], **kwargs: Any
    ) -> ChatResponseAsyncGen:
        aclient = self._get_aclient()
        message_dicts = to_openai_message_dicts(messages)

        async def gen() -> ChatResponseAsyncGen:
            content = ""
            tool_calls: List[ChoiceDeltaToolCall] = []

            is_function = False
            first_chat_chunk = True
            async for response in await aclient.chat.completions.create(
                messages=message_dicts,
                stream=True,
                **self._get_model_kwargs(**kwargs),
            ):
                response = cast(ChatCompletionChunk, response)
                if len(response.choices) > 0:
                    # check if the first chunk has neither content nor tool_calls
                    # this happens when 1106 models end up calling multiple tools
                    if (
                        first_chat_chunk
                        and response.choices[0].delta.content is None
                        and response.choices[0].delta.tool_calls is None
                    ):
                        first_chat_chunk = False
                        continue
                    delta = response.choices[0].delta
                else:
                    if self._is_azure_client():
                        continue
                    else:
                        delta = ChoiceDelta()
                first_chat_chunk = False

                # check if this chunk is the start of a function call
                if delta.tool_calls:
                    is_function = True

                # update using deltas
                role = delta.role or MessageRole.ASSISTANT
                content_delta = delta.content or ""
                content += content_delta

                additional_kwargs = {}
                if is_function:
                    tool_calls = self._update_tool_calls(tool_calls, delta.tool_calls)
                    additional_kwargs["tool_calls"] = tool_calls

                yield ChatResponse(
                    message=ChatMessage(
                        role=role,
                        content=content,
                        additional_kwargs=additional_kwargs,
                    ),
                    delta=content_delta,
                    raw=response,
                    additional_kwargs=self._get_response_token_counts(response),
                )

        return gen()

    @llm_retry_decorator
    async def _acomplete(self, prompt: str, **kwargs: Any) -> CompletionResponse:
        aclient = self._get_aclient()
        all_kwargs = self._get_model_kwargs(**kwargs)
        self._update_max_tokens(all_kwargs, prompt)

        if self.reuse_client:
            response = await aclient.completions.create(
                prompt=prompt,
                stream=False,
                **all_kwargs,
            )
        else:
            async with aclient:
                response = await aclient.completions.create(
                    prompt=prompt,
                    stream=False,
                    **all_kwargs,
                )

        text = response.choices[0].text
        openai_completion_logprobs = response.choices[0].logprobs
        logprobs = None
        if openai_completion_logprobs:
            logprobs = from_openai_completion_logprobs(openai_completion_logprobs)

        return CompletionResponse(
            text=text,
            raw=response,
            logprobs=logprobs,
            additional_kwargs=self._get_response_token_counts(response),
        )

    @llm_retry_decorator
    async def _astream_complete(
        self, prompt: str, **kwargs: Any
    ) -> CompletionResponseAsyncGen:
        aclient = self._get_aclient()
        all_kwargs = self._get_model_kwargs(**kwargs)
        self._update_max_tokens(all_kwargs, prompt)

        async def gen() -> CompletionResponseAsyncGen:
            text = ""
            async for response in await aclient.completions.create(
                prompt=prompt,
                stream=True,
                **all_kwargs,
            ):
                if len(response.choices) > 0:
                    delta = response.choices[0].text
                else:
                    delta = ""
                text += delta
                yield CompletionResponse(
                    delta=delta,
                    text=text,
                    raw=response,
                    additional_kwargs=self._get_response_token_counts(response),
                )

        return gen()

    def chat_with_tools(
        self,
        tools: List["BaseTool"],
        user_msg: Optional[Union[str, ChatMessage]] = None,
        chat_history: Optional[List[ChatMessage]] = None,
        verbose: bool = False,
        allow_parallel_tool_calls: bool = False,
        tool_choice: Union[str, dict] = "auto",
        **kwargs: Any,
    ) -> ChatResponse:
        """Predict and call the tool."""
        from llama_index.agent.openai.utils import resolve_tool_choice

        # misralai uses the same openai tool format
        tool_specs = [tool.metadata.to_openai_tool() for tool in tools]

        if isinstance(user_msg, str):
            user_msg = ChatMessage(role=MessageRole.USER, content=user_msg)

        messages = chat_history or []
        if user_msg:
            messages.append(user_msg)

        response = self.chat(
            messages,
            tools=tool_specs,
            tool_choice=resolve_tool_choice(tool_choice),
            **kwargs,
        )
        if not allow_parallel_tool_calls:
            force_single_tool_call(response)
        return response

    async def achat_with_tools(
        self,
        tools: List["BaseTool"],
        user_msg: Optional[Union[str, ChatMessage]] = None,
        chat_history: Optional[List[ChatMessage]] = None,
        verbose: bool = False,
        allow_parallel_tool_calls: bool = False,
        tool_choice: Union[str, dict] = "auto",
        **kwargs: Any,
    ) -> ChatResponse:
        """Predict and call the tool."""
        from llama_index.agent.openai.utils import resolve_tool_choice

        # misralai uses the same openai tool format
        tool_specs = [tool.metadata.to_openai_tool() for tool in tools]

        if isinstance(user_msg, str):
            user_msg = ChatMessage(role=MessageRole.USER, content=user_msg)

        messages = chat_history or []
        if user_msg:
            messages.append(user_msg)

        response = await self.achat(
            messages,
            tools=tool_specs,
            tool_choice=resolve_tool_choice(tool_choice),
            **kwargs,
        )
        if not allow_parallel_tool_calls:
            force_single_tool_call(response)
        return response

    def get_tool_calls_from_response(
        self,
        response: "AgentChatResponse",
        error_on_no_tool_call: bool = True,
        **kwargs: Any,
    ) -> List[ToolSelection]:
        """Predict and call the tool."""
        tool_calls = response.message.additional_kwargs.get("tool_calls", [])

        if len(tool_calls) < 1:
            if error_on_no_tool_call:
                raise ValueError(
                    f"Expected at least one tool call, but got {len(tool_calls)} tool calls."
                )
            else:
                return []

        tool_selections = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, get_args(OpenAIToolCall)):
                raise ValueError("Invalid tool_call object")
            if tool_call.type != "function":
                raise ValueError("Invalid tool type. Unsupported by OpenAI")
            argument_dict = json.loads(tool_call.function.arguments)

            tool_selections.append(
                ToolSelection(
                    tool_id=tool_call.id,
                    tool_name=tool_call.function.name,
                    tool_kwargs=argument_dict,
                )
            )

        return tool_selections
