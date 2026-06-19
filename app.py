import base64
import io
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime
from urllib.parse import quote_plus

import pandas as pd
import streamlit as st
from PIL import Image
from dotenv import load_dotenv

try:
    import qrcode
except ImportError:
    qrcode = None

try:
    from pyzbar.pyzbar import decode as decode_qr
except ImportError:
    decode_qr = None

try:
    from PyPDF2 import PdfReader, PdfWriter
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
except ImportError:
    PdfReader = None
    PdfWriter = None
    canvas = None
    letter = None

load_dotenv()

DB_FILE = "conference_system.db"
QR_FOLDER = "qr_data"
CERT_FOLDER = "certificate_data"

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_REGEX = re.compile(r"^\+?[0-9]{7,15}$")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

PRIMARY_COLOR = "#2c0d54"
SECONDARY_COLOR = "#6c4fb1"
ACCENT_COLOR = "#f5f3ff"
BACKGROUND_COLOR = "#0f0b1c"
CARD_COLOR = "#1c1540"

os.makedirs(QR_FOLDER, exist_ok=True)
os.makedirs(CERT_FOLDER, exist_ok=True)


def get_connection():
    return sqlite3.connect(DB_FILE, check_same_thread=False)


def initialize_database():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS conferences (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            venue TEXT,
            starts_at TEXT,
            ends_at TEXT,
            description TEXT,
            certificate_template TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS registrants (
            id TEXT PRIMARY KEY,
            conference_id TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            company TEXT,
            payment_status TEXT NOT NULL,
            qr_token TEXT,
            qr_payload TEXT,
            qr_image_base64 TEXT,
            registered_at TEXT NOT NULL,
            attended INTEGER DEFAULT 0,
            certificate_id TEXT,
            certificate_generated_at TEXT,
            audit_status TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS import_reports (
            id TEXT PRIMARY KEY,
            conference_id TEXT NOT NULL,
            filename TEXT,
            imported_at TEXT NOT NULL,
            valid_count INTEGER,
            invalid_count INTEGER,
            metadata TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_logs (
            id TEXT PRIMARY KEY,
            registrant_id TEXT NOT NULL,
            conference_id TEXT NOT NULL,
            scanned_at TEXT NOT NULL,
            scanned_by TEXT,
            scan_source TEXT,
            status TEXT NOT NULL,
            message TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS certificates (
            id TEXT PRIMARY KEY,
            registrant_id TEXT NOT NULL,
            conference_id TEXT NOT NULL,
            verification_code TEXT NOT NULL,
            pdf_base64 TEXT,
            generated_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            target_id TEXT,
            conference_id TEXT,
            actor TEXT,
            message TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


initialize_database()


def validate_email(value: str) -> bool:
    return bool(value and EMAIL_REGEX.match(value.strip()))


def validate_phone(value: str) -> bool:
    if not value:
        return False
    normalized = re.sub(r"[^0-9+]", "", value)
    return bool(PHONE_REGEX.match(normalized))


def create_id(prefix: str = "item") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def now_iso() -> str:
    return datetime.utcnow().isoformat()


def log_audit(event_type: str, target_id: str = None, conference_id: str = None, actor: str = None, message: str = ""):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO audit_logs (id, event_type, target_id, conference_id, actor, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (create_id("audit"), event_type, target_id, conference_id, actor or "system", message, now_iso()),
    )
    conn.commit()
    conn.close()


def get_conferences():
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM conferences ORDER BY starts_at DESC", conn)
    conn.close()
    return df


def get_conference(conference_id: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM conferences WHERE id = ?", (conference_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def create_conference(name: str, venue: str, starts_at: str, ends_at: str, description: str, certificate_template: bytes = None):
    template_b64 = base64.b64encode(certificate_template).decode("utf-8") if certificate_template else None
    conn = get_connection()
    cursor = conn.cursor()
    conference_id = create_id("conf")
    cursor.execute(
        "INSERT INTO conferences (id, name, venue, starts_at, ends_at, description, certificate_template, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (conference_id, name, venue, starts_at, ends_at, description, template_b64, now_iso()),
    )
    conn.commit()
    conn.close()
    log_audit("conference_created", conference_id, conference_id, st.session_state.get("current_user", "admin"), f"Conference {name} created.")
    return conference_id


def get_certificate_template(conference_id: str):
    row = get_conference(conference_id)
    if row and row[6]:
        return base64.b64decode(row[6])
    return None


def generate_secure_token() -> str:
    return uuid.uuid4().hex


def generate_qr_payload(registrant_id: str, name: str, conference_id: str, token: str) -> str:
    payload = {
        "registrant_id": registrant_id,
        "name": name,
        "conference_id": conference_id,
        "token": token,
    }
    return json.dumps(payload)


def generate_qr_image(payload: str) -> str:
    if qrcode is None:
        return ""
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color=SECONDARY_COLOR, back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode("utf-8")


def create_registrant(conference_id: str, name: str, email: str, phone: str, company: str, payment_status: str, auto_qr: bool = True):
    registrant_id = create_id("reg")
    qr_token = None
    qr_payload = None
    qr_image_base64 = None
    if auto_qr and payment_status.lower() == "paid":
        qr_token = generate_secure_token()
        qr_payload = generate_qr_payload(registrant_id, name, conference_id, qr_token)
        qr_image_base64 = generate_qr_image(qr_payload)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO registrants (id, conference_id, name, email, phone, company, payment_status, qr_token, qr_payload, qr_image_base64, registered_at, attended, audit_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (registrant_id, conference_id, name.strip(), email.strip(), phone.strip(), company.strip(), payment_status, qr_token, qr_payload, qr_image_base64, now_iso(), 0, "pending"),
    )
    conn.commit()
    conn.close()
    log_audit("registrant_created", registrant_id, conference_id, st.session_state.get("current_user", "admin"), f"Registrant {name} added with status {payment_status}.")
    return registrant_id


def parse_payment_status(value):
    if isinstance(value, bool):
        return "paid" if value else "pending"
    text = str(value).strip().lower()
    return "paid" if text in {"paid", "yes", "y", "true", "1"} else "pending"


def parse_upload_file(uploaded_file):
    filename = uploaded_file.name.lower()
    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(uploaded_file)
        else:
            df = pd.read_excel(uploaded_file)
    except Exception as exc:
        raise ValueError(f"Unable to parse file: {exc}")
    return df


def build_import_report(conference_id: str, filename: str, valid_count: int, invalid_count: int, metadata: dict):
    report_id = create_id("import")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO import_reports (id, conference_id, filename, imported_at, valid_count, invalid_count, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (report_id, conference_id, filename, now_iso(), valid_count, invalid_count, json.dumps(metadata)),
    )
    conn.commit()
    conn.close()
    return report_id


def send_email_notification(registrant: dict, conference: dict):
    subject = f"{conference['name']} Registration Confirmation"
    body = (
        f"Hello {registrant['name']},\n\n"
        f"Your registration for {conference['name']} is confirmed.\n"
        f"Please keep the attached QR code for check-in.\n\n"
        f"Venue: {conference['venue']}\n"
        f"Starts: {conference['starts_at']}\n\n"
        "Thank you,\nConference Team"
    )
    log_audit("email_sent", registrant['id'], registrant['conference_id'], st.session_state.get("current_user", "admin"), f"Email queued to {registrant['email']}.")
    return True, subject, body


def send_whatsapp_notification(registrant: dict, conference: dict):
    phone = re.sub(r"[^0-9]", "", registrant.get("phone", ""))
    if not phone:
        return False, ""
    text = (
        f"Hello {registrant['name']}, thank you for registering for {conference['name']}. "
        "Please use the attached QR code to check in at the event."
    )
    link = f"https://wa.me/{phone}?text={quote_plus(text)}"
    log_audit("whatsapp_sent", registrant['id'], registrant['conference_id'], st.session_state.get("current_user", "admin"), f"WhatsApp link generated for {phone}.")
    return True, link


def get_registrants(conference_id: str = None):
    conn = get_connection()
    if conference_id:
        df = pd.read_sql_query("SELECT * FROM registrants WHERE conference_id = ? ORDER BY registered_at DESC", conn, params=(conference_id,))
    else:
        df = pd.read_sql_query("SELECT * FROM registrants ORDER BY registered_at DESC", conn)
    conn.close()
    return df


def get_import_reports(conference_id: str = None):
    conn = get_connection()
    if conference_id:
        df = pd.read_sql_query("SELECT * FROM import_reports WHERE conference_id = ? ORDER BY imported_at DESC", conn, params=(conference_id,))
    else:
        df = pd.read_sql_query("SELECT * FROM import_reports ORDER BY imported_at DESC", conn)
    conn.close()
    return df


def get_attendance_metrics(conference_id: str = None):
    registrants = get_registrants(conference_id)
    total = len(registrants)
    paid = len(registrants[registrants["payment_status"] == "paid"])
    checked_in = len(registrants[registrants["attended"] == 1])
    yet_to_arrive = total - checked_in
    percent_checked = round((checked_in / total * 100) if total else 0, 2)
    return {
        "total": total,
        "paid": paid,
        "checked_in": checked_in,
        "yet_to_arrive": yet_to_arrive,
        "percent_checked": percent_checked,
    }


def parse_qr_text(payload_text: str):
    try:
        payload = json.loads(payload_text)
        return payload
    except Exception:
        return None


def mark_attendance(registrant_id: str, conference_id: str, source: str = "scanner", actor: str = None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT attended FROM registrants WHERE id = ?", (registrant_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "Registrant not found"
    if row[0] == 1:
        conn.close()
        return False, "This attendee has already checked in."
    cursor.execute("UPDATE registrants SET attended = 1 WHERE id = ?", (registrant_id,))
    cursor.execute(
        "INSERT INTO attendance_logs (id, registrant_id, conference_id, scanned_at, scanned_by, scan_source, status, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (create_id("scan"), registrant_id, conference_id, now_iso(), actor or "staff", source, "checked_in", "Attendee checked in successfully."),
    )
    conn.commit()
    conn.close()
    log_audit("attendance_marked", registrant_id, conference_id, actor or "staff", "Marked present via scanner.")
    return True, "Attendance recorded successfully."


def generate_certificate_pdf(registrant: dict, conference: dict):
    verification_code = uuid.uuid4().hex[:12].upper()
    if PdfReader and PdfWriter and canvas and letter and conference.get("certificate_template"):
        template_bytes = conference["certificate_template"]
        template_stream = io.BytesIO(base64.b64decode(template_bytes))
        reader = PdfReader(template_stream)
        page = reader.pages[0]
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=(width, height))
        c.setFont("Helvetica-Bold", 42)
        c.setFillColorRGB(0.1, 0.1, 0.4)
        c.drawCentredString(width * 0.5, height * 0.5 + 50, registrant["name"])
        c.setFont("Helvetica", 18)
        c.drawCentredString(width * 0.5, height * 0.5 + 10, f"has successfully attended {conference['name']}")
        c.setFont("Helvetica", 14)
        c.drawCentredString(width * 0.5, height * 0.5 - 20, f"Certificate ID: {verification_code}")
        c.save()
        packet.seek(0)
        overlay_pdf = PdfReader(packet)
        writer = PdfWriter()
        page.merge_page(overlay_pdf.pages[0])
        writer.add_page(page)
        output = io.BytesIO()
        writer.write(output)
        output.seek(0)
        pdf_base64 = base64.b64encode(output.read()).decode("utf-8")
    else:
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=letter) if canvas else None
        if c:
            c.setFont("Helvetica-Bold", 24)
            c.drawCentredString(300, 500, conference["name"])
            c.setFont("Helvetica", 20)
            c.drawCentredString(300, 430, registrant["name"])
            c.setFont("Helvetica", 14)
            c.drawCentredString(300, 400, "Certificate of Attendance")
            c.drawCentredString(300, 370, f"Certificate ID: {verification_code}")
            c.save()
            packet.seek(0)
            pdf_base64 = base64.b64encode(packet.read()).decode("utf-8")
        else:
            pdf_base64 = ""
    certificate_id = create_id("cert")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO certificates (id, registrant_id, conference_id, verification_code, pdf_base64, generated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (certificate_id, registrant["id"], registrant["conference_id"], verification_code, pdf_base64, now_iso()),
    )
    cursor.execute(
        "UPDATE registrants SET certificate_id = ?, certificate_generated_at = ? WHERE id = ?",
        (certificate_id, now_iso(), registrant["id"]),
    )
    conn.commit()
    conn.close()
    log_audit("certificate_generated", registrant["id"], registrant["conference_id"], st.session_state.get("current_user", "admin"), f"Certificate generated with code {verification_code}.")
    return certificate_id, pdf_base64, verification_code


def get_certificate_by_code(code: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM certificates WHERE verification_code = ?", (code,))
    row = cursor.fetchone()
    conn.close()
    return row


def get_attendance_logs(conference_id: str = None):
    conn = get_connection()
    if conference_id:
        df = pd.read_sql_query("SELECT * FROM attendance_logs WHERE conference_id = ? ORDER BY scanned_at DESC", conn, params=(conference_id,))
    else:
        df = pd.read_sql_query("SELECT * FROM attendance_logs ORDER BY scanned_at DESC", conn)
    conn.close()
    return df


def apply_dashboard_style():
    st.markdown(
        f"""
        <style>
        .reportview-container, .main, .block-container {{ background: {BACKGROUND_COLOR}; color: white; }}
        .stButton>button {{ background-color: {SECONDARY_COLOR}; color: white; border: none; }}
        .stSidebar {{ background: {CARD_COLOR}; }}
        .css-1d391kg .stButton>button:hover {{ background: {PRIMARY_COLOR}; }}
        .stDataFrame table {{ background: {CARD_COLOR}; color: white; }}
        .stDataFrame thead th {{ color: white; }}
        .css-1dp5yj-egzxv9 { background: {CARD_COLOR}; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def authenticate():
    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False
        st.session_state.current_user = None
    if not st.session_state.logged_in:
        st.title("TAWCA Conference Admin Login")
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.button("Login"):
            if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
                st.session_state.logged_in = True
                st.session_state.current_user = username
                st.experimental_rerun()
            else:
                st.error("Invalid credentials")
        st.stop()


def parse_registrant_row(row, conference_id):
    name = str(row.get("name") or row.get("Name") or "").strip()
    email = str(row.get("email") or row.get("Email") or "").strip()
    phone = str(row.get("phone") or row.get("Phone") or "").strip()
    company = str(row.get("company") or row.get("Company") or "").strip()
    payment_status = parse_payment_status(row.get("payment_status") or row.get("Payment Status") or row.get("paid") or "pending")
    errors = []
    if not name:
        errors.append("Missing attendee name")
    if not validate_email(email):
        errors.append("Invalid email")
    if phone and not validate_phone(phone):
        errors.append("Invalid phone number")
    return {
        "conference_id": conference_id,
        "name": name,
        "email": email,
        "phone": phone,
        "company": company,
        "payment_status": payment_status,
        "errors": errors,
    }


def render_qr_image(base64_data: str):
    if not base64_data:
        return
    image_bytes = base64.b64decode(base64_data)
    img = Image.open(io.BytesIO(image_bytes))
    st.image(img, use_column_width=False, width=180)


def app_dashboard():
    st.title("Conference Attendance & Certificate System")
    st.markdown("### Admin overview for TAWCA conferences")
    conferences = get_conferences()
    if conferences.empty:
        st.info("No conferences available yet. Create one on the Conferences page.")
        return
    selected_conference_id = st.selectbox("Choose conference", conferences["id"] + " - " + conferences["name"])
    conference_id = selected_conference_id.split(" - ")[0]
    metrics = get_attendance_metrics(conference_id)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Registered", metrics["total"])
    col2.metric("Total Paid", metrics["paid"])
    col3.metric("Checked In", f"{metrics['checked_in']} ({metrics['percent_checked']}%)")
    col4.metric("Yet to Arrive", metrics["yet_to_arrive"])
    st.markdown("---")
    st.subheader("Recent import reports")
    reports = get_import_reports(conference_id)
    if reports.empty:
        st.info("No import reports available.")
    else:
        st.dataframe(reports[["filename", "imported_at", "valid_count", "invalid_count"]])
    st.markdown("---")
    st.subheader("Recent attendance logs")
    logs = get_attendance_logs(conference_id)
    if logs.empty:
        st.info("No attendance scans yet.")
    else:
        st.dataframe(logs[["scanned_at", "scanned_by", "scan_source", "status", "message"]].head(10))


def app_conferences():
    st.title("Conferences")
    st.markdown("Manage event details and certificate templates for each conference.")
    with st.expander("Create a new conference"):
        name = st.text_input("Conference name")
        venue = st.text_input("Venue")
        starts_at = st.date_input("Start date")
        ends_at = st.date_input("End date")
        description = st.text_area("Description")
        template_file = st.file_uploader("Upload certificate PDF template", type=["pdf"])
        if st.button("Create conference"):
            if not name:
                st.error("Conference name is required")
            else:
                template_bytes = template_file.read() if template_file else None
                conference_id = create_conference(name, venue, starts_at.isoformat(), ends_at.isoformat(), description, template_bytes)
                st.success(f"Conference created: {conference_id}")
                st.experimental_rerun()
    st.markdown("---")
    conferences = get_conferences()
    if conferences.empty:
        st.warning("No conferences found.")
        return
    st.dataframe(conferences[["id", "name", "venue", "starts_at", "ends_at"]])


def app_upload():
    st.title("Registrant Upload")
    st.markdown("Upload attendee lists by Excel/CSV or register attendees manually.")
    conferences = get_conferences()
    if conferences.empty:
        st.warning("Please create a conference first.")
        return
    conference_options = conferences["id"] + " - " + conferences["name"]
    selected = st.selectbox("Conference", conference_options)
    conference_id = selected.split(" - ")[0]
    conference = get_conference(conference_id)
    tab = st.tabs(["Bulk Upload", "Manual Registration"])
    with tab[0]:
        uploaded_file = st.file_uploader("Upload Excel/CSV file", type=["csv", "xlsx", "xls"])
        if uploaded_file:
            try:
                df = parse_upload_file(uploaded_file)
                st.success(f"Loaded {len(df)} rows.")
                if st.button("Validate and import records"):
                    progress = st.progress(0)
                    rows = []
                    invalid_rows = []
                    valid_count = 0
                    for index, row in df.iterrows():
                        parsed = parse_registrant_row(row, conference_id)
                        if parsed["errors"]:
                            invalid_rows.append({"row": index + 1, "errors": parsed["errors"]})
                        else:
                            create_registrant(conference_id, parsed["name"], parsed["email"], parsed["phone"], parsed["company"], parsed["payment_status"], auto_qr=True)
                            if parsed["payment_status"] == "paid":
                                registrant = {"id": "", "conference_id": conference_id, "name": parsed["name"], "email": parsed["email"], "phone": parsed["phone"]}
                                send_email_notification(registrant, {
                                    "name": conference[1],
                                    "venue": conference[2],
                                    "starts_at": conference[3],
                                    "certificate_template": conference[6],
                                })
                                send_whatsapp_notification(registrant, {
                                    "name": conference[1],
                                    "venue": conference[2],
                                    "starts_at": conference[3],
                                })
                            valid_count += 1
                        progress.progress(min(100, int((index + 1) / len(df) * 100)))
                    report_id = build_import_report(conference_id, uploaded_file.name, valid_count, len(invalid_rows), {"errors": invalid_rows})
                    st.success(f"Registered {valid_count} attendees. Import report {report_id} created.")
                    if invalid_rows:
                        st.warning(f"{len(invalid_rows)} rows failed validation.")
                        st.write(invalid_rows)
            except Exception as exc:
                st.error(str(exc))
    with tab[1]:
        st.subheader("Manual paid registration")
        name = st.text_input("Full name")
        email = st.text_input("Email")
        phone = st.text_input("Phone")
        company = st.text_input("Company")
        payment_status = st.selectbox("Payment status", ["paid", "pending"])
        if st.button("Register attendee"):
            if not name or not validate_email(email):
                st.error("A valid name and email are required.")
            else:
                reg_id = create_registrant(conference_id, name, email, phone, company, payment_status, auto_qr=True)
                st.success(f"Registrant {name} added: {reg_id}")
                if payment_status == "paid":
                    registrant = {"id": reg_id, "conference_id": conference_id, "name": name, "email": email, "phone": phone}
                    send_email_notification(registrant, {
                        "name": conference[1],
                        "venue": conference[2],
                        "starts_at": conference[3],
                        "certificate_template": conference[6],
                    })
                    send_whatsapp_notification(registrant, {
                        "name": conference[1],
                        "venue": conference[2],
                        "starts_at": conference[3],
                    })


def app_registrants():
    st.title("Registrants")
    conferences = get_conferences()
    if conferences.empty:
        st.warning("No conferences available yet.")
        return
    selected = st.selectbox("Conference", conferences["id"] + " - " + conferences["name"])
    conference_id = selected.split(" - ")[0]
    df = get_registrants(conference_id)
    if df.empty:
        st.info("No registrants found for this conference.")
        return
    search = st.text_input("Search by name, email, or phone")
    if search:
        df = df[df.apply(lambda x: search.lower() in str(x["name"]).lower() or search.lower() in str(x["email"]).lower() or search.lower() in str(x["phone"]).lower(), axis=1)]
    st.dataframe(df[["name", "email", "phone", "company", "payment_status", "attended", "registered_at"]])
    st.markdown("---")
    if st.button("Export registrants to CSV"):
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("Download CSV", csv, file_name=f"registrants_{conference_id}.csv", mime="text/csv")


def app_attendance():
    st.title("Attendance Scanner")
    conferences = get_conferences()
    if conferences.empty:
        st.warning("Create a conference first.")
        return
    selected = st.selectbox("Conference", conferences["id"] + " - " + conferences["name"])
    conference_id = selected.split(" - ")[0]
    st.markdown("### Live scanner")
    if decode_qr is None:
        st.warning("QR scanning requires pyzbar. Please install pyzbar for camera scan support.")
    image_file = st.camera_input("Scan attendee QR code")
    if image_file is not None:
        img = Image.open(image_file)
        if decode_qr:
            codes = decode_qr(img)
            if codes:
                payload_text = codes[0].data.decode("utf-8")
                payload = parse_qr_text(payload_text)
                if payload and payload.get("conference_id") == conference_id:
                    success, message = mark_attendance(payload["registrant_id"], conference_id, source="camera", actor=st.session_state.current_user)
                    st.success(message) if success else st.error(message)
                else:
                    st.error("This QR code does not belong to the selected conference.")
            else:
                st.error("No QR code detected in that image.")
        else:
            st.write("QR decode not available. Use the manual token field below.")
    manual_token = st.text_input("Or paste QR token payload here")
    if st.button("Mark attendance manually") and manual_token:
        payload = parse_qr_text(manual_token)
        if payload and payload.get("conference_id") == conference_id:
            success, message = mark_attendance(payload["registrant_id"], conference_id, source="manual", actor=st.session_state.current_user)
            st.success(message) if success else st.error(message)
        else:
            st.error("Invalid or mismatched QR payload.")
    st.markdown("---")
    metrics = get_attendance_metrics(conference_id)
    st.subheader("Attendance overview")
    st.metric("Total", metrics["total"])
    st.metric("Paid", metrics["paid"])
    st.metric("Checked In", f"{metrics['checked_in']} ({metrics['percent_checked']}%)")
    st.metric("Yet to Arrive", metrics["yet_to_arrive"])


def app_checkin():
    st.title("Public Check-in")
    token = st.experimental_get_query_params().get("token", [""])[0]
    if not token:
        st.info("Open this page with a token query string, e.g. ?token=<QR payload>")
        return
    payload = parse_qr_text(token)
    if not payload:
        st.error("Invalid token payload.")
        return
    registrants = get_registrants(payload["conference_id"])
    registrant = registrants[registrants["id"] == payload["registrant_id"]]
    if registrant.empty:
        st.error("Registrant not found.")
        return
    registrant = registrant.iloc[0].to_dict()
    st.write(f"### {registrant['name']}")
    st.write(f"**Conference ID:** {payload['conference_id']}")
    if registrant["attended"]:
        st.warning("This attendee is already checked in.")
    else:
        if st.button("Confirm check-in"):
            success, message = mark_attendance(registrant["id"], payload["conference_id"], source="public", actor="public_checkin")
            st.success(message) if success else st.error(message)


def app_certificates():
    st.title("Certificates")
    conferences = get_conferences()
    if conferences.empty:
        st.warning("Create a conference first.")
        return
    selected = st.selectbox("Conference", conferences["id"] + " - " + conferences["name"])
    conference_id = selected.split(" - ")[0]
    df = get_registrants(conference_id)
    eligible = df[(df["payment_status"] == "paid") & (df["attended"] == 1)]
    st.markdown(f"Eligible attendees: {len(eligible)}")
    if eligible.empty:
        st.info("No eligible attendees yet.")
        return
    if st.button("Generate certificates for eligible attendees"):
        for _, registrant in eligible.iterrows():
            if not registrant["certificate_id"]:
                create_certificate_for = {
                    "id": registrant["id"],
                    "name": registrant["name"],
                    "conference_id": registrant["conference_id"],
                }
                generate_certificate_pdf(registrant.to_dict(), {
                    "name": selected.split(" - ")[1],
                    "venue": conferences[conferences["id"] == conference_id].iloc[0]["venue"],
                    "starts_at": conferences[conferences["id"] == conference_id].iloc[0]["starts_at"],
                    "certificate_template": conferences[conferences["id"] == conference_id].iloc[0]["certificate_template"],
                })
        st.success("Certificates generated for all eligible attendees.")
    st.markdown("---")
    st.dataframe(eligible[["name", "email", "phone", "company", "attended", "certificate_id"]])


def app_verify_certificate():
    st.title("Verify Certificate")
    code = st.text_input("Enter certificate verification code")
    if st.button("Verify") and code.strip():
        row = get_certificate_by_code(code.strip())
        if not row:
            st.error("Certificate not found.")
        else:
            st.success("Certificate is valid.")
            st.write(f"Registrant ID: {row[1]}")
            st.write(f"Conference ID: {row[2]}")
            st.write(f"Generated At: {row[5]}")
            if row[4]:
                pdf_bytes = base64.b64decode(row[4])
                st.download_button("Download certificate PDF", pdf_bytes, file_name=f"certificate_{code}.pdf", mime="application/pdf")


def app_reports():
    st.title("Reports")
    conferences = get_conferences()
    if conferences.empty:
        st.warning("Create a conference first.")
        return
    selected = st.selectbox("Conference", conferences["id"] + " - " + conferences["name"])
    conference_id = selected.split(" - ")[0]
    df = get_registrants(conference_id)
    st.subheader("Registrant export")
    if st.button("Export Registrants CSV"):
        st.download_button("Download registrants CSV", df.to_csv(index=False).encode("utf-8"), file_name=f"registrants_{conference_id}.csv", mime="text/csv")
    st.subsection("Attendance PDF export")
    if st.button("Create attendance PDF"):
        st.info("PDF export is available in the next version.")


def app_audit_log():
    st.title("Audit Log")
    df = pd.read_sql_query("SELECT * FROM audit_logs ORDER BY created_at DESC", get_connection())
    if df.empty:
        st.info("No audit records found.")
    else:
        st.dataframe(df)


def main():
    apply_dashboard_style()
    authenticate()
    page = st.sidebar.selectbox(
        "Navigation",
        [
            "Dashboard",
            "Conferences",
            "Upload",
            "Registrants",
            "Attendance",
            "Check-in",
            "Certificates",
            "Verify Certificate",
            "Reports",
            "Audit Log",
        ],
    )
    st.sidebar.markdown("---")
    st.sidebar.markdown("Built for TAWCA with a deep indigo/violet admin palette.")
    if page == "Dashboard":
        app_dashboard()
    elif page == "Conferences":
        app_conferences()
    elif page == "Upload":
        app_upload()
    elif page == "Registrants":
        app_registrants()
    elif page == "Attendance":
        app_attendance()
    elif page == "Check-in":
        app_checkin()
    elif page == "Certificates":
        app_certificates()
    elif page == "Verify Certificate":
        app_verify_certificate()
    elif page == "Reports":
        app_reports()
    elif page == "Audit Log":
        app_audit_log()


if __name__ == "__main__":
    main()
