# -*- coding: utf-8 -*-
# @Time    : 2/19/23 1:03 PM
# @FileName: chatgpt.py
# @Software: PyCharm
# @Github    ：sudoskys
import math
import time
import random
import tiktoken
from typing import Union, Optional, Callable, Any, Dict, Tuple, Mapping, List

# from loguru import logger

from ...error import RateLimitError, ServiceUnavailableError
from pydantic import BaseModel, Field
from tenacity import retry_if_exception_type, retry, stop_after_attempt, wait_exponential
from ..agent import Conversation
from ..llms.base import LlmBase, LlmBaseParam
from ..types import LlmReturn
from ...utils.data import DataUtils
from ...utils.setting import llmRetryAttempt, llmRetryTime, llmRetryTimeMax, llmRetryTimeMin
from ...utils import network


class ChatGptParam(LlmBaseParam, BaseModel):
    api: str
    """Mew Mew API"""
    model_name: str = "text-davinci-003"
    """Model name to use."""
    max_tokens: int = 256
    """The maximum number of tokens to generate in the completion.
    -1 returns as many tokens as possible given the prompt and
    the models maximal context size."""
    request_timeout: Optional[Union[float, Tuple[float, float]]] = None
    """Timeout for requests to OpenAI completion API. Default is 600 seconds."""
    model_kwargs: Dict[str, Any] = Field(default_factory=dict)
    """Holds any model parameters valid for create call not explicitly specified."""

    @property
    def _default_params(self) -> Dict[str, Any]:
        """Get the default parameters for calling OpenAI API."""
        normal_params = {
            "max_tokens": self.max_tokens,
            "request_timeout": self.request_timeout,
        }
        return {**normal_params, **self.model_kwargs}

    @property
    def invocation_params(self) -> Dict[str, Any]:
        """Get the parameters used to invoke the model."""
        return self._default_params

    @property
    def identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {**{"model_name": self.model_name}, **self._default_params}


class ChatGpt(LlmBase):
    """CatGpt"""

    def __init__(self, profile: Conversation,
                 api_key: Union[str, list] = None,
                 token_limit: int = 3700,
                 auto_penalty: bool = False,
                 call_func: Callable[[dict, str], Any] = None,
                 ):
        """
        Openai LLM 的方法类集合
        :param api_key: api key
        :param token_limit: 总限制
        :param call_func: 回调
        :param auto_penalty: 不使用自动惩罚参数调整
        """
        self.auto_penalty = auto_penalty
        self.profile = profile
        # if api_key is None:
        #     api_key = setting.openaiApiKey
        if isinstance(api_key, list):
            api_key: list
            if not api_key:
                raise RuntimeError("NO KEY")
            api_key = random.choice(api_key)
            api_key: str
        self.__api_key = api_key
        if not api_key:
            raise RuntimeError("NO KEY")
        self.__start_sequence = self.profile.start_name
        self.__restart_sequence = self.profile.restart_name
        self.__call_func = call_func
        self.token_limit = token_limit

    def get_token_limit(self) -> int:
        return self.token_limit

    def tokenizer(self, text, raw: bool = False) -> Union[int, list]:
        gpt_tokenizer = tiktoken.get_encoding("gpt2")
        _token = gpt_tokenizer.encode(text)
        if raw:
            return _token
        return len(_token)

    @staticmethod
    def parse_response(response) -> list:
        REPLY = []
        Choice = response.get("response")
        if Choice:
            REPLY.append(Choice)
        if not REPLY:
            REPLY = [""]
        return REPLY

    @staticmethod
    def parse_usage(response) -> Optional[int]:
        return 0

    @property
    def _llm_type(self) -> str:
        """Return type of llm."""
        return "unknown"

    def parse_reply(self, reply: List[str]) -> str:
        """处理解码后的列表"""
        _reply = "".join(reply)
        _reply = DataUtils.remove_suffix(input_string=_reply, suffix="<|im_end|>")
        return _reply

    def resize_sentence(self, text: str, token: int) -> str:
        """
        改进后的梯度缓存裁剪算法
        """
        token = token if token > 0 else 0
        step = 4
        _cache = {}

        def _cache_cutter(_text):
            if _cache.get(_text):
                return _cache.get(_text)
            _value = self.tokenizer(_text)
            _cache[_text] = _value
            return _value

        while len(text) > step and _cache_cutter(text) > token:
            _rank = math.floor((_cache_cutter(text) - token) / 100) + 4
            step = _rank * 8
            text = text[step:]
        _cache = {}
        return text

    async def task_context(self, task: str, prompt: str, predict_tokens: int = 500) -> LlmReturn:
        prompt = self.resize_sentence(prompt, 1200)
        _prompt = f"Text:{prompt}\n{task}: "
        llm_result = await self.run(prompt=_prompt,
                                    predict_tokens=predict_tokens,
                                    llm_param=ChatGptParam(model_name="text-davinci-003"),
                                    stop_words=["Text:", "\n\n"]
                                    )
        return llm_result

    def resize_context(self, head: list, body: list, foot: list = None, token: int = 5) -> str:
        if foot is None:
            foot = []
        # 去空
        body = [item for item in body if item]
        # 强制测量
        token = token if token > 5 else 5

        # 弹性计算
        def _connect(_head, _body, _foot):
            _head = '\n'.join(_head) + "\n"
            _body = "\n".join(_body) + "\n"
            _foot = ''.join(_foot)
            # Resize
            return _head + _body + _foot

        _all = _connect(head, body, foot)
        while len(body) > 2 and self.tokenizer(_all) >= token:
            body.pop(0)
            _all = _connect(head, body, foot)
        _all = self.resize_sentence(_all, token=token)
        return _all

    @staticmethod
    def model_context_size(model_name: str) -> int:
        if model_name == "text-davinci-003":
            return 4000
        elif model_name == "text-curie-001":
            return 2048
        elif model_name == "text-babbage-001":
            return 2048
        elif model_name == "text-ada-001":
            return 2048
        elif model_name == "code-davinci-002":
            return 8000
        elif model_name == "code-cushman-001":
            return 2048
        else:
            return 4000

    @retry(retry=retry_if_exception_type((RateLimitError,
                                          ServiceUnavailableError)),
           stop=stop_after_attempt(llmRetryAttempt),
           wait=wait_exponential(multiplier=llmRetryTime, min=llmRetryTimeMin, max=llmRetryTimeMax),
           reraise=True)
    async def run(self,
                  prompt: str,
                  validate: Union[List[str], None] = None,
                  predict_tokens: int = 500,
                  llm_param: ChatGptParam = None,
                  stop_words: list = None,
                  anonymous_user: bool = True,
                  ) -> LlmReturn:
        """
        异步的，得到对话上下文
        :param predict_tokens: 限制返回字符数量
        :param validate: 惩罚验证列表
        :param prompt: 提示词
        :param llm_param: 参数表
        :param anonymous_user:
        :param stop_words:
        :return:
        """
        _request_arg = {
            "top_p": 1,
            "n": 1
        }
        _request_arg: dict
        if stop_words is None:
            stop_words = [f"{self.profile.start_name}:",
                          f"{self.profile.restart_name}:",
                          f"{self.profile.start_name}：",
                          f"{self.profile.restart_name}："]
        # Kwargs
        if llm_param:
            _request_arg.update(llm_param.invocation_params)
        if validate is None:
            validate = []
        # Anonymous
        if anonymous_user:
            _request_arg.pop("user", None)
        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        _message_arg = {
            "message": prompt
        }
        # 自维护 Api 库
        try:
            response = await network.request(
                method="POST",
                url=_request_arg.get("api") + "/message",
                data=_message_arg,
                headers=headers,
                json_body=True,
            )
            _ = response.json()
        except Exception as e:
            raise ServiceUnavailableError(f"Server:{e}")
        if response.status_code != 200:
            raise ServiceUnavailableError(f"Server:{response.json().get('error')}")
        # Reply
        reply = self.parse_response(response)
        self.profile.update_usage(usage=self.parse_usage(response))
        return LlmReturn(model_flag=llm_param.model_name,
                         raw=response,
                         prompt=prompt,
                         usage=self.profile.get_round_usage(),
                         time=int(time.time()),
                         reply=reply,
                         )