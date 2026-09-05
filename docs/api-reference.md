# Resilio Sync GUI API 参考

远端 `http(s)://<host>:8888/gui/`。以下结构均为 2026-08-04 对真实服务器（Resilio Sync，Web UI 版本 2025-10-28）实测确认。客户端实现在 `resilio_api.py`。

## 1. 认证

```
GET /gui/token.html?t=<ms>        （Basic Auth）
```
响应 HTML 中 `<div id='token'>...`，用正则 `id='token'[^>]*>([^<]+)</div>` 提取。

所有后续请求（GET）均带：
- `token=<token>`
- `t=<当前毫秒时间戳>`（防缓存）

> 所有 action 都是 GET 到 `/gui/`，参数经 `params=` 传递。对 `/gui/` 发 POST 会返回 Web UI 的 HTML 页面（非 API），不要用 POST。

## 2. 已实测接口

### 2.1 getsyncfolders — 文件夹列表（含状态/速度/节点）
```
GET /gui/?action=getsyncfolders&discovery=1&token=<t>&t=<ms>
→ {status: 200, folders: [...]}
```
`discovery=1` 为 Web UI 标准用法（比不带 discovery 多返回 peers 等明细）。

**folder 对象关键字段：**

| 字段 | 类型 | 含义 |
|------|------|------|
| id / folderid | str | 文件夹 ID（两字段相同） |
| name / path | str | 名称 / 路径 |
| status | int | 同步状态（见 §3，随阶段变化） |
| paused | bool | 是否暂停 |
| error | int | 0 正常，非 0 错误 |
| down_speed / up_speed | int | 当前下载/上传速度（字节/秒） |
| down_status / up_status | int | 下载/上传进度百分比 0-100（100=完成） |
| down_eta / up_eta | int | 预计剩余秒数 |
| files / local_files / ondisk_files / tree_files | int | 文件数 |
| size / local_size / ondisk_size / tree_size | int | 字节 |
| available_space | int | 剩余磁盘空间（字节） |
| onlinepeerscount | int | 在线节点数 |
| peers | array | 节点明细（见 2.2 folder.peers） |
| is_local / is_owner / iswritable | bool | 归属属性 |
| loading / indexing / rescanning / stopped / remoteindexing | bool | 状态标志 |
| queue_download_files / queue_upload_files 及 _size | int | 队列统计 |
| secret / encryptedsecret | str | 密钥（敏感，勿打印） |
| secrettype | int | 2=加密/读写 |
| share_id | str | 共享 ID |
| warnings / errors | array | 警告/错误 |
| date_added / last_modified / firstsynccompleted / totallastsynccompleted | int | 时间戳 |

### 2.2 folder.peers — 单文件夹节点明细
```
[{'id': '20153D...', 'name': 'DESKTOP-VMGNAJN', 'isonline': bool, 'direct': bool,
  'has_files': int, 'has_files_size': int, 'updiff': int, 'downdiff': int,
  'upfiles': int, 'downfiles': int, 'lastsynctime': int, 'lastreceivedtime': int,
  'lastsenttime': int, 'userid': ''}]
```

### 2.3 getpeersstat — 全局当前连接节点
```
GET /gui/?action=getpeersstat&token=<t>&t=<ms>
→ {status: 200, value: [...]}
```
仅返回**当前活跃连接**的节点；无活跃传输时为空数组（注意：不是报错）。
```
[{'connection_type': 8, 'direct': True, 'id': 'EAUY...', 'loss_rate': -1,
  'name': 'HOUMUKING', 'online': True, 'ping': 0,
  'speed': {'down': 0, 'up': 26340}, 'transfer_id': '<folderid>'}]
```
速度单位为字节/秒。`transfer_id` 为该连接所属文件夹 ID。

### 2.4 getsessionstats — 会话统计
```
GET /gui/?action=getsessionstats&token=<t>&t=<ms>
→ {status: 200, value: {
     max_speed: {down, up},        # 历史最大速度（字节/秒）
     total_transferred: {down, up},# 累计传输（字节）
     transferred: {down, up}}}     # 本会话传输（字节）
```
**无当前速度字段**。当前全局速度从 getstatuses 或文件夹速度求和得到。

### 2.5 getstatuses — 全局状态
```
GET /gui/?action=getstatuses&token=<t>&t=<ms>
→ {status: 200, value: {cpu, disk, errors, loading, warnings,
     speed: {downspeed, upspeed}}}   # 当前全局速度（字节/秒）
```

### 2.6 settings — 读取全局设置
```
GET /gui/?action=settings&token=<t>&t=<ms>
→ {status: 200, value: {dlrate, ulrate, devicename, listeningport, webui_port,
     autostart, check_update, debug_logging, portmapping, ...}}
```
**速度限制字段：`dlrate` / `ulrate`，单位 KB/s，`<=0` 表示不限速**（-1 = 不限速）。

### 2.7 setsettings — 写入全局设置（限速）
```
GET /gui/?action=setsettings&dlrate=<KB/s>&ulrate=<KB/s>&token=<t>&t=<ms>
→ {status: 200}
```
- **action 是 `setsettings`（不是 settings）**，参数直接拼在其后（Web UI JS 实现：`request("action=setsettings"+e)`）
- 值用字符串传递（`str()`），如 `dlrate=-1` 表示不限速
- 已验证：设置后 get_settings 立即返回新值

### 2.8 folderpref / setfolderpref — 文件夹暂停/恢复
```
GET /gui/?action=folderpref&id=<folder_id>&token=<t>&t=<ms>
→ {status: 200, value: {paused: bool, relay: bool, searchlan: bool,
     usehosts: bool, usetracker: bool, selectivesync: bool, deletetotrash: bool,
     override: bool, stopped: bool, transferpriority: int, secrettype: int, ...}}
```
设置：读取全部 prefs → 修改 `paused` → 全量回写
```
GET /gui/?action=setfolderpref&id=<folder_id>&paused=true&<全部字段>&token=<t>&t=<ms>
```
bool 转 `true`/`false`，其余 `str()`。已验证暂停/恢复生效。

### 2.9 removefolder — 移除 / 断开文件夹
```
GET /gui/?action=removefolder&folderid=<id>&deletedirectory=<true|false>&fromalldevices=<true|false>&token=<t>&t=<ms>
```
- **移除（删除）**：`deletedirectory=<bool>` + `fromalldevices=true`（从所有设备移除；deletedirectory=true 同时删磁盘文件）
- **断开连接**：`deletedirectory=false` + `fromalldevices=false` —— 仅从本设备断开，保留磁盘文件，文件夹转为断开状态（synclevel=0），之后可用 addsyncfolder 重新连接
- 已在真实服务器实测：断开→文件夹变 disconnected；重连（addsyncfolder）→ 恢复连接

### 2.10 addsyncfolder — 添加/重新连接文件夹
```
GET /gui/?action=addsyncfolder&path=<p>&secret=<s>[&name=<n>][&force=true]&token=<t>&t=<ms>
→ {status: 200, value: {error: 0|106|200|..., message?, path?, secret?}}
```
- **error=0**：新增成功
- **error=200**（SE_SM_DUPLICATE_FOLDER）：文件夹已存在 / 重新连接，实测返回消息「所选文件夹已添加到 Resilio Sync」，**同样视为成功**（resilio_api.add_folder 对 0/200 都判成功）
- **error=106**：目录不为空，加 `force=true` 重试
- **重新连接断开文件夹**：官方「连接」就是对此 action 用文件夹原密钥重新调用（断开后本地路径可能丢失，UI 需让用户重新填路径）
- **guard 自动重连（2026-08-06，G8）**：sync_guard `_reconnect_folders` 复用此路径，用 conn.json 记录的断开前 path + 当前 secret 调用，error=106 时自动 `force=true` 重试（断开保留文件 → 目录非空必走 106）；成功才写历史动作行 `定期重连: fold=`（见 design-standards §4.2）

## 3. folder.status 数值说明（已实测 + Web UI JS 确认）

从 Web UI JS 的 `SyncConstants` 提取的 `SYNC_TRANSFER_STATUS_*` 映射（2026-08-04 确认）：

| 数值 | 常量 | 含义 |
|------|------|------|
| 0 | NONE | 无状态 |
| 1 | PAUSED | 已暂停 |
| 2 | STOPPED | 已停止 |
| 3 | RECEIVING | 下载中 |
| 4 | SENDING | 上传中 |
| 5 | SYNCING | 双向同步中 |
| 6 | INCOMPLETED | 未完成（部分文件） |
| 7 | SYNCED | 已同步 |
| 8 | NOPEERS | 等待节点（无源节点在线） |
| 9 | PENDING | 等待中 |
| 10 | INVALID | 无效 |
| 11 | NEVER_CONNECTED | 从未连接 |

**断开连接判定（关键）**：`synclevel === 0` 表示文件夹被断开（Web UI JS：`disconnected = 0 === synclevel`）。断开文件夹的 API 表示**只返回稀疏字段**（`folderid/name/secret/synclevel` 等，无 `id/status/path/速度` 等），**不能**因字段缺失就当"已同步"。

**Web UI 状态判定顺序**（app.py `enrich_folder` 按此实现）：
1. `error != 0` 且文件夹**不在传输/同步中**（down=up=0 且 status∉{3,4,5}）→ 错误（errors[].data.description 作 tooltip）
2. `loading / indexing / rescanning` → 正在索引
3. `paused` 或 `status==1` → 已暂停
4. `synclevel==0` → 断开连接
5. 按 status 映射：2 已停止 / 3 ↓正在接收 / 4 ↑正在发送 / 5 ↓↑同步中 / 6 未完成 / 7 已同步（若 `remoteindexing && onlinepeers>0` 则 正在索引）/ 8 等待节点 / 9 等待中 / 10 无效 / 11 从未连接；status 缺失或 0 且无活动 → 断开连接（绝不当已同步）

**非致命错误降级为警告**：`error != 0` 但文件夹仍在接收/发送/同步（`down>0 或 up>0 或 status∈{3,4,5}`）时，如文件级错误 32799 "Can't download file"（error_scope=7，不阻断同步，Sync 会重试），状态降级为 `warning`，**保留传输状态与进度**，错误以红色「错误」徽标 + tooltip 单独标示。

`disconnected` 文件夹的 id 取 `id or folderid`（稀疏表示里只有 folderid）。

## 4. 请求约定（resilio_api.py 统一实现）

- `requests.Session` 挂载 `TlsAdapter`（`create_urllib3_context()` + `set_ciphers('DEFAULT@SECLEVEL=1')`），`verify=False`
- 统一入口 `_request(action, params, timeout=10)`：拼 token/t/action，非 200 或 JSON 解析失败抛 `ResilioApiError`；不输出密码与完整地址
- 既有 CLI 方法（get_sync_folders/add_sync_folders/pause_all_sync/resume_all_sync）保持原输出行为
