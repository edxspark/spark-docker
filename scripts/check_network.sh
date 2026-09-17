#!/usr/bin/env bash
# 网络体检：确认代理分流是否按预期工作（下载走海外、国内直连）。
#
# 背景：本机原来用 ZoogVPN，它的隧道抢走默认路由且没有分流规则，
# 导致 DeepSeek / 阿里云 / 抖音全被绕到日本——慢，而且抖音会看到海外 IP（风控）。
# 换成规则分流的客户端后，用这个脚本确认国内流量确实直连了。
#
# 判据是 TCP 建连耗时，不是「能不能通」：
#   - 国内直连：阿里云/抖音的 connect 通常在 0.01~0.08s
#   - 被绕到海外：实测 0.7~0.95s（差一个数量级）
# 只看「通不通」是看不出来的，这几个站绕道海外照样通。

set -uo pipefail

PROBE_CONNECT_SLOW=0.35   # 国内站点建连超过这个值，基本可以判定在绕道

say() { printf '%s\n' "$*"; }
rule() { printf '%s\n' "----------------------------------------------------------------"; }

say "网络体检　$(date '+%Y-%m-%d %H:%M:%S')"
rule

# ---------------------------------------------------------------- 出口 IP
say "【出口 IP】用 ipify 看当前对外地址（走代理的话显示代理节点 IP）"
exit_ip="$(curl -s -m 10 https://api.ipify.org 2>/dev/null || true)"
if [ -n "$exit_ip" ]; then
  say "  $exit_ip"
  say "  提示：VPN 开着时这里显示的是代理节点 IP（本机原为 5.180.77.160，日本东京）"
else
  say "  取不到（网络不可用？）"
fi
rule

# ---------------------------------------------------------------- 国内直连判定
say "【国内站点】connect 越小说明越可能是直连"
slow=0
total=0
for entry in \
  "阿里云语音合成|https://nls-gateway-cn-shanghai.aliyuncs.com" \
  "DeepSeek|https://api.deepseek.com" \
  "抖音创作者中心|https://creator.douyin.com" \
  "百度（对照）|https://www.baidu.com"
do
  name="${entry%%|*}"
  url="${entry#*|}"
  out="$(curl -s -o /dev/null -m 12 -w "%{http_code} %{time_connect} %{time_total}" "$url" 2>/dev/null)"
  code="$(echo "$out" | awk '{print $1}')"
  conn="$(echo "$out" | awk '{print $2}')"
  tot="$(echo "$out" | awk '{print $3}')"
  total=$((total + 1))
  flag=""
  if [ -n "$conn" ] && awk "BEGIN{exit !($conn > $PROBE_CONNECT_SLOW)}" 2>/dev/null; then
    flag="  ← 偏慢，疑似绕道海外"
    slow=$((slow + 1))
  fi
  printf '  %-16s HTTP %-4s connect %-9s 总计 %-9s%s\n' "$name" "${code:-—}" "${conn:-—}" "${tot:-—}" "$flag"
done
rule

# ---------------------------------------------------------------- 海外连通性
say "【海外站点】下载阶段要靠它，必须通"
yt="$(curl -s -o /dev/null -m 15 -w "%{http_code} %{time_total}" https://www.youtube.com 2>/dev/null)"
printf '  %-16s HTTP %-4s 总计 %s\n' "YouTube" "$(echo "$yt" | awk '{print $1}')" "$(echo "$yt" | awk '{print $2}')"
rule

# ---------------------------------------------------------------- 结论
say "【结论】"
if [ "$slow" -eq 0 ]; then
  say "  ✔ 国内站点建连都很快（$total 个全部 < ${PROBE_CONNECT_SLOW}s）"
  say "    → 分流生效：国内流量直连，没有被绕到海外"
elif [ "$slow" -eq "$total" ]; then
  say "  ✘ 国内站点全部偏慢（$slow/$total）"
  say "    → 分流没生效，所有流量仍在绕道海外"
  say "      检查：代理客户端的规则模式是否开启？ZoogVPN 是否已断开并关闭「自动连接」？"
else
  say "  ⚠ 国内站点部分偏慢（$slow/$total）"
  say "    → 分流不完整，把偏慢的域名补进直连规则，或加一条 GEOIP,CN,DIRECT 兜底"
fi

if [ -n "${1:-}" ] && [ "$1" = "--probe" ]; then
  rule
  say "【真实下载探测】用 yt-dlp 解析一个视频（--probe 才会跑）"
  if [ -x backend/.venv/bin/python ]; then
    head -c 400 <(cd "$(dirname "$0")/.." && backend/.venv/bin/python -m yt_dlp \
      --simulate --no-warnings --socket-timeout 20 \
      "https://www.youtube.com/watch?v=EN7frwQIbKc" 2>&1) || true
    say ""
  else
    say "  跳过：找不到 backend/.venv/bin/python"
  fi
fi
