<h3 align="center">⚠️ 双仓库协作项目 / Two-Repo Project</h3>
<p align="center"><b>本项目需要两个仓库共同部署才能完整运作：</b></p>
<p align="center">
<a href="https://github.com/183kugua/astrbot_plugin_winremote"><b>winremote-astrbot</b></a>（B 端 · AstrBot 插件） ↔ <a href="https://github.com/183kugua/winremote-app"><b>winremote-app</b></a>（C 端 · 执行器）
</p>

> [!IMPORTANT]
> 本仓库是 **C 端执行器**，必须与 **B 端插件 [winremote-astrbot](https://github.com/183kugua/astrbot_plugin_winremote)** 配合部署。请同时获取两个仓库。

---

# winremote-app

> **⚠️ 来源声明**：本仓库是原作者 [@silver-wolf-little-wife](https://github.com/silver-wolf-little-wife) 的 **Cherry Remote**（[cherry-astrbot](https://github.com/silver-wolf-little-wife/cherry-astrbot) / [cherry-remote-app](https://github.com/silver-wolf-little-wife/cherry-remote-app)）的改名延续版（WinRemote），由 [@183kugua](https://github.com/183kugua) 维护。原作者享有原始作品的全功劳，本项目遵循原开源协议（MIT）。


WinRemote 远程操控系统 · **C 端执行器**。当前版本 **v1.7.5**。

部署在目标电脑（C地·家庭局域网内 PC）上的常驻服务，**纯执行器，无任何 AI/LLM 逻辑**。

- 主动外连 B 端 AstrBot 插件的 WebSocket 服务（穿透 NAT）。
- 接收指令并执行：`exec`（shell）、`sys`（系统信息）、`ping`（连通性）、`file`（文件操作）、`app`（应用启停）、`screenshot`（截屏，支持 `reuse_seconds` 复用最近画面）、`input`/`keys`（键盘）、`mouse`（鼠标，点击前自动激活目标窗口）、`clipboard`（剪贴板读写）、`file_pull`（流式文件拉取）。
- **exe 索引（含显示名）**：启动时后台扫描生成 `exe_index.json`（文件名→路径 + 产品名/文件说明→文件与路径）。`app` 的启动/搜索支持按**用户认知的应用名**解析，如「打开米哈游启动器」→ 产品名为“米哈游启动器”的 `HYP.exe`、`ZenlessZoneZero.exe` 的产品名“绝区零”等。
- 回传**原始结果**给 B 端，由 B 端 AI 研判后回复用户。

## 架构

```
[A] 手机(任意IM) → [B] AstrBot+AI → WebSocket → [C] winremote-app(本机)
                       ↑  研判结果  ←──────执行结果────────┘
```

智能全部在 B 端；本程序只执行、不思考。

## 部署（推荐：Release 成品包）

无需安装 Python，从 [Releases](https://github.com/183kugua/winremote-app/releases) 下载最新版 `winremote-app-vX.Y.Z-win64.zip`：

1. **解压** 到任意目录（如 `D:\winremote-app\`），得到两个文件：
   - `winremote-app.exe` —— 主程序
   - `config.yaml` —— 配置文件（已直接生成，**无需改名**）
2. **编辑 `config.yaml`**（记事本打开即可），至少填写以下三项：
   ```yaml
   server_url: "ws://你的服务器:8765/ws"   # B 端 AstrBot 插件 WebSocket 地址
   auth_token: "与B端插件一致的token"       # 必须与 B 端插件 auth_token 完全相同
   device_id: "home-pc"                    # 本机标识，多设备时用于区分
   ```
   > 若 `config.yaml` 不存在：直接运行一次程序会自动生成一份默认配置。
3. **运行**（连 B 端，后台常驻）：
   ```bash
   winremote-app.exe --daemon
   ```
   > **双击 exe 默认打开图形控制台**：点「开启进程」即拉起后台服务（无黑框）。
   > 服务部署/开机自启请用 `--daemon` 参数（见 [docs/DEPLOY.md](docs/DEPLOY.md)）。

> 开机自启 / 注册 Windows 服务 / wss TLS 部署：见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

## 从源码运行（开发）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 首次运行会自动生成 config.yaml（也可从 config.example.yaml 复制）
# 3. 运行（默认打开图形控制台；--daemon 为服务部署模式）
python -m winremote_app                   # 图形控制台（启停进程）
python -m winremote_app --daemon -c config.yaml   # 后台常驻（服务/自启）
```

要求 Python 3.10+。

## 图形控制台（启停进程管理面板）

**默认入口**：双击 exe（或 `--gui` 参数）打开一个简单小窗口，用于开启/重启/关闭本程序的后台进程（`--daemon`）：

```bash
winremote-app.exe           # 打包形态：双击即打开图形控制台
winremote-app.exe --gui      # 等价显式写法
python -m winremote_app      # 源码运行（默认图形控制台）
```

窗口内容：

| 功能 | 说明 |
|---|---|
| 状态显示 | 运行中（含 PID / 已运行时长）或 已停止，每 2 秒自动刷新 |
| 「开启进程」 | 若未运行，以后台方式拉起自身 daemon |
| 「重启进程」 | 先停掉旧 daemon 再重新拉起（运行中可用） |
| 「关闭进程」 | 优雅结束正在运行的 daemon |
| 底部信息 | 当前使用的 config.yaml 路径 |

> 图形控制台本身不连接 B 端；关闭控制台窗口不影响已运行的服务。
> 服务启停也可由 B 端通过 `system` 指令远程控制。

## 打包 exe

```bash
python packaging/build_exe.py           # 默认即无黑框形态：双击 exe 静默后台运行，--gui 打开图形控制台
```

- 默认形态无控制台黑框：双击 exe 直接打开图形控制台（开启/重启/关闭进程，2 秒自动刷新状态）。
- `--daemon`：跳过面板直接后台常驻（服务部署/开机自启用）。
- 关闭图形控制台窗口不影响已运行的服务。
- 打包完成后 `dist/` 会直接生成 `config.yaml`（无需手动改名）。

## 测试

仓库内置两套回归测试（无需 B 端在线，本地模拟全链路）：

```bash
python test_file_pull.py   # file_pull：握手/单帧/流式分块 sha256/缺文件/超限/普通指令回归
python test_smoke.py       # Executor 全方法冒烟：exec/sys/ping/file/app/screenshot/system/白名单
python test_exe_index.py   # exe 索引：版本资源读取/显示名回退链/索引 JSON/显示名解析/app search/旧索引合并
python test_gui.py         # gui 键鼠：按键名/字符映射/shift 符号/Unicode 中文路由/input/keys/mouse 执行器转发（mock 底层）
python test_features.py    # 专项功能测试：剪贴板、点击自动激活窗口、截图复用缓存
python test_gui_console.py # 图形控制台：daemon 启停/重启/进程识别逻辑（无界面环境可跑）
```

全绿输出 `ALL PASS`。改动代码后建议先跑这几套（键鼠真实驱动需 Windows 实测）。

## 配置项

见 [`config.example.yaml`](config.example.yaml)。关键项：

| 键 | 说明 |
|---|---|
| `server_url` | B 端 WebSocket 地址，如 `ws://your-server:8765/ws` |
| `auth_token` | 认证 token，与 B 端插件一致 |
| `device_id` | 本机标识，多设备时用于区分 |
| `build_exe_index` | 启动时构建 exe 索引（文件名 + 产品名/文件说明显示名） |
| `exe_index_file` | 索引输出路径（格式 v2，含 `_product` 显示名映射与 `_meta`） |
| `allowed_actions` | 指令白名单，白名单外一律拒绝（含 `file_pull`） |
| `max_pull_size` | 单次文件拉取大小上限（字节，默认 200MB） |

## 通信协议

见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)（与 winremote-astrbot 共享）。

## 安全

- token 认证握手。
- 指令白名单 `allowed_actions`。
- 建议生产环境使用 wss/TLS。

## 许可

[MIT](LICENSE)
