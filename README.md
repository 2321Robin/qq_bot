# QQ Group Bot

一个基于 NoneBot2、OneBot v11 和 NapCatQQ 的 QQ 群机器人项目。

项目当前支持基础群命令、洛克王国世界精灵与技能查询、AI 对话、群聊记忆、联网搜索增强回答，以及定时群消息发送。

## 功能特性

- `/help`：查看当前可用功能。
- `/version` 或 `/版本`：查看当前机器人版本。
- `/精灵 迪莫` 或 `/洛克 迪莫`：查询本地洛克王国世界精灵详细数据，并返回静态精灵介绍图。
- `/技能 闪光`：查询本地洛克王国世界技能效果，以及可使用该技能的精灵。
- `/计数 迪莫` 或 `/计数 异色 迪莫`：记录当前用户当前赛季的精灵捕捉数量；发送 `/计数` 查看汇总。
- `ai 你好`：显式向配置的 AI 模型提问。
- AI 群聊记忆：自动记录近期群消息，AI 可在同一群聊和用户上下文中引用近期对话。
- AI 搜索增强：启用 Tavily 后，可对新闻、当前事件或搜索类问题自动补充联网搜索结果。
- AI 备用模型：主模型不可用时，可自动切换到备用 OpenAI 兼容接口。
- 定时消息：按配置的时间向指定 QQ 群自动发送固定消息。
- 更新日志：版本变更记录见 `CHANGELOG.md`。

## 技术栈

- Python 3.11+
- NoneBot2
- OneBot v11
- NapCatQQ
- FastAPI driver
- SQLite 群聊记忆
- httpx、Pillow、pydantic-settings

## 项目结构

```text
qq_bot/
├── bot.py                         # NoneBot 启动入口
├── pyproject.toml                 # 项目依赖与测试配置
├── .env.example                   # 环境变量示例
├── start_all.ps1                  # Windows 一键启动脚本
├── 一键启动.bat                    # Windows 批处理启动入口
├── scripts/
│   └── generate_roco_pet_cards.py # 生成洛克王国精灵介绍图
├── src/qq_bot/
│   ├── config.py                  # 配置读取与校验
│   ├── plugins/                   # NoneBot 插件
│   └── services/                  # AI、搜索、记忆、精灵数据等服务
├── data/
│   ├── roco_pet_details/          # 本地精灵详细数据
│   ├── roco_assets/               # 精灵素材
│   └── roco_pet_cards/            # 生成后的精灵介绍图
└── tests/                         # 自动化测试
```

## 环境要求

运行前请准备：

- Python 3.11 或更高版本。
- 可登录 QQ 的 NapCatQQ。
- 可用的 OneBot v11 反向 WebSocket 连接。
- 如需 AI 对话，需要一个 OpenAI 兼容接口的 API Key。
- 如需联网搜索增强，需要 Tavily API Key。

## 本地安装

在项目根目录执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

安装完成后，编辑 `.env` 填写本地配置。

## 配置说明

`.env.example` 提供了完整配置模板。常用配置如下：

```dotenv
DRIVER=~fastapi
HOST=127.0.0.1
PORT=8080
COMMAND_START=["/"]
SUPERUSERS=[]

ALLOWED_GROUP_IDS=
ADMIN_USER_IDS=

AI_API_KEY=
AI_BASE_URL=https://api.openai.com/v1
AI_MODEL=gpt-4o-mini
AI_PREFIX=ai
AI_TIMEOUT_SECONDS=30
AI_FALLBACK_API_KEY=
AI_FALLBACK_BASE_URL=https://open.bigmodel.cn/api/paas/v4
AI_FALLBACK_MODEL=glm-4-flash

CHAT_MEMORY_PATH=data/chat_memory.sqlite3
CHAT_MEMORY_RETENTION_DAYS=3
CHAT_MEMORY_DEFAULT_TURNS=10
CHAT_MEMORY_MAX_RESULTS=20

ROCO_COUNTER_PATH=data/roco_counter.sqlite3
ROCO_COUNTER_SEASON=S2

SEARCH_ENABLED=false
TAVILY_API_KEY=
SEARCH_MAX_RESULTS=5
SEARCH_TIMEOUT_SECONDS=10

SCHEDULED_GROUP_IDS=
SCHEDULED_MESSAGE=现在是定时提醒时间。
SCHEDULED_CRON_TIMES=
SCHEDULED_CRON_HOUR=9
SCHEDULED_CRON_MINUTE=0
```

### 群权限

- `ALLOWED_GROUP_IDS` 控制哪些群可以使用机器人。
- 留空表示不限制群。
- 多个群号使用英文逗号分隔，例如 `123456789,987654321`。

### AI 对话

- `AI_API_KEY` 是主 AI 服务的 API Key。
- `AI_BASE_URL` 需要填写 OpenAI 兼容接口地址。
- `AI_MODEL` 是主模型名称。
- `AI_PREFIX` 是触发 AI 对话的前缀，默认是 `ai`。

示例：

```text
ai 你好
ai 帮我总结一下洛克王国迪莫的特点
```

当问题涉及本地洛克王国精灵或技能数据时，AI 对话会先检索 `data/roco_pet_details/`，再把命中的图鉴、进化关系或技能交集作为可信上下文发给模型。例如：

```text
ai 画精灵怎么进化？
ai 画间沉铁兽是怎么进化得到的？
ai 什么精灵既能学习加固技能又能学习除厄技能？
```

这类问题优先依据本地结构化数据回答；本地没有记录时会说明资料不足，而不是让模型凭记忆补全。

### AI 备用模型

配置 `AI_FALLBACK_API_KEY` 后，机器人会在主 AI 服务请求失败或返回不可用内容时尝试备用模型。

默认备用接口配置为智谱 AI 的 OpenAI 兼容接口：

```dotenv
AI_FALLBACK_BASE_URL=https://open.bigmodel.cn/api/paas/v4
AI_FALLBACK_MODEL=glm-4-flash
```

### 群聊记忆

AI 对话会使用本地 SQLite 保存近期群聊记忆，默认路径为：

```text
data/chat_memory.sqlite3
```

相关配置：

- `CHAT_MEMORY_RETENTION_DAYS`：记忆保留天数，默认 3 天。
- `CHAT_MEMORY_DEFAULT_TURNS`：默认引用最近多少轮上下文。
- `CHAT_MEMORY_MAX_RESULTS`：单次最多检索多少条历史消息。

可用示例：

```text
ai 我喜欢迪莫
ai 我刚才说我喜欢谁
ai 参考最近20条：总结一下
ai 参考 洛克王国 的聊天：我们之前说了什么
ai 参考 @某人 的最近20条：总结他的观点
```

其中 `@某人` 需要在 QQ 群里使用真实的 QQ @ 提及。

### 联网搜索增强

设置以下配置后，AI 可以在搜索类或时效性问题中使用联网搜索结果：

```dotenv
SEARCH_ENABLED=true
TAVILY_API_KEY=你的 Tavily API Key
```

可到 https://app.tavily.com/ 创建 Tavily API Key。请只把 API Key 放在本地 `.env`，不要提交到仓库。

示例：

```text
ai 今天有什么新闻
ai 搜索 DeepSeek 最新消息
```

### 精灵捕捉计数器

- `ROCO_COUNTER_PATH` 控制捕捉计数 SQLite 文件路径，默认 `data/roco_counter.sqlite3`。
- `ROCO_COUNTER_SEASON` 控制当前计数赛季，默认 `S2`。
- 发送 `/计数 迪莫` 记录普通捕捉，发送 `/计数 异色 迪莫` 记录异色捕捉。
- 发送 `/计数` 查看当前群内当前用户的当前赛季汇总。

### 定时消息

- `SCHEDULED_GROUP_IDS` 控制定时消息发送到哪些群。
- `SCHEDULED_MESSAGE` 是定时发送的内容。
- `SCHEDULED_CRON_TIMES` 支持配置多个发送时间。

示例：

```dotenv
SCHEDULED_GROUP_IDS=123456789
SCHEDULED_MESSAGE=现在是定时提醒时间。
SCHEDULED_CRON_TIMES=11:00,12:10,16:10,20:10
```

如果 `SCHEDULED_CRON_TIMES` 为空，则使用 `SCHEDULED_CRON_HOUR` 和 `SCHEDULED_CRON_MINUTE`。

## 运行机器人

在项目根目录执行：

```powershell
.\.venv\Scripts\python bot.py
```

默认情况下，机器人会监听：

```text
127.0.0.1:8081
```

实际监听地址由 `.env` 中的 `HOST` 和 `PORT` 决定。

## 连接 NapCatQQ

在 NapCatQQ 的 OneBot v11 配置中添加反向 WebSocket 地址：

```text
ws://127.0.0.1:8081/onebot/v11/ws
```

如果 NapCatQQ 和机器人不在同一台机器上，请将 `127.0.0.1` 改成机器人所在机器的局域网或服务器地址。

连接成功后，可在允许的 QQ 群中发送 `/help` 查看可用功能。

## Windows 一键启动

仓库包含 Windows 启动和停止脚本：

```text
一键启动.bat
start_all.ps1
停止.bat
stop_all.ps1
```

这些脚本用于在本机启动或停止机器人后端和 NapCatQQ。使用前请检查配置是否符合你的环境：`start_all.ps1` 包含本机路径、QQ 账号、端口和 NapCatQQ WebUI 地址；`stop_all.ps1` 包含项目路径和 NapCat 目录。

双击 `一键启动.bat` 会通过隐藏的 PowerShell 启动后台进程，批处理窗口可能短暂闪现；启动前会自动关闭已有的机器人后端和当前 NapCat 目录下的 NapCat/QQ 进程，然后重新启动。双击 `停止.bat` 可手动关闭这些进程。

后台启动日志写入 `logs/startup/`：

```text
bot.out.log
bot.err.log
napcat.out.log
napcat.err.log
```

## 洛克王国精灵介绍图

精灵查询命令示例：

```text
/精灵 迪莫
/洛克 迪莫
```

命中本地精灵详细数据时，机器人会优先发送静态精灵介绍图。如果对应介绍图不存在，则回退为文字结果。

现有 `/精灵 <名称>` 与 `/洛克 <名称>` 查询使用本地 `data/roco_pet_details/` 详情数据，不会在 QQ 机器人运行时抓取 BWiki。

技能查询命令示例：

```text
/技能 闪光
```

技能查询同样使用本地 `data/roco_pet_details/` 详情数据，不会在 QQ 机器人运行时抓取 BWiki。

启动前可生成或刷新精灵介绍图：

```powershell
.\.venv\Scripts\python scripts\generate_roco_pet_cards.py
```

生成结果会写入：

```text
data/roco_pet_cards/
```

精灵素材位于：

```text
data/roco_assets/
```

迪莫素材和详情数据来源于洛克王国手游 BWiki 页面：

```text
https://wiki.biligame.com/rocom/%E8%BF%AA%E8%8E%AB
```

生成介绍图中会标注来源。BWiki 页面文本数据标注为 CC BY-NC-SA 4.0。

### 洛克王国 BWiki 详情数据

结构化 BWiki 详情数据是离线数据准备步骤，可按需重新抓取生成，不属于 QQ 机器人运行时抓取逻辑：

```powershell
.\.venv\Scripts\python scripts\fetch_roco_pet_detail.py
```

该脚本会优先抓取 BWiki raw 模板页面，避免普通页面反爬/占位文本污染本地 JSON；抓取完成后会补全双向进化关系。只想重新补全现有数据的进化字段时，可运行：

```powershell
.\.venv\Scripts\python scripts\fetch_roco_pet_detail.py --normalize-only
```

当前生成的详情文件路径为：

```text
data/roco_pet_details/001-迪莫.json
```

## 测试

运行完整测试：

```powershell
.\.venv\Scripts\python -m pytest -v
```

## 手动验证清单

启动机器人并连接 NapCatQQ 后，可按以下步骤验证：

- 发送 `/help`，期望看到功能列表。
- 发送 `/version` 或 `/版本`，期望看到当前机器人版本。
- 发送 `/精灵 迪莫`，期望收到静态精灵介绍图。
- 发送 `/精灵 不存在`，期望收到未找到提示。
- 发送 `/技能 闪光`，期望收到技能效果和可使用该技能的精灵列表。
- 发送 `/技能 不存在`，期望收到技能未收录提示。
- 发送 `/计数 迪莫`，期望收到 `S2 迪莫 +1`。
- 发送 `/计数 异色 迪莫`，期望收到 `S2 异色 迪莫 +1`。
- 发送 `/计数`，期望收到当前用户的 S2 捕捉汇总。
- 配置 `AI_API_KEY` 后发送 `ai 你好`，期望收到 AI 回复。
- 配置 `AI_FALLBACK_API_KEY` 后，将 `AI_BASE_URL` 临时改成无效地址并重启，再发送 `ai 你好`，期望仍能通过备用模型收到回复。
- 设置 `SEARCH_ENABLED=true` 和 `TAVILY_API_KEY` 后重启，发送 `ai 搜索 DeepSeek 最新消息`，期望回复中使用联网搜索上下文。
- 发送 `ai 我喜欢迪莫`，再发送 `ai 我刚才说我喜欢谁`，期望回复能引用近期上下文。
- 发送几条普通群消息后，发送 `ai 参考最近5条：总结一下`，期望回复能引用近期群消息。
- 使用真实 QQ @ 发送 `ai 参考 @某人 的最近5条：总结他的观点`，期望回复能引用被提及用户的消息。
- 将 `SCHEDULED_CRON_TIMES` 临时设置为接下来几分钟，重启后观察目标群是否收到 `SCHEDULED_MESSAGE`。

## 注意事项

- `.env` 中可能包含 API Key、群号、QQ 账号等敏感信息，不要提交到仓库。
- `data/chat_memory.sqlite3` 是本地群聊记忆数据库，包含聊天上下文，分享项目前请确认是否需要清理。
- NapCatQQ、机器人后端和 `.env` 中的端口必须一致。
- 如果机器人没有响应，优先检查 NapCatQQ 反向 WebSocket 是否已连接到机器人后端。
- 如果 AI 没有回复，检查 `AI_API_KEY`、`AI_BASE_URL`、`AI_MODEL` 和网络连接。

## 开发说明

常用开发命令：

```powershell
.\.venv\Scripts\python -m pytest -v
.\.venv\Scripts\ruff check .
```

新增功能时建议同步补充：

- `src/qq_bot/plugins/` 中的插件逻辑。
- `src/qq_bot/services/` 中的服务逻辑。
- `tests/` 下对应测试。
- 本 README 中的使用说明和手动验证步骤。
