"""
TenangTugas - web untuk membantu mahasiswa mengelola tugas dan menurunkan stres akademik.

Cara menjalankan:
    pip install -r requirements.txt
    python app.py
Lalu buka http://127.0.0.1:5000 di browser.

Data akun dan aplikasi disimpan di database; lokal memakai SQLite, hosting memakai DATABASE_URL.
Web ini bukan pengganti layanan kesehatan mental.
"""
import copy
import json
import os
import uuid
from datetime import date, timedelta, datetime, timezone
from functools import wraps
from math import ceil

from flask import Flask, g, redirect, render_template, request, session, url_for, flash
from jinja2 import DictLoader
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from werkzeug.local import LocalProxy
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy.pool import NullPool

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from sqlalchemy.pool import NullPool

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "local-development-only-secret")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
)
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    if os.environ.get("VERCEL"):
        raise RuntimeError("DATABASE_URL belum diatur di Environment Variables Vercel.")
    DATABASE_URL = "sqlite:///app.db"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine_options = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite:"):
    engine_options["connect_args"] = {"check_same_thread": False}
else:
    # Wajib untuk pooler Supabase (port 6543, mode transaction)
    engine_options["connect_args"] = {"prepare_threshold": None}
    if os.environ.get("VERCEL"):
        engine_options["poolclass"] = NullPool
engine = create_engine(DATABASE_URL, **engine_options)

with engine.begin() as koneksi:
    koneksi.execute(text("""
        CREATE TABLE IF NOT EXISTS users (
            id VARCHAR(36) PRIMARY KEY,
            nama VARCHAR(120) NOT NULL,
            email VARCHAR(320) NOT NULL UNIQUE,
            sandi VARCHAR(255) NOT NULL,
            state_data TEXT NOT NULL
        )
    """))
    if not DATABASE_URL.startswith("sqlite:"):
        # Jaga-jaga kalau tabel users di Supabase belum punya kolom state_data
        koneksi.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS state_data TEXT NOT NULL DEFAULT '{}'"
        ))

# GANTI DENGAN LINK ASLI KONSELING KAMPUSMU
KONSELING_URL = "https://www.example.ac.id/layanan-konseling"
# Ganti ID ini dengan ID playlist Spotify Ruang Tenang milikmu.
SPOTIFY_PLAYLIST_ID = "37i9dQZF1DX4sWSpwq3LiO"

HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
HARI_PENDEK = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]
BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember"]
MOODS = ["Tenang", "Baik", "Biasa saja", "Cemas", "Kewalahan"]  # skor stres 1..5
MOOD_EMOJI = ["😌", "🙂", "😐", "😟", "😣"]
KESULITAN = ["Mudah", "Sedang", "Sulit"]
# Total durasi langkah kecil, ditentukan dari tingkat kesulitan
MENIT_KESULITAN = {"Mudah": 50, "Sedang": 75, "Sulit": 100}
JAM_DEFAULT = "23:59"

def sekarang():
    # Di komputer sendiri memakai jam komputer. Di Vercel (server UTC) memakai
    # ZONA_UTC: 7 = WIB, 8 = WITA, 9 = WIT.
    zona = os.environ.get("ZONA_UTC") or ("7" if os.environ.get("VERCEL") else None)
    if zona is None:
        return datetime.now()
    return datetime.now(timezone(timedelta(hours=int(zona)))).replace(tzinfo=None)

def hari_ini_lokal():
    return sekarang().date()

# ----------------------------------------------------------------------------
# Data per akun
# ----------------------------------------------------------------------------
STATE_AWAL = {
    "tasks": [],
    "next_id": 1,
    "stres": {},
    "fokus_menit": 0,
    "jurnal": [],
    "braindump": [],
}
STATE = LocalProxy(lambda: g.user_state)

def serialisasi_state(state):
    data = copy.deepcopy(state)
    data["stres"] = {hari.isoformat(): nilai for hari, nilai in state["stres"].items()}
    data["tasks"] = [
        {**tugas, "deadline": tugas["deadline"].isoformat() if tugas["deadline"] else None}
        for tugas in state["tasks"]
    ]
    data["jurnal"] = [
        {**catatan, "tgl": catatan["tgl"].isoformat()}
        for catatan in state["jurnal"]
    ]
    return json.dumps(data)

def pulihkan_state(state_data):
    state = copy.deepcopy(STATE_AWAL)
    if not state_data:
        return state
    state.update(json.loads(state_data))
    state["stres"] = {date.fromisoformat(hari): nilai for hari, nilai in state["stres"].items()}
    for tugas in state["tasks"]:
        if tugas["deadline"]:
            tugas["deadline"] = date.fromisoformat(tugas["deadline"])
    for catatan in state["jurnal"]:
        catatan["tgl"] = date.fromisoformat(catatan["tgl"])
    return state

@app.before_request
def muat_akun():
    user_id = session.get("user_id")
    g.user = None
    g.user_state = copy.deepcopy(STATE_AWAL)
    if user_id:
        with engine.connect() as koneksi:
            akun = koneksi.execute(
                text("SELECT id, nama, email, sandi, state_data FROM users WHERE id = :id"),
                {"id": user_id},
            ).mappings().first()
        if akun:
            g.user = {"id": akun["id"], "nama": akun["nama"], "email": akun["email"], "sandi": akun["sandi"]}
            g.user_state = pulihkan_state(akun["state_data"])
        else:
            session.clear()

@app.after_request
def simpan_state(response):
    if g.user is not None and session.get("user_id") == g.user["id"]:
        with engine.begin() as koneksi:
            koneksi.execute(
                text("UPDATE users SET state_data = :state_data WHERE id = :id"),
                {"state_data": serialisasi_state(g.user_state), "id": g.user["id"]},
            )
    return response

# ----------------------------------------------------------------------------
# Algoritma
# ----------------------------------------------------------------------------
TEMPLAT_LANGKAH = {
    2: ["Kerjakan bagian inti", "Baca ulang dan rapikan"],
    3: ["Kumpulkan bahan", "Kerjakan bagian inti", "Baca ulang dan rapikan"],
    4: ["Kumpulkan bahan", "Susun kerangka", "Kerjakan bagian inti", "Baca ulang dan rapikan"],
    5: ["Cari 3 referensi", "Tulis pendahuluan", "Susun kerangka", "Buat draft bagian inti",
        "Baca ulang dan rapikan"],
    6: ["Cari 3 referensi", "Tulis pendahuluan", "Susun kerangka", "Buat draft bagian inti",
        "Tulis kesimpulan", "Baca ulang dan rapikan"],
}

def pecah_tugas(menit, kesulitan):
    n = ceil(menit / 25)
    if kesulitan == "Sulit":
        n += 1
    n = max(2, min(6, n))
    nama = TEMPLAT_LANGKAH[n]
    bobot = [2 if "inti" in s else 1 for s in nama]
    total = sum(bobot)
    hasil = []
    for s, b in zip(nama, bobot):
        durasi = max(5, round(menit * b / total / 5) * 5)
        hasil.append({"t": s, "m": durasi, "done": False})
    selisih = round(menit / 5) * 5 - sum(l["m"] for l in hasil)
    inti = next(l for l in hasil if "inti" in l["t"])
    inti["m"] = max(5, inti["m"] + selisih)
    return hasil

def jam_tugas(t):
    return t.get("jam") or JAM_DEFAULT  # tugas lama belum punya jam

def waktu_tenggat(t):
    if t["deadline"] is None:
        return datetime.max
    return datetime.combine(t["deadline"], datetime.strptime(jam_tugas(t), "%H:%M").time())

def terlambat(t):
    return waktu_tenggat(t) < sekarang()

def tugas_mendesak():
    # Sudah lewat, tenggat hari ini, atau tenggat besok
    hasil = [t for t in aktif() if t["deadline"] and sisa_hari(t) <= 1]
    return sorted(hasil, key=waktu_tenggat)

def fmt_tenggat(t):
    if t["deadline"] is None:
        return "-"
    return f"{fmt_tgl(t['deadline'])}, pukul {jam_tugas(t).replace(':', '.')}"

def sisa_hari(t):
    return None if t["deadline"] is None else (t["deadline"] - hari_ini_lokal()).days

def kuadran(t):
    s = sisa_hari(t)
    mendesak = s is not None and s <= 3
    penting = t["kesulitan"] != "Mudah"
    if mendesak and penting:
        return 0
    if penting:
        return 1
    if mendesak:
        return 2
    return 3

NAMA_KUADRAN = ["Kerjakan dulu", "Jadwalkan", "Singkat saja", "Lepas dulu"]

def urut_prioritas(tugas):
    return sorted(tugas, key=lambda t: (kuadran(t), waktu_tenggat(t)))

def aktif():
    return [t for t in STATE["tasks"] if not t["selesai"]]

def fokus_tiga():
    return urut_prioritas(aktif())[:3]

def pesan_stres(skor):
    return {
        1: "Stres masih rendah, masih bagus.",
        2: "Stres masih rendah, masih bagus.",
        3: "Stres sedang. Pilih satu langkah kecil saja.",
        4: "Stres mulai naik. Coba jeda sebentar di Ruang Tenang.",
        5: "Stres tinggi. Buka Mode Darurat untuk langkah terkecil dan jeda napas.",
    }[skor]

def langkah_terkecil(n=3):
    semua = []
    for t in aktif():
        for l in t["langkah"]:
            if not l["done"]:
                semua.append((l["m"], l["t"], t["judul"]))
    semua.sort()
    hasil = [f"{t} ({j}, {m} menit)" for m, t, j in semua[:n]]
    cadangan = ["Tarik napas pelan selama 1 menit", "Minum segelas air",
                "Tulis satu hal yang mengganjal di Brain Dump"]
    for c in cadangan:
        if len(hasil) >= n:
            break
        hasil.append(c)
    return hasil

def data_mingguan():
    hari = [hari_ini_lokal() - timedelta(days=i) for i in range(6, -1, -1)]
    nyata = [STATE["stres"].get(d, {}).get("skor") for d in hari]
    contoh = not any(nyata)
    if contoh:
        nyata = [2, 3, 4, 3, 5, 2, 1]
    return [(HARI_PENDEK[d.weekday()], s or 0) for d, s in zip(hari, nyata)], contoh

def pola_stres(data):
    puncak = max(data, key=lambda x: x[1])
    if puncak[1] == 0:
        return "Belum ada pola. Isi cek stres harian dulu."
    return f"Stresmu paling tinggi di hari {HARI[HARI_PENDEK.index(puncak[0])]}. Perhatikan tugas atau tenggat di hari itu."

def jadwal_hari_ini(mulai="10:00"):
    jam = datetime.strptime(mulai, "%H:%M")
    blok = []
    for t in fokus_tiga():
        for l in t["langkah"]:
            if l["done"] or len(blok) >= 5:
                continue
            akhir = jam + timedelta(minutes=min(l["m"], 50))
            blok.append({"waktu": f"{jam:%H.%M}-{akhir:%H.%M}", "judul": l["t"], "sub": t["judul"], "istirahat": False})
            jam = akhir
            blok.append({"waktu": f"{jam:%H.%M}-{(jam + timedelta(minutes=5)):%H.%M}", "judul": "Istirahat", "sub": "", "istirahat": True})
            jam += timedelta(minutes=5)
        if len(blok) >= 5:
            break
    if blok and blok[-1]["istirahat"]:
        blok.pop()
    return blok

def fmt_tgl(d):
    return "-" if d is None else f"{HARI[d.weekday()]}, {d.day} {BULAN[d.month - 1]} {d.year}"

def fmt_menit(m):
    j, s = divmod(int(m), 60)
    return f"{j} j {s} m" if j else f"{s} m"

def cari_tugas(tid):
    return next((t for t in STATE["tasks"] if t["id"] == tid), None)

def tambah_tugas(judul, matkul, deadline, jam, kesulitan):
    t = {"id": STATE["next_id"], "judul": judul.strip(), "matkul": matkul.strip() or "-",
         "deadline": deadline, "jam": jam, "kesulitan": kesulitan, "langkah": [], "selesai": False}
    STATE["next_id"] += 1
    STATE["tasks"].append(t)
    return t

def baca_tanggal(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except ValueError:
        return None

def baca_jam(s, default=JAM_DEFAULT):
    try:
        return datetime.strptime(s, "%H:%M").strftime("%H:%M")
    except (TypeError, ValueError):
        return default

def baca_menit(s, default=60):
    try:
        return max(5, int(s))
    except (TypeError, ValueError):
        return default

def wajib_masuk(f):
    @wraps(f)
    def bungkus(*a, **k):
        if g.user is None:
            return redirect(url_for("daftar"))
        return f(*a, **k)
    return bungkus

@app.context_processor
def konteks():
    jml_aktif = len(aktif()) if g.user else 0
    jml_mendesak = len(tugas_mendesak()) if g.user else 0
    return dict(jml_aktif=jml_aktif, jml_mendesak=jml_mendesak, user=g.user, fmt_tgl=fmt_tgl, fmt_menit=fmt_menit, css=CSS, endpoint=request.endpoint,
                MOODS=MOODS, MOOD_EMOJI=MOOD_EMOJI, KESULITAN=KESULITAN, sisa_hari=sisa_hari,
                kuadran=kuadran, jam_tugas=jam_tugas, fmt_tenggat=fmt_tenggat,
                terlambat=terlambat, NAMA_KUADRAN=NAMA_KUADRAN, KONSELING_URL=KONSELING_URL)

# ----------------------------------------------------------------------------
# Rute: akun dan panduan
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    return redirect(url_for("beranda" if g.user else "daftar"))

@app.route("/daftar", methods=["GET", "POST"])
def daftar():
    tab = request.args.get("tab", "daftar")
    if request.method == "POST":
        aksi = request.form.get("aksi")
        email = request.form.get("email", "").strip().lower()
        sandi = request.form.get("sandi", "")
        if aksi == "daftar":
            nama = request.form.get("nama", "").strip()
            if not nama or "@" not in email or len(sandi) < 8:
                flash("Isi nama, email yang valid, dan kata sandi minimal 8 karakter.")
                return redirect(url_for("daftar"))
            user_id = str(uuid.uuid4())
            sandi_hash = generate_password_hash(sandi)
            try:
                with engine.begin() as koneksi:
                    koneksi.execute(
                        text("""
                            INSERT INTO users (id, nama, email, sandi, state_data)
                            VALUES (:id, :nama, :email, :sandi, :state_data)
                        """),
                        {
                            "id": user_id,
                            "nama": nama,
                            "email": email,
                            "sandi": sandi_hash,
                            "state_data": serialisasi_state(copy.deepcopy(STATE_AWAL)),
                        },
                    )
            except IntegrityError:
                flash("Email ini sudah terdaftar. Silakan masuk.")
                return redirect(url_for("daftar", tab="masuk"))
            session.clear()
            session["user_id"] = user_id
            return redirect(url_for("panduan"))
        with engine.connect() as koneksi:
            u = koneksi.execute(
                text("SELECT id, sandi FROM users WHERE email = :email"),
                {"email": email},
            ).mappings().first()
        if u and check_password_hash(u["sandi"], sandi):
            session.clear()
            session["user_id"] = u["id"]
            return redirect(url_for("beranda"))
        flash("Email atau kata sandi belum cocok.")
        return redirect(url_for("daftar", tab="masuk"))
    return render_template("daftar.html", tab=tab)

@app.route("/panduan")
@wajib_masuk
def panduan():
    return render_template("panduan.html")

@app.route("/keluar")
def keluar():
    session.clear()
    return redirect(url_for("daftar", tab="masuk"))

# ----------------------------------------------------------------------------
# Rute: Beranda dan cek stres
# ----------------------------------------------------------------------------
FITUR = [
    ("Brain Dump", "Tulis semua tugas tanpa harus langsung mengatur.", "braindump", "g"),
    ("Pemecah Tugas", "Pecah tugas besar menjadi langkah-langkah kecil yang mudah dimulai.", "pemecah", "g"),
    ("Matriks Prioritas", "Pilah tugas: mendesak dan penting, penting tapi tidak mendesak, dan seterusnya.", "rencana", "g"),
    ("Kalender dan Pengingat", "Lihat tanggal dan jam tenggat, serta pengingat sebelum deadline.", "tugas", "b"),
    ("Mode Fokus", "Timer Pomodoro 25 menit kerja dan 5 menit istirahat, dengan pelacak progres.", "rencana", "b"),
    ("Cek Stres Harian", "Pilih satu reaksi untuk perasaanmu hari ini, lalu jawab apa yang paling membuatmu cemas.", "beranda", "y"),
    ("Ruang Tenang", "Latihan napas, audio hujan atau white noise, dan jurnal singkat.", "ruang_tenang", "y"),
    ("Mode Darurat", "Saat stres tinggi: 3 langkah terkecil, napas 1 menit, dan tautan layanan konseling kampus.", "darurat", "d"),
]

@app.route("/beranda")
@wajib_masuk
def beranda():
    hari_ini = hari_ini_lokal()
    aktif_list = aktif()
    terdekat = sorted([t for t in aktif_list if t["deadline"]], key=waktu_tenggat)[:3]
    mendesak = tugas_mendesak()
    hari = STATE["stres"].get(hari_ini)
    pesan = pesan_stres(hari["skor"]) if hari else None
    riwayat = sorted(STATE["stres"].items(), reverse=True)[:5]
    mulai = [
        ("Tulis tugas di Brain Dump", bool(STATE["tasks"])),
        ("Pecah satu tugas jadi langkah kecil", any(t["langkah"] for t in STATE["tasks"])),
        ("Isi cek stres hari ini", hari_ini in STATE["stres"]),
        ("Coba Ruang Tenang", bool(STATE["jurnal"])),
    ]
    return render_template("beranda.html", mendesak=mendesak, FITUR=FITUR, ada_tugas=bool(STATE["tasks"]),
        pesan=pesan, riwayat=riwayat, fokus=fokus_tiga(), mulai=mulai, terdekat=terdekat)

@app.route("/cek-stres", methods=["POST"])
@wajib_masuk
def cek_stres():
    mood = request.form.get("mood")
    if mood in MOODS:
        STATE["stres"][hari_ini_lokal()] = {"skor": MOODS.index(mood) + 1,
                                        "catatan": request.form.get("catatan", "").strip()}
    return redirect(url_for("beranda"))

# ----------------------------------------------------------------------------
# Rute: Brain Dump, Tugas, Pemecah Tugas
# ----------------------------------------------------------------------------
@app.route("/braindump", methods=["GET", "POST"])
@wajib_masuk
def braindump():
    if request.method == "POST":
        baris = [b.strip() for b in request.form.get("teks", "").splitlines() if b.strip()]
        if not baris:
            flash("Tulis minimal satu tugas dulu.")
            return redirect(url_for("braindump"))
        STATE["braindump"] = baris
        return redirect(url_for("atur_tenggat"))
    return render_template("braindump.html")

@app.route("/atur-tenggat", methods=["GET", "POST"])
@wajib_masuk
def atur_tenggat():
    baris = STATE["braindump"]
    if request.method == "POST":
        for i, judul in enumerate(baris):
            tambah_tugas(judul, request.form.get(f"matkul{i}", ""), baca_tanggal(request.form.get(f"tgl{i}")),
                         baca_jam(request.form.get(f"jam{i}")), request.form.get(f"sulit{i}", "Sedang"))
        STATE["braindump"] = []
        return redirect(url_for("tugas"))
    if not baris:
        return redirect(url_for("braindump"))
    return render_template("atur_tenggat.html", baris=baris, default=(hari_ini_lokal() + timedelta(days=3)).isoformat())

@app.route("/kalender/<int:id>")
@wajib_masuk
def kalender(id):
    t = cari_tugas(id)
    if not t or not t["deadline"]:
        return "Tugas tidak ditemukan", 404
    mulai = waktu_tenggat(t)
    akhir = mulai + timedelta(hours=1)
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:{mulai:%Y%m%dT%H%M%S}
DTEND:{akhir:%Y%m%dT%H%M%S}
SUMMARY:Tenggat: {t['judul']}
DESCRIPTION:{t.get('matkul', '')} - dari web TenangTugas
END:VEVENT
END:VCALENDAR"""
    from flask import Response
    return Response(ics, mimetype="text/calendar", headers={"Content-Disposition": f"attachment; filename=tugas-{id}.ics"})

@app.route("/tugas")
@wajib_masuk
def tugas():
    f = request.args.get("f", "hari")
    h = hari_ini_lokal()
    daftar_ = STATE["tasks"]
    if f == "selesai":
        hasil = [t for t in daftar_ if t["selesai"]]
    elif f == "terlambat":
        hasil = [t for t in aktif() if terlambat(t)]
    elif f == "mendesak":
        hasil = tugas_mendesak()
    elif f == "minggu":
        hasil = [t for t in aktif() if t["deadline"] is None or h <= t["deadline"] <= h + timedelta(days=6)]
    else:
        f = "hari"
        hasil = [t for t in aktif() if t["deadline"] == h]
    return render_template("tugas.html", f=f, hasil=urut_prioritas(hasil), hari_ini=h.isoformat())

@app.route("/tugas/tambah", methods=["POST"])
@wajib_masuk
def tugas_tambah():
    judul = request.form.get("judul", "").strip()
    if not judul:
        flash("Judul tugas wajib diisi.")
    else:
        tambah_tugas(judul, request.form.get("matkul", ""), baca_tanggal(request.form.get("tgl")),
                     baca_jam(request.form.get("jam")), request.form.get("sulit", "Sedang"))
    return redirect(request.referrer or url_for("tugas"))

@app.route("/tugas/<int:tid>/selesai", methods=["POST"])
@wajib_masuk
def tugas_selesai(tid):
    t = cari_tugas(tid)
    if t:
        t["selesai"] = not t["selesai"]
    return redirect(request.referrer or url_for("tugas"))

@app.route("/tugas/<int:tid>/langkah/<int:i>", methods=["POST"])
@wajib_masuk
def langkah_centang(tid, i):
    t = cari_tugas(tid)
    if t and 0 <= i < len(t["langkah"]):
        t["langkah"][i]["done"] = not t["langkah"][i]["done"]
    return redirect(request.referrer or url_for("tugas"))

@app.route("/pemecah", methods=["GET", "POST"])
@wajib_masuk
def pemecah():
    tid = request.args.get("id", type=int)
    pilih = cari_tugas(tid) if tid else None
    if pilih is None and aktif():
        pilih = aktif()[0]
    if request.method == "POST" and pilih:
        aksi = request.form.get("aksi")
        if aksi == "pecah":
            pilih["matkul"] = request.form.get("matkul", pilih["matkul"]).strip() or "-"
            pilih["deadline"] = baca_tanggal(request.form.get("tgl")) or pilih["deadline"]
            pilih["jam"] = baca_jam(request.form.get("jam"), jam_tugas(pilih))
            pilih["kesulitan"] = request.form.get("sulit", pilih["kesulitan"])
            pilih["langkah"] = pecah_tugas(MENIT_KESULITAN.get(pilih["kesulitan"], 75), pilih["kesulitan"])
        elif aksi == "simpan":
            baru = []
            for i in range(len(pilih["langkah"]) + 1):
                teks = request.form.get(f"t{i}", "").strip()
                if teks and not request.form.get(f"hapus{i}"):
                    lama = pilih["langkah"][i]["done"] if i < len(pilih["langkah"]) else False
                    baru.append({"t": teks, "m": baca_menit(request.form.get(f"m{i}"), 25), "done": lama})
            pilih["langkah"] = baru
            pilih["selesai"] = bool(request.form.get("cukup_baik"))
            flash("Langkah-langkahnya sudah masuk ke rencanamu.")
        return redirect(url_for("pemecah", id=pilih["id"]))
    return render_template("pemecah.html", daftar=aktif(), pilih=pilih)

# ----------------------------------------------------------------------------
# Rute: Rencana Harian, Ruang Tenang, Profil, Darurat
# ----------------------------------------------------------------------------
@app.route("/rencana")
@wajib_masuk
def rencana():
    mulai = request.args.get("mulai", "10:00")
    try:
        blok = jadwal_hari_ini(mulai)
    except ValueError:
        mulai, blok = "10:00", jadwal_hari_ini("10:00")
    kuad = {i: [t for t in aktif() if kuadran(t) == i] for i in range(4)}
    return render_template("rencana.html", blok=blok, mulai=mulai, kuad=kuad, fokus_menit=STATE["fokus_menit"])

@app.route("/fokus/selesai", methods=["POST"])
@wajib_masuk
def fokus_selesai():
    STATE["fokus_menit"] += 25
    return {"total": STATE["fokus_menit"]}

@app.route("/ruang-tenang", methods=["GET", "POST"])
@wajib_masuk
def ruang_tenang():
    if request.method == "POST":
        teks = request.form.get("jurnal", "").strip()
        if teks:
            STATE["jurnal"].insert(0, {"tgl": hari_ini_lokal(), "teks": teks})
        return redirect(url_for("ruang_tenang"))
    return render_template(
        "ruang_tenang.html",
        jurnal=STATE["jurnal"][:3],
        spotify_playlist_id=SPOTIFY_PLAYLIST_ID,
    )

@app.route("/profil")
@wajib_masuk
def profil():
    data, contoh = data_mingguan()
    saran = ("Coba pecah tugas besar dua hari sebelum tenggat."
             if any(t["kesulitan"] == "Sulit" for t in aktif()) else "Pertahankan ritme kecilmu hari ini.")
    return render_template("profil.html", data=data, contoh=contoh, pola=pola_stres(data), saran=saran,
                           selesai=sum(t["selesai"] for t in STATE["tasks"]), fokus=fmt_menit(STATE["fokus_menit"]))

@app.route("/darurat")
@wajib_masuk
def darurat():
    return render_template("darurat.html", langkah=langkah_terkecil())

# ----------------------------------------------------------------------------
# Tampilan (CSS dan template HTML)
# ----------------------------------------------------------------------------
CSS = """
:root{--cream:#faf5e9;--lg:#e2ece2;--g:#2f5d4a;--dark:#1f2d26;--gray:#555f58;--blue:#dde9f3;--navy:#1f4e6b;
--yel:#f7de8c;--lyel:#fbefc5;--olive:#4a3f10;--line:#e5e0cc;--side:#eef3ec}
*{box-sizing:border-box}body{margin:0;font-family:'Segoe UI',system-ui,sans-serif;background:var(--cream);color:var(--dark)}
a{color:var(--g)}h1{font-size:30px;margin:0 0 6px}h2{font-size:18px;margin:0 0 10px}h3{font-size:15px;margin:0 0 6px}
.sub{color:var(--gray);margin:0 0 22px}.app{display:flex;min-height:100vh}
aside{width:240px;background:var(--side);padding:20px;display:flex;flex-direction:column;gap:6px;position:sticky;top:0;height:100vh}
.logo{font-weight:700;font-size:20px;color:var(--g);margin-bottom:22px}.logo:before{content:"🍃 "}
aside a.m{padding:12px 16px;border-radius:12px;text-decoration:none;color:var(--dark)}aside a.m.on{background:#fff;font-weight:700}
aside .bawah{margin-top:auto;display:flex;flex-direction:column;gap:8px}
.kecil{padding:10px;border-radius:12px;text-align:center;text-decoration:none;font-size:14px;border:1px solid var(--line);background:#fff;color:var(--dark)}
.kuning{background:var(--lyel);font-weight:700;color:var(--olive)}
main{flex:1;padding:36px 40px;max-width:1200px}.grid2{display:grid;grid-template-columns:1.6fr 1fr;gap:20px;align-items:start}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.card{background:#fff;border:1px solid var(--line);border-radius:22px;padding:22px;margin-bottom:18px}
.hero{background:var(--g);color:#fff;border:0}.hero p{color:#e2ece2}.yel{background:var(--yel);border:0}.lyel{background:var(--lyel);border:0}
.blue{background:var(--blue);border:0}.dash{background:transparent;border:2px dashed #b5b09a}
.btn{display:inline-block;border:0;border-radius:999px;padding:12px 22px;background:var(--g);color:#fff;font-weight:700;font-size:14px;cursor:pointer;text-decoration:none}
.btn.o{background:transparent;color:var(--g);border:2px solid var(--g)}.btn.y{background:var(--yel);color:var(--olive)}.btn.full{width:100%;text-align:center}
input,select,textarea{width:100%;padding:12px 14px;border-radius:14px;border:1px solid #d9d3bb;background:var(--cream);font:inherit;margin:4px 0 12px}
textarea{min-height:110px}label{font-size:13px;font-weight:600;color:var(--gray)}
.pill{display:inline-block;padding:4px 12px;border-radius:99px;font-size:12px;font-weight:700;background:var(--lg);color:var(--g)}
.pill.s{background:var(--lyel);color:var(--olive)}.row{display:flex;justify-content:space-between;align-items:center;gap:10px}
.flash{background:var(--lyel);padding:12px 16px;border-radius:14px;margin-bottom:16px}.meta{color:var(--gray);font-size:13px}
.tabs{display:flex;gap:8px;margin-bottom:18px;flex-wrap:wrap}.tabs a{padding:9px 20px;border-radius:99px;border:2px solid var(--line);background:#fff;text-decoration:none;font-weight:700;color:var(--dark)}
.tabs a.on{background:var(--g);color:#fff;border-color:var(--g)}
.tugas{border-left:6px solid var(--g)}.tugas.q1{border-left-color:var(--yel)}.tugas.q2,.tugas.q3{border-left-color:#c5d9ea}
.moods{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.moods label{display:block;cursor:pointer}.moods input{display:none}
.moods span{display:block;padding:8px 10px;border:1px solid var(--line);border-radius:12px;text-align:center;font-size:12px;background:#fff;min-width:68px}
.moods span b{display:block;font-size:22px}.moods input:checked+span{background:var(--lg);border:2px solid var(--g)}
.fitur{display:block;text-decoration:none;color:var(--dark);background:#fff;border:1px solid var(--line);border-radius:16px;padding:16px}
.fitur:before{content:"";display:block;width:9px;height:9px;border-radius:50%;margin-bottom:10px;background:var(--g)}
.fitur.b:before{background:var(--navy)}.fitur.y:before{background:#c9a227}.fitur.d{background:var(--lyel)}.fitur.d:before{background:var(--olive)}
.fitur p{margin:4px 0 0;font-size:13px;color:var(--gray)}
.blok{display:flex;gap:16px;margin-bottom:10px;align-items:center}.blok .w{width:110px;font-weight:700;color:#3d6b54}
.blok .i{flex:1;background:#dce9de;border-radius:14px;padding:12px 16px}.blok .i.r{background:var(--lyel);font-weight:700}
.q{border-radius:14px;padding:12px;font-size:13px;min-height:80px}.q b{display:block}
.bar{display:flex;align-items:flex-end;gap:14px;height:210px;border-bottom:2px solid #c9d6cb;padding:0 14px}
.bar div{flex:1;background:#6f9b80;border-radius:10px 10px 0 0}.bar div.max{background:#dfa93d}
.lab{display:flex;gap:14px;padding:6px 14px;font-weight:700;font-size:12px;color:var(--gray)}.lab span{flex:1;text-align:center}
.stat{display:flex;gap:14px}.stat div{flex:1;border-radius:16px;padding:14px;background:var(--lg);font-size:13px}.stat b{display:block;font-size:26px}
.stat div.b{background:var(--blue);color:var(--navy)}
.timer{font-size:44px;font-weight:700;text-align:center;border:10px solid #6f9b80;border-radius:50%;width:170px;height:170px;display:flex;align-items:center;justify-content:center;margin:0 auto 14px}
.napas{width:120px;height:120px;border-radius:50%;background:#6f9b80;margin:10px auto;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;text-align:center;
transition:transform 4s ease-in-out;box-shadow:0 0 0 20px #bdd3e3,0 0 0 40px #dde9f3}
.spotify-embed{width:100%;height:152px;border:0;border-radius:16px;margin-top:8px}
.auth{display:grid;grid-template-columns:1fr 1fr;min-height:100vh}.auth .kiri{background:var(--lg);padding:44px;display:flex;flex-direction:column}
.auth .kanan{display:flex;align-items:center;justify-content:center;padding:30px}.auth form{width:100%;max-width:404px}
.auth h1.big{font-size:42px;line-height:1.1;margin:40px 0 14px}.auth ul{list-style:none;padding:0;line-height:2}
.catatan{margin-top:auto;background:#fff;border-radius:20px;padding:22px;font-size:13px;color:var(--gray)}
.top{display:flex;justify-content:space-between;align-items:center;padding:16px 52px;border-bottom:1px solid var(--line)}
.wrap{padding:36px 52px;max-width:1150px}
.profil{background:#fff;border:1px solid var(--line);border-radius:28px;overflow:hidden;margin:16px 0 26px;box-shadow:0 12px 32px rgba(47,93,74,.12)}
.profil .sampul{height:120px;padding:20px 28px;color:#fff;font-weight:700;background:radial-gradient(circle at 85% 20%,rgba(247,222,140,.75),transparent 32%),radial-gradient(circle at 65% 120%,rgba(221,233,243,.6),transparent 40%),linear-gradient(120deg,#2f5d4a,#6f9b80)}
.profil .isi{display:flex;align-items:flex-start;gap:18px;padding:0 28px 18px;flex-wrap:wrap}
.profil .avatar{width:96px;height:96px;margin-top:-44px;border-radius:50%;border:5px solid #fff;background:linear-gradient(145deg,#f7de8c,#e2ece2);color:var(--g);display:flex;align-items:center;justify-content:center;font-size:40px;font-weight:800;box-shadow:0 6px 16px rgba(31,45,38,.18);flex-shrink:0;position:relative}
.profil .identitas{flex:1;min-width:180px;padding-top:12px}.profil .identitas h2{font-size:26px;margin:0;text-transform:capitalize;overflow-wrap:anywhere}
.profil .identitas p{margin:4px 0 0;color:var(--gray);font-size:14px;overflow-wrap:anywhere}
.profil .keluar{margin-top:18px;padding:10px 20px;border-radius:99px;background:#fff0f0;border:1px solid #f3caca;color:#8b1e1e;font-weight:700;font-size:14px;text-decoration:none}.profil .keluar:hover{background:#ffe0e0}
.profil .kutipan{margin:0 28px 14px;padding:13px 18px;border-radius:16px;background:var(--lg);color:var(--g);font-size:14px;font-weight:600}
.profil .idakun{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 28px 22px;padding-top:14px;border-top:1px dashed var(--line);font-size:12px;color:var(--gray)}
.profil .idakun code{overflow-wrap:anywhere;min-width:0}.profil .idakun button{border:1px solid var(--line);background:var(--cream);color:var(--g);border-radius:99px;padding:5px 14px;font-weight:700;cursor:pointer}
/* Animasi tombol */
.btn,.kecil,.tabs a,aside a.m{transition:transform .2s ease,box-shadow .2s ease,filter .2s ease,background-color .2s ease}
.btn:hover,.kecil:hover,.tabs a:hover{transform:translateY(-2px);box-shadow:0 8px 18px rgba(31,45,38,.18);filter:brightness(1.05)}
.btn:active,.kecil:active,.tabs a:active{transform:scale(.96);box-shadow:none}
aside a.m:hover{background:#fff;transform:translateX(5px)}
.btn{position:relative;overflow:hidden}
.btn::after{content:"";position:absolute;top:0;bottom:0;left:-60%;width:40%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.4),transparent);transform:skewX(-20deg);pointer-events:none}
.btn:hover::after{left:130%;transition:left .6s ease}
.btn:focus-visible,.kecil:focus-visible,.tabs a:focus-visible,.fitur:focus-visible,aside a.m:focus-visible{outline:3px solid #dfa93d;outline-offset:2px}
/* Hover kartu */
.card,.fitur,.profil{transition:transform .25s ease,box-shadow .25s ease}
.card:hover,.profil:hover{transform:translateY(-3px);box-shadow:0 14px 28px rgba(47,93,74,.13)}
.fitur:hover,.fitur:focus-visible{transform:translateY(-6px);box-shadow:0 16px 28px rgba(47,93,74,.18)}
/* Skeleton shimmer saat pemutar Spotify dimuat */
.skeleton{position:relative;height:152px;margin-top:8px;border-radius:16px;overflow:hidden}
.skeleton .spotify-embed{margin:0;display:block}
.skeleton.memuat{background:var(--lg)}
.skeleton.memuat iframe{opacity:0}
.skeleton.memuat::after{content:"";position:absolute;inset:0;pointer-events:none;background:linear-gradient(100deg,transparent 30%,rgba(255,255,255,.8) 50%,transparent 70%);transform:translateX(-100%);animation:shimmer 1.4s infinite}
@keyframes shimmer{to{transform:translateX(100%)}}
@media(prefers-reduced-motion:reduce){.btn,.kecil,.tabs a,aside a.m,.card,.fitur,.profil{transition:none}.btn:hover,.kecil:hover,.tabs a:hover,aside a.m:hover,.card:hover,.fitur:hover,.profil:hover{transform:none}.btn::after{display:none}.skeleton.memuat::after{animation:none;display:none}}
@media(max-width:850px){.app{flex-direction:column}aside{width:100%;height:auto;position:static}.grid2,.grid3,.grid4,.auth{grid-template-columns:1fr}}
"""

# ----------------------------------------------------------------------------
# Motion dan interaksi (hanya di halaman setelah masuk)
# ----------------------------------------------------------------------------
UI_CSS = """
/* Antigravity: gelembung dan daun melayang naik */
.antigravitasi{position:fixed;inset:0;overflow:hidden;pointer-events:none;z-index:0}
.antigravitasi i{position:absolute;bottom:-90px;left:var(--x);width:var(--s);height:var(--s);font-style:normal;font-size:var(--s);line-height:1;opacity:0;animation:melayang var(--d) linear var(--t) infinite}
.antigravitasi i:empty{border-radius:50%;background:radial-gradient(circle at 30% 30%,#fff,#bcd5c4)}
@keyframes melayang{10%{opacity:.55}50%{transform:translate(35px,-55vh) rotate(160deg)}90%{opacity:.35}100%{transform:translate(-20px,-118vh) rotate(340deg);opacity:0}}
.tanpa-gerak .antigravitasi{display:none}.tanpa-gerak .logo:before{animation:none}
main{position:relative;z-index:1;min-width:0}
/* Micro animation */
.logo:before{display:inline-block;margin-right:6px;animation:apung 3.5s ease-in-out infinite}
@keyframes apung{50%{transform:translateY(-5px) rotate(12deg)}}
.moods span{transition:transform .2s ease,background-color .2s}.moods label:hover span{transform:translateY(-4px)}
.moods input:checked+span b{animation:pop .45s ease}
input[type=checkbox]:checked{animation:pop .35s ease}
@keyframes pop{45%{transform:scale(1.35)}}
input,select,textarea{transition:border-color .2s,box-shadow .2s}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--g);box-shadow:0 0 0 4px rgba(47,93,74,.18)}
/* Visual feedback */
.btn,.tab-bawah a,.pil{position:relative;overflow:hidden}
.riak{position:absolute;border-radius:50%;background:currentColor;opacity:.25;pointer-events:none;animation:riak .6s ease-out forwards}
@keyframes riak{from{transform:scale(0)}to{transform:scale(2.5);opacity:0}}
.btn.proses{pointer-events:none;opacity:.8}
.btn.proses::before{content:"";display:inline-block;width:12px;height:12px;margin-right:8px;border:2px solid currentColor;border-top-color:transparent;border-radius:50%;animation:putar .7s linear infinite;vertical-align:-1px}
@keyframes putar{to{transform:rotate(360deg)}}
.flash{animation:masuk .45s ease}@keyframes masuk{from{opacity:0;transform:translateY(-14px)}}
/* Scroll craft */
.kemajuan{position:fixed;top:0;left:0;height:4px;width:100%;background:linear-gradient(90deg,var(--g),#dfa93d);transform-origin:left;transform:scaleX(0);z-index:60}
/* Skeleton screen saat pindah halaman */
.kerangka{display:none}.sedang-pindah main>:not(.kerangka){display:none!important}
.sedang-pindah .kerangka{display:block}
.tulang{position:relative;overflow:hidden;background:#e6e3d6;border-radius:16px;margin-bottom:16px}
.tulang::after{content:"";position:absolute;inset:0;background:linear-gradient(100deg,transparent 30%,rgba(255,255,255,.75) 50%,transparent 70%);transform:translateX(-100%);animation:shimmer 1.3s infinite}
.kerangka .baris{display:grid;grid-template-columns:1.6fr 1fr;gap:20px}
/* Floating pill */
.pil-wadah{position:fixed;right:22px;bottom:22px;z-index:40;display:flex;align-items:center;gap:4px;padding:6px;border-radius:99px;background:rgba(31,45,38,.94);box-shadow:0 12px 30px rgba(31,45,38,.3);animation:naik .5s ease}
@keyframes naik{from{opacity:0;transform:translateY(30px)}}
.pil{display:inline-flex;align-items:center;gap:6px;border:0;border-radius:99px;padding:10px 15px;background:transparent;color:#fff;font:700 13px 'Segoe UI',system-ui,sans-serif;text-decoration:none;cursor:pointer}
.pil:hover{background:rgba(255,255,255,.14);color:#fff}.pil.gawat{background:#b3261e}
.pil kbd{font-size:11px;border:1px solid rgba(255,255,255,.4);border-radius:5px;padding:1px 5px}
main{padding-bottom:100px}
/* Bottom tab bar (layar HP) */
.tab-bawah{display:none}
/* Command palette */
dialog.palet{width:min(540px,92vw);margin-top:12vh;padding:0;border:1px solid var(--line);border-radius:20px;background:#fff;color:var(--dark);box-shadow:0 30px 70px rgba(31,45,38,.35)}
dialog.palet[open]{animation:muncul .2s ease}@keyframes muncul{from{opacity:0;transform:scale(.95) translateY(-10px)}}
dialog.palet::backdrop{background:rgba(31,45,38,.45);backdrop-filter:blur(4px)}
.palet input{border:0;border-bottom:1px solid var(--line);border-radius:0;margin:0;padding:18px 20px;background:#fff;box-shadow:none!important}
.palet ul{list-style:none;margin:0;padding:8px;max-height:48vh;overflow:auto}
.palet li{padding:12px 14px;border-radius:12px;cursor:pointer}.palet li[aria-selected=true]{background:var(--lg);font-weight:700}
.palet p{margin:0;padding:10px 20px 14px;font-size:12px;color:var(--gray)}
@media(max-width:850px){
.app{display:block}aside{padding:14px 16px}aside a.m{display:none}aside .logo{margin-bottom:8px}aside .bawah{flex-direction:row;margin:0}aside .bawah a{flex:1;font-size:12px}
main{padding:22px 16px 170px}.kerangka .baris{grid-template-columns:1fr}
.tab-bawah{display:grid;grid-template-columns:repeat(5,1fr);position:fixed;left:0;right:0;bottom:0;z-index:45;background:rgba(255,255,255,.96);border-top:1px solid var(--line);padding:6px 4px calc(6px + env(safe-area-inset-bottom))}
.tab-bawah a{display:flex;flex-direction:column;align-items:center;gap:2px;padding:6px 0;border-radius:14px;font-size:11px;color:var(--gray);text-decoration:none}
.tab-bawah a b{font-size:20px;transition:transform .25s}.tab-bawah a.on{background:var(--lg);color:var(--g);font-weight:700}.tab-bawah a.on b{transform:translateY(-3px) scale(1.12)}
.pil-wadah{right:50%;transform:translateX(50%);bottom:calc(80px + env(safe-area-inset-bottom));white-space:nowrap;animation:none}.pil kbd{display:none}
}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important}.antigravitasi{display:none}}
"""

UI_HTML = """
<div class="kemajuan" id="kemajuan" aria-hidden="true"></div>
<div class="antigravitasi" aria-hidden="true">
{% for x, ukuran, durasi, tunda, isi in [(6,18,19,0,''),(17,24,24,-9,'🍃'),(29,34,28,-17,''),(41,14,17,-4,''),(53,22,26,-20,'🌿'),(64,44,32,-11,''),(76,16,20,-15,''),(86,26,23,-2,'🍃'),(94,28,29,-23,'')] %}<i style="--x:{{ x }}%;--s:{{ ukuran }}px;--d:{{ durasi }}s;--t:{{ tunda }}s">{{ isi }}</i>{% endfor %}</div>
<div class="pil-wadah">
<a class="pil {{ 'gawat' if jml_mendesak }}" href="{{ url_for('tugas', f='mendesak' if jml_mendesak else 'minggu') }}">{% if jml_mendesak %}⏰ {{ jml_mendesak }} mendesak{% else %}📝 {{ jml_aktif }} tugas aktif{% endif %}</a>
<button type="button" class="pil" id="buka_palet" aria-haspopup="dialog">🔍 Cari <kbd>Ctrl K</kbd></button></div>
<nav class="tab-bawah" aria-label="Menu bawah">
{% for ep, ikon, nama in [('beranda','🏠','Beranda'),('tugas','📝','Tugas'),('rencana','🗓️','Rencana'),('ruang_tenang','🌿','Tenang'),('profil','👤','Profil')] %}<a class="{{ 'on' if endpoint==ep }}" href="{{ url_for(ep) }}" {% if endpoint==ep %}aria-current="page"{% endif %}><b aria-hidden="true">{{ ikon }}</b>{{ nama }}</a>{% endfor %}</nav>
<dialog class="palet" id="palet" aria-label="Pencarian perintah">
<input id="palet_cari" placeholder="Mau ke mana? Ketik perintah..." autocomplete="off" role="combobox" aria-expanded="true" aria-controls="palet_daftar" aria-label="Cari perintah">
<ul id="palet_daftar" role="listbox">
{% for ep, nama in [('beranda','🏠 Beranda'),('tugas','📝 Tugas'),('braindump','🧠 Brain Dump: tulis tugas'),('pemecah','🧩 Pecah tugas jadi langkah kecil'),('rencana','⏱️ Rencana Harian dan Mode Fokus'),('ruang_tenang','🌿 Ruang Tenang: napas dan musik'),('profil','👤 Profil dan Refleksi'),('darurat','🆘 Butuh bantuan (Mode Darurat)'),('panduan','📖 Lihat panduan')] %}<li role="option" id="cmd_{{ ep }}" data-url="{{ url_for(ep) }}">{{ nama }}</li>{% endfor %}
<li role="option" id="cmd_gerak" data-aksi="gerak">🎈 Nyalakan atau matikan efek melayang</li>
<li role="option" id="cmd_keluar" data-url="{{ url_for('keluar') }}">🚪 Logout</li></ul>
<p id="palet_info" aria-live="polite">↑↓ pilih · Enter buka · Esc tutup</p></dialog>
<script>
(function(){
const html=document.documentElement,tenang=matchMedia('(prefers-reduced-motion: reduce)').matches;
try{if(localStorage.getItem('tanpaGerak')==='1')html.classList.add('tanpa-gerak')}catch(e){}

// Skeleton screen: muncul kalau halaman berikutnya agak lama dimuat
const main=document.querySelector('main');
main.insertAdjacentHTML('beforeend','<div class="kerangka" role="status" aria-label="Memuat halaman"><div class="tulang" style="height:36px;width:45%"></div><div class="tulang" style="height:18px;width:70%"></div><div class="baris"><div class="tulang" style="height:260px"></div><div class="tulang" style="height:180px"></div></div><div class="tulang" style="height:130px"></div></div>');
let jam;
function pindah(){clearTimeout(jam);jam=setTimeout(()=>{html.classList.add('sedang-pindah');scrollTo(0,0)},150)}
addEventListener('pageshow',()=>{clearTimeout(jam);html.classList.remove('sedang-pindah');document.querySelectorAll('.proses').forEach(b=>b.classList.remove('proses'))});
document.addEventListener('click',e=>{const a=e.target.closest('a[href]');
  if(!a||e.defaultPrevented||e.button||e.ctrlKey||e.metaKey||e.shiftKey||a.target||a.hasAttribute('download'))return;
  if(a.origin!==location.origin||a.getAttribute('href').startsWith('#'))return;pindah()});

// Visual feedback: riak saat ditekan, putaran saat menyimpan
document.addEventListener('pointerdown',e=>{const t=e.target.closest('.btn,.tab-bawah a,.pil');if(!t||tenang)return;
  const k=t.getBoundingClientRect(),d=Math.max(k.width,k.height),s=document.createElement('span');s.className='riak';
  s.style.cssText=`width:${d}px;height:${d}px;left:${e.clientX-k.left-d/2}px;top:${e.clientY-k.top-d/2}px`;t.appendChild(s);setTimeout(()=>s.remove(),650)});
document.addEventListener('submit',e=>{if(e.defaultPrevented)return;const b=e.submitter||e.target.querySelector('button.btn');if(b)b.classList.add('proses')});

// Scroll craft: garis kemajuan dan kartu yang muncul saat digulir
const garis=document.getElementById('kemajuan');
function ukur(){const m=html.scrollHeight-innerHeight;garis.style.transform=`scaleX(${m>0?Math.min(1,scrollY/m):0})`}
addEventListener('scroll',ukur,{passive:true});addEventListener('resize',ukur);ukur();
if(!tenang&&'IntersectionObserver' in window){
  const lihat=new IntersectionObserver(daftar=>{let n=0;daftar.forEach(d=>{if(!d.isIntersecting)return;lihat.unobserve(d.target);
    d.target.animate([{opacity:0,translate:'0 28px'},{opacity:1,translate:'0 0'}],{duration:550,delay:Math.min(n++,5)*70,easing:'cubic-bezier(.2,.7,.2,1)',fill:'backwards'})})},{threshold:.08});
  main.querySelectorAll('.card,.fitur,.profil').forEach(el=>lihat.observe(el));
}

// Command palette
const palet=document.getElementById('palet'),cari=document.getElementById('palet_cari'),semua=[...palet.querySelectorAll('li')];let tampil=semua,aktif=0;
function tandai(){semua.forEach(li=>li.setAttribute('aria-selected','false'));const li=tampil[aktif];
  if(li){li.setAttribute('aria-selected','true');cari.setAttribute('aria-activedescendant',li.id);li.scrollIntoView({block:'nearest'})}else cari.removeAttribute('aria-activedescendant')}
function saring(){const q=cari.value.trim().toLowerCase();semua.forEach(li=>li.hidden=!li.textContent.toLowerCase().includes(q));tampil=semua.filter(li=>!li.hidden);aktif=0;tandai();
  document.getElementById('palet_info').textContent=tampil.length?'↑↓ pilih · Enter buka · Esc tutup':'Tidak ada perintah yang cocok.'}
function buka(){if(palet.open){palet.close();return}cari.value='';saring();palet.showModal();cari.focus()}
function jalankan(li){if(!li)return;palet.close();
  if(li.dataset.aksi==='gerak'){const mati=html.classList.toggle('tanpa-gerak');try{localStorage.setItem('tanpaGerak',mati?'1':'0')}catch(e){}return}
  pindah();location.href=li.dataset.url}
document.getElementById('buka_palet').onclick=buka;
cari.addEventListener('input',saring);
cari.addEventListener('keydown',e=>{if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();if(tampil.length){aktif=(aktif+(e.key==='ArrowDown'?1:-1)+tampil.length)%tampil.length;tandai()}}
  else if(e.key==='Enter'){e.preventDefault();jalankan(tampil[aktif])}});
semua.forEach(li=>li.onclick=()=>jalankan(li));
palet.addEventListener('click',e=>{if(e.target===palet)palet.close()});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();buka()}});
})();
</script>
"""
CSS += UI_CSS

CSS += """
/* Animasi halaman masuk: dari kusut menjadi terurai */
.masuk-hidup .kiri{position:relative;overflow:hidden;isolation:isolate;background:linear-gradient(135deg,#e2ece2,#f3f1df,#dde9f3,#e2ece2);background-size:300% 300%;animation:langit 16s ease infinite}
@keyframes langit{50%{background-position:100% 100%}}
.masuk-hidup .kiri::before,.masuk-hidup .kiri::after{content:"";position:absolute;z-index:-1;border-radius:50%;filter:blur(2px);animation:gumpal 11s ease-in-out infinite alternate}
.masuk-hidup .kiri::before{width:280px;height:280px;right:-90px;top:-70px;background:rgba(247,222,140,.45)}
.masuk-hidup .kiri::after{width:220px;height:220px;left:-80px;bottom:60px;background:rgba(111,155,128,.22);animation-delay:-5s}
@keyframes gumpal{to{transform:translate(-30px,40px) scale(1.15)}}
.kata{display:inline-block;animation:kata .7s cubic-bezier(.2,.8,.2,1) both;animation-delay:calc(var(--n)*.14s)}
@keyframes kata{from{opacity:0;transform:translateY(28px) rotate(5deg);filter:blur(5px)}}
.kata em{position:relative;display:inline-block;font-style:normal;color:var(--g)}
.kata em svg{position:absolute;left:0;bottom:-8px;width:100%;height:16px;overflow:visible}
.kata em path{fill:none;stroke:#dfa93d;stroke-width:5;stroke-linecap:round;stroke-dasharray:1;animation:gores 1.1s ease 1.2s both}
@keyframes gores{from{stroke-dashoffset:1;opacity:0}5%{opacity:1}to{stroke-dashoffset:0}}
.urai{width:100%;max-width:440px;margin:6px 0 28px}
.benang{display:block;width:100%;height:auto;margin-bottom:14px;overflow:visible}.benang path{fill:none;stroke:var(--g);stroke-width:2.5;stroke-linecap:round;stroke-dasharray:1}
.benang .kusut{animation:kusut 10s ease-in-out infinite}.benang .lurus{stroke:#dfa93d;stroke-width:4;animation:lurus 10s ease-in-out infinite}
@keyframes kusut{0%{stroke-dashoffset:1}28%,86%{stroke-dashoffset:0;opacity:1}96%,100%{stroke-dashoffset:0;opacity:0}}
@keyframes lurus{0%,28%{stroke-dashoffset:1;opacity:0}30%{opacity:1}55%,86%{stroke-dashoffset:0;opacity:1}96%,100%{stroke-dashoffset:0;opacity:0}}
.kertas{display:flex;align-items:center;gap:12px;margin-bottom:10px;padding:11px 16px;border-radius:16px;background:rgba(255,255,255,.94);box-shadow:0 8px 20px rgba(47,93,74,.13);animation:rapi 10s cubic-bezier(.6,0,.2,1) infinite}
.kertas strong{display:block;font-size:14px}.kertas small{color:var(--gray)}
.kertas i{display:flex;align-items:center;justify-content:center;width:24px;height:24px;flex-shrink:0;border:2px solid var(--g);border-radius:50%;font-style:normal}
.kertas i b{display:flex;align-items:center;justify-content:center;width:100%;height:100%;border-radius:50%;background:var(--g);color:#fff;font-size:13px}
.k1{--x:34px;--y:12px;--p:-7deg}.k2{--x:-18px;--y:-3px;--p:6deg}.k3{--x:52px;--y:-10px;--p:-4deg}
@keyframes rapi{0%,18%{transform:translate(var(--x),var(--y)) rotate(var(--p))}38%,86%{transform:none}100%{transform:translate(var(--x),var(--y)) rotate(var(--p))}}
.k1 i b{animation:centang1 10s ease infinite}.k2 i b{animation:centang2 10s ease infinite}.k3 i b{animation:centang3 10s ease infinite}
@keyframes centang1{0%,44%{transform:scale(0)}48%{transform:scale(1.3)}51%,86%{transform:scale(1)}94%,100%{transform:scale(0)}}
@keyframes centang2{0%,54%{transform:scale(0)}58%{transform:scale(1.3)}61%,86%{transform:scale(1)}94%,100%{transform:scale(0)}}
@keyframes centang3{0%,64%{transform:scale(0)}68%{transform:scale(1.3)}71%,86%{transform:scale(1)}94%,100%{transform:scale(0)}}
.masuk-hidup ul li{animation:geser .6s ease both}.masuk-hidup ul li:nth-child(1){animation-delay:.8s}.masuk-hidup ul li:nth-child(2){animation-delay:1s}.masuk-hidup ul li:nth-child(3){animation-delay:1.2s}
@keyframes geser{from{opacity:0;transform:translateX(-22px)}}
.masuk-hidup .kanan>div{animation:naik-form .8s ease .25s both}@keyframes naik-form{from{opacity:0;transform:translateY(24px)}}
@media(prefers-reduced-motion:reduce){.masuk-hidup *,.masuk-hidup *::before,.masuk-hidup *::after{animation:none!important}.masuk-hidup .kiri{animation:none}}
@media(max-width:850px){.masuk-hidup .kiri{padding:28px 22px}.masuk-hidup h1.big{font-size:34px;margin-top:20px}.k1{--x:12px}.k2{--x:-8px}.k3{--x:16px}}
"""

CSS += """
/* Parallax: --dalam makin besar, lapisan terasa makin dekat */
.masuk-hidup .kiri{--mx:0;--my:0}
.masuk-hidup .logo,.masuk-hidup h1.big,.masuk-hidup .sub,.masuk-hidup ul,.masuk-hidup .benang,.masuk-hidup .kertas,.masuk-hidup .daun{translate:calc(var(--mx)*var(--dalam)*1px) calc(var(--my)*var(--dalam)*1px)}
.masuk-hidup .logo{--dalam:4}.masuk-hidup h1.big{--dalam:9}.masuk-hidup .sub{--dalam:6}.masuk-hidup ul{--dalam:8}
.masuk-hidup .benang{--dalam:20}.masuk-hidup .k1{--dalam:11}.masuk-hidup .k2{--dalam:17}.masuk-hidup .k3{--dalam:24}
.masuk-hidup .kiri::before{translate:calc(var(--mx)*-45px) calc(var(--my)*-45px)}
.masuk-hidup .kiri::after{translate:calc(var(--mx)*55px) calc(var(--my)*55px)}
.daun{position:absolute;z-index:-1;opacity:.55;pointer-events:none;line-height:1}
@media(max-width:850px){.masuk-hidup .logo,.masuk-hidup h1.big,.masuk-hidup .sub,.masuk-hidup ul{translate:none}}
@media(prefers-reduced-motion:reduce){.masuk-hidup *,.masuk-hidup .kiri::before,.masuk-hidup .kiri::after{translate:none!important}}
"""

# Parallax halaman masuk: tiap lapisan bergerak dengan jarak berbeda
PARALAKS_JS = """
<script>
(function(){
const panel=document.querySelector('.masuk-hidup .kiri');
if(!panel||matchMedia('(prefers-reduced-motion: reduce)').matches)return;
let tx=0,ty=0,x=0,y=0,jalan=false;
function gambar(){
  x+=(tx-x)*0.08;y+=(ty-y)*0.08; // gerak halus
  panel.style.setProperty('--mx',x.toFixed(4));panel.style.setProperty('--my',y.toFixed(4));
  if(Math.abs(tx-x)>0.001||Math.abs(ty-y)>0.001)requestAnimationFrame(gambar);else jalan=false;
}
function ke(a,b){tx=Math.max(-1,Math.min(1,a));ty=Math.max(-1,Math.min(1,b));if(!jalan){jalan=true;requestAnimationFrame(gambar)}}
// Laptop: mengikuti kursor
addEventListener('pointermove',e=>{if(e.pointerType==='mouse')ke(e.clientX/innerWidth*2-1,e.clientY/innerHeight*2-1)});
document.documentElement.addEventListener('mouseleave',()=>ke(0,0));
// HP: mengikuti guliran layar
addEventListener('scroll',()=>{if(matchMedia('(hover: none),(max-width: 850px)').matches)ke(0,scrollY/(panel.offsetHeight||1)*2)},{passive:true});
})();
</script>"""

PAGES = {}

# Suara pengingat: memutar static/pengingat.mp3; kalau file tidak ada, bunyi "ting-ting"
SUARA_JS = """
<script>
(function(){
const AC=window.AudioContext||window.webkitAudioContext;
const info=document.getElementById('info_suara');
if(!AC){info.textContent='Browser ini tidak mendukung suara.';return}
const ctx=new AC();let nada=[],sudah=false,dimatikan=false;
// File suara sendiri (kalau ada)
const suaraSendiri=fetch('/static/pengingat.mp3')
  .then(r=>r.ok?r.arrayBuffer():Promise.reject())
  .then(a=>ctx.decodeAudioData(a)).catch(()=>null);
let urutan=0;
async function bunyi(){
  hentikan();sudah=true;info.textContent='';
  const ini=urutan,buf=await suaraSendiri;
  if(ini!==urutan)return; // dimatikan atau diputar ulang saat file dimuat
  if(buf){const o=ctx.createBufferSource();o.buffer=buf;o.connect(ctx.destination);o.start();nada.push(o);return}
  // 3 kali "ting-ting"
  for(let i=0;i<3;i++){[880,1175].forEach((f,j)=>{
    const o=ctx.createOscillator(),v=ctx.createGain(),t=ctx.currentTime+i*0.9+j*0.22;
    o.frequency.value=f;v.gain.setValueAtTime(0.0001,t);
    v.gain.exponentialRampToValueAtTime(0.4,t+0.02);v.gain.exponentialRampToValueAtTime(0.0001,t+0.2);
    o.connect(v);v.connect(ctx.destination);o.start(t);o.stop(t+0.22);nada.push(o)})}
}
function hentikan(){urutan++;nada.forEach(o=>{try{o.stop()}catch(e){}});nada=[]}
function otomatis(){if(!sudah&&!dimatikan&&ctx.state==='running')bunyi()}
document.getElementById('putar_suara').onclick=()=>{dimatikan=false;ctx.resume().then(bunyi)};
document.getElementById('stop_suara').onclick=()=>{dimatikan=true;hentikan();info.textContent='Suara dimatikan.'};
// Coba bunyi saat halaman dibuka
ctx.onstatechange=otomatis;otomatis();
// Kalau browser memblokir, bunyi pada klik atau tombol pertama
function pertama(e){if(e.target.closest&&e.target.closest('#stop_suara,#putar_suara,a'))return;
  if(!sudah&&!dimatikan)ctx.resume().then(otomatis)}
document.addEventListener('click',pertama);document.addEventListener('keydown',pertama);
setTimeout(()=>{if(!sudah&&!dimatikan)info.textContent='Browser menahan suara otomatis. Klik di mana saja pada halaman untuk membunyikan pengingat.'},600);
})();
</script>"""

PAGES["base.html"] = """<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TenangTugas</title><style>{{ css|safe }}</style></head>
<body>{% block body %}<div class="app"><aside><div class="logo">TenangTugas</div>
{% for ep, nama in [('beranda','Beranda'),('tugas','Tugas'),('rencana','Rencana Harian'),('ruang_tenang','Ruang Tenang'),('profil','Profil & Refleksi')] %}
<a class="m {{ 'on' if endpoint==ep }}" href="{{ url_for(ep) }}">{{ nama }}</a>{% endfor %}
<div class="bawah"><a class="kecil" href="{{ url_for('panduan') }}">Lihat panduan lagi</a>
<a class="kecil kuning" href="{{ url_for('darurat') }}">Butuh Bantuan?</a>
<a class="kecil" href="{{ url_for('keluar') }}" style="background:#ffe0e0;color:#8b1e1e;font-weight:700">Logout</a></div></aside>
<main>{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}{% block content %}{% endblock %}</main></div>{% endblock %}</body></html>"""

PAGES["daftar.html"] = """{% extends 'base.html' %}{% block body %}<div class="auth masuk-hidup"><div class="kiri">
{% for x, y, ukuran, dalam, isi in [(78,26,34,60,'🍃'),(88,62,22,34,'🌿'),(63,8,16,22,'🍃'),(8,91,26,48,'🌿'),(93,84,14,18,'🍃')] %}<span class="daun" aria-hidden="true" style="left:{{ x }}%;top:{{ y }}%;font-size:{{ ukuran }}px;--dalam:{{ dalam }}">{{ isi }}</span>{% endfor %}<div class="logo">TenangTugas</div>
<h1 class="big" aria-label="Tugas banyak? Mari kita urai pelan-pelan.">{% for k in ['Tugas','banyak?','Mari','kita','urai'] %}<span class="kata" aria-hidden="true" style="--n:{{ loop.index0 }}">{{ k }}</span> {% endfor %}<span class="kata" aria-hidden="true" style="--n:5"><em>pelan-pelan.<svg viewBox="0 0 300 18" preserveAspectRatio="none"><path pathLength="1" d="M4 12 C60 2 110 17 165 9 S255 5 296 10"/></svg></em></span></h1>
<p class="sub">TenangTugas membantu mahasiswa mengelola tugas yang menumpuk sekaligus menurunkan stres akademik.</p>
<ul><li>🟢 Memecah tugas jadi langkah kecil</li><li>🔵 Menentukan prioritas dengan tenang</li><li>🟡 Ada jeda relaksasi untuk menenangkan diri</li></ul>
<div class="urai" aria-hidden="true">
<svg class="benang" viewBox="0 0 440 70"><path class="kusut" pathLength="1" d="M8 38 C60 -8 112 78 82 50 C45 12 150 2 128 46 C108 82 72 14 160 20 C240 26 160 74 142 44 C128 16 215 6 205 46 C200 66 235 40 262 36"/><path class="lurus" pathLength="1" d="M262 36 C310 30 350 38 432 36"/></svg>
{% for nama, ket in [('Tulis semua tugas','Keluarkan dulu dari kepala'),('Pilih satu dulu','Sisanya boleh menunggu'),('Kerjakan pelan-pelan','Satu langkah kecil sudah cukup')] %}<div class="kertas k{{ loop.index }}"><i><b>✓</b></i><span><strong>{{ nama }}</strong><small>{{ ket }}</small></span></div>{% endfor %}
</div>
<div class="catatan">Web ini dapat membantu manajemen stres, tetapi bukan pengganti layanan kesehatan mental.</div></div>
<div class="kanan"><div style="width:100%;max-width:404px">
<div class="tabs"><a class="{{ 'on' if tab=='daftar' }}" href="{{ url_for('daftar') }}">Daftar</a><a class="{{ 'on' if tab=='masuk' }}" href="{{ url_for('daftar', tab='masuk') }}">Masuk</a></div>
{% for m in get_flashed_messages() %}<div class="flash">{{ m }}</div>{% endfor %}
{% if tab=='masuk' %}<h1>Masuk</h1><form method="post"><input type="hidden" name="aksi" value="masuk">
<label>Email</label><input name="email" type="email" placeholder="nama@email.com"><label>Kata sandi</label><div style="display:flex;gap:6px"><input name="sandi" type="password" id="sandi_masuk" style="flex:1"><button type="button" class="btn o" style="padding:12px 14px;width:auto;margin:4px 0 12px" onclick="let i=document.getElementById('sandi_masuk');i.type=i.type=='password'?'text':'password'">👀</button></div>
<button class="btn full">Masuk</button></form>
{% else %}<h1>Buat akunmu</h1><p class="sub">Hanya butuh satu menit. Setelah itu kami kenalkan cara memakai TenangTugas.</p>
<form method="post"><input type="hidden" name="aksi" value="daftar"><label>Nama panggilan</label><input name="nama">
<label>Email</label><input name="email" type="email" placeholder="nama@email.com"><label>Kata sandi</label><div style="display:flex;gap:6px"><input name="sandi" type="password" id="sandi_daftar" placeholder="Minimal 8 karakter" style="flex:1"><button type="button" class="btn o" style="padding:12px 14px;width:auto;margin:4px 0 12px" onclick="let i=document.getElementById('sandi_daftar');i.type=i.type=='password'?'text':'password'">👀</button></div>
<button class="btn full">Buat akun</button></form>{% endif %}
<p class="meta" style="text-align:center;margin-top:18px">Catatan stres harianmu bersifat pribadi.</p></div></div></div>""" + PARALAKS_JS + """{% endblock %}"""

PAGES["panduan.html"] = """{% extends 'base.html' %}{% block body %}<div class="top"><div class="logo" style="margin:0">TenangTugas</div>
<div class="meta">Panduan singkat · sekitar 1 menit &nbsp; <a href="{{ url_for('beranda') }}"><b>Lewati</b></a></div></div>
<div class="wrap"><div class="grid2"><div><h1 style="font-size:38px">Selamat datang, {{ user.nama }}. Kenalan sebentar dengan TenangTugas.</h1>
<p class="sub">Kamu bisa melewati panduan ini kapan saja. Isinya hanya cara kerja singkat supaya kamu tahu harus mulai dari mana.</p></div>
<div class="card blue"><h2 style="color:var(--navy)">Apa itu TenangTugas?</h2><p>Web untuk membantu mahasiswa mengelola tugas yang menumpuk sekaligus menurunkan stres akademik. Fokusnya bukan sekadar daftar tugas, tetapi memecah beban kerja, memilih prioritas, dan menyediakan dukungan emosional.</p></div></div>
<h2>Cara memakainya dalam 4 langkah</h2><div class="grid4">
<div class="card"><h3>1. Tulis semuanya</h3><p class="meta">Buka Brain Dump dan tuangkan semua tugas. Belum perlu rapi atau berurutan.</p></div>
<div class="card"><h3>2. Pecah jadi langkah kecil</h3><p class="meta">Pilih satu tugas yang paling mengganggu, lalu tekan "Pecah jadi langkah kecil".</p></div>
<div class="card"><h3>3. Mulai Mode Fokus</h3><p class="meta">Kerja 25 menit, istirahat 5 menit, lalu lihat progresmu bertambah.</p></div>
<div class="card"><h3>4. Cek stres dan tenangkan diri</h3><p class="meta">Isi cek stres harian, dan buka Ruang Tenang kapan pun kamu butuh jeda.</p></div></div>
<div class="grid2" style="grid-template-columns:1fr 1fr"><div class="card yel"><h2>Kalau terasa terlalu berat</h2>
<p>Tekan "Butuh Bantuan?" di menu kiri kapan saja. Kamu akan melihat 3 langkah terkecil, latihan napas 1 menit, dan tautan layanan konseling kampus.</p></div>
<div class="card"><h2>Yang akan kamu temukan di Beranda</h2><ul><li><b>Menu di sisi kiri:</b> Tugas, Rencana Harian, Ruang Tenang, Profil dan Refleksi.</li>
<li><b>Fokus 3 tugas hari ini:</b> hanya prioritas hari ini yang ditampilkan.</li><li><b>Semua fitur:</b> daftar lengkap fitur yang bisa kamu buka kapan pun.</li></ul></div></div>
<a class="btn" href="{{ url_for('beranda') }}">Mengerti, ke Beranda</a>
<p class="meta">Web ini bukan pengganti layanan kesehatan mental. Jika stres terasa berat atau terus-menerus, hubungi konseling kampus atau layanan kesehatan setempat.</p></div>{% endblock %}"""

PAGES["beranda.html"] = """{% extends 'base.html' %}{% block content %} {% if mendesak %}<div class="card" style="background:#ffe0e0;border:2px solid red" role="alert"><h2>⚠️ Pengingat! Ada {{ mendesak|length }} tugas mendesak!</h2>
<ul>{% for t in mendesak %}<li><b>{{ t.judul }}</b> - {{ fmt_tenggat(t) }} ({{ 'sudah lewat tenggat' if terlambat(t) else ('hari ini' if sisa_hari(t)==0 else 'besok') }})</li>{% endfor %}</ul>
<p>Buruan cek, jangan sampe kelewat. Pecah jadi langkah kecil dulu biar nggak panik.</p><a class="btn" href="{{ url_for('tugas', f='mendesak') }}">Lihat Tugas Mendesak</a>
<button type="button" class="btn o" id="putar_suara">🔔 Bunyikan</button> <button type="button" class="btn o" id="stop_suara">🔕 Matikan suara</button>
<p class="meta" id="info_suara" aria-live="polite"></p></div>""" + SUARA_JS + """{% endif %}<h1>Halo, {{ user.nama }}</h1>
<p class="sub">Ini Beranda-mu. Mulai dari yang paling kecil, satu langkah saja sudah cukup.</p>
<div class="grid2"><div>
{% if not ada_tugas %}<div class="card hero"><h2>Belum ada tugas. Mulai dari Brain Dump.</h2><p>Tuliskan semua tugasmu tanpa harus langsung mengatur. Nanti kita susun dan pecah bersama.</p>
<a class="btn y" href="{{ url_for('braindump') }}">Tulis tugas pertamaku</a></div>{% endif %}
<div class="card"><h2>Bagaimana perasaanmu hari ini?</h2><form method="post" action="{{ url_for('cek_stres') }}"><div class="moods">
{% for m in MOODS %}<label><input type="radio" name="mood" value="{{ m }}" required ><span><b>{{ MOOD_EMOJI[loop.index0] }}</b>{{ m }}</span></label>{% endfor %}</div>
{% if pesan %}<p><span class="pill">{{ pesan }}</span></p>{% endif %}
<label>Apa yang paling membuatmu cemas hari ini?</label><input name="catatan" placeholder="Boleh singkat, boleh dikosongkan">
<button class="btn">Simpan</button></form>
{% if riwayat %}<h3 style="margin-top:16px">Riwayat Perasaanmu</h3>{% for tgl, isi in riwayat %}<p style="margin:6px 0"><b>{{ fmt_tgl(tgl) }}</b> - {{ MOOD_EMOJI[isi.skor-1] }} {{ MOODS[isi.skor-1] }}{% if isi.catatan %} - <span class="meta">{{ isi.catatan }}</span>{% endif %}</p>{% endfor %}{% endif %}</div>
<div class="card dash"><h2>Fokus 3 tugas hari ini</h2>{% if fokus %}{% for t in fokus %}<div class="row" style="margin-bottom:8px"><span><b>{{ t.judul }}</b> <span class="meta">· {{ NAMA_KUADRAN[kuadran(t)] }}</span></span>
<a class="btn o" href="{{ url_for('pemecah', id=t.id) }}">Pecah</a></div>{% endfor %}
{% else %}<p class="meta">Tiga tugas prioritasmu akan muncul di sini setelah kamu menulis tugas. Hanya yang penting hari ini yang ditampilkan, supaya layar tetap lega.</p>{% endif %}</div></div>
<div><div class="card yel"><h2>Mulai di sini</h2>{% for teks, ok in mulai %}<p style="margin:6px 0">{{ '✅' if ok else '⭕' }} {{ teks }}</p>{% endfor %}</div>
<div class="card"><h2>Tenggat terdekat</h2>{% for t in terdekat %}<p style="margin:6px 0"><b>{{ t.judul }}</b><br><span class="meta">{{ fmt_tenggat(t) }}</span></p>
{% else %}<p class="meta">Belum ada tenggat. Tambahkan saat kamu mengatur tugas.</p>{% endfor %}</div>
<div class="card blue"><h2 style="color:var(--navy)">Ruang Tenang</h2><p>Latihan napas 4-7-8 atau napas kotak selama 1 sampai 3 menit, kapan pun kamu butuh jeda.</p>
<a class="btn o" href="{{ url_for('ruang_tenang') }}" style="color:var(--navy);border-color:var(--navy)">Coba napas 1 menit</a></div></div></div>
<h2 style="font-size:22px">Semua fitur TenangTugas</h2><p class="sub">Buka fitur mana pun kapan saja. Tidak perlu berurutan.</p>
<div class="grid3">{% for judul, desk, ep, warna in FITUR %}<a class="fitur {{ warna }}" href="{{ url_for(ep) }}"><h3>{{ judul }}</h3><p>{{ desk }}</p></a>{% endfor %}</div>{% endblock %}"""

PAGES["braindump.html"] = """{% extends 'base.html' %}{% block content %}<h1>Tuliskan semua yang ada di kepalamu</h1>
<p class="sub">Tidak perlu rapi, tidak perlu berurutan. Satu baris untuk satu tugas. Mengatur dan memilih bisa nanti.</p>
<div class="grid2"><form method="post" class="card"><textarea name="teks" style="min-height:260px" placeholder="Laporan Basis Data&#10;Presentasi Psikologi&#10;Balas pesan kelompok"></textarea>
<p class="meta">Tekan Enter untuk baris baru</p><button class="btn">Lanjut: atur tenggat</button> &nbsp; <a href="{{ url_for('beranda') }}">Kembali</a></form>
<div><div class="card blue"><h2 style="color:var(--navy)">Kenapa mulai dari sini?</h2><p>Menuliskan semua tugas tanpa langsung mengatur membantu mengurangi beban mental dan kecemasan di awal. Pikiranmu tidak perlu lagi menyimpan semuanya sendirian.</p></div>
<div class="card lyel"><h2>Boleh berantakan</h2><p>Tugas kuliah, urusan organisasi, atau hal kecil yang mengganjal, semuanya boleh masuk. Kamu bisa menghapus atau menggabungkan nanti.</p></div>
<p class="meta"><i>Kamu tidak perlu menyelesaikan semuanya hari ini; cukup langkah berikutnya.</i></p></div></div>{% endblock %}"""

PAGES["atur_tenggat.html"] = """{% extends 'base.html' %}{% block content %}<h1>Atur tenggat</h1><p class="sub">Isi seperlunya. Tingkat kesulitan dipakai untuk memecah tugas menjadi langkah kecil.</p>
<form method="post">{% for b in baris %}{% set i = loop.index0 %}<div class="card"><h3>{{ b }}</h3><div class="grid4">
<div><label>Mata kuliah</label><input name="matkul{{ i }}"></div><div><label>Tenggat</label><input type="date" name="tgl{{ i }}" value="{{ default }}"></div>
<div><label>Jam tenggat</label><input type="time" name="jam{{ i }}" value="23:00"></div>
<div><label>Tingkat kesulitan</label><select name="sulit{{ i }}">{% for k in KESULITAN %}<option {{ 'selected' if k=='Sedang' }}>{{ k }}</option>{% endfor %}</select></div></div></div>{% endfor %}
<button class="btn">Simpan ke daftar tugas</button></form>{% endblock %}"""

PAGES["tugas.html"] = """{% extends 'base.html' %}{% block content %}<h1>Tugas</h1><div class="tabs">
{% for k, n in [('mendesak','Mendesak'),('hari','Hari ini'),('minggu','Minggu ini'),('selesai','Selesai'),('terlambat','Terlambat')] %}<a class="{{ 'on' if f==k }}" href="{{ url_for('tugas', f=k) }}">{{ n }}</a>{% endfor %}</div>
<div class="grid2"><div>{% for t in hasil %}<div class="card tugas q{{ kuadran(t) }}"><div class="row"><div><h2 style="margin:0">{{ t.judul }}</h2>
<div class="meta">{{ t.matkul }} · {{ fmt_tenggat(t) }}</div></div>
<span class="pill {{ 's' if t.kesulitan=='Sulit' }}">{{ t.kesulitan }}</span></div>
{% if t.langkah %}<p class="meta" style="margin:14px 0 4px"><b>Langkah kecil yang disarankan</b></p>{% for l in t.langkah %}
<form method="post" action="{{ url_for('langkah_centang', tid=t.id, i=loop.index0) }}"><label style="display:flex;gap:10px;align-items:center;font-weight:400;color:var(--dark)">
<input type="checkbox" style="width:auto;margin:6px 0" onchange="this.form.submit()" {{ 'checked' if l.done }}>{{ l.t }} <span class="meta">({{ l.m }} mnt)</span></label></form>{% endfor %}{% endif %}
<div class="row" style="margin-top:12px"><form method="post" action="{{ url_for('tugas_selesai', tid=t.id) }}"><label style="font-weight:400"><input type="checkbox" style="width:auto" onchange="this.form.submit()" {{ 'checked' if t.selesai }}> Tandai cukup baik</label></form>
<a class="btn o" href="{{ url_for('pemecah', id=t.id) }}">Pecah jadi langkah kecil</a></div></div>
{% else %}<div class="card"><p class="meta">Belum ada tugas di bagian ini. Tidak apa-apa, ambil napas dulu.</p><a class="btn o" href="{{ url_for('braindump') }}">Tulis di Brain Dump</a></div>{% endfor %}</div>
<form method="post" action="{{ url_for('tugas_tambah') }}" class="card"><h2>Tambah tugas</h2><label>Judul</label><input name="judul" required><label>Mata kuliah</label><input name="matkul">
<div class="grid2" style="grid-template-columns:1fr 1fr;gap:10px"><div><label>Tenggat</label><input type="date" name="tgl" value="{{ hari_ini }}"></div>
<div><label>Jam tenggat</label><input type="time" name="jam" value="23:00"></div></div>
<label>Tingkat kesulitan</label><select name="sulit">{% for k in KESULITAN %}<option {{ 'selected' if k=='Sedang' }}>{{ k }}</option>{% endfor %}</select><button class="btn full">Tambah tugas</button></form></div>{% endblock %}"""

PAGES["pemecah.html"] = """{% extends 'base.html' %}{% block content %}<h1>Mulai dari satu tugas yang paling mengganggu</h1><p class="sub">Pilih satu saja dulu. Sisanya menunggu dengan tenang di daftar.</p>
{% if not pilih %}<div class="card"><p>Belum ada tugas untuk dipecah.</p><a class="btn" href="{{ url_for('braindump') }}">Tulis di Brain Dump</a></div>{% else %}
<div class="grid2" style="grid-template-columns:1fr 1.1fr"><div>{% for t in daftar %}<a href="{{ url_for('pemecah', id=t.id) }}" class="card" style="display:block;text-decoration:none;color:inherit;padding:16px;margin-bottom:10px;{{ 'background:var(--lg);border:2px solid var(--g)' if t.id==pilih.id }}">{{ '🔘' if t.id==pilih.id else '⚪' }} {{ t.judul }}</a>{% endfor %}
<form method="post" class="card"><input type="hidden" name="aksi" value="pecah"><h2>Detail {{ pilih.judul }}</h2><div class="grid2" style="grid-template-columns:1fr 1fr;gap:10px">
<div><label>Mata kuliah</label><input name="matkul" value="{{ pilih.matkul }}"></div><div><label>Tenggat</label><input type="date" name="tgl" value="{{ pilih.deadline.isoformat() if pilih.deadline }}"></div>
<div><label>Jam tenggat</label><input type="time" name="jam" value="{{ jam_tugas(pilih) }}"></div>
<div><label>Tingkat kesulitan</label><select name="sulit">{% for k in KESULITAN %}<option {{ 'selected' if k==pilih.kesulitan }}>{{ k }}</option>{% endfor %}</select></div></div>
<button class="btn full">Pecah jadi langkah kecil</button></form></div>
<form method="post" class="card"><input type="hidden" name="aksi" value="simpan"><div class="row"><h2>Saran langkah kecil</h2><span class="pill s">{{ pilih.langkah|length }} langkah</span></div>
<p class="meta">Ubah, hapus, atau tambah langkah sesukamu. Ini hanya titik awal.</p>
{% for l in pilih.langkah %}<div class="row"><input name="t{{ loop.index0 }}" value="{{ l.t }}"><input name="m{{ loop.index0 }}" type="number" min="5" step="5" value="{{ l.m }}" style="width:90px">
<label style="white-space:nowrap;font-weight:400"><input type="checkbox" name="hapus{{ loop.index0 }}" style="width:auto"> hapus</label></div>{% endfor %}
{% set n = pilih.langkah|length %}<div class="row"><input name="t{{ n }}" placeholder="Tambah langkah baru"><input name="m{{ n }}" type="number" min="5" step="5" value="25" style="width:90px"></div>
<label style="font-weight:400"><input type="checkbox" name="cukup_baik" style="width:auto" {{ 'checked' if pilih.selesai }}> Tandai tugas ini sebagai "cukup baik" kalau sudah selesai, tidak perlu sempurna.</label><br><br>
<button class="btn">Simpan ke rencanaku</button> &nbsp; <a href="{{ url_for('tugas') }}">Kembali</a></form></div>{% endif %}{% endblock %}"""

PAGES["rencana.html"] = """{% extends 'base.html' %}{% block content %}<h1>Rencana Harian</h1><p class="sub">Pilih 1 sampai 3 prioritas utama, lalu kerjakan per blok waktu.</p>
<div class="grid2" style="grid-template-columns:1.3fr 1fr"><div><div class="card"><div class="row"><h2>Blok waktu hari ini</h2>
<form method="get"><input type="time" name="mulai" value="{{ mulai }}" onchange="this.form.submit()" style="width:auto;margin:0"></form></div>
{% for b in blok %}<div class="blok"><div class="w">{{ b.waktu }}</div><div class="i {{ 'r' if b.istirahat }}"><b>{{ b.judul }}</b>{% if b.sub %}<div class="meta">{{ b.sub }}</div>{% endif %}</div></div>
{% else %}<p class="meta">Belum ada langkah untuk dijadwalkan. Pecah satu tugas jadi langkah kecil dulu.</p><a class="btn o" href="{{ url_for('pemecah') }}">Pecah tugas</a>{% endfor %}
<p class="meta">Jadwal yang jelas membantu waktu terpakai lebih efisien dan menambah rasa kendali.</p></div></div>
<div><div class="card"><h2>Mode Fokus</h2><div class="timer" id="waktu">25:00</div><p class="meta" id="mode" style="text-align:center">25 menit kerja, 5 menit istirahat</p>
<div class="row"><button class="btn" style="flex:1" onclick="mulai()">Mulai fokus</button><button class="btn o" style="flex:1" onclick="jeda()">Jeda</button></div>
<p class="meta" style="text-align:center">Total fokus: <b id="total">{{ fmt_menit(fokus_menit) }}</b></p></div>
<div class="card"><h2>Matriks Prioritas</h2><div class="grid2" style="grid-template-columns:1fr 1fr;gap:10px">
{% for i, warna in [(0,'#dce9de'),(1,'#dde9f3'),(2,'#fbefc5'),(3,'#f0ede4')] %}<div class="q" style="background:{{ warna }}"><b>{{ NAMA_KUADRAN[i] }}</b>{% for t in kuad[i] %}{{ t.judul }}<br>{% else %}<span class="meta">Belum ada</span>{% endfor %}</div>{% endfor %}</div>
<p class="meta">Mendesak: tenggat 3 hari atau kurang. Penting: tugas sedang atau sulit.</p></div></div></div>
<script>
let sisa=1500,kerja=true,tmr=null;const el=document.getElementById('waktu');
function tampil(){el.textContent=String(Math.floor(sisa/60)).padStart(2,'0')+':'+String(sisa%60).padStart(2,'0')}
function mulai(){if(tmr)return;tmr=setInterval(()=>{sisa--;tampil();if(sisa<=0){clearInterval(tmr);tmr=null;selesai()}},1000)}
function jeda(){clearInterval(tmr);tmr=null}
async function selesai(){if(kerja){const r=await fetch('{{ url_for("fokus_selesai") }}',{method:'POST'});const d=await r.json();
document.getElementById('total').textContent=Math.floor(d.total/60)+' j '+(d.total%60)+' m'}
kerja=!kerja;sisa=kerja?1500:300;document.getElementById('mode').textContent=kerja?'Waktunya kerja':'Istirahat 5 menit';tampil();alert(kerja?'Istirahat selesai.':'Sesi fokus selesai. Istirahat dulu.')}
</script>{% endblock %}"""

NAPAS_JS = """
<script>
let jalan=false;
function napas(tombol,detik){if(jalan)return;jalan=true;const b=document.getElementById('bulat'),t=document.getElementById('teks');
const fase=[['Tarik napas',4],['Tahan',7],['Hembuskan',8]];let f=0,s=fase[0][1],total=0;
function atur(){t.textContent=fase[f][0]+' '+s;b.style.transitionDuration=fase[f][1]+'s';b.style.transform=f==0?'scale(1.5)':f==2?'scale(1)':b.style.transform}
atur();const id=setInterval(()=>{s--;total++;if(total>=detik){clearInterval(id);jalan=false;t.textContent='Selesai';b.style.transform='scale(1)';return}
if(s<=0){f=(f+1)%3;s=fase[f][1]}atur()},1000)}
</script>"""

PAGES["ruang_tenang.html"] = """{% extends 'base.html' %}{% block content %}<h1>Ruang Tenang</h1>
<div class="card lyel"><b>Kamu tidak perlu menyelesaikan semuanya hari ini; cukup langkah berikutnya.</b></div>
<div class="grid2" style="grid-template-columns:1fr 1fr"><div class="card"><h2>Latihan napas</h2><p class="meta">Napas 4-7-8 selama 1 menit.</p>
<div class="napas" id="bulat"><span id="teks">Siap</span></div><button class="btn full" onclick="napas(this,60)">Mulai 1 menit</button></div>
<div class="card"><h2>Istirahat mata dan peregangan</h2><p class="meta">Timer singkat untuk menjauh dari layar dan meregangkan badan.</p>
<h1 id="mata" style="text-align:center">20</h1><button class="btn o" onclick="mata()">Mulai timer istirahat</button></div>
<form method="post" class="card"><h2>Jurnal cepat</h2><label>Apa satu hal kecil yang sudah kamu selesaikan?</label><textarea name="jurnal"></textarea><button class="btn">Simpan</button>
{% for j in jurnal %}<p class="meta">{{ fmt_tgl(j.tgl) }}: {{ j.teks }}</p>{% endfor %}</form>
<div class="card"><h2>Musik untuk tenang</h2>
<div class="skeleton memuat" id="skel_spotify"><iframe class="spotify-embed" onload="this.parentNode.classList.remove('memuat')" src="https://open.spotify.com/embed/playlist/{{ spotify_playlist_id }}?utm_source=generator&theme=0" allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture" loading="lazy" title="Playlist Spotify Ruang Tenang"></iframe></div>
<script>setTimeout(()=>document.getElementById('skel_spotify').classList.remove('memuat'),8000)</script>
<a class="btn o" href="https://open.spotify.com/playlist/{{ spotify_playlist_id }}" target="_blank" rel="noopener">Buka di Spotify</a>
<div class="tabs" style="margin-top:14px"><a href="#" onclick="suara('Hujan',1800,0.5);return false">Suara hujan</a><a href="#" onclick="suara('White noise',20000,0);return false">White noise</a></div>
<p class="meta" id="info">Pilih musik Spotify atau suara latar dari browser.</p></div></div>
""" + NAPAS_JS + """<script>
function mata(){let s=20;const e=document.getElementById('mata');const id=setInterval(()=>{s--;e.textContent=s;if(s<=0){clearInterval(id);e.textContent='Selesai, 20';}},1000)}
let ctx=null,sumber=null,aktif='';
function suara(nama,freq,hp){if(sumber){sumber.stop();sumber=null;document.getElementById('info').textContent='Suara dihentikan.';if(aktif==nama){aktif='';return}}
ctx=ctx||new AudioContext();const buf=ctx.createBuffer(1,ctx.sampleRate*2,ctx.sampleRate),d=buf.getChannelData(0);for(let i=0;i<d.length;i++)d[i]=Math.random()*2-1;
sumber=ctx.createBufferSource();sumber.buffer=buf;sumber.loop=true;const lp=ctx.createBiquadFilter();lp.type='lowpass';lp.frequency.value=freq;
const g=ctx.createGain();g.gain.value=0.25;sumber.connect(lp);lp.connect(g);g.connect(ctx.destination);sumber.start();aktif=nama;document.getElementById('info').textContent='Memutar: '+nama}
</script>{% endblock %}"""

PAGES["profil.html"] = """{% extends 'base.html' %}{% block content %}<h1>Profil & Refleksi</h1>
<div class="profil"><div class="sampul"><span>🍃 Akun Saya</span></div>
<div class="isi"><div class="avatar" aria-hidden="true">{{ user.nama[0]|upper }}</div>
<div class="identitas"><h2>{{ user.nama }}</h2><p>✉️ {{ user.email }}</p></div>
<a class="keluar" href="{{ url_for('keluar') }}">Logout →</a></div>
<div class="kutipan">🌱 Pelan-pelan saja, satu langkah kecil tetap berarti.</div>
<div class="idakun"><span>ID Akun</span><code id="id_akun">{{ user.id }}</code>
<button type="button" onclick="const t=this;navigator.clipboard.writeText(document.getElementById('id_akun').textContent).then(()=>{t.textContent='✓ Tersalin'},()=>{t.textContent='Gagal menyalin'})">Salin</button></div></div>
<p class="sub">Lihat pola stresmu minggu ini.{% if contoh %} Angka di bawah adalah contoh data sampai kamu mengisi cek stres harian.{% endif %}</p>
<div class="grid2"><div class="card"><h2>Riwayat stres mingguan</h2><div class="bar">{% set mx = data|map(attribute=1)|max %}
{% for h, s in data %}<div class="{{ 'max' if s==mx and s>0 }}" style="height:{{ s*20 }}%"></div>{% endfor %}</div>
<div class="lab">{% for h, s in data %}<span>{{ h }}</span>{% endfor %}</div><br>
<div class="stat"><div><b>{{ selesai }}</b>Tugas selesai</div><div class="b"><b>{{ fokus }}</b>Waktu fokus</div></div></div>
<div><div class="card"><h2>Pola yang terlihat</h2><p>{{ pola }}</p></div><div class="card lyel"><h2 style="color:var(--olive)">Saran untukmu</h2><p>{{ saran }}</p></div></div></div>{% endblock %}"""

PAGES["darurat.html"] = """{% extends 'base.html' %}{% block content %}<h1>Mode Darurat</h1><p class="sub">Pelan-pelan saja. Cukup lakukan satu hal kecil dulu.</p>
<div class="grid2" style="grid-template-columns:1fr 1fr"><div class="card yel"><h2>3 langkah terkecil</h2><ol>{% for l in langkah %}<li style="margin:8px 0">{{ l }}</li>{% endfor %}</ol></div>
<div class="card"><h2>Napas 1 menit</h2><div class="napas" id="bulat"><span id="teks">Siap</span></div><button class="btn full" onclick="napas(this,60)">Mulai 1 menit</button></div></div>
<div class="card blue"><h2 style="color:var(--navy)">Butuh bicara dengan seseorang?</h2><p>Jika stres terasa berat atau terus-menerus, hubungi konseling kampus atau layanan kesehatan setempat.</p>
<a class="btn" href="{{ KONSELING_URL }}" target="_blank" rel="noopener">Buka layanan konseling kampus</a></div>
<p class="meta">Web ini bukan pengganti layanan kesehatan mental.</p>""" + NAPAS_JS + """{% endblock %}"""

PAGES["base.html"] = PAGES["base.html"].replace("</main></div>{% endblock %}", "</main></div>" + UI_HTML + "{% endblock %}")
# ----------------------------------------------------------------------------
# Parallax untuk semua halaman setelah halaman Daftar/Masuk
# ----------------------------------------------------------------------------
CSS += """
/* --px/--py mengikuti kursor, --gulir mengikuti guliran. --jarak = kedalaman */
:root{--px:0;--py:0;--gulir:0}
main h1,.wrap h1{--jarak:7}main .sub,.wrap .sub{--jarak:4}.card,.fitur,.profil{--jarak:3}
.grid2>div:nth-child(2) .card,.grid3>.fitur:nth-child(3n+2),.grid4>.card:nth-child(even){--jarak:6}
main h1,.wrap h1,main .sub,.wrap .sub,.card,.fitur,.profil{translate:calc(var(--px)*var(--jarak)*1px) calc(var(--py)*var(--jarak)*1px)}
/* Latar: bergerak paling jauh, juga saat digulir */
.antigravitasi i,.hiasan i{translate:calc(var(--px)*var(--jarak)*1px) calc(var(--py)*var(--jarak)*1px - var(--gulir)*var(--laju)*1px)}
.antigravitasi i{--jarak:20;--laju:.06}.antigravitasi i:nth-child(3n+1){--jarak:38;--laju:.14}.antigravitasi i:nth-child(3n+2){--jarak:56;--laju:.24}
.hiasan{position:fixed;inset:0;overflow:hidden;pointer-events:none;z-index:0}.hiasan i{position:absolute;font-style:normal;opacity:.4}
.wrap,.top{position:relative;z-index:1}
.tanpa-gerak main *,.tanpa-gerak .antigravitasi i{translate:none!important}
@media(hover:none),(max-width:850px){main h1,.wrap h1,main .sub,.wrap .sub,.card,.fitur,.profil{translate:none}}
@media(prefers-reduced-motion:reduce){main h1,.wrap h1,main .sub,.wrap .sub,.card,.fitur,.profil,.antigravitasi i,.hiasan i{translate:none!important}}
"""

PARALAKS_UMUM = """
<script>
(function(){
if(document.querySelector('.masuk-hidup')||matchMedia('(prefers-reduced-motion: reduce)').matches)return;
const html=document.documentElement;
// Halaman tanpa lapisan latar (misalnya Panduan) diberi daun hiasan
if(!document.querySelector('.antigravitasi')){
  const h=document.createElement('div');h.className='hiasan';h.setAttribute('aria-hidden','true');
  [[4,20,30,50,.12],[93,14,22,28,.07],[88,72,36,60,.2],[8,80,18,34,.1],[52,92,24,44,.16]].forEach(([x,y,u,d,k],n)=>{
    const i=document.createElement('i');i.textContent=n%2?'🌿':'🍃';
    i.style.cssText=`left:${x}%;top:${y}%;font-size:${u}px;--jarak:${d};--laju:${k}`;h.appendChild(i)});
  document.body.appendChild(h);
}
let tx=0,ty=0,x=0,y=0,jalan=false;
function gambar(){
  x+=(tx-x)*0.08;y+=(ty-y)*0.08;
  html.style.setProperty('--px',x.toFixed(4));html.style.setProperty('--py',y.toFixed(4));
  if(Math.abs(tx-x)>0.001||Math.abs(ty-y)>0.001)requestAnimationFrame(gambar);else jalan=false;
}
addEventListener('pointermove',e=>{
  if(e.pointerType!=='mouse')return;
  // diam saat sedang mengisi formulir
  if(document.activeElement&&document.activeElement.matches('input,textarea,select'))return;
  tx=e.clientX/innerWidth*2-1;ty=e.clientY/innerHeight*2-1;
  if(!jalan){jalan=true;requestAnimationFrame(gambar)}
});
let tunggu=false;
function gulir(){if(tunggu)return;tunggu=true;requestAnimationFrame(()=>{html.style.setProperty('--gulir',Math.min(scrollY,1500));tunggu=false})}
addEventListener('scroll',gulir,{passive:true});gulir();
})();
</script>"""

PAGES["base.html"] = PAGES["base.html"].replace("</body></html>", PARALAKS_UMUM + "</body></html>")
app.jinja_loader = DictLoader(PAGES)

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
