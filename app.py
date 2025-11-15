from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import date, datetime, timedelta
from pathlib import Path
import shutil, csv, uuid, os, io

APP_ROOT = Path(__file__).parent
DATA_ROOT = APP_ROOT / "data"
TEMPLATES = APP_ROOT / "templates"
STATIC_DIR = APP_ROOT / "static"
CONF_DIR = APP_ROOT / "conf"
CONFIG_FILE = CONF_DIR / "config.txt"
ORDER_SEQ_FILE = CONF_DIR / "order_seq.txt"

CATEGORIES = ["Home Delivery", "Frozen Products", "SFH"]
UNITS = ["KG", "GM", "Pieces", "Batch", "Plates", "Portion"]
SUBCATEGORIES = [
    "Infrastructure", "Meat and Fish", "Veggies", "Grocery", "Dairy", "Bakery",
    "Kitchen Tool", "Fuel", "Serving Dish", "Operating Supplies", "Packaging",
]

PAYMENT_STATUSES = ["Live", "Due", "Paid"]
PAYMENT_MODES = ["CurrentUPI", "Cash", "Card", "PersonalUPI", "PersonalCash"]

HEADERS = {
    # Added item_category + code so codes persist in CSV
    "ready_products": [
        "id", "name", "category", "item_category", "code",
        "unit", "unit_cost", "price", "quantity", "threshold"
    ],
    "raw_inventory":  [
        "id", "name", "category", "subcategory", "unit",
        "unit_cost", "stock", "threshold"
    ],
    "purchases":      [
        "id", "date", "category", "subcategory", "item",
        "unit", "qty", "unit_cost", "total_cost", "notes"
    ],
    # Extended sales schema for POS
    "sales": [
        "id","date","category","branch","order_id","item","unit","qty","unit_price",
        "discount","total_price","customer_name","customer_phone","table_no",
        "payment_status","payment_mode","payment_note","notes"
    ],
    "branches": ["id","name","is_active"],
}


# ---- Config ----

def load_config() -> Dict:
    cfg = {"full_invent": False}
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == "full_invent" and v.strip() == "1":
                        cfg["full_invent"] = True
    return cfg


CONFIG = load_config()
FULL_INVENT = bool(CONFIG.get("full_invent", False))


def today_str() -> str:
    return date.today().isoformat()


def _init_csvs(dirpath: Path):
    dirpath.mkdir(parents=True, exist_ok=True)
    for key, headers in HEADERS.items():
        f = dirpath / f"{key}.csv"
        if not f.exists():
            with f.open("w", newline="", encoding="utf-8") as fp:
                csv.writer(fp).writerow(headers)


def _latest_existing_day_dir():
    if not DATA_ROOT.exists():
        return None
    dirs = sorted([p for p in DATA_ROOT.iterdir() if p.is_dir()])
    return dirs[-1] if dirs else None


def _maybe_reset_for_full_invent():
    """
    If FULL_INVENT is enabled, backup existing DATA_ROOT once
    and start fresh so user can re-enter everything.
    """
    if not FULL_INVENT:
        return

    marker = DATA_ROOT / ".full_invent_migrated"
    if marker.exists():
        return  # already done

    if DATA_ROOT.exists() and any(DATA_ROOT.iterdir()):
        backup_root = APP_ROOT / "data_backup"
        backup_root.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = backup_root / f"before_full_invent_{ts}"
        shutil.move(str(DATA_ROOT), str(dest))

    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    marker.write_text("1", encoding="utf-8")


_maybe_reset_for_full_invent()


def get_today_dir() -> Path:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    today_dir = DATA_ROOT / today_str()
    if today_dir.exists():
        _init_csvs(today_dir)
        return today_dir

    latest = _latest_existing_day_dir()
    if latest and latest.name != today_str():
        shutil.copytree(latest, today_dir)
    _init_csvs(today_dir)
    return today_dir


def _csv_path(name: str) -> Path:
    return get_today_dir() / f"{name}.csv"


def read_csv(name: str) -> List[Dict]:
    path = _csv_path(name)
    rows: List[Dict] = []
    with path.open("r", newline="", encoding="utf-8") as fp:
        for row in csv.DictReader(fp):
            rows.append(row)
    return rows


def write_csv(name: str, rows: List[Dict]):
    path = _csv_path(name)
    headers = HEADERS[name]
    with path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=headers)
        w.writeheader()
        for row in rows:
            for h in headers:
                row.setdefault(h, "")
            w.writerow({k: row.get(k, "") for k in headers})


def append_row(name: str, row: Dict):
    rows = read_csv(name)
    rows.append(row)
    write_csv(name, rows)


def gen_id() -> str:
    return uuid.uuid4().hex[:12]


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def convert_qty(qty: float, from_unit: str, to_unit: str) -> float:
    if from_unit == to_unit:
        return qty
    if from_unit == "KG" and to_unit == "GM":
        return qty * 1000.0
    if from_unit == "GM" and to_unit == "KG":
        return qty / 1000.0
    raise ValueError(f"Unit mismatch: cannot convert {from_unit} -> {to_unit}")


# ---- Order sequence ----

def _scan_max_order_id() -> int:
    """Best-effort: read today's sales and return max order_id seen."""
    try:
        rows = read_csv("sales")
        m = 0
        for r in rows:
            try:
                m = max(m, int(r.get("order_id") or 0))
            except Exception:
                pass
        return m
    except Exception:
        return 0


def next_order_id(peek: bool = False) -> int:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    if not ORDER_SEQ_FILE.exists():
        # initialize from existing data
        ORDER_SEQ_FILE.write_text(str(_scan_max_order_id()), encoding="utf-8")
    current = int(ORDER_SEQ_FILE.read_text(encoding="utf-8") or "0")
    nxt = current + 1
    if not peek:
        ORDER_SEQ_FILE.write_text(str(nxt), encoding="utf-8")
    return nxt


# ---- App & static ----

app = FastAPI(title="TBM Inventory and Sells Tracker", version="0.6")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def page(path: str) -> HTMLResponse:
    with (TEMPLATES / path).open("r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/", response_class=HTMLResponse)
def home_page():
    return page("index.html")


@app.get("/ready-products", response_class=HTMLResponse)
def ready_products_page():
    return page("ready_products.html")


@app.get("/raw-materials", response_class=HTMLResponse)
def raw_materials_page():
    return page("raw_materials.html")


@app.get("/sales", response_class=HTMLResponse)
def sales_page():
    return page("sales.html")


@app.get("/import", response_class=HTMLResponse)
def import_page():
    return page("import.html")


@app.get("/api/config")
def get_config():
    return {"full_invent": FULL_INVENT}


# ---- Models ----

class ReadyProductIn(BaseModel):
    name: str
    category: str
    unit: str
    unit_cost: float = 0.0
    price: float = 0.0
    quantity: float = 0.0
    threshold: float = 0.0
    # optional new fields
    item_category: Optional[str] = ""
    code: Optional[str] = ""


class ReadyProductUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    unit: Optional[str] = None
    unit_cost: Optional[float] = None
    price: Optional[float] = None
    quantity: Optional[float] = None
    threshold: Optional[float] = None
    item_category: Optional[str] = None
    code: Optional[str] = None


class RawItemIn(BaseModel):
    name: str
    category: str
    subcategory: str
    unit: str
    unit_cost: float = 0.0
    stock: float = 0.0
    threshold: float = 0.0


class RawItemUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    unit: Optional[str] = None
    unit_cost: Optional[float] = None
    stock: Optional[float] = None
    threshold: Optional[float] = None


class PurchaseIn(BaseModel):
    date: Optional[str] = None
    category: str
    subcategory: str
    item: str
    unit: str
    qty: float
    unit_cost: float
    notes: Optional[str] = ""


class PurchaseResp(BaseModel):
    ok: bool
    id: str
    new_stock: float
    unit: str


class SaleIn(BaseModel):
    date: Optional[str] = None
    category: str
    branch: Optional[str] = ""
    order_id: Optional[int] = None
    item: str
    unit: str
    qty: float
    unit_price: Optional[float] = None
    discount: float = 0.0
    customer_name: Optional[str] = ""
    customer_phone: Optional[str] = ""
    table_no: Optional[str] = ""
    payment_status: str = "Live"      # Live, Due, Paid
    payment_mode: Optional[str] = ""  # only when Paid
    payment_note: Optional[str] = ""  # personal upi/cash owner name
    notes: Optional[str] = ""


class BranchIn(BaseModel):
    name: str
    is_active: bool = True


class SalePaymentPatch(BaseModel):
    id: str
    payment_status: Optional[str] = None
    payment_mode: Optional[str] = None


def _ensure_unit(u: str):
    if u not in UNITS:
        raise HTTPException(400, f"unit must be one of {UNITS}")


def _ensure_subcategory(s: str):
    if s not in SUBCATEGORIES:
        raise HTTPException(400, f"subcategory must be one of {SUBCATEGORIES}")


# ---- Ready Products ----

def _generate_ready_code(name: str, item_category: str, rows: List[Dict]) -> str:
    """
    Generate a 3-character code: <digit><letter><letter>, e.g. 1CM / 5CB.
    - 2nd char = first letter of item_category (fallback to name)
    - 3rd char = first letter of item name
    - 1st char = 1-9 chosen to avoid duplicates for that pair of letters
    """
    base_cat = (item_category or "").strip()
    base_name = (name or "").strip()

    if not base_cat:
        base_cat = base_name

    l2 = (base_cat[:1] or "X").upper()
    l3 = (base_name[:1] or "X").upper()
    suffix = l2 + l3

    existing = {(r.get("code") or "").upper() for r in rows}

    # Try 1–9 for that suffix (most normal case)
    for d in range(1, 10):
        code = f"{d}{suffix}"
        if code not in existing:
            return code

    # Fallback: brute force some other letter combos if things are crazy
    idx = 0
    while True:
        for d in range(1, 10):
            extra = chr(ord("A") + (idx % 26))
            code = f"{d}{l2}{extra}"
            if code not in existing:
                return code
        idx += 1


@app.get("/api/ready_products")
def list_ready_products():
    rows = read_csv("ready_products")
    for r in rows:
        # numeric fields as float for UI
        for k in ("unit_cost", "price", "quantity", "threshold"):
            r[k] = float(r.get(k) or 0)

        # keep new fields always present so frontend can rely on them
        r.setdefault("item_category", "")
        r.setdefault("code", "")
        # expose code also as item_code for the UI
        r["item_code"] = r.get("code", "") or ""
    return {"rows": rows}


@app.post("/api/ready_products")
def add_ready_product_api(item: ReadyProductIn):
    if item.category not in CATEGORIES:
        raise HTTPException(400, f"category must be one of {CATEGORIES}")
    _ensure_unit(item.unit)

    rows = read_csv("ready_products")

    # These two come from the updated model (or default)
    item_category = (item.item_category or "").strip()
    incoming_code = (item.code or "").strip().upper()

    # Validate / generate code (3 chars: 1 digit + 2 letters)
    if incoming_code:
        if len(incoming_code) != 3 or not (
            incoming_code[0].isdigit() and incoming_code[1:].isalpha()
        ):
            raise HTTPException(
                400, "code must be exactly 3 chars: 1 digit + 2 letters (e.g. 1CM, 5CB)"
            )
        # uniqueness check
        for r in rows:
            if (r.get("code") or "").upper() == incoming_code:
                raise HTTPException(400, f"code '{incoming_code}' already exists")
        code = incoming_code
    else:
        code = _generate_ready_code(item.name, item_category, rows)

    row = {
        "id": gen_id(),
        "name": item.name.strip(),
        "category": item.category,
        "item_category": item_category,
        "code": code,
        "unit": item.unit,
        "unit_cost": f"{item.unit_cost:.2f}",
        "price": f"{item.price:.2f}",
        "quantity": f"{item.quantity:.3f}",
        "threshold": f"{item.threshold:.3f}",
    }
    append_row("ready_products", row)
    return {"ok": True, "id": row["id"], "code": code}


@app.put("/api/ready_products/{item_id}")
def update_ready_product_api(item_id: str, patch: ReadyProductUpdate):
    rows = read_csv("ready_products")
    data = patch.model_dump(exclude_none=True)

    # validations
    if "category" in data and data["category"] not in CATEGORIES:
        raise HTTPException(400, f"category must be one of {CATEGORIES}")
    if "unit" in data:
        _ensure_unit(data["unit"])

    # Normalise / validate code if being changed
    if "code" in data:
        raw = (data["code"] or "").strip().upper()
        if raw:
            if len(raw) != 3 or not (raw[0].isdigit() and raw[1:].isalpha()):
                raise HTTPException(
                    400, "code must be exactly 3 chars: 1 digit + 2 letters (e.g. 1CM, 5CB)"
                )
            for r in rows:
                if r["id"] != item_id and (r.get("code") or "").upper() == raw:
                    raise HTTPException(400, f"code '{raw}' already exists")
            data["code"] = raw
        else:
            # allow clearing code
            data["code"] = ""

    found = False
    for r in rows:
        if r["id"] == item_id:
            found = True
            # make sure new fields exist even on very old rows
            r.setdefault("item_category", "")
            r.setdefault("code", "")
            for k, v in data.items():
                r[k] = str(v)
            break

    if not found:
        raise HTTPException(404, "Not found")

    write_csv("ready_products", rows)
    return {"ok": True}


@app.delete("/api/ready_products/{item_id}")
def delete_ready_product_api(item_id: str):
    rows = read_csv("ready_products")
    new = [r for r in rows if r["id"] != item_id]
    if len(new) == len(rows):
        raise HTTPException(404, "Not found")
    write_csv("ready_products", new)
    return {"ok": True}


@app.post("/api/ready_products/{item_id}/adjust_stock")
def adjust_ready_stock_api(item_id: str, delta: float = Form(...)):
    rows = read_csv("ready_products")
    for r in rows:
        if r["id"] == item_id:
            q = float(r.get("quantity") or 0) + float(delta)
            if q < 0:
                raise HTTPException(400, "Resulting stock would be negative")
            r["quantity"] = f"{q:.3f}"
            write_csv("ready_products", rows)
            return {"ok": True, "quantity": q}
    raise HTTPException(404, "Not found")


# ---- Raw Inventory ----

@app.get("/api/raw_inventory")
def list_raw_inventory_api():
    rows = read_csv("raw_inventory")
    for r in rows:
        r["unit_cost"] = float(r.get("unit_cost") or 0)
        r["stock"] = float(r.get("stock") or 0)
        r["threshold"] = float(r.get("threshold") or 0)
    return {"rows": rows}


@app.post("/api/raw_inventory")
def add_raw_item_api(item: RawItemIn):
    if item.category not in CATEGORIES:
        raise HTTPException(400, f"category must be one of {CATEGORIES}")
    _ensure_subcategory(item.subcategory)
    _ensure_unit(item.unit)
    row = {
        "id": gen_id(),
        "name": item.name.strip(),
        "category": item.category,
        "subcategory": item.subcategory,
        "unit": item.unit,
        "unit_cost": f"{item.unit_cost:.2f}",
        "stock": f"{item.stock:.3f}",
        "threshold": f"{item.threshold:.3f}",
    }
    append_row("raw_inventory", row)
    return {"ok": True, "id": row["id"]}


@app.put("/api/raw_inventory/{item_id}")
def update_raw_item_api(item_id: str, patch: RawItemUpdate):
    rows = read_csv("raw_inventory")
    found = False
    data = patch.model_dump(exclude_none=True)

    if "category" in data and data["category"] not in CATEGORIES:
        raise HTTPException(400, f"category must be one of {CATEGORIES}")
    if "subcategory" in data:
        _ensure_subcategory(data["subcategory"])
    if "unit" in data:
        _ensure_unit(data["unit"])

    for r in rows:
        if r["id"] == item_id:
            found = True
            for k, v in data.items():
                r[k] = str(v)
            break

    if not found:
        raise HTTPException(404, "Not found")

    write_csv("raw_inventory", rows)
    return {"ok": True}


@app.delete("/api/raw_inventory/{item_id}")
def delete_raw_item_api(item_id: str):
    rows = read_csv("raw_inventory")
    new = [r for r in rows if r["id"] != item_id]
    if len(new) == len(rows):
        raise HTTPException(404, "Not found")
    write_csv("raw_inventory", new)
    return {"ok": True}


# ---- Branches (for SFH) ----

@app.get("/api/branches")
def list_branches():
    rows = read_csv("branches")
    return {"rows": rows}


@app.post("/api/branches")
def add_branch(b: BranchIn):
    rows = read_csv("branches")
    # prevent dup by name (case-insensitive)
    for r in rows:
        if r["name"].strip().lower() == b.name.strip().lower():
            return {"ok": True, "id": r["id"]}
    row = {"id": gen_id(), "name": b.name.strip(), "is_active": "1" if b.is_active else "0"}
    append_row("branches", row)
    return {"ok": True, "id": row["id"]}


@app.get("/api/branch_tables")
def branch_table_summary(branch: str, status: str = "Live"):
    """Simple live table view: count open orders per table for a branch."""
    rows = read_csv("sales")
    out: Dict[str, int] = {}
    for r in rows:
        if (r.get("branch") or "") == branch and (r.get("payment_status") or "") == status:
            table_no = r.get("table_no") or "—"
            out[table_no] = out.get(table_no, 0) + 1
    items = [{"table_no": k, "open_orders": v} for k, v in sorted(out.items())]
    return {"rows": items}


# ---- Purchases ----

@app.post("/api/purchases", response_model=PurchaseResp)
def add_purchase_api(p: PurchaseIn):
    if p.category not in CATEGORIES:
        raise HTTPException(400, f"category must be one of {CATEGORIES}")
    _ensure_subcategory(p.subcategory)
    _ensure_unit(p.unit)

    d = p.date or today_str()
    inv = read_csv("raw_inventory")
    target = None

    for r in inv:
        if (
            r["name"].strip().lower() == p.item.strip().lower()
            and r.get("category") == p.category
            and r.get("subcategory") == p.subcategory
        ):
            target = r
            break

    if not target:
        target = {
            "id": gen_id(),
            "name": p.item.strip(),
            "category": p.category,
            "subcategory": p.subcategory,
            "unit": p.unit,
            "unit_cost": f"{p.unit_cost:.2f}",
            "stock": "0",
            "threshold": "0",
        }
        inv.append(target)

    try:
        add_qty = float(p.qty)
        if target["unit"] != p.unit:
            add_qty = convert_qty(add_qty, p.unit, target["unit"])
        stock = float(target.get("stock") or 0) + add_qty
    except ValueError as e:
        raise HTTPException(400, str(e))

    target["stock"] = f"{stock:.3f}"
    target["unit_cost"] = f"{p.unit_cost:.2f}"
    write_csv("raw_inventory", inv)

    total = float(p.qty) * float(p.unit_cost)
    prow = {
        "id": gen_id(),
        "date": d,
        "category": p.category,
        "subcategory": p.subcategory,
        "item": p.item.strip(),
        "unit": p.unit,
        "qty": f"{p.qty:.3f}",
        "unit_cost": f"{p.unit_cost:.2f}",
        "total_cost": f"{total:.2f}",
        "notes": p.notes or "",
    }
    append_row("purchases", prow)

    return PurchaseResp(ok=True, id=prow["id"], new_stock=stock, unit=target["unit"])


@app.delete("/api/purchases/{purchase_id}")
def delete_purchase_api(purchase_id: str):
    rows = read_csv("purchases")
    new = [r for r in rows if r["id"] != purchase_id]
    if len(new) == len(rows):
        raise HTTPException(404, "Not found")
    write_csv("purchases", new)
    return {"ok": True}


# ---- Spend report (purchases) ----

def _date_range(period: str, start: Optional[str], end: Optional[str]):
    today = date.today()
    if period == "today":
        return today, today
    if period == "week":
        return today - timedelta(days=6), today
    if period == "month":
        return today.replace(day=1), today
    if period == "last30":
        return today - timedelta(days=29), today
    if period == "last90":
        return today - timedelta(days=89), today
    if period == "last180":
        return today - timedelta(days=179), today
    if period == "daterange" and start and end:
        return parse_date(start), parse_date(end)
    return today - timedelta(days=29), today


@app.get("/api/spend")
def get_spend(
    period: str = "last30",
    category: Optional[str] = None,
    item: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    s, e = _date_range(period, start, end)
    rows = read_csv("purchases")

    total = 0.0
    by_cat: Dict[str, float] = {}
    by_item: Dict[str, float] = {}
    filtered = []

    for r in rows:
        try:
            d = parse_date(r["date"])
        except Exception:
            continue

        if d < s or d > e:
            continue
        if category and r["category"] != category:
            continue
        if item and r["item"].strip().lower() != item.strip().lower():
            continue

        cost = float(r.get("total_cost") or 0)
        total += cost
        by_cat[r["category"]] = by_cat.get(r["category"], 0.0) + cost
        key_item = r["item"].strip()
        by_item[key_item] = by_item.get(key_item, 0.0) + cost
        filtered.append(r)

    return {
        "period": {"start": str(s), "end": str(e)},
        "total_spend": round(total, 2),
        "by_category": {k: round(v, 2) for k, v in by_cat.items()},
        "by_item": {k: round(v, 2) for k, v in by_item.items()},
        "rows": filtered,
    }


# ---- Alerts ----

@app.get("/api/alerts/low/ready")
def low_ready():
    rows = read_csv("ready_products")
    low = []
    for r in rows:
        q = float(r.get("quantity") or 0)
        t = float(r.get("threshold") or 0)
        if t > 0 and q <= t:
            low.append(r)
    return {"rows": low}


@app.get("/api/alerts/low/raw")
def low_raw():
    rows = read_csv("raw_inventory")
    low = []
    for r in rows:
        q = float(r.get("stock") or 0)
        t = float(r.get("threshold") or 0)
        if t > 0 and q <= t:
            low.append(r)
    return {"rows": low}


# ---- Sales (POS) ----

@app.get("/api/sales")
def list_sales():
    return {"rows": read_csv("sales")}


@app.get("/api/sales/next_order")
def peek_next_order():
    return {"next_order_id": next_order_id(peek=True)}


@app.post("/api/sales")
def add_sale_api(s: SaleIn):
    if s.category not in CATEGORIES:
        raise HTTPException(400, f"category must be one of {CATEGORIES}")

    # Load ready product and validate unit
    ready = read_csv("ready_products")
    target = None
    for r in ready:
        if r["name"].strip().lower() == s.item.strip().lower() and r.get("category") == s.category:
            target = r
            break
    if not target:
        raise HTTPException(400, "Ready product not found (match by name & category)")

    prod_unit = target.get("unit") or ""
    if s.unit != prod_unit:
        raise HTTPException(400, f"Sale unit '{s.unit}' must match product unit '{prod_unit}'")

    # price & totals
    price = s.unit_price if s.unit_price is not None else float(target.get("price") or 0.0)
    qty = float(s.qty)
    discount = float(s.discount or 0.0)
    if discount < 0:
        discount = 0.0

    # stock check and deduct
    d = s.date or today_str()
    current = float(target.get("quantity") or 0)
    if current < qty:
        raise HTTPException(400, f"Not enough stock. Available: {current}")
    target["quantity"] = f"{(current - qty):.3f}"
    write_csv("ready_products", ready)

    # Order id
    oid = s.order_id if s.order_id is not None else next_order_id()

    subtotal = qty * float(price or 0.0)
    total = max(0.0, subtotal - discount)

    row = {
        "id": gen_id(),
        "date": d,
        "category": s.category,
        "branch": s.branch or "",
        "order_id": str(oid),
        "item": target["name"],
        "unit": s.unit,
        "qty": f"{qty:.3f}",
        "unit_price": f"{float(price):.2f}",
        "discount": f"{discount:.2f}",
        "total_price": f"{total:.2f}",
        "customer_name": s.customer_name or "",
        "customer_phone": s.customer_phone or "",
        "table_no": s.table_no or "",
        "payment_status": s.payment_status or "Live",
        "payment_mode": s.payment_mode or "",
        "payment_note": s.payment_note or "",
        "notes": s.notes or "",
    }
    append_row("sales", row)

    # simple text bill (download). PDF/SMS sending would need external services.
    return {
        "ok": True,
        "id": row["id"],
        "order_id": oid,
        "remaining_stock": float(target["quantity"]),
        "bill_url": f"/api/sales/{row['id']}/bill"
    }


@app.get("/api/sales/{sale_id}/bill")
def get_bill(sale_id: str):
    """Return a simple text bill; browser can save/print as PDF."""
    rows = read_csv("sales")
    sale = next((r for r in rows if r["id"] == sale_id), None)
    if not sale:
        raise HTTPException(404, "Sale not found")
    lines = []
    lines.append("TBM - Bill")
    lines.append(f"Date: {sale['date']}    Order: #{sale['order_id']}")
    if sale.get("category"):
        lines.append(f"Category: {sale['category']}")
    if sale.get("branch"):
        lines.append(f"Branch: {sale['branch']}")
    if sale.get("table_no"):
        lines.append(f"Table: {sale['table_no']}")
    lines.append("-"*40)
    lines.append(f"Item: {sale['item']}  ({sale['unit']})")
    lines.append(f"Qty: {sale['qty']}  Unit Price: ₹{sale['unit_price']}")
    lines.append(f"Discount: ₹{sale['discount']}")
    lines.append(f"Total: ₹{sale['total_price']}")
    lines.append("-"*40)
    if sale.get("customer_name") or sale.get("customer_phone"):
        lines.append(f"Customer: {sale.get('customer_name','')}  {sale.get('customer_phone','')}")
    lines.append(f"Payment: {sale.get('payment_status','')} {sale.get('payment_mode','')}")
    if sale.get("payment_note"):
        lines.append(f"Note: {sale['payment_note']}")
    if sale.get("notes"):
        lines.append(f"Remarks: {sale['notes']}")
    content = "\n".join(lines)
    return PlainTextResponse(content, media_type="text/plain")


@app.post("/api/sales/update_payment")
def update_sale_payment(p: SalePaymentPatch):
    """
    Update payment_status and/or payment_mode for a sale.
    Used by:
      - Desktop Detailed Records inline dropdowns
      - Mobile settle buttons
    """
    rows = read_csv("sales")
    sale = None
    for r in rows:
        if r.get("id") == p.id:
            sale = r
            break

    if not sale:
        raise HTTPException(404, "Sale not found")

    if p.payment_status is not None:
        if p.payment_status not in PAYMENT_STATUSES:
            raise HTTPException(400, f"payment_status must be one of {PAYMENT_STATUSES}")
        sale["payment_status"] = p.payment_status

    if p.payment_mode is not None:
        mode = p.payment_mode or ""
        if mode and mode not in PAYMENT_MODES:
            raise HTTPException(400, f"payment_mode must be one of {PAYMENT_MODES + ['(empty)']}")
        # if not Paid, force blank mode
        if sale.get("payment_status") != "Paid":
            sale["payment_mode"] = ""
        else:
            sale["payment_mode"] = mode

    write_csv("sales", rows)
    return {"ok": True}


# ---- Sales report ----

@app.get("/api/sales/report")
def sales_report(
    period: str = "last30",
    category: Optional[str] = None,
    item: Optional[str] = None,
    branch: Optional[str] = None,
    payment_status: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    s, e = _date_range(period, start, end)
    rows = read_csv("sales")

    total = 0.0
    by_cat: Dict[str, float] = {}
    by_item: Dict[str, float] = {}
    by_branch: Dict[str, float] = {}
    filtered = []

    for r in rows:
        try:
            d = parse_date(r["date"])
        except Exception:
            continue

        if d < s or d > e:
            continue
        if category and r["category"] != category:
            continue
        if item and (r["item"] or "").strip().lower() != item.strip().lower():
            continue
        if branch and (r.get("branch") or "") != branch:
            continue
        if payment_status and (r.get("payment_status") or "") != payment_status:
            continue

        amt = float(r.get("total_price") or 0.0)
        total += amt
        by_cat[r["category"]] = by_cat.get(r["category"], 0.0) + amt
        by_item[r["item"]] = by_item.get(r["item"], 0.0) + amt
        by_branch[r.get("branch") or ""] = by_branch.get(r.get("branch") or "", 0.0) + amt
        filtered.append(r)

    return {
        "period": {"start": str(s), "end": str(e)},
        "total_sales": round(total, 2),
        "by_category": {k: round(v, 2) for k, v in by_cat.items()},
        "by_item": {k: round(v, 2) for k, v in by_item.items()},
        "by_branch": {k: round(v, 2) for k, v in by_branch.items()},
        "rows": filtered,
    }


# ---- CSV Download / Import ----

@app.get("/download/{name}")
def download_csv(name: str):
    if name not in HEADERS:
        raise HTTPException(404, "Unknown CSV name")
    path = _csv_path(name)
    return FileResponse(path, filename=path.name)


@app.post("/import")
async def import_csv(kind: str = Form(...), file: UploadFile = File(...)):
    if kind not in HEADERS:
        raise HTTPException(400, f"kind must be one of {list(HEADERS.keys())}")
    data = await file.read()
    today_dir = get_today_dir()
    dest = today_dir / f"{kind}.csv"

    text = data.decode("utf-8").splitlines()
    reader = csv.reader(text)
    header = next(reader, [])
    if [h.strip() for h in header] != HEADERS[kind]:
        raise HTTPException(400, f"CSV headers must be: {HEADERS[kind]}")

    with dest.open("wb") as fp:
        fp.write(data)

    return {"ok": True, "saved": dest.name}


# Ensure today's folder & CSVs exist on startup
get_today_dir()
