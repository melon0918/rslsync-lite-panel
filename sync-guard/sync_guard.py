#!/usr/bin/env python3
"""rslsync 配置守护：OOM 强杀后自动恢复 folder paused 状态。

生产环境 rslsync 由 systemd 护栏周期性 OOM 强杀（SIGKILL），
paused 修改未落盘时会在重启后回退。本守护：
1) 每 BACKUP_INTERVAL 秒导出全部 folder prefs（仅 paused 状态）到备份文件；
2) 检测到 NRestarts 增长（= 被 OOM 被动杀过，systemctl restart 不算）
   时，用最近备份恢复 paused 状态；
3) 安全模式：滑窗内 OOM 次数超过阈值时，直接把备份改写为全部暂停并强制回放，
   先暂停所有文件夹打破 OOM 循环；调度器继续运行，按其判定恢复应运行的文件夹
   （非全部停摆）；稳定 SAFE_EXIT_QUIET 秒后自动解除。

注意：备份文件含客户文件夹标识，属敏感数据，权限设 600，勿外泄。
"""
import json
import os
import re
import subprocess
import sys
import time

import sync_sched

import urllib3
import requests
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

WEBUI_URL = os.environ.get("RSL_WEBUI", "https://127.0.0.1:8888/gui/")
WEBUI_USER = os.environ.get("RSL_USER", "admin")
WEBUI_PASS = os.environ.get("RSL_PASS", "")
SERVICE = os.environ.get("RSL_SERVICE", "resilio-sync")
POLL_SEC = int(os.environ.get("RSL_POLL_SEC", "10"))
BACKUP_INTERVAL = int(os.environ.get("RSL_BACKUP_INTERVAL", "30"))
BACKUP_FILE = os.environ.get("RSL_BACKUP", "/var/lib/resilio-sync-guard/prefs.json")
STATS_FILE = os.environ.get("RSL_STATS", "/var/lib/resilio-sync-guard/stats.log")
LOG_FILE = os.environ.get("RSL_LOG", "/var/log/resilio-sync-guard.log")
# 假死健康检查参数
HEALTH_FAIL_LIMIT = int(os.environ.get("RSL_HEALTH_FAIL", "5"))       # WebUI 连续失败次数触发重启
HEALTH_THREAD_MIN = int(os.environ.get("RSL_HEALTH_THREAD_MIN", "8"))  # 线程数低于此值视为异常
HEALTH_THREAD_STREAK = int(os.environ.get("RSL_HEALTH_THREAD_STREAK", "3"))  # 连续异常次数触发重启

# 安全模式：短时间内多次 OOM 强杀 → 暂停所有文件夹并持久化，打破 OOM 循环
SAFE_OOM_WINDOW = int(os.environ.get("RSL_SAFE_OOM_WINDOW", "600"))       # 秒，滑窗统计 OOM 次数
SAFE_OOM_THRESHOLD = int(os.environ.get("RSL_SAFE_OOM_THRESHOLD", "4"))   # 窗内超过该次数(>3)进入安全模式
SAFE_EXIT_QUIET = int(os.environ.get("RSL_SAFE_EXIT_QUIET", "1800"))      # 进入后无新 OOM 持续该秒数 → 解除

# 冗余做种断开：做种人数(seeded=在线且完全同步的对端数)超过阈值 → 断开该文件夹，
# 节省内存（其他对端已覆盖内容，断开不影响可用性）。断开后文件夹保留、可手动重连。
DISCONNECT_SEEDED_ON = os.environ.get("RSL_DISCONNECT_SEEDED_ON", "0") == "1"
DISCONNECT_SEEDED_MIN = int(os.environ.get("RSL_DISCONNECT_SEEDED_MIN", "10"))

# 断开文件夹定期重连：每 interval 重连 1 个到期文件夹（节奏钟 + 退避 + 闸门）
RECONNECT_ON = os.environ.get("RSL_RECONNECT_ON", "0") == "1"
RECONNECT_INTERVAL = int(os.environ.get("RSL_RECONNECT_INTERVAL", "600"))
RECONNECT_MAX_BACKOFF = int(os.environ.get("RSL_RECONNECT_MAX_BACKOFF", "86400"))
RECONNECT_MEM_GATE_PCT = int(os.environ.get("RSL_RECONNECT_MEM_GATE_PCT", "75"))
RECONNECT_IO_GATE_PCT = int(os.environ.get("RSL_RECONNECT_IO_GATE_PCT", "90"))
RECONNECT_IO_AVG_SAMPLES = int(os.environ.get("RSL_RECONNECT_IO_AVG_SAMPLES", "10"))  # ~30s/采样，10≈5分钟均值
STORAGE_PATH = os.environ.get("RSL_STORAGE_PATH", "/var/lib/resilio-sync/")
CONN_FILE = os.environ.get("RSL_CONN", "/var/lib/resilio-sync-guard/conn.json")

# 调度器配置与内存快照（backup_prefs 每 30s 填充，调度器每 interval 消费）
SCHED_CFG = sync_sched.parse_sched_config()
LAST_SNAPSHOT = []

# 守护状态（写 status.json 供 guard_webapi 读取；只含计数与时间戳，脱敏）
STATUS_FILE = os.environ.get("RSL_STATUS", "/var/lib/resilio-sync-guard/status.json")
_status = {
    "ts": 0,
    "nrestarts": 0,
    "last_backup": 0,
    "fail_streak": 0,
    "low_thread_streak": 0,
    "last_restart": 0,
    "last_restart_reason": "",
    "sched_mode": SCHED_CFG["mode"],
    "safe_mode": False,
}

_safe_mode = False            # 安全模式激活中
_safe_entered = 0.0           # 进入安全模式的时间戳
_oom_events = []              # 滑窗内 OOM 时间戳
_oom_recorded_nr = -1         # 已记录过的 NRestarts，避免同一次 OOM 重复计数


def write_status():
    _status["ts"] = time.time()
    _status["nrestarts"] = get_nrestarts()
    _status["sched_mode"] = SCHED_CFG["mode"]
    _status["safe_mode"] = _safe_mode
    _status["io_pct"] = round(_io_pct, 1)
    _status["io_ready"] = _io_ready
    _status["io_gate_pct"] = RECONNECT_IO_GATE_PCT
    _status["reconnect"] = _reconnect_summary()
    try:
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_status, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


class TlsAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def get_nrestarts():
    r = subprocess.run(["systemctl", "show", SERVICE, "-p", "NRestarts", "--value"],
                       capture_output=True, text=True)
    return int(r.stdout.strip() or 0)


def restart_service():
    """主动重启 rslsync 服务（假死健康检查恢复用）。"""
    try:
        subprocess.run(["systemctl", "restart", SERVICE],
                       capture_output=True, timeout=60)
    except Exception:
        pass


def make_client():
    base = WEBUI_URL if WEBUI_URL.endswith("/gui/") else WEBUI_URL.rstrip("/") + "/gui/"
    session = requests.Session()
    session.mount("https://", TlsAdapter())
    session.mount("http://", TlsAdapter())
    session.verify = False
    auth = HTTPBasicAuth(WEBUI_USER, WEBUI_PASS)
    r = session.get(base + "token.html", auth=auth,
                    params={"t": int(time.time() * 1000)}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError("login failed http=%d" % r.status_code)
    m = re.search(r"id='token'[^>]*>([^<]+)</div>", r.text)
    if not m:
        raise RuntimeError("token not found in token.html")
    return session, auth, base, m.group(1)


def api_get(session, auth, base, token, action, params=None):
    q = {"token": token, "action": action, "t": int(time.time() * 1000)}
    if params:
        q.update(params)
    r = session.get(base, auth=auth, params=q, timeout=10)
    r.raise_for_status()
    return r.json()


def _probe_healthy(session, auth, base):
    """假死探测：登录 token.html + 实际调一次轻量 action，均成功才视为健康。

    仅探登录会漏掉『登录正常、action 层假死』的假死（进程活着但 action 卡死），
    本次 8/18 故障即属此类。用 getsessionstats（返回小、guard 本身也调用）做 action 探针，
    超时设 8s，失败即判异常。复用主 session，不额外建连接。
    """
    try:
        r = session.get(base + "token.html", auth=auth,
                        params={"t": int(time.time() * 1000)}, timeout=8)
        if r.status_code != 200:
            return False
        tok = re.search(r"id='token'[^>]*>([^<]+)</div>", r.text)
        if not tok:
            return False
        session.get(base, auth=auth,
                    params={"token": tok.group(1), "action": "getsessionstats",
                            "t": int(time.time() * 1000)}, timeout=8).raise_for_status()
        return True
    except Exception:
        return False


def record_stats(stats):
    """追加会话统计采样到外部文件，弥补 OOM 后 transferred 归零无法追溯。"""
    try:
        os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
        with open(STATS_FILE, "a") as f:
            f.write("%d down=%d up=%d\n"
                    % (int(time.time()), stats.get("down", 0), stats.get("up", 0)))
    except OSError:
        pass


def backup_prefs(session, auth, base, token):
    global LAST_SNAPSHOT
    data = api_get(session, auth, base, token, "getsyncfolders", {"discovery": "1"})
    folders = data.get("folders", [])
    LAST_SNAPSHOT = sync_sched.folders_to_metrics(folders)
    for f in folders:
        # 断开文件夹(synclevel=0)无 id 字段、仅有 folderid；id or folderid 保持一致
        fid = f.get("id") or f.get("folderid")
        if not fid:
            continue
        m = next((x for x in LAST_SNAPSHOT if x.get("id") == fid), None)
        if m:
            # path/secret 供断开(记 path)与重连(addsyncfolder)使用；secret 断开后仍在
            m["path"] = f.get("path")
            m["secret"] = f.get("secret")
    out = {}
    n_paused = 0
    n_missing = 0
    for f in folders:
        if "paused" not in f:
            n_missing += 1
        fid = f.get("id")
        if fid:
            paused = bool(f.get("paused"))
            if paused:
                n_paused += 1
            out[fid] = {"paused": paused}
    # 尽力而为：全局限速设置 + 会话统计（失败不中断备份）
    settings = {}
    stats = {}
    try:
        s = api_get(session, auth, base, token, "settings").get("value", {})
        for k in ("dlrate", "ulrate"):
            if k in s:
                settings[k] = s[k]
    except Exception:
        pass
    try:
        st = api_get(session, auth, base, token, "getsessionstats").get("value", {})
        tr = st.get("transferred", {}) or {}
        stats = {"down": tr.get("down", 0), "up": tr.get("up", 0)}
    except Exception:
        pass
    os.makedirs(os.path.dirname(BACKUP_FILE), exist_ok=True)
    tmp = BACKUP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"ts": time.time(), "folders": out,
                   "settings": settings, "stats": stats}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, BACKUP_FILE)
    _status["last_backup"] = time.time()
    if stats:
        record_stats(stats)
    log("备份 %d 文件夹 (paused=%d, 缺失=%d) settings=%d项 stats=%s nrestarts=%d"
        % (len(out), n_paused, n_missing, len(settings),
           "有" if stats else "无", get_nrestarts()))
    return out


def load_backup():
    try:
        with open(BACKUP_FILE) as f:
            return json.load(f).get("folders", {})
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------
# 断开文件夹定期重连（G8）
# ------------------------------------------------------------------

def load_conn():
    """读 conn.json（记录断开文件夹的 path 与退避）。含敏感 path，600 权限、不进日志。"""
    try:
        with open(CONN_FILE) as f:
            d = json.load(f)
            if not isinstance(d, dict):
                raise ValueError
            d.setdefault("last_attempt", 0.0)
            d.setdefault("folders", {})
            return d
    except (OSError, ValueError):
        return {"last_attempt": 0.0, "folders": {}}


def save_conn(conn):
    try:
        os.makedirs(os.path.dirname(CONN_FILE), exist_ok=True)
        tmp = CONN_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(conn, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONN_FILE)
    except OSError:
        pass


def _reconnect_summary():
    """定期重连进展摘要（脱敏：只含计数与时间戳，不含文件夹 id/path）。

    供 status.json / 面板「定期重连」卡：conn.json 条目总数、已重连/仍断开数、
    下次可重连时间（最久待重连的 next_connect）、最近一次重连时间、IO 闸门现状、重连参数。
    """
    conn = load_conn()
    folders = conn.get("folders") or {}
    by_id = {m.get("id"): m for m in LAST_SNAPSHOT}
    total = len(folders)
    connected = 0
    disconnected = 0
    reconnected = 0            # 累计重连成功过的条目数
    reconnect_ok_total = 0     # 累计重连成功总次数
    next_connect = None
    last_reconnect_ok = None
    for fid, e in folders.items():
        m = by_id.get(fid)
        if m is not None and m.get("synclevel", 0) != 0:
            connected += 1
        else:
            disconnected += 1
        ok = int(e.get("reconnect_ok_count", 0) or 0)
        if ok > 0:
            reconnected += 1
            reconnect_ok_total += ok
        lc = e.get("last_reconnect_ok")
        if lc:
            last_reconnect_ok = max(last_reconnect_ok or 0, lc)
        nc = e.get("next_connect")
        if nc and (next_connect is None or nc < next_connect):
            next_connect = nc
    return {
        "on": bool(RECONNECT_ON),
        "interval": RECONNECT_INTERVAL,
        "max_backoff": RECONNECT_MAX_BACKOFF,
        "mem_gate_pct": RECONNECT_MEM_GATE_PCT,
        "io_gate_pct": RECONNECT_IO_GATE_PCT,
        "total": total,
        "connected": connected,
        "disconnected": disconnected,
        "reconnected": reconnected,
        "reconnect_ok_total": reconnect_ok_total,
        "last_reconnect_ok": last_reconnect_ok or 0,
        "next_connect": next_connect or 0,
        "last_attempt": conn.get("last_attempt") or 0,
        "io_pct": round(_io_pct, 1),
        "io_ready": _io_ready,
    }


def _rslsync_pid():
    """真实服务进程 pid：优先 systemd MainPID（resilio-sync.service）；
    否则取 pgrep 结果中 VmRSS 最大者（排除 /tmp 测试残留/僵尸等假进程）。"""
    try:
        out = subprocess.run(["systemctl", "show", "-p", "MainPID", "resilio-sync"],
                             capture_output=True, text=True, timeout=5).stdout
        pid = out.strip().split("=")[-1]
        if pid.isdigit() and int(pid) > 1:
            return pid
    except (OSError, subprocess.SubprocessError):
        pass
    best, best_rss = None, -1
    try:
        for pid in subprocess.run(["pgrep", "-x", "rslsync"],
                                  capture_output=True, text=True).stdout.split():
            try:
                with open("/proc/%s/status" % pid) as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            rss = int(line.split()[1])
                            if rss > best_rss:
                                best_rss, best = rss, pid
                            break
            except OSError:
                pass
    except subprocess.SubprocessError:
        pass
    return best


def _mem_gate_blocked():
    """内存闸门：rslsync rss/上限 占比 >= RECONNECT_MEM_GATE_PCT 时阻止重连。读取失败放行。"""
    try:
        pid = _rslsync_pid()
        if not pid:
            return False
        rss = None
        with open("/proc/%s/status" % pid) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                    break
        if rss is None:
            return False
        cg_max = None
        with open("/proc/%s/cgroup" % pid) as f:
            for line in f:
                if line.startswith("0::"):
                    base = "/sys/fs/cgroup" + line.split(":", 2)[-1].strip().rstrip("/")
                    try:
                        with open(base + "/memory.max") as f2:
                            val = f2.read().strip()
                        if val and val != "max":
                            cg_max = int(val)
                    except (OSError, ValueError):
                        pass
                    break
        if cg_max is None or cg_max <= 0:
            return False
        return (rss * 100.0 / cg_max) >= RECONNECT_MEM_GATE_PCT
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


_disk_name = None
_io_prev = None
_io_pct = 0.0
_io_ready = False  # 是否已有一次有效差分采样（guard 刚启动时无差分，避免误判 0% 绕过闸门）
_io_samples = []   # 最近 RECONNECT_IO_AVG_SAMPLES 个采样（滚动平均，防偶然低占用触发重连）


def _find_disk():
    """定位同步存储盘（df 找 STORAGE_PATH 挂载设备 → 整盘名），结果缓存。失败返回 None。"""
    global _disk_name
    if _disk_name:
        return _disk_name
    try:
        r = subprocess.run(["df", "-P", STORAGE_PATH],
                           capture_output=True, text=True, timeout=10)
        dev = r.stdout.strip().split("\n")[-1].split()[0]
        base = os.path.basename(dev)
        # 整盘名：去分区后缀（vda1→vda, nvme0n1p1→nvme0n1）
        name = None
        m = re.match(r"^(.*)p\d+$", base)
        if m:
            name = m.group(1)
        else:
            m = re.match(r"^(.*?)(\d+)$", base)
            if m:
                name = m.group(1)
        if name:
            _disk_name = name
        return name
    except Exception:
        return None


def _read_io_ticks(disk):
    """读 /proc/diskstats 中 disk 的 io_ticks（字段13，毫秒）。失败返回 None。"""
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                p = line.split()
                if len(p) > 13 and p[2] == disk:
                    return int(p[12])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _sample_io():
    """采样磁盘 IO 忙度（iostat %util 同义：io_ticks 差分/间隔）。

    _io_pct 为最近 N 个采样的滚动均值：瞬时低谷不会放行重连，盘忙则均值持续高位拦截。
    """
    global _io_prev, _io_pct, _io_ready, _io_samples
    disk = _find_disk()
    ticks = _read_io_ticks(disk) if disk else None
    now = time.time()
    prev = _io_prev
    _io_prev = (now, ticks) if ticks is not None else None
    if ticks is None or not prev or now <= prev[0] or prev[1] is None:
        _io_ready = False
    else:
        delta_ms = (now - prev[0]) * 1000.0
        sample = min(max(0.0, (ticks - prev[1]) * 100.0 / delta_ms), 100.0) if delta_ms > 0 else 0.0
        _io_samples.append(sample)
        if len(_io_samples) > RECONNECT_IO_AVG_SAMPLES:
            del _io_samples[:-RECONNECT_IO_AVG_SAMPLES]
        _io_ready = True
    _io_pct = sum(_io_samples) / len(_io_samples) if _io_samples else 0.0
    return _io_pct


def _reconnect_folders(session, auth, base, token):
    """断开文件夹定期重连：每 interval 至多重连 1 个（节奏钟 + 退避 + 内存闸门）。

    候选：conn.json 记录过、当前已断开(synclevel==0)、且 now>=next_connect（退避）。
    选最久没重连的，用记录的 path + 当前 secret 走 addsyncfolder（目录非空 105/106 时 force 重试）。
    安全模式冻结；重连成功才写历史动作行（定期重连: fold=），失败/拦截不带动作行标记。
    """
    if not RECONNECT_ON or _safe_mode:
        return 0
    conn = load_conn()
    now = time.time()
    if now - conn.get("last_attempt", 0) < RECONNECT_INTERVAL:
        return 0
    by_id = {m.get("id"): m for m in LAST_SNAPSHOT}
    cands = []
    for fid, e in (conn.get("folders") or {}).items():
        m = by_id.get(fid)
        if not m or m.get("synclevel", 1) != 0:
            continue
        if now < e.get("next_connect", 0):
            continue
        cands.append((e.get("last_connected", 0.0), fid, e))
    if not cands:
        return 0
    # 闸门拦截也推进节奏钟：拦截后下一次尝试等一个完整 interval，避免每 30s 重试刷屏
    conn["last_attempt"] = now
    if _mem_gate_blocked():
        log("定期重连跳过: 内存闸门超限(>=%d%%)" % RECONNECT_MEM_GATE_PCT)
        save_conn(conn)
        return 0
    if not _io_ready:
        # guard 刚启动尚无 IO 差分采样：不重连（避免误判 0% 在盘忙时绕过闸门），等下一采样
        return 0
    if _io_pct >= RECONNECT_IO_GATE_PCT:
        log("定期重连跳过: 磁盘IO忙度超限(>=%d%% 当前%d%%)"
            % (RECONNECT_IO_GATE_PCT, _io_pct))
        save_conn(conn)
        return 0
    cands.sort()
    _, fid, entry = cands[0]
    try:
        path = entry.get("path")
        secret = by_id.get(fid, {}).get("secret")
        if not path or not secret:
            log("定期重连跳过 fold=%s 缺少path/secret" % fid[:8])
            save_conn(conn)
            return 0
        params = {"path": path, "secret": secret}
        data = api_get(session, auth, base, token, "addsyncfolder", params)
        err = (data.get("value") or {}).get("error")
        # 106 与 105 都是「目录非空」需 force 确认（生产实测 105 的 message
        # 为「目标文件夹不是空的。仍然添加？」）；只认 106 会漏掉 105 导致
        # 该文件夹永远重连失败并堵死候选队列（8/19 生产事故根因）。
        if err in (105, 106):
            params["force"] = "true"
            data = api_get(session, auth, base, token, "addsyncfolder", params)
            err = (data.get("value") or {}).get("error")
        if err not in (None, 0, 200):
            log("定期重连失败 fold=%s error=%s" % (fid[:8], err))
            save_conn(conn)
            return 0
        entry["last_connected"] = now
        entry["reconnect_ok_count"] = int(entry.get("reconnect_ok_count", 0)) + 1
        entry["last_reconnect_ok"] = now
        save_conn(conn)
        log("定期重连: fold=%s 已重连" % fid[:8])
        return 1
    except Exception:
        save_conn(conn)
        log("定期重连失败 fold=%s %s" % (fid[:8], "API异常"))
        return 0


def _record_oom():
    """记录一次 OOM 重启，返回滑窗内次数。"""
    now = time.time()
    _oom_events.append(now)
    cutoff = now - SAFE_OOM_WINDOW
    _oom_events[:] = [t for t in _oom_events if t >= cutoff]
    return len(_oom_events)


def _force_all_paused_backup(ids):
    """把备份（跨 OOM 持久化的 paused 状态）直接改写为全部暂停，保留 settings/stats。"""
    old = {}
    try:
        with open(BACKUP_FILE) as f:
            old = json.load(f)
    except (OSError, ValueError):
        pass
    payload = {"ts": time.time(),
               "folders": {fid: {"paused": True} for fid in ids},
               "settings": old.get("settings", {}),
               "stats": old.get("stats", {})}
    os.makedirs(os.path.dirname(BACKUP_FILE), exist_ok=True)
    tmp = BACKUP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, BACKUP_FILE)


def _set_paused_via_api(session, auth, base, token, fid, want):
    """设置单个文件夹 paused（folderpref 读 + setfolderpref 全量回写）。返回是否实际变更。"""
    pref = api_get(session, auth, base, token, "folderpref", {"id": fid}).get("value", {})
    if bool(pref.get("paused")) == bool(want):
        return False
    pref["paused"] = bool(want)
    params = {"id": fid}
    for k, v in pref.items():
        params[k] = str(v).lower() if isinstance(v, bool) else str(v)
    api_get(session, auth, base, token, "setfolderpref", params)
    return True


def _enter_safe_mode(session, auth, base, token, count):
    """OOM 风暴进入安全模式：直接改写备份为全部暂停（跨 OOM 持久的关键），
    API 不可用时用上次备份的 id 列表兜底。实际暂停由随后的 restore_prefs 强制执行。"""
    global _safe_mode, _safe_entered
    _safe_mode = True
    _safe_entered = time.time()
    ids = None
    if session is not None:
        try:
            data = api_get(session, auth, base, token, "getsyncfolders", {"discovery": "1"})
            ids = [f.get("id") for f in data.get("folders", []) if f.get("id")]
        except Exception:
            ids = None
    if not ids:
        ids = list(load_backup().keys())
    log("进入安全模式: %ds 内 OOM %d 次(阈值 %d)，暂停全部 %d 个文件夹"
        % (SAFE_OOM_WINDOW, count, SAFE_OOM_THRESHOLD, len(ids)))
    if ids:
        _force_all_paused_backup(ids)


def _disconnect_redundant_seeders(session, auth, base, token):
    """断开做种人数超过阈值的文件夹（节省内存；保留文件、可手动重连）。

    用 LAST_SNAPSHOT（backup_prefs 填充）判定，脱敏日志只含 id 前缀与计数。
    断开前把 path 记入 conn.json（Resilio 断开后清空 path），并记录退避（连续断开次数翻倍）。
    断开后文件夹仍在 getsyncfolders 里(synclevel=0)，面板显示"断开连接"可重连。
    """
    if not DISCONNECT_SEEDED_ON:
        return 0
    conn = load_conn()
    now = time.time()
    conn_folders = conn.setdefault("folders", {})
    n = 0
    for m in LAST_SNAPSHOT:
        if m.get("synclevel", 1) == 0:
            continue  # 已断开
        seeded = m.get("seeded", 0)
        if seeded > DISCONNECT_SEEDED_MIN:
            fid = m.get("id")
            if not fid:
                continue
            prev = conn_folders.get(fid, {})
            count = int(prev.get("disconnect_count", 0)) + 1
            gap = RECONNECT_INTERVAL * (2 ** (count - 1))
            if gap > RECONNECT_MAX_BACKOFF:
                gap = RECONNECT_MAX_BACKOFF
            conn_folders[fid] = {
                "path": m.get("path") or prev.get("path"),
                "last_connected": now,
                "disconnect_count": count,
                "next_connect": now + gap,
                # 重连成功历史跨断开保留（供面板「定期重连」卡展示累计进展）
                "reconnect_ok_count": int(prev.get("reconnect_ok_count", 0)),
                "last_reconnect_ok": prev.get("last_reconnect_ok"),
            }
            try:
                api_get(session, auth, base, token, "removefolder", {
                    "folderid": fid, "deletedirectory": "false",
                    "fromalldevices": "false"})
                n += 1
                log("冗余做种断开: fold=%s seeded=%d>%d" % (fid[:8], seeded, DISCONNECT_SEEDED_MIN))
            except Exception:
                pass
    if n:
        save_conn(conn)
        log("冗余做种断开: 本轮共 %d 个" % n)
    return n


def restore_prefs(session, auth, base, token):
    backup = load_backup()
    if not backup and not _safe_mode:
        log("无备份可恢复，跳过")
        return
    # 1) folder paused 恢复（安全模式时强制全部暂停，覆盖备份）
    data = api_get(session, auth, base, token, "getsyncfolders", {"discovery": "1"})
    cur = {f.get("id"): bool(f.get("paused")) for f in data.get("folders", [])}
    if _safe_mode:
        targets = [fid for fid in cur if fid]
    else:
        targets = [fid for fid in backup.get("folders", {}) if fid in cur]
    n = 0
    for fid in targets:
        want = True if _safe_mode else bool((backup.get("folders", {}).get(fid) or {}).get("paused"))
        if cur.get(fid) != want:
            if _set_paused_via_api(session, auth, base, token, fid, want):
                n += 1
    if n:
        log(("安全模式强制" if _safe_mode else "OOM 后") + "恢复 %d 个文件夹的 paused 状态" % n)
    # 2) 全局限速设置恢复（尽力而为）
    settings = backup.get("settings")
    if settings:
        try:
            params = {k: str(v) for k, v in settings.items()}
            api_get(session, auth, base, token, "setsettings", params)
            log("OOM 后恢复全局设置 %d 项" % len(settings))
        except Exception:
            pass
    backup_prefs(session, auth, base, token)


def main():
    # main() 的退出分支对安全模式状态做赋值，须声明为全局，否则读取会 UnboundLocalError
    global _safe_mode, _oom_recorded_nr
    if not WEBUI_PASS:
        log("缺少 RSL_PASS 环境变量，退出")
        sys.exit(1)
    log("guard 启动 poll=%ds 备份间隔=%ds" % (POLL_SEC, BACKUP_INTERVAL))
    try:
        session, auth, base, token = make_client()
        last_nrestarts = get_nrestarts()
        last_backup = time.time()
        backup_prefs(session, auth, base, token)
        log("首次备份完成")
    except Exception as e:
        log("初始化失败 %s: %s" % (type(e).__name__, e))
        sys.exit(1)

    fail_streak = 0        # 连续登录失败计数（假死检测）
    low_thread_streak = 0  # 连续线程数过低计数
    sched_state = sync_sched.load_sched_state(SCHED_CFG)
    last_sched = time.time()
    sched_cycle = 0
    if SCHED_CFG["mode"] != "off" and LAST_SNAPSHOT:
        sched_state = sync_sched.run_scheduler_eval(
            session, auth, base, token, SCHED_CFG, LAST_SNAPSHOT,
            sched_state, recovery=True, cycle=sched_cycle)
        sched_cycle += 1
        last_sched = time.time()
        log("调度器首轮恢复期评估完成")

    while True:
        try:
            # --- 健康检查：rslsync 假死检测 ---
            # 假死特征：进程存活但 WebUI 无响应 / 线程数异常少（正常 15+，僵死时掉到 ~2）
            try:
                pid = subprocess.run(["pgrep", "-x", SERVICE.replace("resilio-sync", "rslsync")],
                                     capture_output=True, text=True).stdout.strip().split("\n")[0]
            except Exception:
                pid = ""
            threads = 0
            if pid:
                try:
                    with open("/proc/%s/status" % pid) as f:
                        for line in f:
                            if line.startswith("Threads:"):
                                threads = int(line.split()[1])
                                break
                except OSError:
                    pass

            alive = bool(pid)
            web_ok = False
            try:
                # 假死探测：登录 + 实际 action（两者都覆盖，见 _probe_healthy）
                web_ok = _probe_healthy(session, auth, base)
            except Exception:
                web_ok = False

            if alive and not web_ok:
                fail_streak += 1
                if fail_streak >= HEALTH_FAIL_LIMIT:
                    log("检测到假死: WebUI 连续 %d 次无响应，主动重启" % fail_streak)
                    restart_service()
                    _status["last_restart"] = time.time()
                    _status["last_restart_reason"] = "fakedeath_webui"
                    fail_streak = 0
                    low_thread_streak = 0
                    time.sleep(5)
                    session, auth, base, token = make_client()
                    restore_prefs(session, auth, base, token)
                    last_nrestarts = get_nrestarts()
                    if SCHED_CFG["mode"] != "off" and LAST_SNAPSHOT:
                        sched_state = sync_sched.run_scheduler_eval(
                            session, auth, base, token, SCHED_CFG,
                            LAST_SNAPSHOT, sched_state,
                            recovery=True, cycle=sched_cycle)
                        sched_cycle += 1
                        last_sched = time.time()
            else:
                fail_streak = 0

            if alive and threads > 0 and threads < HEALTH_THREAD_MIN:
                low_thread_streak += 1
                if low_thread_streak >= HEALTH_THREAD_STREAK:
                    log("检测到假死: 线程数=%d 低于阈值 %d 连续 %d 次，主动重启"
                        % (threads, HEALTH_THREAD_MIN, low_thread_streak))
                    restart_service()
                    _status["last_restart"] = time.time()
                    _status["last_restart_reason"] = "fakedeath_threads"
                    low_thread_streak = 0
                    fail_streak = 0
                    time.sleep(5)
                    session, auth, base, token = make_client()
                    restore_prefs(session, auth, base, token)
                    last_nrestarts = get_nrestarts()
                    if SCHED_CFG["mode"] != "off" and LAST_SNAPSHOT:
                        sched_state = sync_sched.run_scheduler_eval(
                            session, auth, base, token, SCHED_CFG,
                            LAST_SNAPSHOT, sched_state,
                            recovery=True, cycle=sched_cycle)
                        sched_cycle += 1
                        last_sched = time.time()
            else:
                low_thread_streak = 0

            nr = get_nrestarts()
            if nr > last_nrestarts:
                log("检测到 OOM 自动重启: NRestarts %d->%d，执行恢复" % (last_nrestarts, nr))
                _status["last_restart"] = time.time()
                _status["last_restart_reason"] = "oom"
                oom_count = 0
                if nr != _oom_recorded_nr:
                    oom_count = _record_oom()
                    _oom_recorded_nr = nr
                if not _safe_mode and oom_count >= SAFE_OOM_THRESHOLD:
                    _enter_safe_mode(None, None, None, None, oom_count)
                session, auth, base, token = make_client()
                restore_prefs(session, auth, base, token)
                last_nrestarts = nr
                if SCHED_CFG["mode"] != "off" and LAST_SNAPSHOT:
                    sched_state = sync_sched.run_scheduler_eval(
                        session, auth, base, token, SCHED_CFG,
                        LAST_SNAPSHOT, sched_state,
                        recovery=True, cycle=sched_cycle)
                    sched_cycle += 1
                    last_sched = time.time()
            elif _safe_mode and (time.time() - _safe_entered >= SAFE_EXIT_QUIET
                                 and (not _oom_events
                                      or time.time() - _oom_events[-1] >= SAFE_EXIT_QUIET)):
                log("安全模式解除: 已稳定 %d 秒无 OOM，恢复常规调度" % SAFE_EXIT_QUIET)
                _safe_mode = False
                _oom_events[:] = []
                _oom_recorded_nr = -1
            now = time.time()
            if now - last_backup >= BACKUP_INTERVAL:
                backup_prefs(session, auth, base, token)
                _disconnect_redundant_seeders(session, auth, base, token)
                _sample_io()  # 刷新磁盘 IO 忙度（重连闸门用，最近一个备份周期差分）
                _reconnect_folders(session, auth, base, token)
                last_backup = now
            if (SCHED_CFG["mode"] != "off"
                    and now - last_sched >= SCHED_CFG["interval"]
                    and LAST_SNAPSHOT):
                sched_state = sync_sched.run_scheduler_eval(
                    session, auth, base, token, SCHED_CFG, LAST_SNAPSHOT,
                    sched_state, recovery=False, cycle=sched_cycle)
                sched_cycle += 1
                last_sched = now
            _status["fail_streak"] = fail_streak
            _status["low_thread_streak"] = low_thread_streak
            write_status()
            time.sleep(POLL_SEC)
        except Exception as e:
            log("异常 %s: %s，重连后继续" % (type(e).__name__, e))
            try:
                session, auth, base, token = make_client()
            except Exception:
                pass
            time.sleep(5)


if __name__ == "__main__":
    main()
