#!/usr/bin/env bash
set -euo pipefail

export PATH="/usr/local/go/bin:$PATH"

repo_root="/mnt/d/Business/Agents/RCAgent"
querier_root="/mnt/d/kylin/kylin-insights-server-mainv6-mcp/kylin-insights/server"
gateway_env="/etc/insights-mcp-gateway/gateway.env"
gateway_backup="/etc/insights-mcp-gateway/gateway.env.k8s"

mkdir -p /opt/kylin/rcagent-mcp-mock/bin /var/lib/rcagent-mcp-mock/evidence
(
  cd "$querier_root"
  go build -o /opt/kylin/rcagent-mcp-mock/bin/rcagent-mcp-mock \
    ./querier/cmd/fake-provider-querier
)

install -m 0644 \
  "$repo_root/local_testing/systemd/rcagent-mcp-mock.service" \
  /etc/systemd/system/rcagent-mcp-mock.service

if [[ -f "$gateway_env" && ! -f "$gateway_backup" ]]; then
  cp "$gateway_env" "$gateway_backup"
fi
if grep -q '^QUERIES_GRPC_ADDR=' "$gateway_env"; then
  sed -i 's|^QUERIES_GRPC_ADDR=.*$|QUERIES_GRPC_ADDR=127.0.0.1:19888|' "$gateway_env"
else
  printf '\nQUERIES_GRPC_ADDR=127.0.0.1:19888\n' >> "$gateway_env"
fi
chmod 0600 "$gateway_env"

systemctl daemon-reload
systemctl enable rcagent-mcp-mock.service
systemctl restart rcagent-mcp-mock.service
systemctl restart insights-mcp-gateway.service

systemctl --no-pager --full status rcagent-mcp-mock.service
systemctl --no-pager --full status insights-mcp-gateway.service
