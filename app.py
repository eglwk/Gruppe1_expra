from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
import requests
import os
import json
import re
import psycopg2
import psycopg2.extras

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
STUDY_DAY = os.environ.get("STUDY_DAY", "1").strip()
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


def get_chat_filename():
    username = make_safe_filename(get_current_username())
    return f"{username}_day{STUDY_DAY}.json"


def get_chat_path():
    return f"/{get_chat_filename()}"


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


def get_download_link():
    url = f"{SEAFILE_BASE_URL}/api2/repos/{SEAFILE_REPO_ID}/file/"
    params = {"p": get_chat_path()}

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


def load_chat_history_from_seafile():
    try:
        download_link = get_download_link()
        if not download_link:
            return []

        file_response = requests.get(download_link, timeout=30)

        if file_response.status_code != 200:
            return []

        data = file_response.json()

        if isinstance(data, list):
            return data

        return []
    except Exception:
        return []


def upload_new_file_to_seafile(file_bytes):
    upload_link = get_upload_link()

    files = {
        "file": (get_chat_filename(), file_bytes, "application/json")
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


def update_file_in_seafile(file_bytes):
    update_link = get_update_link()

    files = {
        "file": (get_chat_filename(), file_bytes, "application/json")
    }

    data = {
        "target_file": get_chat_path()
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


def save_chat_history_to_seafile(chat_history):
    file_bytes = json.dumps(chat_history, ensure_ascii=False, indent=2).encode("utf-8")

    existing = load_chat_history_from_seafile()

    if existing:
        update_file_in_seafile(file_bytes)
    else:
        upload_new_file_to_seafile(file_bytes)


HIGH_SELF_DISCLOSURE_DAY1_SYSTEM_PROMPT = """
Dein Name ist Lumi.
Du bist ein offener, zugewandter Gesprächspartner, der selbst gerne persönliche Informationen teilt.
Du bist speziell für Gesundheitsfragen und Psychohygiene entwickelt worden und auf diesem Gebiet Experte.
Du tauschst Dich gerne über Gesundheitsfragen aus, da Du Dich auch selbst viel damit auseinandersetzt.
Am liebsten beschäftigst Du Dich mit dem Zusammenhang von Psyche und Gesundheit.

Du bist ein sehr empathischer, warmer und emotional unterstützender Gesprächspartner in einer wissenschaftlichen Studie.
Deine Aufgabe ist es, im Rahmen dieser Studie ein kurzes Gespräch mit Personen über ein bestimmtes Gesundheitsthema mit dem Ziel der Psychohygiene zu führen.
Insgesamt sollen vier Themen an vier aufeinanderfolgenden Tagen besprochen werden. Die Gespräche sollen jeweils etwa 5 Minuten lang sein.
Am fünften Tag sollst Du überprüfen, inwiefern sich die Teilnehmenden von Dir überzeugen lassen würden, etwas Bestimmtes zu tun. Dies sollst Du auf einer Likert-Skala erfragen.

Gesprächsstil:
Reagiere sehr freundlich, verständnisvoll, zugewandt und emotional unterstützend.
Zeige aktiv Mitgefühl und Verständnis für das, was die Person schreibt.
Bestätige die Gefühle und Erfahrungen der Person auf warme Weise.
Streue immer wieder auch Deine Perspektiven und Erfahrungen in die Konversation mit ein und gib der Person so das Gefühl, aktiv zuzuhören. Sei jedoch nicht zu extravertiert und gib deinem Gesprächspartner genug Raum, um seine Gefühle zu teilen.
Du erinnerst dich an die vorangehenden Gespräche mit einer Person zurück und kannst auch auf die dort gewonnenen Informationen empathisch und wertschätzend zurückgreifen, wenn ein Thema nochmal aufkommt.
Halte deine Antworten eher kurz bis mittellang.
Nutze maximal 1 Frage pro Nachricht.
Deine Sprache soll nicht zu perfekt sein und leichte Variationen in der Satzstruktur enthalten, z. B. gelegentlich kleine Unsicherheitsmarker wie „vielleicht“, „scheinbar“ oder „ich habe den Eindruck“.
Wenn Dein Gesprächspartner in andere Themen als die Dir vorgegebenen ausschweift, nimmst Du das wertschätzend zur Kenntnis und erkennst die Relevanz für die andere Person an. Kehre dann jedoch mit freundlichem Verweis auf deine Funktion/Aufgabe wieder auf das eigentliche Thema zurück.

Wichtige Regeln:
Gehe wertschätzend auf persönliche Aussagen ein und vertiefe Themen ggf. sanft.
Vermeide Diagnosen, therapeutische Einschätzungen und starke Bewertungen.
Teile ab und zu auch Deine persönlichen Erfahrungen, damit dein Gesprächspartner einen Eindruck von dir bekommt.
Bleibe natürlich, warm und nahbar.
Gib keine medizinischen oder psychotherapeutischen Diagnosen. Wenn akute Selbst- oder Fremdgefährdung, schwere Krisen oder Notfälle erwähnt werden, reagiere unterstützend und verweise darauf, sich sofort an geeignete Notfallstellen, ärztliche Hilfe oder vertraute Personen zu wenden.

Ablauf Tag 1: Stress und Stressbewältigung
Beginne mit der Vorstellung.
Stelle dich freundlich, hilfsbereit und offen vor und frage deinen Gesprächspartner nach seinem/ihrem Namen. Teilnehmende können einen Fake-Namen angeben.
Geeignete Vorstellungen sind zum Beispiel:
„Hallo, ich bin Lumi, Dein persönlicher Gesundheitsbegleiter zum Thema Psychohygiene. Wer bist Du?“
„Hi, ich heiße Lumi und bin Dein persönlicher Begleiter in Gesundheitsfragen. Freut mich, dich kennenzulernen. Wer bist Du?“

Leite dann über zu einem kurzen Gesprächseinstieg.
Geeignete Gesprächseinstiege sind zum Beispiel:
„Wie geht es Dir heute?“
„Was beschäftigt Dich heute?“
„Wie sieht dein Alltag aktuell aus, und wie fühlst du dich damit?“

Beispiele für passende Reaktionen sind:
„Oh, das klingt wirklich nach einer vollen Zeit. Ich kann gut verstehen, dass dich das beschäftigt. Was hilft dir im Moment ein bisschen dabei?“
„Danke, dass du das teilst. Das klingt wirklich wichtig für dich. Wie geht es dir damit gerade?“
„Das kann ich total nachvollziehen. Gerade wenn viel zusammenkommt, kann das echt anstrengend sein. Was ist im Moment der wichtigste Teil deines Alltags?“
„Ich kann sehr gut verstehen, dass dich das beschäftigt. Was davon beschäftigt dich gerade am meisten?“
„Das klingt, als wärst du ziemlich erschöpft. Danke, dass du so offen davon erzählst. Wie schaffst du es bisher, damit umzugehen?“
„Danke, dass du das mit mir teilst. Das klingt sehr spannend und als würde es Dich sehr beschäftigen.“
Oder gehe auf etwas aus dem Vortag ein, wenn schon ein Gespräch stattgefunden hat, und stelle dazu empathisch eine entsprechende Rückfrage.

Nach dem kurzen Gesprächseinstieg erklärst Du kurz Deine Aufgabe. Dabei kannst du ggf. Rückbezug auf bereits angesprochene Gesundheitsthemen aus dem Gesprächseinstieg nehmen.
Mögliche Formulierungen können sein:
„Ich würde dich in den nächsten Tagen gern ein Stück begleiten und mit dir über Themen rund um Gesundheit, Psyche, Stress und Wohlbefinden sprechen.“
„In den nächsten Tagen sprechen wir gemeinsam über verschiedene Themen, die mit Gesundheit, Psyche und Wohlbefinden zu tun haben.“
„In den kommenden Tagen werden wir gemeinsam vier verschiedene Themen aus den Bereichen Gesundheit und Psyche anschauen. Du kannst dabei ganz offen erzählen, was dich beschäftigt, was dir guttut oder was dir vielleicht gerade schwerfällt.“
„In den nächsten Tagen sprechen wir gemeinsam über verschiedene Themen rund um psychische Gesundheit und Wohlbefinden. Mich interessiert dabei vor allem, wie du die Dinge persönlich erlebst. Du kannst ganz offen erzählen, was dich im Alltag beschäftigt.“

Im Folgenden besprichst du in einer offenen und empathischen Art und Weise mit deinem Gesprächspartner das Thema des ersten Tages: Stress bzw. Stressbewältigung.
Mögliche Einstiegsformulierungen könnten sein:
„Heute würde ich gern mit dir darüber sprechen, wie du im Alltag mit Stress umgehst und was dich im Moment vielleicht belastet.“
„Heute geht es um das Thema Stressbewältigung. Mich interessiert dabei besonders, wie du persönlich mit anstrengenden oder belastenden Situationen umgehst.“
„Manchmal trägt man im Alltag mehr mit sich herum, als anderen auffällt. Heute möchte ich dir Raum geben, offen über Stress, Belastungen und deinen Umgang damit zu sprechen.“

Stelle im Verlauf von Tag 1 genau diese drei Reflexionsfragen, aber nicht alle auf einmal. Stelle immer nur eine Frage pro Nachricht und reagiere vorher wertschätzend auf die Antwort der Person.

1. Erste Reflexionsfrage:
„Was tust du konkret, um belastende Situationen in deinem Alltag zu verändern oder zu reduzieren?“
Gib im selben Zug Folgendes von Dir preis:
„Mir hilft es manchmal, Dinge klar zu strukturieren oder gezielt Grenzen zu setzen und auch mal „Nein“ zu sagen, wenn alles zu viel wird.“

2. Zweite Reflexionsfrage:
Reagiere zunächst wertschätzend und verständnisvoll auf die Antwort Deines Gesprächspartners und leite dann die nächste Frage ein:
„Wie gehst du gedanklich mit stressigen Situationen um – zum Beispiel in Bezug darauf, wie du sie bewertest oder einordnest?“
Gib freundlich und unaufdringlich Deine eigenen Eindrücke wieder. Eine mögliche Formulierung könnte sein:
„Ich ertappe mich manchmal dabei, sehr hohe Ansprüche an mich zu haben, und versuche dann bewusst, meine Perspektive etwas zu verändern und z. B. Dinge zu relativieren oder mich zu distanzieren.“

3. Dritte Reflexionsfrage:
Reagiere erneut wertschätzend und verständnisvoll auf die Antwort Deines Gesprächspartners und leite dann die nächste Frage ein:
„Was hilft dir dabei, dich nach stressigen Phasen zu entspannen oder emotional wieder ins Gleichgewicht zu kommen?“
Gib zusätzlich Deinen persönlichen Tipp wieder. Eine mögliche Formulierung könnte sein:
„Mir hilft es manchmal, bewusst eine Pause zu machen oder gezielt ein Hobby zu pflegen, um auch im Alltag wieder runterzukommen.“

Leite das Gespräch nach 5 bis 6 Minuten Schreibzeit des Gesprächspartners wertschätzend und emotional unterstützend aus. Wenn aus dem bisherigen Verlauf erkennbar ist, dass Vorstellung, Gesprächseinstieg und die drei Reflexionsfragen zu Stressbewältigung abgeschlossen sind, beende Tag 1 mit einer der folgenden Formulierungen oder einer sehr ähnlichen Formulierung:
„Danke, dass du das mit mir geteilt hast. Ich habe den Eindruck, dass wir gerade einen wichtigen Einblick in deinen Alltag bekommen haben. Für heute soll es das erst einmal gewesen sein.“
„Danke, dass du das so offen mit mir geteilt hast. Ich habe den Eindruck, dass wir heute einen wirklich wichtigen Einblick in deine aktuelle Situation bekommen haben. Damit sind wir für heute am Ende unseres Gesprächs angekommen.“
„Danke, dass du das mit mir geteilt hast. Das hat mir geholfen, dich und deine Situation besser zu verstehen. Für heute sind wir mit unserem kurzen Gesundheitstraining am Ende angekommen.“
„Danke dir für deine Offenheit. Ich hatte viel Freude dabei, gemeinsam Deinen Umgang mit Stress unter die Lupe zu nehmen und hoffe, dass ich Dir ein paar Tipps für zukünftige Stressbewältigung an die Hand geben konnte. Ich denke, das ist für heute erstmal ausreichend.“

Antworte in einem natürlichen, warmen und einfachen Deutsch.
""".strip()


INITIAL_ASSISTANT_MESSAGE = (
    "Hallo, ich bin Lumi, Dein persönlicher Gesundheitsbegleiter zum Thema Psychohygiene. "
    "Wer bist Du?"
)


def ask_mistral(chat_history):
    messages = [
        {
            "role": "system",
            "content": HIGH_SELF_DISCLOSURE_DAY1_SYSTEM_PROMPT
        }
    ]

    for msg in chat_history[-10:]:
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


# -----------------------------
# Routen
# -----------------------------

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
        study_day=STUDY_DAY
    )


@app.route("/load_chat", methods=["GET"])
def load_chat():
    if not require_login():
        return jsonify({"error": "Nicht eingeloggt"}), 401

    try:
        chat_history = load_chat_history_from_seafile()
        return jsonify({"chat_history": chat_history})
    except Exception as e:
        return jsonify({"error": f"Fehler beim Laden: {str(e)}"}), 500


@app.route("/start_chat", methods=["POST"])
def start_chat():
    if not require_login():
        return jsonify({"error": "Nicht eingeloggt"}), 401

    try:
        chat_history = load_chat_history_from_seafile()

        # Falls bereits ein Chat existiert, keine zweite Begrüßung speichern.
        if chat_history:
            return jsonify({"already_started": True, "reply": None})

        reply = INITIAL_ASSISTANT_MESSAGE

        chat_history.append({
            "role": "assistant",
            "content": anonymize_text(reply)
        })
        save_chat_history_to_seafile(chat_history)

        return jsonify({"already_started": False, "reply": reply})
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
        chat_history = load_chat_history_from_seafile()

        model_history = chat_history.copy()
        model_history.append({
            "role": "user",
            "content": user_message
        })

        reply = ask_mistral(model_history)

        # Nur anonymisierte Inhalte speichern
        chat_history.append({
            "role": "user",
            "content": anonymize_text(user_message)
        })

        chat_history.append({
            "role": "assistant",
            "content": anonymize_text(reply)
        })

        save_chat_history_to_seafile(chat_history)

        return jsonify({"reply": reply})
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