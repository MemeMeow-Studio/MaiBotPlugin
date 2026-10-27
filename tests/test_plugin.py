# 使用真实 SDK 与图片编码检查插件配置、聊天隔离和回复附件，不替换网络或发送接口。

import base64
from copy import deepcopy
from io import BytesIO
from uuid import uuid4

import pytest
from maibot_sdk.types import ToolComponentInfo
from PIL import Image
from pydantic import ValidationError

from plugin import (
    Candidate,
    MemeMeowClient,
    MemeMeowConfig,
    MemeMeowError,
    MemeMeowSettings,
    append_meme,
    create_plugin,
)


def test_sdk_configuration_and_component_registration():
    """真实 SDK 应生成带版本的配置与可读取的回复扩展参数。"""
    plugin = create_plugin()
    plugin.set_plugin_config({})
    assert isinstance(plugin.config, MemeMeowConfig)
    assert plugin.config.plugin.config_version == "0.1.0"
    components = plugin.get_components()
    assert len({item["name"] for item in components}) == len(components)
    extension = next(item for item in components if item["type"] == "REPLY_EXTENSION")
    assert extension["name"] == "meme"
    assert "query" in extension["metadata"]["parameters_schema"]["properties"]
    assert "meme_id" in extension["metadata"]["parameters_schema"]["properties"]
    assert plugin.get_webui_config_schema()["sections"]
    sender = next(item for item in components if item["name"] == "mememeow_send")
    schema = ToolComponentInfo(name=sender["name"], **sender["metadata"]).get_parameters_schema()
    assert schema["required"] == ["meme_id"]


@pytest.mark.parametrize("max_edge", [0, 60, 120, 240, 360, 480, 640])
@pytest.mark.parametrize("as_text", [False, True])
def test_webui_size_choices_preserve_integer_configuration(max_edge, as_text):
    """实际 SDK 接受表单数值文本和 TOML 整数，配置导出仍使用整数。"""
    plugin = create_plugin()
    plugin.set_plugin_config({"plugin": {
        "config_version": "0.1.0", "max_edge": str(max_edge) if as_text else max_edge,
    }})
    assert type(plugin.config.plugin.max_edge) is int
    assert plugin.config.plugin.max_edge == max_edge
    assert type(plugin.config.model_dump()["plugin"]["max_edge"]) is int


@pytest.mark.parametrize("max_edge", [-1, 1, 300, 641, "360.5", 360.5, None, "invalid"])
def test_unsupported_size_choices_are_rejected(max_edge):
    """尺寸类型转换保留候选范围校验，并拒绝非整数与无效输入。"""
    with pytest.raises(ValidationError):
        MemeMeowSettings(max_edge=max_edge)


@pytest.mark.parametrize("url", [
    "http://external.example", "https://example.com/path", "https://user:password@example.com",
    "https://example.com?key=value", "https://example.com#fragment", "//example.com",
])
def test_invalid_api_origin_is_rejected(url):
    """配置阶段拒绝不合要求的认证目标，错误不暴露配置中的 API key。"""
    secret = "credential-only-in-memory"
    with pytest.raises(ValidationError) as error:
        MemeMeowSettings(api_base=url, api_key=secret)
    assert secret not in str(error.value)


@pytest.mark.parametrize("url", ["https://mememeow.cc", "http://127.0.0.1:28275", "http://[::1]:28275"])
def test_supported_api_origins(url):
    """接受公网 HTTPS 服务与明确的本机 HTTP 开发服务。"""
    assert MemeMeowSettings(api_base=url).api_base == url


def test_source_configuration_is_unambiguous():
    """指定来源与个人范围必须满足实际 API 的互斥要求。"""
    source = str(uuid4())
    with pytest.raises(ValidationError):
        MemeMeowSettings(collection_id=source, subscription_id=source)
    with pytest.raises(ValidationError):
        MemeMeowSettings(search_scope="personal", collection_id=source)


def test_media_query_is_preserved_and_external_origin_rejected():
    """订阅下载保留查询参数，外部地址和图片 ID 不一致的地址被拒绝。"""
    meme_id = str(uuid4())
    path = f"/server/collection-subscriptions/{uuid4()}/media/{meme_id}?path=child&revision=2"
    assert str(MemeMeowClient.media_url(path, meme_id)) == path
    for invalid in (f"https://example.com/{meme_id}", f"//example.com/{meme_id}", f"/media/{uuid4()}"):
        with pytest.raises(MemeMeowError, match="media_url_invalid"):
            MemeMeowClient.media_url(invalid, meme_id)


def test_selection_is_bound_to_current_chat():
    """真实候选缓存只允许原聊天读取，配置变化后清除原有候选。"""
    plugin = create_plugin()
    plugin.set_plugin_config({})
    candidate = Candidate(str(uuid4()), "/media/image", {"name": "开心"})
    plugin.candidates["chat-a", candidate.meme_id] = candidate
    assert plugin._selected("chat-a", candidate.meme_id) == candidate
    with pytest.raises(MemeMeowError, match="candidate_expired_or_unknown"):
        plugin._selected("chat-b", candidate.meme_id)


def test_chat_authorization_applies_to_all_entries():
    """运营者限制允许的聊天时，其他聊天应在网络请求之前被拒绝。"""
    plugin = create_plugin()
    plugin.set_plugin_config({"plugin": {"config_version": "0.1.0", "allowed_streams": ["allowed"]}})
    assert plugin._settings("allowed").enabled
    with pytest.raises(MemeMeowError, match="chat_not_allowed"):
        plugin._settings("other")
    with pytest.raises(MemeMeowError, match="chat_context_missing"):
        plugin._settings("")


def test_configuration_change_rejects_inflight_results():
    """实际配置对象变化后，旧范围的请求结果不能继续进入回复或候选记录。"""
    plugin = create_plugin()
    plugin.set_plugin_config({})
    previous = plugin._settings("chat-a")
    plugin.set_plugin_config({"plugin": {"config_version": "0.1.0", "search_scope": "personal"}})
    with pytest.raises(MemeMeowError, match="configuration_changed"):
        plugin._ensure_current("chat-a", previous)


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "GIF"])
def test_reply_preserves_text_mentions_quotes_and_image_bytes(image_format):
    """用实际编码图片验证附图过程，原消息和图片格式均保持完整。"""
    image = BytesIO()
    Image.new("RGB", (32, 24), color=(30, 80, 150)).save(image, format=image_format)
    data = image.getvalue()
    original = [{"segments": [
        {"type": "text", "data": "可算完成了"},
        {"type": "at", "data": {"target_user_id": "test-user"}},
        {"type": "image", "data": "原有附件", "hash": "existing-image"},
    ], "quote_previous": True}]
    baseline = deepcopy(original)
    candidate = Candidate(str(uuid4()), "/media/image", {"name": "松了一口气"})
    result = append_meme(original, candidate, data)
    assert original == baseline
    assert result[:-1] == baseline
    segment = result[-1]["segments"][0]
    decoded = base64.b64decode(segment["binary_data_base64"], validate=True)
    assert decoded == data
    with Image.open(BytesIO(decoded)) as restored:
        assert restored.format == image_format
        assert restored.size == (32, 24)


async def test_reply_parameters_and_disabled_behavior():
    """在 prepare 阶段验证参数要求和关闭状态，无须发起网络请求。"""
    plugin = create_plugin()
    plugin.set_plugin_config({})
    assert await plugin.reply_meme(
        phase="prepare", parameters={"query": "开心"}, text="", messages=[], session_id="chat-a",
    ) == {}
    with pytest.raises(MemeMeowError, match="reply_selection_invalid"):
        await plugin.reply_meme(phase="prepare", parameters={}, text="", messages=[], session_id="chat-a")
    plugin.set_plugin_config({"plugin": {"config_version": "0.1.0", "auto_reply": False}})
    with pytest.raises(MemeMeowError, match="auto_reply_disabled"):
        await plugin.reply_meme(
            phase="prepare", parameters={"query": "开心"}, text="", messages=[], session_id="chat-a",
        )
