#!/usr/bin/env bash
set -u -o pipefail

target="${1:-}"
case "$target" in
  poppler|chromium) ;;
  *) echo "用法: $0 poppler|chromium" >&2; exit 2 ;;
esac

mirror_host="mirrors.tuna.tsinghua.edu.cn"
mirror_base="https://${mirror_host}/ubuntu/"
apt_options=(
  -o Acquire::Retries=1
  -o Acquire::http::Timeout=15
  -o Acquire::https::Timeout=15
)

switch_mirror() {
  if ! getent hosts "$mirror_host" >/dev/null 2>&1; then
    echo "::warning::备用镜像 ${mirror_host} DNS 预检失败，保留现有 APT 配置" >&2
    return 1
  fi
  if ! curl -fsI --max-time 8 "${mirror_base}dists/noble/InRelease" >/dev/null; then
    echo "::warning::备用镜像 ${mirror_host} HTTPS 预检失败，保留现有 APT 配置" >&2
    return 1
  fi

  local file
  for file in /etc/apt/apt-mirrors.txt /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    [ -f "$file" ] || continue
    sudo sed -E -i 's#https?://azure\.archive\.ubuntu\.com/ubuntu/?#https://mirrors.tuna.tsinghua.edu.cn/ubuntu/#g' "$file" || return 1
  done
}

for attempt in 1 2 3; do
  if [ "$attempt" -gt 1 ]; then
    switch_mirror || echo "::warning::未切换到备用镜像，本次安装仍按原配置执行" >&2
  fi
  if [ "$target" = "poppler" ]; then
    sudo apt-get "${apt_options[@]}" update --error-on=any &&
      sudo apt-get "${apt_options[@]}" install -y poppler-utils
    rc=$?
  else
    npx playwright install --with-deps chromium
    rc=$?
  fi
  if [ "$rc" -eq 0 ]; then
    if [ "$target" != "poppler" ] || { command -v pdftotext >/dev/null && pdftotext -v; }; then
      exit 0
    fi
    echo "::warning::poppler 安装后 pdftotext 验证失败" >&2
  fi
  if [ "$attempt" -lt 3 ]; then
    echo "::warning::${target} 第 ${attempt} 次安装失败，5 秒后重试"
    sleep 5
  fi
done
exit 1
