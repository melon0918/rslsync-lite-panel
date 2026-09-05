"""只读探测示例:登录 Resilio Sync (rslsync) WebUI,打印文件夹与节点摘要。

演示根目录 resilio_api.ResilioSyncClient 的最小用法:
登录 -> 拉取文件夹列表 -> 拉取活跃节点 -> 打印摘要。
字段结构以 Resilio GUI API 实测为准,详见 docs/api-reference.md。

用法(凭证只从环境变量读取,切勿写进代码或提交到仓库):

    # Linux / macOS
    export RSL_BASE_URL="https://<server-ip>:8888"
    export RSL_USER="admin"
    export RSL_PASS="your-password"

    # Windows PowerShell
    $env:RSL_BASE_URL = "https://<server-ip>:8888"
    $env:RSL_USER = "admin"
    $env:RSL_PASS = "your-password"

    python examples/probe_api_demo.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resilio_api import ResilioApiError, ResilioSyncClient


def main() -> int:
    base_url = os.environ.get("RSL_BASE_URL", "").strip()
    username = os.environ.get("RSL_USER", "").strip()
    password = os.environ.get("RSL_PASS", "")

    if not (base_url and username and password):
        print("缺少环境变量:RSL_BASE_URL / RSL_USER / RSL_PASS")
        return 1

    if not base_url.endswith("/"):
        base_url += "/"

    client = ResilioSyncClient(base_url, username, password)

    try:
        folders = client.get_folder_list()
        peers = client.get_peers_stat()
    except ResilioApiError as exc:
        # ResilioApiError 的文本已脱敏,不含服务器地址与密码
        print(f"API 调用失败:{exc}")
        return 2

    folder_list = folders.get("folders", []) if isinstance(folders, dict) else (folders or [])
    print(f"文件夹总数:{len(folder_list)}")
    for folder in folder_list[:10]:
        name = folder.get("name") or folder.get("path") or "<unknown>"
        secret = str(folder.get("secret") or "")
        tail = f"...{secret[-6:]}" if secret else ""
        print(f"  - {name}{tail}")

    peer_count = len(peers) if isinstance(peers, (list, dict)) else 0
    print(f"活跃节点数:{peer_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
