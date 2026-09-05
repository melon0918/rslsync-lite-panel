# Resilio Sync (rslsync) 内存/性能问题排查与加固 SOP

> 适用：Linux 低内存 VPS 上运行 Resilio Sync，出现 WebUI 打不开、系统负载飙升、整机卡死等问题的排查与加固。

---

## 1. 快速诊断（按顺序执行）

```bash
# 1) 系统负载 / 内存 / swap —— 判断是否过载或换页
cat /proc/loadavg
grep -E "MemTotal|MemAvailable|SwapTotal|SwapFree" /proc/meminfo

# 2) rslsync 进程是否存活、占用多少内存（RSS=物理内存，VmSwap=已换出）
pid=$(pgrep -x rslsync | head -1)
grep -E "State|Threads|VmRSS|VmSwap|VmPeak" /proc/$pid/status

# 3) 是否有大量 D 状态进程（IO 卡死信号）
ps -eo pid,stat,wchan:20,cmd | awk '$2 ~ /D/'

# 4) 服务日志与状态
journalctl -u resilio-sync --no-pager -n 30
systemctl show resilio-sync -p MemoryMax -p MemorySwapMax -p Restart
```

判断要点：
- 负载高但 rslsync CPU 占用不高、大量进程卡在 `ext4_buffered_write_`/`jbd2_journal_wait_up` → **磁盘被 swap 换页拖死**。
- `VmSwap` 持续增长、`MemAvailable` 逼近 0 → **内存膨胀 + 换页抖动**。

## 2. 典型根因

| 现象 | 根因 |
|---|---|
| 内存缓慢膨胀到数 GB，swap 满 | 大量对端/隧道连接对象堆积（老版本连接处理泄漏），或文件夹索引预加载 |
| 负载冲到 20+，SSH/WebUI 全卡 | swap 换入换出 I/O 打爆磁盘 → 全局 IO 阻塞（D 状态） |
| 日志归档 100MB×N 堆积 | 日志默认 100MB 旋转，频繁 gzip 大日志产生 CPU 尖峰 |
| 存储目录异常大 | 残留测试文件、历史日志归档未清理 |

## 3. 内存护栏与 zram 扩容（2026-08-04 最终方案）

### 3.1 override.conf

`/etc/systemd/system/resilio-sync.service.d/override.conf`：

```ini
[Service]
LimitNOFILE=1048576
IOReadIOPSMax=/dev/vda 350      # 限制磁盘 IOPS，降负载
IOWriteIOPSMax=/dev/vda 350
MemoryMax=1.2G                  # 内存硬上限（最优值，见 3.3）
MemorySwapMax=2G                # swap 硬上限（走 zram，见 3.2）
```

### 3.2 zram 压缩 swap（替代机械盘 swap）

**机械盘 swap 是负资产**（换页拖死磁盘 IO，swap 到 ~400M 就 sync 卡死）；但 **zram 是内存压缩，速度接近内存、完全不碰磁盘**。2G 物理机上用它把 rslsync 等效内存从 1.4G 扩到 ~3.4G，被杀周期从 ~1 分钟拉到 25 分钟+。

持久化 `/etc/systemd/system/zram-swap.service`：

```ini
[Unit]
Description=Create zram swap device
After=systemd-modules-load.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/sbin/modprobe zram
ExecStart=/bin/sh -c "echo 2147483648 > /sys/block/zram0/disksize && mkswap /dev/zram0 && swapon -p 100 /dev/zram0"

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable zram-swap   # 开机自动创建 2G zram swap
echo "vm.swappiness=40" > /etc/sysctl.d/99-zram.conf    # 关键参数，持久化
sysctl -p /etc/sysctl.d/99-zram.conf
```

### 3.3 参数搜索结果（2026-08-04 实测，4 方案×3 轮取平均存活周期）

**定格方案：MemoryMax=1.2G + swappiness=60（实测周期 15-19 分钟）**

| 方案 | MemoryMax | swappiness | 验证周期 |
|---|---|---|---|
| A | 1.4G | 60 | ~7 分钟 |
| B | 1.4G | 40 | ~7-8 分钟 |
| **C（定格）** | **1.2G** | **60** | **15-19 分钟** |
| D | 1.1G | 60 | ~7.4 分钟 |

要点：
- **MemoryMax=1.2G 是更长周期的关键**（1.4G 时物理余量小、OOM 更频繁；1.1G 又过度依赖换页）。系统底座不可回收需求约 230M。
- swappiness 40 vs 60 无明显差异（B 的 40 没有优势）。
- **测试陷阱**：参数搜索脚本设 25 分钟超时上限，进程存活超限时脚本跳走，"存活 25 分钟"≠稳定（B 首轮即假象）。判断稳定必须等真实 OOM kill 时间，或长时间观察 NRestarts 不增长。
- **不要用 `MemoryHigh`（2026-08-03 实测否决）**——节流会把进程按住卡死、MemAvailable 挤到 ~34M 濒临整机耗尽，比被杀重启危险得多。cgroup 只有 `max`（杀+重启）或 `high`（卡+系统濒危）两条路。
- OOM 是**全局 OOM**（物理 2G 被 rslsync + zram 压缩数据 + 系统占满），非 cgroup 限制触发；rslsync 需求 ~3G 时物理 2G 是硬天花板，zram 只能延长周期、**无法完全根治**（除非降需求或换更大内存机）。
- 配合单元内 `Restart=on-failure`，被杀后 systemd 秒级拉起。
- **配置防丢失依赖 guard 脚本**（见第 9 节）：OOM 是 SIGKILL，进程来不及把 folder paused / 限速设置写盘，重启后回退；guard 在 OOM 后自动恢复。

应用方式：
```bash
systemctl daemon-reload
systemctl restart resilio-sync
systemctl show resilio-sync -p MemoryMax -p MemorySwapMax   # 确认生效
```

## 4. config.json 调优

`/etc/resilio-sync/config.json`（示例）：

```json
{
    "storage_path": "/var/lib/resilio-sync/",
    "pid_file": "/var/run/resilio-sync/sync.pid",
    "use_upnp": false,
    "log_size": 5,
    "folder_rescan_interval": 824000,
    "tunnel_protocols": "utp;utp2;tcp",
    "webui": {
        "force_https": true,
        "listen": "0.0.0.0:8888"
    }
}
```

各选项说明与坑：
- **`log_size`**：单位是 **MB**（默认 100 = 100MB）。想限制日志大小写 `5` 表示 5MB，**不是**字节数。
- **`folder_rescan_interval`**：文件夹扫描间隔秒数，调大可降低扫描频率。
- **`use_upnp`**：关闭 UPnP/NAT-PMP 打洞尝试，减少无谓连接。
- **`tunnel_protocols`**：传输协议列表（分号分隔）。去掉 `relay` 可禁本机**出站** relay。注意它**管不住 relay 服务器发起的入站隧道探测**。
- **`folder_defaults` 不能写进 config.json**——此版本会报 `Invalid key 'folder_defaults'` 导致**启动失败**。该设置只在 WebUI 全局设置里有效。
- 无效键会导致 rslsync 启动即退出，systemd 进入重启循环（`journalctl` 里看 `Can't parse config file`）。

> 判断某配置键本版本是否支持：`grep -c "键名" /usr/bin/rslsync`，返回 ≥1 说明二进制里存在。

## 5. 存储目录清理

```bash
du -sh /var/lib/resilio-sync/                         # 看总量
du -ah /var/lib/resilio-sync/ | sort -rh | head       # 看大头
ls /var/lib/resilio-sync/*.logtozip | wc -l           # 日志归档数
# 删除旧日志归档（保留最新一份）并截断活动日志
cd /var/lib/resilio-sync
newest=$(ls -1 sync.log.*.zip.logtozip | sort -V | tail -1)
ls -1 sync.log.*.zip.logtozip | grep -vxF "$newest" | xargs -r rm -f
truncate -s 0 sync.log
```

## 6. WebUI API 批量操作（逐文件夹设置）

登录端点**是 `token.html` 不是 `token.php`**，且要求 HTTP Basic 认证 + 老 TLS 密码套件：

```bash
ts=$(date +%s%3N)
resp=$(curl -sk --ciphers "DEFAULT@SECLEVEL=1" -u <user>:<pass> \
      "https://127.0.0.1:8888/gui/token.html?t=${ts}")
token=$(echo "$resp" | sed -n "s/.*display:none;'>\([^<]*\)<.*/\1/p")

# 列文件夹
curl -sk --ciphers "DEFAULT@SECLEVEL=1" -u <user>:<pass> \
  "https://127.0.0.1:8888/gui/?token=${token}&action=getsyncfolders&t=${ts}"

# 读单文件夹配置（返回 value 对象）
curl -sk --ciphers "DEFAULT@SECLEVEL=1" -u <user>:<pass> \
  "https://127.0.0.1:8888/gui/?token=${token}&action=folderpref&id=<fid>&t=${ts}"

# 写单文件夹配置：把读到的全部字段回传，bool 转小写字符串
curl -sk --ciphers "DEFAULT@SECLEVEL=1" -u <user>:<pass> \
  "https://127.0.0.1:8888/gui/?token=${token}&action=setfolderpref&id=<fid>&use_relay_server=false&..."
```

注意：
- folderpref 返回的字段名可能是 `relay`/`searchlan`/`usetracker`（短名），写回时按返回的键名。
- 批量操作务必**先备份**所有文件夹的 prefs（存 JSON），以便回滚。
- 服务器上若有 python3 + requests，可直接用 requests + `DEFAULT@SECLEVEL=1` 适配器跑批量逻辑（TLS 兼容性更好）。

## 7. 验证与监控

- 重启后：`grep VmRSS|VmSwap /proc/<pid>/status` 应回落；`MemAvailable` 应回升。
- WebUI：`curl -sk -o /dev/null -w "%{http_code}" https://127.0.0.1:8888/`（`force_https` 时用 https；301→`/gui/`→401 属正常，401=需登录）。
- 长期：写个循环脚本每 2 分钟采样 pid/load/RSS/VmSwap/WebUI 状态，只在「进程重启 / 负载>6 / VmSwap 接近上限 / WebUI 掉线」时告警。

## 8. 配置备份与回滚

- 改 config.json / override.conf 前先 `cp -a` 一份 `.bak`。
- 批量 API 改动前导出全部 folder prefs。
- 回滚：恢复备份文件 → `systemctl daemon-reload` → `systemctl restart resilio-sync`。

## 9. OOM 配置防丢失（guard 脚本）

### 9.1 背景：OOM（SIGKILL）丢配置

rslsync 对 folder paused、全局限速等配置是**延迟落盘**（改动先存内存，SQLite WAL 定期 checkpoint，实测 >40s 不落盘）。OOM kill 是 **SIGKILL**，进程没机会 flush → 最近未落盘的配置丢失，重启后回退到磁盘旧值；正常 `systemctl restart`（SIGTERM）会 flush，不丢。

排查结论（测试服务器 kill -9 复现）：SIGKILL 后 paused 修改回退、SIGTERM 后保留；且 **rslsync 无任何"落盘间隔"配置**（前端 JS + 二进制 grep 13 个候选键均无）。

### 9.2 guard 脚本（已部署）

`/usr/local/bin/sync_guard.py`，systemd 服务 `resilio-sync-guard.service`（WebUI 凭证在 `/etc/resilio-sync-guard.env`，600 权限）：

功能：
1. **周期备份**（每 30s）：导出全部 folder paused + 全局限速设置到 `/var/lib/resilio-sync-guard/prefs.json`（600）。
2. **OOM 检测恢复**：`NRestarts` 增长（仅由 on-failure/OOM 自动重启产生，`systemctl restart` 不增加计数）→ 用最近备份恢复 paused 和限速设置。
3. **会话统计记录**：每 30s 采样 transferred（down/up）到 `/var/lib/resilio-sync-guard/stats.log`——OOM 后 transferred 归零无法恢复，此文件保留历史累计。
4. **安全模式**（2026-08-05）：滑窗（600s）内 OOM 次数超过阈值（默认 4，即"10 分钟内超过 3 次"）→ 直接把备份改写为全部 paused 并强制回放，暂停所有文件夹打破 OOM 循环；**调度器继续运行**，按其判定恢复应运行的文件夹（非全部停摆）；无新 OOM 持续 `RSL_SAFE_EXIT_QUIET`（默认 1800s）后自动解除。status.json 增加 `safe_mode` 字段供面板显示。

   可调环境变量（追加到 `/etc/resilio-sync-guard.env` 后重启 guard）：
   - `RSL_SCHED_MAX_RUNNING=2`（2026-08-06 生产从 5 调为 2：限制同时运行文件夹数、压内存峰值——87 文件夹静态索引基线低，内存压力来自并发运行数）
   - `RSL_SAFE_OOM_WINDOW=600`（秒，滑窗）
   - `RSL_SAFE_OOM_THRESHOLD=3`（2026-08-06 生产从 4 调为 3：当前 OOM 密度最密 3 次/几分钟，4 达不到）
   - `RSL_SAFE_EXIT_QUIET=1800`（无新 OOM 持续该秒数后解除）
   - `RSL_DISCONNECT_SEEDED_ON=1` / `RSL_DISCONNECT_SEEDED_MIN=10`（2026-08-06 新增：做种人数超过阈值自动断开该文件夹节省内存；断开保留文件、面板可见可重连。生产已开启，首轮断开 22 个暂停文件夹、rss 降 ~400MB）
   - `RSL_RECONNECT_ON=1` / `RSL_RECONNECT_INTERVAL`(600) / `RSL_RECONNECT_MAX_BACKOFF`(86400) / `RSL_RECONNECT_MEM_GATE_PCT`(75) / `RSL_RECONNECT_IO_GATE_PCT`(90) / `RSL_STORAGE_PATH`(/mnt/sync/)（2026-08-06 新增：定期重连断开文件夹复查——每 interval 重连 1 个到期断开文件夹，两道闸门（内存 rss/cg_max≥75%、磁盘 IO 忙度≥90% 任一超限跳过），重连后交给冗余做种断开判定（需要就留着、冗余再断开），连续被再断开的走退避（interval×2^(count-1) 封顶 max_backoff）。conn.json（600）记断开时 path + 退避。生产已开启）
   - 以上 `RSL_SCHED_*` / `RSL_SAFE_*` / `RSL_DISCONNECT_SEEDED_*` / `RSL_RECONNECT_*` 参数均可在**面板「守护」页**修改（guard_webapi `SCHED_KEYS` 白名单，`safe_*` 阈值最小 2、窗口/解除最小 60；`disconnect_seeded_*` 开关限 0/1、阈值最小 1；`reconnect_interval/backoff` 最小 60、`reconnect_*_gate_pct` 1-99、`reconnect_on` 限 0/1）

关键实现点：
- 备份只用 `getsyncfolders` 自带的 paused 字段，**不调 folderpref**——后者在内存饱和时返回 HTTP 500。
- 恢复用 folderpref/setfolderpref（发生在 OOM 重启后、内存空闲时，正常）。
- 日志脱敏，不含文件夹标识；备份文件 600 权限，属敏感数据勿外泄。

运维：
```bash
systemctl enable --now resilio-sync-guard
tail -f /var/log/resilio-sync-guard.log   # 看"备份 N 文件夹" / "OOM 后恢复 N 个"
```

注意：guard 以 python3 运行，在全局 OOM 时可能成为"碰巧触发 OOM 检查"的进程，但被杀的总是 rslsync（最大进程），guard 自身内存小且 `Restart=always` 自动拉起，无需干预。

## 10. guard HTTP 管理接口（guard_webapi，2026-08-05）

供本地面板查看守护状态、读写调度器配置。监听 `:8890`，Bearer token 认证。

### 10.1 部署

```bash
# 1) 复制代码
cp /path/to/sync-guard/guard_webapi.py /usr/local/bin/guard_webapi.py
cp /path/to/sync-guard/sync_guard.py /usr/local/bin/sync_guard.py   # 含 status.json 写入的新版

# 2) token：追加到 guard env 文件（600 权限，已有 RSL_* 凭证）
echo 'RSL_API_TOKEN=<随机长token>' >> /etc/resilio-sync-guard.env

# 3) systemd 单元 /etc/systemd/system/resilio-sync-guard-webapi.service
#    [Unit]
#    Description=Resilio sync guard HTTP API
#    After=resilio-sync-guard.service
#    [Service]
#    Type=simple
#    EnvironmentFile=/etc/resilio-sync-guard.env
#    ExecStart=/usr/bin/python3 /usr/local/bin/guard_webapi.py
#    Restart=always
#    [Install]
#    WantedBy=multi-user.target

systemctl daemon-reload
systemctl enable --now resilio-sync-guard-webapi
systemctl restart resilio-sync-guard   # 让新版 guard 开始写 status.json
```

### 10.2 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/status | 守护状态：nrestarts、last_backup、fail_streak、low_thread_streak、last_restart、last_restart_reason、sched_mode、prefs_ts（脱敏，只含计数与时间戳）+ `mem`（2026-08-06 新增：rslsync 进程内存，脱敏）+ `io_pct`/`io_ready`/`io_gate_pct`（2026-08-06 新增：磁盘 IO 忙度滚动均值）+ `reconnect`（2026-08-06 新增：定期重连进展摘要，脱敏） |
| GET | /api/sched | 当前调度配置（RSL_SCHED_* 白名单参数） |
| POST | /api/sched | 修改调度参数（白名单校验）→ 重写 env → 重启 guard 生效 |

`mem` 字段（字节，读取失败则缺省）：`rss`/`vmswap`（/proc/<pid>/status）、`cg_current`/`cg_max`（cgroup v2 memory.current/max）、`mem_total`/`mem_avail`（/proc/meminfo）。面板仪表盘据此显示内存占用（RSS 占 cgroup 上限百分比，绿<70% / 橙 70-90% / 红≥90%，悬停看明细）。

`io_pct`/`io_ready`/`io_gate_pct` 字段（由 sync_guard 主循环写 status.json，webapi 透传）：`io_pct` = 存储盘 IO 忙度滚动均值（%）；`io_ready` = 是否已有有效差分采样（guard 刚启动为 false）；`io_gate_pct` = `RSL_RECONNECT_IO_GATE_PCT` 实际值。

`reconnect` 对象（由 sync_guard `_reconnect_summary()` 写 status.json，webapi 透传；只含计数与时间戳，不含文件夹 id/path）：`on`/`interval`/`max_backoff`/`mem_gate_pct`/`io_gate_pct`（重连参数）、`total`（conn.json 池内条目数）、`connected`/`disconnected`（当前按 LAST_SNAPSHOT synclevel 判定）、`reconnected`/`reconnect_ok_total`/`last_reconnect_ok`（累计重连成功过的条目数/总次数/最近成功时间）、`next_connect`（最早可重连时间）、`last_attempt`、`io_pct`/`io_ready`。面板仪表盘「定期重连」卡据此显示进度。

```bash
# 快速自测
curl -sk -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8890/api/status
curl -sk -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8890/api/sched
curl -sk -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"mode":"on","max_running":5}' http://127.0.0.1:8890/api/sched
```

### 10.3 说明

- 可写参数白名单：mode(off/dry/on)、max_running、seed_limit、run_min_stay、preheat_sec、权重 w_need/w_scar/w_speed/w_download/w_time/w_wait、安全模式 safe_oom_window/safe_oom_threshold/safe_exit_quiet、冗余断开 disconnect_seeded_on/disconnect_seeded_min、定期重连 reconnect_on/reconnect_interval/reconnect_max_backoff/reconnect_mem_gate_pct/reconnect_io_gate_pct
- 端口/路径/token 均可用 `RSL_API_*` 环境变量覆盖；token 必填，缺失时接口拒绝启动
- 接口返回与日志均脱敏（不含文件夹标识）；面板侧显示真实文件夹名由面板自行从 Resilio API 计算
