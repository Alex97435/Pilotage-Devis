# api/index.py

import sys
from pathlib import Path

# Ajoute le dossier parent (la racine du projet) dans le chemin Python,
# afin que l'importation de "app" fonctionne même lorsque Vercel exécute
# cette fonction depuis le dossier api/
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

# Importe l'application FastAPI depuis app.py
from app import app

# Vercel utilise la variable "handler" (ou "app") comme point d'entrée
# pour exécuter la fonction serverless.
handler = app
