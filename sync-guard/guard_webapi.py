#!/usr/bin/env python3
"""resilio-sync-guard 的 HTTP 管理接口。

供本地面板（或人工）查看守护状态、读写调度器配置。默认监听 :8890，Bearer token 认证。
- GET  /api/status   读 guard-status.json + prefs.json 时间戳 + 进程内存 → 守护状态（脱敏，只含计数/时间戳/内存）
- GET  /api/sched    读 guard env 文件的 RSL_SCHED_* → 当前调度配置
- POST /api/sched    白名单校验后重写 env → systemctl restart resilio-sync-guard

环境变量：
  RSL_API_PORT   监听端口（默认 8890）
  RSL_API_TOKEN  Bearer 认证令牌（必填）
  RSL_GUARD_ENV   guard 的 env 文件路径（默认 /etc/resilio-sync-guard.env）
  RSL_STATUS      guard-status.json 路径（默认 /var/lib/resilio-sync-guard/status.json）
  RSL_PREFS       prefs.json 路径（默认 /var/lib/resilio-sync-guard/prefs.json）
  RSL_SERVICE     重启的服务名（默认 resilio-sync-guard）
"""
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("RSL_API_PORT", "8890"))
TOKEN = os.environ.get("RSL_API_TOKEN", "")
GUARD_ENV = os.environ.get("RSL_GUARD_ENV", "/etc/resilio-sync-guard.env")
STATUS_FILE = os.environ.get("RSL_STATUS", "/var/lib/resilio-sync-guard/status.json")
PREFS_FILE = os.environ.get("RSL_PREFS", "/var/lib/resilio-sync-guard/prefs.json")
LOG_FILE = os.environ.get("RSL_LOG", "/var/log/resilio-sync-guard.log")
HISTORY_LIMIT = int(os.environ.get("RSL_HISTORY_LIMIT", "50"))
SERVICE = os.environ.get("RSL_SERVICE", "resilio-sync-guard")

# 可写调度参数白名单：面板键(小写) -> env 键
SCHED_KEYS = {
    "mode": "RSL_SCHED_MODE",
    "max_running": "RSL_SCHED_MAX_RUNNING",
    "seed_limit": "RSL_SCHED_SEED_LIMIT",
    "run_min_stay": "RSL_SCHED_RUN_MIN_STAY",
    "preheat_sec": "RSL_SCHED_PREHEAT_SEC",
    "w_need": "RSL_SCHED_W_NEED",
    "w_scar": "RSL_SCHED_W_SCAR",
    "w_speed": "RSL_SCHED_W_SPEED",
    "w_download": "RSL_SCHED_W_DOWNLOAD",
    "w_time": "RSL_SCHED_W_TIME",
    "w_wait": "RSL_SCHED_W_WAIT",
    "safe_oom_window": "RSL_SAFE_OOM_WINDOW",
    "safe_oom_threshold": "RSL_SAFE_OOM_THRESHOLD",
    "safe_exit_quiet": "RSL_SAFE_EXIT_QUIET",
    "disconnect_seeded_on": "RSL_DISCONNECT_SEEDED_ON",
    "disconnect_seeded_min": "RSL_DISCONNECT_SEEDED_MIN",
    "reconnect_on": "RSL_RECONNECT_ON",
    "reconnect_interval": "RSL_RECONNECT_INTERVAL",
    "reconnect_max_backoff": "RSL_RECONNECT_MAX_BACKOFF",
    "reconnect_mem_gate_pct": "RSL_RECONNECT_MEM_GATE_PCT",
    "reconnect_io_gate_pct": "RSL_RECONNECT_IO_GATE_PCT",
}
MODE_VALUES = {"off", "dry", "on"}
FLAG_VALUES = {"0", "1", 0, 1}
# 整型参数最小值（防误配：阈值=1 会在单次 OOM 就暂停全部）
SCHED_MIN = {"safe_oom_threshold": 2, "safe_oom_window": 60, "safe_exit_quiet": 60,
             "disconnect_seeded_min": 1,
             "reconnect_interval": 60, "reconnect_max_backoff": 60,
             "reconnect_mem_gate_pct": 1, "reconnect_io_gate_pct": 1}
# 百分比上限（防 100% 把重连彻底卡死）
SCHED_MAX = {"reconnect_mem_gate_pct": 99, "reconnect_io_gate_pct": 99}
# 参数默认值（env 未覆盖时 guard 实际用的值）——必须与 sync_guard.py / sync_sched.py 的 env 默认值一致
SCHED_DEFAULTS = {
    "mode": "off",
    "max_running": "5", "seed_limit": "5", "run_min_stay": "600", "preheat_sec": "60",
    "w_need": "10", "w_scar": "4", "w_speed": "1", "w_download": "1000",
    "w_time": "20", "w_wait": "10",
    "safe_oom_window": "600", "safe_oom_threshold": "4", "safe_exit_quiet": "1800",
    "disconnect_seeded_on": "0", "disconnect_seeded_min": "10",
    "reconnect_on": "0", "reconnect_interval": "600", "reconnect_max_backoff": "86400",
    "reconnect_mem_gate_pct": "75", "reconnect_io_gate_pct": "90",
}


def read_env():
    out = {}
    try:
        with open(GUARD_ENV) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def write_env(env):
    os.makedirs(os.path.dirname(GUARD_ENV), exist_ok=True)
    tmp = GUARD_ENV + ".tmp"
    with open(tmp, "w") as f:
        for k, v in env.items():
            f.write("%s=%s\n" % (k, v))
    os.chmod(tmp, 0o600)
    os.replace(tmp, GUARD_ENV)


def read_status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def read_prefs_ts():
    try:
        with open(PREFS_FILE) as f:
            return json.load(f).get("ts", 0)
    except (OSError, ValueError):
        return 0


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


def read_mem():
    """rslsync 进程内存（脱敏：不含任何文件夹标识）。返回字节或缺失字段 None。
    rss/vmswap 来自 /proc/<pid>/status；cg_current/cg_max 来自 cgroup v2（memory.current/max）；
    mem_total/mem_avail 来自 /proc/meminfo。
    """
    mem = {}
    try:
        pid = _rslsync_pid()
        if pid:
            mem["pid"] = pid
            with open("/proc/%s/status" % pid) as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        mem["rss"] = int(line.split()[1]) * 1024
                    elif line.startswith("VmSwap:"):
                        mem["vmswap"] = int(line.split()[1]) * 1024
            with open("/proc/%s/cgroup" % pid) as f:
                for line in f:
                    if line.startswith("0::"):
                        cg = line.split(":", 2)[-1].strip()
                        base = "/sys/fs/cgroup" + cg.rstrip("/")
                        for key, fn in (("cg_current", "memory.current"),
                                        ("cg_max", "memory.max")):
                            try:
                                with open(base + "/" + fn) as f2:
                                    val = f2.read().strip()
                                if val and val != "max":
                                    mem[key] = int(val)
                            except (OSError, ValueError):
                                pass
                        break
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem["mem_total"] = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    mem["mem_avail"] = int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return mem


def _is_action_line(line):
    """动作行（计入历史配额）：调度器暂停/恢复、逐文件夹断开、重连成功。其余（周期汇总）不计。"""
    return ("动作=" in line
            or "冗余做种断开: fold=" in line
            or "定期重连: fold=" in line)


def read_history(limit):
    """读取 guard 日志中调度历史相关行，返回最后 limit 条**动作行**。

    采集：调度器（调度[）、逐文件夹断开（冗余做种断开: fold=）、重连成功（定期重连: fold=）；
    批量汇总（本轮共 N 个）与失败/拦截日志不含动作行标记，不采集。
    周期汇总行不占配额，但保留在其所处区间内随动作行一并返回。
    日志已脱敏（只含 id 前缀）。
    """
    lines = []
    try:
        with open(LOG_FILE, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if ("调度[" in line
                        or "冗余做种断开: fold=" in line
                        or "定期重连: fold=" in line):
                    lines.append(line.rstrip("\n"))
    except OSError:
        pass
    out = []
    for line in reversed(lines):
        out.append(line)
        if _is_action_line(line):
            limit -= 1
            if limit <= 0:
                break
    out.reverse()
    return out


def load_json(body):
    try:
        return json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        return None


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        return bool(TOKEN) and self.headers.get("Authorization", "") == "Bearer " + TOKEN

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"ok": False, "error": "未授权"})
        if self.path == "/api/status":
            st = read_status()
            st["prefs_ts"] = read_prefs_ts()
            st["mem"] = read_mem()
            return self._send(200, {"ok": True, **st})
        if self.path.startswith("/api/history"):
            limit = HISTORY_LIMIT
            if "limit=" in self.path:
                try:
                    limit = int(self.path.split("limit=")[-1])
                except ValueError:
                    limit = HISTORY_LIMIT
            return self._send(200, {"ok": True, "history": read_history(limit)})
        if self.path == "/api/sched":
            env = read_env()
            return self._send(200, {"ok": True,
                                    "sched": {k: env.get(v) or SCHED_DEFAULTS.get(k)
                                              for k, v in SCHED_KEYS.items()}})
        return self._send(404, {"ok": False, "error": "未知路径"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"ok": False, "error": "未授权"})
        if self.path != "/api/sched":
            return self._send(404, {"ok": False, "error": "未知路径"})
        body = load_json(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)))
        if body is None:
            return self._send(400, {"ok": False, "error": "JSON 无效"})
        patch = {}
        for k, val in body.items():
            if k not in SCHED_KEYS:
                return self._send(400, {"ok": False, "error": "非法参数: %s" % k})
            if k == "mode":
                if val not in MODE_VALUES:
                    return self._send(400, {"ok": False, "error": "mode 需为 off/dry/on"})
                patch[k] = val
            else:
                if k in ("disconnect_seeded_on", "reconnect_on") and val not in FLAG_VALUES:
                    return self._send(400, {"ok": False, "error": "参数 %s 需为 0/1" % k})
                try:
                    iv = int(val)
                except (TypeError, ValueError):
                    return self._send(400, {"ok": False, "error": "参数 %s 需为整数" % k})
                lo = SCHED_MIN.get(k)
                if lo is not None and iv < lo:
                    return self._send(400, {"ok": False, "error": "参数 %s 需 >= %d" % (k, lo)})
                hi = SCHED_MAX.get(k)
                if hi is not None and iv > hi:
                    return self._send(400, {"ok": False, "error": "参数 %s 需 <= %d" % (k, hi)})
                patch[k] = str(iv)
        if not patch:
            return self._send(400, {"ok": False, "error": "无有效参数"})
        env = read_env()
        for k, val in patch.items():
            env[SCHED_KEYS[k]] = val
        write_env(env)
        try:
            subprocess.run(["systemctl", "restart", SERVICE], capture_output=True)
        except Exception:
            pass  # 无 systemctl 环境（如本地 mock）不阻塞
        return self._send(200, {"ok": True, "applied": patch})

    def log_message(self, *args):
        pass


def main():
    if not TOKEN:
        print("缺少 RSL_API_TOKEN 环境变量，退出", flush=True)
        return
    print("guard API 启动: :%d" % PORT, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
