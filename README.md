<div align="center">

# forge

**面向真实仓库的 Local Coding Agent 与 Loop Engineer**

forge 不只执行一次 prompt → tool → prompt 的 agent run；它把复杂编码目标放进一个**可验证、可恢复、可审计的外层工程循环**：建立任务与验收合同、复现问题、执行修复、独立验证、检测停滞，并在需要时转交人工。

接上一个模型 provider 后，forge 就能在本地仓库中读代码、跑命令、改文件、保留运行证据，并把有价值的上下文沉淀成本地记忆。

</div>

<p align="center">
  <img src="assets/screenshots/forge-tui-intro.png" alt="forge TUI 启动界面" width="960">
</p>

---

## forge 是什么

forge 是一个在仓库上下文中运行的本地 coding agent，也是一个面向长期编码目标的 **Loop Engineer**。它把模型能力放进受约束的工程闭环：模型负责探索与修改，合同和 verifier 负责判断，持久化 task state 负责续接，明确的预算、停滞信号与人工升级负责控制边界。

一次 agent run 与 durable task loop 会被拆成几个可观察的部分：

- **provider profile**：决定调用哪个模型、哪个 endpoint、用什么协议。
- **context**：把系统提示、仓库信息、skills、记忆和最近对话装进 prompt。
- **tools**：文件读取、搜索、shell、写文件、patch、子 agent 都走统一工具协议。
- **approval / sandbox**：写操作和 shell 命令可以被审批或沙箱限制。
- **session / run evidence**：对话、事件流、trace、report 都写到本地 `.forge/`。
- **memory / dream**：把 daily log 整理成长期 topic，下次 session 可以继续用。
- **Loop Engineer**：将目标、冻结的验收合同、基线复现、Agent 尝试、独立验证与人工升级组织成可续接的闭环。

forge 关注本地 coding agent 的工程边界：配置清楚、任务可验证可续接、结果可复盘。

## 界面

TUI 直接连接同一个 runtime。输入框、工具结果、状态栏、slash command 和补全都来自当前 session。

| 工具和子 agent | Skills、help 和命令补全 |
| --- | --- |
| ![forge TUI 工具表](assets/screenshots/forge-tui-tools.png) | ![forge TUI skills 和 help](assets/screenshots/forge-tui-skills-help.png) |

| Memory 和 durable topics | Slash command 工作区 |
| --- | --- |
| ![forge TUI memory 和 skills](assets/screenshots/forge-tui-memory-skills.png) | ![forge TUI slash command 补全](assets/screenshots/forge-tui-latest.png) |

## 安装

要求：Python 3.10+，以及至少一个可用的模型 provider key。

一键安装：

```bash
curl -fsSL https://raw.githubusercontent.com/zhangjiahaoaaa/forge/main/install.sh | bash
```

源码安装：

```bash
git clone https://github.com/zhangjiahaoaaa/forge.git
cd forge
pip install -e .
```

开发 checkout 里也可以直接跑：

```bash
uv run forge
```

## 配置 provider

forge 启动前先解析一个 **provider profile**。一个 profile 主要由四项组成：

| 字段 | 作用 |
| --- | --- |
| `protocol` | 请求协议，目前支持 `openai` 和 `anthropic`。 |
| `api_key` | 发给 provider 的 key。 |
| `base_url` | provider endpoint。 |
| `model` | 本次请求使用的模型名。 |

配置合并优先级是：

```text
CLI 参数 > 环境变量 > 项目 .forge.toml > 全局 ~/.config/forge/config.toml > 代码默认值
```

### 方式一：项目 `.forge.toml`

这是最推荐的配置方式，适合每个仓库独立指定 provider：

```bash
cp .forge.toml.example .forge.toml
$EDITOR .forge.toml
```

`.forge.toml` 默认被 `.gitignore` 忽略，不要把真实 key 提交进 git。

最小可用示例：

```toml
provider = "deepseek"

[providers.deepseek]
protocol = "anthropic"
api_key = "sk-..."
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-pro"

[providers.openai]
protocol = "openai"
api_key = "sk-..."
base_url = "https://www.right.codes/codex/v1"
model = "gpt-5.4"

[providers.anthropic]
protocol = "anthropic"
api_key = "sk-ant-..."
base_url = "https://www.right.codes/claude/v1"
model = "claude-sonnet-4-6"
```

注意：`provider = "deepseek"` 只是选择 profile 名字，真正决定请求格式的是
`protocol`。例如 DeepSeek 可以通过 Anthropic-compatible endpoint 使用，所以这里写
`protocol = "anthropic"`。

### 方式二：环境变量

不想把 key 写进 TOML 时，用环境变量：

```bash
export FORGE_PROVIDER=deepseek
export DEEPSEEK_API_KEY=sk-...
export DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic
export DEEPSEEK_MODEL=deepseek-v4-pro

forge
```

常用 provider 变量：

| Provider | 变量 |
| --- | --- |
| DeepSeek | `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL` |
| OpenAI-compatible | `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` |
| Anthropic-compatible | `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL` |

也可以用通用覆盖变量：

```bash
export FORGE_API_KEY=sk-...
export FORGE_BASE_URL=https://api.openai.com/v1
export FORGE_MODEL=gpt-5.4
```

### 方式三：命令行临时覆盖

临时换 provider 或模型：

```bash
forge --provider openai --model gpt-5.4 --base-url https://api.openai.com/v1
forge --provider deepseek --approval ask --max-steps 80
forge --config /path/to/custom.toml --cwd /path/to/repo
```

完整配置说明见 [docs/configuration.md](docs/configuration.md)。

## 启动

常用入口：

```bash
forge                              # 默认 Textual TUI
forge --repl                       # 普通终端 REPL
forge "找出测试失败的根因"          # one-shot 任务
forge goal "修复登录接口空指针"      # 创建并执行可持久化任务循环
forge --resume latest              # 续接最近 session
forge --cwd /path/to/repo          # 指定工作目录
```

常用运行参数：

```bash
forge --approval ask               # shell / 写文件前询问
forge --approval auto              # 普通操作自动通过
forge --approval never             # 非交互模式
forge --sandbox best_effort        # 尽量隔离 shell 命令
forge --no-auto-dream              # 关闭后台 memory 整合
```

## 日常用法

进入 TUI 或 REPL 后可以直接输入自然语言，也可以用 slash command：

```text
> /help
> /skills
> 找出测试失败的根因
> /goal 修复登录接口空指针
> /plan 重构 provider 配置加载逻辑
> /review
> /test tests/test_config.py
> /remember 这个项目用 DeepSeek 的 Anthropic-compatible endpoint
> /dream
```

常用命令：

| 命令 | 作用 |
| --- | --- |
| `/help` | 查看内置命令。 |
| `/skills` | 列出可用 skills。 |
| `/session` | 查看当前 session、events、run 路径。 |
| `/history` | 列出历史 session。 |
| `/resume latest` | 续接最近 session。 |
| `/context` | 查看 prompt context 使用情况。 |
| `/usage` | 查看 provider、model、token 元数据。 |
| `/memory` | 查看 durable memory 索引。 |
| `/working-memory` | 查看当前 session 工作记忆。 |
| `/remember <text>` | 保存一条 durable note 到 daily log。 |
| `/dream` | 把 daily log 整合成 durable memory topics。 |
| `/goal <目标>` | 创建可持久化任务、生成默认验收合同并运行自动循环。 |
| `/plan <topic>` | 进入 plan mode。 |
| `/plan-exit` | 退出 plan mode。 |
| `/agents` | 查看子 agent 状态。 |
| `/model <name>` | 当前 session 临时切模型。 |
| `/compact` | 压缩较早的对话历史。 |
| `/clear` | 开一个新的空 session。 |
| `/exit` | 退出 forge。 |

### Loop Engineer：Durable task loop

`forge goal "<目标>"` 和 `/goal <目标>` 是 Forge 的 Loop Engineer 入口：它们会创建一个可恢复的任务，冻结默认验收合同，再按“基线复现 → Agent 修复 → 独立验证 → 继续、停止或人工升级”的闭环执行。
如果仓库中发现 pytest 测试，默认以 `python -m pytest -q` 作为复现和验收，并禁止修改已发现的测试文件；未发现测试时，才回退到 Python 语法检查。

默认预算为最多 4 个 cycle、80 个工具步骤和 30 分钟。任务可能完成、失败、阻塞或转为等待人工；默认合同未能复现自然语言目标时，Forge 会要求补充目标级测试或手写合同，而不会把任务误报为已解决。运行状态和验收记录保存在 `.forge/tasks/<task_id>/`，可用 `forge task show <task_id>` 查看。

## forge 能做什么

| 能力 | 说明 |
| --- | --- |
| TUI / REPL / one-shot | 同一个 runtime，通过不同入口使用。 |
| 工具执行 | 文件列表、读文件、搜索、shell、写文件、patch、ask_user、子 agent、todo。 |
| Plan mode | 先读代码和拆计划，再进入可写执行阶段。 |
| 子 agent | 启动 bounded Explore / Worker 任务。 |
| Skills | 复用 `/review`、`/test`、`/commit`、`/simplify` 等工作流。 |
| Memory | working memory、daily logs、durable topics、auto-dream。 |
| Evidence | session JSON、event stream、run trace、task state、report。 |
| Sandbox | 对 `run_shell` 做可选隔离。 |

## 本地文件

| 数据 | 路径 |
| --- | --- |
| 项目配置 | `.forge.toml` |
| 全局配置 | `~/.config/forge/config.toml` |
| 会话历史 | `.forge/sessions/<id>.json` |
| 事件流 | `.forge/sessions/<id>.events.jsonl` |
| 运行证据 | `.forge/runs/<run_id>/` |
| Durable tasks 与验收记录 | `.forge/tasks/<task_id>/` |
| 记忆索引 | `.forge/memory/MEMORY.md` |
| Daily logs | `.forge/memory/logs/YYYY/MM/YYYY-MM-DD.md` |
| Durable topics | `.forge/memory/topics/*.md` |
| 用户 skills | `~/.forge/skills/<name>/SKILL.md` |
| 项目 skills | `skills/<name>/SKILL.md` 或 `.forge/skills/<name>/SKILL.md` |

## 项目结构

```text
forge/
├── cli.py                 # CLI 参数、启动模式、REPL 命令
├── config/                # provider profile、TOML、env 解析
├── core/                  # runtime、engine、session、workers、context
├── features/              # memory、skills、sandbox
├── providers/             # OpenAI-compatible / Anthropic-compatible client
├── tools/                 # tool registry 和具体工具
├── tui/                   # Textual TUI
└── evaluation/            # run evidence、metrics、evaluation helpers
```

## 测试

```bash
pip install -e ".[dev]"
pytest tests/ -q

# 真实 provider 烟测需要 key
FORGE_LIVE_SMOKE=1 pytest tests/test_release_smoke.py -q
```

## 文档

| 入口 | 内容 |
| --- | --- |
| [配置](docs/configuration.md) | provider profile、`.forge.toml`、环境变量和 sandbox 配置。 |
| [分层记忆 + auto-dream](docs/memory.md) | working memory、daily logs、durable topics 和后台整合。 |
| [Skills](docs/skills.md) | `SKILL.md` 目录结构、内置技能和自定义 workflow。 |
| [Sandbox](docs/sandbox.md) | `run_shell` 隔离模式、backend 选择和文件系统边界。 |

### v3 发布包

| 入口 | 内容 |
| --- | --- |
| [Release pack](release/v3/README.md) | v3 发布材料入口。 |
| [Changelog](release/v3/CHANGELOG.md) | v3 变更摘要。 |
| [Review pack](release/v3/REVIEW.md) | 项目 pitch、架构地图、边界和评审材料。 |
| [Testing](release/v3/TESTING.md) | v3 测试范围和执行摘要。 |
| [真人场景测试包](release/v3/testing/README.md) | 50 个真实使用场景的测试入口。 |
| [测试设计](release/v3/testing/01-test-design.md) | 场景设计、覆盖面和验收口径。 |
| [执行记录](release/v3/testing/02-execution-record.md) | 全量执行结果和失败修复记录。 |
| [Runner 与证据](release/v3/testing/03-runner-and-evidence.md) | 测试 runner、输出目录和证据文件说明。 |
| [场景检查清单](release/v3/testing/04-scenario-checklist.md) | 50 个场景的逐项状态。 |

### v3 学习文档

按这个顺序读，能从整体架构一路落到模块和测试：

| 顺序 | 文档 |
| --- | --- |
| 0 | [阅读索引](release/v3/learning/00-reading-map.md) |
| 1 | [总体架构](release/v3/learning/01-overall-architecture.md) |
| 2 | [Runtime 和 Engine](release/v3/learning/02-runtime-engine.md) |
| 3 | [上下文、记忆和压缩](release/v3/learning/03-context-memory-compact.md) |
| 4 | [工具、权限和沙箱](release/v3/learning/04-tools-permissions-sandbox.md) |
| 5 | [子 agent、计划模式和 Todo](release/v3/learning/05-workers-plan-todo.md) |
| 6 | [Provider 和配置](release/v3/learning/06-providers-config.md) |
| 7 | [Skills、命令、CLI 和 TUI](release/v3/learning/07-skills-commands-cli-tui.md) |
| 8 | [Session、Run 和 Evaluation](release/v3/learning/08-session-run-evaluation.md) |
| 9 | [模块地图](release/v3/learning/09-module-map.md) |
| 10 | [模块学习指南](release/v3/learning/10-module-learning-guide.md) |
| 11 | [Dream 后台记忆整合](release/v3/learning/11-dream-memory-consolidation.md) |

## License

MIT
