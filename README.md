# chatgpt-ultra-patch

解锁 ChatGPT 桌面版（Electron/MSIX）里隐藏的 **Ultra 紫色推理档位**，并提供一个
**Codex ↔ WorkBuddy 双账号桥接**，让桌面版同时使用两个 WorkBuddy 账号的模型与积分。

> ⚠️ **免责声明 / Legal**
> 本项目**不包含、不分发** OpenAI 或任何第三方应用的任何二进制、资源或代码。
> 你必须使用自己合法安装的应用副本。补丁脚本只对你本机的副本做文本级修改，
> 用于学习和研究客户端行为，风险自负。使用本项目即表示你理解：
> 修改后的客户端可能违反相关服务条款，由此产生的账号风险由你自己承担。
> 本项目与 OpenAI / WorkBuddy 无任何关联。

## 这是什么

ChatGPT 桌面版的思考强度滑块最右端有一个隐藏的 **Ultra（紫色）档**。它被三道门锁住：

1. 服务端下发的 feature flag `1186680773`（includeUltraReasoningEffort）
2. 服务端账户设置 `reasoningEffortVisibility`
3. Electron 启动时的 **asar 完整性校验**（期望哈希嵌在 `ChatGPT.exe` 里）

前两道门是 JS 逻辑，可以在 asar 里的 webview 代码中强制置真；第三道门必须
把 exe 里嵌入的 64 位 hex 哈希替换成补丁后 asar 头的实际哈希（等长替换，安全）。

## 仓库结构

```
tools/
  patch_asar.py       打补丁 asar：强制 flag + 扩展档位列表，重建头与逐文件哈希
  patch_exe_hash.py   把 exe 内嵌的期望哈希替换为补丁后 asar 的实际头哈希
  check_asar.py       逐文件校验 asar 自洽性（补丁后验证）
bridge/
  server.mjs          Codex <-> WorkBuddy 双账号桥接（CN + 国际版）
  responses.mjs       Responses API <-> chat 协议转换
  start_bridge.pyw    Windows 无控制台启动器
UltraPatch.pyw       ★ 傻瓜式一键 GUI（v2.0 新增，零依赖）
```

## 使用方法

### 方式 A：傻瓜式 GUI（推荐，v2.0）

1. 确认装了 Python 3.8+（Windows 商店版也行）
2. 双击 **`UltraPatch.pyw`**（用 pythonw 打开，没有黑窗口）
3. 界面上会自动填好检测到的原版位置；检测不到就点「浏览…」选原版 `ChatGPT.exe`
4. 副本位置保持默认，点 **⚡ 一键补丁**
5. 完成后点「创建桌面快捷方式」「启动补丁版」即可

GUI 会自动完成：复制副本 → asar 补丁 → exe 哈希同步 → 全量自检，
每一步都有日志；已打过补丁的副本会安全跳过，不会重复打。

### 方式 B：命令行

> 全程**不要修改系统里已安装的原版**（MSIX 目录需要管理员且会破坏包校验）。
> 先把整个应用目录复制一份到普通可写目录，对副本操作。

1. **复制应用副本**：把 `C:\Program Files\WindowsApps\OpenAI.Codex_*\app`
   整个复制到任意可写目录（如 `%USERPROFILE%\ChatGPTPatched\app`）。

2. **打 asar 补丁**：

   ```bat
   python tools\patch_asar.py C:\Users\you\ChatGPTPatched\app\resources\app.asar
   ```

   脚本会自动备份原文件为 `.bak-original`，并在结尾打印
   `NEW_HEADER_SHA256 = ...`。

3. **同步 exe 期望哈希**（跳过必崩，`FATAL asar_util.cc Integrity check failed`）：

   ```bat
   python tools\patch_exe_hash.py C:\Users\you\ChatGPTPatched\app\ChatGPT.exe ^
           C:\Users\you\ChatGPTPatched\app\resources\app.asar --backup
   ```

4. **自检**（可选但强烈建议）：

   ```bat
   python tools\check_asar.py C:\Users\you\ChatGPTPatched\app\resources\app.asar
   ```

   `ok: true` 即为补丁自洽。

5. **启动副本**：直接运行 `ChatGPTPatched\app\ChatGPT.exe`。
   会自动继承同一用户的登录态。注意：
   - 副本与原版**不要同时运行**（共用同一份用户配置，会争单实例锁）。
   - 启动日志里 updater 报 package-identity 警告属正常（自动更新不可用，非致命）。

6. **（可选）桌面快捷方式**：指向副本 exe 即可。

### MSIX 备注

- MSIX 版 Chromium 应用脱离 `WindowsApps` 目录直接启动，通常会因为缺包身份崩溃
  （退出码 `0x80000003`）。**ChatGPT 桌面版实测可直接从副本目录启动**——包内
  校验逻辑对身份的要求只作用于更新器，主进程可正常运行。
- 就地修改 `WindowsApps` 原包需要管理员 + 破坏包签名校验，不推荐；松散注册
  （`Add-AppxPackage -Register`）需要开发者模式且 manifest 含特权扩展会被拒。

## 桥接：Codex 同时连两个 WorkBuddy 账号

`bridge/server.mjs` 在一个端口（默认 15790）上合并两个账号的模型列表：

- 国内版模型 id 原样（如 `hy4flash-free`）
- 国际版模型 id 加 `ai-` 前缀（如 `ai-hy4flash-free`），按前缀自动路由、剥前缀转发
- 积分余额映射为 Codex 的 rate-limit 响应头，两个账号各自独立
- `/v1/models` 返回 `supportedReasoningEfforts`（含 ultra），配合补丁让桌面版
  模型选择器渲染出紫色 Ultra 档
- 国际版网关有"非官方客户端"渠道校验（错误 11128），桥接已在请求上补齐官方
  桌面 App 的会话/跟踪头（`X-Conversation-*`、`traceparent` 等），实测可通过

配置（环境变量）：

```bat
set WB_CONNECT_MODULE=file:///C:/path/to/dsh-workbuddy-connect/lib/<entry>.js
set WORKBUDDY_ELECTRON_BIN=D:\apps\WorkBuddy\WorkBuddy.exe
set WORKBUDDY_AI_ELECTRON_BIN=D:\apps\WorkBuddyAI\WorkBuddyAI.exe
set WB_BRIDGE_PORT=15790
REM 可选：本机回环鉴权
set WB_BRIDGE_TOKEN=your-secret
pythonw bridge\start_bridge.pyw
```

在 Codex 的 `config.toml` 里把唯一 provider 指向 `http://127.0.0.1:15790/v1`
即可，无需多 provider 配置。

> **可移植性提示**：`dsh-workbuddy-connect` 是压缩混淆的模块，`server.mjs` 里的
> `h.m / h.S / h.x / h.O / h.I / h.P` 是某一具体插件构建版本的导出别名
> （credential store 构造器、CN/AI 变体、HTTP 客户端、CN/国际版请求体规整）。
> 换插件版本时需从模块导出里重新对号。

## 踩坑记录（重要）

- **改 asar 必须同步改 exe 里的期望哈希**。哈希 = 整个 header JSON 的 sha256，
  存在于 exe 的 `ELECTRONASAR` 资源中，格式
  `[{"file":"resources\\app.asar","alg":"SHA256","value":"<64hex>"}]`。
- **重建 asar 时先快照原始 offset**，再分配新 offset——直接读 `node["offset"]`
  会读到已改写的新值，导致全包数据错乱。
- **刷新树时先 `ch is not nd` 判断**，避免 `clear()` 清掉同一对象把节点摧毁。
- 组装后做**总量校验**（header + 全部文件字节数），再逐文件哈希自检。
- `os.replace` 被锁时，回退为 `r+b` 原地覆写 + `truncate`。
- C 盘写 700MB+ 的 tmp 前，确认磁盘余量 >1GB。

## 许可证

[MIT](LICENSE)。本仓库仅包含原创脚本与文档。
