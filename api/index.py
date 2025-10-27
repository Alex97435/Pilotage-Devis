# api/index.py

import sys
from pathlib import Path

# Ajoute le dossier parent (racine du projet) au chemin Python
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

# Importe l'application FastAPI depuis app.py
from app import app as fastapi_app

# Exporte-la sous le nom 'app' (attendu par Vercel)
app = fastapi_app
