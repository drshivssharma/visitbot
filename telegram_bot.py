import os, json, re, logging, base64
from datetime import datetime, date
import requests
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

PRIMARY_HOSPITAL     = "Care CHL Hospital"
DOCTOR_NAME          = "Shiv Shankar Sharma"
TELEGRAM_TOKEN       = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY       = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL         = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
GOOGLE_SHEET_ID      = os.environ.get("GOOGLE_SHEET_ID")
AUTHORIZED_CHAT_ID   = int(os.environ.get("AUTHORIZED_CHAT_ID", "0"))
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────────────────────────────────────────

DOC_TYPE_PROMPT = """Look at this image and identify the document type.
Return ONLY one of these exact words:
prescription
inpatient
investigation
discharge
unknown"""

VISIT_PROMPT = """You are extracting structured data from a medical document for Dr. Shiv Shankar Sharma, a nephrologist in Indore, India.

STRICT RULES:
1. Only extract text that clearly and visibly exists in the image.
2. If any field is uncertain or unreadable — return null. NEVER invent or guess.
3. Do NOT hallucinate diagnosis, drug names, investigations, or any values.
4. NEVER use Dr. Shiv Shankar Sharma as the patient name — he is the treating doctor.
5. If image is blank, back of prescription, or has no patient data — return [].
6. Patient name comes from: ID sticker on inpatient sheet, or Name field on prescription.
7. Referred By: look for Ref, Ref by, c/o, sent by in top-right corner or header.
8. Phone: extract longest 10-12 digit number visible anywhere on the document.
9. Confidence: HIGH if name+date+diagnosis all clearly readable, MEDIUM if any one is unclear, LOW if multiple fields uncertain.

Return ONLY a valid JSON array. No markdown, no backticks, no explanation.

[{
  "docType": "prescription or inpatient or investigation or discharge",
  "confidence": "HIGH or MEDIUM or LOW",
  "patientName": "Full patient name or null",
  "patientId": "MRN or IP number or null",
  "dob": "DD/MM/YYYY or null",
  "age": "e.g. 54/M or null",
  "gender": "M or F or Other or null",
  "visitDate": "DD/MM/YYYY from document — null if not found, do not use today",
  "hospital": "Hospital name from letterhead or sticker or null",
  "ward": "One of: ICU, CCU, PICU, HDU, General Ward, OPD, Emergency, Other",
  "referredBy": "Referring doctor name or null",
  "phone": "10-12 digit number only, no spaces or symbols, or null",
  "diagnosis": "Clearly written diagnosis or impression — null if uncertain",
  "clinicalNotes": "Vitals, findings, active issues — only what is clearly written",
  "treatmentGiven": "Numbered medication list with doses exactly as written, or null",
  "investigationsOrdered": ["only clearly written investigation names"],
  "keyFindings": ["clearly noted abnormal findings only"],
  "proceduresAdvised": ["dialysis, fistula, biopsy, transplant workup — empty list if none"],
  "keyEvents": [{"type": "Hemodialysis or ICU Admission/Transfer or Antibiotic Change or Critical Lab Result or Imaging Finding or Procedure", "detail": "specific detail"}],
  "routineOnly": true
}]

routineOnly=true only if keyEvents is empty."""

LAB_PROMPT = """You are extracting lab results from a medical investigation sheet.

STRICT RULES:
1. Only extract values that are clearly visible and readable.
2. If uncertain — return null. Never guess or invent values.
3. NEVER use Dr. Shiv Shankar Sharma as the patient name.
4. For serial flow sheets extract each date column as a separate result entry.

Return ONLY a valid JSON object. No markdown, no backticks.

{
  "patientName": "Patient name from sticker or sheet header, or null",
  "patientId": "MRN or IP number if visible, or null",
  "hospital": "Hospital name if visible, or null",
  "results": [
    {
      "date": "DD/MM/YYYY",
      "parameter": "Test name e.g. Creatinine",
      "value": "Numeric value as written",
      "unit": "Unit if visible e.g. mg/dL",
      "flag": "C if critical, H if high, L if low, N if normal — use nephrology ranges: K>6=C, K>5.5=H, Cr>3=H, Na<130=L, Hb<8=L"
    }
  ],
  "imaging": [
    {
      "date": "DD/MM/YYYY",
      "modality": "USG or CT or MRI or Echo or CXR",
      "finding": "Finding exactly as written"
    }
  ],
  "notes": "Any other relevant notes visible on the sheet"
}"""

# ─────────────────────────────────────────────────────────────────────────────
# OPENAI API
# ─────────────────────────────────────────────────────────────────────────────

def call_openai(image_bytes, prompt):
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": OPENAI_MODEL,
            "max_tokens": 3000,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}",
                        "detail": "high"
                    }}
                ]
            }]
        },
        timeout=45
    )
    logger.info(f"OpenAI API status: {response.status_code}")
    if response.status_code != 200:
        logger.error(f"OpenAI error: {response.text}")
        raise Exception(f"OpenAI API {response.status_code}: {response.text[:200]}")
    text = response.json()["choices"][0]["message"]["content"]
    text = re.sub(r"```json|```", "", text).strip()
    return text


def extract_visit(image_bytes):
    text = call_openai(image_bytes, VISIT_PROMPT)
    return json.loads(text)


def extract_labs(image_bytes):
    text = call_openai(image_bytes, LAB_PROMPT)
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
    "visit_id", "visit_date", "entry_timestamp",
    "patient_id", "patient_name", "mrn", "dob", "age", "gender",
    "hospital", "ward", "encounter_type",
    "referred_by", "phone",
    "diagnosis_text", "clinical_notes", "treatment",
    "investigations", "key_findings", "procedures",
    "key_event_type", "key_event_detail",
    "routine_only", "doc_type", "confidence", "photo_ref"
]

PATIENT_MASTER_HEADERS = [
    "patient_id", "patient_name", "mrn", "phone", "age", "gender",
    "primary_hospital", "first_visit", "last_visit", "total_visits",
    "last_diagnosis", "created_at"
]

LAB_HEADERS = [
    "lab_id", "visit_date", "entry_timestamp",
    "patient_name", "mrn", "hospital",
    "parameter", "value", "unit", "flag", "photo_ref"
]

IMAGING_HEADERS = [
    "imaging_id", "visit_date", "entry_timestamp",
    "patient_name", "mrn", "hospital",
    "modality", "finding", "photo_ref"
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


def get_patient_master():
    sh = get_gc()
    return get_or_create_sheet(sh, "Patient_Master", PATIENT_MASTER_HEADERS)


def get_lab_sheet():
    sh = get_gc()
    return get_or_create_sheet(sh, "Labs", LAB_HEADERS)


def get_imaging_sheet():
    sh = get_gc()
    return get_or_create_sheet(sh, "Imaging", IMAGING_HEADERS)


def get_all_visits():
    ws = get_visit_sheet()
    return ws.get_all_records()


def get_all_patients():
    ws = get_patient_master()
    return ws.get_all_records()


def match_patient(name, phone, mrn, patients):
    """Match by MRN first, then name+phone, then name only."""
    name_clean = (name or "").strip().lower()
    phone_clean = re.sub(r"\D", "", phone or "")
    mrn_clean = (mrn or "").strip()
    for p in patients:
        if mrn_clean and mrn_clean == (p.get("mrn") or "").strip():
            return p.get("patient_id")
    for p in patients:
        if (phone_clean and len(phone_clean) >= 10 and
                phone_clean == re.sub(r"\D", "", p.get("phone") or "") and
                name_clean == (p.get("patient_name") or "").strip().lower()):
            return p.get("patient_id")
    for p in patients:
        if name_clean and name_clean == (p.get("patient_name") or "").strip().lower():
            return p.get("patient_id")
    return None


def upsert_patient(entry, visit_date):
    ws = get_patient_master()
    patients = ws.get_all_records()
    name  = (entry.get("patientName") or "").strip()
    phone = re.sub(r"\D", "", entry.get("phone") or "")
    mrn   = (entry.get("patientId") or "").strip()
    hosp  = (entry.get("hospital") or PRIMARY_HOSPITAL).strip()
    diag  = (entry.get("diagnosis") or "").strip()
    now   = datetime.now().isoformat()

    patient_id = match_patient(name, phone, mrn, patients)

    if patient_id:
        # Update existing patient
        for i, p in enumerate(patients, start=2):
            if p.get("patient_id") == patient_id:
                ws.update_cell(i, PATIENT_MASTER_HEADERS.index("last_visit") + 1, visit_date)
                ws.update_cell(i, PATIENT_MASTER_HEADERS.index("total_visits") + 1,
                               int(p.get("total_visits") or 0) + 1)
                ws.update_cell(i, PATIENT_MASTER_HEADERS.index("last_diagnosis") + 1, diag)
                if phone:
                    ws.update_cell(i, PATIENT_MASTER_HEADERS.index("phone") + 1, phone)
                break
    else:
        # Create new patient
        patient_id = f"PAT{datetime.now().strftime('%Y%m%d%H%M%S')}"
        ws.append_row([
            patient_id, name, mrn, phone,
            entry.get("age", ""), entry.get("gender", ""),
            hosp, visit_date, visit_date, 1, diag, now
        ])

    return patient_id


def append_visit(entry, photo_ref, patient_id):
    ws = get_visit_sheet()
    visit_date = entry.get("visitDate") or date.today().strftime("%d/%m/%Y")
    key_types  = " | ".join([e.get("type", "")   for e in (entry.get("keyEvents") or [])])
    key_detail = " | ".join([e.get("detail", "") for e in (entry.get("keyEvents") or [])])
    procs      = "; ".join(entry.get("proceduresAdvised") or [])
    treatment  = entry.get("treatmentGiven", "")
    if isinstance(treatment, list):
        treatment = "; ".join(treatment)
    visit_id = f"VIS{datetime.now().strftime('%Y%m%d%H%M%S')}"
    row = [
        visit_id,
        visit_date,
        datetime.now().isoformat(),
        patient_id,
        entry.get("patientName", ""),
        entry.get("patientId", ""),
        entry.get("dob", ""),
        entry.get("age", ""),
        entry.get("gender", ""),
        entry.get("hospital", PRIMARY_HOSPITAL),
        entry.get("ward", "Other"),
        "OPD" if entry.get("ward") == "OPD" else "IPD",
        entry.get("referredBy", ""),
        entry.get("phone", ""),
        entry.get("diagnosis", ""),
        entry.get("clinicalNotes", ""),
        treatment,
        "; ".join(entry.get("investigationsOrdered") or []),
        "; ".join(entry.get("keyFindings") or []),
        procs,
        key_types,
        key_detail,
        str(entry.get("routineOnly", True)),
        entry.get("docType", ""),
        entry.get("confidence", ""),
        photo_ref
    ]
    ws.append_row(row)
    return visit_id, visit_date


def append_labs(lab_data, photo_ref):
    ws_lab = get_lab_sheet()
    ws_img = get_imaging_sheet()
    name    = lab_data.get("patientName", "Unknown")
    mrn     = lab_data.get("patientId", "")
    hospital= lab_data.get("hospital", "")
    added   = datetime.now().isoformat()
    for r in (lab_data.get("results") or []):
        ws_lab.append_row([
            f"L{datetime.now().strftime('%Y%m%d%H%M%S%f')[:18]}",
            r.get("date", ""), added,
            name, mrn, hospital,
            r.get("parameter", ""), r.get("value", ""),
            r.get("unit", ""), r.get("flag", ""),
            photo_ref
        ])
    for i in (lab_data.get("imaging") or []):
        ws_img.append_row([
            f"I{datetime.now().strftime('%Y%m%d%H%M%S%f')[:18]}",
            i.get("date", ""), added,
            name, mrn, hospital,
            i.get("modality", ""), i.get("finding", ""),
            photo_ref
        ])


def is_duplicate(entry, records):
    name = (entry.get("patientName") or "").strip().lower()
    dt   = entry.get("visitDate", "")
    hosp = (entry.get("hospital") or "").strip().lower()
    mrn  = (entry.get("patientId") or "").strip()
    for r in records:
        if mrn and mrn == (r.get("mrn") or "").strip():
            if r.get("visit_date") == dt:
                return True
        if (
            (r.get("patient_name") or "").strip().lower() == name and
            r.get("visit_date") == dt and
            (r.get("hospital") or "").strip().lower() == hosp
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
        "Medical Visit Tracker v5 - Ready\n\n"
        "Send a PHOTO to extract and save patient data\n"
        "Send /lab before a photo to save investigation results\n\n"
        "Commands:\n"
        "/last - last 5 entries\n"
        "/today - all visits today\n"
        "/status - today and month counts\n"
        "/patients - this month patient list\n"
        "/check [name] - full history for a patient\n"
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
        "Photo - extract visit and save\n"
        "/lab then Photo - extract investigation sheet\n"
        "/last - last 5 entries added\n"
        "/today - today's visits\n"
        "/status - counts today and this month\n"
        "/patients - all patients this month\n"
        "/check [name] - full visit history\n"
        "/stats - breakdown by ward and hospital\n"
        "/monthly - month-end summary\n"
        "/dupes - find duplicates"
    )


async def lab_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    context.user_data["mode"] = "lab"
    await update.message.reply_text("Lab mode ON - send investigation sheet photo now.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    mode = context.user_data.get("mode", "visit")
    context.user_data["mode"] = "visit"
    if mode == "lab":
        await handle_lab_photo(update, context)
    else:
        await handle_visit_photo(update, context)


async def handle_visit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Extracting patient data...")
    try:
        photo = update.message.photo[-1]
        file  = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        photo_ref   = next_photo_ref()
        patients    = extract_visit(bytes(image_bytes))

        if not patients:
            await update.message.reply_text(
                "No patient data found.\n"
                "Blank page or back of prescription - skipped."
            )
            return

        records  = get_all_visits()
        patients_master = get_all_patients()
        added, dupes, low_conf = [], [], []

        for p in patients:
            name = (p.get("patientName") or "").strip()
            if any(x in name.lower() for x in ["shiv shankar", "shiv s. sharma", "s.s. sharma", "dr. shiv"]):
                logger.info(f"Skipping doctor name: {name}")
                continue
            if not name:
                continue
            conf = p.get("confidence", "HIGH")
            if is_duplicate(p, records):
                dupes.append(name)
                continue
            patient_id = upsert_patient(p, p.get("visitDate") or date.today().strftime("%d/%m/%Y"))
            visit_id, visit_date = append_visit(p, photo_ref, patient_id)
            added.append(p)
            if conf == "LOW":
                low_conf.append(name)
            records.append({
                "patient_name": name,
                "visit_date": p.get("visitDate", ""),
                "hospital": p.get("hospital", ""),
                "mrn": p.get("patientId", "")
            })

        lines = []
        for p in added:
            ward  = p.get("ward", "?")
            ke    = p.get("keyEvents") or []
            inv   = p.get("investigationsOrdered") or []
            proc  = p.get("proceduresAdvised") or []
            ref   = p.get("referredBy", "")
            phone = p.get("phone", "")
            conf  = p.get("confidence", "HIGH")
            conf_flag = " LOW CONFIDENCE - please verify" if conf == "LOW" else (" (check)" if conf == "MEDIUM" else "")
            lines.append(
                f"{p.get('patientName', 'Unknown')}{conf_flag}\n"
                f"  {p.get('hospital', '?')} - {ward}\n"
                f"  {p.get('visitDate', '?')} - {p.get('diagnosis', '-')}\n"
                + (f"  Ref: {ref}\n" if ref else "")
                + (f"  Ph: {phone}\n" if phone else "")
                + (f"  Ix: {', '.join(inv)}\n" if inv else "")
                + (f"  Proc: {', '.join(proc)}\n" if proc else "")
                + (f"  Events: {', '.join([e.get('type','') for e in ke])}\n" if ke else "  Routine\n")
                + f"  {photo_ref}"
            )
        for name in dupes:
            lines.append(f"Duplicate skipped: {name}")

        if not lines:
            await update.message.reply_text("No new entries saved.")
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
        file  = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        photo_ref   = next_photo_ref()
        lab_data    = extract_labs(bytes(image_bytes))

        patient = lab_data.get("patientName", "Unknown")
        results = lab_data.get("results") or []
        imaging = lab_data.get("imaging") or []

        if not results and not imaging:
            await update.message.reply_text("No lab results found in this image.")
            return

        append_labs(lab_data, photo_ref)

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


async def last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    records = get_all_visits()
    if not records:
        await update.message.reply_text("No entries yet.")
        return
    last5 = records[-5:][::-1]
    lines = ["Last 5 entries:\n"]
    for r in last5:
        conf = r.get("confidence", "")
        conf_flag = " LOW CONF" if conf == "LOW" else ""
        lines.append(
            f"- {r.get('patient_name', '?')}{conf_flag}\n"
            f"  {r.get('visit_date', '?')} {r.get('hospital', '?')} {r.get('ward', '?')}\n"
            f"  {r.get('diagnosis_text', '-')}"
        )
    await update.message.reply_text("\n\n".join(lines))


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    records   = get_all_visits()
    today_str = date.today().strftime("%d/%m/%Y")
    m = date.today().month
    y = date.today().year
    today_recs = [r for r in records if r.get("visit_date") == today_str]
    month_recs = [r for r in records if _month_match(r.get("visit_date", ""), m, y)]
    opd_t  = sum(1 for r in today_recs if r.get("encounter_type") == "OPD")
    ipd_t  = len(today_recs) - opd_t
    opd_m  = sum(1 for r in month_recs if r.get("encounter_type") == "OPD")
    ipd_m  = len(month_recs) - opd_m
    hd_m   = sum(1 for r in month_recs if "Hemodialysis" in (r.get("key_event_type") or ""))
    pts_m  = len(set(r.get("patient_name", "") for r in month_recs))
    low_c  = sum(1 for r in month_recs if r.get("confidence") == "LOW")
    await update.message.reply_text(
        f"Status - {date.today().strftime('%d %b %Y')}\n\n"
        f"Today\n"
        f"  Visits: {len(today_recs)}  OPD: {opd_t}  IPD: {ipd_t}\n\n"
        f"This Month\n"
        f"  Patients: {pts_m}  Visits: {len(month_recs)}\n"
        f"  OPD: {opd_m}  IPD: {ipd_m}  HD: {hd_m}\n"
        + (f"  Low confidence entries: {low_c} - please verify\n" if low_c else "")
    )


async def patients_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    records = get_all_visits()
    m = date.today().month
    y = date.today().year
    month_recs = [r for r in records if _month_match(r.get("visit_date", ""), m, y)]
    pts = {}
    for r in month_recs:
        name = r.get("patient_name", "?")
        if name not in pts:
            pts[name] = {"visits": 0, "opd": 0, "ipd": 0, "hd": 0}
        pts[name]["visits"] += 1
        if r.get("encounter_type") == "OPD":
            pts[name]["opd"] += 1
        else:
            pts[name]["ipd"] += 1
        if "Hemodialysis" in (r.get("key_event_type") or ""):
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
        await update.message.reply_text("Usage: /check [name]")
        return
    query   = " ".join(context.args).lower()
    records = get_all_visits()
    matches = [r for r in records if query in (r.get("patient_name", "")).lower()]
    if not matches:
        await update.message.reply_text(f"No records found for '{query}'")
        return
    matches.sort(key=lambda r: r.get("visit_date", ""))
    name  = matches[0].get("patient_name", "?")
    lines = [f"{name} - {len(matches)} visit(s)\n"]
    for i, r in enumerate(matches, 1):
        ke   = r.get("key_event_type", "")
        proc = r.get("procedures", "")
        ref  = r.get("referred_by", "")
        conf = r.get("confidence", "")
        conf_flag = " LOW CONF" if conf == "LOW" else ""
        lines.append(
            f"Visit {i} - {r.get('visit_date', '?')}{conf_flag}\n"
            f"  {r.get('ward', '?')} ({r.get('encounter_type', '?')}) {r.get('hospital', '?')}\n"
            f"  Dx: {r.get('diagnosis_text', '-')}\n"
            + (f"  Ref: {ref}\n" if ref else "")
            + (f"  Proc: {proc}\n" if proc else "")
            + (f"  Events: {ke}\n" if ke else "  Routine\n")
        )
    for chunk in _chunk("\n".join(lines)):
        await update.message.reply_text(chunk)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    records   = get_all_visits()
    today_str = date.today().strftime("%d/%m/%Y")
    today_recs = [r for r in records if r.get("visit_date") == today_str]
    if not today_recs:
        await update.message.reply_text("No visits today yet.")
        return
    lines = [f"Today - {today_str} ({len(today_recs)} visits)\n"]
    for r in today_recs:
        ke   = r.get("key_event_type", "")
        conf = r.get("confidence", "")
        conf_flag = " LOW CONF" if conf == "LOW" else ""
        lines.append(
            f"- {r.get('patient_name', '?')}{conf_flag} {r.get('ward', '?')} ({r.get('encounter_type', '?')})\n"
            f"  {r.get('diagnosis_text', '-')}"
            + (f"\n  {ke}" if ke else "")
        )
    await update.message.reply_text("\n".join(lines))


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    records = get_all_visits()
    m = date.today().month
    y = date.today().year
    month_recs = [r for r in records if _month_match(r.get("visit_date", ""), m, y)]
    if not month_recs:
        await update.message.reply_text("No data this month yet.")
        return
    ward_bd, hosp_bd, event_bd, proc_bd = {}, {}, {}, {}
    for r in month_recs:
        w = r.get("ward", "?")
        ward_bd[w] = ward_bd.get(w, 0) + 1
        h = r.get("hospital", "?")
        hosp_bd[h] = hosp_bd.get(h, 0) + 1
        for ev in (r.get("key_event_type") or "").split(" | "):
            if ev.strip():
                event_bd[ev.strip()] = event_bd.get(ev.strip(), 0) + 1
        for pr in (r.get("procedures") or "").split(";"):
            if pr.strip():
                proc_bd[pr.strip()] = proc_bd.get(pr.strip(), 0) + 1
    total      = len(month_recs)
    ward_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(ward_bd.items(),  key=lambda x: -x[1]))
    hosp_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(hosp_bd.items(),  key=lambda x: -x[1]))
    ev_lines   = "\n".join(f"  {k}: {v}" for k, v in sorted(event_bd.items(), key=lambda x: -x[1])) or "  None"
    proc_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(proc_bd.items(),  key=lambda x: -x[1])) or "  None"
    await update.message.reply_text(
        f"Stats - {date.today().strftime('%B %Y')}\n\n"
        f"By Ward ({total} visits)\n{ward_lines}\n\n"
        f"By Hospital\n{hosp_lines}\n\n"
        f"Key Events\n{ev_lines}\n\n"
        f"Procedures\n{proc_lines}"
    )


async def monthly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    records = get_all_visits()
    m = date.today().month
    y = date.today().year
    month_recs = [r for r in records if _month_match(r.get("visit_date", ""), m, y)]
    if not month_recs:
        await update.message.reply_text("No data this month.")
        return
    pts = {}
    for r in month_recs:
        name = r.get("patient_name", "?")
        if name not in pts:
            pts[name] = []
        pts[name].append(r)
    lines = [
        f"Monthly Summary - {date.today().strftime('%B %Y')}",
        f"{len(pts)} patients / {len(month_recs)} visits\n"
    ]
    for name, visits in sorted(pts.items()):
        visits.sort(key=lambda r: r.get("visit_date", ""))
        opd  = sum(1 for v in visits if v.get("encounter_type") == "OPD")
        ipd  = len(visits) - opd
        hd   = sum(1 for v in visits if "Hemodialysis" in (v.get("key_event_type") or ""))
        ke   = sum(1 for v in visits if v.get("key_event_type", ""))
        dxs  = list(dict.fromkeys(v.get("diagnosis_text", "") for v in visits if v.get("diagnosis_text", "")))
        proc = list(dict.fromkeys(v.get("procedures", "") for v in visits if v.get("procedures", "")))
        lines.append(
            f"{name}\n"
            f"  {visits[0].get('hospital', '?')} - {len(visits)}x (OPD:{opd} IPD:{ipd})\n"
            f"  Dx: {'; '.join(dxs[:2]) or '-'}\n"
            + (f"  HD: {hd} sessions\n" if hd else "")
            + (f"  Proc: {'; '.join(proc[:2])}\n" if proc else "")
            + (f"  Key events: {ke}\n" if ke else "")
            + f"  {visits[0].get('visit_date', '?')} to {visits[-1].get('visit_date', '?')}"
        )
    for chunk in _chunk("\n\n".join(lines)):
        await update.message.reply_text(chunk)


async def dupes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    records = get_all_visits()
    seen, dupes = set(), []
    for r in records:
        key = f"{(r.get('patient_name') or '').lower()}|{r.get('visit_date', '')}|{(r.get('hospital') or '').lower()}"
        if key in seen:
            dupes.append(r)
        else:
            seen.add(key)
    if not dupes:
        await update.message.reply_text("No duplicates found.")
    else:
        lines = [f"{len(dupes)} Duplicates Found\n"]
        for r in dupes:
            lines.append(f"- {r.get('patient_name', '?')} {r.get('visit_date', '?')} {r.get('hospital', '?')}")
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
    app.add_handler(CommandHandler("last",     last_cmd))
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
