"""
Medical Visit Tracker - Telegram Bot v4
- Added: Referred By, Treatment Given, Procedures Advised columns
- Added: Investigation sheet photo support (separate Labs tab)
- Fixed: Ignores back-of-prescription images, won't extract doctor as patient
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
ANTHROPIC_MODEL      = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
PRIMARY_HOSPITAL     = "Care CHL Hospital"
DOCTOR_NAME          = "Shiv Shankar Sharma"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTION PROMPTS
# ─────────────────────────────────────────────────────────────────────────────

VISIT_PROMPT = f"""You are a Medical Visit Documentation Assistant for Dr. Shiv Shankar Sharma, a visiting nephrologist/intensivist in Indore, India.

IMPORTANT RULES:
1. If this image is the BACK of a prescription, a blank page, or contains only Dr. Shiv Shankar Sharma's own letterhead/signature with NO patient information — return an empty array: []
2. NEVER extract Dr. Shiv Shankar Sharma / Dr. Shiv S. Sharma / Dr. S.S. Sharma as a patient. He is the treating doctor, not the patient.
3. Patient names come from: ID stickers (top of inpatient sheets), or the "Name:" field on prescription paper.
4. Referred By: look in top-right corner of prescription for referring doctor name, or "Ref:" or "c/o" or "Ref by" field, or the primary consultant name on inpatient stickers.

Analyze this image and return ONLY a valid JSON array. No markdown, no backticks, no explanation.

For EACH patient visible:
{{
  "patientName": "Full patient name — NOT the doctor name",
  "patientId": "MRN or IP number from sticker, null if absent",
  "dob": "DD/MM/YYYY, null if absent",
  "age": "Age with M/F e.g. 54/M",
  "gender": "M/F/Other",
  "date": "Visit date DD/MM/YYYY — from notes or sticker, use today if absent",
  "hospital": "Hospital name — from sticker header or letterhead",
  "ward": "One of: ICU, CCU, PICU, HDU, General Ward, OPD, Emergency, Other",
  "referredBy": "Referring doctor name if visible, else null",
  "diagnosis": "Primary diagnosis or impression — look for: Impression, Dx, K/c/o, Known case, KICIO, Diagnosis",
  "clinicalNotes": "Complete summary: vitals (BP, weight, pulse), exam findings, active issues listed",
  "treatmentGiven": "Numbered list of medications exactly as written with doses and frequency e.g. 1. T. Telma-AM 40mg OD, 2. T. Sobisis 500mg BD",
  "investigationsOrdered": ["list of investigations advised today"],
  "keyFindings": ["important abnormal findings noted"],
  "proceduresAdvised": ["dialysis, transplant workup, fistula creation, biopsy, or any procedure advised — empty list if none"],
  "keyEvents": [
    {{
      "type": "One of: Hemodialysis, ICU Admission/Transfer, Antibiotic Change, Critical Lab Result, Imaging Finding, Procedure",
      "detail": "Specific detail with values, drug names, doses"
    }}
  ],
  "routineOnly": true or false
}}

routineOnly=true only if zero key events."""

LAB_PROMPT = f"""You are a Medical Lab Report Extractor for Dr. Shiv Shankar Sharma's nephrology practice.

IMPORTANT:
- Extract the patient name from the sticker or top of the sheet
- NEVER use Dr. Shiv Shankar Sharma as the patient name
- Extract ALL lab values visible with their dates
- For investigation flow sheets (serial results), extract each date column separately

Return ONLY a valid JSON object. No markdown, no backticks.

{{
  "patientName": "Patient name from sticker or sheet header",
  "patientId": "MRN/IP number if visible, null if absent",
  "hospital": "Hospital name if visible",
  "results": [
    {{
      "date": "DD/MM/YYYY",
      "parameter": "Test name e.g. Creatinine, Potassium, Hb",
      "value": "Numeric value as written",
      "unit": "Unit if visible e.g. mg/dL, mEq/L",
      "flag": "H if high, L if low, C if critical, N if normal — use clinical judgment for nephrology: Cr>3=H, K>5.5=H, K>6=C, Na<130=L, Hb<8=L"
    }}
  ],
  "imaging": [
    {{
      "date": "DD/MM/YYYY",
      "modality": "USG/CT/MRI/Echo/CXR",
      "finding": "Finding as written"
    }}
  ],
  "notes": "Any other relevant notes on the sheet"
}}"""

# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE API
# ─────────────────────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash-lite")

def call_claude(image_bytes, prompt):
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
            ]
        }],
        "generationConfig": {"maxOutputTokens": 3000}
    }
    response = requests.post(url, json=payload, timeout=45)
    logger.info(f"Gemini API status: {response.status_code}")
    if response.status_code != 200:
        logger.error(f"Gemini error: {response.text}")
        raise Exception(f"Gemini API {response.status_code}: {response.text[:200]}")
    text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"```json|```", "", text).strip()
    return text
```

**Also add to Railway Variables:**
```
GEMINI_MODEL = gemini-2.0-flash-lite

def extract_visit(image_bytes):
    text = call_claude(image_bytes, VISIT_PROMPT)
    return json.loads(text)

def extract_labs(image_bytes):
    text = call_claude(image_bytes, LAB_PROMPT)
    return json.loads(text)

# ─────────────────────────────────────────────────────────────────────────────
# GOOGLE SHEETS
# ─────────────────────────────────────────────────────────────────────────────

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

VISIT_HEADERS = [
    "ID", "Date", "Patient Name", "MRN", "DOB", "Age", "Gender",
    "Hospital", "Ward", "OPD/IPD", "Referred By",
    "Diagnosis", "Clinical Notes", "Treatment Given",
    "Investigations Ordered", "Key Findings", "Procedures Advised",
    "Key Events (Type)", "Key Events (Detail)",
    "Routine Only", "Photo Ref", "Added At"
]

LAB_HEADERS = [
    "ID", "Date", "Patient Name", "MRN", "Hospital",
    "Parameter", "Value", "Unit", "Flag", "Photo Ref", "Added At"
]

IMAGING_HEADERS = [
    "ID", "Date", "Patient Name", "MRN", "Hospital",
    "Modality", "Finding", "Photo Ref", "Added At"
]

def get_gc():
    creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(GOOGLE_SHEET_ID)

def get_or_create_sheet(sh, title, headers):
    try:
        ws = sh.worksheet(title)
        if ws.row_count < 1000:
            ws.add_rows(5000)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=5000, cols=len(headers))
        ws.append_row(headers)
        ws.freeze(rows=1)
    return ws

def get_visit_sheet():
    sh = get_gc()
    return get_or_create_sheet(sh, "Visits", VISIT_HEADERS)

def get_lab_sheet():
    sh = get_gc()
    return get_or_create_sheet(sh, "Labs", LAB_HEADERS)

def get_imaging_sheet():
    sh = get_gc()
    return get_or_create_sheet(sh, "Imaging", IMAGING_HEADERS)

def get_all_records():
    ws = get_visit_sheet()
    return ws.get_all_records()

def append_visit(entry, photo_ref):
    ws = get_visit_sheet()
    key_types  = " | ".join([e["type"]   for e in (entry.get("keyEvents") or [])])
    key_detail = " | ".join([e["detail"] for e in (entry.get("keyEvents") or [])])
    procs = "; ".join(entry.get("proceduresAdvised") or [])
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
        entry.get("referredBy", ""),
        entry.get("diagnosis", ""),
        entry.get("clinicalNotes", ""),
        ("; ".join(entry.get("treatmentGiven", [])) if isinstance(entry.get("treatmentGiven"), list) else entry.get("treatmentGiven", "")),
        "; ".join(entry.get("investigationsOrdered") or []),
        "; ".join(entry.get("keyFindings") or []),
        procs,
        key_types,
        key_detail,
        str(entry.get("routineOnly", False)),
        photo_ref,
        datetime.now().isoformat()
    ]
    ws.append_row(row)

def append_labs(lab_data, photo_ref):
    ws_lab = get_lab_sheet()
    ws_img = get_imaging_sheet()
    name    = lab_data.get("patientName", "Unknown")
    mrn     = lab_data.get("patientId", "")
    hospital= lab_data.get("hospital", "")
    added   = datetime.now().isoformat()
    for r in (lab_data.get("results") or []):
        ws_lab.append_row([
            f"L-{datetime.now().strftime('%Y%m%d%H%M%S%f')[:18]}",
            r.get("date", ""),
            name, mrn, hospital,
            r.get("parameter", ""),
            r.get("value", ""),
            r.get("unit", ""),
            r.get("flag", ""),
            photo_ref, added
        ])
    for i in (lab_data.get("imaging") or []):
        ws_img.append_row([
            f"I-{datetime.now().strftime('%Y%m%d%H%M%S%f')[:18]}",
            i.get("date", ""),
            name, mrn, hospital,
            i.get("modality", ""),
            i.get("finding", ""),
            photo_ref, added
        ])

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

# ─────────────────────────────────────────────────────────────────────────────
# PHOTO COUNTER
# ─────────────────────────────────────────────────────────────────────────────

_photo_counter = {"n": 1}

def next_photo_ref():
    ref = f"IMG-{str(_photo_counter['n']).zfill(3)}"
    _photo_counter["n"] += 1
    return ref

# ─────────────────────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────────────────────

def authorized(update):
    chat_id = update.effective_chat.id
    logger.info(f"Message from chat_id: {chat_id} | authorized: {AUTHORIZED_CHAT_ID}")
    return chat_id == AUTHORIZED_CHAT_ID

# ─────────────────────────────────────────────────────────────────────────────
# HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "Medical Visit Tracker - Ready\n\n"
        "Send a PHOTO to extract and save patient data\n"
        "Send /lab before a photo to save it as investigation results\n\n"
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
        "Photo - extract visit notes and save\n"
        "/lab then Photo - extract investigation sheet\n"
        "/status - visits today and this month\n"
        "/patients - all patients this month\n"
        "/check [name] - visits for a patient\n"
        "/today - today's entries\n"
        "/stats - breakdown by ward and hospital\n"
        "/monthly - month-end summary\n"
        "/dupes - check for duplicates"
    )

async def lab_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    context.user_data["mode"] = "lab"
    await update.message.reply_text(
        "Lab mode ON - send your investigation sheet photo now.\n"
        "I will extract all results into the Labs tab."
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    mode = context.user_data.get("mode", "visit")
    context.user_data["mode"] = "visit"  # reset after use

    if mode == "lab":
        await handle_lab_photo(update, context)
    else:
        await handle_visit_photo(update, context)

async def handle_visit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Extracting patient data...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        photo_ref = next_photo_ref()
        patients = extract_visit(bytes(image_bytes))

        if not patients:
            await update.message.reply_text(
                "No patient data found.\n"
                "This looks like a blank page or back of prescription — skipped."
            )
            return

        records = get_all_records()
        added, dupes = [], []

        for p in patients:
            # Safety check — never save doctor as patient
            name = (p.get("patientName") or "").strip()
            if any(x in name.lower() for x in ["shiv shankar", "shiv s. sharma", "s.s. sharma", "dr. shiv"]):
                logger.info(f"Skipping doctor name: {name}")
                continue
            if is_duplicate(p, records):
                dupes.append(name)
            else:
                append_visit(p, photo_ref)
                added.append(p)
                records.append({
                    "Patient Name": name,
                    "Date": p.get("date", ""),
                    "Hospital": p.get("hospital", "")
                })

        lines = []
        for p in added:
            ward = p.get("ward", "?")
            ke   = p.get("keyEvents") or []
            inv  = p.get("investigationsOrdered") or []
            proc = p.get("proceduresAdvised") or []
            ref  = p.get("referredBy", "")
            lines.append(
                f"{p.get('patientName', 'Unknown')}\n"
                f"  {p.get('hospital', '?')} - {ward} ({'OPD' if ward == 'OPD' else 'IPD'})\n"
                f"  {p.get('date', '?')} - {p.get('diagnosis', '-')}\n"
                + (f"  Ref: {ref}\n" if ref else "")
                + (f"  Ix: {', '.join(inv)}\n" if inv else "")
                + (f"  Proc: {', '.join(proc)}\n" if proc else "")
                + (f"  Events: {', '.join([e['type'] for e in ke])}\n" if ke else "  Routine visit\n")
                + f"  {photo_ref}"
            )
        for name in dupes:
            lines.append(f"Duplicate skipped: {name}")

        if not lines:
            await update.message.reply_text("No new patient entries saved.")
        else:
            await update.message.reply_text("\n\n".join(lines))

    except json.JSONDecodeError:
        await update.message.reply_text("Could not parse response. Try again.")
    except Exception as e:
        logger.error(f"Visit photo error: {e}")
        await update.message.reply_text(f"Error: {str(e)[:300]}")

async def handle_lab_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Extracting investigation results...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        photo_ref = next_photo_ref()
        lab_data = extract_labs(bytes(image_bytes))

        patient = lab_data.get("patientName", "Unknown")
        results = lab_data.get("results") or []
        imaging = lab_data.get("imaging") or []

        if not results and not imaging:
            await update.message.reply_text("No lab results found in this image.")
            return

        append_labs(lab_data, photo_ref)

        # Build summary
        critical = [r for r in results if r.get("flag") == "C"]
        high     = [r for r in results if r.get("flag") == "H"]
        low      = [r for r in results if r.get("flag") == "L"]

        lines = [
            f"Labs saved - {patient}\n"
            f"  {len(results)} results + {len(imaging)} imaging\n"
            f"  {photo_ref}"
        ]
        if critical:
            lines.append("CRITICAL: " + ", ".join(f"{r['parameter']}={r['value']}" for r in critical))
        if high:
            lines.append("High: " + ", ".join(f"{r['parameter']}={r['value']}" for r in high))
        if low:
            lines.append("Low: " + ", ".join(f"{r['parameter']}={r['value']}" for r in low))
        if imaging:
            lines.append("Imaging: " + "; ".join(f"{i['modality']}: {i['finding']}" for i in imaging))

        await update.message.reply_text("\n".join(lines))

    except json.JSONDecodeError:
        await update.message.reply_text("Could not parse lab results. Try again.")
    except Exception as e:
        logger.error(f"Lab photo error: {e}")
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
        ke   = r.get("Key Events (Type)", "")
        proc = r.get("Procedures Advised", "")
        ref  = r.get("Referred By", "")
        lines.append(
            f"Visit {i} - {r.get('Date', '?')}\n"
            f"  {r.get('Ward', '?')} ({r.get('OPD/IPD', '?')}) {r.get('Hospital', '?')}\n"
            f"  Dx: {r.get('Diagnosis', '-')}\n"
            + (f"  Ref: {ref}\n" if ref else "")
            + (f"  Proc: {proc}\n" if proc else "")
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
    ward_bd, hosp_bd, event_bd, proc_bd = {}, {}, {}, {}
    for r in month_recs:
        w = r.get("Ward", "?")
        ward_bd[w] = ward_bd.get(w, 0) + 1
        h = r.get("Hospital", "?")
        hosp_bd[h] = hosp_bd.get(h, 0) + 1
        for ev in (r.get("Key Events (Type)") or "").split(" | "):
            if ev.strip():
                event_bd[ev.strip()] = event_bd.get(ev.strip(), 0) + 1
        for pr in (r.get("Procedures Advised") or "").split(";"):
            if pr.strip():
                proc_bd[pr.strip()] = proc_bd.get(pr.strip(), 0) + 1
    total = len(month_recs)
    def bar(n):
        filled = round((n / total) * 10) if total else 0
        return "#" * filled + "-" * (10 - filled)
    ward_lines = "\n".join(f"  [{bar(v)}] {k}: {v}" for k, v in sorted(ward_bd.items(), key=lambda x: -x[1]))
    hosp_lines = "\n".join(f"  [{bar(v)}] {k}: {v}" for k, v in sorted(hosp_bd.items(), key=lambda x: -x[1]))
    ev_lines   = "\n".join(f"  - {k}: {v}" for k, v in sorted(event_bd.items(), key=lambda x: -x[1])) or "  None"
    proc_lines = "\n".join(f"  - {k}: {v}" for k, v in sorted(proc_bd.items(), key=lambda x: -x[1])) or "  None"
    await update.message.reply_text(
        f"Stats - {date.today().strftime('%B %Y')}\n\n"
        f"By Ward\n{ward_lines}\n\n"
        f"By Hospital\n{hosp_lines}\n\n"
        f"Key Events\n{ev_lines}\n\n"
        f"Procedures Advised\n{proc_lines}"
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
        opd  = sum(1 for v in visits if v.get("OPD/IPD") == "OPD")
        ipd  = len(visits) - opd
        hd   = sum(1 for v in visits if "Hemodialysis" in (v.get("Key Events (Type)") or ""))
        ke   = sum(1 for v in visits if v.get("Key Events (Type)", ""))
        dxs  = list(dict.fromkeys(v.get("Diagnosis", "") for v in visits if v.get("Diagnosis", "")))
        proc = list(dict.fromkeys(v.get("Procedures Advised", "") for v in visits if v.get("Procedures Advised", "")))
        lines.append(
            f"{name}\n"
            f"  {visits[0].get('Hospital', '?')} - {len(visits)}x (OPD:{opd} IPD:{ipd})\n"
            f"  Dx: {'; '.join(dxs[:2]) or '-'}\n"
            + (f"  HD: {hd} sessions\n" if hd else "")
            + (f"  Proc: {'; '.join(proc[:2])}\n" if proc else "")
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

# ─────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

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

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("help",     help_cmd))
    app.add_handler(CommandHandler("lab",      lab_mode))
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
