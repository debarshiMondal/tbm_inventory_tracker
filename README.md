# TBM Inventory & Sales Tracker (FastAPI)

CSV-driven, lightweight POS + inventory web app built with **FastAPI**.  
Modules:
- Ready Products, Raw Materials, Purchases (Spend report)
- **Sales (POS)** with incremental Order IDs, discounts, payment status/mode
- Simple text bill endpoint (download/print as PDF from browser)
- Daily foldered data (`data/YYYY-MM-DD`) for easy archiving

---

## 1) Project Structure

```
.
├─ app.py
├─ conf/
│  ├─ config.txt          # config flags (see below)
│  └─ order_seq.txt       # auto-created: last order id
├─ data/                  # auto-created; holds dated folders with CSVs
├─ static/
│  ├─ main.css
│  └─ ui.js
├─ templates/
│  ├─ index.html
│  ├─ ready_products.html
│  ├─ raw_materials.html
│  ├─ sales.html
│  └─ import.html
├─ requirements.txt
└─ README.md
```

### CSV schemas
Defined in `HEADERS` inside `app.py`:
- `ready_products.csv`: `id,name,category,unit,unit_cost,price,quantity,threshold`
- `raw_inventory.csv`: `id,name,category,subcategory,unit,unit_cost,stock,threshold`
- `purchases.csv`: `id,date,category,subcategory,item,unit,qty,unit_cost,total_cost,notes`
- `sales.csv`:  
  `id,date,category,branch,order_id,item,unit,qty,unit_price,discount,total_price,customer_name,customer_phone,table_no,payment_status,payment_mode,payment_note,notes`
- `branches.csv`: `id,name,is_active`

---

## 2) Local Dev

**Prereqs:** Python 3.11+

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt

# run
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Open: `http://127.0.0.1:8000`

---

## 3) Config

`conf/config.txt` (auto-created if missing):

```ini
# Set to 1 to back up existing ./data to ./data_backup/before_full_invent_<ts>
# and start fresh once. A marker prevents repeat resets.
full_invent=0
```

- Order sequence persists in `conf/order_seq.txt`.
- Daily data lives in `./data/YYYY-MM-DD/`.

---

## 4) AWS EC2 (Free-tier friendly) Deployment

### A. Launch EC2
- **Region:** choose the one closest to your customers; free-tier works in any.
- **AMI:** *Amazon Linux 2023* (AL2023)
- **Instance type:** `t2.micro` or `t3.micro` (free-tier eligible)
- **Security Group:** allow TCP **80** (HTTP), **443** (HTTPS), and **22** (SSH) from your IP.
- **Key pair:** create/download `.pem`.

### B. Connect & install system packages
SSH:
```bash
ssh -i your-key.pem ec2-user@<EC2_PUBLIC_IP>
```

Packages (AL2023):
```bash
sudo dnf update -y
sudo dnf install -y git nginx python3.11 python3.11-pip
```

### C. Pull project & set up venv
```bash
cd /home/ec2-user
git clone https://github.com/<your-user>/<your-repo>.git tbm
cd tbm
python3.11 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

First run (sanity):
```bash
uvicorn app:app --host 0.0.0.0 --port 8000
# Ctrl+C to stop after verifying http://EC2_PUBLIC_IP:8000 works
```

### D. Systemd service (run FastAPI in background)
Create `/etc/systemd/system/tbm.service`:
```ini
[Unit]
Description=TBM FastAPI
After=network.target

[Service]
User=ec2-user
WorkingDirectory=/home/ec2-user/tbm
Environment="PATH=/home/ec2-user/tbm/venv/bin"
ExecStart=/home/ec2-user/tbm/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

Enable & start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tbm
sudo systemctl status tbm
```

### E. Nginx reverse proxy
Create `/etc/nginx/conf.d/tbm.conf`:
```nginx
server {
    listen 80;
    server_name calcuttafoodproduction.food;

    # Static files
    location /static/ {
        alias /home/ec2-user/tbm/static/;
        access_log off;
        expires 30d;
    }

    # App
    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host               $host;
        proxy_set_header   X-Real-IP          $remote_addr;
        proxy_set_header   X-Forwarded-For    $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto  $scheme;
    }
}
```

Test & reload:
```bash
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

### F. DNS
- In your DNS (Route53 or your registrar), create an **A record**:
  - Name: `calcuttafoodproduction.food`
  - Value: **EC2 public IPv4**
- Wait for propagation (usually a few minutes).

Visit: `http://calcuttafoodproduction.food`

### G. HTTPS (optional quick path with Certbot)
```bash
sudo dnf install -y python3-certbot-nginx
sudo certbot --nginx -d calcuttafoodproduction.food
# Accept, provide email, pick redirect to HTTPS
```
Auto-renewal is installed by Certbot.

> **AWS-native TLS (optional):** Use ACM + ALB/CloudFront if you prefer managed certificates in front of Nginx. For simplicity, Certbot on Nginx is fine.

---

## 5) Operations

### Update app
```bash
cd /home/ec2-user/tbm
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart tbm
```

### Logs
```bash
journalctl -u tbm -f
sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log
```

### Data backup
```bash
tar -czf tbm-data-$(date +%F).tar.gz data conf
```

---

## 6) API Highlights

- **Ready Products**: `GET/POST/PUT/DELETE /api/ready_products...`
- **Raw Inventory**: `GET/POST/PUT/DELETE /api/raw_inventory...`
- **Purchases (Spend report)**: `POST /api/purchases`, `GET /api/spend`
- **Branches (SFH)**: `GET /api/branches`, `POST /api/branches`
- **Sales (POS)**:
  - `GET /api/sales/next_order`
  - `POST /api/sales` (creates sale, decrements stock, returns bill URL)
  - `GET /api/sales` (list)
  - `GET /api/sales/{sale_id}/bill` (plain-text bill to print as PDF)

---

## 7) Troubleshooting

- **`python3-venv` not found** on AL2023 → use `python3.11`/`python3.11-pip`.
- **502/404 via Nginx** → check `tbm` service is running on `127.0.0.1:8000`, and paths in `tbm.conf`.
- **Static 404** → ensure `/static/` alias path matches your repo location.
- **Order IDs** reset? They persist in `conf/order_seq.txt`. Don’t delete it unless you intend to reset (you can reseed it manually).

---

## 8) License
Private/internal. Use at your own discretion.
