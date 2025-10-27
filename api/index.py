# api/index.py

import sys
from pathlib import Path

# Ajouter le dossier parent (racine du projet) au chemin Python
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

# Importer l'application FastAPI définie dans app.py
from app import app as fastapi_app

# Vercel utilise la variable "app" comme point d'entrée
app = fastapi_app
