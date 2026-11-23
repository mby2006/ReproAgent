#!/usr/bin/env bash
# ReproAgent 服务器侧编排（在服务器上跑，仓库假定在 /opt/reproagent）。
#
# 这套步骤的由来：11-23 用 `supabase start` 起过一次，然后用它文档里公开的开发密钥
# 签了个 role=service_role 的 JWT，直接 200 拿到了 /auth/v1/admin/users 和 /rest/v1 的表。
# 那不是配置失误，是 supabase start 的设计前提 —— 它是给本地开发用的，密钥天生公开。
# 所以这里改成官方自托管 compose：机密由 gen_secrets.sh 在本机生成，公网侧再套一层
# 只放行 auth 四个精确路径的 nginx。两道防线各自独立，任一条被绕过另一条仍在。
#
#   ./bootstrap.sh data      建 /var/lib/reproagent 下的数据目录，并把上游 compose 里
#                            ./volumes/db/data 与 ./volumes/storage 改成指向它们的软链接
#   ./bootstrap.sh supabase  起精简数据层（db/auth/rest/storage/imgproxy/kong）
#   ./bootstrap.sh migrate   按文件名顺序打 8 个迁移（ON_ERROR_STOP，任一失败即停）
#   ./bootstrap.sh app       起应用层（api/worker/agent/runner/redis/edge/tunnel）
#   ./bootstrap.sh gate      上线关卡：端口没漏、白名单生效、伪造失败、登录成功
#   ./bootstrap.sh status    各容器状态 + 宿主机监听端口
#   ./bootstrap.sh down      只停容器，数据都在 /var/lib/reproagent 下
#
# 顺序就是依赖顺序：data → supabase → migrate → app → gate。
# 没有 supabase 就没有 supabase_default 网络，app 的 external 网络会直接报错，
# 这是刻意的：起错顺序就起不来，而不是静默连到一个空数据库。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
UPSTREAM="${UPSTREAM_DOCKER_DIR:-/opt/supabase-src/docker}"
ENV_FILE="$HERE/.env.prod"
DATA_ROOT=/var/lib/reproagent/supabase
MIGRATIONS=( "$REPO"/infra/supabase/migrations/*.sql )

SB=(docker compose -p supabase --env-file "$ENV_FILE"
    -f "$UPSTREAM/docker-compose.yml"
    -f "$UPSTREAM/docker-compose.kong.yml"
    -f "$HERE/docker-compose.slim.yml")
APP=(docker compose --env-file "$ENV_FILE" -f "$HERE/docker-compose.prod.yml")

need_env() {
  [ -f "$ENV_FILE" ] || { echo "缺少 $ENV_FILE：先跑 gen_secrets.sh" >&2; exit 1; }
}

step_data() {
  local pair in_clone target link
  mkdir -p "$DATA_ROOT/db-data" "$DATA_ROOT/storage" "$DATA_ROOT/../runner-work"
  # 上游 compose 用相对路径 ./volumes/...，所以把那两个位置做成软链接指过去：
  # 数据落在 /var/lib/reproagent，而 /opt/supabase-src 是个可以随手删掉重来的 clone。
  for pair in "db/data:$DATA_ROOT/db-data" "storage:$DATA_ROOT/storage"; do
    in_clone="${pair%%:*}"
    target="${pair#*:}"
    link="$UPSTREAM/volumes/$in_clone"
    # 上游仓库在 volumes/storage 里放了一个 .gitkeep 占位；它不是数据。
    # 除此以外只要有真实内容就拒绝覆盖 —— 那个位置可能留着上一版的对象文件。
    local real_entries
    real_entries=$(find "$link" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' 2>/dev/null | wc -l)
    if [ -d "$link" ] && [ ! -L "$link" ] && [ "$real_entries" != 0 ]; then
      echo "$link 里已有非占位文件，不敢动（可能是旧数据）。请人工确认后再重跑。" >&2
      exit 1
    fi
    if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
      echo "==> $link 已指向 $target"
      continue
    fi
    rm -rf "$link"
    ln -s "$target" "$link"
    echo "==> $link -> $target"
  done
}

step_supabase() {
  need_env
  # 只点需要的六个服务：realtime/meta/functions/supavisor/vector/studio 既不在请求里，
  # 也不被这六个依赖（api-gw 对 studio 的依赖已在 slim.yml 里换掉），所以根本不会起。
  "${SB[@]}" up -d db auth rest storage imgproxy api-gw
  "${SB[@]}" ps
}

step_migrate() {
  need_env
  local f
  for f in "${MIGRATIONS[@]}"; do
    echo "==> $(basename "$f")"
    # 容器内经 unix socket 连本地 postgres：免口令，且流量完全不经过任何网络。
    # ON_ERROR_STOP=1 配上 set -e：任一迁移报错就停在这里，而不是让后面的静默错位。
    "${SB[@]}" exec -T db psql -U postgres -v ON_ERROR_STOP=1 -q -f - < "$f"
  done
  # 迁移生效的证据用我们自己建的表，而不是 supabase_migrations 那种上游记账表：
  # 我们是直接 psql 打进去的，没有版本号表可读。
  local n
  n=$("${SB[@]}" exec -T db psql -U postgres -tA -c \
        "select count(*) from information_schema.tables where table_schema='public'")
  echo "==> public 表数：$n"
  "${SB[@]}" exec -T db psql -U postgres -tA -c \
    "select relname || ' ' || case relrowsecurity when true then 'RLS on' else 'RLS OFF' end
       from pg_class where relname in ('papers','runs','reproductions','evidence','comparisons')
      order by relname"
}

step_app() {
  need_env
  "${APP[@]}" up -d --build
  "${APP[@]}" ps
}

step_status() {
  echo "--- supabase ---"; "${SB[@]}" ps || true
  echo "--- reproagent-prod ---"; "${APP[@]}" ps || true
  echo "--- 宿主机监听端口（非回环，只应看到 sshd；54321/54322/8000/8443 都不该出现）---"
  ss -ltnp | awk 'NR==1 || ($4 != "" && $4 !~ /^(127\.0\.0\.1|\[::1\]|::1):/)'
}

# 在 api 容器里发 HTTP：它一定有 python3（镜像就是 uvicorn），而且已经在
# supabase_default 网络内 —— 于是能同时打到 kong 和 edge，等于把「内网可达、
# 公网白名单」这两件事分开验证，而不是笼统地看公网通不通。
probe() { # probe <url> [apikey] [bearer]
  # 注意用 exec -e 传变量：直接写 `VAR=x docker compose exec ...` 只会设置 docker CLI
  # 自己的环境，容器里读不到（2026-11-23 这里报过 KeyError: 'API_URL'）。
  local url="$1" apikey="${2:-}" bearer="${3:-}"
  "${APP[@]}" exec -T -e "API_URL=$url" -e "API_KEY=$apikey" -e "API_BEARER=$bearer" \
    api python3 -c '
import os, urllib.error, urllib.request
req = urllib.request.Request(os.environ["API_URL"])
if os.environ["API_KEY"]:
    req.add_header("apikey", os.environ["API_KEY"])          # Kong 要的是裸 key
if os.environ["API_BEARER"]:
    bearer = os.environ["API_BEARER"]
    req.add_header("Authorization", bearer if bearer.startswith("Bearer") else "Bearer " + bearer)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
'
}

expect() { # expect <描述> <期望码> <实际码>
  local desc="$1" want="$2" got="$3"
  if [ "$got" = "$want" ]; then
    echo "  ok   $desc -> $got"
  else
    echo "  FAIL $desc -> 期望 $want，实际 $got"
    FAILURES=$((FAILURES + 1))
  fi
}

step_gate() {
  need_env
  FAILURES=0
  local anon_key dev_forged sb_origin api_origin demo_email demo_password
  anon_key=$(sed -n 's/^ANON_KEY=\(.*\)$/\1/p' "$ENV_FILE")
  sb_origin=$(sed -n 's/^SUPABASE_PUBLIC_URL=\(.*\)$/\1/p' "$ENV_FILE")
  api_origin=$(sed -n 's/^API_EXTERNAL_URL=\(.*\)$/\1/p' "$ENV_FILE")
  demo_email=$(sed -n 's/^DEMO_EMAIL=\(.*\)$/\1/p' "$ENV_FILE")
  demo_password=$(sed -n 's/^DEMO_PASSWORD=\(.*\)$/\1/p' "$ENV_FILE")
  # 现场签一枚「用 Supabase 公开开发密钥签的 service_role」JWT。
  # 之所以值得专门测它：11-23 那次实测，同样这串字节在 supabase start 上是 200。
  dev_forged=$(python3 - <<'PY'
import base64, hashlib, hmac, json
def b64(raw): return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
secret = b"super-secret-jwt-token-with-at-least-32-characters-long"
signing = (b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode()) + "." +
           b64(json.dumps({"role": "service_role", "iss": "http://localhost:54321/auth/v1",
                           "iat": 1700000000, "exp": 1800000000}, separators=(",", ":")).encode()))
print(signing + "." + b64(hmac.new(secret, signing.encode(), hashlib.sha256).digest()))
PY
)

  echo "==> 1. 宿主机端口面：除 api 的回环端口，Supabase 的任何服务都不该被发布"
  local leaked
  # 注意 `|| true`：set -o pipefail 下 grep 无匹配会返回 1，而「没有泄漏」正是无匹配。
  leaked=$(ss -ltn | grep -oE ':(54321|54322|5432|8443|3000)\b' | sort -u | tr '\n' ' ' || true)
  if [ -n "$leaked" ]; then
    echo "  FAIL 宿主机仍在监听：$leaked"; FAILURES=$((FAILURES + 1))
  else
    echo "  ok   54321/54322/5432/8443/3000 均未在宿主机监听"
  fi
  # api 允许发布，但只允许在回环上：0.0.0.0:8000 就等于绕过隧道的鉴权面。
  if ss -ltn | grep -qE '^(LISTEN).*(0\.0\.0\.0|\*):8000\b'; then
    echo "  FAIL api 端口发布到了非回环地址"; FAILURES=$((FAILURES + 1))
  else
    echo "  ok   8000 只在 127.0.0.1（smoke_test 用）"
  fi

  echo "==> 2. 内网侧：Kong 功能完整（api/worker 要用 /rest 和 /storage，必须通）"
  # health 也要 apikey：Kong 的 auth-v1 服务挂了 key-auth，浏览器侧 supabase-js 每次都带。
  expect "内网 Kong auth 健康"  200 "$(probe http://kong:8000/auth/v1/health "$anon_key")"
  expect "内网 Kong rest 读表"  200 "$(probe "http://kong:8000/rest/v1/papers?select=id&limit=1" "$anon_key" "$anon_key")"

  echo "==> 3. 白名单：edge 只放行 auth 的四个精确路径，其余 404"
  expect "edge 放行 /auth/v1/health" 200 "$(probe http://edge/auth/v1/health "$anon_key")"
  expect "edge 拦截 /rest/v1"        404 "$(probe http://edge/rest/v1/papers)"
  expect "edge 拦截 /storage/v1"     404 "$(probe http://edge/storage/v1/object/papers)"
  expect "edge 拦截 /auth/v1/admin"  404 "$(probe http://edge/auth/v1/admin/users "$anon_key" "$anon_key")"
  # 前缀漏洞自查：/auth/v1/token 放行不等于 /auth/v1/token/.. 也放行。
  expect "edge 不吞子路径"           404 "$(probe http://edge/auth/v1/token/../../auth/v1/admin/users)"

  echo "==> 4. 公开的 supabase 开发密钥签出来的 JWT，在这里必须无效"
  expect "dev-secret 伪造 JWT 无 apikey" 401 "$(probe "http://kong:8000/rest/v1/papers?select=id&limit=1" "" "$dev_forged")"

  echo "==> 5. 公网侧（域名/隧道未就绪时这些会 000）"
  # 刻意不走本机解析器：这台机器的 systemd-resolved 有两条链路，其中一个挂着云厂商
  # 的 metadata DNS，对 api.<域名> 会返回 "<invalid>"，而 sb.<域名> 又是好的。
  # 直接向权威 NS 要 IP 再 --resolve 直连，测的是「隧道 + Cloudflare + 白名单」，
  # 而不是这台机器的 DNS 配置。
  pub_code() { # pub_code <host> <path> [curl 附加参数...]
    local host="$1" path="$2" ip; shift 2
    command -v dig >/dev/null || { echo 000; return; }
    ip=$(dig +short +time=4 +tries=1 A "$host" @223.5.5.5 | awk '/^[0-9.]+$/{print; exit}')
    [ -n "$ip" ] || { echo 000; return; }
    curl -4 -m 15 -so /dev/null -w '%{http_code}' --resolve "$host:443:$ip" "https://$host$path" "$@" 2>/dev/null || true
  }
  local sb_host api_host
  sb_host="${sb_origin#https://}"
  api_host="${api_origin#https://}"
  expect "公网 anon key 读表被挡" 404 "$(pub_code "$sb_host" "/rest/v1/papers?select=id&limit=1" \
    -H "apikey: $anon_key" -H "Authorization: Bearer $anon_key")"
  expect "公网 admin 被挡" 404 "$(pub_code "$sb_host" "/auth/v1/admin/users" \
    -H "apikey: $anon_key" -H "Authorization: Bearer $anon_key")"
  expect "公网登录成功" 200 "$(pub_code "$sb_host" "/auth/v1/token?grant_type=password" \
    -X POST -H 'Content-Type: application/json' -H "apikey: $anon_key" \
    -d "{\"email\":\"$demo_email\",\"password\":\"$demo_password\"}")"
  expect "公网 API 健康" 200 "$(pub_code "$api_host" "/health")"

  if [ "$FAILURES" != 0 ]; then
    echo "==> 关卡未通过：$FAILURES 条不符" >&2
    echo "    第 5 组全 000 = 隧道还没起，先补 CLOUDFLARE_TUNNEL_TOKEN；其余不符不要上线。" >&2
    exit 1
  fi
  echo "==> 关卡通过"
}

case "${1:-}" in
  data)      step_data ;;
  supabase)  step_supabase ;;
  migrate)   step_migrate ;;
  app)       step_app ;;
  gate)      step_gate ;;
  status)    step_status ;;
  down)      "${APP[@]}" down; "${SB[@]}" down ;;
  *) echo "用法: $0 {data|supabase|migrate|app|gate|status|down}" >&2; exit 2 ;;
esac
