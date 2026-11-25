# iCourse Subscriber Local

和原仓库 https://github.com/LeafCreeper/Fudan_iCourse_Subscriber 的区别：
1. 除AI以外的后台服务完全在本地运行，不依赖 GitHub Actions和邮件，AI也可以选择本地Ollama运行；
2. 支持在网页随时自行切换 LLM 模型服务商（支持在写入环境的多个API中选择，或者在网页直接添加新的API）；
3. 支持选择需要解析的课次，未选中的课次不会被处理；
4. 以及一些细节优化。

在自己的 Mac 上订阅复旦 iCourse 课程，在浏览器里阅读每节课的摘要、转写和 PPT 文字。课程笔记和同步进度保存在本机的 SQLite 数据库里。

## 在 Mac 上使用

在项目根目录打开终端。先准备可在终端运行的 `ffmpeg`，以及 Python 3.12、Python 3.11 或 `uv` 中的任意一个。如果你使用 Homebrew，缺少哪些工具就安装哪些，例如 `brew install ffmpeg python@3.12`。

第一次运行安装脚本。它把 Python 依赖装进项目的 `.venv`，准备 SenseVoice 语音识别和 Silero 语音检测模型，并在缺少 `.env` 时创建一份空白配置。首次安装需要联网，重跑会复用已有模型。如果只有 `uv`，脚本会在项目的 `.local-python` 中准备 Python 3.12。

```sh
bash scripts/setup_local.sh
```

用文本编辑器打开项目根目录的 `.env`，填写以下项目，保存后启动本地课程库。

| 配置 | 填写内容 |
|---|---|
| `StuId`、`UISPsw` | 复旦学号和 UIS 密码，变量名区分大小写 |
| `UIS_TOTP_SECRET` | UIS 动态口令绑定时取得的 Base32 密钥，六位验证码会由程序在登录时生成 |
| `COURSE_IDS` | 可选的初始订阅，填写课程网址中的数字，多个用英文逗号分隔。也可以打开网页后添加 |
| 模型服务密钥 | 可选。可填写 `DASHSCOPE_API_KEY`（ModelScope）、`DEEPSEEK_API_KEY` 或 `GEMINI_API_KEY`，也可以稍后在网页里添加 |

已有的 `LLM_BASE_URL`、`LLM_MODEL` 和 `LLM_API_KEY` 组合也会出现在网页的 API 列表里。接口需兼容 OpenAI Chat Completions。

在 Finder 中双击 `打开课程库.command`，或在终端运行：

```sh
.venv/bin/python local_app.py
```

浏览器会打开本地课程库，优先使用 `http://127.0.0.1:8765/`。如果该端口正忙，程序会自动选择可用端口并在终端显示网址。

1. 在「管理订阅」保存课程 ID，点击「检查新课次」。程序只从 iCourse 读取录播列表，并与本机笔记比对。这一步无需模型密钥。
2. 在待选清单中按课程或逐节勾选，默认选中 0 节。可以随时关闭清单，之后从「待选课次」继续选择。
3. 选择本次的摘要 API。要添加其他账号或模型，打开「API 设置」，分别填写名称、接口地址、模型 ID 和密钥。网页添加的配置保存在本机的 `data/providers.json`，已保存的密钥不会回显。
4. 点击「解析所选 N 节」后，Mac 才开始处理这些录播，并把转录和识别出的 PPT 文字发给选定的模型。每批只使用确认的这项 API。页面显示当前课程、课次、处理阶段和已处理数，刷新后可继续查看。未勾选或处理失败的课次可以稍后再选。课程材料仅供个人学习使用。

课程数据默认保存在 `data/icourse.db`，运行日志在 `data/sync.log`。网页保存的订阅在 `data/subscriptions.json`，之后以这份清单为准。启动时如需自己打开浏览器，使用 `.venv/bin/python local_app.py --no-browser`。可用 `--port 8766` 指定端口。下面的命令只更新待选清单便退出。打开网页并确认课次后才会解析。

```sh
.venv/bin/python local_app.py sync
```

### 停止运行并释放端口

**回到显示「本地课程库 http://…」的终端窗口，按 `Control + C`。** 双击 `打开课程库.command` 启动时，也在它打开的终端里按这组键。网页服务退出后会释放端口。只关闭浏览器标签页，服务仍会运行。

**找不到原来的终端时**，新开一个终端，按浏览器地址里的实际端口查找进程。例如地址为 `http://127.0.0.1:55350/` 时，运行：

```sh
port=55350
lsof -nP -iTCP:"$port" -sTCP:LISTEN
```

没有输出表示该端口已无程序监听。如果有输出，再核对启动命令和项目目录：

```sh
server_pid=$(lsof -nP -t -iTCP@127.0.0.1:"$port" -sTCP:LISTEN)
ps -p "$server_pid" -o pid=,command=
lsof -a -p "$server_pid" -d cwd
```

确认命令中有 `local_app.py`，且 `cwd` 是本项目目录，再停止服务并复查端口。最后一条命令没有输出即表示端口已释放。

```sh
kill -TERM "$server_pid"
lsof -nP -iTCP:"$port" -sTCP:LISTEN
```

**如果还要中断正在检查或解析的课次**，网页发起的任务在独立进程中运行，关闭网页服务后仍会继续。要一并停止，在项目根目录查找持有同步锁的进程。下面使用默认数据库路径，自定义了 `DB_PATH` 时请在实际数据库文件名后加 `.sync.lock`。

```sh
lsof -n data/icourse.db.sync.lock
```

如果锁文件不存在或没有进程持有它，后台任务已停止。如果列出了 PID，先核对启动命令：

```sh
worker_pid=$(lsof -t data/icourse.db.sync.lock)
ps -p "$worker_pid" -o pid=,command=
```

确认命令以本项目的 `local_app.py _worker` 结尾后，发送中断信号：

```sh
kill -INT "$worker_pid"
```

任务退出后，再查同步锁应无持有者。已入库的笔记会保留。若在终端前台运行的是 `.venv/bin/python local_app.py sync`，直接在该终端按 `Control + C` 即可。已启用每日自动检查的用户，可按下一节的步骤取消定时任务。

### 可选的 macOS 定时同步

如果希望每天自动检查课程，先确认手动检查能成功，再在项目根目录运行以下命令。它们会为当前 macOS 用户注册一个任务，在本地时间 19:36 更新待选清单。定时检查可以在网页关闭时运行，解析仍需在网页中手动确认。

```sh
mkdir -p "$HOME/Library/LaunchAgents" data
agent="$HOME/Library/LaunchAgents/local.fudan.icourse.sync.plist"
.venv/bin/python scripts/local_launchd.py > "$agent"
launchctl bootstrap "gui/$(id -u)" "$agent"
```

日志保存在 `data/scheduled-sync.log`。定时任务使用项目的绝对路径，移动项目后需要重新生成配置。要停止并取消定时同步，可执行以下命令。

```sh
agent="$HOME/Library/LaunchAgents/local.fudan.icourse.sync.plist"
launchctl bootout "gui/$(id -u)/local.fudan.icourse.sync"
rm "$agent"
```

## 原版云端部署（GitHub Actions 与邮件）

云端版通过 GitHub Actions 定时执行同一套课程处理流程，并通过邮件推送到你的邮箱。下面保留了该部署方式的配置说明。

> [!NOTE]
> 本项目严禁大规模传播（例如，不准分享到树洞、班级群、大群等地），否则信息办可能随时ban掉该项目。
> 如果该项目对你有用，请点个**star**⭐作为对作者的鼓励，并将项目**单独**分享给你的好朋友。


## 它能做什么？

假设你选了「摸鱼学导论」和「躺平学原理」两门课。在每天设定的时间，iCourse Subscriber 会自动：

1. 登录你的复旦 iCourse 账号（通过 WebVPN）
2. 检查这两门课是否有新的录播视频
3. 如果有：提取音频 → 语音识别 → 提取 PPT 图片 → OCR 转写 → AI 生成课程笔记
4. 将所有新课次的笔记汇总成**一封邮件**发送给你

邮件包含专业排版的 Markdown 渲染内容（含 LaTeX 公式渲染）。如果老师提到了作业、考试、签到、组队等重要课程事项，会在笔记开头醒目标注。


## 快速部署（5 分钟）

### 第 1 步：Fork 本仓库

点击页面右上角的 **Fork** 按钮，将仓库复制到你的 GitHub 账号下。在fork的时候，请不要取消勾选默认的「Copy the `main` branch only」选项，否则后续会运行失败。

### 第 2 步：配置 Secrets

进入你 Fork 后的仓库，点击 **Settings → Secrets and variables → Actions → New repository secret**，逐个添加以下 Secret：

| Secret 名称 | 必填 | 说明 | 示例 |
|---|---|---|---|
| `STUID` | ✅ | 复旦学号 | `22307110000` |
| `UISPSW` | ✅ | UIS 统一身份认证密码 | `your_password` |
| `UIS_TOTP_SECRET` | ✅ | 二次验证要求的动态口令（OTP）（见下） | `XXW33***********` |
| `COURSE_IDS` | ✅ | 要监控的课程 ID，多个用英文逗号分隔 | `35472,30251` |
| `DASHSCOPE_API_KEY` | ⬜ | ModelScope 平台 API Key | `ms-xxxxxxxx` |
| `DEEPSEEK_API_KEY` | ⬜ | DeepSeek API Key（推荐） | `sk-xxxxxxxx` |
| `GEMINI_API_KEY` | ⬜ | Gemini API Key | `AIza...` |
| `SMTP_EMAIL` | ✅ | 用于发送邮件的 QQ 邮箱 | `123456@qq.com` |
| `SMTP_PASSWORD` | ✅ | QQ 邮箱 SMTP **授权码**（不是登录密码） | `abcdefghijklmnop` |
| `RECEIVER_EMAIL` | ✅ | 接收摘要邮件的邮箱 | `you@m.fudan.edu.com` |

> 至少配置一个 LLM API Key（DASHSCOPE、DEEPSEEK 或 GEMINI）。程序按配置顺序自动回退尝试。如果需要选择其他的LLM供应商，可以在`src\runtime\config.py`路径下自定义供应商。

#### 配置动态口令二次验证（TOTP）

> 2026/11/09 iCourse 平台升级了登录策略，要求所有账号在密码验证后进行二次验证（TOTP），我们采用 TOP 登录的方法进行二次验证

进入 [UIS 个人中心的账号安全页面](https://id.fudan.edu.cn/uc/#/pc/userCenter/accountSecurity)，在「动态口令」中按提示完成绑定，并保存绑定时获取的 **Base32 密钥**，按上述的 Secrets 填入 `UIS_TOTP_SECRET` 即可

![UIS 动态口令绑定页面，箭头所指密钥用于 UIS_TOTP_SECRET](docs/uis-totp.png)


### 第 3 步：获取课程 ID

登录 [iCourse 网页版](https://icourse.fudan.edu.cn)，进入你要监控的课程页面，URL 中的数字就是课程 ID：

![课程id如图所示](docs/courseid.png)

多门课用英文逗号隔开：`35472,30251,40123`

### 第 4 步：获取 API Key

选择一个或多个模型服务商：

| 服务商 | 获取方式 | 免费额度 |
|---|---|---|
| **ModelScope**（`DASHSCOPE_API_KEY`） | [API 密钥管理](https://modelscope.cn/my/myaccesstoken) | 每天 2000 次免费调用，推荐 |
| **DeepSeek**（`DEEPSEEK_API_KEY`） | [DeepSeek Platform](https://platform.deepseek.com/) | 注册赠额度 |
| **Gemini**（`GEMINI_API_KEY`） | [Google AI Studio](https://aistudio.google.com/) | flash模型每日免费额度 |

### 第 5 步：获取 QQ 邮箱 SMTP 授权码

1. 登录 [QQ 邮箱](https://mail.qq.com) → 设置 → 账户与安全 → 安全设置
2. 找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务」
3. 开启 SMTP 服务，按提示获取**授权码**（16 位字母）
4. 将授权码填入 `SMTP_PASSWORD`

### 第 6 步：运行

- **自动运行**：默认每天 19:36（北京时间）自动执行
- **手动触发**：进入仓库 → Actions → **iCourse Check** → Run workflow
- **立即触发**：也可通过前端页面点击「触发订阅检查」按钮

首次运行会处理所有已有录播，后续只处理新增课次。

### 更新你的程序

作者会长期维护本仓库。当本仓库代码更新时，你可以在你fork的仓库主页上点击`merge`按钮来更新到最新代码。

## 前端页面（索引与查看）

![alt text](docs/frontend.png)

本项目自带一个浏览器端加密数据库查看器，部署在 GitHub Pages：

访问 `https://你的用户名.github.io/Fudan_iCourse_Subscriber/`

功能介绍：
- **浏览器端解密**：输入你的凭据，浏览器用 WebCrypto 解密 sql.js 读取 shard 数据库，凭据不离开本地
- **按课程/课次浏览**：查看每节课的转录和摘要内容
- **导出 PDF**：通过 GitHub Actions 触发导出工作流，生成格式化课程笔记 PDF 并邮件发送

> 前端页面需要你手动在 GitHub Pages 设置中开启（Settings → Pages → Source → GitHub Actions），然后触发一次 Deploy Frontend workflow 即可部署。

> [!TIP]
>
> ## V2 更新说明
>
> - 重构了代码架构，清晰的分层化设计。
>
> - 更新了 ASR 系统，引入插件化后端机制，目前支持 SenseVoice（默认）、FireRed 与 Zipformer 三种后端，并新增统一的跨后端后处理流程。
>
> - 引入了 PPT OCR 流水线和清洗机制，通过 PPT 信息引导模型生成更丰富的课程总结。
>
> - 引入新的调度系统，以改善 GitHub Actions 低核数环境下的吞吐与稳定性。
>
> - 更新了数据库结构，从原本的单文件数据库演进为按课程约 10MB 的分片结构，支持前端增量加载与按需读取，降低课程加载开销。
>
> - 新增 GitHub Pages 前端查看器，支持加密数据库浏览、订阅编辑器与 PDF 导出等功能。
>
> - 更新了 LLM 调度逻辑，将原本的单一 provider 调用改为多 provider 列表式自动回退机制。
>
> - 设计了完善的版本过渡机制，V1 可以直接升级为 V2，程序会自动处理所有兼容性问题。


> [!CAUTION]
> **⚠️ 合规使用声明**
>
> 本项目的设计初衷仅为辅助本校学生进行**个人的日常学习与复习**与进行技术交流。程序采用"封闭容器、流式处理、阅后即焚"的架构，默认不保存任何视频文件。任何人在部署和使用本项目时，必须严格遵守《复旦大学智慧教学资源平台使用规范》及相关校纪校规。**严禁使用者利用本程序进行以下违规操作，一切因滥用导致的账号封禁或纪律处分（如通报批评、限制平台权限等），均由使用者自行承担，与本仓库及作者无关：**
>
> * **严禁二次分发与传播**：《规范》第二部分明确指出，平台教学资源属于职务作品，未经许可不得传播。**禁止**将推送到你邮箱的课程摘要、转录文本或笔记转发给他人，或发布到任何公共网络平台。
> * **严禁修改代码非法下载视频**：《规范》严禁未经许可对平台资源进行复制和下载。基于此，本项目并不留存视频，**严禁**任何人修改源代码将受版权保护的课程录播违规下载、保存到任何本地或云端存储介质。
> * **严禁解密或泄露数据库**：仓库中的 `icourse.db.enc` 仅用于程序追踪课次进度避免重复计算。**严禁**手动解密该数据库以提取、滥用或公开其中的转录和摘要文本信息。
> * **注意账号环境安全**：本程序会使用你的 UIS 凭证进行云端 WebVPN 自动化登录，有触发异地登录风控的可能。请妥善保管个人 Secret，因使用云端自动化服务导致的账号异常风险由使用者自行评估。
>
> **当你 Fork 并配置 Secret 运行本项目时，即代表你已知晓上述风险，并承诺仅在授权范围内为个人学习目的使用本工具，遵守相关校纪校规。**
