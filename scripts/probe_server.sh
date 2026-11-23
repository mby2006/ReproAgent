#!/usr/bin/env bash
# Read-only server health probe for the ReproAgent phase-2 deployment.
# Changes nothing; safe to re-run. Intended for a root shell on the target host.
set -u

hr() { printf '%s\n' "----------"; }
sec() { printf '\n===== %s =====\n' "$1"; }
ok()  { printf '  [ok]   %s\n' "$1"; }
bad() { printf '  [FAIL] %s\n' "$1"; }
warn(){ printf '  [warn] %s\n' "$1"; }
info(){ printf '  %s\n' "$1"; }

probe_url() { # name url
  local code
  code=$(curl -m 12 -so /dev/null -w '%{http_code}' "$2" 2>/dev/null)
  if [ "${code:-000}" = "000" ]; then bad "$1: 无响应（DNS 或出网被拦）"
  else ok "$1: HTTP $code"; fi
}

tcp() { # name host port
  if timeout 8 bash -c "exec 3<>/dev/tcp/$2/$3" 2>/dev/null; then
    exec 3<&- 3>&- 2>/dev/null
    ok "$1: $2:$3 可连"
  else
    bad "$1: $2:$3 不可连"
  fi
}

sec "系统"
info "主机名: $(hostname 2>/dev/null)"
info "内核:   $(uname -srm 2>/dev/null)"
info "发行版: $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || echo unknown)"
info "架构:   $(uname -m)"
info "运行级: $(whoami) / uptime: $(uptime 2>/dev/null | sed 's/^ *//')"

sec "内存与交换（4GB 机器的核心约束）"
free -h | sed 's/^/  /'
sw_kb=$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)
if [ "${sw_kb:-0}" -lt 1048576 ]; then
  warn "交换空间不足 1GB。Supabase + 4 个应用容器 + 训练沙箱在 4GB 上必须靠 swap 兜底。"
else
  ok "交换空间 $((sw_kb / 1048576))GB 起"
fi
info "swappiness: $(cat /proc/sys/vm/swappiness 2>/dev/null)"
info "OOM 历史: $(dmesg -T 2>/dev/null | grep -ci 'out of memory' || echo 0) 条"

sec "磁盘"
df -h / /var/lib/docker 2>/dev/null | sed 's/^/  /'
free_mb=$(df -Pm / | awk 'NR==2{print $4}')
if [ "${free_mb:-0}" -lt 20480 ]; then
  bad "根分区可用 $((free_mb / 1024))GB；exec 镜像（torch CPU + 内置 MNIST）解包约 2GB，整栈镜像需 ≥20GB"
else
  ok "根分区可用 $((free_mb / 1024))GB"
fi

sec "CPU"
info "核数: $(nproc)  型号: $(awk -F': ' '/model name/{print $2; exit}' /proc/cpuinfo)"

sec "Docker"
if command -v docker >/dev/null 2>&1; then
  ok "docker $(docker --version 2>/dev/null | awk '{print $3}' | tr -d ',')"
  docker compose version >/dev/null 2>&1 && ok "compose v2 插件在" || bad "缺 docker compose v2 插件"
  if docker info >/dev/null 2>&1; then
    ok "守护进程运行中"
    info "存储驱动: $(docker info -f '{{.Driver}}' 2>/dev/null)  cgroup: $(docker info -f '{{.CgroupVersion}}' 2>/dev/null)"
    n_running=$(docker ps -q 2>/dev/null | wc -l)
    n_all=$(docker ps -aq 2>/dev/null | wc -l)
    info "容器: 运行 $n_running / 共 $n_all"
    [ "$n_all" -gt 0 ] && docker ps -a --format '  {{.Names}}\t{{.Status}}\t{{.Ports}}' | head -25
    info "镜像占用: $(docker system df --format '{{.Type}}: {{.Size}}' 2>/dev/null | tr '\n' ' ')"
  else
    bad "docker 已装但守护进程不可用（需 systemctl start docker）"
  fi
else
  bad "未安装 docker"
fi

sec "其它工具"
for t in git curl tar unzip systemctl; do
  command -v "$t" >/dev/null 2>&1 && ok "$t" || warn "缺 $t"
done
command -v cloudflared >/dev/null 2>&1 && ok "cloudflared 已装" || info "cloudflared 未装（本脚本稍后会装）"
command -v supabase >/dev/null 2>&1 && ok "supabase CLI: $(supabase --version 2>/dev/null)" || info "supabase CLI 未装（需要 v2.5x+，与本地 3.2.0 对齐）"

sec "端口占用（部署要用的）"
for p in 54321 54322 8000 3000 8080 443 80; do
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q "[:.]$p\$"; then
    warn "端口 $p 已被占用"
  else
    ok "端口 $p 空闲"
  fi
done

sec "出网可达性（决定用哪条构建路线）"
probe_url "Docker Hub API"    "https://registry-1.docker.io/v2/"
probe_url "GitHub Releases"   "https://api.github.com/rate_limit"
probe_url "PyPI"              "https://pypi.org/simple/"
probe_url "npm registry"      "https://registry.npmjs.org/"
probe_url "PyTorch CPU 索引"  "https://download.pytorch.org/whl/cpu/"
probe_url "Cloudflare 根"     "https://www.cloudflare.com/"
tcp "Cloudflare 隧道端口 7844/tcp" v25.root.cf-ips.com 7844
tcp "PyPI 镜像（清华）"        pypi.tuna.tsinghua.edu.cn 443
tcp "npm 镜像（阿里）"         registry.npmmirror.com 443

hr
info "国内大陆机房通常无法直连 Docker Hub；上面 [FAIL] 的每一项都会在部署方案里换成对应镜像源或改用本机 docker save 传输。"
info "本脚本只读，未做任何修改。把完整输出贴回来即可。"
