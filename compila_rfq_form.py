# -*- coding: utf-8 -*-
"""

Compilazione automatica form RFQ stampi.
Matching su colonna 'cad_filename' in settings.csv.

Funzionalità:
- analisi geometrica robusta
- volume, area proiettata, bounding box
- diagnosi volume zero
- repair automatico shell/solidi
- repair con tolleranze multiple
- spessore pareti con ray-casting (nominale, medio, massimo)
- soglia massima spessore da CSV
- log su file
- analisi separata per singolo corpo se assembly/multibody
"""

import os
import re
import copy
import csv
import traceback
import math
import random
import logging
from datetime import datetime
from types import SimpleNamespace
from statistics import mean, median

import openpyxl
from openpyxl.drawing.image import Image as XLImage
from openpyxl.worksheet.cell_range import CellRange

import cadquery as cq

from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.GProp import GProp_GProps
from OCP.TopAbs import (
    TopAbs_SOLID,
    TopAbs_COMPSOLID,
    TopAbs_COMPOUND,
    TopAbs_SHELL,
    TopAbs_FACE,
)
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib

from OCP.ShapeFix import ShapeFix_Shape
from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing, BRepBuilderAPI_MakeSolid
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.BRep import BRep_Tool, BRep_Builder
from OCP.TopoDS import TopoDS_Compound
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopLoc import TopLoc_Location
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.gp import gp_Pnt, gp_Vec, gp_Dir, gp_Lin

try:
    import cairosvg
except Exception:
    cairosvg = None


# =========================================================================
# PERCORSI
# =========================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PATH_SETTINGS = os.path.join(BASE_DIR, "settings.csv")
PATH_INPUT = os.path.join(BASE_DIR, "input")
OUTPUT_DIR_VISTE = os.path.join(BASE_DIR, "viste")
LOG_FILE = os.path.join(BASE_DIR, "analisi.log")


# =========================================================================
# CONFIGURAZIONE GENERALE
# =========================================================================
DEBUG_GEOMETRY = True

# Tolleranze usate per il repair progressivo.
# Dalla più fine alla più aggressiva.
REPAIR_TOLERANCES = [
    1e-5,
    5e-5,
    1e-4,
    5e-4,
    1e-3,
]

# Valore usato se il CSV non definisce una soglia massima.
DEFAULT_MAX_THICKNESS_MM = 150.0

# Parametri misura spessore (ray-casting).
# Numero di raggi per corpo: i punti di partenza sono distribuiti
# uniformemente sulla superficie (campionamento pesato sull'area).
THICKNESS_RAY_COUNT = 3000
# Angolo massimo tra il raggio e la normale della parete opposta:
# oltre questo valore il raggio ha colpito una parete laterale
# (spigoli, raccordi) e il campione viene scartato.
THICKNESS_MAX_EXIT_ANGLE_DEG = 45.0
# Distanza minima per ignorare l'intersezione con la faccia di partenza.
THICKNESS_MIN_MM = 1e-3
# Se diametro sfera inscritta / lunghezza raggio è sotto questa soglia,
# il raggio non ha attraversato una parete e viene scartato.
THICKNESS_SPHERE_RATIO = 0.5
# Seme fisso: risultati ripetibili a ogni esecuzione.
THICKNESS_RANDOM_SEED = 12345

# Se True, gli assembly/multibody vengono analizzati corpo per corpo.
PROCESS_ASSEMBLY_BODIES_SEPARATELY = True


# =========================================================================
# CONFIGURAZIONE TEMPLATE EXCEL
# =========================================================================
# Nomi cercati nella cartella dello script, in ordine di priorità.
# Se nessuno esiste, viene cercato un qualsiasi file "*rfq*form*.xls*".
TEMPLATE_CANDIDATES = [
    "RFQ_molds_form.xlsx",
    "RFQ_molds_form.xlsm",
    "RFQ_molds_form.xltx",
]

# Nome del foglio del form. None = foglio attivo del template.
TEMPLATE_SHEET_NAME = None

# Prima riga dati del form e cella della data.
DATA_START_ROW = 3
DATE_CELL = "E5"

# Colonna Excel per lo spessore massimo (es. "M"). None = non scritto.
THICKNESS_MAX_COLUMN = None


# =========================================================================
# LOGGING
# =========================================================================
logger = logging.getLogger("analisi_rfq")
logger.setLevel(logging.INFO)

if not logger.handlers:
    file_handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    console_handler = logging.StreamHandler()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


# =========================================================================
# FUNZIONI GENERICHE
# =========================================================================
def sanitize_filename(value):
    """Rende una stringa sicura come nome file."""
    value = str(value or "").strip()
    value = re.sub(r"[^\w\-]+", "_", value, flags=re.UNICODE)
    return value.strip("_") or "Default_Project"


def normalize_key(value):
    """Normalizza nomi di colonna/chiavi CSV per matching robusto."""
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def cfg_get(cfg, key, default=""):
    """
    Legge un valore dal dizionario cfg in modo robusto.
    Gestisce differenze tipo:
    - 'part_name'
    - 'part name'
    - 'Part Name'
    - 'PART_NAME'
    """
    if not cfg:
        return default

    target = normalize_key(key)
    if not target:
        return default

    val = None

    # Match diretto
    if key in cfg:
        val = cfg[key]

    # Match normalizzato
    if val is None:
        for k, v in cfg.items():
            if normalize_key(k) == target:
                val = v
                break

    # Tentativi extra con spazi/underscore
    if val is None:
        variants = [
            key.replace("_", " "),
            key.replace(" ", "_"),
        ]
        for variant in variants:
            if variant in cfg:
                val = cfg[variant]
                break

    return default if val is None else val


def normalize_filename(value):
    """Normalizza il nome file per matching robusto."""
    value = os.path.basename(str(value or ""))
    value = os.path.splitext(value)[0]
    value = re.sub(r"[^a-z0-9]+", "", value.lower())
    return value


def parse_float_value(value):
    """Converte un valore in float in modo robusto."""
    if value is None:
        return None

    txt = str(value).strip().replace(",", ".")
    if not txt:
        return None

    try:
        return float(txt)
    except Exception:
        return None


def get_max_thickness_from_cfg(cfg):
    """
    Legge la soglia massima di spessore dal CSV.

    Chiavi supportate:
    - max_thickness_mm
    - thickness_max_mm
    - max thickness mm
    - spessore_max_mm
    """
    keys = [
        "max_thickness_mm",
        "thickness_max_mm",
        "max thickness mm",
        "spessore_max_mm",
        "max_thickness",
        "thickness_max",
    ]

    for key in keys:
        val = parse_float_value(cfg_get(cfg, key, ""))
        if val is not None and val > 0:
            return float(val)

    return float(DEFAULT_MAX_THICKNESS_MM)


# =========================================================================
# SUPPORTO OCCT / CADQUERY
# =========================================================================
def wrap_occ(shape):
    """Ritorna l'oggetto OCCT sottostante, se presente."""
    return getattr(shape, "wrapped", shape)


def wrap_cq(wrapped):
    """Prova a trasformare un oggetto OCCT in oggetto CadQuery."""
    if wrapped is None:
        return None

    if hasattr(wrapped, "wrapped"):
        return wrapped

    try:
        return cq.Shape.cast(wrapped)
    except Exception:
        return None


def gprop_call(name, wrapped, props):
    """
    Chiama un metodo statico di BRepGProp (VolumeProperties, SurfaceProperties).

    Nel binding OCP (usato da CadQuery) i metodi statici hanno il suffisso
    '_s' (es. BRepGProp.VolumeProperties_s): chiamare la versione senza
    suffisso solleva AttributeError. Il fallback senza suffisso resta per
    compatibilità con pythonocc.
    """
    fn = getattr(BRepGProp, name + "_s", None) or getattr(BRepGProp, name)
    fn(wrapped, props)
    return props


def signed_volume(wrapped):
    """
    Volume con segno: negativo se il solido è orientato al contrario
    (normali delle facce verso l'interno).
    """
    props = gprop_call("VolumeProperties", wrapped, GProp_GProps())
    return float(props.Mass())


def occ_signature(wrapped):
    """
    Firma geometrica semplice per ridurre duplicati.
    Usa volume + baricentro. Se non riesce, usa id().
    """
    if wrapped is None:
        return None

    try:
        props = gprop_call("VolumeProperties", wrapped, GProp_GProps())
        c = props.CentreOfMass()
        return (
            round(float(props.Mass()), 6),
            round(float(c.X()), 3),
            round(float(c.Y()), 3),
            round(float(c.Z()), 3),
        )
    except Exception:
        try:
            return id(wrapped)
        except Exception:
            return None


def shape_type_name(shape_type):
    """Nome leggibile del tipo shape OCCT."""
    mapping = {
        TopAbs_SOLID: "SOLID",
        TopAbs_COMPSOLID: "COMPSOLID",
        TopAbs_COMPOUND: "COMPOUND",
        TopAbs_SHELL: "SHELL",
        TopAbs_FACE: "FACE",
    }
    return mapping.get(shape_type, f"TYPE_{shape_type}")


# =========================================================================
# GEOMETRIA BASE
# =========================================================================
def get_top_shapes(imported):
    """
    Estrae gli oggetti Shape da un risultato importStep.
    Gestisce Workplane, Assembly/Compound e oggetti già Shape.
    """
    objs = []

    # Workplane / oggetti con vals()
    if hasattr(imported, "vals"):
        try:
            vals = imported.vals()
            if isinstance(vals, (list, tuple)):
                objs.extend(vals)
            elif vals is not None:
                objs.append(vals)
        except Exception:
            pass

    # Se non ha vals(), prova val()
    if not objs and hasattr(imported, "val"):
        try:
            v = imported.val()
            if v is not None:
                objs.append(v)
        except Exception:
            pass

    # Se è un Assembly, prova a convertirlo in Compound
    if not objs and hasattr(imported, "toCompound"):
        try:
            c = imported.toCompound()
            if c is not None:
                objs.append(c)
        except Exception:
            pass

    # Se è già una Shape
    if not objs and hasattr(imported, "wrapped"):
        objs.append(imported)

    # Filtra oggetti nulli
    valid_objs = []
    for o in objs:
        try:
            w = getattr(o, "wrapped", o)
            if w is not None and not w.IsNull():
                valid_objs.append(o)
        except Exception:
            continue

    return valid_objs


def combine_shapes(shapes):
    """Combina più shape in un compound, se possibile."""
    shapes = [s for s in shapes if s is not None]

    if not shapes:
        return None

    if len(shapes) == 1:
        return shapes[0]

    try:
        return cq.Compound.makeCompound(shapes)
    except Exception:
        return shapes[0]


def safe_volume(shape):
    """
    Calcola il volume in modo robusto usando direttamente OpenCASCADE.
    Ritorna 0.0 se il volume non è calcolabile.
    """
    if shape is None:
        return 0.0

    wrapped = getattr(shape, "wrapped", shape)
    if wrapped is None:
        return 0.0

    try:
        if wrapped.IsNull():
            return 0.0
    except Exception:
        return 0.0

    try:
        shape_type = wrapped.ShapeType()
    except Exception:
        return 0.0

    # Consideriamo solo forme potenzialmente volumetriche
    if shape_type not in (
        TopAbs_SOLID,
        TopAbs_COMPSOLID,
        TopAbs_COMPOUND,
        TopAbs_SHELL,
    ):
        return 0.0

    # Se è una shell, proviamo solo se chiusa
    if shape_type == TopAbs_SHELL:
        try:
            if not wrapped.Closed():
                return 0.0
        except Exception:
            pass

    try:
        return abs(signed_volume(wrapped))
    except Exception as e:
        logger.debug(f"Volume non calcolabile: {e}")
        return 0.0


def safe_surface_area(shape):
    """
    Calcola l'area superficiale in modo robusto usando direttamente OpenCASCADE.
    Ritorna 0.0 se non calcolabile.
    """
    if shape is None:
        return 0.0

    wrapped = getattr(shape, "wrapped", shape)
    if wrapped is None:
        return 0.0

    try:
        if wrapped.IsNull():
            return 0.0
    except Exception:
        return 0.0

    try:
        props = gprop_call("SurfaceProperties", wrapped, GProp_GProps())
        return abs(float(props.Mass()))
    except Exception as e:
        logger.debug(f"Area non calcolabile: {e}")
        return 0.0


def collect_solids(shapes):
    """Raccoglie tutti i Solid dalle shape fornite."""
    solids = []
    seen_ids = set()

    for sh in shapes:
        if id(sh) in seen_ids:
            continue

        # Prova con metodo Solids()
        try:
            ss = sh.Solids()
            if ss:
                for s in ss:
                    if id(s) not in seen_ids:
                        solids.append(s)
                        seen_ids.add(id(s))
                continue
        except Exception:
            pass

        # Se non ha Solids() ma è esso stesso un Solid
        try:
            w = getattr(sh, "wrapped", None)
            if w is not None and not w.IsNull() and w.ShapeType() == TopAbs_SOLID:
                solids.append(sh)
                seen_ids.add(id(sh))
        except Exception:
            pass

    return solids


def collect_faces(shapes):
    """Raccoglie tutte le Face dalle shape fornite."""
    faces = []
    seen_ids = set()

    for sh in shapes:
        if id(sh) in seen_ids:
            continue

        # Prova con metodo Faces()
        try:
            fs = sh.Faces()
            if fs:
                for f in fs:
                    if id(f) not in seen_ids:
                        faces.append(f)
                        seen_ids.add(id(f))
                continue
        except Exception:
            pass

        # Se non ha Faces() ma è esso stesso una Face
        try:
            w = getattr(sh, "wrapped", None)
            if w is not None and not w.IsNull() and w.ShapeType() == TopAbs_FACE:
                faces.append(sh)
                seen_ids.add(id(sh))
        except Exception:
            pass

    return faces


def safe_face_area(face):
    """Calcola area di una faccia in modo sicuro."""
    try:
        return float(face.Area())
    except Exception:
        return safe_surface_area(face)


def safe_bounding_box(shapes):
    """
    Calcola il bounding box in modo robusto usando direttamente OpenCASCADE.
    Ritorna un oggetto SimpleNamespace con xlen, ylen, zlen.
    """
    if not shapes:
        return None

    bbox = Bnd_Box()
    added = False

    for sh in shapes:
        wrapped = getattr(sh, "wrapped", sh)
        if wrapped is None:
            continue

        try:
            if wrapped.IsNull():
                continue
        except Exception:
            continue

        try:
            BRepBndLib.Add_s(wrapped, bbox, False)
            added = True
        except Exception:
            try:
                BRepBndLib.Add(wrapped, bbox)
                added = True
            except Exception:
                continue

    if not added:
        return None

    try:
        if bbox.IsVoid():
            return None
    except Exception:
        pass

    try:
        xmin, ymin, zmin, xmax, ymax, zmax = bbox.Get()
    except Exception:
        return None

    return SimpleNamespace(
        xmin=xmin,
        ymin=ymin,
        zmin=zmin,
        xmax=xmax,
        ymax=ymax,
        zmax=zmax,
        xlen=max(0.0, xmax - xmin),
        ylen=max(0.0, ymax - ymin),
        zlen=max(0.0, zmax - zmin),
    )


def bbox_diagonal(shapes):
    """Diagonale del bounding box, utile come limite massimo spessore."""
    bb = safe_bounding_box(shapes)
    if bb is None:
        return 100.0

    return math.sqrt(
        bb.xlen * bb.xlen +
        bb.ylen * bb.ylen +
        bb.zlen * bb.zlen
    )


# =========================================================================
# LOGICA GEOMETRIA AVANZATA
# =========================================================================
def determine_best_parting_axis(faces):
    """
    Determina l'asse di sformo ottimale analizzando i sottosquadri.
    Ritorna:
    ((vx, vy, vz), nome_asse), min_undercuts
    """
    axes = [
        ((0.0, 0.0, 1.0), "<Z or >Z"),
        ((1.0, 0.0, 0.0), "<X or >X"),
        ((0.0, 1.0, 0.0), "<Y or >Y"),
    ]

    if not faces:
        return axes[0], 0

    best_axis = axes[0]
    min_undercuts = float("inf")

    for ax_vector, ax_name in axes:
        undercut_count = 0

        for f in faces:
            try:
                fn = f.normalAt()
                dot = abs(
                    fn.x * ax_vector[0]
                    + fn.y * ax_vector[1]
                    + fn.z * ax_vector[2]
                )

                # Faccia quasi parallela alla direzione considerata
                if dot < 0.01:
                    undercut_count += 1
            except Exception:
                continue

        if undercut_count < min_undercuts:
            min_undercuts = undercut_count
            best_axis = (ax_vector, ax_name)

    if min_undercuts == float("inf"):
        return axes[0], 0

    return best_axis, min_undercuts


def calculate_projected_area_accurate(faces, direction):
    """
    Calcola l'area proiettata sommando il contributo delle facce visibili
    rispetto alla direzione di sformo.
    """
    if not faces:
        return 0.0

    projected_area = 0.0

    for f in faces:
        try:
            fn = f.normalAt()
            cos_angle = (
                fn.x * direction[0]
                + fn.y * direction[1]
                + fn.z * direction[2]
            )

            if cos_angle > 0:
                face_area = safe_face_area(f)
                projected_area += face_area * abs(cos_angle)
        except Exception:
            continue

    return projected_area


# =========================================================================
# MATCHING CSV
# =========================================================================
def find_cfg_for_step(step_path, cfg_list):
    """
    Trova la configurazione CSV usando 'cad_filename' con matching flessibile.
    Restituisce {} se non trova corrispondenza.
    """
    if not cfg_list:
        return {}

    filename = os.path.basename(str(step_path))
    stem = os.path.splitext(filename)[0]
    file_norm = normalize_filename(filename)

    # 1) Match esatto su nome file completo o stem
    for cfg in cfg_list:
        cad_name = str(cfg_get(cfg, "cad_filename", "")).strip()
        if not cad_name:
            continue

        cad_base = os.path.basename(cad_name)
        cad_stem = os.path.splitext(cad_base)[0]

        if cad_base.lower() == filename.lower():
            return cfg

        if cad_stem.lower() == stem.lower():
            return cfg

    # 2) Match normalizzato / sottostringa
    for cfg in cfg_list:
        cad_name = str(cfg_get(cfg, "cad_filename", "")).strip()
        if not cad_name:
            continue

        cad_norm = normalize_filename(cad_name)

        if not cad_norm or not file_norm:
            continue

        if file_norm in cad_norm or cad_norm in file_norm:
            logger.info(f"Match flessibile: '{filename}' ↔ cad_filename='{cad_name}'")
            return cfg

    logger.warning(
        f"Nessuna config per '{filename}'. "
        f"Verificare 'cad_filename' in settings.csv."
    )
    return {}


# =========================================================================
# EXCEL
# =========================================================================
def copy_row_formatting(source_ws, source_row, target_row):
    """Copia la formattazione da una riga sorgente a una riga destinazione."""
    for col in range(1, source_ws.max_column + 1):
        source_cell = source_ws.cell(row=source_row, column=col)
        target_cell = source_ws.cell(row=target_row, column=col)

        if source_cell.has_style:
            target_cell.font = copy.copy(source_cell.font)
            target_cell.border = copy.copy(source_cell.border)
            target_cell.fill = copy.copy(source_cell.fill)
            target_cell.number_format = copy.copy(source_cell.number_format)
            target_cell.protection = copy.copy(source_cell.protection)
            target_cell.alignment = copy.copy(source_cell.alignment)


def insert_row_keep_layout(ws, row_idx):
    """
    Inserisce una riga come ws.insert_rows(), ma sposta anche celle unite
    e altezze righe sottostanti: openpyxl non lo fa, e senza questo il
    layout del template (intestazioni, note, piè di pagina) si rompe.
    """
    merged_below = [
        str(rng) for rng in ws.merged_cells.ranges
        if rng.min_row >= row_idx
    ]
    for ref in merged_below:
        ws.unmerge_cells(ref)

    heights = {
        r: dim.height
        for r, dim in ws.row_dimensions.items()
        if r >= row_idx and dim.height is not None
    }

    ws.insert_rows(row_idx)

    for ref in merged_below:
        rng = CellRange(ref)
        rng.shift(row_shift=1)
        ws.merge_cells(rng.coord)

    for r in sorted(heights, reverse=True):
        ws.row_dimensions[r + 1].height = heights[r]
    ws.row_dimensions[row_idx].height = ws.row_dimensions[row_idx - 1].height


def find_template_path():
    """
    Cerca il template RFQ nella cartella dello script.
    Ritorna il percorso, oppure None se non trovato.
    """
    try:
        files = os.listdir(BASE_DIR)
    except Exception:
        return None

    by_lower = {f.lower(): f for f in files}

    for name in TEMPLATE_CANDIDATES:
        real = by_lower.get(name.lower())
        if real:
            return os.path.join(BASE_DIR, real)

    for f in sorted(files):
        low = f.lower()
        if (
            "rfq" in low and "form" in low
            and low.endswith((".xlsx", ".xlsm", ".xltx"))
            and not low.startswith("~$")
        ):
            return os.path.join(BASE_DIR, f)

    return None


def open_template(template_path):
    """
    Apre il template mantenendone il formato.
    Ritorna (workbook, estensione output).
    """
    ext = os.path.splitext(template_path)[1].lower()
    keep_vba = ext == ".xlsm"

    wb = openpyxl.load_workbook(template_path, keep_vba=keep_vba)

    if ext == ".xltx":
        # Salva come cartella di lavoro normale, non come modello.
        wb.template = False

    return wb, (".xlsm" if keep_vba else ".xlsx")


# =========================================================================
# DIAGNOSTICA / REPORT
# =========================================================================
def log_geometry_report(
    step_path,
    body_index,
    body_count,
    shapes,
    original_solids,
    repaired_solids,
    used_solids,
    faces,
    used_volume,
    used_surface,
    thickness,
    cfg_max_thickness,
    effective_max_thickness,
    repair_note,
    repair_logs,
):
    """
    Scrive un report dettagliato su console e file log.
    """
    lines = []
    lines.append("=" * 80)
    lines.append(
        f"REPORT GEOMETRIA | {os.path.basename(step_path)} | "
        f"Body {body_index}/{body_count}"
    )
    lines.append("=" * 80)

    type_counts = {}
    for sh in shapes:
        try:
            st = wrap_occ(sh).ShapeType()
            name = shape_type_name(st)
        except Exception:
            name = "UNKNOWN"

        type_counts[name] = type_counts.get(name, 0) + 1

    lines.append(f"Shapes input: {len(shapes)}")
    lines.append(f"Tipologie shapes: {type_counts}")
    lines.append(f"Solidi originali: {len(original_solids)}")
    lines.append(f"Solidi riparati: {len(repaired_solids)}")
    lines.append(f"Solidi usati: {len(used_solids)}")
    lines.append(f"Facce usate: {len(faces)}")
    lines.append(f"Volume usato: {used_volume:.3f} mm3")
    lines.append(f"Superficie usata: {used_surface:.3f} mm2")
    lines.append(f"Metodo spessore: {thickness.method}")
    lines.append(
        f"Raggi validi: {thickness.samples}/{thickness.rays} "
        f"({thickness.coverage:.0%} della superficie)"
    )
    lines.append(f"Spessore nominale (mediana): {thickness.nominal:.3f} mm")
    lines.append(f"Spessore medio: {thickness.mean:.3f} mm")
    lines.append(
        f"Spessore P2 / P10 / P90 / P98 / max: "
        f"{thickness.p2:.3f} / {thickness.p10:.3f} / "
        f"{thickness.p90:.3f} / {thickness.p98:.3f} / {thickness.max:.3f} mm"
    )
    if thickness.histogram:
        lines.append("Distribuzione spessori (% superficie):")
        for label, pct in thickness.histogram:
            lines.append(f"  {label:>16} mm: {pct:5.1f}%")
    lines.append(f"Max spessore da CSV: {cfg_max_thickness}")
    lines.append(f"Max spessore effettivo: {effective_max_thickness}")
    lines.append(f"Nota repair: {repair_note}")

    if repair_logs:
        lines.append("-" * 80)
        for entry in repair_logs:
            lines.append(f"REPAIR | {entry}")

    if used_volume <= 0.0:
        lines.append("-" * 80)
        lines.append("WARNING: volume = 0. Possibili cause:")
        lines.append("  - STEP contiene superfici/facce e non solidi")
        lines.append("  - shell non chiusa")
        lines.append("  - geometria corrotta o non cucita")
        lines.append("  - modello non valido per calcolo volumetrico")

    if used_surface <= 0.0:
        lines.append("WARNING: superficie = 0. Non posso stimare lo spessore globale.")

    lines.append("=" * 80)

    text = "\n".join(lines)

    if DEBUG_GEOMETRY:
        logger.info(text)


# =========================================================================
# REPAIR GEOMETRICO
# =========================================================================
def fix_occ_shape(wrapped, tol=1e-4):
    """
    Prova a riparare una shape con ShapeFix.
    Ritorna la shape riparata, o quella originale se fallisce.
    """
    if wrapped is None:
        return None

    try:
        if wrapped.IsNull():
            return wrapped
    except Exception:
        pass

    try:
        try:
            fix = ShapeFix_Shape(wrapped)
        except TypeError:
            fix = ShapeFix_Shape()
            fix.Init(wrapped)

        try:
            fix.SetMaxTolerance(tol)
        except Exception:
            pass

        fix.Perform()
        out = fix.Shape()

        if out is not None:
            try:
                if not out.IsNull():
                    return out
            except Exception:
                return out
    except Exception:
        pass

    return wrapped


def sew_occ_shapes(wrapped_list, tol=1e-4):
    """
    Prova a cucire più shape in una shell/compound più sano.
    """
    try:
        sewing = BRepBuilderAPI_Sewing(tol)
        added = False

        for w in wrapped_list:
            if w is None:
                continue

            try:
                if w.IsNull():
                    continue
            except Exception:
                pass

            try:
                sewing.Add(w)
                added = True
            except Exception:
                continue

        if not added:
            return None

        sewing.Perform()
        out = sewing.SewedShape()

        if out is not None:
            try:
                if not out.IsNull():
                    return out
            except Exception:
                return out

        return None
    except Exception:
        return None


def explore_shapes_by_type(wrapped, shape_type):
    """
    Estrae tutte le sotto-shape di un certo tipo usando TopExp_Explorer.
    """
    result = []

    if wrapped is None:
        return result

    try:
        if wrapped.IsNull():
            return result
    except Exception:
        pass

    try:
        exp = TopExp_Explorer(wrapped, shape_type)
        while exp.More():
            cur = exp.Current()
            try:
                if cur is not None and not cur.IsNull():
                    result.append(cur)
            except Exception:
                result.append(cur)
            exp.Next()
    except Exception:
        pass

    return result


def cast_to_topods_shell(shell_occ):
    """
    Prova a convertire una shape in TopoDS_Shell in modo robusto.
    """
    for fn_name in ("Shell_s", "Shell"):
        fn = getattr(TopoDS, fn_name, None)
        if callable(fn):
            try:
                return fn(shell_occ)
            except Exception:
                pass

    try:
        from OCP.TopoDS import topods
        if hasattr(topods, "Shell"):
            return topods.Shell(shell_occ)
    except Exception:
        pass

    return None


def make_solid_from_shell_occ(shell_occ, tol=1e-4):
    """
    Prova a trasformare una shell in solido.
    Prima ripara la shell, poi usa BRepBuilderAPI_MakeSolid.
    """
    if shell_occ is None:
        return None

    fixed = fix_occ_shape(shell_occ, tol)

    shells = explore_shapes_by_type(fixed, TopAbs_SHELL)

    if not shells and fixed is not None:
        try:
            if fixed.ShapeType() == TopAbs_SHELL:
                shells = [fixed]
        except Exception:
            pass

    for sh in shells:
        top_shell = cast_to_topods_shell(sh)
        if top_shell is None:
            continue

        try:
            mk = BRepBuilderAPI_MakeSolid(top_shell)
            if not mk.IsDone():
                continue

            solid = mk.Solid()
            if solid is None:
                continue

            try:
                if solid.IsNull():
                    continue
            except Exception:
                pass

            solid = fix_occ_shape(solid, tol)
            if solid is not None:
                return solid
        except Exception:
            continue

    return None


def try_repair_to_solids(top_shapes, tol=1e-4):
    """
    Prova a ottenere solidi validi partendo dalle shape importate.

    Strategia:
    1. ShapeFix sulle shape originali
    2. ricerca solidi già presenti
    3. sewing delle shape
    4. ricerca solidi nel risultato cucito
    5. conversione shell -> solido
    """
    solids = []
    seen = set()
    repaired_shapes = []
    fixed_wrapped = []

    def add_solid_occ(sol_occ):
        if sol_occ is None:
            return

        try:
            if sol_occ.IsNull():
                return
        except Exception:
            pass

        sig = occ_signature(sol_occ)
        if sig in seen:
            return

        cq_obj = wrap_cq(sol_occ)
        if cq_obj is None:
            return

        if safe_volume(cq_obj) <= 0.0:
            return

        seen.add(sig)
        solids.append(cq_obj)

    # 1. Fix iniziale
    for s in top_shapes:
        w = wrap_occ(s)
        if w is None:
            continue

        fixed = fix_occ_shape(w, tol)
        fixed_wrapped.append(fixed)
        repaired_shapes.append(wrap_cq(fixed) or s)

    # 2. Solidi già presenti dopo fix
    for w in fixed_wrapped:
        for sol in explore_shapes_by_type(w, TopAbs_SOLID):
            add_solid_occ(sol)

        for sol in explore_shapes_by_type(w, TopAbs_COMPSOLID):
            add_solid_occ(sol)

    if solids:
        return solids, repaired_shapes

    # 3. Sewing
    sewed = sew_occ_shapes(fixed_wrapped, tol)
    if sewed is not None:
        repaired_shapes.append(wrap_cq(sewed))

        for sol in explore_shapes_by_type(sewed, TopAbs_SOLID):
            add_solid_occ(sol)

        for sol in explore_shapes_by_type(sewed, TopAbs_COMPSOLID):
            add_solid_occ(sol)

        if solids:
            return solids, repaired_shapes

        # 4/5. Shell cucite -> solidi
        for shell in explore_shapes_by_type(sewed, TopAbs_SHELL):
            solid = make_solid_from_shell_occ(shell, tol)
            add_solid_occ(solid)

        if solids:
            return solids, repaired_shapes

    # 5. Shell residue senza sewing globale
    for w in fixed_wrapped:
        for shell in explore_shapes_by_type(w, TopAbs_SHELL):
            solid = make_solid_from_shell_occ(shell, tol)
            add_solid_occ(solid)

        if solids:
            break

    return solids, repaired_shapes


def try_repair_to_solids_multi(top_shapes, tolerances=None):
    """
    Prova il repair con più tolleranze, in modo progressivo.
    Ritorna:
    - lista migliori di solidi trovati
    - log dei tentativi
    """
    if tolerances is None:
        tolerances = REPAIR_TOLERANCES

    best_solids = []
    best_volume = 0.0
    logs = []

    for tol in tolerances:
        try:
            solids, _ = try_repair_to_solids(top_shapes, tol=tol)
            vol = sum(safe_volume(s) for s in solids)

            logs.append(
                f"tol={tol:.0e} -> solidi={len(solids)}, volume={vol:.6f}"
            )

            if vol > best_volume:
                best_volume = vol
                best_solids = solids

        except Exception as e:
            logs.append(f"tol={tol:.0e} -> errore: {e}")

    return best_solids, logs


# =========================================================================
# SPESSORE PARETI (RAY-CASTING)
# =========================================================================
#
# Metodo:
# 1. si campionano N punti distribuiti uniformemente sulla superficie del
#    solido (ogni punto rappresenta la stessa area);
# 2. da ogni punto si lancia un raggio verso l'interno, lungo la normale
#    esatta della superficie;
# 3. la distanza dalla prima parete incontrata è lo spessore locale;
# 4. il campione è valido solo se la parete d'uscita è "opposta" (la sua
#    normale è quasi parallela al raggio): così si scartano i raggi che
#    entrano in una parete laterale invece di attraversare il materiale;
# 5. controllo "sfera inscritta": si misura il diametro della sfera più
#    grande centrata a metà raggio. Se è molto più piccolo del raggio,
#    il raggio ha corso lungo una parete invece di attraversarla (es.
#    partito dal fianco stretto di una lastra, o dal fondo sotto una
#    nervatura) e il campione viene scartato. Senza questo controllo i
#    fianchi gonfierebbero media e massimo.
#
# Poiché i punti sono pesati sull'area, la mediana dei campioni è lo
# spessore nominale "della maggior parte del pezzo", poco sensibile a
# nervature, borchie e zone massive locali.

THICKNESS_HISTOGRAM_EDGES = [
    0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0, 15.0, 20.0,
    float("inf"),
]


def _vec_norm(x, y, z):
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-12:
        return None
    return (x / length, y / length, z / length)


def face_normal_at(evaluator, u, v):
    """
    Punto e normale (uscente, unitaria) di una faccia nei parametri (u, v).
    BRepGProp_Face tiene conto dell'orientamento della faccia nel solido.
    """
    pnt = gp_Pnt()
    nrm = gp_Vec()
    evaluator.Normal(u, v, pnt, nrm)

    n = _vec_norm(nrm.X(), nrm.Y(), nrm.Z())
    if n is None:
        return None, None

    return (pnt.X(), pnt.Y(), pnt.Z()), n


def make_shells_compound(solid_occ):
    """
    Compound con le shell del solido: serve per la distanza punto-superficie
    (con il solido, un punto interno avrebbe distanza 0).
    """
    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)

    for shell in explore_shapes_by_type(solid_occ, TopAbs_SHELL):
        builder.Add(comp, shell)

    return comp


def distance_to_surface(point, surface_occ):
    """Distanza minima tra un punto e la superficie del solido."""
    vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
    dist = BRepExtrema_DistShapeShape(vertex, surface_occ)
    if not dist.IsDone():
        return None
    return float(dist.Value())


def to_topods_face(shape):
    """Converte una TopoDS_Shape in TopoDS_Face."""
    fn = getattr(TopoDS, "Face_s", None) or getattr(TopoDS, "Face")
    return fn(shape)


def sample_surface_points(solid_occ, count, rng, mesh_tol):
    """
    Campiona 'count' punti distribuiti uniformemente sulla superficie.

    La triangolazione serve solo a sapere DOVE campionare (scelta dei
    triangoli pesata sull'area): le coordinate UV del punto vengono
    interpolate dai nodi UV del triangolo, poi punto e normale vengono
    valutati sulla superficie esatta, senza errori di discretizzazione.

    Ritorna una lista di (punto, normale_uscente).
    """
    BRepMesh_IncrementalMesh(solid_occ, mesh_tol, False, 0.5, True)

    triangles = []
    weights = []

    for face_shape in explore_shapes_by_type(solid_occ, TopAbs_FACE):
        try:
            face = to_topods_face(face_shape)
            poly = BRep_Tool.Triangulation_s(face, TopLoc_Location())
        except Exception:
            continue

        if poly is None or not poly.HasUVNodes():
            continue

        evaluator = BRepGProp_Face(face)

        for i in range(1, poly.NbTriangles() + 1):
            n1, n2, n3 = poly.Triangle(i).Get()
            p1, p2, p3 = poly.Node(n1), poly.Node(n2), poly.Node(n3)

            ax, ay, az = p2.X() - p1.X(), p2.Y() - p1.Y(), p2.Z() - p1.Z()
            bx, by, bz = p3.X() - p1.X(), p3.Y() - p1.Y(), p3.Z() - p1.Z()
            area = 0.5 * math.sqrt(
                (ay * bz - az * by) ** 2
                + (az * bx - ax * bz) ** 2
                + (ax * by - ay * bx) ** 2
            )
            if area <= 1e-12:
                continue

            uvs = [poly.UVNode(k) for k in (n1, n2, n3)]
            triangles.append((
                evaluator,
                [(uv.X(), uv.Y()) for uv in uvs],
            ))
            weights.append(area)

    if not triangles:
        return []

    points = []
    for idx in rng.choices(range(len(triangles)), weights=weights, k=count):
        evaluator, (uv1, uv2, uv3) = triangles[idx]

        # Punto casuale uniforme nel triangolo (coordinate baricentriche).
        r1, r2 = rng.random(), rng.random()
        if r1 + r2 > 1.0:
            r1, r2 = 1.0 - r1, 1.0 - r2

        u = uv1[0] + r1 * (uv2[0] - uv1[0]) + r2 * (uv3[0] - uv1[0])
        v = uv1[1] + r1 * (uv2[1] - uv1[1]) + r2 * (uv3[1] - uv1[1])

        try:
            p, n = face_normal_at(evaluator, u, v)
        except Exception:
            continue

        if p is not None:
            points.append((p, n))

    return points


def measure_wall_thickness(solid, max_thickness, ray_count):
    """
    Misura lo spessore locale di un solido con ray-casting.
    Ritorna (lista spessori validi, numero raggi lanciati).
    """
    wrapped = wrap_occ(solid)
    if wrapped is None:
        return [], 0

    try:
        if wrapped.ShapeType() not in (TopAbs_SOLID, TopAbs_COMPSOLID):
            return [], 0
        vol = signed_volume(wrapped)
    except Exception:
        return [], 0

    if abs(vol) <= 0.0:
        return [], 0

    # Solido orientato al contrario: le normali puntano verso l'interno.
    orientation = -1.0 if vol < 0 else 1.0

    diag = bbox_diagonal([solid])
    mesh_tol = min(max(diag * 2e-3, 0.01), 1.0)

    rng = random.Random(THICKNESS_RANDOM_SEED)
    points = sample_surface_points(wrapped, ray_count, rng, mesh_tol)
    if not points:
        return [], 0

    intersector = IntCurvesFace_ShapeIntersector()
    intersector.Load(wrapped, 1e-6)
    surface = make_shells_compound(wrapped)

    cos_limit = math.cos(math.radians(THICKNESS_MAX_EXIT_ANGLE_DEG))
    samples = []

    for p, n in points:
        d = (-n[0] * orientation, -n[1] * orientation, -n[2] * orientation)

        try:
            line = gp_Lin(gp_Pnt(*p), gp_Dir(*d))
            intersector.Perform(line, THICKNESS_MIN_MM, max_thickness)

            if not intersector.IsDone() or intersector.NbPnt() == 0:
                continue

            hit = min(
                range(1, intersector.NbPnt() + 1),
                key=intersector.WParameter,
            )
            dist = intersector.WParameter(hit)

            _, hit_n = face_normal_at(
                BRepGProp_Face(intersector.Face(hit)),
                intersector.UParameter(hit),
                intersector.VParameter(hit),
            )
        except Exception:
            continue

        if hit_n is None:
            continue

        # La parete d'uscita deve guardare nella stessa direzione del raggio.
        cos_exit = orientation * (
            hit_n[0] * d[0] + hit_n[1] * d[1] + hit_n[2] * d[2]
        )
        if cos_exit < cos_limit:
            continue

        # Sfera inscritta centrata a metà raggio.
        mid = (
            p[0] + d[0] * dist * 0.5,
            p[1] + d[1] * dist * 0.5,
            p[2] + d[2] * dist * 0.5,
        )
        try:
            radius = distance_to_surface(mid, surface)
        except Exception:
            radius = None

        if radius is not None and 2.0 * radius < THICKNESS_SPHERE_RATIO * dist:
            continue

        samples.append(float(dist))

    return samples, len(points)


def percentile(sorted_values, q):
    """Percentile con interpolazione lineare (q tra 0 e 100)."""
    if not sorted_values:
        return 0.0

    pos = (len(sorted_values) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def thickness_histogram(sorted_values):
    """Distribuzione percentuale (sulla superficie) per classi di spessore."""
    total = len(sorted_values)
    if not total:
        return []

    result = []
    edges = THICKNESS_HISTOGRAM_EDGES
    for lo, hi in zip(edges[:-1], edges[1:]):
        # Tolleranza: 3.0 misurato come 2.9999999 resta nella classe 3-4.
        cnt = sum(1 for x in sorted_values if lo <= x + 1e-6 < hi)
        if cnt:
            label = f">= {lo:g}" if math.isinf(hi) else f"{lo:g} - {hi:g}"
            result.append((label, 100.0 * cnt / total))

    return result


def compute_wall_thickness(
    solids,
    fallback_volume,
    fallback_surface,
    max_thickness,
):
    """
    Statistiche di spessore del corpo.

    Priorità:
    1. ray-casting sui solidi
    2. fallback globale 2V/S (esatto solo per lastre sottili)

    Ritorna un SimpleNamespace con: method, nominal (mediana), mean,
    p2, p10, p90, p98, max, samples, rays, coverage, histogram.
    I percentili sono sulla superficie: p98 = 98% della superficie ha
    spessore inferiore (massimo robusto, senza i singoli raggi anomali).
    """
    samples = []
    rays = 0

    if solids:
        areas = [max(safe_surface_area(s), 0.0) for s in solids]
        total_area = sum(areas) or 1.0

        for s, area in zip(solids, areas):
            # Raggi distribuiti tra i solidi in proporzione alla superficie.
            count = max(200, int(round(THICKNESS_RAY_COUNT * area / total_area)))
            try:
                s_samples, s_rays = measure_wall_thickness(s, max_thickness, count)
            except Exception as e:
                logger.warning(f"Misura spessore fallita su un solido: {e}")
                continue

            samples.extend(s_samples)
            rays += s_rays

    if samples:
        values = sorted(samples)
        return SimpleNamespace(
            method="ray_casting",
            nominal=float(median(values)),
            mean=float(mean(values)),
            p2=percentile(values, 2),
            p10=percentile(values, 10),
            p90=percentile(values, 90),
            p98=percentile(values, 98),
            max=values[-1],
            samples=len(values),
            rays=rays,
            coverage=len(values) / rays if rays else 0.0,
            histogram=thickness_histogram(values),
        )

    value = (2.0 * fallback_volume / fallback_surface) if fallback_surface > 0 else 0.0
    value = float(value)
    return SimpleNamespace(
        method="global_estimate_2V/S",
        nominal=value,
        mean=value,
        p2=value,
        p10=value,
        p90=value,
        p98=value,
        max=value,
        samples=0,
        rays=rays,
        coverage=0.0,
        histogram=[],
    )


# =========================================================================
# EXPORT IMMAGINI
# =========================================================================
def export_iso_image(shapes, safe_name, imported=None):
    """
    Genera immagine ISO in PNG.
    Se imported viene fornito, prova prima con quello.
    """
    png_path = None

    try:
        os.makedirs(OUTPUT_DIR_VISTE, exist_ok=True)

        temp_svg = os.path.join(OUTPUT_DIR_VISTE, f"temp_{safe_name}.svg")
        png_candidate = os.path.join(OUTPUT_DIR_VISTE, f"iso_{safe_name}.png")

        exported = False

        if imported is not None:
            try:
                cq.exporters.export(
                    imported,
                    temp_svg,
                    opt={"projectionDir": (1, -1, 0.75)}
                )
                exported = True
            except Exception as e:
                logger.warning(f"Export immagine con imported fallito: {e}")

        if not exported:
            combined = combine_shapes(shapes)
            if combined is None:
                return None

            cq.exporters.export(
                combined,
                temp_svg,
                opt={"projectionDir": (1, -1, 0.75)}
            )

        if os.path.exists(temp_svg):
            if cairosvg is not None:
                cairosvg.svg2png(url=temp_svg, write_to=png_candidate)

                if os.path.exists(png_candidate):
                    png_path = png_candidate

            try:
                os.remove(temp_svg)
            except Exception:
                pass

    except Exception as e:
        logger.warning(f"Impossibile generare immagine: {e}")
        png_path = None

    return png_path


# =========================================================================
# ANALISI CORPI SEPARATI
# =========================================================================
def extract_body_groups(top_shapes):
    """
    Se PROCESS_ASSEMBLY_BODIES_SEPARATELY è True, prova a separare
    assembly o compound multi-solid in corpi distinti.
    """
    if not PROCESS_ASSEMBLY_BODIES_SEPARATELY:
        return [top_shapes]

    groups = []

    for sh in top_shapes:
        try:
            solids = collect_solids([sh])
        except Exception:
            solids = []

        if len(solids) > 1:
            # Multi-solid dentro un singolo oggetto: separiamo i solidi.
            groups.extend([[s] for s in solids])
        else:
            groups.append([sh])

    groups = [g for g in groups if g]

    if len(groups) <= 1:
        return [top_shapes]

    return groups


# =========================================================================
# ANALISI COMPLETA DI UN GRUPPO / CORPO
# =========================================================================
def analyze_shape_group(
    step_path,
    shapes,
    cfg,
    body_index,
    body_count,
    imported_for_image=None,
):
    """
    Analisi geometrica di un singolo corpo/gruppo.
    """
    step_path = str(step_path)
    filename = os.path.basename(step_path)
    clean_name = os.path.splitext(filename)[0]
    safe_name = sanitize_filename(clean_name) or "part"

    if body_count > 1:
        safe_name = f"{safe_name}_body_{body_index}"

    # Solidi originali
    original_solids = collect_solids(shapes)

    original_volume = sum(safe_volume(s) for s in original_solids)

    # Repair multi-tolleranza, solo se i solidi originali non bastano
    if original_volume > 0.0:
        repaired_solids, repair_logs = [], ["repair non necessario"]
    else:
        repaired_solids, repair_logs = try_repair_to_solids_multi(shapes)

    repaired_volume = sum(safe_volume(s) for s in repaired_solids)

    # Sceglie il set migliore di solidi
    if repaired_volume > original_volume:
        solids = repaired_solids
        repair_note = (
            f"Usati solidi riparati "
            f"(volume riparato {repaired_volume:.6f} > "
            f"originale {original_volume:.6f})."
        )
    else:
        solids = original_solids
        if original_solids:
            repair_note = "Usati solidi originali."
        else:
            repair_note = "Nessun solido originale valido."

    # Facce
    face_source = solids if solids else shapes
    faces = collect_faces(face_source)

    if not faces:
        faces = collect_faces(shapes)

    # Volume
    if solids:
        vol = sum(safe_volume(s) for s in solids)
    else:
        vol = sum(safe_volume(s) for s in shapes)

    # Area superficiale
    if solids:
        surf = sum(safe_surface_area(s) for s in solids)
    else:
        surf = sum(safe_surface_area(s) for s in shapes)

    # Fallback area: somma aree facce
    if surf <= 0:
        surf = sum(safe_face_area(f) for f in faces)

    # Bounding box
    bbox_shapes = solids if solids else shapes
    bb = safe_bounding_box(bbox_shapes)

    if bb is None:
        bbox_str = "0.00 x 0.00 x 0.00"
    else:
        bbox_str = f"{bb.xlen:.2f} x {bb.ylen:.2f} x {bb.zlen:.2f}"

    bbox_diag_value = bbox_diagonal(bbox_shapes)

    # Soglia massima spessore da CSV
    cfg_max_thickness = get_max_thickness_from_cfg(cfg)

    if cfg_max_thickness > 0:
        effective_max_thickness = min(
            cfg_max_thickness,
            max(1.0, bbox_diag_value)
        )
    else:
        effective_max_thickness = max(50.0, bbox_diag_value)

    # Spessore pareti (ray-casting)
    thickness = compute_wall_thickness(
        solids,
        vol,
        surf,
        effective_max_thickness,
    )

    # Asse di sformo e area proiettata
    (best_axis_vector, axis_name), min_undercuts = determine_best_parting_axis(faces)
    projected_area = calculate_projected_area_accurate(faces, best_axis_vector)

    # Valore cella B
    part_name = str(cfg_get(cfg, "part_name", "")).strip()
    body_suffix = f" [Body {body_index}/{body_count}]" if body_count > 1 else ""
    b_value = f"{part_name} CAD data: {filename}{body_suffix}".strip()

    # Dati slider/lifter da CSV
    sliders_lifters_info = str(cfg_get(cfg, "slider+lifter_Type/Qty", "")).strip()

    # Immagine ISO
    png_path = export_iso_image(
        shapes=shapes,
        safe_name=safe_name,
        imported=imported_for_image,
    )

    # Report/log
    log_geometry_report(
        step_path=step_path,
        body_index=body_index,
        body_count=body_count,
        shapes=shapes,
        original_solids=original_solids,
        repaired_solids=repaired_solids,
        used_solids=solids,
        faces=faces,
        used_volume=vol,
        used_surface=surf,
        thickness=thickness,
        cfg_max_thickness=cfg_max_thickness,
        effective_max_thickness=effective_max_thickness,
        repair_note=repair_note,
        repair_logs=repair_logs,
    )

    return {
        "B": b_value,
        "C": png_path,
        "I": float(vol),
        "N": round(thickness.nominal, 2),
        "N_max": round(thickness.p98, 2),
        "O": bbox_str,
        "P": float(projected_area),
        "V": sliders_lifters_info,
    }


# =========================================================================
# ANALISI STEP -> RIGHE EXCEL
# =========================================================================
def analyze_step_for_excel(step_path, cfg):
    """
    Analizza uno STEP e ritorna una lista di righe.
    Se lo STEP è assembly/multibody, può ritornare più righe.
    """
    step_path = str(step_path)

    imported = cq.importers.importStep(step_path)
    top_shapes = get_top_shapes(imported)

    if not top_shapes:
        raise ValueError("Nessuna geometria valida importata dallo STEP.")

    body_groups = extract_body_groups(top_shapes)

    rows = []

    for idx, group in enumerate(body_groups, start=1):
        data = analyze_shape_group(
            step_path=step_path,
            shapes=group,
            cfg=cfg,
            body_index=idx,
            body_count=len(body_groups),
            imported_for_image=imported if len(body_groups) == 1 else None,
        )
        rows.append(data)

    return rows


# =========================================================================
# MAIN
# =========================================================================
def main():
    # Mapping colonne CSV -> Excel
    SETTINGS_COLUMN_MAP = {
        "demand_average_yearly_volume": "D",
        "raw_material": "E",
        "color": "F",
        "surface_finishing": "G",
        "cavities": "H",
        "density": "K",
        "inserts_steel": "Q",
        "sliders_steel": "R",
        "gate_type": "W",
        "drop_number": "Y",
        "mold_lifetime": "AA",
        "cycle_time": "AM",
        "press": "AN",
    }

    # ---------------------------------------------------------------------
    # Caricamento settings.csv
    # ---------------------------------------------------------------------
    cfg_list = []

    if os.path.exists(PATH_SETTINGS):
        with open(PATH_SETTINGS, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)

            for row in reader:
                # Salta righe completamente vuote
                if all(v is None or str(v).strip() == "" for v in row.values()):
                    continue

                # Pulizia sicura: gestisce None sia in chiavi che in valori
                cleaned_row = {}

                for k, v in row.items():
                    clean_key = k.strip() if isinstance(k, str) else ""

                    if not clean_key:
                        continue

                    if isinstance(v, str):
                        clean_val = v.strip()
                    elif v is None:
                        clean_val = ""
                    else:
                        clean_val = v

                    cleaned_row[clean_key] = clean_val

                if cleaned_row:
                    cfg_list.append(cleaned_row)

        logger.info(f"Caricati {len(cfg_list)} record da settings.csv")
    else:
        logger.warning("Attenzione: settings.csv non trovato.")
        cfg_list = []

    project = cfg_get(cfg_list[0], "project", "Default_Project") if cfg_list else "Default_Project"
    project_safe = sanitize_filename(project)

    # ---------------------------------------------------------------------
    # Template Excel
    # ---------------------------------------------------------------------
    # Il form viene SEMPRE generato dal template: se manca ci si ferma,
    # invece di produrre un foglio bianco con i dati incollati.
    template_path = find_template_path()
    if template_path is None:
        logger.error(
            f"Template non trovato in {BASE_DIR}. "
            f"Atteso uno di: {', '.join(TEMPLATE_CANDIDATES)}"
        )
        return

    try:
        wb, out_ext = open_template(template_path)
    except Exception as e:
        logger.error(f"Errore apertura template {template_path}: {e}")
        traceback.print_exc()
        return

    target_excel_path = os.path.join(BASE_DIR, f"{project_safe}{out_ext}")
    if os.path.abspath(target_excel_path) == os.path.abspath(template_path):
        target_excel_path = os.path.join(BASE_DIR, f"{project_safe}_RFQ{out_ext}")

    if TEMPLATE_SHEET_NAME:
        if TEMPLATE_SHEET_NAME not in wb.sheetnames:
            logger.error(
                f"Foglio '{TEMPLATE_SHEET_NAME}' non presente nel template. "
                f"Fogli disponibili: {wb.sheetnames}"
            )
            return
        ws = wb[TEMPLATE_SHEET_NAME]
    else:
        ws = wb.active

    logger.info(f"Template: {template_path} | foglio: '{ws.title}'")

    os.makedirs(OUTPUT_DIR_VISTE, exist_ok=True)

    # Data odierna
    ws[DATE_CELL] = datetime.now().strftime("%Y-%m-%d")

    # ---------------------------------------------------------------------
    # Ricerca file STEP
    # ---------------------------------------------------------------------
    if os.path.exists(PATH_INPUT):
        step_files = sorted([
            os.path.join(PATH_INPUT, f)
            for f in os.listdir(PATH_INPUT)
            if f.lower().endswith((".step", ".stp"))
        ])
    else:
        os.makedirs(PATH_INPUT, exist_ok=True)
        step_files = []

    logger.info(f"Avvio elaborazione: {len(step_files)} file STEP.")

    processed_rows = 0
    excel_row = DATA_START_ROW

    # ---------------------------------------------------------------------
    # Elaborazione STEP
    # ---------------------------------------------------------------------
    for step_file in step_files:
        try:
            cfg = find_cfg_for_step(step_file, cfg_list)

            # Salta se non c'è configurazione valida
            if not cfg:
                logger.info(f"SKIP '{os.path.basename(step_file)}'")
                continue

            # Analisi geometrica: può produrre più righe se multibody
            rows = analyze_step_for_excel(step_file, cfg)

            if not rows:
                logger.warning(f"Nessuna riga generata per '{os.path.basename(step_file)}'")
                continue

            for data in rows:
                # Inserimento riga Excel
                if excel_row > DATA_START_ROW:
                    insert_row_keep_layout(ws, excel_row)
                    copy_row_formatting(ws, DATA_START_ROW, excel_row)

                # Dati CSV
                for key, col in SETTINGS_COLUMN_MAP.items():
                    cell_ref = f"{col}{excel_row}"
                    val = cfg_get(cfg, key, "")
                    ws[cell_ref] = val

                # Logica Cell Z: Valutazione pressa (tonnellaggio)
                press_text = str(cfg_get(cfg, "press", ""))
                tons_list = [int(n) for n in re.findall(r"\d+", press_text)]
                max_tons = max(tons_list) if tons_list else 0

                ws[f"Z{excel_row}"] = "hydraulic" if max_tons > 550 else "mechanic"

                # Dati analisi geometrica
                mapping = {
                    "B": "B",
                    "I": "I",
                    "N": "N",
                    "O": "O",
                    "P": "P",
                    "V": "V",
                }

                if THICKNESS_MAX_COLUMN:
                    mapping["N_max"] = THICKNESS_MAX_COLUMN

                for key, col in mapping.items():
                    ws[f"{col}{excel_row}"] = data.get(key, "")

                # Immagine in colonna C
                img_path = data.get("C")

                if img_path and os.path.exists(img_path):
                    try:
                        img = XLImage(img_path)
                        img.width = 100
                        img.height = 75
                        ws.add_image(img, f"C{excel_row}")
                        ws.row_dimensions[excel_row].height = 60
                    except Exception as img_exc:
                        logger.warning(f"Errore inserimento immagine: {img_exc}")

                processed_rows += 1

                logger.info(
                    f"Riga {excel_row}: {data['B']} | "
                    f"Press: {max_tons}t -> {ws[f'Z{excel_row}'].value} | "
                    f"Sliders: {data['V']}"
                )

                excel_row += 1

        except Exception as e:
            logger.error(f"Errore su {step_file}: {e}")
            traceback.print_exc()
            continue

    # ---------------------------------------------------------------------
    # Pulizia SVG residui
    # ---------------------------------------------------------------------
    for f in os.listdir(OUTPUT_DIR_VISTE):
        if f.lower().endswith(".svg"):
            try:
                os.remove(os.path.join(OUTPUT_DIR_VISTE, f))
            except Exception:
                pass

    # ---------------------------------------------------------------------
    # Salvataggio Excel
    # ---------------------------------------------------------------------
    try:
        wb.save(target_excel_path)
        logger.info(f"Completato: {processed_rows} righe generate.")
        logger.info(f"Salvato in: {target_excel_path}")
    except Exception as e:
        logger.error(f"Errore salvataggio: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()