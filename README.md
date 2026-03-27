# Vector Monitor

A lightweight web UI for viewing logs from Vector services running across your EC2 instances.

## What it does

- Lists all `vector-*` systemd services on every EC2 host (dev + prod environments)
- Shows service status (running / failed / inactive) with colour-coded indicators
- Renders `journalctl` output with ERROR / WARN / INFO / DEBUG highlighting
- Live-tail via Server-Sent Events (`journalctl -f`)
- Filter services by name, choose log depth (100–1 000 lines), filter by log level
- One-click service restart

## Prerequisites

- Python 3.11+
- SSH access to the EC2 hosts (key-based, no passwords)
- The `ec2-user` (or configured user) must be able to run `sudo journalctl` and `sudo systemctl` without a password prompt

## Quick start

```bash
cd /Users/jm237@apac.comcast.com/Desktop/sky/vector-monitor

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

# Edit config.yaml and set the correct ssh.key_file path
python app.py
```

The UI is then available at **http://localhost:8080**

## Configuration (`config.yaml`)

| Key | Purpose |
|---|---|
| `environments.*.hosts[].ip` | EC2 private IP |
| `environments.*.hosts[].ssh_user` | SSH user (default `ec2-user`) |
| `environments.*.hosts[].key_file` | Per-host override for SSH key |
| `ssh.key_file` | Global SSH private key path (supports `~/`) |
| `ssh.port` | SSH port (default `22`) |

## Access from your laptop

The EC2s are on private IPs (`10.x.x.x`). You need either:

1. **VPN** — if a VPN gives you direct access to the VPC
2. **SSH tunnel / bastion** — `ssh -L 2222:10.53.172.9:22 user@bastion` then set the host `ip` to `127.0.0.1` and `port` to `2222`
3. **AWS SSM** — uncomment the `aws_ssm` connection in the Ansible inventory

## Project layout

```
vector-monitor/
  app.py          ← FastAPI backend (SSH + API endpoints)
  config.yaml     ← Host inventory (edit this)
  requirements.txt
  static/
    index.html    ← Single-page UI
```
