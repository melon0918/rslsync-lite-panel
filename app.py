"""Resilio Sync 轻量本地管理面板 — Flask 入口。

只在本地渲染 UI，对远端仅发最少最轻的 JSON API 调用。
安全：服务器地址/密码只存于 Flask session 客户端签名 cookie（base64 明文可读、防篡改），
不写盘、不进日志；登录会话永久化（默认 30 天），关浏览器后无需重填凭据。
"""
import csv
import io
import os
import time
import threading
import functools
from datetime import timedelta
import requests

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, session, url_for, abort)

from resilio_api import ResilioSyncClient, ResilioApiError, ResilioAuthError

# 复用 sync-guard 调度器的分级逻辑（纯函数），面板侧计算"调度判定"
import sys as _sys
_guard_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sync-guard')
if _guard_dir not in _sys.path:
    _sys.path.insert(0, _guard_dir)
import sync_sched

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
# 登录会话永久化：session cookie 带 Max-Age（默认 30 天），浏览器重启后仍保持登录；
# 守护接口地址/令牌（guard_url/guard_token）随同一 session 一并保留，无需重填。
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)


def _secret_key():
    """会话签名密钥：优先环境变量，否则在 instance/ 下持久化一份（跨重启有效）。"""
    env = os.environ.get('RSYNC_SECRET_KEY')
    if env:
        return env.encode()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', '.secret_key')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, 'wb') as f:
            f.write(os.urandom(32))
    with open(path, 'rb') as f:
        return f.read()


app.secret_key = _secret_key()

# ----------------------------------------------------------------------
# 后台抓取缓存：无论浏览器是否在前台，后台线程定时从远端拉取数据，
# 浏览器轮询 /api/status 时直接读本地缓存（零远端调用）。
# 凭据在登录时暂存于进程内存（不写盘），供后台线程使用。
# ----------------------------------------------------------------------

FETCH_INTERVAL = 3          # 秒
_fetch_lock = threading.Lock()
_active_conn = None         # {base_url, username, password, token}
# 持久共享客户端：复用同一 requests.Session/token。Resilio 的 token 绑定在获取它的
# HTTP 会话（cookie）上，每轮新建客户端=新会话=旧 token 必失效 → 后台每轮都重登
# 打印"登录成功"。持久复用后仅在 token 真正失效（OOM 重启）时重登。
_shared_client = None
_client_lock = threading.Lock()
_status_cache = None        # /api/status 的载荷
_cache_ts = 0

# guard 内存采样：后台抓取线程节流拉取（guard status.json 每 30s 更新，无需更频繁）
# _guard_cfg 是 guard_url/token 的进程内存镜像，供无请求上下文的后台线程读取
GUARD_MEM_INTERVAL = 30     # 秒
_guard_cfg = {}             # {'url':..., 'token':...}
_guard_cache = None         # {'mem': {...}, 'io': {...}} 或 None
_guard_ts = 0.0

# 调度评分用配置：面板侧用默认值；若配置了 guard，则用生产调度参数（seed_limit/权重等）覆盖，
# 使「调度判定」页分级与实际调度一致（否则用户改生产 seed_limit 后显示仍按默认分级）
SCHED_CFG_PANEL = sync_sched.parse_sched_config()
_guard_sched_cache = None
_guard_sched_ts = 0.0
GUARD_SCHED_INTERVAL = 60     # 秒，生产调度参数缓存
# 参与评分/分级的整型参数键（guard_webapi SCHED_KEYS 子集）
_GRADING_INT_KEYS = ('max_running', 'seed_limit', 'run_min_stay', 'preheat_sec',
                     'w_need', 'w_scar', 'w_speed', 'w_download', 'w_time', 'w_wait')


def _guard_sched_cfg():
    """生产调度参数（guard 接口）与面板默认合并；未配置/失败用默认。带缓存。"""
    global _guard_sched_cache, _guard_sched_ts
    now = time.time()
    if _guard_sched_cache is not None and now - _guard_sched_ts < GUARD_SCHED_INTERVAL:
        return _guard_sched_cache
    cfg = dict(SCHED_CFG_PANEL)
    url = _guard_cfg.get('url')
    token = _guard_cfg.get('token')
    if url and token:
        try:
            sched = GuardApi(url, token).get_sched().get('sched') or {}
            for k in _GRADING_INT_KEYS:
                v = sched.get(k)
                if v not in (None, ''):
                    try:
                        cfg[k] = int(v)
                    except (TypeError, ValueError):
                        pass
        except GuardApiError:
            pass
    _guard_sched_cache = cfg
    _guard_sched_ts = now
    return cfg

# 观察 paused 状态变化，跟踪每文件夹 last_paused / last_resumed（供评分的时间项）
_pause_times = {}   # folder_id -> {'last_paused': ts, 'last_resumed': ts}
_prev_paused = {}   # folder_id -> bool


def _track_pause_state(folders):
    """根据观察到的 paused 状态，维护每文件夹暂停/恢复时间戳。"""
    now = time.time()
    seen = set()
    for f in folders:
        fid = f['id']
        paused = f['paused']
        seen.add(fid)
        st = _pause_times.setdefault(fid, {'last_paused': None, 'last_resumed': None})
        if fid not in _prev_paused:
            if paused:
                st['last_paused'] = now
            else:
                st['last_resumed'] = now
        elif _prev_paused[fid] != paused:
            if paused:
                st['last_paused'] = now
            else:
                st['last_resumed'] = now
        _prev_paused[fid] = paused
    for fid in list(_prev_paused):
        if fid not in seen:
            _prev_paused.pop(fid, None)
            _pause_times.pop(fid, None)


def _elapsed(fid, paused):
    """返回 (elapsed_run, elapsed_wait)：与 sync_sched.decide 的时间语义一致。"""
    st = _pause_times.get(fid) or {}
    now = time.time()
    if paused:
        last = st.get('last_paused')
        return 0, (now - last) if last else 0
    last = st.get('last_resumed')
    return (now - last) if last else 0, 0


def _sched_metrics(folder):
    """从 enriched folder 的 peers 计算调度指标（与 sync_sched.folders_to_metrics 同语义）。
    need=待上传对端数 seeded=完全同步对端数 dneed=待下载对端数
    """
    need = seeded = dneed = 0
    for p in folder.get('peers', []):
        if not p.get('online'):
            continue
        up = p.get('updiff') or 0
        dn = p.get('downdiff') or 0
        if up > 0:
            need += 1
        elif up == 0 and dn == 0:
            seeded += 1
        if dn > 0:
            dneed += 1
    return {'need': need, 'seeded': seeded, 'dneed': dneed}


def _build_status(client):
    """按 /api/status 的返回结构，从远端拉取并组装载荷。"""
    folders = [enrich_folder(f) for f in client.get_folder_list()]
    _track_pause_state(folders)
    cfg = _guard_sched_cfg()
    sched = []
    for f in folders:
        if f['display'] == 'disconnected':
            continue  # 已断开(synclevel=0)的文件夹不在调度判定范围，不显示"运行"
        m = _sched_metrics(f)
        g = sync_sched.grade(m, cfg['seed_limit'])
        er, ew = _elapsed(f['id'], f['paused'])
        sc = sync_sched.score(m, cfg, er, ew)
        f['grade'] = g
        sched.append({
            'id': f['id'], 'name': f['name'],
            'grade': g, 'score': round(sc, 1),
            'need': m['need'], 'seeded': m['seeded'],
            'dneed': m['dneed'], 'running': not f['paused'],
        })
    # 判定页按分数从高到低排列（与"谁最优先跑"一致）
    sched.sort(key=lambda s: s['score'], reverse=True)
    total = {
        'count': len(folders),
        'syncing': sum(1 for x in folders
                       if x['display'] in ('syncing', 'downloading', 'uploading', 'incomplete')),
        'paused': sum(1 for x in folders if x['display'] == 'paused'),
        'disconnected': sum(1 for x in folders if x['display'] == 'disconnected'),
        'download_speed': sum(x['download_speed'] for x in folders),
        'upload_speed': sum(x['upload_speed'] for x in folders),
        'total_size': sum(x['size'] for x in folders),
    }
    statuses = client.get_statuses()
    session = client.get_session_stats()
    return {
        'ok': True,
        'folders': folders,
        'sched': sched,
        'total': total,
        'statuses': {'cpu': statuses.get('cpu', 0),
                     'disk': statuses.get('disk', 0)},
        'session': session,
    }


def _ensure_shared_client():
    """返回共享客户端（复用同一 session/token），必要时用登录凭据创建并补 token。"""
    global _shared_client
    conn = _active_conn
    if not conn:
        raise AuthError()
    with _client_lock:
        if _shared_client is None:
            _shared_client = ResilioSyncClient(conn['base_url'],
                                               conn['username'], conn['password'])
        c = _shared_client
        if not c.token:
            c.token = conn.get('token')
        if not c.token:
            if not c.login():
                raise AuthError()
            conn['token'] = c.token
        return c


def _fetch_once():
    global _status_cache, _cache_ts
    conn = _active_conn
    if not conn:
        return
    try:
        c = _ensure_shared_client()
    except AuthError:
        return
    try:
        payload = _build_status(c)
    except ResilioAuthError:
        # token 失效/过期（如 OOM 重启）→ 同一 session 重登一次（打印登录成功属正常）
        with _client_lock:
            if c.login():
                conn['token'] = c.token
                try:
                    payload = _build_status(c)
                except Exception:
                    return
            else:
                return
    except ResilioApiError:
        # 连接失败 / 服务器错误 → 重登无意义，保留旧缓存
        return
    except Exception:
        return
    with _fetch_lock:
        g = _sample_guard()
        payload['memory'] = (g or {}).get('mem')
        payload['guard_reconnect'] = (g or {}).get('reconnect')
        _status_cache = payload
        _cache_ts = time.time()


def _fetch_loop():
    while True:
        _fetch_once()
        time.sleep(FETCH_INTERVAL)


# ----------------------------------------------------------------------
# 登录与客户端
# ----------------------------------------------------------------------

class AuthError(Exception):
    pass


def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('base_url') or not session.get('username'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def _sync_active_conn():
    """若后台连接未建立但 session 有凭据，则从 session 恢复 _active_conn。

    面板重启后浏览器 session 仍有效（页面可开），但后台抓取线程的
    _active_conn 是进程内存全局、重启即丢 → 缓存不更新 → /api/status 503。
    任何已登录请求进来时调用，即可恢复后台连接。
    """
    global _active_conn
    _sync_guard_cfg()
    if _active_conn is None:
        base = session.get('base_url')
        user = session.get('username')
        pwd = session.get('password')
        if base and user and pwd:
            _active_conn = {'base_url': base, 'username': user,
                            'password': pwd, 'token': session.get('token')}


def _sync_guard_cfg():
    """从当前请求会话镜像 guard 配置到进程全局（供无请求上下文的后台线程采样用）。"""
    global _guard_cfg
    try:
        url = session.get('guard_url')
        token = session.get('guard_token')
    except RuntimeError:
        return  # 后台线程无请求上下文，保持旧镜像
    if url and token:
        _guard_cfg = {'url': url, 'token': token}
    elif _guard_cfg:
        _guard_cfg = {}


def _sample_guard():
    """节流（30s）采样 guard 状态（内存 + 定期重连进展摘要）；未配置/失败返回 None。"""
    global _guard_cache, _guard_ts
    url = _guard_cfg.get('url')
    token = _guard_cfg.get('token')
    if not url or not token:
        # 未配置 guard：清缓存且不节流（配置一出现立即采样，无需等满 30s）
        _guard_cache = None
        _guard_ts = 0.0
        return None
    now = time.time()
    if now - _guard_ts < GUARD_MEM_INTERVAL:
        return _guard_cache
    _guard_ts = now
    try:
        st = GuardApi(url, token).get_status()
        _guard_cache = {'mem': st.get('mem') or {},
                        'reconnect': st.get('reconnect') or {}}
    except GuardApiError:
        _guard_cache = None
    return _guard_cache


def _client():
    """返回共享客户端（复用同一 session/token）。未登录抛 AuthError。"""
    _sync_active_conn()
    if not session.get('base_url') or not session.get('username'):
        raise AuthError()
    return _ensure_shared_client()


def _call(c, fn):
    """执行远端调用；仅认证失败时重登一次再试（连接/服务器错误重登无意义）。"""
    try:
        return fn()
    except ResilioAuthError:
        with _client_lock:
            if c.login():
                session['token'] = c.token
                if _active_conn:
                    _active_conn['token'] = c.token
                return fn()
        raise


class GuardApiError(Exception):
    pass


class GuardApi:
    """服务器端 guard HTTP 接口（sync-guard/guard_webapi.py）客户端。"""

    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip('/')
        self.token = token

    def _request(self, method, path, data=None):
        headers = {'Authorization': 'Bearer ' + self.token}
        url = self.base_url + path
        try:
            if method == 'POST':
                r = requests.post(url, headers=headers, json=data, timeout=8)
            else:
                r = requests.get(url, headers=headers, timeout=8)
        except requests.RequestException as e:
            raise GuardApiError(f"连接守护接口失败: {e}")
        try:
            body = r.json()
        except ValueError:
            raise GuardApiError(f"守护接口响应异常 (HTTP {r.status_code})")
        if not body.get('ok'):
            raise GuardApiError(body.get('error') or f"HTTP {r.status_code}")
        return body

    def get_status(self):
        return self._request('GET', '/api/status')

    def get_sched(self):
        return self._request('GET', '/api/sched')

    def set_sched(self, patch):
        return self._request('POST', '/api/sched', data=patch)

    def get_history(self, limit=50):
        return self._request('GET', f'/api/history?limit={limit}')


def _guard_client():
    url = session.get('guard_url')
    token = session.get('guard_token')
    if not url or not token:
        raise GuardApiError('未配置守护接口（请到设置页填写）')
    return GuardApi(url, token)


def api(f):
    """API 路由统一异常处理 → JSON。"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except AuthError:
            return jsonify(ok=False, error='未登录'), 401
        except GuardApiError as e:
            return jsonify(ok=False, error=str(e)), 502
        except ResilioApiError as e:
            return jsonify(ok=False, error=str(e)), 502
        except Exception as e:
            app.logger.exception('API 内部错误')
            return jsonify(ok=False, error='内部错误'), 500
    return wrapper


def same_origin():
    """写操作同源校验：浏览器 POST 会带 Origin，只放行同 host（允许非浏览器无 Origin）。"""
    origin = request.headers.get('Origin')
    if origin:
        from urllib.parse import urlparse
        return urlparse(origin).netloc == request.host
    return True


# ----------------------------------------------------------------------
# 页面路由
# ----------------------------------------------------------------------

@app.route('/')
def index():
    if session.get('base_url'):
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        base = request.form.get('base_url', '').strip()
        user = request.form.get('username', '').strip()
        pwd = request.form.get('password', '')
        if not base or not user or not pwd:
            return render_template('login.html', error='服务器地址、用户名、密码均不能为空',
                                   base_url=base, username=user)
        c = ResilioSyncClient(base, user, pwd)
        if c.login():
            session.permanent = True  # 永久 cookie（30 天）：关浏览器后免重填/免重登
            session['base_url'] = base
            session['username'] = user
            session['password'] = pwd
            session['token'] = c.token
            global _active_conn, _shared_client
            _active_conn = {'base_url': base, 'username': user,
                            'password': pwd, 'token': c.token}
            _shared_client = c
            _fetch_once()
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='登录失败，请检查服务器地址与凭据',
                               base_url=base, username=user)
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    global _active_conn, _status_cache, _cache_ts, _shared_client
    _active_conn = None
    _shared_client = None
    _status_cache = None
    _cache_ts = 0
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')


@app.route('/peers')
@login_required
def peers():
    return render_template('peers.html')


@app.route('/sched')
@login_required
def sched():
    return render_template('sched.html')


@app.route('/sched/history')
@login_required
def sched_history():
    return render_template('sched_history.html')


@app.route('/guard')
@login_required
def guard():
    return render_template('guard.html')


@app.route('/settings')
@login_required
def settings_page():
    return render_template('settings.html')


# ----------------------------------------------------------------------
# API 路由
# ----------------------------------------------------------------------

def enrich_folder(f):
    """按 Web UI 判定逻辑将文件夹字段映射为显示状态。

    优先级：error → 索引中(loading/indexing/rescanning) → 已暂停 → 断开连接(synclevel=0)
    → 按 status 数值；SYNCED+remoteindexing+在线节点 视为索引中（官方 meta-loading）。
    status 数值见 docs/api-reference.md。
    """
    error = f.get('error', 0) or 0
    paused = bool(f.get('paused')) or f.get('status') == 1
    disconnected = f.get('synclevel') == 0
    down = f.get('down_speed', 0) or 0
    up = f.get('up_speed', 0) or 0
    down_status = f.get('down_status') or 0
    up_status = f.get('up_status') or 0
    status = f.get('status')
    onlinepeers = f.get('onlinepeerscount', 0)

    # 文件级/瞬时错误（如 "Can't download file"）不阻断同步：若文件夹仍在
    # 接收/发送/同步，降级为警告，保留传输状态与进度，错误单独标示。
    active = (down > 0 or up > 0) or status in (3, 4, 5)
    warning = bool(error) and active

    if error and not warning:
        display = 'error'
    elif f.get('loading') or f.get('indexing') or f.get('rescanning'):
        display = 'indexing'
    elif paused:
        display = 'paused'
    elif disconnected:
        display = 'disconnected'
    else:
        display = {
            2: 'stopped',          # STOPPED
            3: 'downloading',      # RECEIVING
            4: 'uploading',        # SENDING
            5: 'syncing',          # SYNCING（双向）
            6: 'incomplete',       # INCOMPLETED
            7: 'uptodate',         # SYNCED
            8: 'nopeers',          # NOPEERS（等待节点）
            9: 'pending',          # PENDING
            10: 'invalid',         # INVALID
            11: 'neverconnected',  # NEVER_CONNECTED
        }.get(status)
        if display == 'uptodate' and f.get('remoteindexing') and onlinepeers > 0:
            display = 'indexing'
        if display is None:
            if down > 0 or up > 0 or down_status < 100 or up_status < 100:
                display = 'syncing'
            else:
                # status 为 0(NONE) 或缺失：未连接/无状态，绝不当已同步
                display = 'disconnected'

    if display == 'downloading':
        progress = down_status
    elif display == 'uploading':
        progress = up_status
    elif display in ('syncing', 'incomplete'):
        progress = min(down_status, up_status)
    else:
        progress = 0

    return {
        'id': f.get('id') or f.get('folderid'),
        'name': f.get('name'),
        'path': f.get('path'),
        'display': display,
        'paused': paused,
        'error': error,
        'warning': warning,
        'status': status,
        'download_speed': down,
        'upload_speed': up,
        'progress': progress,
        'peers_connected': f.get('onlinepeerscount', 0),
        'peers_total': len(f.get('peers', [])),
        'size': f.get('size', 0),
        'files': f.get('files', 0),
        'errors': f.get('errors', []),
        'peers': [{
            'id': p.get('id'),
            'name': p.get('name'),
            'online': p.get('isonline', False),
            'lastsynctime': p.get('lastsynctime', 0),
            'has_files': p.get('has_files', 0),
            'upfiles': p.get('upfiles', 0),
            'updiff': p.get('updiff', 0),
            'downfiles': p.get('downfiles', 0),
            'downdiff': p.get('downdiff', 0),
        } for p in f.get('peers', [])],
    }


@app.route('/api/status')
@api
def api_status():
    _sync_active_conn()
    if not session.get('base_url'):
        raise AuthError()
    with _fetch_lock:
        if _status_cache is not None:
            data = dict(_status_cache)
            data['_ts'] = int(_cache_ts)
            return jsonify(**data)
    _fetch_once()
    with _fetch_lock:
        if _status_cache is not None:
            data = dict(_status_cache)
            data['_ts'] = int(_cache_ts)
            return jsonify(**data)
    return jsonify(ok=False, error='暂无数据'), 503


@app.route('/api/peers')
@api
def api_peers():
    c = _client()
    folders = _call(c, c.get_folder_list)
    active = _call(c, c.get_peers_stat)
    by_id = {}
    for p in active:
        e = by_id.setdefault(p['id'], {
            'name': p.get('name'), 'id': p['id'], 'online': p.get('online'),
            'down': 0, 'up': 0, 'folders': [], 'active': True})
        e['down'] = (p.get('speed') or {}).get('down', 0)
        e['up'] = (p.get('speed') or {}).get('up', 0)
        e['online'] = e['online'] or p.get('online')
    for f in folders:
        fname = f.get('name')
        for p in f.get('peers', []):
            e = by_id.setdefault(p['id'], {
                'name': p.get('name'), 'id': p['id'], 'online': p.get('isonline'),
                'down': 0, 'up': 0, 'folders': [], 'active': False})
            if fname and fname not in e['folders']:
                e['folders'].append(fname)
    return jsonify(ok=True, peers=list(by_id.values()))


@app.route('/api/settings', methods=['GET', 'POST'])
@api
def api_settings():
    c = _client()
    if request.method == 'POST':
        if not same_origin():
            return jsonify(ok=False, error='来源不合法'), 403
        data = request.get_json(silent=True) or {}
        dl = data.get('download_limit')
        ul = data.get('upload_limit')

        def norm(v):
            return -1 if v in (None, '', 0) else int(v)

        _call(c, lambda: c.set_speed_limits(norm(dl), norm(ul)))
        _fetch_once()
    s = _call(c, c.get_settings)
    return jsonify(ok=True, settings={
        'download_limit': s.get('dlrate'),
        'upload_limit': s.get('ulrate'),
        'devicename': s.get('devicename'),
        'listeningport': s.get('listeningport'),
        'webui_port': s.get('webui_port'),
    })


@app.route('/api/guard/status')
@api
def api_guard_status():
    ga = _guard_client()
    return jsonify(ok=True, status=ga.get_status())


@app.route('/api/guard/sched', methods=['GET', 'POST'])
@api
def api_guard_sched():
    global _guard_sched_cache
    ga = _guard_client()
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        ga.set_sched(data)
        _guard_sched_cache = None  # 配置已变更，下次分级立即取新值
    s = ga.get_sched()
    return jsonify(ok=True, sched=s.get('sched') or {})


@app.route('/api/guard/history')
@api
def api_guard_history():
    ga = _guard_client()
    limit = request.args.get('limit', type=int, default=50)
    if limit > 2000:
        limit = 2000
    h = ga.get_history(limit)
    history = h.get('history') or []
    history.reverse()  # 最新在前
    return jsonify(ok=True, history=history)


@app.route('/api/guard/config', methods=['GET', 'POST'])
@api
def api_guard_config():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        session['guard_url'] = (data.get('url') or '').strip()
        session['guard_token'] = (data.get('token') or '').strip()
        return jsonify(ok=True)
    return jsonify(ok=True, url=session.get('guard_url', ''),
                   token=session.get('guard_token', ''))


@app.route('/api/folder/<folder_id>/pause', methods=['POST'])
@api
def api_pause_folder(folder_id):
    if not same_origin():
        return jsonify(ok=False, error='来源不合法'), 403
    c = _client()
    _call(c, lambda: c.pause_folder(folder_id))
    _fetch_once()
    return jsonify(ok=True)


@app.route('/api/folder/<folder_id>/resume', methods=['POST'])
@api
def api_resume_folder(folder_id):
    if not same_origin():
        return jsonify(ok=False, error='来源不合法'), 403
    c = _client()
    _call(c, lambda: c.resume_folder(folder_id))
    _fetch_once()
    return jsonify(ok=True)


@app.route('/api/folder/<folder_id>/remove', methods=['POST'])
@api
def api_remove_folder(folder_id):
    if not same_origin():
        return jsonify(ok=False, error='来源不合法'), 403
    c = _client()
    _call(c, lambda: c.remove_folder(folder_id))
    _fetch_once()
    return jsonify(ok=True)


@app.route('/api/folder/<folder_id>/disconnect', methods=['POST'])
@api
def api_disconnect_folder(folder_id):
    if not same_origin():
        return jsonify(ok=False, error='来源不合法'), 403
    c = _client()
    _call(c, lambda: c.disconnect_folder(folder_id))
    _fetch_once()
    return jsonify(ok=True)


@app.route('/api/folder/add', methods=['POST'])
@api
def api_add_folder():
    if not same_origin():
        return jsonify(ok=False, error='来源不合法'), 403
    c = _client()
    data = request.get_json(silent=True) or {}
    path = (data.get('path') or '').strip()
    secret = (data.get('secret') or '').strip()
    if not path or not secret:
        return jsonify(ok=False, error='保存路径与密钥均不能为空'), 400
    name = (data.get('name') or '').strip()
    res = _call(c, lambda: c.add_folder(name, path, secret))
    if not res['ok']:
        return jsonify(ok=False, error=f"添加失败: {res.get('message')}"), 502
    _fetch_once()
    return jsonify(ok=True)


@app.route('/api/folder/<folder_id>/connect', methods=['POST'])
@api
def api_connect_folder(folder_id):
    if not same_origin():
        return jsonify(ok=False, error='来源不合法'), 403
    c = _client()
    data = request.get_json(silent=True) or {}
    path = (data.get('path') or '').strip()
    if not path:
        return jsonify(ok=False, error='请填写服务器端的保存路径'), 400
    folders = _call(c, c.get_folder_list)
    folder = next((f for f in folders
                   if (f.get('id') or f.get('folderid')) == folder_id), None)
    if not folder:
        return jsonify(ok=False, error='文件夹不存在或已移除'), 404
    secret = folder.get('secret')
    if not secret:
        return jsonify(ok=False, error='文件夹缺少密钥，无法连接'), 400
    res = _call(c, lambda: c.add_folder(folder.get('name'), path, secret))
    if not res['ok']:
        return jsonify(ok=False, error=f"连接失败: {res.get('message')}"), 502
    _fetch_once()
    return jsonify(ok=True)


@app.route('/api/pause-all', methods=['POST'])
@api
def api_pause_all():
    if not same_origin():
        return jsonify(ok=False, error='来源不合法'), 403
    c = _client()
    for f in _call(c, c.get_folder_list):
        _call(c, lambda: c.set_folder_paused(f['id'], True))
    _fetch_once()
    return jsonify(ok=True)


@app.route('/api/resume-all', methods=['POST'])
@api
def api_resume_all():
    if not same_origin():
        return jsonify(ok=False, error='来源不合法'), 403
    c = _client()
    for f in _call(c, c.get_folder_list):
        _call(c, lambda: c.set_folder_paused(f['id'], False))
    _fetch_once()
    return jsonify(ok=True)


@app.route('/api/export')
@api
def api_export():
    c = _client()
    folders = _call(c, c.get_folder_list)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=['name', 'secret', 'readonlysecret', 'encryptedsecret', 'path'])
    writer.writeheader()
    for f in folders:
        writer.writerow({
            'name': f.get('name', ''),
            'secret': f.get('secret', ''),
            'readonlysecret': f.get('readonlysecret', ''),
            'encryptedsecret': f.get('encryptedsecret', ''),
            'path': f.get('path', ''),
        })
    resp = Response(buf.getvalue(), mimetype='text/csv')
    resp.headers['Content-Disposition'] = 'attachment; filename=sync_folders.csv'
    return resp


@app.route('/api/import', methods=['POST'])
@api
def api_import():
    if not same_origin():
        return jsonify(ok=False, error='来源不合法'), 403
    c = _client()
    file = request.files.get('file')
    key_type = request.form.get('key_type', 'secret')
    if not file:
        return jsonify(ok=False, error='未选择文件'), 400
    raw = file.read()
    text = None
    for enc in ('utf-8-sig', 'gbk', 'utf-8'):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if text is None:
        return jsonify(ok=False, error='无法识别文件编码（支持 UTF-8/GBK）'), 400
    results = []
    for row in csv.DictReader(io.StringIO(text)):
        clean = {str(k).strip(): str(v).strip() for k, v in row.items() if k is not None}
        name = clean.get('name')
        path = clean.get('path')
        secret = clean.get(key_type)
        if not secret or not path:
            results.append({'name': name, 'ok': False, 'message': '密钥或路径为空'})
            continue
        res = _call(c, lambda: c.add_folder(name, path, secret))
        results.append({'name': name, **res})
    _fetch_once()
    return jsonify(ok=True, results=results)


if __name__ == '__main__':
    print("Resilio 管理面板: http://127.0.0.1:5000")
    threading.Thread(target=_fetch_loop, daemon=True).start()
    app.run(host='127.0.0.1', port=5000, debug=False)
