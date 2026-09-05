"""Resilio Sync GUI API 客户端。

既有方法（login / get_sync_folders / add_sync_folders /
pause_all_sync / resume_all_sync）由早期 CLI 版本迁移而来，行为保持一致（含 CLI 输出）；
新增的读取/操作方法为 Web 面板设计，返回纯数据、抛 ResilioApiError，不输出凭据。
"""
import re
import time
import csv
import os
import requests
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class ResilioApiError(Exception):
    """Resilio Sync GUI API 调用失败。"""


class ResilioAuthError(ResilioApiError):
    """认证失败（token 无效/过期），重新登录可恢复。"""


class TlsAdapter(HTTPAdapter):
    """兼容旧设备：降低 TLS 加密等级。"""

    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.set_ciphers('DEFAULT@SECLEVEL=1')
        kwargs['ssl_context'] = context
        return super(TlsAdapter, self).init_poolmanager(*args, **kwargs)


class ResilioSyncClient:
    def __init__(self, base_url, username, password):
        if not base_url.endswith('/gui/'):
            base_url = base_url.rstrip('/') + '/gui/'

        self.base_url = base_url
        self.auth = HTTPBasicAuth(username, password)
        self.token = None
        self.session = requests.Session()

        adapter = TlsAdapter()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.verify = False

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------

    def login(self):
        """获取登录所需的 token（保持原 CLI 输出行为）。"""
        try:
            # 增加 timeout 防止在 SSL 握手卡住时无限等待
            response = self.session.get(
                f"{self.base_url}token.html", auth=self.auth,
                params={'t': int(time.time() * 1000)}, timeout=15)
            if response.status_code == 200:
                match = re.search(r"id='token'[^>]*>([^<]+)</div>", response.text)
                if match:
                    self.token = match.group(1)
                    print("[*] 登录成功！")
                    return True
            print(f"[!] 登录失败，状态码: {response.status_code}")
            return False
        except Exception as e:
            print(f"[!] 连接出错: {e}")
            return False

    def _request(self, action, params=None, timeout=10):
        """统一发起 /gui/ GET 请求，返回解析后的 JSON dict。

        失败抛 ResilioApiError（不包含密码与服务器完整地址）。
        仅认证类失败抛 ResilioAuthError（token 无效/过期），供上层判断是否重登。
        """
        query = {'token': self.token, 'action': action, 't': int(time.time() * 1000)}
        if params:
            query.update(params)
        try:
            response = self.session.get(
                self.base_url, auth=self.auth, params=query, timeout=timeout)
        except requests.RequestException as e:
            raise ResilioApiError(f"连接服务器失败: {e}") from e
        status = response.status_code
        if status in (401, 403):
            raise ResilioAuthError(f"远端返回 HTTP {status}（未授权）")
        if status != 200:
            if status == 400 and 'invalid request' in response.text:
                raise ResilioAuthError('token 无效或已过期')
            raise ResilioApiError(f"远端返回 HTTP {status}")
        try:
            data = response.json()
        except ValueError as e:
            raise ResilioApiError("远端响应不是合法 JSON") from e
        if isinstance(data, dict) and isinstance(data.get('error'), str):
            msg = data['error']
            if any(k in msg.lower() for k in ('auth', 'token', 'login', 'session')):
                raise ResilioAuthError(f"远端返回错误: {msg}")
            raise ResilioApiError(f"远端返回错误: {msg}")
        return data

    # ------------------------------------------------------------------
    # 读取（Web 面板使用，纯数据）
    # ------------------------------------------------------------------

    def get_folder_list(self):
        """所有文件夹及其状态/速度/节点，对应 getsyncfolders&discovery=1。"""
        data = self._request('getsyncfolders', {'discovery': '1'})
        return data.get('folders', [])

    def get_peers_stat(self):
        """全局节点统计。此版本服务器返回空数组；节点明细以 get_folder_list 的 peers 为准。"""
        data = self._request('getpeersstat')
        return data.get('value', [])

    def get_session_stats(self):
        """会话统计：max_speed / total_transferred / transferred（down/up，字节）。"""
        data = self._request('getsessionstats')
        return data.get('value', {})

    def get_statuses(self):
        """全局状态：cpu/disk/errors/loading/speed(downspeed/upspeed)。"""
        data = self._request('getstatuses')
        return data.get('value', {})

    def get_settings(self):
        """全局设置，含 dlrate/ulrate 限速（KB/s，<=0 为不限速）。"""
        data = self._request('settings')
        return data.get('value', {})

    def get_folder_pref(self, folder_id):
        """单个文件夹的偏好配置（含 paused 等）。"""
        data = self._request('folderpref', {'id': folder_id})
        return data.get('value', {})

    # ------------------------------------------------------------------
    # 写入（Web 面板使用）
    # ------------------------------------------------------------------

    def set_speed_limits(self, download_kbps=None, upload_kbps=None):
        """设置全局限速（KB/s，<=0 或 None 表示保持/不限速）。返回最新设置。"""
        current = self.get_settings()
        dl = current.get('dlrate', -1) if download_kbps is None else int(download_kbps)
        ul = current.get('ulrate', -1) if upload_kbps is None else int(upload_kbps)
        params = {'dlrate': str(dl), 'ulrate': str(ul)}
        self._request('setsettings', params)
        return self.get_settings()

    def set_folder_paused(self, folder_id, paused):
        """设置单个文件夹暂停/恢复（folderpref 读取后全量回写，与 CLI 相同机制）。"""
        prefs = self.get_folder_pref(folder_id)
        prefs['paused'] = bool(paused)
        params = {'id': folder_id}
        for k, v in prefs.items():
            params[k] = str(v).lower() if isinstance(v, bool) else str(v)
        self._request('setfolderpref', params)

    def pause_folder(self, folder_id):
        self.set_folder_paused(folder_id, True)

    def resume_folder(self, folder_id):
        self.set_folder_paused(folder_id, False)

    def remove_folder(self, folder_id, delete_files=False):
        """从所有设备移除文件夹。delete_files=True 同时删除磁盘文件（破坏性操作）。"""
        self._request('removefolder', {
            'folderid': folder_id,
            'deletedirectory': 'true' if delete_files else 'false',
            'fromalldevices': 'true',
        })

    def disconnect_folder(self, folder_id):
        """从本设备断开文件夹（保留磁盘文件），文件夹转为断开连接(synclevel=0)。

        与官方 Web UI 的「断开连接」一致：removefolder&deletedirectory=false&fromalldevices=false。
        """
        self._request('removefolder', {
            'folderid': folder_id,
            'deletedirectory': 'false',
            'fromalldevices': 'false',
        })

    def add_folder(self, name, path, secret):
        """添加/重新连接文件夹（非交互）。name 可空。目录非空(106)时自动 force 重试。

        成功条件：error 为 0（新增）或 200（SE_SM_DUPLICATE_FOLDER，已存在/重连）。
        部分版本对「重连已断开文件夹」返回其它非 0/200 码但实际已重连，故兜底：
        若该路径下存在 synclevel!=0 的文件夹，也视为成功。
        返回 {'ok','error','message'}。
        """
        params = {'path': path, 'secret': secret}
        if name:
            params['name'] = name

        def _parse(data):
            value = data.get('value', {})
            if 'error' not in value:
                # 无 error 字段 = 成功（返回文件夹信息，如 force 添加成功）
                return 0, value.get('message', '成功')
            return value.get('error', -1), value.get('message', '成功')

        err, msg = _parse(self._request('addsyncfolder', params))
        if err in (0, 200):
            return {'ok': True, 'error': err, 'message': msg}
        if err == 106:
            params['force'] = 'true'
            err, msg = _parse(self._request('addsyncfolder', params))
            if err in (0, 200):
                return {'ok': True, 'error': err, 'message': '目录非空，已强制添加'}
        if path and any(f.get('path') == path and f.get('synclevel', 0) != 0
                        for f in self.get_folder_list()):
            return {'ok': True, 'error': err, 'message': '已重新连接'}
        return {'ok': False, 'error': err, 'message': msg}

    # ------------------------------------------------------------------
    # 既有 CLI 方法（保持原行为，含文件读写与输出）
    # ------------------------------------------------------------------

    def get_sync_folders(self):
        """功能 1: 导出 CSV。"""
        params = {'token': self.token, 'action': 'getsyncfolders', 't': int(time.time()*1000)}
        try:
            response = self.session.get(self.base_url, auth=self.auth, params=params)
            if response.status_code == 200:
                folders = response.json().get('folders', [])
                filename = "sync_folders.csv"
                headers = ['name', 'secret', 'readonlysecret', 'encryptedsecret', 'path']
                with open(filename, mode='w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.DictWriter(f, fieldnames=headers)
                    writer.writeheader()
                    for folder in folders:
                        writer.writerow({
                            'name': folder.get('name', ''),
                            'secret': folder.get('secret', ''),
                            'readonlysecret': folder.get('readonlysecret', ''),
                            'encryptedsecret': folder.get('encryptedsecret', ''),
                            'path': folder.get('path', '')
                        })
                print(f"[+] 导出成功: {filename}")
            else:
                print(f"[!] 获取失败: {response.text}")
        except Exception as e:
            print(f"[!] 出错: {e}")

    def add_sync_folders(self):
        """功能 2: 从 CSV 导入项目。"""
        filename = "sync_folders.csv"
        if not os.path.exists(filename):
            print(f"[!] 找不到文件 {filename}，请先执行功能 1。")
            return

        print("\n--- 导入选项 ---")
        print("1. 使用读写密钥 (secret)")
        print("2. 使用只读密钥 (readonlysecret)")
        print("3. 使用加密密钥 (encryptedsecret)")
        key_choice = input("请选择导入时使用的密钥类型 (1-3): ")

        key_map = {'1': 'secret', '2': 'readonlysecret', '3': 'encryptedsecret'}
        target_key = key_map.get(key_choice)

        if not target_key:
            print("[!] 无效选择，返回菜单。")
            return

        encodings = ['utf-8-sig', 'gbk', 'utf-8']
        rows = []
        success_read = False

        for enc in encodings:
            try:
                with open(filename, mode='r', encoding=enc) as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
                    success_read = True
                    break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if not success_read:
            print("[!] 无法读取 CSV 文件，请确保文件编码为 UTF-8 或 GBK。")
            return

        for row in rows:
            clean_row = {str(k).strip(): str(v).strip() for k, v in row.items() if k is not None}
            name = clean_row.get('name')
            path = clean_row.get('path')
            secret = clean_row.get(target_key)

            if not secret or not path:
                print(f"[-] 跳过项目 [{name}]: 密钥或路径为空")
                continue

            params = {
                'token': self.token,
                'action': 'addsyncfolder',
                'name': name,
                'path': path,
                'secret': secret,
                't': int(time.time()*1000)
            }

            try:
                resp = self.session.get(self.base_url, auth=self.auth, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    err = data.get('value', {}).get('error', -1)
                    if err == 0:
                        print(f"[+] 成功导入: {name}")
                    elif err == 106:
                        print(f"[-] 目录不为空 [{name}]，添加 force=true 确认...")
                        time.sleep(0.5)
                        params['force'] = 'true'
                        retry = self.session.get(self.base_url, auth=self.auth, params=params)
                        if retry.status_code == 200:
                            rdata = retry.json()
                            rerr = rdata.get('value', {}).get('error')
                            if rerr is None or rerr == 0:
                                print(f"[+] 成功导入: {name}")
                            else:
                                msg = rdata.get('value', {}).get('message', '未知错误')
                                print(f"[!] 导入失败 [{name}]: [{rerr}] {msg}")
                        else:
                            print(f"[!] 导入失败 [{name}]: HTTP {retry.status_code}")
                    else:
                        msg = data.get('value', {}).get('message', '未知错误')
                        print(f"[!] 导入失败 [{name}]: [{err}] {msg}")
                else:
                    print(f"[!] 导入失败 [{name}]: HTTP {resp.status_code} {resp.text}")
            except Exception as e:
                print(f"[!] 请求出错 [{name}]: {e}")

            time.sleep(0.5)

    def pause_all_sync(self):
        """功能 3: 暂停所有本地同步文件夹。"""
        params = {'token': self.token, 'action': 'getsyncfolders', 't': int(time.time()*1000)}
        try:
            response = self.session.get(self.base_url, auth=self.auth, params=params)
            if response.status_code != 200:
                print(f"[!] 获取文件夹列表失败")
                return
            folders = response.json().get('folders', [])
        except Exception as e:
            print(f"[!] 获取文件夹列表出错: {e}")
            return

        total = len(folders)
        paused = 0
        skipped = 0

        for folder in folders:
            name = folder.get('name', '')
            fid = folder.get('id')
            if not fid:
                skipped += 1
                continue

            try:
                pref_params = {'token': self.token, 'action': 'folderpref', 'id': fid, 't': int(time.time()*1000)}
                pref_resp = self.session.get(self.base_url, auth=self.auth, params=pref_params)
                if pref_resp.status_code != 200:
                    print(f"[-] 跳过 [{name}]: 无法读取配置")
                    skipped += 1
                    continue

                prefs = pref_resp.json().get('value', {})
                if prefs.get('paused'):
                    print(f"[=] 已暂停 [{name}]")
                    paused += 1
                    continue

                prefs['paused'] = True
                set_params = {'token': self.token, 'action': 'setfolderpref', 'id': fid}
                for k, v in prefs.items():
                    if isinstance(v, bool):
                        set_params[k] = str(v).lower()
                    else:
                        set_params[k] = str(v)
                set_params['t'] = int(time.time()*1000)

                set_resp = self.session.get(self.base_url, auth=self.auth, params=set_params)
                if set_resp.status_code == 200:
                    print(f"[+] 已暂停 [{name}]")
                    paused += 1
                else:
                    print(f"[!] 暂停失败 [{name}]: {set_resp.text}")
                    skipped += 1
            except Exception as e:
                print(f"[!] 暂停出错 [{name}]: {e}")
                skipped += 1

            time.sleep(0.3)

        print(f"\n[=] 完成: {paused} 个已暂停, {skipped} 个跳过(非本地), 共 {total} 个")

    def resume_all_sync(self):
        """功能 4: 恢复所有本地同步文件夹。"""
        params = {'token': self.token, 'action': 'getsyncfolders', 't': int(time.time()*1000)}
        try:
            response = self.session.get(self.base_url, auth=self.auth, params=params)
            if response.status_code != 200:
                print(f"[!] 获取文件夹列表失败")
                return
            folders = response.json().get('folders', [])
        except Exception as e:
            print(f"[!] 获取文件夹列表出错: {e}")
            return

        total = len(folders)
        resumed = 0
        skipped = 0

        for folder in folders:
            name = folder.get('name', '')
            fid = folder.get('id')
            if not fid:
                skipped += 1
                continue

            try:
                pref_params = {'token': self.token, 'action': 'folderpref', 'id': fid, 't': int(time.time()*1000)}
                pref_resp = self.session.get(self.base_url, auth=self.auth, params=pref_params)
                if pref_resp.status_code != 200:
                    skipped += 1
                    continue

                prefs = pref_resp.json().get('value', {})
                if not prefs.get('paused'):
                    print(f"[=] 运行中 [{name}]")
                    resumed += 1
                    continue

                prefs['paused'] = False
                set_params = {'token': self.token, 'action': 'setfolderpref', 'id': fid}
                for k, v in prefs.items():
                    if isinstance(v, bool):
                        set_params[k] = str(v).lower()
                    else:
                        set_params[k] = str(v)
                set_params['t'] = int(time.time()*1000)

                set_resp = self.session.get(self.base_url, auth=self.auth, params=set_params)
                if set_resp.status_code == 200:
                    print(f"[+] 已恢复 [{name}]")
                    resumed += 1
                else:
                    skipped += 1
            except Exception as e:
                print(f"[!] 恢复出错 [{name}]: {e}")
                skipped += 1

            time.sleep(0.3)

        print(f"\n[=] 完成: {resumed} 个已恢复, {skipped} 个跳过(非本地), 共 {total} 个")
