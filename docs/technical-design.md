# 技术设计文档（Technical Design）

## 1. 总体架构

```
本地浏览器                    本地 Flask 服务器              远端 Resilio Sync 服务器
┌──────────┐   HTTP/JSON    ┌──────────────┐   HTTP/JSON   ┌─────────────────────┐
│ Dashboard │ ◄────────────► │  app.py      │ ◄────────────► │ /gui/?action=...    │
│ Peers     │   (localhost)  │  resilio_api │   (轻量API)   │ 192.168.x.x:8888    │
│ Settings  │                │  (API客户端) │               │                     │
└──────────┘                └──────────────┘               └─────────────────────┘
```

**核心设计原则：**
1. 只请求 JSON 数据，绝不加载远端 Web UI 的 HTML/CSS/JS
2. 轮询频率降到 5-10 秒（远端 Web UI 默认 3 秒）
3. 每次轮询只调用 2 个关键 API：`getsyncfolders` + `getpeersstat`
4. 速度设置等写操作只在用户手动触发时请求
5. API 客户端内置 `TlsAdapter`（SSL 兼容模式）

## 2. 技术选型

| 组件 | 选择 | 理由 |
|------|------|------|
| 本地 Web 框架 | Flask >= 3.0 | 轻量、内置 Jinja2 模板 + session |
| HTTP 客户端 | requests | 复用内置 TlsAdapter |
| SSL 兼容 | urllib3 TlsAdapter | 兼容旧版 Resilio Sync 的弱加密/TLSv1 |
| 前端 UI | Bootstrap 5（CDN） | 无本地构建依赖 |
| 前端交互 | 原生 fetch() | 现代浏览器，无框架依赖 |
| 依赖管理 | requirements.txt | flask>=3.0, requests, urllib3 |

## 3. 组件设计

### 3.1 resilio_api.py（API 客户端）

内置 `TlsAdapter` 与 `ResilioSyncClient` 类（由早期 CLI 版本提取而来），提供只读/操作 API。

**类：`ResilioSyncClient`**

```python
def __init__(self, base_url, username, password)   # 挂载 TlsAdapter, session.verify=False
def login(self) -> bool                            # token.html 解析 token
def get_folder_list(self) -> dict                  # getsyncfolders&discovery=1
def get_peers_stat(self) -> dict                   # getpeersstat
def get_session_stats(self) -> dict                # getsessionstats
def get_settings(self) -> dict                     # ?action=settings (GET)
def set_speed_limits(self, dl_limit, up_limit)     # ?action=settings (POST)
def pause_folder(self, folder_id) / resume_folder(self, folder_id)
def remove_folder(self, folder_id, delete_files=False)
def add_folder(self, name, path, secret)                  # 非交互导入（106 自动 force）
def get_sync_folders(self)                         # 导出 CSV
def add_sync_folders(self)                         # 导入 CSV
def pause_all_sync(self) / resume_all_sync(self)   # 由早期 CLI 版本迁移
```

**统一请求约定：**
- 所有请求带 `token`、`t=当前毫秒时间戳` 参数
- 每个请求设置 timeout（登录 15s，其余按操作复杂度定，默认 10s）
- 所有方法返回解析后的 JSON（dict）；失败时抛出 `ResilioApiError`（自定义异常），由调用方捕获
- 不打印服务器地址/密码，错误信息脱敏

### 3.2 app.py（Flask 应用）

**路由表：**

| 路由 | 方法 | 说明 |
|------|------|------|
| `/login` | GET/POST | 登录页 / 处理登录 |
| `/` | GET | 重定向 /dashboard |
| `/dashboard` | GET | 仪表盘页面 |
| `/peers` | GET | 节点页面 |
| `/settings` | GET | 设置页面 |
| `/api/status` | GET | 聚合 JSON：文件夹列表 + 全局速度 |
| `/api/peers` | GET | JSON：节点列表 |
| `/api/settings` | GET/POST | JSON：读取/更新速度限制 |
| `/api/folder/<id>/pause` | POST | 暂停文件夹 |
| `/api/folder/<id>/resume` | POST | 恢复文件夹 |
| `/api/folder/<id>/remove` | POST | 删除文件夹（从所有设备） |
| `/api/folder/<id>/disconnect` | POST | 从本设备断开（保留文件，转断开状态） |
| `/api/folder/add` | POST | 添加文件夹（密钥 + 路径，addsyncfolder） |
| `/api/folder/<id>/connect` | POST | 重新连接断开文件夹（取该文件夹密钥 + 用户填路径） |
| `/api/export` | GET | 导出 CSV |
| `/api/import` | POST | 导入 CSV |
| `/api/pause-all` | POST | 暂停所有 |
| `/api/resume-all` | POST | 恢复所有 |
| `/logout` | GET | 退出登录 |

**会话设计：**
- Flask `session`（客户端签名 cookie，base64 明文可解码 + HMAC 防篡改，并非加密）存储 `base_url`、`username`、`password`
- 登录成功后 `session.permanent = True`，`PERMANENT_SESSION_LIFETIME = 30 天` → cookie 带 Expires，浏览器重启后仍保持登录；guard_url/guard_token 随同一 session 一并持久，无需重填（2026-08-21）
- 必须设置强 `SECRET_KEY`（从环境变量 `RSYNC_SECRET_KEY` 读取，否则持久化到 instance/.secret_key，跨重启有效）
- `@login_required` 装饰器：未登录跳转 /login，已登录由 `_client()` 返回**共享客户端**
- **共享客户端（2026-08-05）**：后台线程与请求处理器复用同一个 `_shared_client`（同一 `requests.Session` + token）。原因：Resilio 的 token 绑定在获取它的 HTTP 会话 cookie 上，每轮新建客户端=新会话=旧 token 必失效 → 曾导致每轮后台抓取都重登（控制台刷"登录成功"）。持久复用后仅在 token 真正失效（OOM 重启）时重登一次。`_client_lock` 串行化重登
- API 错误统一返回 `{ok: false, error: "..."}`，HTTP 状态码区分（401 未登录 / 400 参数错 / 502 远端不可达）

**后台抓取缓存（关键）：**
- 独立 daemon 线程每 `FETCH_INTERVAL`（3s）用登录时暂存的凭据（`_active_conn`，进程内存、不写盘）从远端抓取 folders/statuses/session，组装成 `/api/status` 载荷存入 `_status_cache`
- 浏览器轮询 `/api/status` 时**只读本地缓存**（零远端调用），即使浏览器在后台/隐藏，缓存仍由后台线程保持新鲜
- 写操作成功后调用 `_fetch_once()` 立即刷新缓存，UI 无需等下一次轮询
- 登录时设置 `_active_conn` + `_shared_client` 并立即抓一次；登出清空凭据、客户端与缓存

**合并请求策略（关键）：**
- `/api/status`（仪表盘）调用 `getsyncfolders&discovery=1` + `getstatuses` 两次：文件夹列表、全局速度（由文件夹 down_speed/up_speed 求和）、CPU/磁盘 IO 负载（getstatuses.cpu/disk，官方状态栏同源）；节点数据直接取内嵌 `peers`
- `/api/peers`（节点页）调用 `getsyncfolders` + `getpeersstat`（`getpeersstat` 仅返回当前活跃连接）
- 写操作（暂停/恢复/删除/速度设置/导入/批量）单独请求，不进入轮询

### 3.3 前端

```
templates/
├── base.html        # Bootstrap 5 + 导航栏 + 模板继承
├── login.html       # 登录页
├── dashboard.html   # 摘要卡片 + 文件夹表格
├── peers.html       # 节点表格
└── settings.html    # 速度限制表单
static/js/app.js     # 轮询 & AJAX 逻辑
```

- **轮询机制**：`startPolling('/api/status', 5000)`，用 `fetch` 拉取 JSON，仅更新对应 DOM 节点（不整页刷新）
- **错误处理**：轮询失败显示连接状态提示，不中断定时器；连续失败 N 次后停止并提示重试
- **写操作**：暂停/恢复/删除按钮触发 `POST`，成功后立即刷新一次数据
- 页面离开时停止轮询（`visibilitychange` 优化）

## 4. 安全设计

| 风险 | 对策 |
|------|------|
| 密码明文存 cookie | Flask session 客户端签名（base64 明文可解码、防篡改，需随机 SECRET_KEY）；仅 localhost / SSH 隧道访问；会话 30 天有效期 |
| 中间人窃听 | 本应用建议仅绑定 `127.0.0.1`；远端通信走 HTTPS（复用 TlsAdapter 兼容自签名证书） |
| 服务器地址/密码泄露到日志 | 日志脱敏，不打印凭据与完整地址 |
| CSRF | 写操作接口校验 `Origin/Referer` 或使用简单 token（同源 localhost 风险低，但保留校验） |

## 5. 错误处理与超时

- 远端请求统一设置 timeout，防止 SSL 握手卡死
- 远端返回非 200 或 JSON 解析失败 → 抛出 `ResilioApiError`，页面显示可读错误
- 登录失败、token 过期 → 提示重新登录
- 仪表盘某次轮询失败 → 前端保留上次数据并显示"连接中..."状态

## 6. 待实现阶段的技术关注点

| 阶段 | 技术关注点 |
|------|-----------|
| P1 重构客户端 | 重构后原有 CLI 必须无行为变化；新增方法先用独立脚本冒烟测试 |
| P2 Flask 骨架 | SECRET_KEY 管理；`g.client` 请求级生命周期 |
| P3 仪表盘 | 合并请求性能；轮询防抖；DOM 更新最小化 |
| P5 设置 | `?action=settings` 的 GET/POST 参数格式需实测确认 |
| P6 导入导出 | 复用 resilio_api 的 CSV 读写与编码处理逻辑 |

## 7. sync-guard 集成架构（2026-08-05）

### 7.1 总览

```
浏览器 ──► 面板(本地 :5000) ──► ① Resilio GUI API(:8888)   [已有]
                            └──► ② guard HTTP 接口(:8890)  [新增, token]
                                     │ 读 guard-status.json / env
                                     │ 写 env + systemctl restart resilio-sync-guard
```

服务器端 sync-guard 项目见 [sync-guard/](../sync-guard/)，含 [sync_guard.py](../sync-guard/sync_guard.py)（守护）与 [sync_sched.py](../sync-guard/sync_sched.py)（调度器）。

### 7.2 面板显示调度判定（G1，纯面板侧）

- [app.py](../app.py) 把 sync-guard 目录加进 `sys.path` 后 `import sync_sched`，复用其**纯函数** `grade`（D/C/B/A 分级）
- `_build_status`（后台缓存构建处）：从缓存文件夹的 peers（含 online/updiff/downdiff）计算每文件夹 metrics（need=待上传对端数 / seeded=完全同步对端数 / dneed=待下载对端数），调 `sync_sched.grade` 得分级；往 enriched folder 加 `grade` 字段，并加 `sched` 数组供「调度」页使用
- 「调度」页 `/sched` 轮询 `/api/status`，显示真实文件夹名 + 分级 + 需求指标 + 判定说明

### 7.3 guard HTTP 接口（G4，服务器端新增）

- [sync_guard.py](../sync-guard/sync_guard.py)：主循环周期性写 `guard-status.json`（NRestarts、最近备份 ts、健康检查 fail_streak/low_thread_streak、最近重启时间/原因、调度模式）——只含计数与时间戳，脱敏
- 新增 [sync-guard/guard_webapi.py](../sync-guard/guard_webapi.py)：stdlib `http.server`，端口 8890，Bearer token 认证（`RSL_API_TOKEN`）
  - `GET /api/status` → 读 guard-status.json + prefs.json 时间戳 → 守护状态
  - `GET /api/sched` → 读 `/etc/resilio-sync-guard.env` 的 `RSL_SCHED_*` → 当前配置
  - `POST /api/sched` → 白名单校验（mode、max_running、seed_limit、run_min_stay、preheat_sec、权重 w_*）→ 重写 env → `systemctl restart resilio-sync-guard`
- systemd 单元示例 + 部署说明写入 [sync-guard/resilio-sync-SOP.md](../sync-guard/resilio-sync-SOP.md)

### 7.4 面板守护页与调度控制（G2/G3）

- [app.py](../app.py)：`GuardApi` 客户端类（requests，GET/POST，Bearer token）；新端点 `/api/guard/status`、`/api/guard/sched`(GET/POST)
- [settings.html](../templates/settings.html)：「守护接口」配置卡（base_url + token，存 Flask session，与 GUI 凭据同等待遇）
- 「守护」页 `/guard`：核心状态展示 + 控制表单（模式 select + 常用参数）
- 面板中 guard token 存 session（不写盘）；控制接口白名单校验

### 7.5 安全模式（2026-08-05 新增）

**核心洞察**：跨 OOM 真正持久的 paused 状态在 **guard 备份 prefs.json**（原子写 + 每次 OOM 重启后 `restore_prefs` 强制回放），rslsync 自身的写入会被 SIGKILL 打丢。因此"直接修改数据库暂停所有任务"落地为**改写这份备份**，不碰 rslsync 内部 SQLite（风险高、无官方 schema）。

**流程**（sync_guard.py 主循环）：

```
NRestarts 增长(检测到 OOM)
  → _record_oom() 记录滑窗(600s)内时间戳
  → 次数 >= 阈值(4) 且未处于安全模式
      → _enter_safe_mode()：直接改写备份为全部 paused + 记状态
  → restore_prefs()：安全模式时强制全部暂停(覆盖备份)，否则按备份恢复
  → 调度器继续运行：按其判定恢复应运行的文件夹（安全模式不抑制调度器）
解除：无新 OOM 持续 SAFE_EXIT_QUIET(1800s) → 安全模式关闭，恢复常规调度
```

**状态暴露**：status.json 增加 `safe_mode` 布尔，guard_webapi GET /api/status 自动透传，面板「守护」页可显示。

**配置项（面板可改，2026-08-06）**：`RSL_SAFE_OOM_WINDOW` / `RSL_SAFE_OOM_THRESHOLD` / `RSL_SAFE_EXIT_QUIET`。guard_webapi `SCHED_KEYS` 白名单新增 `safe_oom_window` / `safe_oom_threshold` / `safe_exit_quiet`（小写面板键 → env 键），POST /api/sched 校验最小值（阈值≥2、窗口/解除≥60）后写 env + 重启 guard；面板「守护」页控制表单新增安全模式参数区（guard.html + app.js `SCHED_FIELD_MAP`），并显示安全模式激活徽标。

**限并发策略（2026-08-06）**：生产 OOM 根因 = rslsync 并发运行文件夹数多 → 内存顶到 1.8G（87 文件夹静态索引基线低，运行集把内存推到峰值 1.82G）。`RSL_SCHED_MAX_RUNNING` 由 5 调 2，限制同时服务数；不断开文件夹（避免重索引）。安全模式治风暴不治稳态超限，限并发才是稳态解法。

### 7.6 断开文件夹定期重连（G8，2026-08-06 新增）

**数据文件 conn.json**（`/var/lib/resilio-sync-guard/conn.json`，600 权限，与 prefs.json 同级敏感）：

```json
{
  "last_attempt": 1754500000.0,          // 全局节奏钟：最近一次重连尝试时间（每 interval 至多尝试 1 个）
  "folders": {
    "<fid>": {
      "path": "/srv/sync/folderA",        // 断开前记录的服务器本地路径（Resilio 断开后清空）
      "last_connected": 1754500000.0,     // 该文件夹上次重连时间
      "disconnect_count": 2,              // 连续被再断开次数（退避基数）
      "next_connect": 1754520000.0        // 退避后的下次可重连时间
    }
  }
}
```

**写入点**：
- `_disconnect_redundant_seeders`（G6 断开）→ 断开**前**记录 `path`（从 LAST_SNAPSHOT 取，backup_prefs 需把 `path` 加进快照）到 conn.json；断开后置 `disconnect_count+1`、`next_connect = now + min(interval * 2^count, MAX_BACKOFF)`、`last_connected = now`
- 重连成功 → 更新 `last_connected = now`（`disconnect_count` 保留，若保持连接则后续由调度器接管，无需清零；再次被断开时 `disconnect_count+1` 延续退避）
- 手动断开（面板）不写 conn.json，不受定期重连干预

**重连循环**（主循环 backup 周期内，30s 检查一次，节奏由 conn.json 控制）：

```
安全模式激活 → 跳过
now - last_attempt < RSL_RECONNECT_INTERVAL(600) → 跳过（节奏钟）
内存闸门：_read_mem_gate()  rss/cg_max > MEM_GATE_PCT(75) → 跳过
磁盘闸门：_read_io_busy()  %util > IO_GATE_PCT(90) → 跳过
候选 = conn.json 中 当前 synclevel==0（从 LAST_SNAPSHOT 判）且 now >= next_connect
无候选 → 跳过
选 last_connected 最早的 1 个 → addsyncfolder(path, secret)
成功：last_attempt=now；更新 last_connected；log "定期重连: fold=%s 已重连"
失败：last_attempt=now；log 不带动作行标记（不进历史，仅排障）
```

**闸门读取**：sync_guard 独立进程，不复用 guard_webapi.read_mem——自读 `/proc/<pid>/status`（VmRSS）+ cgroup `memory.max`；磁盘 IO 忙度读 `/proc/diskstats` 两次采样差分（io_ticks 增量/间隔，即 iostat %util），`df` 定位存储目录所在盘（设备去分区号映射回整盘）。

**与调度器协同**：`decide()`（sync_sched）跳过 `synclevel==0` 的文件夹，与面板侧 [app.py:141](app.py#L141) 对齐——避免对断开文件夹发无意义 resume 指令；重连后的文件夹回到快照成为正常候选，运行与否由 `max_running=2` 决定（重连 ≠ 运行）。

### 7.7 调度历史扩充（G9，2026-08-06 新增）

**日志行分类**（read_history 消费 guard 日志，见 design-standards §4.2 的行格式约定）：

```
动作行（计入 50 条配额）：
  调度器暂停/恢复  含 动作=暂停|恢复
  断开            含 冗余做种断开: fold=
  重连            含 定期重连: fold=
汇总行（不计配额，可开关显示）：含 周期# 且非上述动作行
不采集：断开批量汇总（本轮共 N 个）、重连失败/闸门拦截（无动作行标记）
```

**read_history(limit) 配额规则**：整文件遍历收集"调度[ 或 冗余做种断开: fold= 或 定期重连: fold="的行；从末尾向前数，**动作行计入 limit**，收集到的区间内汇总行一并保留；到 limit 即停，再整体反转恢复时间序。

**前端渲染**（app.js renderHistoryLine）：
- 三态动作行：暂停/恢复（现有）+ 断开（徽标「断开」）+ 重连（徽标「重连」）；断开/重连行 分级/需上传/待下载/做种/上传速度列留空，备注列放 seeded 数等
- **名字映射改从 `/api/status` 的 `folders`（全量，含断开）构建**，当前用 `sched`（排除断开）导致断开文件夹显示 id 前缀

**截断 bug 修复**：汇总行 `rest` 提取 `line.split('] ')` 会按**每次** `] ` 拆分，`调度[on] ` 内的 `] ` 使 `[1]` 变成「调度[on」→ 改为 `line.split('] ', 1)`（仅首个 `] `，时间戳后）。
