# Deploy to Google Compute Engine e2-micro Free Tier

## Prerequisites
- GCP account with billing enabled (e2-micro is free-tier eligible).
- `gcloud` CLI installed and authenticated (`gcloud auth login`).
- A GCP project with Compute Engine API enabled.
- SSH key pair (default ~/.ssh/id_rsa or specify).

## One-time setup
1. Create a firewall rule allowing HTTP/HTTPS traffic.
2. Reserve a static external IP (optional but recommended).
3. Create the VM:
   - Machine type: e2-micro
   - Boot disk: Debian 12 or Ubuntu 22.04 LTS
   - Allow HTTP/HTTPS traffic: Yes
   - Set up SSH key

## Deploy commands (run from repo root)
1. Copy repo to VM:
   gcloud compute scp --recurse . <user>@<VM_IP>:~/jobhunter
2. SSH in:
   gcloud compute ssh <user>@<VM_IP>
3. On VM, run:
   - sudo apt update && sudo apt install -y python3 python3-venv python3-pip nginx git
   - sudo mkdir -p /opt/jobhunter && cp -r ~/jobhunter/* /opt/jobhunter/
   - cd /opt/jobhunter && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   - Create .env with OPENROUTER_API_KEY=... and JOBHUNTER_DB=/opt/jobhunter/data/jobhunter.sqlite3
   - mkdir -p data/outputs
   - Copy jobhunter.service to /etc/systemd/system/ and run:
     - sudo systemctl daemon-reload
     - sudo systemctl enable --now jobhunter
4. Configure Nginx reverse proxy:
   - Copy nginx.conf to /etc/nginx/sites-available/jobhunter
   - sudo ln -s /etc/nginx/sites-available/jobhunter /etc/nginx/sites-enabled/
   - sudo nginx -t && sudo systemctl reload nginx
5. Verify:
   - curl http://127.0.0.1:8080/api/health
   - sudo systemctl status jobhunter
   - Open dashboard via VM external IP or custom domain.

## Notes
- Keep exactly one app worker; do not scale horizontally.
- Back up /opt/jobhunter/data regularly.
- Use a custom domain + Let's Encrypt for HTTPS.

## Updating a running deployment

```bash
# from the repo root, on your laptop
tar --exclude='.venv' --exclude='.git' --exclude='.env' --exclude='data' \
    --exclude='__pycache__' --exclude='*.pyc' \
    -czf /tmp/update.tar.gz jobhunter requirements.txt README.md config deploy
gcloud compute scp /tmp/update.tar.gz jobhunter-vm:~/update.tar.gz --zone us-central1-a
gcloud compute ssh jobhunter-vm --zone us-central1-a --command '
  cd /opt/jobhunter
  sudo tar -xzf ~/update.tar.gz -C /opt/jobhunter
  sudo chown -R sbrahman:sbrahman /opt/jobhunter/jobhunter /opt/jobhunter/config
  .venv/bin/pip install -q -r requirements.txt
  sudo systemctl restart jobhunter'
```

`data/` and `.env` are excluded so the job database and API key on the VM are preserved.

## PDF export on the VM

The e2-micro has 1GB RAM, so Chromium is not installed. PDF export uses **WeasyPrint**,
which needs system libraries:

```bash
sudo apt-get install -y libpango-1.0-0 libpangocairo-1.0-0 libcairo2 \
    libgdk-pixbuf2.0-0 libffi-dev shared-mime-info fonts-dejavu-core
/opt/jobhunter/.venv/bin/pip install weasyprint pypdf
```

Verify with `/opt/jobhunter/.venv/bin/python -m jobhunter.cli doctor --no-live | grep pdf`.
A render takes ~2s and adds ~50MB peak RSS. If PDF export is ever unwanted, uninstall
WeasyPrint — the pipeline just skips the `.pdf` artifact.

## Free-tier guardrails (re-check after any change)

```bash
gcloud compute instances list        # must stay e2-micro in us-central1
gcloud compute disks list            # must stay pd-standard, <= 30GB
gcloud compute addresses list        # must be EMPTY (reserved IPs are billed)
gcloud compute forwarding-rules list # must be EMPTY (no load balancer)
gcloud compute routers list          # must be EMPTY (no Cloud NAT)
```

The external IP is **ephemeral**. If the VM is ever stopped and restarted, the IP changes
and DuckDNS must be re-pointed:

```bash
IP=$(gcloud compute instances describe jobhunter-vm --zone us-central1-a \
     --format='get(networkInterfaces[0].accessConfigs[0].natIP)')
curl "https://www.duckdns.org/update?domains=jobhunter-ashwini&token=<TOKEN>&ip=$IP"
```
