"""
Medical Visit Tracker — Telegram Bot
=====================================
Photographs → Gemini Flash extraction → Google Sheets storage
Author: Built for visiting nephrologist/intensivist workflow
"""

import os, json, re, asyncio, io, logging
from datetime import datetime, date
import gspread
import google.generativeai as genai
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from google.oauth2.service_account import Credentials

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — set these as environment variables on Railway/Render
# ─────────────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY")
GOOGLE_SHEET_ID     = os.environ.get("GOOGLE_SHEET_ID")
AUTHORIZED_CHAT_ID  = int(os.environ.get("AUTHORIZED_CHAT_ID", "0"))  # Your Telegram chat ID
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")  # Full JSON string

PRIMARY_HOSPITAL = "Care CHL Hospital"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# GOOGLE SHEETS SETUP
# ─────────────────────────────────────────────────────────────────────────────
SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SHEET_HEADERS = [
    "ID", "Date", "Patient Name", "MRN", "DOB", "Age", "Gender",
    "Hospital", "Ward", "OPD/IPD", "Diagnosis", "Clinical Notes",
    "Investigations Ordered", "Key Findings",
    "Key Events (Type)", "Key Events (Detail)",
    "Routine Only", "Photo Ref", "Added At"
]

def get_sheet():
    """Returns the main worksheet, creating it if needed."""
    creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet("Visits")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Visits", rows=5000, cols=len(SHEET_HEADERS))
        ws.append_row(SHEET_HEADERS)
        ws.format("A1:S1", {
            "backgroundColor": {"red": 0.05, "green": 0.15, "blue": 0.35},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}
        })
        # Freeze header row
        ws.freeze(rows=1)
    return ws

def get_all_records():
    """Returns all rows as list of dicts."""
    ws = get_sheet()
    return ws.get_all_records()

def append_row(entry: dict, photo_ref: str):
    """Appends a single extracted entry to the sheet."""
    ws = get_sheet()
    key_types  = " | ".join([e["type"]   for e in (entry.get("keyEvents") or [])])
    key_detail = " | ".join([e["detail"] for e in (entry.get("keyEvents") or [])])
    row = [
        f"ID-{datetime.now().strftime('%Y%m%d%H%M%S%f')[:18]}",
        entry.get("date", date.today().strftime("%d/%m/%Y")),
        entry.get("patientName", ""),
        entry.get("patientId", ""),
        entry.get("dob", ""),
        entry.get("age", ""),
        entry.get("gender", ""),
        entry.get("hospital", PRIMARY_HOSPITAL),
        entry.get("ward", "Other"),
        "OPD" if entry.get("ward") == "OPD" else "IPD",
        entry.get("diagnosis", ""),
        entry.get("clinicalNotes", ""),
        "; ".join(entry.get("investigationsOrdered") or []),
        "; ".join(entry.get("keyFindings") or []),
        key_types,
        key_detail,
        str(entry.get("routineOnly", False)),
        photo_ref,
        datetime.now().isoformat()
    ]
    ws.append_row(row)

def is_duplicate(entry: dict, records: list) -> bool:
    """Checks if same patient + date + hospital already exists."""
    name  = (entry.get("patientName") or "").strip().lower()
    dt    = entry.get("date", "")
    hosp  = (entry.get("hospital") or "").strip().lower()
    for r in records:
        if (
            (r.get("Patient Name") or "").strip().lower() == name and
            r.get("Date") == dt and
            (r.get("Hospital") or "").strip().lower() == hosp
        ):
            return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
# GEMINI EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-1.5-flash")

EXTRACT_PROMPT = """You are a Medical Visit Documentation Assistant for a visiting nephrologist/intensivist who sees 10–25 patients daily.

Analyze this image (patient ID sticker + handwritten clinical notes).
Return ONLY a valid JSON array — no markdown, no backticks, no explanation.

For EACH patient visible extract:
{
  "patientName": "Full name from sticker",
  "patientId": "MRN/Hospital ID, null if absent",
  "dob": "DD/MM/YYYY, null if absent",
  "age": "Age if visible, null if absent",
  "gender": "M/F/Other, null if absent",
  "date": "Visit date DD/MM/YYYY — from notes or sticker, use today if absent",
  "hospital": "Hospital name from sticker",
  "ward": "One of: ICU, CCU, PICU, HDU, General Ward, OPD, Emergency, Other",
  "diagnosis": "Primary diagnosis or reason for visit",
  "clinicalNotes": "Complete summary: vitals, exam findings, active problems, medications with doses and routes, investigation results if noted, plan — as detailed as possible",
  "investigationsOrdered": ["list of investigations ordered today"],
  "keyFindings": ["important abnormal findings noted"],
  "keyEvents": [
    {
      "type": "One of: Hemodialysis, ICU Admission/Transfer, Antibiotic Change, Critical Lab Result, Imaging Finding, Procedure",
      "detail": "Specific clinical detail with values, drug names, doses"
    }
  ],
  "routineOnly": true or false
}

KEY EVENTS: hemodialysis (session#, duration, UF), ICU admissions/transfers, antibiotic changes/discontinuations, critical labs (K>6, Na<120/>160, Cr>8, Hb<7, plt<50k, glucose<50/>500), significant imaging, invasive procedures.
routineOnly=true only if zero key events — purely routine review.

Today's date: """ + date.today().strftime("%d/%m/%Y")

async def extract_from_image(image_bytes: bytes) -> list:
    """Sends image to Gemini Flash and returns list of extracted patient dicts."""
    img = Image.open(io.BytesIO(image_bytes))
    response = gemini.generate_content([EXTRACT_PROMPT, img])
    text = response.text.strip()
    # Strip markdown if present
    text = re.sub(r"```json|```", "", text).strip()
    return json.loads(text)

# ─────────────────────────────────────────────────────────────────────────────
# PHOTO COUNTER (per session, resets on bot restart — acceptable)
# ─────────────────────────────────────────────────────────────────────────────
_photo_counter = {"n": 1}

def next_photo_ref():
    ref = f"IMG-{str(_photo_counter['n']).zfill(3)}"
    _photo_counter["n"] += 1
    return ref

# ─────────────────────────────────────────────────────────────────────────────
# AUTHORIZATION CHECK
# ─────────────────────────────────────────────────────────────────────────────
def authorized(update: Update) -> bool:
    logger.info(f"Message from chat_id: {update.effective_chat.id} | authorized_id: {AUTHORIZED_CHAT_ID}")
    return update.effective_chat.id == AUTHORIZED_CHAT_ID
```

6. Click **Commit changes**

---

## Step 2 — Send /start Again

Once Railway redeploys (wait for `Bot started — polling…` in logs):
1. Send `/start` to your bot on Telegram
2. Immediately go to Railway **Logs** tab

You will see a line like:
```
Message from chat_id: XXXXXXXXX | authorized_id: 6190782543

# ─────────────────────────────────────────────────────────────────────────────
# BOT HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    await update.message.reply_text(
        "🏥 *Medical Visit Tracker* — Ready\n\n"
        "📸 *Send a photo* → AI extracts patient data → saved to Google Sheets\n\n"
        "Commands:\n"
        "/status — today's visit count\n"
        "/patients — this month's patient list\n"
        "/check [name] — summary for a patient\n"
        "/today — all visits added today\n"
        "/monthly — generate month-end report\n"
        "/help — full command list",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    await update.message.reply_text(
        "📋 *Commands*\n\n"
        "📸 *Photo* — send patient sticker + notes photo to extract and save\n"
        "/status — visits logged today vs this month\n"
        "/patients — full patient list this month\n"
        "/check [partial name] — visits and summary for a patient\n"
        "/today — all entries added today\n"
        "/stats — ward/hospital breakdown this month\n"
        "/monthly — generate month-end Telegram summary\n"
        "/dupes — check for possible duplicates\n"
        "/help — this message",
        parse_mode="Markdown"
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler — photo received, extract, deduplicate, save."""
    if not authorized(update): return

    await update.message.reply_text("🔍 Extracting patient data…")

    try:
        # Get highest resolution photo
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        photo_ref = next_photo_ref()
        patients = await extract_from_image(bytes(image_bytes))

        if not patients:
            await update.message.reply_text("⚠️ No patient data found in this image. Try a clearer photo.")
            return

        records = get_all_records()
        added, dupes = [], []

        for p in patients:
            if is_duplicate(p, records):
                dupes.append(p.get("patientName", "Unknown"))
            else:
                append_row(p, photo_ref)
                added.append(p)
                records.append({  # Update local cache to catch same-batch dupes
                    "Patient Name": p.get("patientName", ""),
                    "Date": p.get("date", ""),
                    "Hospital": p.get("hospital", "")
                })

        # Build response message
        lines = []
        for p in added:
            ward   = p.get("ward", "?")
            opd_ipd = "OPD" if ward == "OPD" else "IPD"
            ke     = p.get("keyEvents") or []
            inv    = p.get("investigationsOrdered") or []
            lines.append(
                f"✅ *{p.get('patientName', 'Unknown')}*\n"
                f"   🏥 {p.get('hospital', '?')} · {ward} ({opd_ipd})\n"
                f"   📅 {p.get('date', '?')} · {p.get('diagnosis', '—')}\n"
                + (f"   🔬 Ix: {', '.join(inv)}\n" if inv else "")
                + (f"   ⚡ Events: {', '.join([e['type'] for e in ke])}\n" if ke else "   ✔️ Routine visit\n")
                + f"   📷 {photo_ref}"
            )

        for name in dupes:
            lines.append(f"⚠️ *{name}* — duplicate (same date/hospital), skipped")

        await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")

    except json.JSONDecodeError:
        await update.message.reply_text("❌ Gemini returned unexpected format. Try again or retake the photo.")
    except Exception as e:
        logger.error(f"Photo handler error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Today's and this month's visit counts."""
    if not authorized(update): return
    records = get_all_records()
    today_str = date.today().strftime("%d/%m/%Y")
    month_num = date.today().month
    year_num  = date.today().year

    today_recs = [r for r in records if r.get("Date") == today_str]
    month_recs = [r for r in records if _month_match(r.get("Date",""), month_num, year_num)]

    opd_t = sum(1 for r in today_recs if r.get("OPD/IPD") == "OPD")
    ipd_t = sum(1 for r in today_recs if r.get("OPD/IPD") == "IPD")
    opd_m = sum(1 for r in month_recs if r.get("OPD/IPD") == "OPD")
    ipd_m = sum(1 for r in month_recs if r.get("OPD/IPD") == "IPD")
    ke_m  = sum(1 for r in month_recs if r.get("Key Events (Type)", ""))
    hd_m  = sum(1 for r in month_recs if "Hemodialysis" in (r.get("Key Events (Type)") or ""))
    pts_m = len(set(r.get("Patient Name","") for r in month_recs))

    await update.message.reply_text(
        f"📊 *Status — {date.today().strftime('%d %b %Y')}*\n\n"
        f"*Today*\n"
        f"  Visits: {len(today_recs)}  ·  OPD: {opd_t}  ·  IPD: {ipd_t}\n\n"
        f"*This Month*\n"
        f"  Patients: {pts_m}  ·  Visits: {len(month_recs)}\n"
        f"  OPD: {opd_m}  ·  IPD: {ipd_m}\n"
        f"  Key Events: {ke_m}  ·  HD Sessions: {hd_m}",
        parse_mode="Markdown"
    )

async def patients_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all unique patients this month."""
    if not authorized(update): return
    records = get_all_records()
    month_num = date.today().month
    year_num  = date.today().year
    month_recs = [r for r in records if _month_match(r.get("Date",""), month_num, year_num)]

    # Group by patient
    pts = {}
    for r in month_recs:
        name = r.get("Patient Name","?")
        if name not in pts: pts[name] = {"visits":0,"opd":0,"ipd":0,"hosp":r.get("Hospital",""),"hd":0}
        pts[name]["visits"] += 1
        if r.get("OPD/IPD") == "OPD": pts[name]["opd"] += 1
        else: pts[name]["ipd"] += 1
        if "Hemodialysis" in (r.get("Key Events (Type)") or ""): pts[name]["hd"] += 1

    if not pts:
        await update.message.reply_text("No patients recorded this month yet.")
        return

    lines = [f"👥 *{len(pts)} Patients — {date.today().strftime('%B %Y')}*\n"]
    for i, (name, d) in enumerate(sorted(pts.items()), 1):
        hd_str = f" · HD×{d['hd']}" if d["hd"] else ""
        lines.append(f"{i}. *{name}*  {d['visits']}× visit{'s' if d['visits']>1 else ''} (OPD:{d['opd']} IPD:{d['ipd']}){hd_str}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Summary for a specific patient. Usage: /check ramesh"""
    if not authorized(update): return
    if not context.args:
        await update.message.reply_text("Usage: /check [patient name]\nExample: /check ramesh")
        return

    query = " ".join(context.args).lower()
    records = get_all_records()
    matches = [r for r in records if query in (r.get("Patient Name","")).lower()]

    if not matches:
        await update.message.reply_text(f"No records found for '{query}'")
        return

    # Group by patient name (take first match)
    matches.sort(key=lambda r: r.get("Date",""))
    name = matches[0].get("Patient Name","?")
    lines = [f"📋 *{name}* — {len(matches)} visit(s)\n"]

    for i, r in enumerate(matches, 1):
        ke = r.get("Key Events (Type)","")
        inv = r.get("Investigations Ordered","")
        findings = r.get("Key Findings","")
        lines.append(
            f"*Visit {i}* — {r.get('Date','?')}\n"
            f"  {r.get('Ward','?')} ({r.get('OPD/IPD','?')}) · {r.get('Hospital','?')}\n"
            f"  Dx: {r.get('Diagnosis','—')}\n"
            + (f"  Ix: {inv}\n" if inv else "")
            + (f"  Findings: {findings}\n" if findings else "")
            + (f"  ⚡ {ke}\n" if ke else "  ✔️ Routine\n")
        )

    # Telegram message limit is 4096 chars — chunk if needed
    full = "\n".join(lines)
    for chunk in _chunk_message(full):
        await update.message.reply_text(chunk, parse_mode="Markdown")

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """All entries added today."""
    if not authorized(update): return
    records = get_all_records()
    today_str = date.today().strftime("%d/%m/%Y")
    today_recs = [r for r in records if r.get("Date") == today_str]

    if not today_recs:
        await update.message.reply_text("No visits recorded today yet. Send a photo!")
        return

    lines = [f"📅 *Today's Visits — {today_str}*\n"]
    for r in today_recs:
        ke = r.get("Key Events (Type)","")
        lines.append(
            f"• *{r.get('Patient Name','?')}* — {r.get('Ward','?')} ({r.get('OPD/IPD','?')})\n"
            f"  {r.get('Diagnosis','—')}"
            + (f"\n  ⚡ {ke}" if ke else "")
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ward and hospital breakdown this month."""
    if not authorized(update): return
    records = get_all_records()
    month_num = date.today().month
    year_num  = date.today().year
    month_recs = [r for r in records if _month_match(r.get("Date",""), month_num, year_num)]

    if not month_recs:
        await update.message.reply_text("No data this month yet.")
        return

    ward_bd, hosp_bd, event_bd = {}, {}, {}
    for r in month_recs:
        w = r.get("Ward","?"); ward_bd[w] = ward_bd.get(w,0)+1
        h = r.get("Hospital","?"); hosp_bd[h] = hosp_bd.get(h,0)+1
        for ev in (r.get("Key Events (Type)") or "").split(" | "):
            if ev.strip(): event_bd[ev.strip()] = event_bd.get(ev.strip(),0)+1

    total = len(month_recs)
    def bar(n, tot, width=12):
        filled = round((n/tot)*width) if tot else 0
        return "█"*filled + "░"*(width-filled)

    ward_lines = "\n".join(f"  {bar(v,total)} {k}: {v}" for k,v in sorted(ward_bd.items(), key=lambda x:-x[1]))
    hosp_lines = "\n".join(f"  {bar(v,total)} {k}: {v}" for k,v in sorted(hosp_bd.items(), key=lambda x:-x[1]))
    ev_lines   = "\n".join(f"  • {k}: {v}" for k,v in sorted(event_bd.items(), key=lambda x:-x[1])) or "  None"

    await update.message.reply_text(
        f"📈 *Stats — {date.today().strftime('%B %Y')}*\n\n"
        f"*By Ward*\n{ward_lines}\n\n"
        f"*By Hospital*\n{hosp_lines}\n\n"
        f"*Key Events*\n{ev_lines}",
        parse_mode="Markdown"
    )

async def monthly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate formatted month-end summary directly in Telegram."""
    if not authorized(update): return

    await update.message.reply_text(
        "📊 Generating monthly summary…\n"
        "_(For Word Doc + Excel + Google Doc → run the Colab notebook)_",
        parse_mode="Markdown"
    )

    records = get_all_records()
    month_num = date.today().month
    year_num  = date.today().year
    month_recs = [r for r in records if _month_match(r.get("Date",""), month_num, year_num)]

    if not month_recs:
        await update.message.reply_text("No data for this month.")
        return

    # Group by patient
    pts = {}
    for r in month_recs:
        name = r.get("Patient Name","?")
        if name not in pts: pts[name] = []
        pts[name].append(r)

    lines = [f"🏥 *Monthly Summary — {date.today().strftime('%B %Y')}*",
             f"_{len(pts)} patients · {len(month_recs)} visits_\n"]

    for name, visits in sorted(pts.items()):
        visits.sort(key=lambda r: r.get("Date",""))
        opd = sum(1 for v in visits if v.get("OPD/IPD")=="OPD")
        ipd = sum(1 for v in visits if v.get("OPD/IPD")=="IPD")
        hd  = sum(1 for v in visits if "Hemodialysis" in (v.get("Key Events (Type)") or ""))
        ke  = sum(1 for v in visits if v.get("Key Events (Type)",""))
        dxs = list(dict.fromkeys(v.get("Diagnosis","") for v in visits if v.get("Diagnosis","")))
        hosp = visits[0].get("Hospital","?")
        wards = list(dict.fromkeys(v.get("Ward","") for v in visits))

        lines.append(
            f"*{name}*\n"
            f"  🏥 {hosp}  ·  Visits: {len(visits)} (OPD:{opd} IPD:{ipd})\n"
            f"  Wards: {', '.join(wards)}\n"
            f"  Dx: {'; '.join(dxs[:2]) or '—'}\n"
            + (f"  🩸 HD: {hd} sessions\n" if hd else "")
            + (f"  ⚡ Key events: {ke}\n" if ke else "")
            + f"  📅 {visits[0].get('Date','?')} → {visits[-1].get('Date','?')}"
        )

    full = "\n\n".join(lines)
    for chunk in _chunk_message(full):
        await update.message.reply_text(chunk, parse_mode="Markdown")

    await update.message.reply_text(
        "✅ Telegram summary done.\n\n"
        "📌 *For Word Doc, Excel & Google Doc:*\n"
        "Open your Colab notebook → Run All → files will be generated and sent here.",
        parse_mode="Markdown"
    )

async def dupes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check for possible duplicates in the sheet."""
    if not authorized(update): return
    records = get_all_records()
    seen, dupes = set(), []
    for r in records:
        key = f"{(r.get('Patient Name') or '').lower()}|{r.get('Date','')}|{(r.get('Hospital') or '').lower()}"
        if key in seen: dupes.append(r)
        else: seen.add(key)

    if not dupes:
        await update.message.reply_text("✅ No duplicates found in the sheet.")
    else:
        lines = [f"⚠️ *{len(dupes)} Duplicate Rows Found*\n"]
        for r in dupes:
            lines.append(f"• {r.get('Patient Name','?')} — {r.get('Date','?')} — {r.get('Hospital','?')}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────────────────────
def _month_match(date_str, month, year):
    try:
        parts = date_str.split("/")
        return int(parts[1]) == month and int(parts[2]) == year
    except: return False

def _chunk_message(text, limit=4000):
    """Split long messages into Telegram-safe chunks."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line + "\n"
        else:
            current += line + "\n"
    if current: chunks.append(current)
    return chunks

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("help",    help_cmd))
    app.add_handler(CommandHandler("status",  status))
    app.add_handler(CommandHandler("patients",patients_cmd))
    app.add_handler(CommandHandler("check",   check_cmd))
    app.add_handler(CommandHandler("today",   today_cmd))
    app.add_handler(CommandHandler("stats",   stats_cmd))
    app.add_handler(CommandHandler("monthly", monthly_cmd))
    app.add_handler(CommandHandler("dupes",   dupes_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Bot started — polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
