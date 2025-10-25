import os, io, re, json, datetime, random, asyncio
from typing import Dict, Any, List, Tuple, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

from telegram import Update, InputFile
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader

from dotenv import load_dotenv
from rapidfuzz import fuzz

# ==== IA opcional ====
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# ==== CONFIG ====
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BASE_URL       = os.getenv("BASE_URL", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_SA_JSON_PATH = os.getenv("GOOGLE_SA_JSON_PATH")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LOGO_PATH = "./assets/logo.png"

if not TELEGRAM_TOKEN or not SPREADSHEET_ID or not GOOGLE_SA_JSON_PATH:
    raise RuntimeError("Faltan envs: TELEGRAM_TOKEN, SPREADSHEET_ID, GOOGLE_SA_JSON_PATH")

# ==== Datos de la empresa ====
EMPRESA_NOMBRE = "Zabalza Gladys Beatriz"
EMPRESA_SUB    = "OCASO – Ropa de trabajo"
EMPRESA_CUIT   = "CUIT: 27-13058782-3"
EMPRESA_DIR    = "Castelli 45 - San Vicente, Buenos Aires"
EMPRESA_TEL    = "Tel: 2224501287"
EMPRESA_MAIL   = "ocasoseguridadlaboral@gmail.com"

# ==== Cargar catálogo de Google Sheets ====
def fetch_catalog() -> List[Dict[str, Any]]:
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(GOOGLE_SA_JSON_PATH, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    w  = sh.worksheet("Productos")
    rows = w.get_all_records()
    out = []
    for r in rows:
        prod = str(r.get("Producto", "")).strip()
        price = r.get("Precio", r.get("Precio ($)", 0))
        if isinstance(price, str):
            price = price.replace(".", "").replace(",", ".")
        try:
            price = float(price or 0)
        except:
            price = 0.0
        if prod:
            out.append({"Producto": prod, "Precio": price})
    return out

# ==== Parser ====
ITEM_SEP = re.compile(r"[,\n;]+", re.I)
QTY_PAT  = re.compile(r"(?:x\s*(\d+))|(?:(\d+)\s*(?:u|unid|unidades)?)", re.I)

def split_parts(text: str) -> List[str]:
    return [p.strip() for p in ITEM_SEP.split(text) if p.strip()]

def extract_qty(s: str) -> Tuple[str, int]:
    qty = 1
    m = QTY_PAT.search(s)
    if m:
        qty = int(m.group(1) or m.group(2))
        s = QTY_PAT.sub("", s).strip()
    return s, max(qty, 1)

def best_match(query: str, catalog: List[Dict[str,Any]]) -> Optional[Dict[str,Any]]:
    query = query.lower()
    best=None; best_score=-1
    for row in catalog:
        cand = row["Producto"].lower()
        score = fuzz.WRatio(query, cand)
        if score>best_score:
            best_score=score; best=row
    return best if best_score>=80 else None

def parse_items(text: str, catalog: List[Dict[str,Any]]) -> Tuple[List[Tuple[Dict[str,Any],int]], List[str]]:
    out=[]; warnings=[]
    for part in split_parts(text):
        frag, qty = extract_qty(part)
        match = best_match(frag, catalog)
        if match: out.append((match, qty))
        else: warnings.append(f"⚠️ No entendí “{part}”.")
    return out, warnings

# ==== IA (opcional) ====
def ai_extract(text: str) -> Optional[List[Dict[str,Any]]]:
    if not (OPENAI_API_KEY and OpenAI):
        return None
    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = (
        "Extrae ítems de este texto de venta de indumentaria en español.\n"
        "Devuelve JSON con key 'items': [{\"descripcion\": str, \"cantidad\": int}]. SOLO JSON.\n"
        f"TEXTO:\n{text}"
    )
    try:
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],
            temperature=0
        )
        data = json.loads(res.choices[0].message.content)
        return data.get("items")
    except Exception:
        return None

# ==== LOGO ====
def load_logo_imagereader() -> Optional[ImageReader]:
    if os.path.exists(LOGO_PATH):
        try:
            return ImageReader(LOGO_PATH)
        except Exception:
            pass
    return None

LOGO_IR = load_logo_imagereader()

# ==== PDFs ====
def draw_header(c, title: str):
    W, H = A4
    if LOGO_IR:
        c.drawImage(LOGO_IR, 15*mm, H-30*mm, width=30*mm, height=12*mm, preserveAspectRatio=True, mask='auto')
    c.setFont("Helvetica-Bold", 14); c.drawString(50*mm, H-18*mm, EMPRESA_SUB)
    c.setFont("Helvetica", 10)
    c.drawString(50*mm, H-25*mm, EMPRESA_NOMBRE)
    c.drawString(50*mm, H-31*mm, EMPRESA_CUIT)
    c.drawString(50*mm, H-37*mm, EMPRESA_DIR)
    c.drawString(50*mm, H-43*mm, f"{EMPRESA_TEL} | {EMPRESA_MAIL}")
    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(W - 15*mm, H - 18*mm, title)

def pdf_presupuesto(p_id, cliente, items, desc_pct):
    buf=io.BytesIO(); c=canvas.Canvas(buf, pagesize=A4); W,H=A4
    draw_header(c, f"Presupuesto {p_id}")
    c.setFont("Helvetica", 10)
    c.drawString(15*mm, H-50*mm, f"Cliente: {cliente}")
    c.drawString(15*mm, H-57*mm, datetime.datetime.now().strftime("Fecha: %Y-%m-%d %H:%M"))
    y=H-70*mm; subtotal=0.0
    c.setFont("Helvetica-Bold",10)
    c.drawString(15*mm,y,"Descripción")
    c.drawString(140*mm,y,"Cant.")
    c.drawRightString(190*mm,y,"Precio")
    y-=6*mm; c.line(15*mm,y,190*mm,y); y-=6*mm; c.setFont("Helvetica",10)
    for prod,qty in items:
        if y<30*mm: c.showPage(); draw_header(c,f"Presupuesto {p_id}"); y=H-30*mm
        c.drawString(15*mm,y,prod["Producto"][:90])
        c.drawString(140*mm,y,str(qty))
        c.drawRightString(190*mm,y,f"${prod['Precio']:,.2f}")
        subtotal+=prod["Precio"]*qty; y-=6*mm
    total=subtotal*(1-desc_pct/100)
    y-=6*mm; c.line(120*mm,y,190*mm,y); y-=8*mm
    c.drawString(120*mm,y,"Subtotal:"); c.drawRightString(190*mm,y,f"${subtotal:,.2f}"); y-=6*mm
    c.drawString(120*mm,y,f"Descuento ({desc_pct:.0f}%):"); c.drawRightString(190*mm,y,f"-${subtotal*desc_pct/100:,.2f}")
    y-=6*mm; c.setFont("Helvetica-Bold",11)
    c.drawString(120*mm,y,"TOTAL:"); c.drawRightString(190*mm,y,f"${total:,.2f}")
    c.showPage(); c.save(); buf.seek(0); return buf.read()

def pdf_remito(r_id, cliente, items):
    buf=io.BytesIO(); c=canvas.Canvas(buf, pagesize=A4); W,H=A4
    draw_header(c, f"Remito {r_id}")
    c.setFont("Helvetica",10)
    c.drawString(15*mm, H-50*mm, f"Cliente: {cliente}")
    c.drawString(15*mm, H-57*mm, datetime.datetime.now().strftime("Fecha: %Y-%m-%d %H:%M"))
    y=H-70*mm
    c.setFont("Helvetica-Bold",10)
    c.drawString(15*mm,y,"Descripción")
    c.drawString(140*mm,y,"Cant.")
    y-=6*mm; c.line(15*mm,y,190*mm,y); y-=6*mm; c.setFont("Helvetica",10)
    for prod,qty in items:
        if y<30*mm: c.showPage(); draw_header(c,f"Remito {r_id}"); y=H-30*mm
        c.drawString(15*mm,y,prod["Producto"][:100])
        c.drawString(140*mm,y,str(qty)); y-=6*mm
    c.showPage(); c.save(); buf.seek(0); return buf.read()

def new_id(prefix): 
    return f"{prefix}-{datetime.datetime.now().strftime('%Y%m%d-%H%M')}-{random.randint(1000,9999)}"

# ==== BOT ====
app = FastAPI()
bot_app: Application = ApplicationBuilder().token(TELEGRAM_TOKEN).updater(None).build()

ASK_CLIENT="ASK_CLIENT"; ASK_ITEMS="ASK_ITEMS"; ASK_DISC="ASK_DISC"; FLOW="FLOW"

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Bot de Presupuestos & Remitos*\n\n"
        "Comandos:\n"
        "• /presupuesto → cliente → ítems → descuento → PDF con precios\n"
        "• /remito → cliente → ítems → PDF sin precios\n\n"
        "Ejemplo:\n`2 pantalones cargo 42 verde, remera negra L x1`",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_presupuesto(update, context):
    context.user_data[FLOW]="PRESU"; context.user_data[ASK_CLIENT]=True
    await update.message.reply_text("Cliente del *presupuesto*:", parse_mode=ParseMode.MARKDOWN)

async def cmd_remito(update, context):
    context.user_data[FLOW]="REMITO"; context.user_data[ASK_CLIENT]=True
    await update.message.reply_text("Cliente del *remito*:", parse_mode=ParseMode.MARKDOWN)

async def route_text(update, context):
    msg=(update.message.text or "").strip()
    if context.user_data.get(ASK_CLIENT):
        context.user_data["cliente"]=msg
        context.user_data.pop(ASK_CLIENT,None)
        context.user_data[ASK_ITEMS]=True
        await update.message.reply_text("Enviá los *ítems* (natural, separados por coma o renglón):", parse_mode=ParseMode.MARKDOWN)
        return
    if context.user_data.get(ASK_ITEMS):
        catalog=fetch_catalog()
        pairs, warnings = parse_items(msg, catalog)
        if not pairs:
            await update.message.reply_text("No pude entender los ítems. Probá ajustar la descripción.")
            return
        context.user_data["items"]=pairs
        lista="\n".join([f"- {p['Producto']} x {q}" for p,q in pairs])
        if context.user_data.get(FLOW)=="PRESU":
            context.user_data[ASK_ITEMS]=False; context.user_data[ASK_DISC]=True
            await update.message.reply_text(f"Ítems entendidos:\n{lista}\n\n¿Qué *% de descuento* aplico? (0–100)", parse_mode=ParseMode.MARKDOWN)
        else:
            r_id=new_id("R")
            pdf=pdf_remito(r_id, context.user_data.get("cliente",""), pairs)
            await update.message.reply_document(document=InputFile(io.BytesIO(pdf), filename=f"Remito_{r_id}.pdf"), caption=f"Remito {r_id} listo ✅")
            context.user_data.clear()
        return
    if context.user_data.get(ASK_DISC):
        try: d=float(msg.replace(",","."))
        except: 
            await update.message.reply_text("Ingresá un número válido de descuento (0–100)."); return
        p_id=new_id("P")
        pdf=pdf_presupuesto(p_id, context.user_data.get("cliente",""), context.user_data.get("items",[]), d)
        await update.message.reply_document(document=InputFile(io.BytesIO(pdf), filename=f"Presupuesto_{p_id}.pdf"), caption=f"Presupuesto {p_id} listo ✅ (Desc: {d:.0f}%)")
        context.user_data.clear(); return
    await update.message.reply_text("Usá /presupuesto o /remito.")

# ==== WEBHOOK (solo si BASE_URL es https) ====
@app.on_event("startup")
async def on_startup():
    await bot_app.bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL.startswith("https://"):
        url=f"{BASE_URL}/webhook/{TELEGRAM_TOKEN}"
        await bot_app.bot.set_webhook(url=url)
    else:
        print("⚠️ BASE_URL no es https o no configurada — no se registra webhook (Render lo hará después).")

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != TELEGRAM_TOKEN:
        return JSONResponse(status_code=403, content={"ok":False})
    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return {"ok":True}

@app.get("/")
async def root():
    return {"ok": True, "msg": "Ocaso Bot Presupuestos & Remitos activo"}

# Opcional: endpoint para setear webhook manualmente
@app.post("/setwebhook")
async def set_webhook():
    if not BASE_URL.startswith("https://"):
        raise HTTPException(400, "BASE_URL no configurada o no es https")
    url=f"{BASE_URL}/webhook/{TELEGRAM_TOKEN}"
    await bot_app.bot.set_webhook(url=url)
    return {"ok":True, "webhook":url}

bot_app.add_handler(CommandHandler("start", cmd_start))
bot_app.add_handler(CommandHandler("presupuesto", cmd_presupuesto))
bot_app.add_handler(CommandHandler("remito", cmd_remito))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
