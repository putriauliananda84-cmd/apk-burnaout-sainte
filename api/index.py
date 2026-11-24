import os
# Paksa database pakai /tmp biar bisa jalan di Vercel
os.environ["DATABASE_URL"] = "sqlite:////tmp/tenangtugas.db"

import sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from app import app
