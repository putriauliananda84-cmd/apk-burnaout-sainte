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
        DATABASE_URL = "sqlite:////tmp/tenangtugas.db"
    else:
        DATABASE_URL = "sqlite:///app.db"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
engine_options = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite:"):
    engine_options["connect_args"] = {"check_same_thread": False}
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
    return dict(user=g.user, fmt_tgl=fmt_tgl, fmt_menit=fmt_menit, css=CSS, endpoint=request.endpoint,
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
@media(max-width:850px){.app{flex-direction:column}aside{width:100%;height:auto;position:static}.grid2,.grid3,.grid4,.auth{grid-template-columns:1fr}}
"""

PAGES = {}

# Bunyi pengingat dibuat langsung di browser (tanpa file audio)
SUARA_JS = """
<script>
(function(){
const AC=window.AudioContext||window.webkitAudioContext;
const info=document.getElementById('info_suara');
if(!AC){info.textContent='Browser ini tidak mendukung suara.';return}
const ctx=new AC();let nada=[],sudah=false,dimatikan=false;
function bunyi(){
  hentikan();sudah=true;info.textContent='';
  // 3 kali "ting-ting"
  for(let i=0;i<3;i++){[880,1175].forEach((f,j)=>{
    const o=ctx.createOscillator(),v=ctx.createGain(),t=ctx.currentTime+i*0.9+j*0.22;
    o.frequency.value=f;v.gain.setValueAtTime(0.0001,t);
    v.gain.exponentialRampToValueAtTime(0.4,t+0.02);v.gain.exponentialRampToValueAtTime(0.0001,t+0.2);
    o.connect(v);v.connect(ctx.destination);o.start(t);o.stop(t+0.22);nada.push(o)})}
}
function hentikan(){nada.forEach(o=>{try{o.stop()}catch(e){}});nada=[]}
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

PAGES["daftar.html"] = """{% extends 'base.html' %}{% block body %}<div class="auth"><div class="kiri"><div class="logo">TenangTugas</div>
<h1 class="big">Tugas banyak? Mari kita urai pelan-pelan.</h1>
<p class="sub">TenangTugas membantu mahasiswa mengelola tugas yang menumpuk sekaligus menurunkan stres akademik.</p>
<ul><li>🟢 Memecah tugas jadi langkah kecil</li><li>🔵 Menentukan prioritas dengan tenang</li><li>🟡 Ada jeda relaksasi untuk menenangkan diri</li></ul>
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
<p class="meta" style="text-align:center;margin-top:18px">Catatan stres harianmu bersifat pribadi.</p></div></div></div>{% endblock %}"""

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
<iframe class="spotify-embed" src="https://open.spotify.com/embed/playlist/{{ spotify_playlist_id }}?utm_source=generator&theme=0" allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture" loading="lazy" title="Playlist Spotify Ruang Tenang"></iframe>
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
<div class="card"><div class="row"><div><h2 style="margin:0">Akun Saya</h2><p style="margin:6px 0"><b>{{ user.nama }}</b><br><span class="meta">{{ user.email }}</span><br><span class="meta">ID Akun: {{ user.id }}</span></p></div><div style="width:52px;height:52px;border-radius:50%;background:var(--g);color:#fff;display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:700;flex-shrink:0">{{ user.nama[0]|upper }}</div></div><a class="btn o" href="{{ url_for('keluar') }}">Logout</a></div>
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

app.jinja_loader = DictLoader(PAGES)

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
