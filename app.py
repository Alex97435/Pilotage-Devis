"""
Application de suivi et de centralisation des devis
--------------------------------------------------

Cette application Web est un petit outil de démonstration permettant de
gérer des devis mois par mois, de les catégoriser par corps de métier ou
fournisseur et de produire rapidement un PDF. Il est également
possible d'ajouter une signature sous forme d'image sur le devis.

L'application est basée sur FastAPI et utilise Jinja2 pour rendre
les vues HTML. Les fichiers PDF sont générés via Pillow (PIL) afin
de respecter la contrainte d'absence de bibliothèques externes comme
ReportLab. Les données sont persistées dans une base SQLite.

Avant d'exécuter l'application, assurez‑vous que les dépendances
suivantes sont présentes dans votre environnement Python :

    - fastapi (et starlette)
    - jinja2
    - pillow

Ces bibliothèques sont disponibles dans l'environnement de ce projet.

Pour lancer l'application en local :

    uvicorn app:app --reload --host 0.0.0.0 --port 8000

Ensuite, ouvrez votre navigateur à l'adresse http://localhost:8000.

"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Request, Form, UploadFile, File, Depends
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from PIL import Image, ImageDraw, ImageFont


# Répertoire de base pour stocker les données et fichiers générés
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "static" / "uploads"
SIGNATURE_DIR = BASE_DIR / "static" / "signatures"
TEMPLATES_DIR = BASE_DIR / "templates"

# S'assurer que les répertoires existent
DATA_DIR.mkdir(parents=True, exist_ok=True)
SIGNATURE_DIR.mkdir(parents=True, exist_ok=True)


def get_db_connection():
    """Ouvre une connexion SQLite et crée la table si nécessaire."""
    conn = sqlite3.connect(BASE_DIR / "data.db")
    conn.row_factory = sqlite3.Row
    # Créer la table si elle n'existe pas
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT NOT NULL,
            quote_date TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            amount REAL,
            pdf_filename TEXT,
            signed_pdf_filename TEXT,
            invoice_amount REAL,
            invoice_comment TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    conn.commit()
    return conn


class Quote(BaseModel):
    id: int
    client_name: str
    quote_date: str  # format YYYY-MM-DD
    category: str
    description: Optional[str] = ""
    amount: Optional[float] = 0.0
    pdf_filename: Optional[str] = None
    signed_pdf_filename: Optional[str] = None
    invoice_amount: Optional[float] = None
    invoice_comment: Optional[str] = None
    created_at: str
    updated_at: str

    @property
    def month(self) -> str:
        """Retourne la chaîne AAAA-MM du mois correspondant à la date du devis."""
        return self.quote_date[:7]


def generate_pdf(quote: Quote, signature_path: Optional[Path] = None) -> str:
    """Génère un fichier PDF pour le devis.

    Utilise Pillow pour créer une page blanche A4 et dessiner les
    informations du devis. Si un chemin vers une image de signature est
    fourni, l'image est collée dans le coin inférieur droit.

    :param quote: objet Quote contenant les informations à afficher
    :param signature_path: chemin vers l'image de signature (facultatif)
    :return: nom de fichier du PDF généré
    """
    # Dimensions A4 en points à 72 DPI (approximation) : 595 x 842
    width, height = 595, 842
    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)

    # Charger une police par défaut. Si la police n'existe pas, la police par
    # défaut de Pillow sera utilisée.
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 14)
    except Exception:
        font = ImageFont.load_default()

    # Texte du devis à afficher
    lines = [
        f"Devis n° {quote.id}",
        f"Client : {quote.client_name}",
        f"Date : {quote.quote_date}",
        f"Catégorie : {quote.category}",
        f"Montant : {quote.amount:.2f} EUR",
        "",
        "Description :",
    ]
    # Ajouter la description multi‑lignes
    if quote.description:
        for line in quote.description.split("\n"):
            lines.append(line)
    else:
        lines.append("(aucune description)")

    # Position initiale
    x_margin = 40
    y = 780
    line_height = 20

    for line in lines:
        draw.text((x_margin, y), line, fill="black", font=font)
        y -= line_height

    # Ajouter la signature si elle existe
    if signature_path and signature_path.exists():
        try:
            sig_img = Image.open(signature_path).convert("RGBA")
            # Redimensionner l'image de signature pour qu'elle tienne dans un
            # rectangle de 200x100 pixels (conserve le ratio)
            max_sig_width = 200
            max_sig_height = 100
            ratio = min(max_sig_width / sig_img.width, max_sig_height / sig_img.height, 1.0)
            new_size = (int(sig_img.width * ratio), int(sig_img.height * ratio))
            sig_resized = sig_img.resize(new_size, Image.LANCZOS)

            # Calculer la position de la signature (coin inférieur droit)
            sig_x = width - x_margin - new_size[0]
            sig_y = 40  # marge inférieure

            # Coller l'image sur le PDF. Utiliser le canal alpha pour gérer la transparence
            img.paste(sig_resized, (sig_x, sig_y), sig_resized)
        except Exception:
            # Si un problème survient avec l'image, on ignore la signature
            pass

    # Construire le nom du fichier
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"quote_{quote.id}_{timestamp}.pdf"
    filepath = DATA_DIR / filename
    # Enregistrer l'image en PDF
    img.save(filepath, "PDF", resolution=100.0)
    return filename


def save_signature(upload_file: UploadFile) -> Optional[Path]:
    """Enregistre le fichier de signature téléchargé et retourne le chemin.

    :param upload_file: fichier envoyé par l'utilisateur
    :return: chemin du fichier enregistré ou None en cas d'erreur
    """
    try:
        suffix = Path(upload_file.filename).suffix
        if suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            return None
        sig_name = f"sig_{datetime.now().strftime('%Y%m%d%H%M%S')}{suffix}"
        sig_path = SIGNATURE_DIR / sig_name
        with open(sig_path, "wb") as f:
            content = upload_file.file.read()
            f.write(content)
        return sig_path
    except Exception:
        return None


# Créer l'application FastAPI
app = FastAPI(title="Gestion des devis")

# Monter le répertoire static pour servir les PDF et images
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Configurer les templates Jinja2
templates = Jinja2Templates(directory=TEMPLATES_DIR)


@app.middleware("http")
async def db_session_middleware(request: Request, call_next):
    """Ouvre et ferme une connexion à la base pour chaque requête."""
    request.state.db = get_db_connection()
    response = await call_next(request)
    request.state.db.close()
    return response


def get_db(request: Request):
    """Dépendance FastAPI pour accéder à la base."""
    return request.state.db


@app.get("/")
def index(request: Request, month: Optional[str] = None, category: Optional[str] = None, db: sqlite3.Connection = Depends(get_db)):
    """Affiche la liste des devis avec filtres facultatifs sur le mois et la catégorie."""
    query = "SELECT * FROM quotes"
    filters: List[str] = []
    params: List = []
    if month:
        filters.append("quote_date LIKE ?")
        params.append(f"{month}%")
    if category:
        filters.append("category = ?")
        params.append(category)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY quote_date DESC"
    cur = db.execute(query, params)
    rows = cur.fetchall()
    quotes = [Quote(**row) for row in rows]

    # Récupérer la liste unique des catégories et des mois disponibles pour les filtres
    cats = db.execute("SELECT DISTINCT category FROM quotes ORDER BY category").fetchall()
    months = db.execute("SELECT DISTINCT substr(quote_date, 1, 7) as month FROM quotes ORDER BY month DESC").fetchall()
    categories = [c[0] for c in cats]
    month_list = [m[0] for m in months]
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "quotes": quotes,
            "selected_month": month,
            "selected_category": category,
            "categories": categories,
            "months": month_list,
        },
    )


@app.get("/import_excel")
def import_excel_form(request: Request):
    """Formulaire d'import de devis depuis un fichier Excel/CSV."""
    return templates.TemplateResponse("import_excel.html", {"request": request})


@app.post("/import_excel")
async def import_excel(
    request: Request,
    excel_file: UploadFile = File(...),
    db: sqlite3.Connection = Depends(get_db),
):
    """Traite le fichier Excel/CSV téléchargé et importe des devis en base."""
    # Sauvegarder temporairement le fichier téléchargé
    suffix = Path(excel_file.filename).suffix.lower()
    import tempfile, pandas as pd
    # On lit CSV ou Excel selon l'extension
    try:
        content = await excel_file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        if suffix in [".xlsx", ".xls"]:
            df = pd.read_excel(tmp_path)
        elif suffix in [".csv", ".txt"]:
            df = pd.read_csv(tmp_path)
        else:
            return RedirectResponse(url="/import_excel", status_code=303)
        # Colonnes attendues : client_name, quote_date, category, description, amount
        imported_count = 0
        now = datetime.now().isoformat()
        for _, row_data in df.iterrows():
            try:
                client = str(row_data.get("client_name", "")).strip()
                date = str(row_data.get("quote_date", "")).strip()
                cat = str(row_data.get("category", "")).strip()
                desc = str(row_data.get("description", ""))
                amt = float(row_data.get("amount", 0.0))
                if not client or not date or not cat:
                    continue
                # Insérer le devis
                cur = db.execute(
                    "INSERT INTO quotes (client_name, quote_date, category, description, amount, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (client, date, cat, desc, amt, now, now),
                )
                quote_id = cur.lastrowid
                db.commit()
                # Charger quote et générer PDF
                row_q = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
                quote = Quote(**row_q)
                pdf_filename = generate_pdf(quote)
                db.execute(
                    "UPDATE quotes SET pdf_filename = ?, updated_at = ? WHERE id = ?",
                    (pdf_filename, now, quote_id),
                )
                db.commit()
                imported_count += 1
            except Exception:
                continue
        # Nettoyer le fichier temporaire
        import os
        os.remove(tmp_path)
        # Rediriger vers l'accueil avec un paramètre de succès
        return RedirectResponse(url="/", status_code=303)
    except Exception:
        return RedirectResponse(url="/import_excel", status_code=303)


@app.get("/new")
def new_quote_form(request: Request):
    """Affiche le formulaire de création d'un nouveau devis."""
    return templates.TemplateResponse("new.html", {"request": request})


@app.post("/new")
async def create_quote(
    request: Request,
    client_name: str = Form(...),
    quote_date: str = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    amount: float = Form(0.0),
    pdf_upload: UploadFile = File(None),
    db: sqlite3.Connection = Depends(get_db),
):
    """Crée un nouveau devis.

    Si un fichier PDF est téléchargé, il est utilisé comme devis existant. Sinon un PDF est généré à partir des données.
    """
    now = datetime.now().isoformat()
    cur = db.execute(
        """
        INSERT INTO quotes (client_name, quote_date, category, description, amount, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (client_name, quote_date, category, description, amount, now, now),
    )
    quote_id = cur.lastrowid
    db.commit()
    # Récupérer l'enregistrement
    row = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    quote = Quote(**row)
    pdf_filename: Optional[str] = None
    if pdf_upload and pdf_upload.filename:
        # Enregistrer le fichier PDF fourni
        suffix = Path(pdf_upload.filename).suffix
        if suffix.lower() == ".pdf":
            pdf_name = f"imported_quote_{quote.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}{suffix}"
            pdf_path = DATA_DIR / pdf_name
            with open(pdf_path, "wb") as f:
                content = await pdf_upload.read()
                f.write(content)
            pdf_filename = pdf_name
    # Si aucun PDF importé, générer le PDF
    if not pdf_filename:
        pdf_filename = generate_pdf(quote)
    # Mettre à jour l'enregistrement
    db.execute(
        "UPDATE quotes SET pdf_filename = ?, updated_at = ? WHERE id = ?",
        (pdf_filename, now, quote_id),
    )
    db.commit()
    return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)


@app.get("/quote/{quote_id}")
def quote_detail(request: Request, quote_id: int, db: sqlite3.Connection = Depends(get_db)):
    """Affiche les détails d'un devis et propose le téléchargement ou la signature."""
    row = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    return templates.TemplateResponse(
        "quote_detail.html",
        {
            "request": request,
            "quote": quote,
        },
    )


@app.get("/quote/{quote_id}/download")
def download_pdf(quote_id: int, signed: bool = False, db: sqlite3.Connection = Depends(get_db)):
    """Permet de télécharger le PDF (signé ou non)."""
    row = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    filename = quote.signed_pdf_filename if signed and quote.signed_pdf_filename else quote.pdf_filename
    if not filename:
        return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)
    file_path = DATA_DIR / filename
    return FileResponse(path=file_path, media_type="application/pdf", filename=filename)


@app.get("/quote/{quote_id}/sign")
def sign_form(request: Request, quote_id: int, db: sqlite3.Connection = Depends(get_db)):
    """Affiche le formulaire pour importer une signature et signer le devis."""
    row = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    return templates.TemplateResponse(
        "sign.html",
        {
            "request": request,
            "quote": quote,
        },
    )


@app.post("/quote/{quote_id}/invoice")
async def submit_invoice(
    request: Request,
    quote_id: int,
    invoice_amount: float = Form(...),
    invoice_comment: str = Form(""),
    db: sqlite3.Connection = Depends(get_db),
):
    """Enregistre le montant de la facture finale et un commentaire en cas d'écart."""
    row = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if not row:
        return RedirectResponse(url="/", status_code=303)
    now = datetime.now().isoformat()
    db.execute(
        "UPDATE quotes SET invoice_amount = ?, invoice_comment = ?, updated_at = ? WHERE id = ?",
        (invoice_amount, invoice_comment if invoice_comment else None, now, quote_id),
    )
    db.commit()
    return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)


@app.post("/quote/{quote_id}/sign")
async def sign_quote(
    request: Request,
    quote_id: int,
    signature: UploadFile = File(...),
    db: sqlite3.Connection = Depends(get_db),
):
    """Applique la signature téléchargée au devis et génère un PDF signé."""
    # Enregistrer la signature
    sig_path = save_signature(signature)
    if not sig_path:
        # Signature invalide
        return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)
    # Récupérer les données du devis
    row = db.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    # Générer le PDF signé
    signed_filename = generate_pdf(quote, signature_path=sig_path)
    now = datetime.now().isoformat()
    # Mettre à jour la base avec le nom du PDF signé
    db.execute(
        "UPDATE quotes SET signed_pdf_filename = ?, updated_at = ? WHERE id = ?",
        (signed_filename, now, quote_id),
    )
    db.commit()
    return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)
