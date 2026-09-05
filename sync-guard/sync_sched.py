#!/usr/bin/env python3
"""智能分批同步调度器（被 sync_guard.py import 调用）。

在资源有限（2G 内存 + 1 核）的 seedbox 上，对全部同步文件夹做
"稀缺性优先"分批调度：只运行"最需要帮助"的文件夹（有人等数据 +
别人没做种 + 有下载需求），其余自动暂停，把带宽/CPU 集中给最稀缺资源。

三态模式（RSL_SCHED_MODE）：
  off  默认，完全禁用（零行为变更）
  dry  只记录每轮的判定结果，不实际改 paused 状态
  on   真实执行暂停/恢复

隐私：本模块不记录也不输出任何文件夹 name/path/secret、对端
name/id/userid。日志只含 folder id 前缀与纯计数。备份/状态文件 600 权限。
"""
import json
import os
import subprocess
import time

LOG_FILE = os.environ.get("RSL_LOG", "/var/log/resilio-sync-guard.log")


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def parse_sched_config():
    """读取并校验全部 RSL_SCHED_* 环境变量。非法值一律按 off 处理。"""
    mode = os.environ.get("RSL_SCHED_MODE", "off").strip().lower()
    if mode not in ("off", "dry", "on"):
        mode = "off"

    def _int(name, default):
        try:
            return int(os.environ.get(name, str(default)))
        except (TypeError, ValueError):
            return default

    return {
        "mode": mode,
        "interval": _int("RSL_SCHED_INTERVAL", 60),
        "max_running": _int("RSL_SCHED_MAX_RUNNING", 5),
        "seed_limit": _int("RSL_SCHED_SEED_LIMIT", 5),
        "pause_grace": _int("RSL_SCHED_PAUSE_GRACE", 3),
        "resume_grace": _int("RSL_SCHED_RESUME_GRACE", 2),
        "run_min_stay": _int("RSL_SCHED_RUN_MIN_STAY", 600),
        "max_changes": _int("RSL_SCHED_MAX_CHANGES", 10),
        "max_resume": _int("RSL_SCHED_MAX_RESUME", 2),
        "w_need": _int("RSL_SCHED_W_NEED", 10),
        "w_scar": _int("RSL_SCHED_W_SCAR", 4),
        "w_speed": _int("RSL_SCHED_W_SPEED", 1),
        "w_download": _int("RSL_SCHED_W_DOWNLOAD", 1000),
        "max_seed_time": _int("RSL_SCHED_MAX_SEED_TIME", 1800),
        "w_time": _int("RSL_SCHED_W_TIME", 20),
        "max_wait_time": _int("RSL_SCHED_MAX_WAIT_TIME", 3600),
        "w_wait": _int("RSL_SCHED_W_WAIT", 10),
        "speed_norm": _int("RSL_SCHED_SPEED_NORM", 524288),
        "state_file": os.environ.get("RSL_SCHED_STATE",
                                     "/var/lib/resilio-sync-guard/sched-state.json"),
        "preheat_sec": _int("RSL_SCHED_PREHEAT_SEC", 60),
        "log_fullid": os.environ.get("RSL_SCHED_LOG_FULLID", "0").strip() == "1",
        "always_run": [x for x in
                       os.environ.get("RSL_SCHED_ALWAYS_RUN", "").split(",") if x],
    }


# ------------------------------------------------------------------
# 纯函数（可本地单测，不含 IO）
# ------------------------------------------------------------------

def folders_to_metrics(folders):
    """纯函数：从 getsyncfolders 原始列表聚合指标，立即丢弃 peer 明细。

    返回 [{id, paused, up_speed, onlinepeerscount, need, seeded, dneed, synclevel}]
    need  = 在线且 updiff>0 的对端数（有人在等我们上传）
    dneed = 在线且 downdiff>0 的对端数（我们要下载）
    seeded= 在线且 updiff==0 且 downdiff==0 的对端数（完全同步）
    """
    out = []
    for f in folders or []:
        # 断开文件夹(synclevel=0)无 id 字段、仅有 folderid；id or folderid 保证断开前后一致
        fid = f.get("id") or f.get("folderid")
        if not fid:
            continue
        need = dneed = seeded = 0
        for p in (f.get("peers") or []):
            if not p.get("isonline"):
                continue
            up = p.get("updiff") or 0
            dn = p.get("downdiff") or 0
            if up > 0:
                need += 1
            elif up == 0 and dn == 0:
                seeded += 1
            if dn > 0:
                dneed += 1
        out.append({
            "id": fid,
            "paused": bool(f.get("paused")),
            "up_speed": int(f.get("up_speed") or 0),
            "onlinepeerscount": int(f.get("onlinepeerscount") or 0),
            "need": need,
            "seeded": seeded,
            "dneed": dneed,
            "synclevel": int(f.get("synclevel") or 0),
        })
    return out


def grade(m, seed_limit):
    """分级：D 下载需求 / C 上传需求 / B 做种充足 / A 无需求。"""
    if m.get("dneed", 0) > 0:
        return "D"
    if m.get("need", 0) == 0:
        return "A"
    if m.get("seeded", 0) > seed_limit:
        return "B"
    return "C"


def score(m, cfg, elapsed_run, elapsed_wait):
    """打分（仅 D/C 级有效）：
    base + 下载加成 - 做种时长惩罚 + 等待时长加成
    """
    g = grade(m, cfg["seed_limit"])
    if g in ("A", "B"):
        return 0.0
    scar = max(0.0, min(1.0, (cfg["seed_limit"] + 1 - m.get("seeded", 0))
                        / (cfg["seed_limit"] + 1)))
    speed = max(0.0, min(1.0, m.get("up_speed", 0) / max(1, cfg["speed_norm"])))
    base = (cfg["w_need"] * m.get("need", 0)
            + cfg["w_scar"] * scar
            + cfg["w_speed"] * speed)
    bonus = cfg["w_download"] if m.get("dneed", 0) > 0 else 0.0
    tpen = cfg["w_time"] * min(elapsed_run / max(1, cfg["max_seed_time"]), 1.0)
    wbon = cfg["w_wait"] * min(elapsed_wait / max(1, cfg["max_wait_time"]), 1.0)
    return base + bonus - tpen + wbon


def decide(cfg, snapshot, prev_state, recovery=False):
    """核心决策（纯函数）。返回 (actions, new_state)。

    actions = [(fid, "pause"|"resume"), ...] 已按先暂停后恢复、套预算
    """
    now = time.time()
    # 已断开文件夹（synclevel==0）不参与调度决策（面板侧同规则），仅保留其状态
    active = [m for m in snapshot if m.get("synclevel", 1) != 0]

    # 1) 目标运行集：D 级（含 always_run）按分优先占满 max_running，C 级填剩余。
    #    D 级也受名额约束（download_bonus 保证优先，但超额的 D 级不无限豁免，
    #    运行集总数不超过 max_running，防止下载需求过多时运行数膨胀）。
    def _g(m):
        return grade(m, cfg["seed_limit"])

    def _sc(m):
        st = prev_state.get(m["id"], {})
        er = 0 if m["paused"] else (now - (st.get("last_resumed") or now))
        ew = (now - (st.get("last_paused") or now)) if m["paused"] else 0
        return score(m, cfg, er, ew)

    desired = set()
    d_items = [m for m in active if _g(m) == "D" or m["id"] in cfg["always_run"]]
    d_items.sort(key=lambda m: -_sc(m))
    for m in d_items[:cfg["max_running"]]:
        desired.add(m["id"])
    remaining = max(0, cfg["max_running"] - len(desired))
    if remaining > 0:
        c_items = [m for m in active if _g(m) == "C"]
        c_items.sort(key=lambda m: -_sc(m))
        for m in c_items[:remaining]:
            desired.add(m["id"])

    # 2) 防抖：产出暂停/恢复意图
    new_state = {}
    intents = {"pause": [], "resume": []}
    for m in active:
        fid = m["id"]
        g = grade(m, cfg["seed_limit"])
        st = dict(prev_state.get(fid, {}))
        ps = st.get("ps", 0)
        rs = st.get("rs", 0)
        lr = st.get("last_resumed")
        protected = fid in cfg["always_run"]

        if fid in desired:
            if m["paused"]:
                rs += 1
                ps = 0
                if rs >= cfg["resume_grace"]:
                    intents["resume"].append(fid)
                    rs = 0
            else:
                rs = 0
                ps = 0
        else:
            if m["paused"]:
                rs = 0
                ps = 0
            elif protected:
                # D 级/始终运行：豁免，不暂停（下载进行中不停）
                rs = 0
                ps = 0
            else:
                ps += 1
                rs = 0
                grace = 0 if recovery else cfg["pause_grace"]
                stay_ok = (recovery or lr is None
                           or (now - lr) >= cfg["run_min_stay"])
                if ps >= grace and stay_ok:
                    intents["pause"].append(fid)
                    ps = 0
        st["ps"] = ps
        st["rs"] = rs
        new_state[fid] = st

    # 3) 预算：先暂停（释放）后恢复（拉负载），恢复另有上限
    pauses = intents["pause"][:cfg["max_changes"]]
    budget_left = max(0, cfg["max_changes"] - len(pauses))
    resumes = intents["resume"][:min(cfg["max_resume"], budget_left)]
    # 4) 运行总数硬约束：暂停腾位后，恢复不得超过剩余名额，
    #    防止"每周期恢复 N 个 + 被保护的不让位"导致运行数膨胀超 max_running
    cur_running = sum(1 for m in active if not m["paused"])
    avail = cfg["max_running"] - (cur_running - len(pauses))
    if avail < 0:
        avail = 0
    resumes = resumes[:avail]
    actions = [("pause", f) for f in pauses] + [("resume", f) for f in resumes]

    # 5) 仅对实际执行的动作更新 last_* 时间戳
    for act, fid in actions:
        st = new_state.get(fid, {})
        if act == "pause":
            st["last_paused"] = now
            st["last_resumed"] = None
        else:
            st["last_resumed"] = now
            st["last_paused"] = None
    # 6) 断开文件夹保留原状态（重连后从头累计，避免陈旧的 grace 计数）
    for m in snapshot:
        if m.get("synclevel", 1) == 0:
            new_state.setdefault(m["id"], dict(prev_state.get(m["id"], {})))

    return actions, new_state


# ------------------------------------------------------------------
# 状态持久化
# ------------------------------------------------------------------

def load_sched_state(cfg):
    try:
        with open(cfg["state_file"]) as f:
            d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("folders"), dict):
                return d["folders"]
    except (OSError, ValueError):
        pass
    return {}


def save_sched_state(cfg, folders_state):
    try:
        os.makedirs(os.path.dirname(cfg["state_file"]), exist_ok=True)
        tmp = cfg["state_file"] + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": time.time(), "folders": folders_state}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, cfg["state_file"])
    except OSError:
        pass


# ------------------------------------------------------------------
# 执行与编排
# ------------------------------------------------------------------

def api_get(session, auth, base, token, action, params=None):
    q = {"token": token, "action": action, "t": int(time.time() * 1000)}
    if params:
        q.update(params)
    r = session.get(base, auth=auth, params=q, timeout=10)
    r.raise_for_status()
    return r.json()


def set_folder_paused(session, auth, base, token, fid, paused):
    """folderpref 读 + setfolderpref 全量回写 paused。返回是否成功。"""
    try:
        d = api_get(session, auth, base, token, "folderpref", {"id": fid}).get("value", {})
        d["paused"] = bool(paused)
        params = {"id": fid}
        for k, v in d.items():
            params[k] = str(v).lower() if isinstance(v, bool) else str(v)
        api_get(session, auth, base, token, "setfolderpref", params)
        return True
    except Exception:
        return False


def _fid_label(cfg, fid):
    return fid if cfg["log_fullid"] else fid[:8]


def rslsync_start_epoch():
    """返回 rslsync 进程启动的 epoch 秒；获取失败返回 0。"""
    try:
        pid = subprocess.run(["pgrep", "-x", "rslsync"],
                             capture_output=True, text=True).stdout.strip().split("\n")[0]
        if not pid:
            return 0
        r = subprocess.run(["ps", "-o", "lstart=", "-p", pid],
                           capture_output=True, text=True).stdout.strip()
        return time.mktime(time.strptime(r, "%a %b %d %H:%M:%S %Y"))
    except Exception:
        return 0


def run_scheduler_eval(session, auth, base, token, cfg, snapshot,
                       prev_state, recovery=False, cycle=0):
    """编排器：decide → apply（dry 只记日志 / on 真实执行）→ save → 脱敏日志。"""
    if cfg["mode"] == "off":
        return prev_state
    # 启动预热期：rslsync 刚重启时对端连接未建立，peer 状态不可靠，
    # 会误判为"无需求"而暂停运行中的文件夹。预热期跳过评估，等连接稳定。
    start = rslsync_start_epoch()
    if start and (time.time() - start) < cfg["preheat_sec"]:
        log("调度[%s] 周期#%d 跳过：rslsync 启动预热中(%ds<%ds)"
            % (cfg["mode"], cycle, int(time.time() - start), cfg["preheat_sec"]))
        return prev_state
    t0 = time.time()
    actions, new_state = decide(cfg, snapshot, prev_state, recovery)

    ok_pause = ok_resume = 0
    for act, fid in actions:
        m = next((x for x in snapshot if x["id"] == fid), {})
        if act == "pause":
            if cfg["mode"] == "on":
                if set_folder_paused(session, auth, base, token, fid, True):
                    ok_pause += 1
            log("调度[%s] 周期#%d fold=%s C%s need=%d dneed=%d seed=%d "
                "up=%dKB 动作=暂停" % (cfg["mode"], cycle, _fid_label(cfg, fid),
                                      grade(m, cfg["seed_limit"]), m.get("need", 0),
                                      m.get("dneed", 0), m.get("seeded", 0),
                                      m.get("up_speed", 0) // 1024))
        else:
            if cfg["mode"] == "on":
                if set_folder_paused(session, auth, base, token, fid, False):
                    ok_resume += 1
            log("调度[%s] 周期#%d fold=%s C%s need=%d dneed=%d seed=%d "
                "up=%dKB 动作=恢复" % (cfg["mode"], cycle, _fid_label(cfg, fid),
                                      grade(m, cfg["seed_limit"]), m.get("need", 0),
                                      m.get("dneed", 0), m.get("seeded", 0),
                                      m.get("up_speed", 0) // 1024))

    save_sched_state(cfg, new_state)
    c_run = sum(1 for m in snapshot if not m["paused"])
    c_c = sum(1 for m in snapshot if grade(m, cfg["seed_limit"]) == "C")
    c_d = sum(1 for m in snapshot if grade(m, cfg["seed_limit"]) == "D")
    n_pause = sum(1 for a in actions if a[0] == "pause")
    n_resume = sum(1 for a in actions if a[0] == "resume")
    log("调度[%s] 周期#%d 文件夹=%d 运行=%d C=%d D=%d 变更=%d(暂停%d 恢复%d) "
        "耗时%dms 模式=%s" % (cfg["mode"], cycle, len(snapshot), c_run, c_c, c_d,
                             len(actions), n_pause, n_resume,
                             int((time.time() - t0) * 1000),
                             "dry观察" if cfg["mode"] == "dry" else "执行"))
    return new_state
