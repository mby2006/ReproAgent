#!/usr/bin/env bash
# 把「从公网进来的、目标是容器」的转发流量一刀切断。
#
# 为什么必须做：`supabase start` 会把 54321/54322/Studio/pgMeta/analytics 等一大串端口
# 发布到 0.0.0.0，而且这些是开发用的默认口令。本机无所谓，公网机器上等于把数据库和
# 控制台敞开。Docker 的发布规则走 DOCKER-CHAIN 之前的 nat，ufw 管不到转发流量，
# 唯一可靠的插入点是 Docker 自己预留的 DOCKER-USER 链。
#
# SSH 登录的是宿主机自身，属于 INPUT 而非 FORWARD，不受这条规则影响。
# 公网侧唯一入口仍然是 cloudflared 的出站连接（它自己不监听端口）。
#
#   ./lockdown.sh apply    安装规则并写一个开机重放的 systemd 单元
#   ./lockdown.sh status   查看当前规则
#   ./lockdown.sh revert   删掉规则与单元
set -euo pipefail

UNIT=/usr/local/sbin/reproagent-docker-lockdown.sh
SVC=/etc/systemd/system/reproagent-docker-lockdown.service
COMMENT=reproagent-block-wan-to-containers

wan_iface() { ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1; }

apply() {
  local iface; iface=$(wan_iface)
  [ -n "$iface" ] || { echo "识别不到出口网卡" >&2; exit 1; }
  echo "==> 出口网卡: $iface"

  # 先清掉本脚本装过的旧规则，避免重复叠加
  revert_rules || true

  # 顺序重要：先放行容器主动外连的回程包，再丢弃公网主动进来的。
  # 两条都用 `-I ... 1`：DOCKER-USER 可能是空链，插到位置 2 会被 iptables 判索引越界；
  # 后插的那条排在前面，所以先插 DROP 再插 ACCEPT。
  iptables -I DOCKER-USER 1 -i "$iface" -m comment --comment "$COMMENT" -j DROP
  iptables -I DOCKER-USER 1 -i "$iface" -m conntrack --ctstate RELATED,ESTABLISHED -m comment --comment "$COMMENT" -j ACCEPT
  # ip6tables 同样处理，否则 v6 侧漏。
  if ip -6 route get 2400:3200::1 >/dev/null 2>&1; then
    ip6tables -I DOCKER-USER 1 -i "$iface" -m comment --comment "$COMMENT" -j DROP || true
    ip6tables -I DOCKER-USER 1 -i "$iface" -m conntrack --ctstate RELATED,ESTABLISHED -m comment --comment "$COMMENT" -j ACCEPT || true
  fi

  mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<EOF
#!/usr/bin/env bash
# 由 deploy/server/lockdown.sh 生成。dockerd 重启会重建它自己创建的所有链，
# DOCKER-USER 也在其中，规则会静默丢失；而隧道容器一起来就需要封锁在位。
# 所以用 systemd timer 每分钟幂等重放（已存在就不重复插入）。
set -u
IFACE=\$(ip route get 1.1.1.1 | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
[ -n "\$IFACE" ] || exit 0
if ! iptables -S DOCKER-USER 2>/dev/null | grep -q -- "--comment $COMMENT"; then
  iptables -I DOCKER-USER 1 -i "\$IFACE" -m comment --comment $COMMENT -j DROP
  iptables -I DOCKER-USER 1 -i "\$IFACE" -m conntrack --ctstate RELATED,ESTABLISHED -m comment --comment $COMMENT -j ACCEPT
fi
EOF
  chmod 755 "$UNIT"

  cat > "$SVC" <<'EOF'
[Unit]
Description=ReproAgent: keep the public internet out of published container ports
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/reproagent-docker-lockdown.sh

[Install]
WantedBy=multi-user.target
EOF

  # dockerd 重启会把它自己创建的链连同 DOCKER-USER 一起重建，光靠开机一次的 oneshot
  # 会在每次 docker 重启后静默失效。用一分钟一次的幂等重放兜住这个窗口。
  cat > "${SVC%.service}.timer" <<'EOF'
[Unit]
Description=Replay ReproAgent container lockdown every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable --now reproagent-docker-lockdown.service >/dev/null
  systemctl enable --now reproagent-docker-lockdown.timer >/dev/null
  echo "==> 已启用：$SVC + $(basename "${SVC%.service}.timer")（幂等重放）"
  status
}

revert_rules() {
  local tbl line
  for tbl in iptables ip6tables; do
    command -v "$tbl" >/dev/null 2>&1 || continue
    "$tbl" -S DOCKER-USER >/dev/null 2>&1 || continue
    while true; do
      # `iptables -S CHAIN` 的第一行是 `-N CHAIN`，所以规则序号 = 行号 - 1。
      line=$("$tbl" -S DOCKER-USER | grep -n -- "-A DOCKER-USER.*--comment $COMMENT" | head -1 | cut -d: -f1)
      [ -n "${line:-}" ] || break
      "$tbl" -D DOCKER-USER "$((line - 1))" 2>/dev/null || break
    done
  done
}

revert() {
  revert_rules
  rm -f "$UNIT"
  systemctl disable --now reproagent-docker-lockdown.timer >/dev/null 2>&1 || true
  systemctl disable --now reproagent-docker-lockdown.service >/dev/null 2>&1 || true
  rm -f "$SVC" "${SVC%.service}.timer"
  systemctl daemon-reload 2>/dev/null || true
  echo "==> 规则、单元与重放定时器已移除"
}

status() {
  echo "--- iptables DOCKER-USER ---"
  iptables -S DOCKER-USER
  echo
  echo "--- 自宿主机回环探测（应当 200）---"
  code=$(curl -m 6 -so /dev/null -w '%{http_code}' http://127.0.0.1:54321/auth/v1/health 2>/dev/null || echo 000)
  echo "  127.0.0.1:54321 -> HTTP $code"
  hostip=$(hostname -I 2>/dev/null | awk '{print $1}')
  code=$(curl -m 6 -so /dev/null -w '%{http_code}' "http://$hostip:54321/auth/v1/health" 2>/dev/null || echo 000)
  echo "  ${hostip}:54321 -> HTTP $code  （期望 000 / 超时：公网侧已封）"
}

case "${1:-}" in
  apply)  apply ;;
  revert) revert ;;
  status) status ;;
  *) echo "用法: $0 {apply|status|revert}" >&2; exit 2 ;;
esac
