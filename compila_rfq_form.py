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
- spessore medio più robusto
- soglia massima spessore da CSV
- log su file
- analisi separata per singolo corpo se assembly/multibody
"""

import os
import re
import copy
import shutil
import csv
import traceback
import math
import logging
from datetime import datetime
from types import SimpleNamespace
from statistics import mean, median

import openpyxl
from openpyxl.drawing.image import Image as XLImage

import cadquery as cq

from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.TopAbs import (
    TopAbs_SOLID,
    TopAbs_COMPSOLID,
    TopAbs_COMPOUND,
    TopAbs_SHELL,
    TopAbs_FACE,
    TopAbs_IN,
    TopAbs_OUT,
)
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib

from OCP.ShapeFix import ShapeFix_Shape
from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing, BRepBuilderAPI_MakeSolid
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.gp import gp_Pnt

try:
    import cairosvg
except Exception:
    cairosvg = None


# =========================================================================
# PERCORSI
# =========================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PATH_SETTINGS = os.path.join(BASE_DIR, "settings.csv")
PATH_TEMPLATE = os.path.join(BASE_DIR, "RFQ_mods_form.xlsx")
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

# Parametri campionamento spessore locale.
THICKNESS_TOL = 1e-4
THICKNESS_MAX_SAMPLES = 48

# Se True, gli assembly/multibody vengono analizzati corpo per corpo.
PROCESS_ASSEMBLY_BODIES_SEPARATELY = True


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


def occ_signature(wrapped):
    """
    Firma geometrica semplice per ridurre duplicati.
    Usa volume + baricentro. Se non riesce, usa id().
    """
    if wrapped is None:
        return None

    try:
        props = GProp_GProps()
        BRepGProp.VolumeProperties(wrapped, props)
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


def safe_volume(shape, tol=1e-5):
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

    props = GProp_GProps()

    try:
        if tol:
            BRepGProp.VolumeProperties(wrapped, props, tol)
        else:
            BRepGProp.VolumeProperties(wrapped, props)
        return abs(float(props.Mass()))
    except TypeError:
        try:
            BRepGProp.VolumeProperties(wrapped, props)
            return abs(float(props.Mass()))
        except Exception:
            return 0.0
    except Exception:
        return 0.0


def safe_surface_area(shape, tol=1e-5):
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

    props = GProp_GProps()

    try:
        if tol:
            BRepGProp.SurfaceProperties(wrapped, props, tol)
        else:
            BRepGProp.SurfaceProperties(wrapped, props)
        return abs(float(props.Mass()))
    except TypeError:
        try:
            BRepGProp.SurfaceProperties(wrapped, props)
            return abs(float(props.Mass()))
        except Exception:
            return 0.0
    except Exception:
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
    thickness_value,
    thickness_method,
    thickness_samples_count,
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
    lines.append(f"Volume usato: {used_volume}")
    lines.append(f"Superficie usata: {used_surface}")
    lines.append(f"Spessore calcolato: {thickness_value}")
    lines.append(f"Metodo spessore: {thickness_method}")
    lines.append(f"Campioni spessore validi: {thickness_samples_count}")
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

    logger.info(text)

    if DEBUG_GEOMETRY:
        print(text)


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
# SPESSORE MEDIO PIÙ ROBUSTO
# =========================================================================
def make_solid_classifier(wrapped):
    """
    Crea un classificatore punto-dentro/solido.
    """
    try:
        return BRepClass3d_SolidClassifier(wrapped)
    except Exception:
        classifier = BRepClass3d_SolidClassifier()
        classifier.Load(wrapped)
        return classifier


def _classify_point(classifier, x, y, z, tol=THICKNESS_TOL):
    try:
        return classifier.Perform(gp_Pnt(x, y, z), tol)
    except Exception:
        return None


def _state_at_offset(classifier, base, direction, dist, tol=THICKNESS_TOL):
    x = base[0] + direction[0] * dist
    y = base[1] + direction[1] * dist
    z = base[2] + direction[2] * dist
    return _classify_point(classifier, x, y, z, tol)


def _find_inside_direction(classifier, base, normal, tol=THICKNESS_TOL):
    """
    Prova a capire quale direzione della normale entra nel solido.
    Ritorna un vettore direzione oppure None.
    """
    offsets = [tol * 10.0, tol * 100.0, 0.01, 0.1, 1.0]

    for sign in (-1.0, 1.0):
        direction = (
            normal[0] * sign,
            normal[1] * sign,
            normal[2] * sign,
        )

        for off in offsets:
            state = _state_at_offset(classifier, base, direction, off, tol)
            if state == TopAbs_IN:
                return direction

    return None


def measure_local_thicknesses(
    solid,
    max_samples=THICKNESS_MAX_SAMPLES,
    max_thickness=None,
    tol=THICKNESS_TOL,
):
    """
    Misura uno spessore locale campionando alcune facce.

    Metodo:
    - prende una faccia
    - prova a entrare nel solido lungo la normale
    - cerca il punto in cui riesce dall'altra parte
    - stima la distanza come spessore locale

    Ritorna una lista di spessori validi.
    """
    wrapped = wrap_occ(solid)
    if wrapped is None:
        return []

    try:
        st = wrapped.ShapeType()
    except Exception:
        return []

    if st not in (TopAbs_SOLID, TopAbs_COMPSOLID):
        return []

    if max_thickness is None:
        max_thickness = max(50.0, bbox_diagonal([solid]))

    try:
        classifier = make_solid_classifier(wrapped)
    except Exception:
        return []

    faces = collect_faces([solid])
    if not faces:
        return []

    try:
        faces = sorted(faces, key=lambda f: safe_face_area(f), reverse=True)
    except Exception:
        pass

    samples = []
    attempts = 0
    max_attempts = max_samples * 4

    for face in faces:
        if len(samples) >= max_samples:
            break

        if attempts >= max_attempts:
            break

        attempts += 1

        try:
            c = face.Center()
            n = face.normalAt()

            base = (float(c.x), float(c.y), float(c.z))
            normal = (float(n.x), float(n.y), float(n.z))
        except Exception:
            continue

        norm_len = math.sqrt(normal[0] ** 2 + normal[1] ** 2 + normal[2] ** 2)
        if norm_len < 1e-9:
            continue

        normal = (
            normal[0] / norm_len,
            normal[1] / norm_len,
            normal[2] / norm_len,
        )

        inside_dir = _find_inside_direction(classifier, base, normal, tol)
        if inside_dir is None:
            continue

        # Trova un punto sicuramente dentro il materiale
        lo_candidates = [max(tol * 10.0, 1e-4), 0.001, 0.01, 0.1, 1.0]
        lo = None

        for off in lo_candidates:
            state = _state_at_offset(classifier, base, inside_dir, off, tol)
            if state == TopAbs_IN:
                lo = off
                break

        if lo is None:
            continue

        # Espandi finché non riesci dal materiale
        hi = lo * 2.0
        hi_ok = False

        while hi <= max_thickness:
            state = _state_at_offset(classifier, base, inside_dir, hi, tol)

            if state is None:
                break

            if state == TopAbs_OUT:
                hi_ok = True
                break

            hi *= 2.0

        if not hi_ok:
            state = _state_at_offset(
                classifier,
                base,
                inside_dir,
                max_thickness,
                tol
            )
            if state == TopAbs_OUT:
                hi = max_thickness
                hi_ok = True

        if not hi_ok:
            continue

        # Binary search della superficie di uscita
        a = lo
        b = hi
        valid = True

        for _ in range(20):
            m = (a + b) * 0.5
            state = _state_at_offset(classifier, base, inside_dir, m, tol)

            if state is None:
                valid = False
                break

            if state == TopAbs_OUT:
                b = m
            else:
                a = m

        if valid and b > 1e-6 and b <= max_thickness:
            samples.append(float(b))

    return samples


def compute_average_thickness(
    solids,
    fallback_volume,
    fallback_surface,
    max_thickness=None,
):
    """
    Calcola una media spessori più robusta.

    Priorità:
    1. campionamento locale su solidi
    2. fallback globale 2V/S

    Ritorna:
    valore, metodo, numero campioni validi
    """
    samples = []

    if solids:
        if max_thickness is None:
            max_thickness = max(50.0, bbox_diagonal(solids))

        per_solid = max(4, THICKNESS_MAX_SAMPLES // max(1, len(solids)))

        for s in solids:
            try:
                samples.extend(
                    measure_local_thicknesses(
                        s,
                        max_samples=per_solid,
                        max_thickness=max_thickness,
                    )
                )
            except Exception:
                pass

    if samples:
        med = median(samples)

        if med > 0:
            # Filtro anti-outlier semplice
            filtered = [
                x for x in samples
                if (0.15 * med) <= x <= (6.0 * med)
            ]

            if not filtered:
                filtered = samples

            try:
                value = float(mean(filtered))
            except Exception:
                value = float(median(filtered))

            return value, "local_sampled", len(filtered)

    value = (2.0 * fallback_volume / fallback_surface) if fallback_surface > 0 else 0.0
    return float(value), "global_estimate", 0


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

    # Repair multi-tolleranza
    repaired_solids, repair_logs = try_repair_to_solids_multi(shapes)

    original_volume = sum(safe_volume(s) for s in original_solids)
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

    # Spessore medio più robusto
    ratio_thick, thickness_method, thickness_samples_count = compute_average_thickness(
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
        thickness_value=ratio_thick,
        thickness_method=thickness_method,
        thickness_samples_count=thickness_samples_count,
        cfg_max_thickness=cfg_max_thickness,
        effective_max_thickness=effective_max_thickness,
        repair_note=repair_note,
        repair_logs=repair_logs,
    )

    return {
        "B": b_value,
        "C": png_path,
        "I": float(vol),
        "N": float(ratio_thick),
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
    target_excel_path = os.path.join(BASE_DIR, f"{project_safe}.xlsx")

    # ---------------------------------------------------------------------
    # Gestione progetto e file Excel
    # ---------------------------------------------------------------------
    os.makedirs(OUTPUT_DIR_VISTE, exist_ok=True)

    if os.path.exists(PATH_TEMPLATE):
        shutil.copy(PATH_TEMPLATE, target_excel_path)
        logger.info(f"Template copiato in: {target_excel_path}")
    elif not os.path.exists(target_excel_path):
        wb_new = openpyxl.Workbook()
        wb_new.save(target_excel_path)
        logger.info(f"Creato nuovo file Excel: {target_excel_path}")

    try:
        wb = openpyxl.load_workbook(target_excel_path)
        ws = wb.active

        if ws is None:
            ws = wb.create_sheet(project_safe[:31])
            ws.title = project_safe[:31]

    except Exception as e:
        logger.error(f"Errore caricamento {target_excel_path}: {e}. Creo un nuovo file.")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = project_safe[:31]

    # Data odierna in E5
    ws["E5"] = datetime.now().strftime("%Y-%m-%d")

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
    excel_row = 3

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
                if excel_row > 3:
                    ws.insert_rows(excel_row)
                    copy_row_formatting(ws, 3, excel_row)

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