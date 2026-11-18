#!/usr/bin/env bash
set -euo pipefail

gateway_env="/etc/insights-mcp-gateway/gateway.env"
gateway_backup="/etc/insights-mcp-gateway/gateway.env.k8s"

if [[ ! -f "$gateway_backup" ]]; then
  echo "没有找到 $gateway_backup，无法恢复原来的 K8s 路由。" >&2
  exit 1
fi

install -m 0600 "$gateway_backup" "$gateway_env"
systemctl restart insights-mcp-gateway.service
systemctl --no-pager --full status insights-mcp-gateway.service
