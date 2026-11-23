#!/usr/bin/env bash
# 换对外域名：只重写和域名绑死的几行，并重签 anon / service_role 两枚 key。
#
# 为什么不直接 gen_secrets.sh --force：那会把数据库口令、JWT_SECRET、演示账号密码
# 全部重新随机，等于换一台机器的凭据 —— 库里的账号还在，但你填进去的 QWEN_API_KEY
# 这类手工值也会被覆盖。换域名只需要动「iss 里那个字符串」和两个 URL。
#
#   ./set_domain.sh api.mbyheran.site sb.mbyheran.site
#
# 之后必须重建这些容器（env 变了，不重建不生效）：
#   supabase: auth（GOTRUE_JWT_ISSUER）、api-gw（Kong 的 keyauth 凭据）、storage（ANON/SERVICE key）
#   reproagent-prod: api、worker
# 用 bootstrap.sh 的写法重建：见下方打印的命令。
set -euo pipefail
cd "$(dirname "$0")"

API_HOST="${1:-}"
SB_HOST="${2:-}"
[ -n "$API_HOST" ] && [ -n "$SB_HOST" ] || { echo "用法: $0 <api 域名> <sb 域名>" >&2; exit 2; }
case "$API_HOST$SB_HOST" in */*|*:*) echo "只传主机名，不要带 https:// 或路径" >&2; exit 2;; esac
[ -f .env.prod ] || { echo "缺少 .env.prod" >&2; exit 1; }

read_env() { sed -n "s/^$1=//p" .env.prod | head -1; }
set_kv() {
  local key="$1" val="$2"
  if grep -qE "^${key}=" .env.prod; then
    sed -i "s|^${key}=.*|${key}=${val}|" .env.prod
  else
    printf '%s=%s\n' "$key" "$val" >>.env.prod
  fi
}

JWT_SECRET=$(read_env JWT_SECRET)
SITE_ORIGIN=$(read_env SITE_URL)
[ -n "$JWT_SECRET" ] && [ -n "$SITE_ORIGIN" ] || { echo ".env.prod 缺 JWT_SECRET 或 SITE_URL" >&2; exit 1; }

API_EXTERNAL_URL="https://$API_HOST"
SB_ORIGIN="https://$SB_HOST"
ANON_KEY=$(python3 jwt_key.py "$JWT_SECRET" "$API_EXTERNAL_URL" anon)
SERVICE_ROLE_KEY=$(python3 jwt_key.py "$JWT_SECRET" "$API_EXTERNAL_URL" service_role)

cp .env.prod ".env.prod.bak-$(date +%s)"
set_kv API_EXTERNAL_URL         "$API_EXTERNAL_URL"
set_kv SUPABASE_PUBLIC_URL      "$SB_ORIGIN"
set_kv ADDITIONAL_REDIRECT_URLS "$SITE_ORIGIN/login,$SITE_ORIGIN/"
set_kv ANON_KEY                 "$ANON_KEY"
set_kv SERVICE_ROLE_KEY         "$SERVICE_ROLE_KEY"

# 只打印指纹，不把密钥打到终端记录里。
mask() { local v=$1; printf '%s…%s(%d字符)' "${v:0:12}" "${v: -6}" "${#v}"; }
cat <<EOF
==> 已更新 .env.prod（旧文件备份为 .env.prod.bak-*）
    API_EXTERNAL_URL    = $API_EXTERNAL_URL
    SUPABASE_PUBLIC_URL = $SB_ORIGIN
    ANON_KEY            = $(mask "$ANON_KEY")
    SERVICE_ROLE_KEY    = $(mask "$SERVICE_ROLE_KEY")
    JWT_SECRET / POSTGRES_PASSWORD / DEMO_* 未改动

==> 现在重建受影响的容器：
    cd $(pwd)
    ./bootstrap.sh supabase        # 会重建 auth / api-gw / storage
    docker compose --env-file .env.prod -f docker-compose.prod.yml up -d api worker
EOF
