# MemeMeow 的 MaiBot 插件入口，提供检索工具、图片发送和正常回复中的表情包附件。

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import httpx
from cachetools import TTLCache
from loguru import logger
from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, ReplyExtension, Tool
from maibot_sdk.types import ErrorPolicy
from pydantic import ConfigDict, TypeAdapter, field_validator, model_validator

PROACTIVE_PROMPT_ID = "cc.mememeow.maibot.proactive"
PROACTIVE_PROMPT = (
    "当你决定参与聊天时，主动使用 MemeMeow 表情包。根据当前话题、对方的话和你准备回复的内容，"
    "积极寻找适合的表情包，用于接梗、调侃、回应、举例或补充表达，无须等待用户要求。"
    "查询可以描述人物、动作、台词、场景、相关梗或情绪。"
    "为文字回复配图时，在 reply 的 plugin_options 中填写 "
    '"cc.mememeow.maibot.meme": {"query": "具体查询"}，插件会自动搜索并附上一张图片。'
    "需要比较候选时，调用 mememeow_search，阅读 meme_context 后在同一扩展中填写选中的 meme_id；"
    "query 与 meme_id 只填写一个。独立发送图片可以调用 mememeow_send。"
    "注意上下文关联和最近发送的内容，减少重复；尊重用户明确提出的纯文字要求。"
)


class MemeMeowError(RuntimeError):
    """保存经过密钥脱敏的阶段和具体错误，供 MaiBot 记录本次调用的失败原因。"""

    def __init__(self, code: str, stage: str, **details: Any) -> None:
        """接收稳定错误类别、发生阶段和诊断字段，构造可读取的 JSON 错误。"""
        self.details = {"error": code, "stage": stage, **details}
        super().__init__(json.dumps(self.details, ensure_ascii=False))


class MemeMeowSettings(PluginConfigBase):
    """定义运营者配置；搜索范围和允许使用的聊天流由运营者统一控制。"""

    model_config = ConfigDict(hide_input_in_errors=True)
    __ui_label__ = "MemeMeow"

    config_version: str = Field(default="0.1.0", description="插件配置版本")
    enabled: bool = Field(default=True, description="启用 MemeMeow")
    api_base: str = Field(default="https://mememeow.cc", description="MemeMeow 服务根地址")
    api_key: str = Field(default="", repr=False, description="机器人账户的 API key",
                         json_schema_extra={"input_type": "password"})
    auto_reply: bool = Field(default=True, description="允许麦麦在正常回复中自主选择表情包")
    proactive_memes: bool = Field(
        default=False, description="积极使用表情包：在 Planner 请求前注入使用建议；需要开启 auto_reply，"
                                   "可能增加搜索请求、服务额度消耗和回复等待时间",
    )
    allowed_streams: list[str] = Field(
        default_factory=list, description="允许使用的聊天流 ID；空数组允许全部",
    )
    search_scope: Literal["all", "personal"] = Field(default="all", description="全部可用来源或个人图库")
    collection_id: str = Field(default="", description="限定本人合集的 UUID；空字符串使用默认范围")
    subscription_id: str = Field(default="", description="限定订阅的 UUID；与本人合集互斥")
    candidate_count: int = Field(default=5, ge=1, le=10, description="每次检索的候选图片数量")
    llm_enhance: bool = Field(default=False, description="使用 MemeMeow 的 LLM 增强搜索")
    max_edge: Literal[0, 60, 120, 240, 360, 480, 640] = Field(
        default=480, description="图片最长边；0 使用原图",
    )
    request_timeout: float = Field(default=20, gt=0, le=40, description="单次 HTTP 请求超时秒数")
    max_media_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024,
                                 description="单张图片允许下载的最大字节数")

    @field_validator("max_edge", mode="before")
    @classmethod
    def validate_max_edge(cls, value: Any) -> int:
        """将 WebUI 下拉菜单提交的数值文本转为整数，随后校验允许的图片尺寸。"""
        return TypeAdapter(int).validate_python(value)

    @field_validator("api_base")
    @classmethod
    def validate_api_base(cls, value: str) -> str:
        """接受 HTTPS 根地址与本机 HTTP 地址，确保认证请求有确定的服务来源。"""
        url = httpx.URL(value)
        local_http = url.scheme == "http" and url.host in {"127.0.0.1", "localhost", "::1"}
        if ((url.scheme != "https" and not local_http) or not url.host or url.userinfo
                or url.query or url.fragment or url.path not in {"", "/"}):
            raise ValueError("api_base 需要 HTTPS 根地址；本机回环地址允许 HTTP")
        return str(url).rstrip("/")

    @field_validator("collection_id", "subscription_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        """将运营者填写的来源标识规范为 UUID，空字符串表示未指定来源。"""
        return str(UUID(value)) if value else ""

    @model_validator(mode="after")
    def validate_search_scope(self) -> MemeMeowSettings:
        """拒绝互斥的来源配置，使搜索范围在请求之前明确确定。"""
        if self.collection_id and self.subscription_id:
            raise ValueError("collection_id 与 subscription_id 只能填写一个")
        if self.search_scope == "personal" and (self.collection_id or self.subscription_id):
            raise ValueError("personal 搜索范围不能同时指定合集或订阅")
        return self


class MemeMeowConfig(PluginConfigBase):
    """向 MaiBot 声明带有版本号的 plugin 配置节，支持配置生成和更新。"""

    model_config = ConfigDict(hide_input_in_errors=True)
    plugin: MemeMeowSettings = Field(default_factory=MemeMeowSettings)


@asynccontextmanager
async def operation_timeout(stage: str):
    """限制一次操作的总时间，超时时报告明确阶段并取消本次全部网络请求。"""
    try:
        async with asyncio.timeout(50):
            yield
    except TimeoutError:
        raise MemeMeowError("operation_timeout", stage, timeout_seconds=50) from None


@dataclass(frozen=True)
class Candidate:
    """保留检索所得的完整媒体地址与公开语境，供当前聊天选择和认证下载。"""

    meme_id: str
    media_url: str
    description: dict[str, Any]

    def public(self) -> dict[str, Any]:
        """返回模型选择图片所需的信息，媒体地址由插件内部保存。"""
        return {"meme_id": self.meme_id, **self.description}


class MemeMeowClient:
    """在一次操作中复用异步 HTTP 连接，执行搜索、语境读取和有大小限制的图片下载。"""

    def __init__(self, config: MemeMeowSettings) -> None:
        """保存配置快照并检查凭据，所有认证请求仅访问配置的服务。"""
        self.config = config
        key = config.api_key.strip()
        if not key:
            raise MemeMeowError("api_key_missing", "configuration", message="请在插件配置中填写 api_key")
        if not key.isascii() or any(char.isspace() or ord(char) < 32 for char in key):
            raise MemeMeowError("api_key_format_invalid", "configuration", message="api_key 不能包含空白字符")
        self.key = key
        self.http = httpx.AsyncClient(
            base_url=config.api_base,
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(config.request_timeout, connect=min(10, config.request_timeout)),
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self) -> MemeMeowClient:
        """进入一次业务操作，共享本次操作的 HTTP 连接。"""
        await self.http.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        """在成功、失败或取消时释放本次操作的 HTTP 连接。"""
        await self.http.__aexit__(*args)

    def _error(self, code: str, stage: str, **details: Any) -> MemeMeowError:
        """保留服务诊断字段，并从错误文本中删除当前 API key。"""
        sanitized = json.dumps(details, ensure_ascii=False, default=str).replace(self.key, "[密钥已隐藏]")
        return MemeMeowError(code, stage, **json.loads(sanitized))

    async def _check(self, response: httpx.Response, stage: str) -> None:
        """HTTP 状态异常时保留原始错误类别、消息和 Retry-After。"""
        if response.is_success:
            return
        await response.aread()
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type == "application/json" or content_type.endswith("+json"):
            try:
                detail = response.json()
            except ValueError as exc:
                raise self._error("invalid_json_response", stage, http_status=response.status_code,
                                  message=str(exc)) from None
        else:
            detail = {"message": response.text[:4000], "content_type": content_type}
        raise self._error("http_error", stage, http_status=response.status_code, response=detail,
                          retry_after=response.headers.get("retry-after"))

    async def _json(self, method: str, path: str, stage: str, **kwargs: Any) -> dict[str, Any]:
        """发起真实 JSON 请求，网络异常与格式异常均保留发生阶段。"""
        try:
            response = await self.http.request(method, path, **kwargs)
            await self._check(response, stage)
            value = response.json()
        except httpx.RequestError as exc:
            raise self._error(type(exc).__name__, stage, message=str(exc)) from None
        except ValueError as exc:
            raise self._error("invalid_json_response", stage, message=str(exc)) from None
        if not isinstance(value, dict):
            raise self._error("invalid_json_response", stage, message="响应需要 JSON 对象")
        logger.bind(mememeow=True).info("阶段={} HTTP={}", stage, response.status_code)
        return value

    async def search(self, query: str, tag_filter: dict[str, list[str]] | None = None) -> list[Candidate]:
        """按运营者规定的范围检索并读取各项语境，任一接口异常立即报告原因。"""
        if len(query) > 500 or (not query.strip() and not any((tag_filter or {}).values())):
            raise MemeMeowError("query_invalid", "search", message="请提供最多 500 个字符的查询或有效标签")
        payload: dict[str, Any] = {
            "query": query, "tag_filter": tag_filter or {}, "n_results": self.config.candidate_count,
            "llm_enhance": self.config.llm_enhance, "search_scope": self.config.search_scope,
        }
        for field in ("collection_id", "subscription_id"):
            if value := getattr(self.config, field):
                payload[field] = value
        result = await self._json("POST", "/search", "search", json=payload)
        if not isinstance(result.get("result_media"), list):
            raise MemeMeowError("invalid_search_response", "search", message="响应缺少 result_media 数组")
        tasks = [asyncio.create_task(self._candidate(item)) for item in result["result_media"]]
        try:
            return await asyncio.gather(*tasks)
        finally:
            # 任一语境请求异常时取消其余请求，保留原始错误并完成连接清理。
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _candidate(self, item: dict[str, Any]) -> Candidate:
        """读取单个候选的公开语境，并验证媒体地址与图片 ID 的对应关系。"""
        meme_id = str(UUID(item["meme_id"]))
        media_url = self.media_url(item["media_url"], meme_id)
        metadata = await self._json("GET", "/images/metadata", "metadata", params={"meme_id": meme_id})
        if metadata.get("meme_id") != meme_id:
            raise MemeMeowError("invalid_metadata_response", "metadata", message="响应图片 ID 不一致")
        return Candidate(meme_id, str(media_url), {
            "name": item.get("name", metadata.get("display_name", "")),
            "tags": item.get("tags", metadata.get("meme_context", {}).get("tags", [])),
            "meme_context": metadata.get("meme_context", {}),
            "search_source": item.get("search_source"),
            "score": item.get("score"),
        })

    @staticmethod
    def media_url(value: str, meme_id: str) -> httpx.URL:
        """仅接受同一服务的相对媒体地址，完整保留订阅与子合集查询参数。"""
        url = httpx.URL(value)
        if (url.is_absolute_url or url.host or not url.path.startswith("/") or url.fragment
                or url.path.rsplit("/", 1)[-1] != meme_id):
            raise MemeMeowError("media_url_invalid", "download", message="媒体地址必须与检索图片对应")
        return url

    async def download(self, candidate: Candidate) -> tuple[bytes, str]:
        """认证下载当前候选，限制字节数并完整保留 PNG、JPEG、GIF、WebP 和 APNG 数据。"""
        url = self.media_url(candidate.media_url, candidate.meme_id)
        if self.config.max_edge:
            url = url.copy_set_param("max_edge", self.config.max_edge)
        try:
            async with self.http.stream("GET", url) as response:
                await self._check(response, "download")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type not in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/apng"}:
                    raise self._error("unexpected_media_type", "download", content_type=content_type)
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > self.config.max_media_bytes:
                        raise self._error("media_too_large", "download", limit=self.config.max_media_bytes)
                    data.extend(chunk)
        except httpx.RequestError as exc:
            raise self._error(type(exc).__name__, "download", message=str(exc)) from None
        if not data:
            raise self._error("empty_media_response", "download")
        logger.bind(mememeow=True).info("阶段=下载完成 图片={} 字节数={}", candidate.meme_id, len(data))
        return bytes(data), content_type


def append_meme(messages: list[dict[str, Any]], candidate: Candidate, data: bytes) -> list[dict[str, Any]]:
    """向同一次回复追加表情消息，保留已有文字、引用、@ 和全部附件。"""
    if not messages or not data:
        raise MemeMeowError("reply_content_missing", "before_send", message="回复消息和图片数据不能为空")
    result = deepcopy(messages)
    result.append({"segments": [{
        "type": "emoji", "data": candidate.description.get("name") or "MemeMeow 表情包",
        "binary_data_base64": base64.b64encode(data).decode("ascii"),
    }], "quote_previous": False})
    return result


class MemeMeowPlugin(MaiBotPlugin):
    """将检索和认证下载接入 MaiBot；候选记录按聊天流隔离并在五分钟后过期。"""

    config_model = MemeMeowConfig

    def __init__(self) -> None:
        """初始化有容量和有效期限制的候选记录，以及属于本插件的日志处理器。"""
        super().__init__()
        self.candidates: TTLCache = TTLCache(maxsize=512, ttl=300)
        self.log_sink: int | None = None

    async def on_load(self) -> None:
        """在插件专属目录建立轮转日志，保留正常配置入口供运营者填写凭据。"""
        path = self.ctx.paths.data_dir / "logs"
        path.mkdir(parents=True, exist_ok=True)
        self.log_sink = logger.add(
            path / "mememeow.log", filter=lambda record: record["extra"].get("mememeow", False),
            rotation="1 MB", retention="3 days", encoding="utf-8", backtrace=False, diagnose=False,
        )
        self.ctx.logger.info("MemeMeow 已加载，正常回复表情扩展可用")

    async def on_unload(self) -> None:
        """清理本插件的候选记录与日志处理器，不影响其他插件的日志。"""
        self.candidates.clear()
        if self.log_sink is not None:
            logger.remove(self.log_sink)
            self.log_sink = None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置更新后清除旧搜索范围的候选，下一次操作读取最新配置。"""
        if scope == "self":
            self.candidates.clear()
            self.ctx.logger.info("MemeMeow 配置已更新")

    def _settings(self, stream_id: str) -> MemeMeowSettings:
        """按真实聊天流检查启用状态和运营者权限，返回本次操作的独立配置。"""
        config = self.config.plugin.model_copy(deep=True)
        if not config.enabled:
            raise MemeMeowError("plugin_disabled", "authorization")
        if not stream_id:
            raise MemeMeowError("chat_context_missing", "authorization")
        if config.allowed_streams and stream_id not in config.allowed_streams:
            raise MemeMeowError("chat_not_allowed", "authorization")
        return config

    async def _search(self, stream_id: str, query: str, tag_filter: dict | None = None) -> list[Candidate]:
        """检索真实图片并在当前聊天保存候选，供后续回复或独立发送选择。"""
        settings = self._settings(stream_id)
        async with MemeMeowClient(settings) as client:
            found = await client.search(query, tag_filter)
        self._ensure_current(stream_id, settings)
        for candidate in found:
            self.candidates[stream_id, candidate.meme_id] = candidate
        return found

    def _ensure_current(self, stream_id: str, settings: MemeMeowSettings) -> None:
        """耗时请求结束后重新检查权限和配置，阻止使用已变更范围的结果。"""
        if self._settings(stream_id) != settings:
            raise MemeMeowError("configuration_changed", "authorization", message="配置已更新，请重新操作")

    def _selected(self, stream_id: str, meme_id: str) -> Candidate:
        """读取当前聊天实际检索到的候选，拒绝过期或来自其他聊天的选择。"""
        candidate = self.candidates.get((stream_id, str(UUID(meme_id))))
        if candidate is None:
            raise MemeMeowError("candidate_expired_or_unknown", "selection", message="请在当前聊天重新搜索")
        return candidate

    @HookHandler(
        "maisaka.planner.before_request",
        name="proactive_meme_prompt",
        description="根据运营者设置，在 Planner 决定回复方式前提供积极使用表情包的建议",
        error_policy=ErrorPolicy.ABORT,
    )
    async def proactive_meme_prompt(self, *, hook_name: str = "", **kwargs: Any) -> dict:
        """在允许的会话注入系统提示，返回当前请求的独立副本并保留其余上下文。"""
        settings = self.config.plugin
        stream_id = kwargs["session_id"]
        if (not settings.enabled or not settings.auto_reply or not settings.proactive_memes
                or not settings.api_key.strip() or not stream_id
                or (settings.allowed_streams and stream_id not in settings.allowed_streams)):
            return {"action": "continue"}
        items = kwargs["items"]
        if any(item["meta"]["item_id"] == PROACTIVE_PROMPT_ID for item in items):
            return {"action": "continue"}
        prompt = {
            "item_type": "SystemMessageItem",
            "meta": {"item_id": PROACTIVE_PROMPT_ID, "logical_turn_id": None,
                     "timestamp": datetime.now(UTC).isoformat()},
            "parts": [{"type": "text", "text": PROACTIVE_PROMPT}],
        }
        modified = deepcopy(kwargs)
        modified["items"].insert(0, prompt)
        logger.bind(mememeow=True).debug("阶段=Planner提示 已注入积极使用表情包建议")
        return {"action": "continue", "modified_kwargs": modified}

    @Tool(
        "mememeow_search",
        brief_description="按当前话题、人物、动作、台词、场景、相关梗或情绪搜索 MemeMeow 表情包。"
                          "阅读候选的 meme_context 后选择图片，可在 reply 的 "
                          "cc.mememeow.maibot.meme 扩展填写 meme_id 附图，或通过 mememeow_send 独立发送。",
        detailed_description="阅读候选的 meme_context 后选择合适图片。正常回复可用 reply 的 "
                             "cc.mememeow.maibot.meme 扩展附图；独立发图使用 mememeow_send。",
        parameters={
            "query": {"type": "string", "description": "简短描述话题、人物、动作、台词、场景、梗或情绪",
                      "default": ""},
            "tag_filter": {"type": "object", "description": "标签条件：all、any、none", "default": {},
                           "properties": {key: {"type": "array", "items": {"type": "string"}}
                                          for key in ("all", "any", "none")}, "additionalProperties": False},
        },
    )
    async def search_tool(self, query: str = "", tag_filter: dict | None = None, **context: Any) -> dict:
        """向 Planner 返回可读取的候选语境，搜索结果为空时明确报告未找到图片。"""
        async with operation_timeout("search"):
            found = await self._search(context["stream_id"], query, tag_filter)
        candidates = [candidate.public() for candidate in found]
        return {
            "success": True, "candidates": candidates,
            "content": (
                json.dumps({"candidates": candidates}, ensure_ascii=False) if found else "没有找到匹配图片。"
            ),
        }

    @ReplyExtension(
        "meme",
        description="正常聊天回复时，可根据当前话题和准备回复的内容，自主搜索表情包用于接梗、调侃、"
                    "回应、举例或补充表达，加入本次回复。"
                    "提供 query 会自动检索并附上排名第一的图片；也可先调用 mememeow_search，"
                    "阅读图片语境后填写选中的 meme_id。仅在表情包有助于表达时使用。",
        parameters={
            "query": {"type": "string", "default": "", "maxLength": 500,
                      "description": "适合本次回复的话题、人物、动作、台词、场景、梗或情绪；填写后自动搜索"},
            "meme_id": {"type": "string", "default": "", "description": "当前聊天搜索结果中选中的图片 ID"},
        },
        timeout_ms=60000,
    )
    async def reply_meme(self, *, phase: str, parameters: dict, text: str,
                         messages: list[dict], **context: Any) -> dict:
        """根据 Planner 的本次选择检索并生成表情附件，由 MaiBot 统一发送整组回复。"""
        stream_id = context["session_id"]
        settings = self._settings(stream_id)
        if not settings.auto_reply:
            raise MemeMeowError("auto_reply_disabled", phase)
        query, meme_id = parameters.get("query", "").strip(), parameters.get("meme_id", "").strip()
        if bool(query) == bool(meme_id):
            raise MemeMeowError("reply_selection_invalid", phase, message="query 与 meme_id 必须填写其中一个")
        if phase == "prepare":
            if meme_id:
                self._selected(stream_id, meme_id)
            return {}
        if phase != "before_send":
            raise MemeMeowError("reply_phase_invalid", phase)
        async with operation_timeout("before_send"):
            async with MemeMeowClient(settings) as client:
                if meme_id:
                    candidate = self._selected(stream_id, meme_id)
                else:
                    found = await client.search(query)
                    if not found:
                        logger.bind(mememeow=True).info("阶段=回复配图 检索结果为空，保留本次文字回复")
                        return {}
                    candidate = found[0]
                data, _ = await client.download(candidate)
        self._ensure_current(stream_id, settings)
        logger.bind(mememeow=True).info("阶段=附件准备完成 图片={} 等待 MaiBot 发送", candidate.meme_id)
        return {"messages": append_meme(messages, candidate, data)}

    @Tool(
        "mememeow_send", brief_description="使用 mememeow_search 返回的 meme_id，"
                                           "将当前聊天选中的 MemeMeow 表情包独立发送。"
                                           "配合文字回复时优先使用 reply 的表情扩展。",
        detailed_description="使用 mememeow_search 返回的 meme_id。配合文字回复时优先使用 reply 的表情扩展。",
        parameters={"meme_id": {"type": "string", "required": True,
                                "description": "当前聊天搜索结果中的图片 ID"}},
    )
    async def send_tool(self, meme_id: str, **context: Any) -> dict:
        """下载实际候选并检查 MaiBot 的发送确认，失败时明确报告发送阶段。"""
        stream_id = context["stream_id"]
        settings = self._settings(stream_id)
        candidate = self._selected(stream_id, meme_id)
        async with operation_timeout("send"):
            async with MemeMeowClient(settings) as client:
                data, _ = await client.download(candidate)
            self._ensure_current(stream_id, settings)
            result = await self.ctx.send.emoji(base64.b64encode(data).decode("ascii"), stream_id,
                                               return_details=True)
        if not result.get("sent"):
            raise MemeMeowError("message_not_sent", "send", message="MaiBot 未确认表情包发送成功")
        return {"success": True, "content": "表情包已发送。", "message_id": result.get("message_id")}

    @Command("meme_command", description="搜索并发送 MemeMeow 表情包",
             pattern=r"^/meme(?:\s+(?P<query>[\s\S]+))?$")
    async def meme_command(self, **context: Any) -> tuple[bool, str, int]:
        """处理用户主动索图，复用当前聊天的检索和发送流程。"""
        self._settings(context["stream_id"])
        query = context.get("matched_groups", {}).get("query") or ""
        if not query.strip():
            sent = await self.ctx.send.text("使用方法：/meme 终于下班", context["stream_id"])
            if not sent:
                raise MemeMeowError("message_not_sent", "send")
            return True, "使用说明已发送", 1
        async with operation_timeout("command"):
            found = await self._search(context["stream_id"], query)
            if not found:
                sent = await self.ctx.send.text("没有找到匹配图片。", context["stream_id"])
                if not sent:
                    raise MemeMeowError("message_not_sent", "send")
                return True, "没有找到匹配图片", 1
            result = await self.send_tool(found[0].meme_id, **context)
        return True, result["content"], 1


def create_plugin() -> MemeMeowPlugin:
    """由 MaiBot Runner 创建插件实例，配置与上下文在加载阶段注入。"""
    return MemeMeowPlugin()
