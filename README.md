# LIS Daemon — ASTM E1394 Result Receiver for Odoo HMS

Standalone service that receives lab test results from **Mindray machines** (BS-240Pro, BC-series) via ASTM E1394 over TCP/IP, and auto-links them to Odoo lab request critearea.

## Architecture

```
Mindray Machine ──TCP/ASTM──> LIS Daemon ──JSON-RPC──> Odoo (dnd_lab_lis_connector)
     (port 5001/5002)            (Docker)              (action_receive_result)
```

## Quick Start

### 1. Configure Environment

```bash
cp .env.example .env
# Edit .env with your Odoo URL, DB, and API key
```

### 2. Review ASTM Config

```bash
# Adjust field positions if needed (usually not required for Mindray)
vim config/daemon.yml
```

### 3. Run

```bash
docker compose up -d
docker compose logs -f lis-daemon
```

### 4. Add More Machines

1. Create `dnd.lis.machine` record in Odoo (Settings > Laboratory > LIS Integration > Machines)
2. Fill test code mappings
3. Add port to `.env`: `LIS_PORT_3=5003`
4. Add port to `docker-compose.yml`: `'${LIS_PORT_3}:${LIS_PORT_3}'`
5. `docker compose up -d --force-recreate`
6. Open firewall port

## Running Without Docker

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env
export $(cat .env | xargs)
python -m src.main
```

## Troubleshooting

| Issue | Check |
|---|---|
| `Authentication failed` | Verify ODOO_API_KEY or ODOO_PASSWORD in .env |
| `No active machines found` | Create dnd.lis.machine records in Odoo and set active=True |
| Results showing as `unmatched` | Check dnd.lis.machine.test.map — parameter_name must match exactly |
| Connection timeout | Verify machine IP and port, check firewall rules |

## File Structure

```
service_lis_daemon/
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── requirements.txt
├── config/
│   └── daemon.yml          # ASTM field positions + timeouts
├── src/
│   ├── __init__.py
│   ├── main.py             # Entry point
│   ├── lis_daemon.py        # Async TCP server
│   ├── astm_parser.py       # ASTM E1394 parser
│   └── odoo_client.py       # JSON-RPC client
└── README.md
```
