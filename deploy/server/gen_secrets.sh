#!/usr/bin/env bash
# 在服务器上生成 deploy/server/.env.prod（含全部自托管 Supabase 机密）。
#
# 为什么必须自己生成：`supabase start` 用的那套 JWT_SECRET / anon key / service key
# 是公开写在 Supabase 文档和二进制里的开发默认值。我在 11-23 实测过：拿那个公开的
# secret 自己签一个 role=service_role 的 JWT，直连 /auth/v1/admin/users 返回 200，
# 直连 /rest/v1/papers 也返回 200 —— 等于任何人都能绕开 RLS 读全库、列全部用户。
# 自托管的价值就在这几个字节上：密钥只有这台机器知道，伪造就再也不成立。
#
# 变量清单直接取自上游 .env.example（cp 之后逐项覆盖），因为官方 compose 里有上百个
# ${VAR} 插值，漏一个就变成空串 —— 那种「本地能跑、上线少变量」的坑不该靠记忆排。
#
# 用法（服务器上，仓库在 /opt/reproagent）：
#   cd /opt/reproagent/deploy/server
#   ./gen_secrets.sh <静态站 origin> <api 域名> <sb 域名> [--force]
# 例：
#   ./gen_secrets.sh https://paper-repro-lab-xxxx.qoder.zone api.example.xyz sb.example.xyz
#
# 脚本只在文件不存在时写入；已有真实机密时须显式 --force（会先备份）。
set -euo pipefail

SITE_ORIGIN="${1:-}"; API_HOST="${2:-}"; SB_HOST="${3:-}"
FORCE=""
for a in "$@"; do [ "$a" = "--force" ] && FORCE=1; done
[ -n "$SITE_ORIGIN" ] && [ -n "$API_HOST" ] && [ -n "$SB_HOST" ] \
  || { echo "用法: $0 <静态站 origin> <api 域名> <sb 域名> [--force]" >&2; exit 2; }
case "$SITE_ORIGIN" in https://*) ;; *) echo "静态站 origin 必须是 https://…" >&2; exit 2;; esac

HERE="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM="${UPSTREAM_DOCKER_DIR:-/opt/supabase-src/docker}"
TEMPLATE="$UPSTREAM/.env.example"
OUT="$HERE/.env.prod"

[ -f "$TEMPLATE" ] || { echo "找不到上游模板：$TEMPLATE（先跑 bootstrap 的 sparse clone）" >&2; exit 1; }
if [ -f "$OUT" ] && [ -z "$FORCE" ]; then
  echo "$OUT 已存在；要重新生成请带 --force（旧的会先备份成 .env.prod.bak-<时间戳>）" >&2
  exit 1
fi
[ -f "$OUT" ] && cp "$OUT" "$OUT.bak-$(date +%s)"

# 只生成 URL 安全字符：这些值要过 sed 的替换体，还要进 JWT，绝不能出现 | / " / \。
rand_hex() { openssl rand -hex "$1"; }

JWT_SECRET=$(rand_hex 32)                      # 64 hex，HS256 签名密钥（>=32 字节）
POSTGRES_PASSWORD=$(rand_hex 24)
SECRET_KEY_BASE=$(rand_hex 32)
VAULT_ENC_KEY=$(rand_hex 16)                   # 上游要求 32 字符
DASHBOARD_PASSWORD=$(rand_hex 16)
S3_PROTOCOL_ACCESS_KEY_ID=$(rand_hex 16)
S3_PROTOCOL_ACCESS_KEY_SECRET=$(rand_hex 32)

API_EXTERNAL_URL="https://$API_HOST"
SB_ORIGIN="https://$SB_HOST"

# anon / service_role key：HS256 自签，iss 必须与 GOTRUE_JWT_ISSUER 完全一致
# （= API_EXTERNAL_URL，见官方 compose 的 auth 段），GoTrue 会逐字校验 iss。
make_key() {
  JWT_SECRET="$JWT_SECRET" API_EXTERNAL_URL="$API_EXTERNAL_URL" python3 - "$1" <<'PY'
import base64, hashlib, hmac, json, os, sys, time
def b64(raw): return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
now = int(time.time())
header = {"alg": "HS256", "typ": "JWT"}
claims = {"role": sys.argv[1], "iss": os.environ["API_EXTERNAL_URL"],
          "iat": now, "exp": now + 10 * 365 * 86400}
signing = (b64(json.dumps(header, separators=(",", ":")).encode()) + "." +
           b64(json.dumps(claims, separators=(",", ":")).encode()))
secret = os.environ["JWT_SECRET"].encode()
sig = b64(hmac.new(secret, signing.encode(), hashlib.sha256).digest())
print(f"{signing}.{sig}")
PY
}
ANON_KEY=$(make_key anon)
SERVICE_ROLE_KEY=$(make_key service_role)

cp "$TEMPLATE" "$OUT"
chmod 600 "$OUT"

# 存在则替换、不存在则追加；键名按字面处理，值里的 / 不冲突（分隔符用 |）。
set_kv() {
  local key="$1" val="$2"
  if grep -qE "^${key}=" "$OUT"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$OUT"
  else
    printf '%s=%s\n' "$key" "$val" >>"$OUT"
  fi
}

# ---- 机密 ----
set_kv POSTGRES_PASSWORD          "$POSTGRES_PASSWORD"
set_kv JWT_SECRET                 "$JWT_SECRET"
set_kv ANON_KEY                   "$ANON_KEY"
set_kv SERVICE_ROLE_KEY           "$SERVICE_ROLE_KEY"
set_kv SECRET_KEY_BASE            "$SECRET_KEY_BASE"
set_kv VAULT_ENC_KEY              "$VAULT_ENC_KEY"
set_kv DASHBOARD_PASSWORD         "$DASHBOARD_PASSWORD"
set_kv S3_PROTOCOL_ACCESS_KEY_ID  "$S3_PROTOCOL_ACCESS_KEY_ID"
set_kv S3_PROTOCOL_ACCESS_KEY_SECRET "$S3_PROTOCOL_ACCESS_KEY_SECRET"
# 非对称 JWT / 不透明 key 是托管平台的东西，自托管留空即可（kong-entrypoint 会据此走 legacy 分支）。
set_kv SUPABASE_PUBLISHABLE_KEY   ""
set_kv SUPABASE_SECRET_KEY        ""
set_kv ANON_KEY_ASYMMETRIC        ""
set_kv SERVICE_ROLE_KEY_ASYMMETRIC ""
set_kv JWT_KEYS                   ""
set_kv JWT_JWKS                   ""

# ---- 对外地址 ----
set_kv SITE_URL                   "$SITE_ORIGIN"
set_kv ADDITIONAL_REDIRECT_URLS   "$SITE_ORIGIN/login,$SITE_ORIGIN/"
set_kv API_EXTERNAL_URL           "$API_EXTERNAL_URL"
# Storage 签名 URL 的拼接前缀。公网 sb. 只放行 auth 路径，所以这个值现在只影响
# 「万一有人内网拿到签名 URL」的形态；保留 https 是为了不让任何链路退回明文。
set_kv SUPABASE_PUBLIC_URL        "$SB_ORIGIN"

# ---- 注册闸门：账号只由服务端管理接口预建 ----
set_kv DISABLE_SIGNUP             "true"
# 名字骗人：官方 compose 把它接到 GOTRUE_EXTERNAL_EMAIL_ENABLED，即「整个邮件登录
# 提供者」的开关，不只是注册。设成 false 会让已经建好的账号也登不进来
# （2026-11-23 实测 422 email_provider_disabled）。注册由上面的 DISABLE_SIGNUP 关。
set_kv ENABLE_EMAIL_SIGNUP        "true"
set_kv ENABLE_EMAIL_AUTOCONFIRM   "true"
set_kv ENABLE_PHONE_SIGNUP        "false"
set_kv ENABLE_PHONE_AUTOCONFIRM   "false"
set_kv ENABLE_ANONYMOUS_USERS     "false"
set_kv JWT_EXPIRY                 "3600"

# ---- 没有 SMTP：把邮件相关指向一个不存在的 host，避免 GoTrue 连本地 mailpit ----
# 忘记密码在前端已整体隐藏，这里再关掉服务端这一侧，两层各自独立。
set_kv SMTP_HOST                  "127.0.0.1"
set_kv SMTP_PORT                  "25"
set_kv SMTP_USER                  ""
set_kv SMTP_PASS                  ""
set_kv SMTP_ADMIN_EMAIL           "noreply@$API_HOST"
set_kv SMTP_SENDER_NAME           "ReproAgent"

# ---- 应用侧（本文件同时给 docker-compose.prod.yml 用）----
set_kv CORS_ORIGINS               "$SITE_ORIGIN"
set_kv SITE_ORIGIN                "$SITE_ORIGIN"
set_kv QWEN_API_KEY               "REPLACE_ME"
set_kv QWEN_BASE_URL              "https://api.siliconflow.cn/v1"
set_kv QWEN_MODEL                 "Qwen/Qwen2.5-72B-Instruct-128K"
set_kv WORKER_MAX_JOBS            "1"
set_kv RUNNER_MAX_CONCURRENT_RUNS "1"
set_kv RUNNER_TIMEOUT_SECONDS     "600"
set_kv RUNNER_HOST_WORK_DIR       "/var/lib/reproagent/runner-work"
set_kv RUNNER_ENFORCE_DISK_QUOTA  "true"
set_kv DEMO_EMAIL                 "demo@reproagent.dev"
set_kv DEMO_PASSWORD              "$(openssl rand -hex 8)"
set_kv CLOUDFLARE_TUNNEL_TOKEN    "REPLACE_ME"

cat >&2 <<EOF
==> 已生成 $OUT（chmod 600）

静态站 origin : $SITE_ORIGIN
API 域名      : $API_EXTERNAL_URL
Supabase 域名 : $SB_ORIGIN

上线前必须手工填的两行：
  QWEN_API_KEY=REPLACE_ME
  CLOUDFLARE_TUNNEL_TOKEN=REPLACE_ME
可选改：DEMO_EMAIL / DEMO_PASSWORD（一键演示账号的凭据，之后由 create_demo_user.sh 建号）。

注意：API_EXTERNAL_URL 已写进 ANON_KEY / SERVICE_ROLE_KEY 的 iss 声明里。
以后换域名要重新生成密钥，否则 GoTrue 会拒收这两个 key。
EOF
