#!/usr/bin/env bash
# 用 GoTrue 管理接口预建演示账号（生产已关自助注册，注册入口不再可用）。
# 可重复执行：已存在则只报告，不重置密码。
#
#   ./create_demo_user.sh          # 凭据取自同目录 .env.prod 的 DEMO_EMAIL / DEMO_PASSWORD
#
# 为什么请求要从 api 容器里发：服务器上不发布任何 Supabase 端口（见 bootstrap.sh 的
# 架构说明），宿主机上没有 127.0.0.1:54321 可打。api 容器本来就在 supabase_default
# 网络里、本来就持有 service_role key，用它当跳板既不用开口，也不用把密钥再传一遍。
#
# 凭据走 stdin 而不是命令行参数：argv 会出现在 docker exec 的进程列表里，
# 那台机器上任何能 ps 的人都能看到演示账号密码。
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env.prod ] || { echo "缺少 deploy/server/.env.prod" >&2; exit 1; }
CURRENT=/var/lib/reproagent/releases/current
[ -n "${IMAGE_TAG:-}" ] || [ -f "$CURRENT" ] || {
  echo "缺少 $CURRENT：先运行 ops.py release，或显式设置 IMAGE_TAG" >&2; exit 1;
}
export IMAGE_TAG="${IMAGE_TAG:-$(cat "$CURRENT")}"
# 不 source 这个文件：它以上游 .env.example 为底本，里面有不带引号的含空格值
# （STUDIO_DEFAULT_ORGANIZATION=Default Organization），bash 会把它当成两条命令。
# compose 的解析器按「等号后整行」取值，所以只有 shell 这边需要绕开。
read_env() { sed -n "s/^$1=//p" .env.prod | head -1; }
email="${DEMO_EMAIL:-$(read_env DEMO_EMAIL)}"
password="${DEMO_PASSWORD:-$(read_env DEMO_PASSWORD)}"
[ -n "$email" ] && [ -n "$password" ] || { echo ".env.prod 里没有 DEMO_EMAIL / DEMO_PASSWORD" >&2; exit 1; }

# 值要拼进 JSON，先挡掉会破坏结构的字符。
case "$email$password" in
  *'"'*|*'\'*) echo "DEMO_EMAIL / DEMO_PASSWORD 不能包含引号或反斜杠" >&2; exit 1 ;;
esac

# 脚本本身用 -c 传进去，凭据走 stdin。
# 注意不能用 `python3 - <<'PY'`：heredoc 会顶掉前面的管道，python 拿到的是空 stdin
# （2026-11-23 这里真的踩了一次，报 "not enough values to unpack"）。
py=$(cat <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

base = os.environ["SUPABASE_URL"].rstrip("/")
service = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
anon = os.environ["SUPABASE_ANON_KEY"]
email, password = sys.stdin.read().splitlines()[:2]


def call(path, body=None, key=service, method="POST"):
    req = urllib.request.Request(
        base + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


status, payload = call(
    "/auth/v1/admin/users",
    {"email": email, "password": password, "email_confirm": True},
)
if status in (200, 201):
    print(f"==> 已创建 {email}")
elif "already" in json.dumps(payload).lower():
    print(f"==> {email} 已存在，未做改动")
else:
    print(f"==> 创建失败：HTTP {status} {payload}", file=sys.stderr)
    sys.exit(1)

# 再按浏览器将要走的 grant_type=password 走一遍，确认这个账号真能登录。
status, payload = call(
    "/auth/v1/token?grant_type=password",
    {"email": email, "password": password},
    key=anon,
)
if status == 200 and "access_token" in payload:
    print("==> 密码登录验证通过。前端构建时把这两个值给 NEXT_PUBLIC_DEMO_EMAIL / _PASSWORD。")
else:
    print(f"==> 登录验证失败：HTTP {status} {payload}", file=sys.stderr)
    sys.exit(1)
PY
)

printf '%s\n%s\n' "$email" "$password" | docker compose --env-file .env.prod \
  -f docker-compose.prod.yml exec -T api python3 -c "$py"
