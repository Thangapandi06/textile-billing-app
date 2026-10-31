"""Textile shop POS - single counter. Run: uvicorn main:app --host 127.0.0.1 --port 8080
Money = integer paise. Quantity = integer milli-units (1 piece = 1000, 1.5 m = 1500).
Prices are GST-inclusive; tax is split out of the line total (CGST/SGST, intra-state)."""
import sqlite3, hashlib, secrets
from contextlib import contextmanager
from datetime import datetime, date
from html import escape
from pathlib import Path
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

DB = "shop.db"
SHOP = {"name": "வெற்றிவேல் கார்மெண்ட்ஸ்", "gstin": "", "address": "எண்: 4 வடுகபட்டி பேருந்து நிலையம் அருகில், வடுகபட்டி"}
MODES = {"CASH", "UPI", "CARD", "CREDIT"}
app = FastAPI(title=SHOP["name"])

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees(id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, active INT DEFAULT 1);
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, barcode TEXT UNIQUE, name_en TEXT NOT NULL, name_ta TEXT DEFAULT '',
  hsn TEXT DEFAULT '', gst_rate INT NOT NULL DEFAULT 5, unit TEXT NOT NULL DEFAULT 'PIECE' CHECK(unit IN('PIECE','METER')),
  price INT NOT NULL, stock_milli INT NOT NULL DEFAULT 0, active INT DEFAULT 1);
CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, ts TEXT NOT NULL, idem TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL, ref_invoice INT, cashier_id INT, customer_phone TEXT, taxable INT, cgst INT, sgst INT, total INT);
CREATE TABLE IF NOT EXISTS lines(id INTEGER PRIMARY KEY, invoice_id INT NOT NULL REFERENCES invoices(id), item_id INT NOT NULL,
  ref_line INT, salesman_id INT, name_en TEXT, name_ta TEXT, hsn TEXT, gst_rate INT, qty_milli INT, total INT, taxable INT, cgst INT, sgst INT,
  meter_rate_paise INT, meter_price_paise INT);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, invoice_id INT NOT NULL REFERENCES invoices(id), mode TEXT, amount INT);
CREATE TABLE IF NOT EXISTS stock_moves(id INTEGER PRIMARY KEY, item_id INT, change_milli INT, reason TEXT, ref_invoice INT, ts TEXT);
CREATE TABLE IF NOT EXISTS day_close(day TEXT PRIMARY KEY, expected_cash INT, counted_cash INT, diff INT, note TEXT, ts TEXT);
CREATE INDEX IF NOT EXISTS ix_items_name ON items(name_en, name_ta);
"""


def connect():
    c = sqlite3.connect(DB, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=FULL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


@contextmanager
def tx():
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def rows(sql, *a):
    c = connect()
    try:
        return [dict(r) for r in c.execute(sql, a).fetchall()]
    finally:
        c.close()


with tx() as c:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            c.execute(stmt)
    if not c.execute("SELECT 1 FROM employees").fetchone():
        c.execute("INSERT INTO employees(name,role) VALUES('Owner','CASHIER'),('Salesman 1','SALES')")


EXTRA = """CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, name TEXT, phone TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, customer_id INT, ts TEXT, amount INT, mode TEXT, ref_invoice INT, kind TEXT);
CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY, ts TEXT, supplier TEXT, bill_no TEXT, total INT);
CREATE TABLE IF NOT EXISTS purchase_lines(id INTEGER PRIMARY KEY, purchase_id INT, item_id INT, qty_milli INT, cost INT)"""
TOKENS = set()


def pin_hash(p):
    return hashlib.sha256(("tpos:" + p).encode()).hexdigest()


def owner(x_token: str = Header("")):
    if x_token not in TOKENS:
        raise HTTPException(401, "Owner PIN needed")


with tx() as c:
    for stmt in EXTRA.split(";"):
        c.execute(stmt)
    for alt in ("ALTER TABLE invoices ADD COLUMN igst INT DEFAULT 0", "ALTER TABLE invoices ADD COLUMN customer_id INT",
                "ALTER TABLE lines ADD COLUMN igst INT DEFAULT 0", "ALTER TABLE lines ADD COLUMN is_manual INT NOT NULL DEFAULT 0",
                "ALTER TABLE lines ADD COLUMN unit TEXT NOT NULL DEFAULT 'PIECE'",
                "ALTER TABLE lines ADD COLUMN meter_rate_paise INT",
                "ALTER TABLE lines ADD COLUMN meter_price_paise INT",
                "ALTER TABLE items ADD COLUMN cost INT DEFAULT 0"):
        try:
            c.execute(alt)
        except sqlite3.OperationalError:
            pass
    if not c.execute("SELECT 1 FROM settings WHERE k='pin'").fetchone():
        c.execute("INSERT INTO settings VALUES('pin',?)", (pin_hash("1234"),))  # default owner PIN - change it!


def now():
    return datetime.now().isoformat(" ", "seconds")


def split_tax(gross, rate):
    taxable = round(gross * 100 / (100 + rate))
    cgst = (gross - taxable) // 2
    return taxable, cgst, gross - taxable - cgst


def next_no(c, kind):
    n = datetime.now()
    y = n.year if n.month >= 4 else n.year - 1
    p = ("INV" if kind == "SALE" else "CN") + f"/{y % 100:02d}{(y + 1) % 100:02d}/"
    k = c.execute("SELECT COUNT(*) FROM invoices WHERE number LIKE ?", (p + "%",)).fetchone()[0]
    return p + f"{k + 1:06d}"


def save_invoice(c, kind, key, cashier, ref, lines, pays, phone=None, cid=None):
    total = sum(l["total"] for l in lines)
    if sum(p["amount"] for p in pays) != total:
        raise HTTPException(400, f"Payments must add up to the bill total ({total / 100:.2f})")
    if any(p["mode"] == "CREDIT" for p in pays) and not cid:
        raise HTTPException(400, "Credit needs a customer phone number")
    cur = c.execute("INSERT INTO invoices(number,ts,idem,kind,ref_invoice,cashier_id,customer_phone,customer_id,taxable,cgst,sgst,igst,total) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (next_no(c, kind), now(), key, kind, ref, cashier, phone, cid, sum(l["taxable"] for l in lines),
                     sum(l["cgst"] for l in lines), sum(l["sgst"] for l in lines), sum(l.get("igst", 0) for l in lines), total))
    iid = cur.lastrowid
    for l in lines:
        c.execute("INSERT INTO lines(invoice_id,item_id,ref_line,salesman_id,name_en,name_ta,hsn,gst_rate,qty_milli,total,taxable,cgst,sgst,igst,is_manual,unit,meter_rate_paise,meter_price_paise) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (iid, l["item_id"], l.get("ref_line"), l.get("salesman_id"), l["name_en"], l["name_ta"], l["hsn"],
                   l["gst_rate"], l["qty"], l["total"], l["taxable"], l["cgst"], l["sgst"], l.get("igst", 0),
                   l.get("is_manual", 0), l.get("unit", "PIECE"), l.get("meter_rate_paise"), l.get("meter_price_paise")))
        if not l.get("is_manual"):
            c.execute("UPDATE items SET stock_milli=stock_milli-? WHERE id=?", (l["qty"], l["item_id"]))
            c.execute("INSERT INTO stock_moves(item_id,change_milli,reason,ref_invoice,ts) VALUES(?,?,?,?,?)",
                      (l["item_id"], -l["qty"], kind, iid, now()))
    for p in pays:
        c.execute("INSERT INTO payments(invoice_id,mode,amount) VALUES(?,?,?)", (iid, p["mode"], p["amount"]))
        if p["mode"] == "CREDIT":
            c.execute("INSERT INTO ledger(customer_id,ts,amount,mode,ref_invoice,kind) VALUES(?,?,?,?,?,?)",
                      (cid, now(), p["amount"], "CREDIT", iid, "CREDIT_SALE"))
    return iid


def check_modes(pays):
    for p in pays:
        if p["mode"] not in MODES:
            raise HTTPException(400, "Payment mode must be CASH, UPI or CARD")


class CI(BaseModel):
    item_id: int | None = None
    qty_milli: int = Field(gt=0)
    salesman_id: int | None = None
    discount_paise: int = Field(0, ge=0)
    manual_name: str | None = Field(None, min_length=1, max_length=200)
    manual_hsn: str | None = Field(None, max_length=32)
    manual_price_paise: int | None = Field(None, gt=0)
    manual_gst_rate: int | None = Field(None, ge=0, le=40)
    manual_unit: str | None = None
    manual_meter_rate_paise: int | None = Field(None, gt=0)
    manual_meter_price_paise: int | None = Field(None, gt=0)


class Pay(BaseModel):
    mode: str
    amount_paise: int = Field(gt=0)


class Checkout(BaseModel):
    idempotency_key: str
    cashier_id: int
    items: list[CI] = Field(min_length=1, max_length=200)
    payments: list[Pay] = Field(min_length=1)
    customer_phone: str | None = None
    allow_negative_stock: bool = False
    customer_name: str | None = None


@app.post("/api/checkout")
def checkout(r: Checkout):
    with tx() as c:
        old = c.execute("SELECT id FROM invoices WHERE idem=?", (r.idempotency_key,)).fetchone()
        if old:
            return {"invoice_id": old["id"], "duplicate": True}
        lines, used = [], {}
        for it in r.items:
            if it.item_id is None:
                if not it.manual_name or not it.manual_name.strip() or it.manual_price_paise is None or it.manual_gst_rate is None:
                    raise HTTPException(400, "One-off items need a name, price and GST rate")
                if it.manual_unit not in {"PIECE", "METER"}:
                    raise HTTPException(400, "One-off item unit must be PIECE or METER")
                name_en, name_ta, hsn = it.manual_name.strip(), "", (it.manual_hsn or "").strip()
                if any(value is not None for value in (it.manual_meter_rate_paise, it.manual_meter_price_paise)) and it.manual_unit != "METER":
                    raise HTTPException(400, "A per-meter price is only valid for meter items")
                if it.manual_meter_rate_paise is not None and it.manual_meter_price_paise is not None:
                    raise HTTPException(400, "Enter one per-meter price only")
                if it.manual_meter_price_paise is not None and it.manual_meter_price_paise != it.manual_price_paise:
                    raise HTTPException(400, "Meter price must match the GST-inclusive unit price")
                item_id, price, gst_rate, unit = 0, it.manual_price_paise, it.manual_gst_rate, it.manual_unit
            else:
                if any(value is not None for value in (it.manual_name, it.manual_hsn, it.manual_price_paise, it.manual_gst_rate, it.manual_unit, it.manual_meter_rate_paise, it.manual_meter_price_paise)):
                    raise HTTPException(400, "Choose either a catalog item or a one-off item")
                row = c.execute("SELECT * FROM items WHERE id=? AND active=1", (it.item_id,)).fetchone()
                if not row:
                    raise HTTPException(404, f"Item {it.item_id} not found")
                used[it.item_id] = used.get(it.item_id, 0) + it.qty_milli
                if row["stock_milli"] < used[it.item_id] and not r.allow_negative_stock:
                    raise HTTPException(409, f"Not enough stock for {row['name_en']} (have {row['stock_milli'] / 1000:g})")
                name_en, name_ta, hsn = row["name_en"], row["name_ta"], row["hsn"]
                item_id, price, gst_rate, unit = row["id"], row["price"], row["gst_rate"], row["unit"]
            gross = round(price * it.qty_milli / 1000) - it.discount_paise
            if gross < 0:
                raise HTTPException(400, "Discount is larger than the line amount")
            t, cg, sg = split_tax(gross, gst_rate)
            lines.append(dict(item_id=item_id, is_manual=int(it.item_id is None), unit=unit, salesman_id=it.salesman_id,
                              name_en=name_en, name_ta=name_ta, hsn=hsn, gst_rate=gst_rate, qty=it.qty_milli,
                              total=gross, taxable=t, cgst=cg, sgst=sg, igst=0,
                              meter_rate_paise=it.manual_meter_rate_paise if it.item_id is None else None,
                              meter_price_paise=it.manual_meter_price_paise if it.item_id is None else None))
        pays = [dict(mode=p.mode.upper(), amount=p.amount_paise) for p in r.payments]
        check_modes(pays)
        cid = cust(c, r.customer_name, r.customer_phone, any(p["mode"] == "CREDIT" for p in pays))
        iid = save_invoice(c, "SALE", r.idempotency_key, r.cashier_id, None, lines, pays, r.customer_phone, cid)
    return {"invoice_id": iid, "duplicate": False}


class RL(BaseModel):
    line_id: int
    qty_milli: int = Field(gt=0)


class Ret(BaseModel):
    idempotency_key: str
    invoice_id: int
    cashier_id: int
    lines: list[RL] = Field(min_length=1)
    refund_mode: str = "CASH"


@app.post("/api/return", dependencies=[Depends(owner)])
def do_return(r: Ret):
    with tx() as c:
        old = c.execute("SELECT id FROM invoices WHERE idem=?", (r.idempotency_key,)).fetchone()
        if old:
            return {"invoice_id": old["id"], "duplicate": True}
        inv = c.execute("SELECT kind, customer_id FROM invoices WHERE id=?", (r.invoice_id,)).fetchone()
        if not inv or inv["kind"] != "SALE":
            raise HTTPException(400, "Returns can only be made against a sale invoice")
        out = []
        for rl in r.lines:
            o = c.execute("SELECT * FROM lines WHERE id=? AND invoice_id=?", (rl.line_id, r.invoice_id)).fetchone()
            if not o:
                raise HTTPException(404, "Line not on this invoice")
            back = -c.execute("SELECT COALESCE(SUM(qty_milli),0) FROM lines WHERE ref_line=?", (o["id"],)).fetchone()[0]
            if rl.qty_milli + back > o["qty_milli"]:
                raise HTTPException(409, f"Cannot return more than sold for {o['name_en']}")
            f = rl.qty_milli / o["qty_milli"]
            total, taxable, cg, ig = round(o["total"] * f), round(o["taxable"] * f), round(o["cgst"] * f), round((o["igst"] or 0) * f)
            sg = total - taxable - cg - ig
            out.append(dict(item_id=o["item_id"], is_manual=o["is_manual"], unit=o["unit"], ref_line=o["id"], salesman_id=o["salesman_id"], name_en=o["name_en"],
                            name_ta=o["name_ta"], hsn=o["hsn"], gst_rate=o["gst_rate"], qty=-rl.qty_milli,
                            total=-total, taxable=-taxable, cgst=-cg, sgst=-sg, igst=-ig,
                            meter_rate_paise=o["meter_rate_paise"], meter_price_paise=o["meter_price_paise"]))
        mode = r.refund_mode.upper()
        if mode == "CREDIT" and not inv["customer_id"]:
            raise HTTPException(400, "This invoice has no customer account to credit")
        pays = [dict(mode=mode, amount=sum(l["total"] for l in out))]
        check_modes(pays)
        iid = save_invoice(c, "RETURN", r.idempotency_key, r.cashier_id, r.invoice_id, out, pays, None, inv["customer_id"])
    return {"invoice_id": iid, "duplicate": False}


@app.get("/api/items")
def items(q: str = ""):
    q = q.strip()
    return rows("SELECT * FROM items WHERE active=1 AND (barcode=? OR name_en LIKE ? OR name_ta LIKE ?) ORDER BY name_en LIMIT 30",
                q, f"%{q}%", f"%{q}%")


class ItemIn(BaseModel):
    barcode: str
    name_en: str
    name_ta: str = ""
    hsn: str = ""
    gst_rate: int = Field(5, ge=0, le=40)
    unit: str = "PIECE"
    price_paise: int = Field(gt=0)
    add_stock_milli: int = 0


@app.post("/api/items", dependencies=[Depends(owner)])
def upsert_item(i: ItemIn):
    with tx() as c:
        c.execute("INSERT INTO items(barcode,name_en,name_ta,hsn,gst_rate,unit,price) VALUES(?,?,?,?,?,?,?) "
                  "ON CONFLICT(barcode) DO UPDATE SET name_en=excluded.name_en,name_ta=excluded.name_ta,hsn=excluded.hsn,"
                  "gst_rate=excluded.gst_rate,unit=excluded.unit,price=excluded.price,active=1",
                  (i.barcode, i.name_en, i.name_ta, i.hsn, i.gst_rate, i.unit, i.price_paise))
        iid = c.execute("SELECT id FROM items WHERE barcode=?", (i.barcode,)).fetchone()["id"]
        if i.add_stock_milli:
            c.execute("UPDATE items SET stock_milli=stock_milli+? WHERE id=?", (i.add_stock_milli, iid))
            c.execute("INSERT INTO stock_moves(item_id,change_milli,reason,ts) VALUES(?,?,'RECEIVE',?)", (iid, i.add_stock_milli, now()))
    return {"item_id": iid}


@app.get("/api/employees")
def employees():
    return rows("SELECT * FROM employees WHERE active=1")


@app.get("/api/invoices")
def invoices(limit: int = 20):
    return rows("SELECT * FROM invoices ORDER BY id DESC LIMIT ?", min(limit, 100))


def full_invoice(iid):
    inv = rows("SELECT * FROM invoices WHERE id=?", iid)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    lines = rows("SELECT l.*, COALESCE((SELECT -SUM(qty_milli) FROM lines r WHERE r.ref_line=l.id),0) AS returned_milli "
                 "FROM lines l WHERE invoice_id=?", iid)
    return {**inv[0], "lines": lines, "payments": rows("SELECT mode,amount FROM payments WHERE invoice_id=?", iid)}


@app.get("/api/invoices/{iid}")
def get_invoice(iid: int):
    return full_invoice(iid)


@app.get("/receipt/{iid}", response_class=HTMLResponse)
def receipt(iid: int):
    v = full_invoice(iid)
    m = lambda p: f"{p / 100:,.2f}"
    def meter_rate_note(line):
        if line["unit"] == "METER" and line["meter_price_paise"] is not None:
            return f"<br><small>Meter price ₹{m(line['meter_price_paise'])}/m incl. GST</small>"
        if line["unit"] == "METER" and line["meter_rate_paise"] is not None:
            return f"<br><small>Meter rate ₹{m(line['meter_rate_paise'])}/m before GST</small>"
        return ""
    rowsh = "".join(f"<tr><td>{escape(l['name_en'])}<br>{escape(l['name_ta'] or '')}<small> HSN {escape(l['hsn'] or '-')} @{l['gst_rate']}%</small>"
                    f"{meter_rate_note(l)}</td>"
                    f"<td>{l['qty_milli'] / 1000:g}{' m' if l['unit'] == 'METER' else ''}</td><td>{m(l['total'])}</td></tr>" for l in v["lines"])
    pays = ", ".join(f"{p['mode']} {m(p['amount'])}" for p in v["payments"])
    title = "TAX INVOICE" if v["kind"] == "SALE" else "CREDIT NOTE"
    return f"""<meta charset=utf-8><title>{v['number']}</title><style>body{{font:12px "Noto Sans Tamil",system-ui;width:76mm;margin:2mm auto}}
table{{width:100%;border-collapse:collapse}}td{{padding:2px 0;vertical-align:top}}td:nth-child(n+2){{text-align:right}}h3,p{{text-align:center;margin:2px}}hr{{border:0;border-top:1px dashed #000}}</style>
<img src="/shop-icon.png" alt="" style="display:block;width:18mm;height:18mm;object-fit:cover;object-position:center 45%;border-radius:4mm;margin:0 auto 2mm">
<h3>{escape(SHOP['name'])}</h3><p>{escape(SHOP['address'])}<br>GSTIN: {escape(SHOP['gstin'] or '-')}</p><hr><p><b>{title}</b><br>{v['number']}<br>{v['ts']}</p><hr>
<table>{rowsh}</table><hr><table><tr><td>Taxable</td><td></td><td>{m(v['taxable'])}</td></tr><tr><td>CGST</td><td></td><td>{m(v['cgst'])}</td></tr>
<tr><td>SGST</td><td></td><td>{m(v['sgst'])}</td></tr>{'' if not v['igst'] else '<tr><td>IGST</td><td></td><td>' + m(v['igst']) + '</td></tr>'}<tr><td><b>Total</b></td><td></td><td><b>{m(v['total'])}</b></td></tr></table><hr><p>{pays}</p>
<p>Thank you / நன்றி</p><script>window.addEventListener('load',()=>print())</script>"""


@app.get("/api/dayclose", dependencies=[Depends(owner)])
def day_summary(day: str = ""):
    day = day or date.today().isoformat()
    pay = {r["mode"]: r["s"] for r in rows("SELECT p.mode, SUM(p.amount) s FROM payments p JOIN invoices i ON i.id=p.invoice_id "
                                            "WHERE date(i.ts)=? GROUP BY p.mode", day)}
    gst = rows("SELECT l.gst_rate, SUM(l.taxable) taxable, SUM(l.cgst) cgst, SUM(l.sgst) sgst, SUM(l.igst) igst, SUM(l.total) total FROM lines l "
               "JOIN invoices i ON i.id=l.invoice_id WHERE date(i.ts)=? GROUP BY l.gst_rate", day)
    closed = rows("SELECT * FROM day_close WHERE day=?", day)
    st = {r["mode"]: -r["s"] for r in rows("SELECT mode, SUM(amount) s FROM ledger WHERE kind='SETTLE' AND date(ts)=? GROUP BY mode", day)}
    return {"day": day, "by_mode": pay, "collected": st, "expected_cash": pay.get("CASH", 0) + st.get("CASH", 0), "gst": gst, "closed": closed[0] if closed else None}


class Close(BaseModel):
    day: str = ""
    counted_cash_paise: int = Field(ge=0)
    note: str = ""


@app.post("/api/dayclose", dependencies=[Depends(owner)])
def close_day(b: Close):
    s = day_summary(b.day)
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO day_close VALUES(?,?,?,?,?,?)",
                  (s["day"], s["expected_cash"], b.counted_cash_paise, b.counted_cash_paise - s["expected_cash"], b.note, now()))
    return day_summary(b.day)


@app.get("/")
def home():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/shop-icon.png")
@app.get("/baby-murugan-images-5.png")
def shop_icon():
    return FileResponse(Path(__file__).parent / "baby-murugan-images-5.png", media_type="image/png")


class Login(BaseModel):
    pin: str


@app.post("/api/login")
def login(b: Login):
    row = rows("SELECT v FROM settings WHERE k='pin'")
    if not row or row[0]["v"] != pin_hash(b.pin):
        raise HTTPException(401, "Wrong PIN")
    t = secrets.token_hex(16)
    TOKENS.add(t)
    return {"token": t}


class NewPin(BaseModel):
    pin: str = Field(min_length=4, max_length=12)


@app.post("/api/pin", dependencies=[Depends(owner)])
def set_pin(b: NewPin):
    with tx() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES('pin',?)", (pin_hash(b.pin),))
    return {"ok": True}


@app.get("/api/items/all", dependencies=[Depends(owner)])
def all_items(q: str = ""):
    return rows("SELECT * FROM items WHERE active=1 AND (name_en LIKE ? OR name_ta LIKE ? OR barcode LIKE ?) ORDER BY name_en LIMIT 300",
                *[f"%{q}%"] * 3)


@app.delete("/api/items/{iid}", dependencies=[Depends(owner)])
def del_item(iid: int):
    with tx() as c:
        c.execute("UPDATE items SET active=0 WHERE id=?", (iid,))
    return {"ok": True}


def cust(c, name, phone, need):
    if not phone:
        if need:
            raise HTTPException(400, "Customer phone is needed for credit sales")
        return None
    c.execute("INSERT INTO customers(name,phone) VALUES(?,?) ON CONFLICT(phone) DO UPDATE SET name=COALESCE(NULLIF(excluded.name,''),name)",
              (name or "", phone))
    return c.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()["id"]


@app.get("/api/customers")
def customers():
    result = rows("SELECT c.id,c.name,c.phone,COALESCE(SUM(l.amount),0) balance FROM customers c "
                  "LEFT JOIN ledger l ON l.customer_id=c.id GROUP BY c.id ORDER BY balance DESC")
    c = connect()
    try:
        for customer in result:
            bills = [dict(row) for row in c.execute(
                "SELECT s.id invoice_id,s.number,s.ts,"
                "COALESCE(SUM(l.amount),0) balance "
                "FROM invoices s LEFT JOIN ledger l ON l.customer_id=? AND l.kind IN('CREDIT_SALE','SETTLE') "
                "AND (l.ref_invoice=s.id OR l.ref_invoice IN "
                "(SELECT r.id FROM invoices r WHERE r.kind='RETURN' AND r.ref_invoice=s.id)) "
                "WHERE s.kind='SALE' AND s.customer_id=? "
                "GROUP BY s.id ORDER BY s.ts,s.id",
                (customer["id"], customer["id"])).fetchall()]
            unassigned = c.execute(
                "SELECT COALESCE(SUM(amount),0) FROM ledger "
                "WHERE customer_id=? AND kind='SETTLE' AND ref_invoice IS NULL",
                (customer["id"],)).fetchone()[0]
            for bill in bills:
                bill["balance"] += unassigned
                unassigned = min(0, bill["balance"])
                bill["balance"] = max(0, bill["balance"])
            customer["credit_invoices"] = [bill for bill in bills if bill["balance"] > 0]
    finally:
        c.close()
    return result


def invoice_credit_balance(c, customer_id, invoice_id):
    bill = c.execute("SELECT ts FROM invoices WHERE id=? AND customer_id=? AND kind='SALE'",
                     (invoice_id, customer_id)).fetchone()
    if not bill:
        raise HTTPException(404, "Credit invoice not found for this customer")
    balance = c.execute(
        "SELECT COALESCE(SUM(l.amount),0) FROM ledger l "
        "WHERE l.customer_id=? AND l.kind IN('CREDIT_SALE','SETTLE') "
        "AND (l.ref_invoice=? OR l.ref_invoice IN "
        "(SELECT r.id FROM invoices r WHERE r.kind='RETURN' AND r.ref_invoice=?))",
        (customer_id, invoice_id, invoice_id)).fetchone()[0]
    unassigned = c.execute(
        "SELECT COALESCE(SUM(amount),0) FROM ledger "
        "WHERE customer_id=? AND kind='SETTLE' AND ref_invoice IS NULL",
        (customer_id,)).fetchone()[0]
    older = c.execute(
        "SELECT id FROM invoices WHERE customer_id=? AND kind='SALE' AND (ts<? OR (ts=? AND id<?)) ORDER BY ts,id",
        (customer_id, bill["ts"], bill["ts"], invoice_id)).fetchall()
    for row in older:
        prior = c.execute(
            "SELECT COALESCE(SUM(l.amount),0) FROM ledger l "
            "WHERE l.customer_id=? AND l.kind IN('CREDIT_SALE','SETTLE') "
            "AND (l.ref_invoice=? OR l.ref_invoice IN "
            "(SELECT r.id FROM invoices r WHERE r.kind='RETURN' AND r.ref_invoice=?))",
            (customer_id, row["id"], row["id"])).fetchone()[0]
        unassigned = min(0, prior + unassigned)
    return max(0, balance + unassigned)


class Settle(BaseModel):
    amount_paise: int = Field(gt=0)
    mode: str = "CASH"
    invoice_id: int | None = None


@app.post("/api/customers/{cid}/settle")
def settle(cid: int, b: Settle):
    if b.mode.upper() not in {"CASH", "UPI", "CARD"}:
        raise HTTPException(400, "Mode must be CASH, UPI or CARD")
    with tx() as c:
        if b.invoice_id is not None:
            balance = invoice_credit_balance(c, cid, b.invoice_id)
            if balance <= 0:
                raise HTTPException(409, "This invoice has no outstanding credit")
            if b.amount_paise > balance:
                raise HTTPException(400, f"Collection exceeds this invoice's outstanding balance ({balance / 100:.2f})")
        c.execute("INSERT INTO ledger(customer_id,ts,amount,mode,ref_invoice,kind) VALUES(?,?,?,?,?,'SETTLE')",
                  (cid, now(), -b.amount_paise, b.mode.upper(), b.invoice_id))
    return {"ok": True}


class PL(BaseModel):
    item_id: int
    qty_milli: int = Field(gt=0)
    cost_paise: int = Field(ge=0)


class Purch(BaseModel):
    supplier: str = Field(min_length=1)
    bill_no: str = ""
    lines: list[PL] = Field(min_length=1)


@app.post("/api/purchases", dependencies=[Depends(owner)])
def purchase(b: Purch):
    supplier = b.supplier.strip()
    if not supplier:
        raise HTTPException(400, "Supplier name is required")
    with tx() as c:
        total = sum(round(l.cost_paise * l.qty_milli / 1000) for l in b.lines)
        pid = c.execute("INSERT INTO purchases(ts,supplier,bill_no,total) VALUES(?,?,?,?)", (now(), supplier, b.bill_no.strip(), total)).lastrowid
        for l in b.lines:
            c.execute("INSERT INTO purchase_lines(purchase_id,item_id,qty_milli,cost) VALUES(?,?,?,?)", (pid, l.item_id, l.qty_milli, l.cost_paise))
            c.execute("UPDATE items SET stock_milli=stock_milli+?, cost=? WHERE id=?", (l.qty_milli, l.cost_paise, l.item_id))
            c.execute("INSERT INTO stock_moves(item_id,change_milli,reason,ref_invoice,ts) VALUES(?,?,'PURCHASE',?,?)", (l.item_id, l.qty_milli, pid, now()))
    return {"purchase_id": pid}


@app.get("/api/purchases", dependencies=[Depends(owner)])
def purchases():
    return rows("SELECT * FROM purchases ORDER BY id DESC LIMIT 30")


@app.get("/labels", response_class=HTMLResponse)
def labels(ids: str, copies: int = 1):
    n = [int(i) for i in ids.split(",") if i.strip().isdigit()][:100]
    its = rows(f"SELECT * FROM items WHERE id IN ({','.join('?' * len(n))})", *n) if n else []
    cells = "".join(f"<div class=l><b>{escape(i['name_en'])}</b><small>{escape(i['name_ta'] or '')}</small>"
                    f"<svg class=bc data-v=\"{escape(i['barcode'] or str(i['id']))}\"></svg><span>Rs {i['price'] / 100:,.2f}</span></div>"
                    for i in its for _ in range(max(1, min(copies, 60))))
    return f"""<meta charset=utf-8><title>Labels</title><style>body{{margin:0;font:10px "Noto Sans Tamil",sans-serif}}
.l{{display:inline-block;width:50mm;height:25mm;padding:1mm 2mm;box-sizing:border-box;text-align:center;overflow:hidden;page-break-inside:avoid}}
.l b,.l small,.l span{{display:block;white-space:nowrap;overflow:hidden}}.bc{{height:11mm;max-width:100%}}</style>{cells}
<script src="https://cdnjs.cloudflare.com/ajax/libs/jsbarcode/3.11.6/JsBarcode.all.min.js"></script>
<script>document.querySelectorAll('.bc').forEach(s=>JsBarcode(s,s.dataset.v,{{format:'CODE128',displayValue:true,fontSize:11,height:30,margin:0}}));setTimeout(print,400)</script>"""
