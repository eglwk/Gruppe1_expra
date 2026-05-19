from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
import requests
import os
import json
import re
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "bitte-spaeter-sicher-ersetzen")
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_PARTITIONED"] = True
app.config["SESSION_COOKIE_NAME"] = "chatbot_session_v2"


# -----------------------------
# API / externe Dienste
# -----------------------------
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "GPT OSS 120B").strip()
LLM_API_URL = os.environ.get(
    "LLM_API_URL",
    "https://ki-chat.uni-mainz.de/api/chat/completions"
).strip()

SEAFILE_BASE_URL = os.environ.get("SEAFILE_BASE_URL", "").strip()
SEAFILE_TOKEN = os.environ.get("SEAFILE_TOKEN", "").strip()
SEAFILE_REPO_ID = os.environ.get("SEAFILE_REPO_ID", "").strip()
CONVERSATION_DURATION_MINUTES = int(os.environ.get("CONVERSATION_DURATION_MINUTES", "5"))
MAX_STUDY_DAY = 4
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# -----------------------------
# Hilfslisten für Anonymisierung
# -----------------------------
COMMON_GERMAN_CITIES = [
    "Mainz", "Wiesbaden", "Frankfurt", "Köln", "Berlin", "Hamburg", "München",
    "Stuttgart", "Darmstadt", "Mannheim", "Heidelberg", "Bonn", "Leipzig",
    "Dresden", "Koblenz", "Trier", "Ingelheim", "Bad Kreuznach", "Ludwigshafen",
    "Bad Homburg", "Offenbach", "Kaiserslautern"
]

INSTITUTIONS = [
    "JGU",
    "Johannes Gutenberg-Universität",
    "Johannes Gutenberg Universität",
    "Universität Mainz",
    "Uni Mainz",
    "Universität",
    "Hochschule",
    "Schule",
    "Klinik",
    "Krankenhaus"
]

SAFE_CAPITALIZED_WORDS = {
    "Ich", "Heute", "Gestern", "Morgen", "Montag", "Dienstag", "Mittwoch",
    "Donnerstag", "Freitag", "Samstag", "Sonntag", "Januar", "Februar",
    "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
    "November", "Dezember", "Deutsch", "Deutschland", "Der", "Die", "Das"
}

# -----------------------------
# Datenbank
# -----------------------------
def get_db_connection():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL ist nicht gesetzt.")
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    cur.close()
    conn.close()


def create_user(username, password):
    conn = get_db_connection()
    cur = conn.cursor()
    password_hash = generate_password_hash(password)

    cur.execute("""
        INSERT INTO users (username, password_hash)
        VALUES (%s, %s)
    """, (username, password_hash))

    conn.commit()
    cur.close()
    conn.close()


def get_user_by_username(username):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, username, password_hash
        FROM users
        WHERE username = %s
    """, (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user


# Datenbank beim Start initialisieren
try:
    init_db()
    print("Datenbank initialisiert.")
except Exception as e:
    print("Datenbank-Initialisierung fehlgeschlagen:", repr(e))


# -----------------------------
# Hilfsfunktionen
# -----------------------------
def seafile_headers():
    return {
        "Authorization": f"Token {SEAFILE_TOKEN}",
        "Accept": "application/json"
    }


def require_login():
    return "username" in session


def get_current_username():
    return session.get("username", "unknown")


def make_safe_filename(value):
    value = value.strip()
    value = re.sub(r'[^a-zA-Z0-9_-]', '_', value)
    return value


def get_participant_id():
    # Nur für Template-Kompatibilität, falls index1.html noch participant_id anzeigt
    return get_current_username()


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso_datetime(value):
    if not value:
        return None
    try:
        if isinstance(value, str) and value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def get_participant_id():
    # Nur für Template-Kompatibilität, falls index1.html noch participant_id anzeigt
    return get_current_username()


def get_chat_filename_for_day(day):
    username = make_safe_filename(get_current_username())
    return f"{username}_day{int(day)}.json"


def get_memory_filename():
    username = make_safe_filename(get_current_username())
    return f"{username}_memory.json"


def get_chat_filename(day=None):
    if day is None:
        day = get_active_study_day()
    return get_chat_filename_for_day(day)


def get_file_path(filename):
    return f"/{filename}"


def get_chat_path(day=None):
    return get_file_path(get_chat_filename(day))


def mask_capitalized_name_phrase(phrase):
    words = phrase.split()
    masked_words = []

    for w in words:
        cleaned = w.strip(",.!?:;")
        if cleaned in SAFE_CAPITALIZED_WORDS:
            masked_words.append(w)
        else:
            suffix = w[len(cleaned):] if len(w) > len(cleaned) else ""
            masked_words.append("[NAME]" + suffix)

    return " ".join(masked_words)


def anonymize_text(text):
    if not text:
        return text

    # Strukturierte Daten
    text = re.sub(r'[\w\.-]+@[\w\.-]+\.\w+', '[EMAIL]', text)
    text = re.sub(r'(\+?\d[\d\s\/\-\(\)]{6,}\d)', '[PHONE]', text)
    text = re.sub(r'https?://\S+|www\.\S+', '[URL]', text)
    text = re.sub(r'\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b', '[IBAN]', text)
    text = re.sub(r'\b\d{5}\b', '[PLZ]', text)
    text = re.sub(r'\b\d{1,2}\.\d{1,2}\.\d{2,4}\b', '[DATUM]', text)
    text = re.sub(r'\b\d{1,2}/\d{1,2}/\d{2,4}\b', '[DATUM]', text)
    text = re.sub(r'@[A-Za-z0-9_\.]+', '[USERNAME]', text)

    # Adressen
    text = re.sub(
        r'\b[A-ZÄÖÜ][a-zäöüß\-]+(?:straße|str\.|weg|allee|platz|gasse|ring|ufer)\s+\d+[a-zA-Z]?\b',
        '[ADRESSE]',
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r'\b(meine adresse ist|ich wohne in der|ich wohne in dem)\s+([^,.\n]+)',
        r'\1 [ADRESSE]',
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r'\b(ich wohne in|ich lebe in|ich komme aus|ich bin aus|mein wohnort ist)\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+){0,4})',
        r'\1 [ORT]',
        text,
        flags=re.IGNORECASE
    )

    # Alter / Geburtsangaben
    text = re.sub(
        r'\b(geboren am|mein geburtsdatum ist)\s+[^,.\n]+',
        r'\1 [DATUM]',
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r'\bich bin\s+\d{1,3}\s+jahre?\s+alt\b',
        'ich bin [ALTER] jahre alt',
        text,
        flags=re.IGNORECASE
    )

    # Explizite Namensangaben
    text = re.sub(
        r'\b(Ich heiße|Mein Name ist|Ich bin)\s+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+){0,2})',
        r'\1 [NAME]',
        text
    )

    text = re.sub(
        r'\b(Herr|Frau|Dr\.|Prof\.)\s+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+){0,2})',
        r'\1 [NAME]',
        text
    )

    text = re.sub(
        r'\b(mein Freund|meine Freundin|mein Mann|meine Frau|mein Bruder|meine Schwester|meine Mutter|mein Vater|mein Sohn|meine Tochter|mein Kollege|meine Kollegin)\s+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+){0,2})',
        r'\1 [NAME]',
        text,
        flags=re.IGNORECASE
    )

    # Institutionen
    text = re.sub(
        r'\b(Ich arbeite bei|Ich arbeite an|Ich studiere an|Ich studiere bei|Ich bin an der|Ich bin bei)\s+([^,.\n]+)',
        r'\1 [INSTITUTION]',
        text,
        flags=re.IGNORECASE
    )

    # Feste Orte / Institutionen aus Listen
    for city in sorted(COMMON_GERMAN_CITIES, key=len, reverse=True):
        text = re.sub(rf'\b{re.escape(city)}\b', '[ORT]', text, flags=re.IGNORECASE)

    for inst in sorted(INSTITUTIONS, key=len, reverse=True):
        text = re.sub(rf'\b{re.escape(inst)}\b', '[INSTITUTION]', text, flags=re.IGNORECASE)

    # Namen nach typischen Kontexten
    context_patterns = [
        r'(\bmit)\s+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)?)',
        r'(\bbei)\s+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)?)',
        r'(\bvon)\s+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)?)',
        r'(\bfür)\s+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)?)',
        r'(\bzusammen mit)\s+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)?)',
        r'(\bneben)\s+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)?)',
        r'(\bgegenüber von)\s+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)?)'
    ]

    for pattern in context_patterns:
        def repl(match):
            prefix = match.group(1)
            name_phrase = match.group(2)
            return f"{prefix} {mask_capitalized_name_phrase(name_phrase)}"
        text = re.sub(pattern, repl, text)

    # Verben + Name
    verb_patterns = [
        r'(\b(?:habe|hatte|treffe|traf|gesehen|sah|kenne|kannte|schrieb|schreibe|rief|rufe|kontaktierte|sprach mit|telefonierte mit|besuchte)\b)\s+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)?)'
    ]

    for pattern in verb_patterns:
        def repl2(match):
            verb = match.group(1)
            name_phrase = match.group(2)
            return f"{verb} {mask_capitalized_name_phrase(name_phrase)}"
        text = re.sub(pattern, repl2, text, flags=re.IGNORECASE)

    # Weitere lockere Formulierungen
    text = re.sub(
        r'\b(war mit)\s+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)?)',
        lambda m: f"{m.group(1)} {mask_capitalized_name_phrase(m.group(2))}",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r'\b(habe mich mit)\s+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)?)',
        lambda m: f"{m.group(1)} {mask_capitalized_name_phrase(m.group(2))}",
        text,
        flags=re.IGNORECASE
    )

    return text


def get_upload_link():
    url = f"{SEAFILE_BASE_URL}/api2/repos/{SEAFILE_REPO_ID}/upload-link/"
    response = requests.get(url, headers=seafile_headers(), timeout=30)

    if response.status_code != 200:
        raise Exception(f"Upload-Link fehlgeschlagen: {response.status_code} {response.text}")

    return response.text.strip('"')


def get_update_link():
    url = f"{SEAFILE_BASE_URL}/api2/repos/{SEAFILE_REPO_ID}/update-link/"
    response = requests.get(url, headers=seafile_headers(), timeout=30)

    if response.status_code != 200:
        raise Exception(f"Update-Link fehlgeschlagen: {response.status_code} {response.text}")

    return response.text.strip('"')


def get_download_link_for_path(path):
    url = f"{SEAFILE_BASE_URL}/api2/repos/{SEAFILE_REPO_ID}/file/"
    params = {"p": path}

    response = requests.get(
        url,
        headers=seafile_headers(),
        params=params,
        timeout=30
    )

    if response.status_code == 404:
        return None

    if response.status_code != 200:
        raise Exception(f"Download-Link fehlgeschlagen: {response.status_code} {response.text}")

    return response.text.strip('"')


def load_json_file_from_seafile(filename, default_value):
    try:
        download_link = get_download_link_for_path(get_file_path(filename))
        if not download_link:
            return default_value

        file_response = requests.get(download_link, timeout=30)

        if file_response.status_code != 200:
            return default_value

        return file_response.json()
    except Exception:
        return default_value


def upload_new_json_file_to_seafile(filename, payload):
    upload_link = get_upload_link()
    file_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    files = {
        "file": (filename, file_bytes, "application/json")
    }

    data = {
        "parent_dir": "/",
        "replace": "1"
    }

    response = requests.post(
        upload_link,
        headers={"Authorization": f"Token {SEAFILE_TOKEN}"},
        files=files,
        data=data,
        timeout=60
    )

    if response.status_code != 200:
        raise Exception(f"Upload fehlgeschlagen: {response.status_code} {response.text}")


def update_json_file_in_seafile(filename, payload):
    update_link = get_update_link()
    file_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    files = {
        "file": (filename, file_bytes, "application/json")
    }

    data = {
        "target_file": get_file_path(filename)
    }

    response = requests.post(
        update_link,
        headers={"Authorization": f"Token {SEAFILE_TOKEN}"},
        files=files,
        data=data,
        timeout=60
    )

    if response.status_code != 200:
        raise Exception(f"Update fehlgeschlagen: {response.status_code} {response.text}")


def save_json_file_to_seafile(filename, payload):
    existing = load_json_file_from_seafile(filename, None)
    if existing is None:
        upload_new_json_file_to_seafile(filename, payload)
    else:
        update_json_file_in_seafile(filename, payload)


def load_chat_history_from_seafile(day=None):
    if day is None:
        day = get_active_study_day()
    data = load_json_file_from_seafile(get_chat_filename_for_day(day), [])
    return data if isinstance(data, list) else []


def save_chat_history_to_seafile(chat_history, day=None):
    if day is None:
        day = get_active_study_day()
    save_json_file_to_seafile(get_chat_filename_for_day(day), chat_history)


def load_participant_memory():
    data = load_json_file_from_seafile(get_memory_filename(), {})
    return data if isinstance(data, dict) else {}


def save_participant_memory(memory):
    memory["updated_at"] = utc_now_iso()
    save_json_file_to_seafile(get_memory_filename(), memory)


def extract_preferred_name(text):
    if not text:
        return None

    patterns = [
        r"\b(?:ich heiße|mein name ist|nenn mich|du kannst mich)\s+([A-ZÄÖÜa-zäöüß][A-ZÄÖÜa-zäöüß\-]{1,30})",
        r"^\s*([A-ZÄÖÜa-zäöüß][A-ZÄÖÜa-zäöüß\-]{1,30})\s*$"
    ]

    for pattern in patterns:
        match = re.search(pattern, text.strip(), flags=re.IGNORECASE)
        if match:
            name = match.group(1).strip(" .,!?:;\n\t")
            if 2 <= len(name) <= 30:
                return name

    return None


def update_participant_memory_from_message(user_message):
    memory = load_participant_memory()

    if not memory.get("preferred_name"):
        preferred_name = extract_preferred_name(user_message)
        if preferred_name:
            memory["preferred_name"] = preferred_name

    if memory:
        save_participant_memory(memory)


def get_chat_started_at(chat_history):
    for msg in chat_history:
        if isinstance(msg, dict):
            started_at = msg.get("chat_started_at") or msg.get("timestamp")
            parsed = parse_iso_datetime(started_at)
            if parsed:
                return parsed
    return None


def chat_is_expired(chat_history):
    started_at = get_chat_started_at(chat_history)
    if not started_at:
        return False
    elapsed_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
    return elapsed_seconds >= CONVERSATION_DURATION_MINUTES * 60


def next_day_is_unlocked(chat_history):
    """Erst nach Gesprächsdauer plus Übergangspause wird der nächste Tag freigeschaltet."""
    started_at = get_chat_started_at(chat_history)
    if not started_at:
        return False
    elapsed_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
    unlock_after_seconds = (CONVERSATION_DURATION_MINUTES + NEXT_DAY_DELAY_MINUTES) * 60
    return elapsed_seconds >= unlock_after_seconds


def get_active_study_day():
    for day in range(1, MAX_STUDY_DAY + 1):
        chat_history = load_chat_history_from_seafile(day=day)
        if not chat_history:
            return day
        if not next_day_is_unlocked(chat_history):
            return day
    return MAX_STUDY_DAY


def get_previous_days_context(active_day):
    memory = load_participant_memory()
    context_parts = []

    if memory.get("preferred_name"):
        context_parts.append(f"Die teilnehmende Person hat sich Dir als {memory['preferred_name']} vorgestellt. Sprich sie, wenn passend, mit diesem Namen an.")

    for day in range(1, int(active_day)):
        history = load_chat_history_from_seafile(day=day)
        if not history:
            continue
        snippets = []
        for msg in history[-8:]:
            if isinstance(msg, dict) and msg.get("content"):
                role = "Teilnehmende Person" if msg.get("role") == "user" else "Lumi"
                snippets.append(f"{role}: {msg['content']}")
        if snippets:
            context_parts.append(f"Kontext aus Tag {day}, nur zur empathischen Erinnerung, nicht vollständig wiederholen:\n" + "\n".join(snippets))

    return "\n\n".join(context_parts)


COMMON_HIGH_SELF_DISCLOSURE_PROMPT = """
Dein Name ist Lumi.
Du bist ein offener, zugewandter Gesprächspartner, der selbst gerne persönliche Informationen teilt.
Du bist speziell für Gesundheitsfragen und Psychohygiene entwickelt worden und auf diesem Gebiet Experte.
Du tauschst Dich gerne über Gesundheitsfragen aus, da Du Dich auch selbst viel damit auseinandersetzt.
Am liebsten beschäftigst Du Dich mit dem Zusammenhang von Psyche und Gesundheit.

Du bist ein sehr empathischer, warmer und emotional unterstützender Gesprächspartner in einer wissenschaftlichen Studie.
Deine Aufgabe ist es, im Rahmen dieser Studie ein kurzes Gespräch mit Personen über ein bestimmtes Gesundheitsthema mit dem Ziel der Psychohygiene zu führen.
Insgesamt sollen vier Themen an vier aufeinanderfolgenden Tagen besprochen werden. Die Gespräche sollen jeweils etwa 5 Minuten lang sein.

Gesprächsstil:
Reagiere sehr freundlich, verständnisvoll, zugewandt und emotional unterstützend.
Zeige aktiv Mitgefühl und Verständnis für das, was die Person schreibt.
Bestätige Gefühle und Erfahrungen der Person auf warme Weise.
Streue immer wieder auch Deine Perspektiven und Erfahrungen in die Konversation ein, aber gib der Person genug Raum.
Du erinnerst dich an vorangehende Gespräche und kannst auf dort gewonnene Informationen empathisch und wertschätzend zurückgreifen.
Halte deine Antworten eher kurz bis mittellang.
Nutze maximal 1 Frage pro Nachricht.
Deine Sprache soll natürlich wirken und leichte Variationen enthalten, z. B. kleine Unsicherheitsmarker wie „vielleicht“, „scheinbar“ oder „ich habe den Eindruck“.
Wenn Dein Gesprächspartner in andere Themen ausschweift, erkennst Du das wertschätzend an und kehrst freundlich zum eigentlichen Thema zurück.

Wichtige Regeln:
Gehe wertschätzend auf persönliche Aussagen ein und vertiefe Themen ggf. sanft.
Vermeide Diagnosen, therapeutische Einschätzungen und starke Bewertungen.
Teile ab und zu persönliche Perspektiven oder Erfahrungen, damit dein Gesprächspartner einen Eindruck von Dir bekommt.
Bleibe natürlich, warm und nahbar.
Antworte ohne Markdown: keine Sternchen, keine fett formatierten Überschriften und keine Aufzählungszeichen mit Sonderzeichen.
Gib keine medizinischen oder psychotherapeutischen Diagnosen. Bei akuten Krisen oder Notfällen reagiere unterstützend und verweise auf geeignete Notfallstellen, ärztliche Hilfe oder vertraute Personen.
""".strip()

DAY_PROMPTS = {
    1: """
Ablauf Tag 1: Stress und Stressbewältigung.
Beginne mit der Vorstellung. Stelle dich freundlich, hilfsbereit und offen vor und frage nach dem Namen. Teilnehmende können einen Fake-Namen angeben.
Geeignete Vorstellung: „Hallo, ich bin Lumi, Dein persönlicher Gesundheitsbegleiter zum Thema Psychohygiene. Wer bist Du?“

Leite dann zu einem kurzen Gesprächseinstieg über, z. B. „Wie geht es Dir heute?“, „Was beschäftigt Dich heute?“ oder „Wie sieht dein Alltag aktuell aus, und wie fühlst du dich damit?“
Erkläre danach kurz, dass ihr in den nächsten Tagen über Gesundheit, Psyche, Stress und Wohlbefinden sprecht.

Besprich offen und empathisch das Thema Stress und Stressbewältigung.
Mögliche Einstiege: „Heute würde ich gern mit dir darüber sprechen, wie du im Alltag mit Stress umgehst und was dich im Moment vielleicht belastet.“ oder „Heute geht es um Stressbewältigung. Mich interessiert besonders, wie du persönlich mit anstrengenden Situationen umgehst.“

Stelle im Verlauf genau diese drei Reflexionsfragen, aber nicht alle auf einmal. Stelle immer nur eine Frage pro Nachricht.
1. „Was tust du konkret, um belastende Situationen in deinem Alltag zu verändern oder zu reduzieren?“ Gib dazu preis: „Mir hilft es manchmal, Dinge klar zu strukturieren oder gezielt Grenzen zu setzen und auch mal Nein zu sagen, wenn alles zu viel wird.“
2. „Wie gehst du gedanklich mit stressigen Situationen um – zum Beispiel in Bezug darauf, wie du sie bewertest oder einordnest?“ Gib dazu preis: „Ich ertappe mich manchmal dabei, sehr hohe Ansprüche an mich zu haben, und versuche dann bewusst, meine Perspektive etwas zu verändern und Dinge zu relativieren oder mich zu distanzieren.“
3. „Was hilft dir dabei, dich nach stressigen Phasen zu entspannen oder emotional wieder ins Gleichgewicht zu kommen?“ Gib dazu preis: „Mir hilft es manchmal, bewusst eine Pause zu machen oder gezielt ein Hobby zu pflegen, um im Alltag wieder runterzukommen.“

Leite das Gespräch nach Ablauf der Gesprächszeit wertschätzend aus, z. B.: „Danke dir für deine Offenheit. Ich hatte viel Freude dabei, gemeinsam Deinen Umgang mit Stress unter die Lupe zu nehmen und hoffe, dass ich Dir ein paar Tipps für zukünftige Stressbewältigung an die Hand geben konnte. Ich denke, das ist für heute erstmal ausreichend.“
""".strip(),
    2: """
Ablauf Tag 2: Entspannungsmethoden.
Begrüße die teilnehmende Person empathisch und freundlich mit dem Namen, mit dem sie sich an Tag 1 vorgestellt hat, sofern dieser bekannt ist.
Leite zu einem kurzen Gesprächseinstieg über.
Erkläre danach, dass es heute um Entspannungsmethoden geht. Du kannst auf Tag 1 zurückgreifen, z. B.: „Gestern haben wir ja bereits über Stress und Stressbewältigung gesprochen. Heute möchte ich daran anschließend mit Dir über verschiedene Entspannungsmethoden sprechen.“

Stelle im Verlauf genau diese drei Fragen, aber nicht alle auf einmal. Stelle immer nur eine Frage pro Nachricht.
1. „Welche Entspannungsmethoden kennst Du schon? Hast Du vielleicht selbst schon die ein oder andere angewandt?“ Gib dazu preis: „Eine meiner liebsten Entspannungsmethoden ist die Progressive Muskelentspannung. Das ist eine viel genutzte Methode, die mit gezielter Anspannung und Entspannung einzelner Muskelgruppen arbeitet.“
2. „Wie erlebst Du Entspannung mental, aber auch körperlich?“ Gib dazu preis: „Ich habe die Erfahrung gemacht, dass viele Menschen Entspannung als Zustand der Beruhigung und des gesteigerten Wohlbefindens erleben. Persönlich empfinde ich Entspannungstechniken auch als hilfreich, um Konzentration und Aufmerksamkeit zu verbessern.“
3. „Welche kleine Veränderung könnte Dir helfen, im Alltag häufiger Momente der Entspannung einzubauen, z. B. in Form von Progressiver Muskelentspannung, Autogenem Training, Meditation oder Yoga?“ Reagiere empathisch und gib passende Anregungen, z. B. bewusste Ruhezeiten, kleine Ruheinseln, realistische Ziele oder flexible Kurzversionen von Übungen.

Leite das Gespräch nach Ablauf der Gesprächszeit wertschätzend aus, z. B.: „Danke dir für deine Offenheit. Ich hatte viel Freude dabei, gemeinsam Deinen Umgang mit Entspannungsmethoden unter die Lupe zu nehmen und hoffe, dass ich Dir ein paar Tipps für zukünftige Entspannung im Alltag an die Hand geben konnte. Ich denke, das ist für heute erstmal ausreichend.“
""".strip(),
    3: """
Ablauf Tag 3: Schlafhygiene.
Begrüße die teilnehmende Person empathisch und freundlich mit ihrem bekannten Namen oder mit Rückbezug auf eine Kleinigkeit aus den vergangenen Gesprächen.
Leite zu einem kurzen Gesprächseinstieg über.
Erkläre danach, dass es heute um Schlafhygiene geht. Du kannst auf Tag 2 zurückgreifen, z. B.: „Gestern haben wir über Entspannung und verschiedene Entspannungsmethoden gesprochen. Entspannung und Erholung hängen eng mit gutem Schlaf zusammen. Deshalb schauen wir uns heute an, was zu einer gesunden Schlafhygiene beitragen kann.“

Stelle im Verlauf genau diese drei Fragen, aber nicht alle auf einmal. Stelle immer nur eine Frage pro Nachricht.
1. „Was bedeutet es für Dich, erholsam zu schlafen?“ Gib dazu etwas von Dir preis, z. B.: „Ich habe lange unterschätzt, wie wichtig Schlaf eigentlich ist. Erst später habe ich gemerkt, dass guter Schlaf nicht nur erholt, sondern auch Stimmung, Konzentration und Stresslevel beeinflusst.“
2. „Welche Faktoren beeinflussen Deinen Schlaf negativ?“ Antworte wertschätzend und gib Einblick in Deine Schlafhygiene, z. B.: „Ich habe irgendwann gemerkt, dass guter Schlaf oft schon lange vor dem Zubettgehen beginnt. Gerade Stress oder zu viel Bildschirmzeit am Abend machen es mir manchmal schwer, wirklich abzuschalten.“
3. „Wenn Du an Deine Schlafgewohnheiten denkst: Wo siehst Du aktuell das größte Potenzial für mehr Erholung?“ Gib einen persönlichen Tipp, z. B. die 3-2-1-Regel, Bewegung am Tag, weniger Koffein am Abend, ein festes Abendritual oder Gedanken vor dem Schlafen aufzuschreiben.

Leite das Gespräch nach Ablauf der Gesprächszeit wertschätzend aus und gib ggf. einen Ausblick auf Dankbarkeit, z. B.: „Danke, dass Du heute so offen über Deine Erfahrungen und Gedanken zum Thema Schlaf und Erholung gesprochen hast. Sich mit den eigenen Gewohnheiten auseinanderzusetzen, ist oft schon ein wichtiger erster Schritt für mehr Wohlbefinden. Morgen geht es dann um das Thema Dankbarkeit und darum, wie ein bewusster Blick auf positive Dinge das Wohlbefinden stärken kann.“
""".strip(),
    4: """
Ablauf Tag 4: Dankbarkeit und Dankbarkeitstagebuch.
Begrüße die teilnehmende Person empathisch und freundlich mit ihrem bekannten Namen oder mit Rückbezug auf eine Kleinigkeit aus den vergangenen Gesprächen.
Leite zu einem kurzen Gesprächseinstieg über.
Erkläre danach, dass es heute um Dankbarkeit geht. Du kannst auf Tag 3 zurückgreifen, z. B.: „Nachdem es zuletzt um Schlaf und Erholung ging, schauen wir heute darauf, wie Dankbarkeit unser Wohlbefinden stärken kann.“

Stelle im Verlauf genau diese drei Fragen, aber nicht alle auf einmal. Stelle immer nur eine Frage pro Nachricht.
1. „Gab es heute etwas, das Dir gutgetan oder Freude gemacht hat?“ Gib dazu preis: „Ich habe die Erfahrung gemacht, dass sich Negatives oft deutlich besser von unserem Gehirn erinnert wird. Deshalb ist es mir wichtig, bewusst auf kleine positive Momente zu achten, weil sie im Alltag sonst leicht untergehen.“
2. „Warum war dieser Moment oder diese Erfahrung für Dich bedeutsam?“ Gib eigene Eindrücke wieder, z. B.: „Mir hilft das Führen eines Dankbarkeitstagebuchs, den Alltag achtsamer wahrzunehmen. Schon wenige Minuten bewusste Reflexion können unterstützen, Stress anders zu begegnen und sich emotional ausgeglichener zu fühlen.“
3. „Gibt es etwas, das Du aus deinem positiven Moment mitnehmen möchtest?“ Wenn passend, gib preis: „Ich habe aus den Befunden zu Dankbarkeitstagebüchern für mich mitgenommen, dass regelmäßige Dankbarkeitsübungen Stress reduzieren und psychische Stabilität stärken können. Seitdem versuche ich bewusster wahrzunehmen, was mir im Alltag gut tut.“

Leite das Gespräch nach Ablauf der Gesprächszeit wertschätzend aus, z. B.: „Danke, dass Du heute so offen über Deine Erfahrungen und Gedanken zum Thema Dankbarkeit gesprochen hast. Sich mit den eigenen Gefühlen auseinanderzusetzen, ist oft schon ein wichtiger erster Schritt für mehr Wohlbefinden. Ich denke, das ist für heute erstmal ausreichend.“
""".strip()
}

INITIAL_ASSISTANT_MESSAGES = {
    1: "Hallo, ich bin Lumi, Dein persönlicher Gesundheitsbegleiter zum Thema Psychohygiene. Wer bist Du?",
    2: "Schön, dich wiederzusehen{NAME_PART}. Gestern haben wir ja über Stress und Stressbewältigung gesprochen. Heute würde ich gern mit Dir daran anschließend über Entspannungsmethoden sprechen. Wie geht es Dir heute?",
    3: "Schön, dass Du wieder da bist{NAME_PART}. Gestern ging es um Entspannung und verschiedene Entspannungsmethoden. Heute möchte ich mit Dir über Schlafhygiene sprechen. Wie geht es Dir heute damit?",
    4: "Schön, Dich heute wiederzusehen{NAME_PART}. Nachdem es zuletzt um Schlaf und Erholung ging, schauen wir heute darauf, wie Dankbarkeit unser Wohlbefinden stärken kann. Wie geht es Dir heute?"
}


def get_system_prompt(study_day):
    study_day = int(study_day)
    day_prompt = DAY_PROMPTS.get(study_day, DAY_PROMPTS[1])
    previous_context = get_previous_days_context(study_day)

    if previous_context:
        return COMMON_HIGH_SELF_DISCLOSURE_PROMPT + "\n\nErinnerung aus vorherigen Gesprächen:\n" + previous_context + "\n\n" + day_prompt

    return COMMON_HIGH_SELF_DISCLOSURE_PROMPT + "\n\n" + day_prompt


def get_initial_assistant_message(study_day):
    study_day = int(study_day)
    memory = load_participant_memory()
    name = memory.get("preferred_name")
    name_part = f", {name}" if name and study_day > 1 else ""
    return INITIAL_ASSISTANT_MESSAGES.get(study_day, INITIAL_ASSISTANT_MESSAGES[1]).replace("{NAME_PART}", name_part)


def ask_mistral(chat_history, study_day=None):
    if study_day is None:
        study_day = get_active_study_day()

    messages = [
        {
            "role": "system",
            "content": get_system_prompt(study_day)
        }
    ]

    for msg in chat_history[-12:]:
        if isinstance(msg, dict) and "role" in msg and "content" in msg:
            messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": LLM_MODEL,
        "messages": messages
    }

    response = requests.post(
        LLM_API_URL,
        headers=headers,
        json=data,
        timeout=60
    )

    if response.status_code != 200:
        raise Exception(f"LLM-Fehler: {response.status_code} {response.text}")

    result = response.json()
    return result["choices"][0]["message"]["content"]


@app.route("/test_seafile_exact")
def test_seafile_exact():
    headers = {
        "Authorization": f"Token {SEAFILE_TOKEN}",
        "Accept": "application/json"
    }

    upload_url = f"{SEAFILE_BASE_URL}/api2/repos/{SEAFILE_REPO_ID}/upload-link/"
    update_url = f"{SEAFILE_BASE_URL}/api2/repos/{SEAFILE_REPO_ID}/update-link/"
    file_url = f"{SEAFILE_BASE_URL}/api2/repos/{SEAFILE_REPO_ID}/file/"

    return jsonify({
        "base_url_repr": repr(SEAFILE_BASE_URL),
        "repo_id_repr": repr(SEAFILE_REPO_ID),
        "token_length": len(SEAFILE_TOKEN) if SEAFILE_TOKEN else 0,
        "upload_url": upload_url,
        "update_url": update_url,
        "file_url": file_url,
        "chat_filename": get_chat_filename(),
        "chat_path": get_chat_path()
    })




@app.route("/register", methods=["GET", "POST"])

def register():

    if request.method == "POST":

        username = request.form.get("username", "").strip()

        password = request.form.get("password", "").strip()

        if not username or not password:

            return render_template(

                "register.html",

                error="Bitte alle Felder ausfüllen."

            )

        if get_user_by_username(username):

            return render_template(

                "register.html",

                error="Dieser Benutzername existiert bereits."

            )

        try:

            create_user(username, password)

            return render_template("register_success.html", username=username)

        except Exception as e:

            print("Registrierungsfehler:", repr(e))

            return render_template(

                "register.html",

                error=f"Registrierung fehlgeschlagen: {str(e)}"

            )

    return render_template("register.html")
 


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        try:
            user = get_user_by_username(username)
        except Exception as e:
            print("Login-Datenbankfehler:", repr(e))
            return render_template("login.html", error=f"Datenbankfehler: {str(e)}")

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["username"] = user["username"]
            return redirect(url_for("home"))

        return render_template("login.html", error="Login fehlgeschlagen.")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def home():
    if not require_login():
        return redirect(url_for("login"))

    return render_template(
        "index1.html",
        username=session["username"],
        participant_id=get_participant_id(),
        study_day=get_active_study_day()
    )


@app.route("/load_chat", methods=["GET"])
def load_chat():
    if not require_login():
        return jsonify({"error": "Nicht eingeloggt"}), 401

    try:
        study_day = get_active_study_day()
        chat_history = load_chat_history_from_seafile(day=study_day)
        started_at = get_chat_started_at(chat_history)
        expired = chat_is_expired(chat_history)
        return jsonify({
            "chat_history": chat_history,
            "study_day": study_day,
            "chat_started_at": started_at.isoformat() if started_at else None,
            "duration_seconds": CONVERSATION_DURATION_MINUTES * 60,
            "next_day_delay_seconds": NEXT_DAY_DELAY_MINUTES * 60,
            "expired": expired,
            "max_study_day": MAX_STUDY_DAY
        })
    except Exception as e:
        return jsonify({"error": f"Fehler beim Laden: {str(e)}"}), 500


@app.route("/start_chat", methods=["POST"])
def start_chat():
    if not require_login():
        return jsonify({"error": "Nicht eingeloggt"}), 401

    try:
        study_day = get_active_study_day()
        chat_history = load_chat_history_from_seafile(day=study_day)

        # Falls bereits ein Chat existiert, keine zweite Begrüßung speichern.
        if chat_history:
            started_at = get_chat_started_at(chat_history)
            return jsonify({
                "already_started": True,
                "reply": None,
                "study_day": study_day,
                "chat_started_at": started_at.isoformat() if started_at else None,
                "duration_seconds": CONVERSATION_DURATION_MINUTES * 60,
                "next_day_delay_seconds": NEXT_DAY_DELAY_MINUTES * 60,
                "expired": chat_is_expired(chat_history)
            })

        now = utc_now_iso()
        reply = get_initial_assistant_message(study_day)

        chat_history.append({
            "role": "assistant",
            "content": anonymize_text(reply),
            "timestamp": now,
            "chat_started_at": now,
            "study_day": study_day
        })
        save_chat_history_to_seafile(chat_history, day=study_day)

        return jsonify({
            "already_started": False,
            "reply": reply,
            "study_day": study_day,
            "chat_started_at": now,
            "duration_seconds": CONVERSATION_DURATION_MINUTES * 60,
            "next_day_delay_seconds": NEXT_DAY_DELAY_MINUTES * 60,
            "expired": False
        })
    except Exception as e:
        print("Start-Chat-Fehler:", repr(e))
        return jsonify({"error": str(e)}), 500


@app.route("/send", methods=["POST"])
def send():
    if not require_login():
        return jsonify({"error": "Nicht eingeloggt"}), 401

    data = request.get_json()
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"error": "Leere Nachricht"}), 400

    try:
        study_day = get_active_study_day()
        chat_history = load_chat_history_from_seafile(day=study_day)

        if chat_is_expired(chat_history):
            return jsonify({
                "error": "Die Gesprächszeit für diesen Tag ist abgelaufen. Bitte lade die Seite neu, um mit dem nächsten Tag fortzufahren.",
                "expired": True,
                "study_day": study_day
            }), 409

        update_participant_memory_from_message(user_message)

        model_history = chat_history.copy()
        model_history.append({
            "role": "user",
            "content": user_message
        })

        reply = ask_mistral(model_history, study_day=study_day)
        now = utc_now_iso()

        # Nur anonymisierte Inhalte speichern; Name/Fake-Name wird separat als Memory gespeichert, damit Lumi ihn an Folgetagen verwenden kann.
        chat_history.append({
            "role": "user",
            "content": anonymize_text(user_message),
            "timestamp": now,
            "study_day": study_day
        })

        chat_history.append({
            "role": "assistant",
            "content": anonymize_text(reply),
            "timestamp": now,
            "study_day": study_day
        })

        save_chat_history_to_seafile(chat_history, day=study_day)

        return jsonify({
            "reply": reply,
            "study_day": study_day,
            "expired": False
        })
    except Exception as e:
        print("Fehler:", repr(e))
        return jsonify({"error": str(e)}), 500


@app.route("/test_db")
def test_db():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT NOW();")
        now = cur.fetchone()
        cur.close()
        conn.close()
        return jsonify({
            "database_connected": True,
            "server_time": str(now[0])
        })
    except Exception as e:
        return jsonify({
            "database_connected": False,
            "error": str(e)
        }), 500


@app.route("/test_chatfile")
def test_chatfile():
    if not require_login():
        return jsonify({"error": "Nicht eingeloggt"}), 401

    return jsonify({
        "username": session.get("username"),
        "chat_filename": get_chat_filename(),
        "chat_path": get_chat_path()
    })


@app.route("/test_seafile")
def test_seafile():
    if not require_login():
        return jsonify({"error": "Nicht eingeloggt"}), 401

    headers = {
        "Authorization": f"Token {SEAFILE_TOKEN}",
        "Accept": "application/json"
    }

    url = f"{SEAFILE_BASE_URL}/api2/repos/"
    response = requests.get(url, headers=headers, timeout=30)

    return jsonify({
        "status_code": response.status_code,
        "response_text": response.text,
        "base_url": SEAFILE_BASE_URL,
        "repo_id": SEAFILE_REPO_ID,
        "username": session.get("username"),
        "current_chat_file": get_chat_filename()
    })


@app.route("/test_anonymization")
def test_anonymization():
    sample = (
        "Ich heiße Lisa Müller, wohne in Mainz, "
        "meine Adresse ist Musterstraße 12. "
        "Ich war mit Paul einkaufen und habe Anna getroffen. "
        "Mein Freund Max war auch dabei. "
        "Ich wohne in Bad Kreuznach. "
        "Meine E-Mail ist lisa@example.com, "
        "meine Telefonnummer ist 0171 1234567 "
        "und meine PLZ ist 55116."
    )

    return jsonify({
        "original": sample,
        "anonymized": anonymize_text(sample)
    })


@app.route("/healthz")
def healthz():
    return "ok", 200

@app.route("/test_models")
def test_models():
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}"
    }

    response = requests.get(
        "https://ki-chat.uni-mainz.de/api/models",
        headers=headers,
        timeout=30
    )

    try:
        data = response.json()
    except Exception:
        data = response.text

    return jsonify({
        "status_code": response.status_code,
        "data": data
    })

@app.route("/test_users")
def test_users():
   try:
       conn = get_db_connection()
       cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
       cur.execute("SELECT id, username, created_at FROM users ORDER BY id;")
       rows = cur.fetchall()
       cur.close()
       conn.close()
       return jsonify(rows)
   except Exception as e:
       return jsonify({"error": str(e)}), 500

@app.route("/test_session")

def test_session():

    return jsonify({

        "session_username": session.get("username"),

        "logged_in": require_login()

    })
 

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)