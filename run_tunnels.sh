#!/usr/bin/env bash
# Expose local Ashwini (8080) and Ragini (8081) portals to the internet via Cloudflare Tunnels (100% Free, no card needed)

CLOUDFLARED="$HOME/.local/bin/cloudflared"

if ! command -v "$CLOUDFLARED" >/dev/null 2>&1; then
  echo "Error: cloudflared not found at $CLOUDFLARED"
  exit 1
fi

echo "Starting Cloudflare Tunnels..."

# Kill existing tunnel processes if any
pkill -f "cloudflared tunnel" 2>/dev/null || true
sleep 1

"$CLOUDFLARED" tunnel --url http://127.0.0.1:8080 > /tmp/tunnel_ashwini.log 2>&1 &
"$CLOUDFLARED" tunnel --url http://127.0.0.1:8081 > /tmp/tunnel_ragini.log 2>&1 &

echo "Waiting for public HTTPS URLs..."
for i in {1..12}; do
  ASHWINI_URL=$(grep -o 'https://[a-zA-Z0-9.-]*\.trycloudflare\.com' /tmp/tunnel_ashwini.log 2>/dev/null | head -1)
  RAGINI_URL=$(grep -o 'https://[a-zA-Z0-9.-]*\.trycloudflare\.com' /tmp/tunnel_ragini.log 2>/dev/null | head -1)
  if [ -n "$ASHWINI_URL" ] && [ -n "$RAGINI_URL" ]; then
    break
  fi
  sleep 1
done

echo "=================================================="
echo " Ashwini Portal : $ASHWINI_URL"
echo " Ragini Portal  : $RAGINI_URL"
echo "=================================================="
