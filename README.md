# MemeMeow for MaiBot

麦麦可以根据聊天语境和准备表达的情绪，搜索 MemeMeow 表情包，加入同一次正常回复。用户也可以发送 `/meme 终于下班` 主动索取表情包。

插件使用 MaiBot 的 `ReplyExtension`。麦麦选择配图时，插件检索并认证下载图片，将表情消息交给 MaiBot 与文字一起处理；文字、引用、@ 和原有附件完整保留。是否使用表情由麦麦决定，用户无须每次发出命令。

运行要求：Python 3.12+、MaiBot 1.3.15、`maibot-plugin-sdk` 2.16.0。当前验证范围为 MaiBot 1.3 系列、SDK 2.16 系列。许可证为 AGPL-3.0-or-later。

## 安装与配置

将本仓库放入自己的 MaiBot 实例 `plugins/` 目录，目录名称使用 `MaiBot-MemeMeow`。启动或通过 WebUI 重载插件。MaiBot 根据 `_manifest.json` 安装运行依赖，并根据插件配置模型生成 `config.toml`。

在 WebUI 插件配置中填写机器人账户的 `api_key`，确认 `auto_reply` 已经启用。也可以编辑生成的 `config.toml` 后重载插件：

```toml
[plugin]
config_version = "0.1.0"
enabled = true
api_base = "https://mememeow.cc"
api_key = "填写机器人账户的 API key"
auto_reply = true
allowed_streams = []
search_scope = "all"
collection_id = ""
subscription_id = ""
candidate_count = 5
llm_enhance = false
max_edge = 480
request_timeout = 20.0
max_media_bytes = 8388608
```

`api_key` 使用 MemeMeow 账户签发的 API key。该账户决定插件可以访问的个人图库、合集和订阅，以及对应的服务额度。使用专门的机器人账户可以独立管理机器人能够发送的图片范围。

| 配置 | 用途 |
| --- | --- |
| `api_base` | 服务根地址，公网服务使用 HTTPS；本机回环地址允许 HTTP |
| `auto_reply` | 允许麦麦在正常回复中主动搜索和附上表情包 |
| `allowed_streams` | 允许调用插件的聊天流 ID；空数组允许全部聊天 |
| `search_scope` | `all` 使用账户全部可用来源；`personal` 仅使用个人图库 |
| `collection_id` | 限定本人合集的 UUID |
| `subscription_id` | 限定订阅的 UUID；与 `collection_id` 互斥 |
| `candidate_count` | 每次返回的候选数量，范围 1–10 |
| `llm_enhance` | 是否使用 MemeMeow 的 LLM 增强搜索 |
| `max_edge` | 最长边可选 60、120、240、360、480、640；0 下载原图 |
| `request_timeout` | 单次请求超时，最多 40 秒；完整操作最多 50 秒 |
| `max_media_bytes` | 单张下载大小上限，默认 8 MiB，最多 16 MiB |

`personal` 范围不能同时指定 `collection_id` 或 `subscription_id`。候选按聊天分别保存五分钟，配置更新后清除。配置和凭据均由安装实例管理，`config.toml` 已加入 Git 忽略规则。

## 聊天中的使用方式

正常回复时，麦麦的 `reply` 工具会获得 `cc.mememeow.maibot.meme` 参数。麦麦根据当前情绪填写查询，例如：

```json
{
  "plugin_options": {
    "cc.mememeow.maibot.meme": {
      "query": "终于完成工作，开心又松了一口气"
    }
  }
}
```

插件会搜索并使用排名第一的结果。麦麦也可以先调用 `mememeow_search`，阅读候选的 `meme_context`，随后在回复扩展中填写选中的 `meme_id`。`query` 与 `meme_id` 每次填写一个。

`mememeow_search` 支持 `query` 和 `tag_filter`；标签条件包括 `all`、`any`、`none`。这些工具通过 MaiBot 的工具发现机制提供。`mememeow_send` 使用当前聊天已经检索到的 `meme_id` 单独发送图片，并检查 MaiBot 的发送确认。

主动索图示例：

```text
/meme 开心到飞起
/meme 不想上班
```

搜索成功且没有匹配图片时，正常回复保留文字。认证、网络、语境读取、下载等错误会终止本次操作，并报告具体阶段、HTTP 状态和服务错误类别。正常回复的附件准备完成后，最终发送结果由 MaiBot 判定。

插件支持 PNG、JPEG 和 GIF，保留 GIF 动画。媒体通过 Bearer 认证下载为图片数据，订阅路径与查询参数完整保留；下载请求限定在配置的服务中，不跟随重定向。

日志位于 MaiBot 为插件提供的 `data_dir/logs/mememeow.log`，每个文件达到 1 MB 后轮转，保留三天。MaiBot 的 Runner 日志保存组件调用异常。

## 开发与验证

```bash
mkdir -p .local-test/tmp
TMPDIR="$PWD/.local-test/tmp" uv sync --group integration
TMPDIR="$PWD/.local-test/tmp" uv run --no-sync ruff check .
TMPDIR="$PWD/.local-test/tmp" uv run --no-sync pytest -q
```

本地 Runner 验证使用完全独立的实例目录：

```bash
uv run --no-sync maibot init --directory .local-test/host
ln -s "$PWD" .local-test/host/plugins/MaiBot-MemeMeow
TMPDIR="$PWD/.local-test/tmp" uv run --no-sync python tests/verify_runner.py --instance .local-test/host
```

真实服务验收使用自己创建的测试账户、已完成处理和索引的 PNG、JPEG、动画 GIF，以及私有的测试记录文件。文件结构如下，其中 `marker` 是三张图片共同具有的唯一标签：

```json
{
  "api_base": "http://127.0.0.1:28275",
  "api_key": "填写测试账户的 API key",
  "marker": "本次验收专用标签",
  "images": {
    "图片的 UUID": {"format": "PNG", "frames": 1},
    "另一张图片的 UUID": {"format": "JPEG", "frames": 1},
    "动画图片的 UUID": {"format": "GIF", "frames": 2}
  }
}
```

将记录保存在 `.local-test/fixture.json`，限制文件读取权限后执行：

```bash
chmod 600 .local-test/fixture.json
TMPDIR="$PWD/.local-test/tmp" uv run --no-sync python tests/verify_runner.py --instance .local-test/host --fixture .local-test/fixture.json
```

此入口启动真实 Host 与 Runner，通过实际 RPC 调用和 HTTP 服务完成搜索、下载、回复消息校验，并验证认证错误、聊天隔离与配置更新。测试记录中的图片应当为 400×300，验收会检查缩放后的 240×180 图片及动画帧数。该入口不会调用聊天平台发送接口。

2026-10-27 已完成验证：19 项 pytest 检查和 Ruff 检查通过；MaiBot 1.3.15 的独立 Runner 成功加载插件，真实开发服务中的搜索、语境读取、PNG/JPEG/GIF 下载、GIF 两帧保留、查询配图和 Host 消息校验通过。真实 HTTP 401 原因、空结果、聊天隔离和配置更新后的候选清理也已验证。

## 发布

当前仓库仅在本地开发。Manifest 的 `urls.repository` 预留为 `https://github.com/MemeMeow-Studio/MaiBot-MemeMeow`，发布前需要确认公开仓库地址并同步修改该字段。

1. 在确认的公开 GitHub 仓库中发布插件代码，根目录包含 `_manifest.json`、`plugin.py`、`LICENSE` 和本文件。
2. 在实际聊天平台完成文字配图、主动索图、GIF 和发送回执验收，并验证使用的 Planner 能够自主选择回复扩展。
3. 将 Manifest 与 `pyproject.toml` 的版本号设为相同的三段式版本，创建对应 Tag 和 GitHub Release，例如 `0.1.0` 与 `v0.1.0`。
4. 按照 [MaiBot 官方发布说明](https://docs.maibot.chat/plugin/submission.md) 向官方插件索引提交添加申请。审核通过后，用户可以通过 WebUI 插件市场安装。

插件发布后，每次更新均需要验证对应 MaiBot 与 SDK 版本，并同步维护 Manifest 的兼容范围。实际聊天平台的发送结果与模型选择行为需要在接入环境中验证。
