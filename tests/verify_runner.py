# 启动独立的真实 MaiBot Runner，核验插件加载、RPC 调用与回复消息校验。

import argparse
import asyncio
import base64
import json
import os
from io import BytesIO
from pathlib import Path

from PIL import Image


async def verify_planner_prompt(supervisor, plugin_id, stream_id):
    """通过真实 Hook 分发与 Runner RPC，验证配置开关、聊天范围及 Host 上下文恢复。"""
    from src.llm_models.payload_content.context_item import (
        CONTEXT_ITEM_SCHEMA_VERSION,
        ContextItemMeta,
        ContextTextPart,
        UserMessageItem,
    )
    from src.plugin_runtime.hook_payloads import deserialize_prompt_items, serialize_prompt_items

    original = [UserMessageItem(meta=ContextItemMeta.create(), parts=(ContextTextPart("聊聊猫猫"),))]
    request = {"session_id": stream_id, "items": serialize_prompt_items(original),
               "item_schema_version": CONTEXT_ITEM_SCHEMA_VERSION, "tool_definitions": [],
               "selected_history_count": 1, "built_message_count": 1, "selection_reason": "插件验收"}
    disabled = await supervisor.invoke_hook("maisaka.planner.before_request", **request)
    assert not disabled.errors and disabled.kwargs == request, disabled.errors
    for overrides, expected in [
        ({}, True), ({"allowed_streams": ["another-chat"]}, False),
        ({"auto_reply": False}, False), ({"proactive_memes": False}, False),
    ]:
        config = {"plugin": {"config_version": "0.1.0", "proactive_memes": True,
                             "api_key": "hook-verification", **overrides}}
        assert await supervisor.notify_plugin_config_updated(
            plugin_id, config_data=config, config_version="0.1.0",
        )
        dispatched = await supervisor.invoke_hook("maisaka.planner.before_request", **request)
        assert not dispatched.errors and not dispatched.aborted, dispatched.errors
        restored = deserialize_prompt_items(dispatched.kwargs["items"], original_items=original)
        assert len(restored) == 1 + int(expected)
        assert restored[-1] is original[0]
        if expected:
            assert restored[0].__class__.__name__ == "SystemMessageItem"
            assert "cc.mememeow.maibot.meme" in restored[0].parts[0].text
            repeated = await supervisor.invoke_hook("maisaka.planner.before_request", **dispatched.kwargs)
            assert not repeated.errors and repeated.kwargs == dispatched.kwargs
    assert await supervisor.notify_plugin_config_updated(
        plugin_id, config_data={"plugin": {"config_version": "0.1.0"}}, config_version="0.1.0",
    )
    print("真实 Runner：Planner 提示注入、配置开关、聊天范围、重复请求与 Host 上下文恢复通过", flush=True)


async def verify(instance: Path, fixture: dict | None) -> None:
    """在专属实例目录运行实际 Host 与 Runner；有测试凭据时检查认证检索和回复附图。"""
    from src.config.config import config_manager
    from src.plugin_runtime.host.component_registry import ComponentTypes
    from src.plugin_runtime.host.hook_spec_registry import HookSpecRegistry
    from src.plugin_runtime.host.reply_extension_schema import validate_parameters
    from src.plugin_runtime.host.reply_extensions import validate_reply_messages
    from src.plugin_runtime.host.supervisor import PluginRunnerSupervisor

    config_manager.initialize()
    from src.maisaka.chat_loop_service import register_maisaka_hook_specs

    hook_specs = HookSpecRegistry()
    register_maisaka_hook_specs(hook_specs)
    supervisor = PluginRunnerSupervisor(
        plugin_dirs=[instance / "plugins"], socket_path=str(instance / "runner.sock"),
        max_restart_attempts=0, runner_spawn_timeout_sec=30, hook_spec_registry=hook_specs,
    )
    plugin_id = "cc.mememeow.maibot"
    stream_id = "mememeow-plugin-e2e"
    try:
        await supervisor.start()
        assert supervisor.get_plugin_load_statuses().get(plugin_id) == "success", (
            supervisor.get_plugin_load_failure_reasons()
        )
        assert not supervisor.get_plugin_config_error(plugin_id)
        entries = supervisor.component_registry.get_components_by_type(ComponentTypes.REPLY_EXTENSION.value)
        extension = next(item for item in entries if item.full_name == plugin_id + ".meme")
        parameters = validate_parameters({"query": "开心"}, extension.parameters_schema)
        prepared = await supervisor.invoke_plugin(
            "plugin.invoke_reply_extension", plugin_id, "meme", {
                "phase": "prepare", "parameters": parameters, "session_id": stream_id,
                "text": "", "messages": [],
            },
        )
        assert prepared.payload == {"success": True, "result": {}}, prepared.payload
        print("真实 Runner：插件加载、组件注册和 prepare 阶段通过", flush=True)
        await verify_planner_prompt(supervisor, plugin_id, stream_id)
        if not fixture:
            return

        config = {"plugin": {
            "config_version": "0.1.0", "api_base": fixture["api_base"],
            "api_key": fixture["api_key"], "search_scope": "personal", "max_edge": 240,
        }}
        assert await supervisor.notify_plugin_config_updated(
            plugin_id, config_data=config, config_version="0.1.0",
        )

        found = await supervisor.invoke_plugin(
            "plugin.invoke_tool", plugin_id, "mememeow_search", {
                "stream_id": stream_id, "tag_filter": {"all": [fixture["marker"]]},
            }, timeout_ms=60000,
        )
        assert found.payload.get("success"), found.payload
        result = found.payload["result"]
        assert result["success"] and result["content"]
        assert {item["meme_id"] for item in result["candidates"]} == set(fixture["images"]), result
        original = [{"segments": [{"type": "text", "data": "终于完成了，真开心"}], "quote_previous": False}]
        for candidate in result["candidates"]:
            transformed = await supervisor.invoke_plugin(
                "plugin.invoke_reply_extension", plugin_id, "meme", {
                    "phase": "before_send", "parameters": {"meme_id": candidate["meme_id"]},
                    "session_id": stream_id, "text": "终于完成了，真开心", "messages": original,
                }, timeout_ms=60000,
            )
            assert transformed.payload.get("success"), transformed.payload
            messages = validate_reply_messages(transformed.payload["result"]["messages"])
            assert messages[:-1] == original
            encoded = messages[-1]["segments"][0]["binary_data_base64"]
            with Image.open(BytesIO(base64.b64decode(encoded, validate=True))) as image:
                expected = fixture["images"][candidate["meme_id"]]
                assert image.format == expected["format"]
                assert image.size == (240, 180)
                assert getattr(image, "n_frames", 1) == expected["frames"]
        automatic = await supervisor.invoke_plugin(
            "plugin.invoke_reply_extension", plugin_id, "meme", {
                "phase": "before_send", "parameters": {"query": "开心"},
                "session_id": stream_id, "text": "终于完成了，真开心", "messages": original,
            }, timeout_ms=60000,
        )
        assert automatic.payload.get("success"), automatic.payload
        assert len(validate_reply_messages(automatic.payload["result"]["messages"])) == 2
        isolated = await supervisor.invoke_plugin(
            "plugin.invoke_reply_extension", plugin_id, "meme", {
                "phase": "prepare", "parameters": {"meme_id": result["candidates"][0]["meme_id"]},
                "session_id": "another-test-chat", "text": "", "messages": [],
            },
        )
        assert not isolated.payload.get("success")
        assert "candidate_expired_or_unknown" in isolated.payload["result"]
        empty = await supervisor.invoke_plugin(
            "plugin.invoke_tool", plugin_id, "mememeow_search", {
                "stream_id": stream_id, "tag_filter": {"all": [fixture["marker"] + "-missing"]},
            }, timeout_ms=60000,
        )
        assert empty.payload.get("success"), empty.payload
        assert empty.payload["result"]["candidates"] == []

        invalid_config = {"plugin": {**config["plugin"], "api_key": "deliberately-invalid-e2e-credential"}}
        assert await supervisor.notify_plugin_config_updated(
            plugin_id, config_data=invalid_config, config_version="0.1.0",
        )
        unauthorized = await supervisor.invoke_plugin(
            "plugin.invoke_tool", plugin_id, "mememeow_search", {"stream_id": stream_id, "query": "开心"},
            timeout_ms=60000,
        )
        assert not unauthorized.payload.get("success")
        reason = json.loads(unauthorized.payload["result"])
        assert reason["http_status"] == 401 and reason["response"]["error"] == "authentication_required"
        assert invalid_config["plugin"]["api_key"] not in unauthorized.payload["result"]
        assert await supervisor.notify_plugin_config_updated(
            plugin_id, config_data=config, config_version="0.1.0",
        )
        expired = await supervisor.invoke_plugin(
            "plugin.invoke_reply_extension", plugin_id, "meme", {
                "phase": "prepare", "parameters": {"meme_id": result["candidates"][0]["meme_id"]},
                "session_id": stream_id, "text": "", "messages": [],
            },
        )
        assert not expired.payload.get("success")
        assert "candidate_expired_or_unknown" in expired.payload["result"]
        print("真实 Runner：搜索、PNG/JPEG/GIF 下载、自主查询附图和聊天隔离通过", flush=True)
        print("真实 Runner：空结果、HTTP 401 原因、密钥脱敏和配置更新清除候选通过", flush=True)
        print("消息已通过 Host 校验；本次验收未向聊天平台发送消息", flush=True)
    finally:
        await supervisor.stop()


def main() -> None:
    """读取独立实例与可选真实测试记录，所有运行文件保存在仓库忽略目录内。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args()
    instance = args.instance.resolve()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8")) if args.fixture else None
    os.environ["MAIBOT_INSTANCE_DIR"] = str(instance)
    os.chdir(instance)
    asyncio.run(verify(instance, fixture))


if __name__ == "__main__":
    main()
