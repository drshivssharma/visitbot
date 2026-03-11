"""
Medical Visit Tracker - Telegram Bot
Uses Claude (Anthropic) for image extraction
"""

import os, json, re, logging, io, base64
from datetime import datetime, date
import requests
import gspread
from PIL import Image
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# CONFIG
TELEGRAM_TOKEN       = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_SHEET_ID      = os.environ.get("GOOGLE_SHEET_ID")
AUTHORIZED_CHAT_ID   = int(os.environ.get("AUTHORIZED_CHAT_ID", "0"))
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")
PRIMARY_HOSPITAL     = "Care CHL Hospital"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# CLAUDE IMAGE EXTRACTION
EXTRACT_PROMPT = """You are a Medical Visit Documentation Assistant for a visiting nephrologist/intensivist.

Analyze this image (patient ID sticker + handwritten clinical notes).
Return ONLY a valid JSON array. No markdown, no backticks, no explanation.

For EACH patient visible extract:
{
  "patientName": "Full name from sticker",
  "patientId": "MRN/Hospital ID, null if absent",
  "dob": "DD/MM/YYYY, null if absent",
  "age": "Age if visible, null if absent",
  "gender": "M/F/Other, null if absent",
  "date": "Visit date DD/MM/YYYY",
  "hospital": "Hospital name from sticker",
  "ward": "One of: ICU, CCU, PICU, HDU, General Ward, OPD, Emergency, Other",
  "diagnosis": "Primary diagnosis",
  "clinicalNotes": "Complete notes: vitals, exam, medications with doses, plan",
  "investigationsOrdered": ["list of investigations ordered today"],
  "keyFindings": ["important abnormal findings"],
  "keyEvents": [
    {
      "type": "One of: Hemodialysis, ICU Admission/Transfer, Antibiotic Change, Critical Lab Result, Imaging Finding, Procedure",
      "detail": "Specific detail with values and doses"
    }
  ],
  "routineOnly": true or false
}

routineOnly=true only if zero key events."""

def extract_from_image(image_bytes):
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 2048,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64
                        }
                    },
                    {
                        "type": "text",
                        "text": EXTRACT_PROMPT
                    }
                ]
            }]
        },
        timeout=30
    )
    logger.info(f"Claude API status: {response.status_code}")
    if response.status_code != 200:
        logger.error(f"Claude error: {response.text}")
        raise Exception(f"Claude API {response.status_code}: {response.text[:200]}")
    text = response.json()["content"][0]["text"]
    text = re.sub(r"```json|```", "", text).strip()
    return json.loads(text)

# GOOGLE SHEETS
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
    creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet("Visits")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Visits", rows=5000, cols=len(SHEET_HEADERS))
        ws.append_row(SHEET_HEADERS)
        ws.freeze(rows=1)
    return ws

def get_all_records():
    ws = get_sheet()
    return ws.get_all_records()

def append_row(entry, photo_ref):
    ws = get_sheet()
    key_types  = " | ".join([e["type"]   for e in (entry.get("keyEvents") or [])])
    key_detail = " | ".join([e["detail"] for e in (entry.get("keyEvents") or [])])
    row = [
        f"ID-{datetime.now().strftime('%Y%m%d%H%M%S')}",
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

def is_duplicate(entry, records):
    name = (entry.get("patientName") or "").strip().lower()
    dt   = entry.get("date", "")
    hosp = (entry.get("hospital") or "").strip().lower()
    for r in records:
        if (
            (r.get("Patient Name") or "").strip().lower() == name and
            r.get("Date") == dt and
            (r.get("Hospital") or "").strip().lower() == hosp
        ):
            return True
    return False

# PHOTO COUNTER
_photo_counter = {"n": 1}

def next_photo_ref():
    ref = f"IMG-{str(_photo_counter['n']).zfill(3)}"
    _photo_counter["n"] += 1
    return ref

# AUTH
def authorized(update):
    chat_id = update.effective_chat.id
    logger.info(f"Message from chat_id: {chat_id} | authorized: {AUTHORIZED_CHAT_ID}")
    return chat_id == AUTHORIZED_CHAT_ID

# HANDLERS
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "Medical Visit Tracker - Ready\n\n"
        "Send a photo to extract and save patient data\n\n"
        "Commands:\n"
        "/status - today's visit count\n"
        "/patients - this month's patient list\n"
        "/check [name] - summary for a patient\n"
        "/today - all visits added today\n"
        "/stats - ward and hospital breakdown\n"
        "/monthly - month-end summary\n"
        "/dupes - check for duplicates\n"
        "/help - this message"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "Commands:\n\n"
        "Photo - extract and save patient data\n"
        "/status - visits today and this month\n"
        "/patients - all patients this month\n"
        "/check [name] - visits for a patient\n"
        "/today - today's entries\n"
        "/stats - breakdown by ward and hospital\n"
        "/monthly - month-end summary\n"
        "/dupes - check for duplicates"
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text("Extracting patient data...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        photo_ref = next_photo_ref()
        patients = extract_from_image(bytes(image_bytes))
        if not patients:
            await update.message.reply_text("No patient data found. Try a clearer photo.")
            return
        records = get_all_records()
        added, dupes = [], []
        for p in patients:
            if is_duplicate(p, records):
                dupes.append(p.get("patientName", "Unknown"))
            else:
                append_row(p, photo_ref)
                added.append(p)
                records.append({
                    "Patient Name": p.get("patientName", ""),
                    "Date": p.get("date", ""),
                    "Hospital": p.get("hospital", "")
                })
        lines = []
        for p in added:
            ward = p.get("ward", "?")
            ke = p.get("keyEvents") or []
            inv = p.get("investigationsOrdered") or []
            lines.append(
                f"{p.get('patientName', 'Unknown')}\n"
                f"  {p.get('hospital', '?')} - {ward} ({'OPD' if ward == 'OPD' else 'IPD'})\n"
                f"  {p.get('date', '?')} - {p.get('diagnosis', '-')}\n"
                + (f"  Ix: {', '.join(inv)}\n" if inv else "")
                + (f"  Events: {', '.join([e['type'] for e in ke])}\n" if ke else "  Routine visit\n")
                + f"  {photo_ref}"
            )
        for name in dupes:
            lines.append(f"Duplicate skipped: {name}")
        await update.message.reply_text("\n\n".join(lines))
    except json.JSONDecodeError:
        await update.message.reply_text("Could not parse response. Try again.")
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await update.message.reply_text(f"Error: {str(e)[:300]}")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    records = get_all_records()
    today_str = date.today().strftime("%d/%m/%Y")
    m = date.today().month
    y = date.today().year
    today_recs = [r for r in records if r.get("Date") == today_str]
    month_recs = [r for r in records if _month_match(r.get("Date", ""), m, y)]
    opd_t = sum(1 for r in today_recs if r.get("OPD/IPD") == "OPD")
    ipd_t = len(today_recs) - opd_t
    opd_m = sum(1 for r in month_recs if r.get("OPD/IPD") == "OPD")
    ipd_m = len(month_recs) - opd_m
    hd_m  = sum(1 for r in month_recs if "Hemodialysis" in (r.get("Key Events (Type)") or ""))
    pts_m = len(set(r.get("Patient Name", "") for r in month_recs))
    await update.message.reply_text(
        f"Status - {date.today().strftime('%d %b %Y')}\n\n"
        f"Today\n"
        f"  Visits: {len(today_recs)}  OPD: {opd_t}  IPD: {ipd_t}\n\n"
        f"This Month\n"
        f"  Patients: {pts_m}  Visits: {len(month_recs)}\n"
        f"  OPD: {opd_m}  IPD: {ipd_m}  HD: {hd_m}"
    )

async def patients_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    records = get_all_records()
    m = date.today().month
    y = date.today().year
    month_recs = [r for r in records if _month_match(r.get("Date", ""), m, y)]
    pts = {}
    for r in month_recs:
        name = r.get("Patient Name", "?")
        if name not in pts:
            pts[name] = {"visits": 0, "opd": 0, "ipd": 0, "hd": 0}
        pts[name]["visits"] += 1
        if r.get("OPD/IPD") == "OPD":
            pts[name]["opd"] += 1
        else:
            pts[name]["ipd"] += 1
        if "Hemodialysis" in (r.get("Key Events (Type)") or ""):
            pts[name]["hd"] += 1
    if not pts:
        await update.message.reply_text("No patients this month yet.")
        return
    lines = [f"{len(pts)} Patients - {date.today().strftime('%B %Y')}\n"]
    for i, (name, d) in enumerate(sorted(pts.items()), 1):
        hd_str = f" HD:{d['hd']}" if d["hd"] else ""
        lines.append(f"{i}. {name} {d['visits']}x (OPD:{d['opd']} IPD:{d['ipd']}){hd_str}")
    await update.message.reply_text("\n".join(lines))

async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /check [name]\nExample: /check ramesh")
        return
    query = " ".join(context.args).lower()
    records = get_all_records()
    matches = [r for r in records if query in (r.get("Patient Name", "")).lower()]
    if not matches:
        await update.message.reply_text(f"No records found for '{query}'")
        return
    matches.sort(key=lambda r: r.get("Date", ""))
    name = matches[0].get("Patient Name", "?")
    lines = [f"{name} - {len(matches)} visit(s)\n"]
    for i, r in enumerate(matches, 1):
        ke = r.get("Key Events (Type)", "")
        lines.append(
            f"Visit {i} - {r.get('Date', '?')}\n"
            f"  {r.get('Ward', '?')} ({r.get('OPD/IPD', '?')}) {r.get('Hospital', '?')}\n"
            f"  Dx: {r.get('Diagnosis', '-')}\n"
            + (f"  Events: {ke}\n" if ke else "  Routine\n")
        )
    for chunk in _chunk("\n".join(lines)):
        await update.message.reply_text(chunk)

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    records = get_all_records()
    today_str = date.today().strftime("%d/%m/%Y")
    today_recs = [r for r in records if r.get("Date") == today_str]
    if not today_recs:
        await update.message.reply_text("No visits today yet. Send a photo!")
        return
    lines = [f"Today - {today_str}\n"]
    for r in today_recs:
        ke = r.get("Key Events (Type)", "")
        lines.append(
            f"- {r.get('Patient Name', '?')} {r.get('Ward', '?')} ({r.get('OPD/IPD', '?')})\n"
            f"  {r.get('Diagnosis', '-')}"
            + (f"\n  {ke}" if ke else "")
        )
    await update.message.reply_text("\n".join(lines))

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    records = get_all_records()
    m = date.today().month
    y = date.today().year
    month_recs = [r for r in records if _month_match(r.get("Date", ""), m, y)]
    if not month_recs:
        await update.message.reply_text("No data this month yet.")
        return
    ward_bd, hosp_bd, event_bd = {}, {}, {}
    for r in month_recs:
        w = r.get("Ward", "?")
        ward_bd[w] = ward_bd.get(w, 0) + 1
        h = r.get("Hospital", "?")
        hosp_bd[h] = hosp_bd.get(h, 0) + 1
        for ev in (r.get("Key Events (Type)") or "").split(" | "):
            if ev.strip():
                event_bd[ev.strip()] = event_bd.get(ev.strip(), 0) + 1
    total = len(month_recs)
    def bar(n):
        filled = round((n / total) * 10) if total else 0
        return "#" * filled + "-" * (10 - filled)
    ward_lines = "\n".join(f"  [{bar(v)}] {k}: {v}" for k, v in sorted(ward_bd.items(), key=lambda x: -x[1]))
    hosp_lines = "\n".join(f"  [{bar(v)}] {k}: {v}" for k, v in sorted(hosp_bd.items(), key=lambda x: -x[1]))
    ev_lines = "\n".join(f"  - {k}: {v}" for k, v in sorted(event_bd.items(), key=lambda x: -x[1])) or "  None"
    await update.message.reply_text(
        f"Stats - {date.today().strftime('%B %Y')}\n\n"
        f"By Ward\n{ward_lines}\n\n"
        f"By Hospital\n{hosp_lines}\n\n"
        f"Key Events\n{ev_lines}"
    )

async def monthly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    records = get_all_records()
    m = date.today().month
    y = date.today().year
    month_recs = [r for r in records if _month_match(r.get("Date", ""), m, y)]
    if not month_recs:
        await update.message.reply_text("No data this month.")
        return
    pts = {}
    for r in month_recs:
        name = r.get("Patient Name", "?")
        if name not in pts:
            pts[name] = []
        pts[name].append(r)
    lines = [
        f"Monthly Summary - {date.today().strftime('%B %Y')}",
        f"{len(pts)} patients / {len(month_recs)} visits\n"
    ]
    for name, visits in sorted(pts.items()):
        visits.sort(key=lambda r: r.get("Date", ""))
        opd = sum(1 for v in visits if v.get("OPD/IPD") == "OPD")
        ipd = len(visits) - opd
        hd  = sum(1 for v in visits if "Hemodialysis" in (v.get("Key Events (Type)") or ""))
        ke  = sum(1 for v in visits if v.get("Key Events (Type)", ""))
        dxs = list(dict.fromkeys(v.get("Diagnosis", "") for v in visits if v.get("Diagnosis", "")))
        lines.append(
            f"{name}\n"
            f"  {visits[0].get('Hospital', '?')} - {len(visits)}x (OPD:{opd} IPD:{ipd})\n"
            f"  Dx: {'; '.join(dxs[:2]) or '-'}\n"
            + (f"  HD: {hd} sessions\n" if hd else "")
            + (f"  Key events: {ke}\n" if ke else "")
            + f"  {visits[0].get('Date', '?')} to {visits[-1].get('Date', '?')}"
        )
    for chunk in _chunk("\n\n".join(lines)):
        await update.message.reply_text(chunk)

async def dupes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    records = get_all_records()
    seen, dupes = set(), []
    for r in records:
        key = f"{(r.get('Patient Name') or '').lower()}|{r.get('Date', '')}|{(r.get('Hospital') or '').lower()}"
        if key in seen:
            dupes.append(r)
        else:
            seen.add(key)
    if not dupes:
        await update.message.reply_text("No duplicates found.")
    else:
        lines = [f"{len(dupes)} Duplicates Found\n"]
        for r in dupes:
            lines.append(f"- {r.get('Patient Name', '?')} {r.get('Date', '?')} {r.get('Hospital', '?')}")
        await update.message.reply_text("\n".join(lines))

# UTILITIES
def _month_match(date_str, month, year):
    try:
        parts = date_str.split("/")
        return int(parts[1]) == month and int(parts[2]) == year
    except Exception:
        return False

def _chunk(text, limit=4000):
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line + "\n"
        else:
            current += line + "\n"
    if current:
        chunks.append(current)
    return chunks

# MAIN
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("help",     help_cmd))
    app.add_handler(CommandHandler("status",   status))
    app.add_handler(CommandHandler("patients", patients_cmd))
    app.add_handler(CommandHandler("check",    check_cmd))
    app.add_handler(CommandHandler("today",    today_cmd))
    app.add_handler(CommandHandler("stats",    stats_cmd))
    app.add_handler(CommandHandler("monthly",  monthly_cmd))
    app.add_handler(CommandHandler("dupes",    dupes_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Bot started - polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
