import os, io, re, json, datetime, random
from typing import Dict, Any, List, Tuple, Optional

from fastapi import FastAPI, Request
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

# IA opcional
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# ============ CONFIG / ENV ============
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BASE_URL       = os.getenv("BASE_URL")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_SA_JSON_PATH = os.getenv("GOOGLE_SA_JSON_PATH")  # Secret File en Render
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # opcional
LOGO_URL       = os.getenv("LOGO_URL")        # opcional (http/https)
LOGO_PATH      = os.getenv("LOGO_PATH", "./assets/logo.png")  # opcional (archivo en repo)

if not TELEGRAM_TOKEN or not SPREADSHEET_ID or not GOOGLE_SA_JSON_PATH:
    raise RuntimeError("Faltan envs: TELEGRAM_TOKEN, SPREADSHEET_ID, GOOGLE_SA_JSON_PATH")

# Datos de la empresa (encabezado)
EMPRESA_NOMBRE = "Zabalza Gladys Beatriz"
EMPRESA_SUB    = "OCASO – Ropa de trabajo"
EMPRESA_CUIT   = "CUIT: 27-13058782-3"
EMPRESA_DIR    = "Castelli 45 - San Vicente, Buenos Aires"
EMPRESA_TEL    = "Tel: 2224501287"
EMPRESA_MAIL   = "ocasoseguridadlaboral@gmail.com"

# ============ CATALOGO (solo lectura) ============
def fetch_catalog() -> List[Dict[str, Any]]:
    """Lee pestaña 'Productos' con columnas: Producto | Precio"""
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
        prod = str(r.get("Producto","")).strip()
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

# ============ PARSER NATURAL ============
ITEM_SEP = re.compile(r"[,\n;]+", re.I)
QTY_PAT  = re.compile(r"(?:x\s*(\d+))|(?:(\d+)\s*(?:u|unid|unidades)?)", re.I)

def split_parts(text: str) -> List[str]:
    parts = [p.strip() for p in ITEM_SEP.split(text) if p.strip()]
    return parts if parts else [text.strip()]

def extract_qty(s: str) -> Tuple[str,int]:
    qty=1
    m=QTY_PAT.search(s)
    if m:
        qty=int(m.group(1) or m.group(2))
        s=QTY_PAT.sub("", s).strip()
    return s, max(qty,1)

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
    warnings=[]
    out=[]
    # Formato estricto: "CODIGO x CANT" por línea (lo permitimos pero sin buscar código en catálogo)
    strict=False
    for line in text.splitlines():
        m=re.match(r"^([A-Za-z0-9\-\_\.]+)\s*[xX]\s*(\d+)$", line.strip())
        if m:
            strict=True
    if strict:
        warnings.append("Usaste formato CODIGO x CANT. Como el catálogo no tiene códigos, haré fuzzy por descripción.")
    # Natural
    for part in split_parts(text):
        frag, qty = extract_qty(part)
        q = re.sub(r"\s+"," ",frag).strip()
        if not q: continue
        match = best_match(q, catalog)
        if match:
            out.append((match, qty))
        else:
            warnings.append(f"⚠️ No entendí “{part}”.")
    return out, warnings

def ai_extract(text: str) -> Optional[List[Dict[str,Any]]]:
    if not (OPENAI_API_KEY and OpenAI): return None
    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = (
        "Extrae ítems de este texto de venta de indumentaria en español.\n"
        "Devuelve JSON con key 'items': [{\"descripcion\": str, \"cantidad\": int>=1}]. SOLO JSON.\n"
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

# ============ LOGO ============
def load_logo_imagereader() -> Optional[ImageReader]:
    # 1) archivo en repo (assets/logo.png)
    if os.path.exists(LOGO_PATH):
        try:
            return ImageReader(LOGO_PATH)
        except Exception:
            pass
    # 2) URL https
    if LOGO_URL:
        try:
            import requests
            r = requests.get(LOGO_URL, timeout=10)
            r.raise_for_status()
            return ImageReader(io.BytesIO(r.content))
        except Exception:
            pass
    return None

LOGO_IR = load_logo_imagereader()

# ============ PDFs ============
def draw_header(c, title: str):
    W,H = A4
    y = H - 18*mm
    # Logo (si hay)
    if LOGO_IR:
        try:
            c.drawImage(LOGO_IR, 15*mm, H-30*mm, width=30*mm, height=12*mm, preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    # Títulos
    c.setFont("Helvetica-Bold", 14); c.drawString(50*mm, y, EMPRESA_SUB)
    c.setFont("Helvetica", 11)
    c.drawString(50*mm, y-6,  EMPRESA_NOMBRE)
    c.drawString(50*mm, y-6-12, EMPRESA_CUIT)
    c.drawString(50*mm, y-6-24, EMPRESA_DIR)
    c.drawString(50*mm, y-6-36, f"{EMPRESA_TEL}  |  {EMPRESA_MAIL}")

    # Título del documento (derecha)
    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(W - 15*mm, H - 18*mm, title)

def pdf_presupuesto(p_id: str, cliente: str, items: List[Tuple[Dict[str,Any],int]], desc_pct: float) -> bytes:
    buf=io.BytesIO(); c=canvas.Canvas(buf, pagesize=A4); W,H=A4
    draw_header(c, f"Presupuesto {p_id}")
    c.setFont("Helvetica", 10)
    c.drawString(15*mm, H-45*mm, f"Cliente: {cliente}")
    c.drawString(15*mm, H-52*mm, datetime.datetime.now().strftime("Fecha: %Y-%m-%d %H:%M"))

    y = H - 65*mm
    c.setFont("Helvetica-Bold",10)
    c.drawString(15*mm,y,"Descripción")
    c.drawString(140*mm,y,"Cant.")
    c.drawRightString(190*mm,y,"Precio")
    y-=6*mm; c.line(15*mm,y,190*mm,y); y-=6*mm; c.setFont("Helvetica",10)

    subtotal=0.0
    for prod,qty in items:
        if y<30*mm: c.showPage(); draw_header(c, f"Presupuesto {p_id}"); y=H-30*mm
        c.drawString(15*mm,y, prod["Producto"][:90])
        c.drawString(140*mm,y, str(qty))
        c.drawRightString(190*mm,y, f"${prod['Precio']:,.2f}")
        subtotal += prod["Precio"]*qty
        y-=6*mm

    total = subtotal * (1 - desc_pct/100.0)
    y-=6*mm; c.line(120*mm,y,190*mm,y); y-=8*mm
    c.drawString(120*mm,y,"Subtotal:"); c.drawRightString(190*mm,y,f"${subtotal:,.2f}"); y-=6*mm
    c.drawString(120*mm,y,f"Descuento ({desc_pct:.0f}%):"); c.drawRightString(190*mm,y,f"- ${subtotal*desc_pct/100:,.2f}"); y-=6*mm
    c.setFont("Helvetica-Bold",11); c.drawString(120*mm,y,"TOTAL:"); c.drawRightString(190*mm,y,f"${total:,.2f}")
    c.showPage(); c.save(); buf.seek(0); return buf.read()

def pdf_remito(r_id: str, cliente: str, items: List[Tuple[Dict[str,Any],int]]) -> bytes:
    buf=io.BytesIO(); c=canvas.Canvas(buf, pagesize=A4); W,H=A4
    draw_header(c, f"Remito {r_id}")
    c.setFont("Helvetica",10)
    c.drawString(15*mm, H-45*mm, f"Cliente: {cliente}")
    c.drawString(15*mm, H-52*mm, datetime.datetime.now().strftime("Fecha: %Y-%m-%d %H:%M"))

    y = H - 65*mm
    c.setFont("Helvetica-Bold",10)
    c.drawString(15*mm,y,"Descripción")
    c.drawString(140*mm,y,"Cant.")
    y-=6*mm; c.line(15*mm,y,190*mm,y); y-=6*mm; c.setFont("Helvetica",10)

    for prod,qty in items:
        if y<30*mm: c.showPage(); draw_header(c, f"Remito {r_id}"); y=H-30*mm
        c.drawString(15*mm,y, prod["Producto"][:100])
        c.drawString(140*mm,y, str(qty))
        y-=6*mm
    c.showPage(); c.save(); buf.seek(0); return buf.read()

def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.datetime.now().strftime('%Y%m%d-%H%M')}-{random.randint(1000,9999)}"

# ============ BOT ============
app = FastAPI()
bot_app: Application = ApplicationBuilder().token(TELEGRAM_TOKEN).updater(None).build()

ASK_CLIENT="ASK_CLIENT"; ASK_ITEMS="ASK_ITEMS"; ASK_DISC="ASK_DISC"; FLOW="FLOW"

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot *simple* de Presupuestos & Remitos (catálogo solo lectura).\n\n"
        "Comandos:\n"
        "• /presupuesto → cliente → ítems → % desc → PDF (con tu encabezado y logo)\n"
        "• /remito → cliente → ítems → PDF sin precios\n\n"
        "Ítems ejemplos:\n"
        "• Natural: `2 pantalones cargo 42 verde, remera negra L x1`\n"
        "• También entiende líneas separadas, y sugiere si no encuentra coincidencias.",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[FLOW]="PRESU"
    context.user_data[ASK_CLIENT]=True
    await update.message.reply_text("Cliente del *presupuesto*:", parse_mode=ParseMode.MARKDOWN)

async def cmd_remito(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[FLOW]="REMITO"
    context.user_data[ASK_CLIENT]=True
    await update.message.reply_text("Cliente del *remito*:", parse_mode=ParseMode.MARKDOWN)

def parse_flow(text: str) -> Tuple[List[Tuple[Dict[str,Any],int]], List[str]]:
    catalog = fetch_catalog()
    # IA primero (opcional)
    pairs: List[Tuple[Dict[str,Any],int]]=[]
    warnings: List[str]=[]
    ai = ai_extract(text)
    if ai:
        for it in ai:
            desc=str(it.get("descripcion","")).strip()
            qty=int(it.get("cantidad",1) or 1)
            if not desc: continue
            match = best_match(desc, catalog)
            if match: pairs.append((match, qty))
            else: warnings.append(f"⚠️ No entendí “{desc}”.")
    else:
        pairs, warnings = parse_items(text, catalog)
    return pairs, warnings

async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg=(update.message.text or "").strip()

    if context.user_data.get(ASK_CLIENT):
        context.user_data["cliente"]=msg
        context.user_data.pop(ASK_CLIENT,None)
        context.user_data[ASK_ITEMS]=True
        await update.message.reply_text(
            "Enviá los *ítems* (natural, separados por coma o renglón). Ej: `2 pantalones cargo 42 verde, remera negra L x1`",
            parse_mode=ParseMode.MARKDOWN
        ); return

    if context.user_data.get(ASK_ITEMS):
        pairs, warnings = parse_flow(msg)
        if not pairs:
            await update.message.reply_text(
                (";\n".join(warnings) if warnings else "No pude entender los ítems.") + "\nProbá afinar la descripción.",
                parse_mode=ParseMode.MARKDOWN
            ); return

        context.user_data["items"]=pairs
        lista = "\n".join([f"- {p['Producto']} x {q}" for p,q in pairs])

        if context.user_data.get(FLOW)=="PRESU":
            context.user_data[ASK_ITEMS]=False
            context.user_data[ASK_DISC]=True
            warn = ("\n\n" + " ".join(warnings)) if warnings else ""
            await update.message.reply_text(
                f"Ítems entendidos:\n{lista}{warn}\n\n¿Qué *% de descuento* aplico? (0–100)",
                parse_mode=ParseMode.MARKDOWN
            ); return
        else:
            r_id=new_id("R")
            pdf=pdf_remito(r_id, context.user_data.get("cliente",""), pairs)
            await update.message.reply_document(
                document=InputFile(io.BytesIO(pdf), filename=f"Remito_{r_id}.pdf"),
                caption=f"Ítems:\n{lista}\n\nRemito {r_id} listo ✅"
            )
            for k in [ASK_ITEMS,"cliente","items",FLOW,ASK_DISC]:
                context.user_data.pop(k, None)
            return

    if context.user_data.get(ASK_DISC):
        try:
            d=float(msg.replace(",","."))
            if not (0<=d<=100): raise ValueError()
        except:
            await update.message.reply_text("Ingresá un número entre 0 y 100.")
            return
        p_id=new_id("P")
        pdf=pdf_presupuesto(p_id, context.user_data.get("cliente",""), context.user_data.get("items",[]), d)
        lista = "\n".join([f"- {p['Producto']} x {q}" for p,q in context.user_data.get("items",[])])
        await update.message.reply_document(
            document=InputFile(io.BytesIO(pdf), filename=f"Presupuesto_{p_id}.pdf"),
            caption=f"Ítems:\n{lista}\n\nPresupuesto {p_id} listo ✅ (Desc: {d:.0f}%)"
        )
        for k in [ASK_ITEMS,"cliente","items",FLOW,ASK_DISC]:
            context.user_data.pop(k, None)
        return

    await update.message.reply_text("Usá /presupuesto o /remito.")

# ============ WEBHOOK ============
@app.on_event("startup")
async def on_startup():
    url=f"{BASE_URL}/webhook/{TELEGRAM_TOKEN}"
    await bot_app.bot.set_webhook(url=url)

@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    if token != TELEGRAM_TOKEN:
        return JSONResponse(status_code=403, content={"ok":False})
    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return {"ok":True}

@app.get("/")
async def health():
    return {"ok":True,"service":"presu-remito-lec-only"}

bot_app.add_handler(CommandHandler("start", cmd_start))
bot_app.add_handler(CommandHandler("presupuesto", cmd_presupuesto))
bot_app.add_handler(CommandHandler("remito", cmd_remito))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))

if __name__=="__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT","8000")))
