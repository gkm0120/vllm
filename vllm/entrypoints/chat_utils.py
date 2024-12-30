import asyncio
import codecs
import json
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from functools import lru_cache, partial
from pathlib import Path
from typing import (Any, Awaitable, Callable, Dict, Generic, Iterable, List,
                    Literal, Optional, Tuple, TypeVar, Union, cast)

import jinja2.nodes
import transformers.utils.chat_template_utils as hf_chat_utils
# yapf conflicts with isort for this block
# yapf: disable
from openai.types.chat import (ChatCompletionAssistantMessageParam,
                               ChatCompletionContentPartImageParam,
                               ChatCompletionContentPartInputAudioParam)
from openai.types.chat import (
    ChatCompletionContentPartParam as OpenAIChatCompletionContentPartParam)
from openai.types.chat import (ChatCompletionContentPartRefusalParam,
                               ChatCompletionContentPartTextParam)
from openai.types.chat import (
    ChatCompletionMessageParam as OpenAIChatCompletionMessageParam)
from openai.types.chat import (ChatCompletionMessageToolCallParam,
                               ChatCompletionToolMessageParam)
from openai.types.chat.chat_completion_content_part_input_audio_param import (
    InputAudio)
# yapf: enable
# pydantic needs the TypedDict from typing_extensions
from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast
from typing_extensions import Required, TypeAlias, TypedDict

from vllm.config import ModelConfig
from vllm.logger import init_logger
from vllm.multimodal import MultiModalDataDict
from vllm.multimodal.utils import MediaConnector
from vllm.transformers_utils.tokenizer import AnyTokenizer, MistralTokenizer
from vllm.utils import print_warning_once

logger = init_logger(__name__)


class AudioURL(TypedDict, total=False):
    url: Required[str]
    """
    Either a URL of the audio or a data URL with base64 encoded audio data.
    """


class ChatCompletionContentPartAudioParam(TypedDict, total=False):
    audio_url: Required[AudioURL]

    type: Required[Literal["audio_url"]]
    """The type of the content part."""


class VideoURL(TypedDict, total=False):
    url: Required[str]
    """
    Either a URL of the video or a data URL with base64 encoded video data.
    """


class ChatCompletionContentPartVideoParam(TypedDict, total=False):
    video_url: Required[VideoURL]

    type: Required[Literal["video_url"]]
    """The type of the content part."""


class CustomChatCompletionContentSimpleImageParam(TypedDict, total=False):
    """A simpler version of the param that only accepts a plain image_url.
    This is supported by OpenAI API, although it is not documented.

    Example:
    {
        "image_url": "https://example.com/image.jpg"
    }
    """
    image_url: Required[str]


class CustomChatCompletionContentSimpleAudioParam(TypedDict, total=False):
    """A simpler version of the param that only accepts a plain audio_url.

    Example:
    {
        "audio_url": "https://example.com/audio.mp3"
    }
    """
    audio_url: Required[str]


class CustomChatCompletionContentSimpleVideoParam(TypedDict, total=False):
    """A simpler version of the param that only accepts a plain audio_url.

    Example:
    {
        "video_url": "https://example.com/video.mp4"
    }
    """
    video_url: Required[str]


ChatCompletionContentPartParam: TypeAlias = Union[
    OpenAIChatCompletionContentPartParam, ChatCompletionContentPartAudioParam,
    ChatCompletionContentPartInputAudioParam,
    ChatCompletionContentPartVideoParam, ChatCompletionContentPartRefusalParam,
    CustomChatCompletionContentSimpleImageParam,
    CustomChatCompletionContentSimpleAudioParam,
    CustomChatCompletionContentSimpleVideoParam, str]


class CustomChatCompletionMessageParam(TypedDict, total=False):
    """Enables custom roles in the Chat Completion API."""
    role: Required[str]
    """The role of the message's author."""

    content: Union[str, List[ChatCompletionContentPartParam]]
    """The contents of the message."""

    name: str
    """An optional name for the participant.

    Provides the model information to differentiate between participants of the
    same role.
    """

    tool_call_id: Optional[str]
    """Tool call that this message is responding to."""

    tool_calls: Optional[Iterable[ChatCompletionMessageToolCallParam]]
    """The tool calls generated by the model, such as function calls."""


ChatCompletionMessageParam = Union[OpenAIChatCompletionMessageParam,
                                   CustomChatCompletionMessageParam]


# TODO: Make fields ReadOnly once mypy supports it
class ConversationMessage(TypedDict, total=False):
    role: Required[str]
    """The role of the message's author."""

    content: Union[Optional[str], List[Dict[str, str]]]
    """The contents of the message"""

    tool_call_id: Optional[str]
    """Tool call that this message is responding to."""

    name: Optional[str]
    """The name of the function to call"""

    tool_calls: Optional[Iterable[ChatCompletionMessageToolCallParam]]
    """The tool calls generated by the model, such as function calls."""


# Passed in by user
ChatTemplateContentFormatOption = Literal["auto", "string", "openai"]

# Used internally
_ChatTemplateContentFormat = Literal["string", "openai"]


def _is_var_access(node: jinja2.nodes.Node, varname: str) -> bool:
    if isinstance(node, jinja2.nodes.Name):
        return node.ctx == "load" and node.name == varname

    return False


def _is_attr_access(node: jinja2.nodes.Node, varname: str, key: str) -> bool:
    if isinstance(node, jinja2.nodes.Getitem):
        return (_is_var_access(node.node, varname)
                and isinstance(node.arg, jinja2.nodes.Const)
                and node.arg.value == key)

    if isinstance(node, jinja2.nodes.Getattr):
        return _is_var_access(node.node, varname) and node.attr == key

    return False


def _is_var_or_elems_access(
    node: jinja2.nodes.Node,
    varname: str,
    key: Optional[str] = None,
) -> bool:
    if isinstance(node, jinja2.nodes.Filter):
        return (node.node is not None
                and _is_var_or_elems_access(node.node, varname, key))
    if isinstance(node, jinja2.nodes.Test):
        return _is_var_or_elems_access(node.node, varname, key)

    if (isinstance(node, jinja2.nodes.Getitem)
            and isinstance(node.arg, jinja2.nodes.Slice)):
        return _is_var_or_elems_access(node.node, varname, key)

    # yapf: disable
    return (
        _is_attr_access(node, varname, key) if key
        else _is_var_access(node, varname)
    ) # yapf: enable


def _iter_nodes_assign_var_or_elems(root: jinja2.nodes.Node, varname: str):
    # Global variable that is implicitly defined at the root
    yield root, varname

    # Iterative BFS
    related_varnames = deque([varname])
    while related_varnames:
        related_varname = related_varnames.popleft()

        for assign_ast in root.find_all(jinja2.nodes.Assign):
            lhs = assign_ast.target
            rhs = assign_ast.node

            if _is_var_or_elems_access(rhs, related_varname):
                assert isinstance(lhs, jinja2.nodes.Name)
                yield assign_ast, lhs.name

                # Avoid infinite looping for self-assignment
                if lhs.name != related_varname:
                    related_varnames.append(lhs.name)


# NOTE: The proper way to handle this is to build a CFG so that we can handle
# the scope in which each variable is defined, but that is too complicated
def _iter_nodes_assign_messages_item(root: jinja2.nodes.Node):
    messages_varnames = [
        varname
        for _, varname in _iter_nodes_assign_var_or_elems(root, "messages")
    ]

    # Search for {%- for message in messages -%} loops
    for loop_ast in root.find_all(jinja2.nodes.For):
        loop_iter = loop_ast.iter
        loop_target = loop_ast.target

        for varname in messages_varnames:
            if _is_var_or_elems_access(loop_iter, varname):
                assert isinstance(loop_target, jinja2.nodes.Name)
                yield loop_ast, loop_target.name
                break


def _iter_nodes_assign_content_item(root: jinja2.nodes.Node):
    message_varnames = [
        varname for _, varname in _iter_nodes_assign_messages_item(root)
    ]

    # Search for {%- for content in message['content'] -%} loops
    for loop_ast in root.find_all(jinja2.nodes.For):
        loop_iter = loop_ast.iter
        loop_target = loop_ast.target

        for varname in message_varnames:
            if _is_var_or_elems_access(loop_iter, varname, "content"):
                assert isinstance(loop_target, jinja2.nodes.Name)
                yield loop_ast, loop_target.name
                break


def _try_extract_ast(chat_template: str) -> Optional[jinja2.nodes.Template]:
    try:
        jinja_compiled = hf_chat_utils._compile_jinja_template(chat_template)
        return jinja_compiled.environment.parse(chat_template)
    except Exception:
        logger.exception("Error when compiling Jinja template")
        return None


def _detect_content_format(
    chat_template: str,
    *,
    default: _ChatTemplateContentFormat,
) -> _ChatTemplateContentFormat:
    jinja_ast = _try_extract_ast(chat_template)
    if jinja_ast is None:
        return default

    try:
        next(_iter_nodes_assign_content_item(jinja_ast))
    except StopIteration:
        return "string"
    except Exception:
        logger.exception("Error when parsing AST of Jinja template")
        return default
    else:
        return "openai"


def _resolve_chat_template_content_format(
    chat_template: Optional[str],
    given_format: ChatTemplateContentFormatOption,
    tokenizer: AnyTokenizer,
) -> _ChatTemplateContentFormat:
    if isinstance(tokenizer, (PreTrainedTokenizer, PreTrainedTokenizerFast)):
        tokenizer_chat_template = tokenizer.chat_template
    else:
        tokenizer_chat_template = None

    jinja_text: Optional[str]
    if isinstance(tokenizer_chat_template, str) and chat_template is None:
        jinja_text = tokenizer_chat_template
    elif (isinstance(tokenizer_chat_template, dict)
            and chat_template in tokenizer_chat_template):
        jinja_text = tokenizer_chat_template[chat_template]
    else:
        jinja_text = load_chat_template(chat_template, is_literal=True)

    detected_format = ("string" if jinja_text is None else
                       _detect_content_format(jinja_text, default="string"))

    return detected_format if given_format == "auto" else given_format


@lru_cache
def resolve_chat_template_content_format(
    chat_template: Optional[str],
    given_format: ChatTemplateContentFormatOption,
    tokenizer: AnyTokenizer,
) -> _ChatTemplateContentFormat:
    detected_format = _resolve_chat_template_content_format(
        chat_template,
        given_format,
        tokenizer,
    )

    logger.info(
        "Detected the chat template content format to be '%s'. "
        "You can set `--chat-template-content-format` to override this.",
        detected_format,
    )

    if given_format != "auto" and given_format != detected_format:
        logger.warning(
            "You specified `--chat-template-content-format %s` "
            "which is different from the detected format '%s'. "
            "If our automatic detection is incorrect, please consider "
            "opening a GitHub issue so that we can improve it: "
            "https://github.com/vllm-project/vllm/issues/new/choose",
            given_format,
            detected_format,
        )

    return detected_format


ModalityStr = Literal["image", "audio", "video"]
_T = TypeVar("_T")


class BaseMultiModalItemTracker(ABC, Generic[_T]):
    """
    Tracks multi-modal items in a given request and ensures that the number
    of multi-modal items in a given request does not exceed the configured
    maximum per prompt.
    """

    def __init__(self, model_config: ModelConfig, tokenizer: AnyTokenizer):
        super().__init__()

        self._model_config = model_config
        self._tokenizer = tokenizer
        self._allowed_items = (model_config.multimodal_config.limit_per_prompt
                               if model_config.multimodal_config else {})

        self._items_by_modality = defaultdict[str, list[_T]](list)

    @property
    def model_config(self) -> ModelConfig:
        return self._model_config

    @property
    def allowed_local_media_path(self):
        return self._model_config.allowed_local_media_path

    @staticmethod
    @lru_cache(maxsize=None)
    def _cached_token_str(tokenizer: AnyTokenizer, token_index: int) -> str:
        return tokenizer.decode(token_index)

    def _placeholder_str(self, modality: ModalityStr,
                         current_count: int) -> Optional[str]:
        # TODO: Let user specify how to insert image tokens into prompt
        # (similar to chat template)
        hf_config = self._model_config.hf_config
        model_type = hf_config.model_type

        if modality == "image":
            if model_type == "phi3_v":
                # Workaround since this token is not defined in the tokenizer
                return f"<|image_{current_count}|>"
            if model_type == "minicpmv":
                return "(<image>./</image>)"
            if model_type in ("blip-2", "chatglm", "fuyu", "paligemma",
                              "pixtral"):
                # These models do not use image tokens in the prompt
                return None
            if model_type == "qwen":
                return f"Picture {current_count}: <img></img>"
            if model_type.startswith("llava"):
                return self._cached_token_str(self._tokenizer,
                                              hf_config.image_token_index)
            if model_type in ("chameleon", "internvl_chat", "NVLM_D",
                              "h2ovl_chat"):
                return "<image>"
            if model_type == "mllama":
                return "<|image|>"
            if model_type == "qwen2_vl":
                return "<|vision_start|><|image_pad|><|vision_end|>"
            if model_type == "molmo":
                return ""
            if model_type == "idefics3":
                return "<image>"
            if model_type == "aria":
                return "<|fim_prefix|><|img|><|fim_suffix|>"

            raise TypeError(f"Unknown {modality} model type: {model_type}")
        elif modality == "audio":
            if model_type == "ultravox":
                return "<|audio|>"
            if model_type == "qwen2_audio":
                return (f"Audio {current_count}: "
                        f"<|audio_bos|><|AUDIO|><|audio_eos|>")
            raise TypeError(f"Unknown model type: {model_type}")
        elif modality == "video":
            if model_type == "qwen2_vl":
                return "<|vision_start|><|video_pad|><|vision_end|>"
            if model_type.startswith("llava"):
                return self._cached_token_str(self._tokenizer,
                                              hf_config.video_token_index)
            raise TypeError(f"Unknown {modality} model type: {model_type}")
        else:
            raise TypeError(f"Unknown modality: {modality}")

    def add(self, modality: ModalityStr, item: _T) -> Optional[str]:
        """
        Add a multi-modal item to the current prompt and returns the
        placeholder string to use, if any.
        """
        allowed_count = self._allowed_items.get(modality, 1)
        current_count = len(self._items_by_modality[modality]) + 1
        if current_count > allowed_count:
            raise ValueError(
                f"At most {allowed_count} {modality}(s) may be provided in "
                "one request.")

        self._items_by_modality[modality].append(item)

        return self._placeholder_str(modality, current_count)

    @abstractmethod
    def create_parser(self) -> "BaseMultiModalContentParser":
        raise NotImplementedError


class MultiModalItemTracker(BaseMultiModalItemTracker[object]):

    def all_mm_data(self) -> Optional[MultiModalDataDict]:
        if self._items_by_modality:
            return dict(self._items_by_modality)

        return None

    def create_parser(self) -> "BaseMultiModalContentParser":
        return MultiModalContentParser(self)


class AsyncMultiModalItemTracker(BaseMultiModalItemTracker[Awaitable[object]]):

    async def all_mm_data(self) -> Optional[MultiModalDataDict]:
        if self._items_by_modality:
            return {
                modality: await asyncio.gather(*items)
                for modality, items in self._items_by_modality.items()
            }

        return None

    def create_parser(self) -> "BaseMultiModalContentParser":
        return AsyncMultiModalContentParser(self)


class BaseMultiModalContentParser(ABC):

    def __init__(self) -> None:
        super().__init__()

        # multimodal placeholder_string : count
        self._placeholder_counts: Dict[str, int] = defaultdict(lambda: 0)

    def _add_placeholder(self, placeholder: Optional[str]):
        if placeholder:
            self._placeholder_counts[placeholder] += 1

    def mm_placeholder_counts(self) -> Dict[str, int]:
        return dict(self._placeholder_counts)

    @abstractmethod
    def parse_image(self, image_url: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def parse_audio(self, audio_url: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def parse_input_audio(self, input_audio: InputAudio) -> None:
        raise NotImplementedError

    @abstractmethod
    def parse_video(self, video_url: str) -> None:
        raise NotImplementedError


class MultiModalContentParser(BaseMultiModalContentParser):

    def __init__(self, tracker: MultiModalItemTracker) -> None:
        super().__init__()

        self._tracker = tracker

        self._connector = MediaConnector(
            allowed_local_media_path=tracker.allowed_local_media_path,
        )

    def parse_image(self, image_url: str) -> None:
        image = self._connector.fetch_image(image_url)

        placeholder = self._tracker.add("image", image)
        self._add_placeholder(placeholder)

    def parse_audio(self, audio_url: str) -> None:
        audio = self._connector.fetch_audio(audio_url)

        placeholder = self._tracker.add("audio", audio)
        self._add_placeholder(placeholder)

    def parse_input_audio(self, input_audio: InputAudio) -> None:
        audio_data = input_audio.get("data", "")
        audio_format = input_audio.get("format", "")
        audio_url = f"data:audio/{audio_format};base64,{audio_data}"

        return self.parse_audio(audio_url)

    def parse_video(self, video_url: str) -> None:
        video = self._connector.fetch_video(video_url)

        placeholder = self._tracker.add("video", video)
        self._add_placeholder(placeholder)


class AsyncMultiModalContentParser(BaseMultiModalContentParser):

    def __init__(self, tracker: AsyncMultiModalItemTracker) -> None:
        super().__init__()

        self._tracker = tracker
        self._connector = MediaConnector(
            allowed_local_media_path=tracker.allowed_local_media_path,
        )

    def parse_image(self, image_url: str) -> None:
        image_coro = self._connector.fetch_image_async(image_url)

        placeholder = self._tracker.add("image", image_coro)
        self._add_placeholder(placeholder)

    def parse_audio(self, audio_url: str) -> None:
        audio_coro = self._connector.fetch_audio_async(audio_url)

        placeholder = self._tracker.add("audio", audio_coro)
        self._add_placeholder(placeholder)

    def parse_input_audio(self, input_audio: InputAudio) -> None:
        audio_data = input_audio.get("data", "")
        audio_format = input_audio.get("format", "")
        audio_url = f"data:audio/{audio_format};base64,{audio_data}"

        return self.parse_audio(audio_url)

    def parse_video(self, video_url: str) -> None:
        video = self._connector.fetch_video_async(video_url)

        placeholder = self._tracker.add("video", video)
        self._add_placeholder(placeholder)


def validate_chat_template(chat_template: Optional[Union[Path, str]]):
    """Raises if the provided chat template appears invalid."""
    if chat_template is None:
        return

    elif isinstance(chat_template, Path) and not chat_template.exists():
        raise FileNotFoundError(
            "the supplied chat template path doesn't exist")

    elif isinstance(chat_template, str):
        JINJA_CHARS = "{}\n"
        if not any(c in chat_template
                   for c in JINJA_CHARS) and not Path(chat_template).exists():
            raise ValueError(
                f"The supplied chat template string ({chat_template}) "
                f"appears path-like, but doesn't exist!")

    else:
        raise TypeError(
            f"{type(chat_template)} is not a valid chat template type")


def load_chat_template(
    chat_template: Optional[Union[Path, str]],
    *,
    is_literal: bool = False,
) -> Optional[str]:
    if chat_template is None:
        return None

    if is_literal:
        if isinstance(chat_template, Path):
            raise TypeError("chat_template is expected to be read directly "
                            "from its value")

        return codecs.decode(chat_template, "unicode_escape")

    try:
        with open(chat_template) as f:
            return f.read()
    except OSError as e:
        if isinstance(chat_template, Path):
            raise

        JINJA_CHARS = "{}\n"
        if not any(c in chat_template for c in JINJA_CHARS):
            msg = (f"The supplied chat template ({chat_template}) "
                   f"looks like a file path, but it failed to be "
                   f"opened. Reason: {e}")
            raise ValueError(msg) from e

        # If opening a file fails, set chat template to be args to
        # ensure we decode so our escape are interpreted correctly
        return load_chat_template(chat_template, is_literal=True)


# TODO: Let user specify how to insert multimodal tokens into prompt
# (similar to chat template)
def _get_full_multimodal_text_prompt(placeholder_counts: Dict[str, int],
                                     text_prompt: str) -> str:
    """Combine multimodal prompts for a multimodal language model."""

    # Look through the text prompt to check for missing placeholders
    missing_placeholders: List[str] = []
    for placeholder in placeholder_counts:

        # For any existing placeholder in the text prompt, we leave it as is
        placeholder_counts[placeholder] -= text_prompt.count(placeholder)

        if placeholder_counts[placeholder] < 0:
            raise ValueError(
                f"Found more '{placeholder}' placeholders in input prompt than "
                "actual multimodal data items.")

        missing_placeholders.extend([placeholder] *
                                    placeholder_counts[placeholder])

    # NOTE: For now we always add missing placeholders at the front of
    # the prompt. This may change to be customizable in the future.
    return "\n".join(missing_placeholders + [text_prompt])


# No need to validate using Pydantic again
_TextParser = partial(cast, ChatCompletionContentPartTextParam)
_ImageParser = partial(cast, ChatCompletionContentPartImageParam)
_AudioParser = partial(cast, ChatCompletionContentPartAudioParam)
_InputAudioParser = partial(cast, ChatCompletionContentPartInputAudioParam)
_RefusalParser = partial(cast, ChatCompletionContentPartRefusalParam)
_VideoParser = partial(cast, ChatCompletionContentPartVideoParam)

_ContentPart: TypeAlias = Union[str, Dict[str, str], InputAudio]

# Define a mapping from part types to their corresponding parsing functions.
MM_PARSER_MAP: Dict[
    str,
    Callable[[ChatCompletionContentPartParam], _ContentPart],
] = {
    "text":
    lambda part: _TextParser(part).get("text", ""),
    "image_url":
    lambda part: _ImageParser(part).get("image_url", {}).get("url", ""),
    "audio_url":
    lambda part: _AudioParser(part).get("audio_url", {}).get("url", ""),
    "input_audio":
    lambda part: _InputAudioParser(part).get("input_audio", {}),
    "refusal":
    lambda part: _RefusalParser(part).get("refusal", ""),
    "video_url":
    lambda part: _VideoParser(part).get("video_url", {}).get("url", ""),
}


def _parse_chat_message_content_mm_part(
        part: ChatCompletionContentPartParam) -> tuple[str, _ContentPart]:
    """
    Parses a given multi-modal content part based on its type.

    Args:
        part: A dict containing the content part, with a potential 'type' field.

    Returns:
        A tuple (part_type, content) where:
        - part_type: Type of the part (e.g., 'text', 'image_url').
        - content: Parsed content (e.g., text, image URL).

    Raises:
        ValueError: If the 'type' field is missing and no direct URL is found.
    """
    assert isinstance(
        part, dict)  # This is needed to avoid mypy errors: part.get() from str
    part_type = part.get("type", None)

    if isinstance(part_type, str) and part_type in MM_PARSER_MAP:
        content = MM_PARSER_MAP[part_type](part)

        # Special case for 'image_url.detail'
        # We only support 'auto', which is the default
        if part_type == "image_url" and part.get("detail", "auto") != "auto":
            logger.warning("'image_url.detail' is currently not supported "
                           "and will be ignored.")

        return part_type, content

    # Handle missing 'type' but provided direct URL fields.
    # 'type' is required field by pydantic
    if part_type is None:
        if part.get("image_url") is not None:
            image_params = cast(CustomChatCompletionContentSimpleImageParam,
                                part)
            return "image_url", image_params.get("image_url", "")
        if part.get("audio_url") is not None:
            audio_params = cast(CustomChatCompletionContentSimpleAudioParam,
                                part)
            return "audio_url", audio_params.get("audio_url", "")
        if part.get("input_audio") is not None:
            input_audio_params = cast(Dict[str, str], part)
            return "input_audio", input_audio_params
        if part.get("video_url") is not None:
            video_params = cast(CustomChatCompletionContentSimpleVideoParam,
                                part)
            return "video_url", video_params.get("video_url", "")
        # Raise an error if no 'type' or direct URL is found.
        raise ValueError("Missing 'type' field in multimodal part.")

    if not isinstance(part_type, str):
        raise ValueError("Invalid 'type' field in multimodal part.")
    return part_type, "unknown part_type content"


VALID_MESSAGE_CONTENT_MM_PART_TYPES = ("text", "refusal", "image_url",
                                       "audio_url", "input_audio", "video_url")


def _parse_chat_message_content_parts(
    role: str,
    parts: Iterable[ChatCompletionContentPartParam],
    mm_tracker: BaseMultiModalItemTracker,
    *,
    wrap_dicts: bool,
) -> List[ConversationMessage]:
    content = list[_ContentPart]()

    mm_parser = mm_tracker.create_parser()

    for part in parts:
        parse_res = _parse_chat_message_content_part(
            part,
            mm_parser,
            wrap_dicts=wrap_dicts,
        )
        if parse_res:
            content.append(parse_res)

    if wrap_dicts:
        # Parsing wraps images and texts as interleaved dictionaries
        return [ConversationMessage(role=role,
                                    content=content)]  # type: ignore
    texts = cast(List[str], content)
    text_prompt = "\n".join(texts)
    mm_placeholder_counts = mm_parser.mm_placeholder_counts()
    if mm_placeholder_counts:
        text_prompt = _get_full_multimodal_text_prompt(mm_placeholder_counts,
                                                       text_prompt)
    return [ConversationMessage(role=role, content=text_prompt)]


def _parse_chat_message_content_part(
    part: ChatCompletionContentPartParam,
    mm_parser: BaseMultiModalContentParser,
    *,
    wrap_dicts: bool,
) -> Optional[_ContentPart]:
    """Parses a single part of a conversation. If wrap_dicts is True,
    structured dictionary pieces for texts and images will be
    wrapped in dictionaries, i.e., {"type": "text", "text", ...} and
    {"type": "image"}, respectively. Otherwise multimodal data will be
    handled by mm_parser, and texts will be returned as strings to be joined
    with multimodal placeholders.
    """
    if isinstance(part, str):  # Handle plain text parts
        return part

    # Handle structured dictionary parts
    part_type, content = _parse_chat_message_content_mm_part(part)

    # if part_type is text/refusal/image_url/audio_url/video_url/input_audio but
    # content is empty, log a warning and skip
    if part_type in VALID_MESSAGE_CONTENT_MM_PART_TYPES and not content:
        logger.warning(
            "Skipping multimodal part (type: '%s')"
            "with empty / unparsable content.", part_type)
        return None

    if part_type in ("text", "refusal"):
        str_content = cast(str, content)
        if wrap_dicts:
            return {'type': 'text', 'text': str_content}
        else:
            return str_content

    if part_type == "image_url":
        str_content = cast(str, content)
        mm_parser.parse_image(str_content)
        return {'type': 'image'} if wrap_dicts else None

    if part_type == "audio_url":
        str_content = cast(str, content)
        mm_parser.parse_audio(str_content)
        return {'type': 'audio'} if wrap_dicts else None

    if part_type == "input_audio":
        dict_content = cast(InputAudio, content)
        mm_parser.parse_input_audio(dict_content)
        return {'type': 'audio'} if wrap_dicts else None

    if part_type == "video_url":
        str_content = cast(str, content)
        mm_parser.parse_video(str_content)
        return {'type': 'video'} if wrap_dicts else None

    raise NotImplementedError(f"Unknown part type: {part_type}")


# No need to validate using Pydantic again
_AssistantParser = partial(cast, ChatCompletionAssistantMessageParam)
_ToolParser = partial(cast, ChatCompletionToolMessageParam)


def _parse_chat_message_content(
    message: ChatCompletionMessageParam,
    mm_tracker: BaseMultiModalItemTracker,
    content_format: _ChatTemplateContentFormat,
) -> List[ConversationMessage]:
    role = message["role"]
    content = message.get("content")

    if content is None:
        content = []
    elif isinstance(content, str):
        content = [
            ChatCompletionContentPartTextParam(type="text", text=content)
        ]
    result = _parse_chat_message_content_parts(
        role,
        content,  # type: ignore
        mm_tracker,
        wrap_dicts=(content_format == "openai"),
    )

    for result_msg in result:
        if role == 'assistant':
            parsed_msg = _AssistantParser(message)

            if "tool_calls" in parsed_msg:
                result_msg["tool_calls"] = list(parsed_msg["tool_calls"])
        elif role == "tool":
            parsed_msg = _ToolParser(message)
            if "tool_call_id" in parsed_msg:
                result_msg["tool_call_id"] = parsed_msg["tool_call_id"]

        if "name" in message and isinstance(message["name"], str):
            result_msg["name"] = message["name"]

    return result


def _postprocess_messages(messages: List[ConversationMessage]) -> None:
    # per the Transformers docs & maintainers, tool call arguments in
    # assistant-role messages with tool_calls need to be dicts not JSON str -
    # this is how tool-use chat templates will expect them moving forwards
    # so, for messages that have tool_calls, parse the string (which we get
    # from openAI format) to dict
    for message in messages:
        if (message["role"] == "assistant" and "tool_calls" in message
                and isinstance(message["tool_calls"], list)):

            for item in message["tool_calls"]:
                item["function"]["arguments"] = json.loads(
                    item["function"]["arguments"])


def parse_chat_messages(
    messages: List[ChatCompletionMessageParam],
    model_config: ModelConfig,
    tokenizer: AnyTokenizer,
    content_format: _ChatTemplateContentFormat,
) -> Tuple[List[ConversationMessage], Optional[MultiModalDataDict]]:
    conversation: List[ConversationMessage] = []
    mm_tracker = MultiModalItemTracker(model_config, tokenizer)

    for msg in messages:
        sub_messages = _parse_chat_message_content(
            msg,
            mm_tracker,
            content_format,
        )

        conversation.extend(sub_messages)

    _postprocess_messages(conversation)

    return conversation, mm_tracker.all_mm_data()


def parse_chat_messages_futures(
    messages: List[ChatCompletionMessageParam],
    model_config: ModelConfig,
    tokenizer: AnyTokenizer,
    content_format: _ChatTemplateContentFormat,
) -> Tuple[List[ConversationMessage], Awaitable[Optional[MultiModalDataDict]]]:
    conversation: List[ConversationMessage] = []
    mm_tracker = AsyncMultiModalItemTracker(model_config, tokenizer)

    for msg in messages:
        sub_messages = _parse_chat_message_content(
            msg,
            mm_tracker,
            content_format,
        )

        conversation.extend(sub_messages)

    _postprocess_messages(conversation)

    return conversation, mm_tracker.all_mm_data()


def apply_hf_chat_template(
    tokenizer: Union[PreTrainedTokenizer, PreTrainedTokenizerFast],
    conversation: List[ConversationMessage],
    chat_template: Optional[str],
    *,
    tokenize: bool = False,  # Different from HF's default
    **kwargs: Any,
) -> str:
    if chat_template is None and tokenizer.chat_template is None:
        raise ValueError(
            "As of transformers v4.44, default chat template is no longer "
            "allowed, so you must provide a chat template if the tokenizer "
            "does not define one.")

    return tokenizer.apply_chat_template(
        conversation=conversation,  # type: ignore[arg-type]
        chat_template=chat_template,
        tokenize=tokenize,
        **kwargs,
    )


def apply_mistral_chat_template(
    tokenizer: MistralTokenizer,
    messages: List[ChatCompletionMessageParam],
    chat_template: Optional[str] = None,
    **kwargs: Any,
) -> List[int]:
    if chat_template is not None:
        print_warning_once(
            "'chat_template' cannot be overridden for mistral tokenizer.")
    if "add_generation_prompt" in kwargs:
        print_warning_once(
            "'add_generation_prompt' is not supported for mistral tokenizer, "
            "so it will be ignored.")
    if "continue_final_message" in kwargs:
        print_warning_once(
            "'continue_final_message' is not supported for mistral tokenizer, "
            "so it will be ignored.")

    return tokenizer.apply_chat_template(
        messages=messages,
        **kwargs,
    )