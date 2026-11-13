# antigravity-agy：Hermes Agent 的 Google Antigravity 订阅后端

[English](README.md) | 简体中文

> [!WARNING]
> **已停止维护，请改用 [`antigravity-subscription-directsdk`](https://github.com/soyelmismo/hermes-antigravity-subscription)。**
> 本插件把整段对话拍平成一条 agy 用户消息，而 `agy` 会静默截断超过约 65–69k tokens 的单条用户消息（保留开头）。因此长会话会丢失最新的几轮，模型可能反复打转。`antigravity-subscription-directsdk` 使用常驻 agy 进程、只发送新增消息，没有这个问题。本仓库已归档，只读。

这是一个 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的 model-provider 插件。它驱动本机已登录的官方 [`agy` CLI](https://antigravity.google/docs/cli/install)，让 Hermes 使用你的 **Google Antigravity 订阅额度**。

**agy 只当模型，所有工具都由 Hermes 执行。** agy 以关闭全部内置工具的自定义 agent 运行；Hermes 的工具通过一个只声明、不执行的 MCP 服务器暴露给模型；每次工具调用都会被截获并交回 Hermes，由 Hermes 按自己的审批和沙箱执行。

> **声明**：推理经由 Google 官方的 `agy` 二进制完成，消耗的是 `agy` 当前登录账号的 Antigravity/Gemini 订阅额度。插件从不读取、复制或链接 agy 的凭证文件，登录完全由 `agy` 自己负责。用自动化方式使用订阅客户端可能受 Google 对你账号的条款约束，使用前请自行确认。本项目是独立的社区项目，与 Google、Nous Research 无关。

## 特点

- **官方客户端，不碰凭证**：以 print 模式启动 `agy`，没有 OAuth、token 文件或反向代理。
- **内置工具直接关闭**：自动生成的[自定义 agent](docs/design.md#31-turning-off-agys-built-in-tools-a-markdown-custom-agent) 设置 `excludeDefaultComponents: true`，把 agy 的文件、命令类工具从模型的工具集中去掉，而不是事后拒绝执行；agy 基础提示也从约 24.7k tokens 降到约 1k。
- **原生函数调用**：Hermes 工具通过 agy 的 MCP 调度器调用，参数经过 schema 校验，而不是从模型输出的文本里解析。
- **失败即中止**：agy 一旦尝试 Hermes 清单之外的任何工具，或静默回退到默认 agent，插件立即终止该次运行，请求报错。
- **工具循环更快**：预热 agy 进程让工具循环的下一轮跳过 agy 约 5.5 s 的启动，两步工具任务从 44.5 s 降到 30.2 s（见[性能文档](docs/perf.md)）。
- **流式、usage 与缓存统计**：文本增量实时转发；usage（含缓存命中和思考 tokens）按 OpenAI 格式上报。真实会话中 agy 提示缓存的 token 命中率约 89%。
- **清晰报错**：CLI 缺失、未登录、额度耗尽、超时、工具越权各有独立的错误类型，并展示 agy 自己的报错信息（例如可用模型列表）。
- **支持 `/model` 与 gateway**：可用 `/model <slug> --provider antigravity-agy` 或 `/model` 选择器切换，适用于 CLI、TUI 和消息 gateway。

## 要求

- Hermes Agent **≥ 0.21.4**。
- `agy` CLI（已在 1.2.10–1.2.11 上测试），并以**运行 Hermes 的同一用户**登录：先交互式运行一次 `agy`，再用 `agy -p=/quota` 确认。
- Linux 或 macOS（agy 按 POSIX 进程组管理）。

## 安装

```bash
hermes plugins install Realtyxxx/hermes-plugin-antigravity-agy --no-enable
```

**让 AI 帮你安装？** 把 [docs/install.md](docs/install.md#中文-prompt) 里现成的 prompt 交给它：包含前提检查、安装、校验、冒烟测试、可选的工具执行审计和排错，并带有安全约束（不碰凭证、未经允许不改配置、不重启 gateway）。

或直接克隆到 Hermes home：

```bash
git clone https://github.com/Realtyxxx/hermes-plugin-antigravity-agy ~/.hermes/plugins/antigravity-agy
```

Hermes 根据 `plugin.yaml`（`kind: model-provider`）自动发现插件，不需要修改 `config.yaml`。正在运行的 gateway 需要重启后才会加载新装或更新后的插件代码。

## 使用

单次使用，不改默认模型：

```bash
hermes chat -q "总结 README.md" --provider antigravity-agy --model gemini-3.8-flash-medium
```

交互式会话（CLI、TUI 或消息 gateway）中切换，只作用于当前会话：

```text
/model gemini-3.8-flash-medium --provider antigravity-agy
/model pro --provider antigravity-agy        # 别名
/model                                       # 选择器中选 “Antigravity (agy CLI)”
```

设为默认，在 `~/.hermes/config.yaml` 中：

```yaml
model:
  provider: antigravity-agy
  default: gemini-3.8-flash-medium
```

## 模型

模型列表实时来自 `agy models`，不做过滤；下表是 `agy` 无法查询时使用的回退列表。推理强度是 slug 的一部分。

| Slug                                 | 报告给 Hermes 的上下文窗口 |
| ------------------------------------ | -------------------------- |
| `gemini-3.8-flash-{low,medium,high}` | 1,000,000                  |
| `gemini-3.7-flash-{low,medium,high}` | 1,000,000                  |
| `gemini-3.6-flash-{low,medium,high}` | 1,000,000                  |
| `gemini-3.1-pro-{low,high}`          | 1,000,000                  |
| `claude-sonnet-4-6`                  | 200,000                    |
| `claude-opus-4-6-thinking`           | 200,000                    |
| `gpt-oss-120b-medium`                | 128,000                    |

- 别名：`flash` → `gemini-3.8-flash-medium`，`pro` → `gemini-3.1-pro-high`，`sonnet` → `claude-sonnet-4-6`，`opus` → `claude-opus-4-6-thinking`。
- 不带强度后缀的族名（如 `gemini-3.8-flash`）会映射到 `-medium` 变体，没有则依次用 `-high`、`-low`。
- 额度按模型组（Gemini；Claude 与 GPT）分别计算，有 5 小时窗口和每周上限，用 `agy -p=/quota` 查看。

## 配置

以下均为 Hermes 进程的可选环境变量。

| 变量                          | 默认                        | 作用                                                            |
| ----------------------------- | --------------------------- | --------------------------------------------------------------- |
| `ANTIGRAVITY_AGY_COMMAND`     | `PATH` 中的 `agy`           | 要运行的 agy 可执行文件                                         |
| `ANTIGRAVITY_AGY_PREWARM`     | `2`                         | 每种请求形态预热的 agy 进程数（`0` 关闭预热，全局上限 4）       |
| `ANTIGRAVITY_AGY_PREWARM_TTL` | `300`                       | 未被使用的预热进程最长空闲秒数                                  |
| `ANTIGRAVITY_AGY_CLEANUP`     | `unused`                    | 哪些由插件产生的 agy 会话记录移入回收站：`unused`、`all`、`off` |
| `ANTIGRAVITY_AGY_DATA_DIR`    | `~/.gemini/antigravity-cli` | agy 数据目录（仅用于定位插件自己的会话记录以便清理）            |
| `ANTIGRAVITY_AGY_CAPTURE_WAIT` | `2`                        | 等待一条对得上的 `tools/call` 捕获的秒数，超时则失败关闭       |
| `ANTIGRAVITY_AGY_TRACE_DIR`   | 未设置                      | 把每次 agy 运行的原始 NDJSON 事件流写到这里，用于审计           |

运行 Hermes 的进程（例如 gateway 的 systemd unit）还必须具备 `agy` 本身需要的环境，例如代理变量。

## 工作原理

1. 每个请求创建一个私有工作区，写入自定义 agent `.agents/agents/hermes-relay/agent.md`：`excludeDefaultComponents: true`、`inheritMcp: false`、唯一的 MCP 服务器 `inert_mcp.py`，并把 Hermes 的系统提示和 Hermes 工具目录写成 agent 的系统提示。
2. 对话历史拍平成一条 stream-json 用户消息写入 agy 的 stdin；agy 输出的 NDJSON 转成 OpenAI 格式的 chunk 交给 Hermes。
3. 模型调用 Hermes 工具（`call_mcp_tool` → `hermes/<tool>`）时，`inert_mcp.py` 记录这条 `tools/call` 且不应答。插件等待这一条对得上的捕获，然后终止 agy，并把捕获中的参数交回 Hermes。`step_update` 里的展示文本不会被执行。捕获缺失、读不出或对不上时，运行被终止且请求失败。Hermes 执行交回的调用后发起下一轮请求。
4. 出现任何其他工具步骤，或在 agy 日志中检测到 agent 回退，都会终止运行并让请求报错。

完整细节、实测依据与取舍见 [docs/design.md](docs/design.md)（英文）。

### 审计：确认只有 Hermes 执行工具

```bash
ANTIGRAVITY_AGY_TRACE_DIR=/tmp/agy-trace hermes chat -q "…" --provider antigravity-agy --model gemini-3.8-flash-low
python scripts/audit_trace.py /tmp/agy-trace    # 出现任何非 Hermes 工具步骤时退出码为 1，否则输出 "violations: 0"
```

trace 中包含对话内容，用完请删除。

## 与 `antigravity-subscription-directsdk` 的区别

Hermes 插件目录里已有 [`antigravity-subscription-directsdk`](https://github.com/soyelmismo/hermes-antigravity-subscription)，同样驱动 `agy`，但做法不同：

|                | antigravity-subscription-directsdk                    | antigravity-agy（本插件）                                   |
| -------------- | ----------------------------------------------------- | ----------------------------------------------------------- |
| agy 内置工具   | 保留在默认 agent 中；agy 报告工具步骤时终止运行       | 用自定义 agent 从模型中移除；任何非 Hermes 工具步骤也会终止 |
| agy 基础提示   | agy 默认 agent（本项目实测其基础提示约 24.7k tokens） | relay agent（约 1k tokens）                                 |
| 工具调用协议   | 模型在文本中写 `<tool_call>` 标签，由插件解析         | 通过 MCP 的原生函数调用，参数经 schema 校验                 |
| 凭证           | 把 agy 的 OAuth token 软链接进每个会话的临时 `HOME`   | 从不触碰 agy 的凭证文件                                     |
| agent 回退检测 | —                                                     | 有（agent 定义有误时 agy 会静默回退到默认 agent）           |
| 预热           | —                                                     | 有                                                          |
| 推理强度       | 把 Hermes 的推理强度映射到 slug                       | 由 slug 决定，忽略 `reasoning_effort`                       |
| 平台           | Linux、macOS、Windows                                 | Linux、macOS                                                |

## 已知限制

- **每轮一个工具调用**：agy 串行派发工具，并行调用会拆成多轮请求。
- **只支持文本**：图片会被替换为占位符。
- **三个 agy 基础工具**（`list_resources`、`read_resource`、`manage_task`）无法对模型隐藏；模型若调用它们，本轮请求报错，而不是执行。
- **会话记录**：实际处理过提示的运行仍会在 `~/.gemini/antigravity-cli/` 下留下 agy 会话记录，除非设置 `ANTIGRAVITY_AGY_CLEANUP=all`。
- **内存**：每个空闲预热进程约 416 MB；内存紧张的 gateway 请调低 `ANTIGRAVITY_AGY_PREWARM`。
- **被忽略的参数**：Hermes 的 `reasoning_effort`、`temperature`、`max_tokens`、`response_format` 不生效。
- **`/model` 写法**：`/model antigravity-agy/<slug>`、不带 `--provider` 的裸 slug、只写 `--provider antigravity-agy` 不带模型，都受 Hermes 核心路由限制。
- **上下文窗口是估计值**：agy 的真实上限未公开。

更多见 [docs/design.md § 4](docs/design.md#4-known-limitations)。

## 开发

```bash
uv venv && uv pip install pytest pyyaml requests rich httpx ruamel.yaml
HERMES_AGENT_REPO=~/.hermes/hermes-agent .venv/bin/python -m pytest -q
hermes plugins validate .
```

测试使用假 `agy`（`tests/fake_agy.py`），不消耗额度。Hermes 核心从 hermes-agent 源码目录导入（`HERMES_AGENT_REPO`，默认 `~/.hermes/hermes-agent`）。`scripts/bench.py` 用于真实端到端性能测试（见 [docs/perf.md](docs/perf.md#6-reproducing)）。

## 致谢

架构参考了 Nous Research 的 [`claude-subscription-directsdk`](https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk) 插件：它驱动官方 Claude Code CLI，并用只声明、不执行的 MCP 服务器让 Hermes 掌管工具执行。

## 许可证

[MIT](LICENSE)
