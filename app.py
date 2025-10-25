import os, io, re, json, datetime, random
from typing import Dict, Any, List, Tuple, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, CallbackQueryHandler, filters
)
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

# ==== ENV / CONFIG ====
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BASE_URL       = os.getenv("BASE_URL", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_SA_JSON_PATH = os.getenv("GOOGLE_SA_JSON_PATH")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH_PNG = os.path.join(BASE_DIR, "assets", "logo.png")
LOGO_PATH_JPG = os.path.join(BASE_DIR, "assets", "logo.jpg")

if not TELEGRAM_TOKEN or not SPREADSHEET_ID or not GOOGLE_SA_JSON_PATH:
    raise RuntimeError("Faltan envs: TELEGRAM_TOKEN, SPREADSHEET_ID, GOOGLE_SA_JSON_PATH")

# ==== Datos de la empresa ====
EMPRESA_NOMBRE = "Zabalza Gladys Beatriz"
EMPRESA_CUIT   = "CUIT: 27-13058782-3"
EMPRESA_DIR    = "Castelli 45 - San Vicente, Buenos Aires"
EMPRESA_TEL    = "Tel: 2224501287"
EMPRESA_MAIL   = "ocasoseguridadlaboral@gmail.com"

FOOTER_TEXT = "Todos los valores incluyen IVA · Validez 10 días · Cualquier consulta, no dudes en escribirnos"

# ==== Google Sheets (solo lectura) ====
def fetch_catalog() -> List[Dict[str, Any]]:
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(GOOGLE_SA_JSON_PATH, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)

    ws = None
    for w in sh.worksheets():
        if w.title.strip().lower() == "productos":
            ws = w
            break
    if ws is None:
        raise RuntimeError("No encontré la pestaña 'Productos' en el Google Sheet.")

    rows = ws.get_all_records()
    if not rows:
        raise RuntimeError("La pestaña 'Productos' está vacía o sin encabezados.")

    out = []
    for r in rows:
        prod = (r.get("Producto") or r.get("producto") or r.get("PRODUCTO") or "").strip()
        price = r.get("Precio") or r.get("precio") or r.get("Precio ($)") or r.get("precio ($)")
        if isinstance(price, str):
            price = price.replace(".", "").replace(",", ".")
        try:
            price = float(price or 0)
        except:
            price = 0.0
        if prod:
            out.append({"Producto": prod, "Precio": price})

    if not out:
        raise RuntimeError("No se encontraron filas válidas (revisá columnas 'Producto' y 'Precio').")

    return out

# ==== Parser ====
ITEM_SEP = re.compile(r"[,\n;]+", re.I)
QTY_PAT  = re.compile(r"(?:x\s*(\d+))|(?:(\d+)\s*(?:u|unid|unidades)?)", re.I)

def split_parts(text: str) -> List[str]:
    parts = [p.strip() for p in ITEM_SEP.split(text) if p.strip()]
    return parts or [text.strip()]

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
        if score > best_score:
            best_score = score; best = row
    return best if best_score >= 80 else None

def parse_items(text: str, catalog: List[Dict[str,Any]]) -> Tuple[List[Tuple[Dict[str,Any],int]], List[str]]:
    out=[]; warnings=[]
    for part in split_parts(text):
        frag, qty = extract_qty(part)
        match = best_match(frag, catalog)
        if match: out.append((match, qty))
        else: warnings.append(f"⚠️ No entendí “{part}”.")
    return out, warnings

# ==== Logo & encabezado ====
def load_logo_imagereader() -> Optional[ImageReader]:
    path = LOGO_PATH_PNG if os.path.exists(LOGO_PATH_PNG) else (LOGO_PATH_JPG if os.path.exists(LOGO_PATH_JPG) else None)
    if path:
        try:
            with open(path, "rb") as f:
                data = f.read()
            return ImageReader(io.BytesIO(data))
        except Exception as e:
            print("[logo] Error al cargar logo:", e)
            return None
    else:
        print("[logo] No se encontró assets/logo.png ni assets/logo.jpg")
        return None

LOGO_IR = load_logo_imagereader()

TOP_MARGIN_MM = 15
# Bloque más alto para alojar logo grande y todos los datos antes de la línea separadora:
HEADER_BLOCK_HEIGHT_MM = 50

def draw_footer(c: canvas.Canvas):
    W, H = A4
    c.setFont("Helvetica-Oblique", 9)
    c.setFillGray(0.25)
    c.drawCentredString(W/2, 12*mm, FOOTER_TEXT)
    c.setFillGray(0)

def draw_header(c: canvas.Canvas, doc_label: str, doc_id: str) -> float:
    """
    Encabezado:
      - Izquierda: logo (64x28mm) + datos empresa
      - Derecha (arriba): Fecha/Hora + Nº (chico)
      - Debajo, derecha: título grande
      - Línea separadora DESPUÉS de Tel/Mail
      - Retorna Y inicial para contenido
    """
    W, H = A4
    top = H - TOP_MARGIN_MM*mm
    x_left = 15*mm
    x_right = W - 15*mm
    y = top

    # Logo grande (doble)
    if LOGO_IR:
        try:
            c.drawImage(LOGO_IR, x_left, y - 28*mm, width=64*mm, height=28*mm,
                        preserveAspectRatio=True, mask='auto')
            y -= 30*mm  # espacio posteado
        except Exception as e:
            print("[logo] drawImage error:", e)

    # Datos empresa (sin "subnombre" para evitar redundancia)
    c.setFont("Helvetica", 10)
    c.drawString(x_left, y, EMPRESA_NOMBRE); y -= 5*mm
    c.drawString(x_left, y, EMPRESA_CUIT);   y -= 5*mm
    c.drawString(x_left, y, EMPRESA_DIR);    y -= 5*mm
    c.drawString(x_left, y, f"{EMPRESA_TEL} | {EMPRESA_MAIL}")

    # Derecha: Fecha/Hora y Nº (chico)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    c.setFont("Helvetica", 9)
    c.drawRightString(x_right, top,        f"Fecha: {now_str}")
    c.drawRightString(x_right, top - 5*mm, f"Nº: {doc_id}")

    # Título grande a la derecha (ligeramente más abajo para armonizar con el logo)
    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(x_right, top - 14*mm, doc_label)

    # Línea separadora (ahora seguro debajo de Tel/Mail)
    sep_y = H - (TOP_MARGIN_MM + HEADER_BLOCK_HEIGHT_MM)*mm
    c.setLineWidth(0.6)
    c.line(15*mm, sep_y, x_right, sep_y)

    return sep_y - 6*mm

# ==== PDFs ====
def pdf_presupuesto(p_id: str, cliente: str, items: List[Tuple[Dict[str,Any],int]], desc_pct: float) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    content_y = draw_header(c, "Presupuesto", p_id)
    c.setFont("Helvetica", 10)
    c.drawString(15*mm, content_y - 6*mm, f"Cliente: {cliente}")
    y = content_y - 16*mm

    # Cabecera de tabla
    c.setFont("Helvetica-Bold", 10)
    c.drawString(15*mm,  y, "Descripción")
    c.drawString(140*mm, y, "Cant.")
    c.drawRightString(190*mm, y, "Precio")
    y -= 5*mm
    c.setLineWidth(0.4); c.line(15*mm, y, 190*mm, y)
    y -= 6*mm
    c.setFont("Helvetica", 10)

    total = 0.0
    for prod, qty in items:
        price_unit = float(prod["Precio"]) * (1 - float(desc_pct)/100.0)  # descuento oculto
        if y < 30*mm:
            draw_footer(c)
            c.showPage()
            content_y = draw_header(c, "Presupuesto", p_id)
            c.setFont("Helvetica", 10)
            c.drawString(15*mm, content_y - 6*mm, f"Cliente: {cliente}")
            y = content_y - 16*mm
            c.setFont("Helvetica-Bold", 10)
            c.drawString(15*mm,  y, "Descripción")
            c.drawString(140*mm, y, "Cant.")
            c.drawRightString(190*mm, y, "Precio")
            y -= 5*mm; c.setLineWidth(0.4); c.line(15*mm, y, 190*mm, y)
            y -= 6*mm; c.setFont("Helvetica", 10)

        c.drawString(15*mm,  y, prod["Producto"][:90])
        c.drawString(140*mm, y, str(qty))
        c.drawRightString(190*mm, y, f"${price_unit:,.2f}")
        total += price_unit * qty
        y -= 6*mm

    y -= 4*mm
    c.setLineWidth(0.4); c.line(120*mm, y, 190*mm, y)
    y -= 8*mm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(120*mm, y, "TOTAL:")
    c.drawRightString(190*mm, y, f"${total:,.2f}")

    draw_footer(c)
    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()

def pdf_remito(r_id: str, cliente: str, items: List[Tuple[Dict[str,Any],int]]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4

    content_y = draw_header(c, "Remito", r_id)
    c.setFont("Helvetica", 10)
    c.drawString(15*mm, content_y - 6*mm, f"Cliente: {cliente}")
    y = content_y - 16*mm

    c.setFont("Helvetica-Bold", 10)
    c.drawString(15*mm,  y, "Descripción")
    c.drawString(140*mm, y, "Cant.")
    y -= 5*mm
    c.setLineWidth(0.4); c.line(15*mm, y, 190*mm, y)
    y -= 6*mm
    c.setFont("Helvetica", 10)

    for prod, qty in items:
        if y < 30*mm:
            draw_footer(c)
            c.showPage()
            content_y = draw_header(c, "Remito", r_id)
            c.setFont("Helvetica", 10)
            c.drawString(15*mm, content_y - 6*mm, f"Cliente: {cliente}")
            y = content_y - 16*mm
            c.setFont("Helvetica-Bold", 10)
            c.drawString(15*mm,  y, "Descripción")
            c.drawString(140*mm, y, "Cant.")
            y -= 5*mm; c.setLineWidth(0.4); c.line(15*mm, y, 190*mm, y)
            y -= 6*mm; c.setFont("Helvetica", 10)

        c.drawString(15*mm,  y, prod["Producto"][:100])
        c.drawString(140*mm, y, str(qty))
        y -= 6*mm

    draw_footer(c)
    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()

def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.datetime.now().strftime('%Y%m%d-%H%M')}-{random.randint(1000,9999)}"

# ==== Bot ====
app = FastAPI()
bot_app: Application = ApplicationBuilder().token(TELEGRAM_TOKEN).updater(None).build()

# Estados
ASK_CLIENT="ASK_CLIENT"
ASK_ITEMS="ASK_ITEMS"
ASK_REVIEW="ASK_REVIEW"
ASK_DISC="ASK_DISC"
FLOW="FLOW"

def render_items_list(pairs: List[Tuple[Dict[str,Any],int]]) -> str:
    return "\n".join([f"- {p['Producto']} x {q}" for p,q in pairs]) or "(sin ítems)"

def apply_corrections(current: List[Tuple[Dict[str,Any],int]], msg: str, catalog: List[Dict[str,Any]]) -> List[Tuple[Dict[str,Any],int]]:
    items = current[:]
    lines = [ln.strip() for ln in re.split(r"[,\n;]+", msg) if ln.strip()]
    for ln in lines:
        m = re.match(r"(?i)^eliminar\s+(.+)$", ln)
        if m:
            target = m.group(1).strip()
            best_i = -1; best_score = -1
            for i,(prod,qty) in enumerate(items):
                sc = fuzz.WRatio(target.lower(), prod["Producto"].lower())
                if sc > best_score:
                    best_score = sc; best_i = i
            if best_i >= 0:
                items.pop(best_i)
            continue
        frag, qty = extract_qty(ln)
        match = best_match(frag, catalog)
        if match:
            replaced = False
            for i,(prod,old_q) in enumerate(items):
                if prod["Producto"].lower() == match["Producto"].lower():
                    items[i] = (prod, qty)
                    replaced = True
                    break
            if not replaced:
                items.append((match, qty))
    return items

def review_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("✅ Confirmar", callback_data="confirm"),
         InlineKeyboardButton("✏️ Editar", callback_data="edit")]
    ]
    return InlineKeyboardMarkup(kb)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Bot de Presupuestos & Remitos*\n\n"
        "Comandos:\n"
        "• /presupuesto → cliente → ítems → revisión → descuento (oculto) → PDF\n"
        "• /remito → cliente → ítems → revisión → PDF sin precios\n\n"
        "Ejemplo:\n`2 pantalones cargo 42 verde, remera negra L x1`\n\n"
        "En revisión podés:\n"
        "• `eliminar <descripción>`\n"
        "• `<descripción> x <cantidad>`",
        parse_mode=ParseMode.MARKDOWN
    )

async def cmd_presupuesto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[FLOW]="PRESU"; context.user_data[ASK_CLIENT]=True
    await update.message.reply_text("Cliente del *presupuesto*:", parse_mode=ParseMode.MARKDOWN)

async def cmd_remito(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[FLOW]="REMITO"; context.user_data[ASK_CLIENT]=True
    await update.message.reply_text("Cliente del *remito*:", parse_mode=ParseMode.MARKDOWN)

async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg=(update.message.text or "").strip()

    # Cliente
    if context.user_data.get(ASK_CLIENT):
        context.user_data["cliente"]=msg
        context.user_data.pop(ASK_CLIENT,None)
        context.user_data[ASK_ITEMS]=True
        await update.message.reply_text(
            "Enviá los *ítems* (separados por coma o renglón). Ej: `2 pantalones cargo 42 verde, remera negra L x1`",
            parse_mode=ParseMode.MARKDOWN
        ); return

    # Ítems -> revisión (con botones)
    if context.user_data.get(ASK_ITEMS):
        catalog = fetch_catalog()
        pairs, warnings = parse_items(msg, catalog)
        if not pairs:
            await update.message.reply_text(
                (";\n".join(warnings) if warnings else "No pude entender los ítems. Probá ajustar la descripción."),
                parse_mode=ParseMode.MARKDOWN
            ); return

        context.user_data["items"]=pairs
        context.user_data["catalog"]=catalog
        context.user_data.pop(ASK_ITEMS, None)
        context.user_data[ASK_REVIEW]=True

        lista = render_items_list(pairs)
        warning_text = ("\n\n" + " ".join(warnings)) if warnings else ""
        await update.message.reply_text(
            f"Revisión de ítems:\n{lista}{warning_text}\n\n"
            "Usá los botones para *Confirmar* o *Editar*. También podés enviar correcciones por texto.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=review_keyboard()
        ); return

    # Revisión/edición por texto (sigue vigente además de los botones)
    if context.user_data.get(ASK_REVIEW):
        if msg.lower() in ["ok", "listo", "confirmar", "sí", "si"]:
            # Simular confirmación
            await handle_confirm(update, context)
            return
        else:
            # Aplicar correcciones y volver a mostrar teclado
            catalog = context.user_data.get("catalog", fetch_catalog())
            current = context.user_data.get("items", [])
            updated = apply_corrections(current, msg, catalog)
            context.user_data["items"] = updated
            lista = render_items_list(updated)
            await update.message.reply_text(
                f"Lista actualizada:\n{lista}\n\n"
                "¿Seguimos? Usá *Confirmar* o *Editar*, o mandá más correcciones.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=review_keyboard()
            ); return

    # Descuento y emisión
    if context.user_data.get(ASK_DISC):
        try:
            d=float(msg.replace(",","."))
            if not (0<=d<=100): raise ValueError()
        except:
            await update.message.reply_text("Ingresá un número entre 0 y 100.")
            return
        p_id=new_id("P")
        pdf=pdf_presupuesto(p_id, context.user_data.get("cliente",""), context.user_data.get("items",[]), d)
        await update.message.reply_document(
            document=InputFile(io.BytesIO(pdf), filename=f"Presupuesto_{p_id}.pdf"),
            caption=f"Presupuesto {p_id} listo ✅"
        )
        context.user_data.clear(); return

    await update.message.reply_text("Usá /presupuesto o /remito.")

# --- Botones de revisión ---
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = (query.data or "").lower()

    if not context.user_data.get(ASK_REVIEW):
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if data == "confirm":
        # Confirmar
        await handle_confirm(update, context, from_button=True)
    elif data == "edit":
        # Indicar cómo editar (el estado se mantiene en ASK_REVIEW)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Enviá correcciones. Ejemplos:\n"
            "• `eliminar remera negra`\n"
            "• `remera negra L x 2`",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await query.edit_message_reply_markup(reply_markup=None)

async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, from_button: bool=False):
    # limpiar teclado si vino de botón
    if from_button and update.callback_query:
        try:
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    context.user_data.pop(ASK_REVIEW, None)
    if context.user_data.get(FLOW)=="PRESU":
        context.user_data[ASK_DISC]=True
        await (update.callback_query.message.reply_text if from_button else update.message.reply_text)(
            "Perfecto. ¿Qué *% de descuento* aplico? (0–100)\n"
            "*(el descuento no se mostrará; se aplica por renglón)*",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        r_id=new_id("R")
        pdf=pdf_remito(r_id, context.user_data.get("cliente",""), context.user_data.get("items",[]))
        await (update.callback_query.message.reply_document if from_button else update.message.reply_document)(
            document=InputFile(io.BytesIO(pdf), filename=f"Remito_{r_id}.pdf"),
            caption=f"Remito {r_id} listo ✅"
        )
        context.user_data.clear()

# ==== Ciclo de vida / Webhook ====
app = FastAPI()

bot_app: Application = ApplicationBuilder().token(TELEGRAM_TOKEN).updater(None).build()

@app.on_event("startup")
async def on_startup():
    try:
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.bot.delete_webhook(drop_pending_updates=True)
        if BASE_URL and BASE_URL.startswith("https://"):
            url = f"{BASE_URL}/webhook/{TELEGRAM_TOKEN}"
            await bot_app.bot.set_webhook(url=url)
            print(f"[startup] Webhook seteado en: {url}")
        else:
            print("[startup] BASE_URL no configurada o sin https. Webhook no seteado (ok para 1er deploy).")
    except Exception as e:
        import traceback
        print("[startup][ERROR]", e)
        print(traceback.format_exc())

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await bot_app.stop()
        await bot_app.shutdown()
        print("[shutdown] Application detenida correctamente.")
    except Exception as e:
        print("[shutdown][WARN]", e)

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

@app.post("/setwebhook")
async def set_webhook():
    if not BASE_URL.startswith("https://"):
        raise HTTPException(400, "BASE_URL no configurada o no es https")
    url=f"{BASE_URL}/webhook/{TELEGRAM_TOKEN}"
    await bot_app.bot.set_webhook(url=url)
    return {"ok":True, "webhook":url}

# Handlers
bot_app.add_handler(CommandHandler("start", cmd_start))
bot_app.add_handler(CommandHandler("presupuesto", cmd_presupuesto))
bot_app.add_handler(CommandHandler("remito", cmd_remito))
bot_app.add_handler(CallbackQueryHandler(on_button))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
