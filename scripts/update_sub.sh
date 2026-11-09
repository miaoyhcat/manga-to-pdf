#!/bin/bash
# 自动更新 mihomo（Clash.Meta）订阅，并保留 EH 分流配置。
#
# 订阅地址不写在仓库里，放到单独文件：
#   sudo tee /etc/mihomo-sub.conf <<'EOF'
#   MIHOMO_SUB_URL="https://你的机场订阅地址"
#   EOF
#   sudo chmod 600 /etc/mihomo-sub.conf
#
# 也可以临时用环境变量覆盖：MIHOMO_SUB_URL=... ./update_sub.sh
set -e

CONF_FILE=${MIHOMO_SUB_CONF:-/etc/mihomo-sub.conf}
if [ -f "$CONF_FILE" ]; then
    # shellcheck disable=SC1090
    . "$CONF_FILE"
fi
: "${MIHOMO_SUB_URL:?未设置 MIHOMO_SUB_URL，请写进 /etc/mihomo-sub.conf}"

MIHOMO_HOME=${MIHOMO_HOME:-/opt/mihomo}
PATCH=${MIHOMO_HOME}/patch_mihomo.py
TMP=${MIHOMO_HOME}/config.new.yaml

curl -s --max-time 60 -A "clash-verge/v2.5.10" -o "$TMP" "$MIHOMO_SUB_URL"
SIZE=$(stat -c%s "$TMP")
if [ "$SIZE" -lt 20000 ]; then
    echo "订阅下载异常，size=$SIZE，放弃更新"
    rm -f "$TMP"
    exit 1
fi

python3 "$PATCH" "$TMP"

if ! "$MIHOMO_HOME/mihomo" -t -d "$MIHOMO_HOME" -f "$TMP" >/tmp/mihomo_test.log 2>&1; then
    echo "新配置校验失败，保留旧配置"
    cat /tmp/mihomo_test.log
    rm -f "$TMP"
    exit 1
fi

cp "$MIHOMO_HOME/config.yaml" "$MIHOMO_HOME/config.yaml.bak.$(date +%Y%m%d)"
mv "$TMP" "$MIHOMO_HOME/config.yaml"
systemctl restart mihomo
sleep 8
curl -s -x "${MIHOMO_PROXY:-http://127.0.0.1:7890}" --max-time 40 -o /dev/null \
    -w "e-hentai after update: %{http_code}\n" https://e-hentai.org/
echo "订阅更新完成 $(date)"
