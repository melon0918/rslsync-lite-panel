# 开发步骤与分阶段计划（Development Plan）

> 说明：本文档为开发过程的历史执行记录。文中提到的 devlog/、CLAUDE.md、main.py 等属于原开发工作目录的文件，未包含在本仓库。

## 0. 推进原则

1. **分阶段、小步走**：每个阶段独立完成、独立验证，通过后才进入下一阶段。禁止"一口气做太多"。
2. **main.py 不回归**：它是备用 CLI，任何重构都保证其行为不变。
3. **每阶段收尾**：更新当天 devlog（完成/待办），必要时同步更新 docs。
4. **先文档后代码**：实现前阅读 requirements / technical-design / design-standards。

## 1. 阶段总览

| 阶段 | 名称 | 交付物 | 验证方式 |
|------|------|--------|----------|
| P0 | 项目脚手架 | docs/、devlog/、CLAUDE.md、requirements.txt | 本计划 + 文档完整 |
| P1 | 重构 API 客户端 | resilio_api.py；main.py 改为 import | CLI 回归 + 冒烟脚本 |
| P2 | Flask 骨架与登录 | app.py、base/login 模板、session | 浏览器登录成功 |
| P3 | 仪表盘 | /api/status、dashboard、轮询 JS | 浏览器实时刷新 |
| P4 | 节点列表 | /api/peers、peers.html | 浏览器显示节点 |
| P5 | 设置与文件夹操作 | settings API/页面、pause/resume/remove | 浏览器操作生效 |
| P6 | 导入导出与批量 | export/import、pause-all/resume-all | 浏览器/脚本验证 |
| P7 | 整体验证与性能对比 | 全功能回归 + 负载对比 | 验收标准逐项通过 |

## 2. 阶段详情

### P0 — 项目脚手架（当前）

- [x] 创建 docs/（需求、技术设计、设计规范、本计划、API 参考）
- [x] 创建 devlog/（README、模板、当日日志）
- [x] 创建 CLAUDE.md（文档指引 + 工作说明）
- [x] 创建 requirements.txt（flask>=3.0、requests、urllib3）
- [x] 配置 SessionStop hook（每日 devlog 自动记录）
- **完成标准**：本计划所列文件全部就位，格式符合约定

### P1 — 重构 API 客户端（resilio_api.py）

目标：从 main.py 提取 `TlsAdapter` + `ResilioSyncClient`，新增只读/操作 API。

- [x] 1.1 新建 `resilio_api.py`，原样迁移 `TlsAdapter`、`ResilioSyncClient` 的登录与 4 个既有方法
- [x] 1.2 `main.py` 删除类定义，改为 `from resilio_api import ...`，保持 CLI 交互与行为完全一致（管道回归测试通过）
- [x] 1.3 新增只读方法：`get_folder_list`（getsyncfolders&discovery=1）、`get_peers_stat`、`get_session_stats`、`get_statuses`、`get_settings`
- [x] 1.4 新增操作方法：`set_speed_limits`（setsettings）、`pause_folder`、`resume_folder`、`remove_folder`
- [x] 1.5 定义 `ResilioApiError`，统一 `_request` 超时与错误处理
- **验证（已完成）**：main.py 回归通过；真机验证限速设置/还原、单文件夹暂停/恢复生效；响应结构已记录到 api-reference.md

### P2 — Flask 骨架与登录

- [x] 2.1 创建 `app.py`：Flask 实例、SECRET_KEY 管理（env/instance 持久化）、`@login_required`、`g` 生命周期（按请求建客户端）
- [x] 2.2 `/login` GET/POST：登录表单 → 客户端实例 → `login()` 验证 → 写 session（含 token）
- [x] 2.3 `templates/base.html`、`templates/login.html`
- **验证（已完成）**：未登录访问 /api/* 返回 401；登录后 session cookie 可用；错误凭据有提示

### P3 — 仪表盘

- [x] 3.1 `/api/status`：仅调用 getsyncfolders(discovery=1)，聚合文件夹 + 全局速度（速度由 down_speed/up_speed 求和）
- [x] 3.2 `templates/dashboard.html`：摘要卡片（总数/同步中/暂停/下载/上传速度）+ 文件夹表格（名称/路径/状态/进度/速度/节点/大小/操作）
- [x] 3.3 `static/js/app.js`：`startPolling` + `updateDashboard` + 错误处理
- **验证（已完成）**：真机轮询显示同步中/进度/速度；暂停/恢复按钮 POST 后状态更新

### P4 — 节点列表

- [x] 4.1 `/api/peers`：getpeersstat（活跃）+ 合并 folder.peers（全部）
- [x] 4.2 `templates/peers.html` + JS 更新函数，与仪表盘共享轮询
- **验证（已完成）**：节点表格显示名称/ID/在线/速度/所属文件夹

### P5 — 设置与文件夹操作

- [x] 5.1 `/api/settings` GET/POST：读取/保存限速（实测 `setsettings&dlrate/ulrate`，KB/s）
- [x] 5.2 `templates/settings.html`：下载/上传限速输入框（KB/s，0=不限速）
- [x] 5.3 `/api/folder/<id>/pause|resume|remove`（remove 留待 UI 试用确认后真机验证）
- **验证（已完成）**：限速设置/还原生效；单文件夹暂停/恢复生效；同源校验（Origin）生效

### P6 — 导入导出与批量操作

- [x] 6.1 `/api/export`：CSV 下载（复用导出字段，readonlysecret 此版本响应缺失为空列）
- [x] 6.2 `/api/import`：上传 CSV 批量导入（密钥类型选择，自动 force 重试）
- [x] 6.3 `/api/pause-all`、`/api/resume-all`：遍历文件夹设置暂停/恢复
- **验证（已完成）**：导出内容正确；批量/导入路由可用（未做破坏性导入验证）

### P7 — 整体验证与性能对比

- [ ] 7.1 按 requirements.md 验收标准逐项测试
- [ ] 7.2 对比远端 Web UI 与本应用的 API 请求量/响应
- [ ] 7.3 完善 docs 与实际实现差异
- **完成标准**：全部验收标准通过，文档与实现一致

## 3. 每阶段收尾检查清单

- [ ] 功能按需求实现，无"做了一半"
- [ ] 遵守 design-standards.md
- [ ] main.py 未回归
- [ ] devlog 已记录完成事项与待办
- [ ] 相关 docs 已同步

## 4. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| `?action=settings` 参数格式未知 | P5 阻塞 | P5 开始时先连远端实测，记录到 api-reference.md |
| 远端服务器旧版 API 字段缺失 | 面板显示为空 | 字段取值全部用 `.get()` 兜底，前端显示 "--" |
| 自签名证书/弱 TLS | 连接失败 | 复用 TlsAdapter，兼容模式已验证 |
| 轮询加重远端负担 | 违背核心目标 | 轮询默认 5 秒可调；visibilitychange 停止后台轮询 |
| session 存密码的泄露风险 | 凭据泄露 | 仅 localhost；随机 SECRET_KEY；不写盘 |

## 5. 当前进度

- 2026-08-04：P0 完成（脚手架 + 文档 + hook）。
- 2026-08-04：P1 完成（resilio_api.py 重构 + main.py 回归 + 真机验证）。
- 2026-08-04：P2-P6 完成（Flask 应用 + 仪表盘/节点/设置/导入导出/批量，已真机验证路由与 UI 渲染）。**可功能试用**。
- 下一步：P7 整体验收（用户浏览器试用后按验收标准逐项核对；移除文件夹功能需确认后真机验证）。
- 2026-08-06：G6（冗余做种断开）、G7（仪表盘内存卡片）、G9（调度历史）已部署生产并验证；**G8（定期重连）已全部实现并部署生产**（folderid bug 修复、conn.json 43 条、IO 闸门 50%+5min 滚动平均、节奏钟/首采样修复、面板配置回填默认值、调度判定页分级随生产配置）。当前：~18/43 已重连复查，其余被 IO 闸门推迟（盘索引波期 70-88% 忙），盘均值回落 <50% 后自动恢复。has_files 索引期间早判方案放弃（`has_files`/diff 哈希期不可靠）。监控已停止，可续。

## 6. sync-guard 集成阶段（2026-08-05 新增）

需求见 [requirements.md](requirements.md) §7，架构见 [technical-design.md](technical-design.md) §7。

| 阶段 | 名称 | 交付物 | 验证 |
|------|------|--------|------|
| G0 | 文档与规范更新 | docs + CLAUDE.md 登记集成需求/架构/阶段 | 文档齐全、与确认需求一致 |
| G1 | 面板显示调度判定 | /api/status 加 sched、调度页、仪表盘分级徽标 | 真机验证 sched 数据 + 渲染 |
| G2 | guard HTTP 接口 | sync_guard 写 status.json + guard_webapi.py | 用户部署后接口可用、token 认证 |
| G3 | 面板守护页 + 控制 | GuardApi、/api/guard/*、guard.html、设置配置 | 本地 mock guard API 验证 |
| G4 | 联调 | 面板连真实 guard 接口端到端 | 部署后联调 |
| G5 | 安全模式 | sync_guard 滑窗 OOM 检测 + 改写备份为全部暂停 + 强制回放 + 自动解除；status.json 加 safe_mode | 本地单测（模拟 API）+ 用户部署后观察 |
| G6 | 冗余做种断开 | sync_guard `_disconnect_redundant_seeders` + guard_webapi `disconnect_seeded_*` 配置 + 面板 | **已完成（2026-08-06）**：部署生产，首轮断开 22 个、rss 降 ~400MB |
| G7 | 仪表盘内存占用 | guard_webapi `read_mem()` + 面板内存卡片 | **已完成（2026-08-06）**：部署生产，`/api/status` 返回真实 mem（rss=872MB/cg_max=1.8G→48% 绿） |
| G8 | 断开文件夹定期重连 | sync_guard 重连循环 + conn.json + 两道闸门 + 退避 + guard_webapi 配置 + 面板 | 分 4 子阶段（G8-1~G8-4），见下方 |
| G9 | 调度历史扩充 | guard_webapi 历史配额/筛选 + 前端动作行 + 汇总截断修复 | 分 2 子阶段（G9-1~G9-2），见下方 |

### G1 内容
- `app.py` 引入 sync_sched（sys.path 加 sync-guard 目录），`_build_status` 计算每文件夹 metrics（need/seeded/dneed）+ 复用 `sync_sched.grade` 得分级；folder 加 `grade` 字段、载荷加 `sched` 数组
- 新增「调度」页 `/sched`（真实文件夹名 + 分级徽标 + 需上传/做种/待下载对端 + 当前状态 + 判定说明）
- 仪表盘名称单元格加分级小徽标（D 红 / C 橙 / B 绿 / A 灰）；导航加「调度」

### G2 内容
- `sync_guard.py` 主循环周期性写 `guard-status.json`（脱敏：只含计数与时间戳）
- 新增 `guard_webapi.py`（http.server，:8890，Bearer token）：GET /api/status、GET/POST /api/sched；POST 白名单校验后重写 env + `systemctl restart resilio-sync-guard`
- 部署说明写入 sync-guard/resilio-sync-SOP.md

### G3 内容
- `GuardApi` 客户端 + `/api/guard/status`、`/api/guard/sched`(GET/POST)
- settings.html「守护接口」配置卡（base_url + token 存 session）
- 新增「守护」页 `/guard`：核心状态 + 控制表单（模式 + 常用参数）

### G4 内容
- 用户部署 guard_webapi.py + sync_guard.py 改动到服务器；面板连真实接口端到端验证

### G5 内容（2026-08-05 新增，2026-08-06 扩展）
- `sync_guard.py`：滑窗 OOM 检测（`_record_oom`）+ 触发时 `_enter_safe_mode`（改写备份为全部 paused，兜底用上次备份 id）+ `restore_prefs` 安全模式强制全部暂停 + **调度器继续运行**（按判定恢复应运行的文件夹）+ 稳定 `SAFE_EXIT_QUIET` 秒后自动解除；status.json 加 `safe_mode`
- 配置项：`RSL_SAFE_OOM_WINDOW`（600）/ `RSL_SAFE_OOM_THRESHOLD`（默认 4，生产调 3）/ `RSL_SAFE_EXIT_QUIET`（1800）
- 2026-08-06 扩展：guard_webapi `SCHED_KEYS` 白名单加 safe_* 三键（面板可改，含最小值校验）；面板 guard.html/app.js 加安全模式参数区 + 激活徽标；`RSL_SCHED_MAX_RUNNING` 生产调 2（限并发压内存）
- 验证：guard_webapi 本地测试（GET 含 safe 键、阈值 3 接受、阈值 1/窗口 30 拒绝、401）；部署后重启机器验证服务 + 面板读写

### G8 内容：断开文件夹定期重连（2026-08-06 新增）

需求见 requirements §7.7，设计见 technical-design §7.6。按子阶段独立推进：

- **G8-1 重连核心（sync_guard.py）**（**2026-08-06 已完成，本地单测通过**）：conn.json（断开前记 path，`backup_prefs` 把 path+secret 加进 LAST_SNAPSHOT）；重连循环（节奏钟每 `RSL_RECONNECT_INTERVAL` 重连 1 个 + 退避 `next_connect`，addsyncfolder 目录非空 106 自动 force）；内存闸门（sync_guard 自读 VmRSS/cg_max）；安全模式冻结；调度器 `decide()` 跳过 `synclevel==0`；重连日志 `定期重连: fold=…`（成功才写，跳过/失败不带动作行标记）。验证：mock API + mock /proc 单测全过；生产部署待 G8-4
- **G8-2 磁盘 IO 忙度闸门**（**2026-08-06 已完成，本地单测通过**）：`_find_disk`（df 定位 `RSL_STORAGE_PATH` 挂载设备 → 整盘名，缓存）+ `_sample_io`（/proc/diskstats io_ticks 差分算 %util，主循环每备份周期刷新）+ `_io_pct >= RSL_RECONNECT_IO_GATE_PCT`(90) 阻止重连。验证：mock df/diskstats/时钟单测全过；生产部署待 G8-4
- **G8-3 面板可配**（**2026-08-06 已完成，冒烟通过**）：guard_webapi `SCHED_KEYS` 白名单加 `reconnect_on/interval/max_backoff/mem_gate_pct/io_gate_pct`；`SCHED_MIN`（interval/backoff ≥60、gate ≥1）+ `SCHED_MAX`（gate ≤99）+ `reconnect_on` 0/1 开关校验；面板「守护」页定期重连表单（guard.html + app.js `SCHED_FIELD_MAP`）。验证：mock env 冒烟（GET 含键、合法 200 + env 写入、非法值/非法键 400、401）全过；生产部署待 G8-4
- **G8-4 部署 + 真机实测**（**2026-08-06 已完成，生产运行中**）：备份 + 上传 3 文件 + 编译 + env 开 `RSL_RECONNECT_ON=1`/`RSL_STORAGE_PATH=/mnt/sync/` + 重启。**实测 `addsyncfolder` 重连断开文件夹**成功（首轮 `定期重连: fold=… 已重连`，备份数 44→45）。**发现并修复 folderid bug**：断开文件夹只有 `folderid` 无 `id`，识别统一 `id or folderid`。conn.json 为 43 个断开文件夹建条目（path=/mnt/sync/名称）。监控中（Monitor 每 60s 轮询生产日志）。回填 api-reference.md + SOP

### G9 内容：调度历史扩充（2026-08-06 新增）

需求见 requirements §7.8，设计见 technical-design §7.7。按子阶段独立推进：

- **G9-1 历史 bug 修复 + 配额规则**（**2026-08-06 已完成，本地验证通过**）：修复周期汇总截断（`line.indexOf('] ')` 切首个 `] `——JS 的 `split('] ', 1)` limit 语义是截断数组非只拆首个，node 测试排除）；`read_history(limit)` 改为**动作行计入 limit、汇总行不占配额**（向后扫描数动作行、保留区间内汇总行）。验证：单测 + node + HTTP 冒烟全过；生产部署待 G8-4
- **G9-2 历史动作行扩充**（**2026-08-06 已完成，本地验证通过**）：`read_history` 筛选放宽（`冗余做种断开: fold=` / `定期重连: fold=` 行，排除批量汇总与失败）；前端 `renderHistoryLine` 重构 `actionRow` 助手渲染断开/重连独立动作行（徽标 + 备注，指标列留空）；名字映射改从 `/api/status` `folders` 全量构建（断开文件夹显示真实名）。验证：单测 + node + HTTP 冒烟全过；生产部署待 G8-4
