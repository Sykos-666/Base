# -*- coding: utf-8 -*-
"""
Compilazione automatica form RFQ stampi (RFQ_molds_form.xlsx).

Per ogni file STEP in input/ con una riga corrispondente in settings.csv
(colonna 'cad_filename', abbinamento in rfq_common.py, verificato prima
da diagnostica.py):
- volume e ingombro (bounding box), con repair di shell/superfici aperte
- spessore pareti con ray-casting (nominale = mediana sulla superficie)
- direzione di apertura stampo (analisi sottosquadri) e area proiettata
- vista isometrica

Gli assembly/multibody generano una riga per corpo.
Output in output/: Excel compilato, viste, log completo (analisi.log).
In console solo l'avanzamento.

Di norma si avvia con avvia_rfq.py (o l'eseguibile AVVIA_RFQ), che lancia
prima la diagnostica.
"""

import os
import re
import sys
import time
import copy
import math
import random
import logging
import tempfile
import contextlib
import xml.etree.ElementTree as ET
from datetime import datetime
from types import SimpleNamespace
from statistics import mean, median

import openpyxl
from openpyxl.drawing.image import Image as XLImage
from openpyxl.worksheet.cell_range import CellRange
from openpyxl.formula.translate import Translator
from openpyxl.utils import get_column_letter
from PIL import Image, ImageDraw, ImageOps

from shapely.geometry import Polygon
from shapely.ops import unary_union

import cadquery as cq

from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_SOLID, TopAbs_COMPSOLID, TopAbs_SHELL, TopAbs_FACE
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.ShapeFix import ShapeFix_Shape
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_Sewing,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_MakeVertex,
)
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Compound
from OCP.BRep import BRep_Tool, BRep_Builder
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.TopLoc import TopLoc_Location
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.gp import gp_Pnt, gp_Vec, gp_Dir, gp_Lin
from cadquery.occ_impl.exporters.svg import getSVG

from rfq_common import (
    OUTPUT_DIR,
    PATH_INPUT,
    PATH_TEMPLATE,
    cfg_get,
    find_cfg_for_step,
    list_step_files,
    load_settings,
)


# =========================================================================
# PERCORSI
# =========================================================================
OUTPUT_DIR_VISTE = os.path.join(OUTPUT_DIR, "viste")
LOG_FILE = os.path.join(OUTPUT_DIR, "analisi.log")


# =========================================================================
# CONFIGURAZIONE
# =========================================================================
# Repair di shell/superfici aperte: tolleranze dalla più fine alla più
# aggressiva (si tiene il risultato con volume maggiore).
REPAIR_TOLERANCES = [1e-5, 5e-5, 1e-4, 5e-4, 1e-3]

# Assembly/multibody analizzati corpo per corpo (una riga per corpo).
PROCESS_ASSEMBLY_BODIES_SEPARATELY = True

# Spessore massimo misurabile se il CSV non ha 'max_thickness_mm'.
DEFAULT_MAX_THICKNESS_MM = 150.0

# Spessore (ray-casting): raggi per corpo, da punti distribuiti
# uniformemente sulla superficie.
THICKNESS_RAY_COUNT = 3000
# Angolo massimo tra il raggio e la normale della parete d'uscita: oltre,
# il raggio ha colpito una parete laterale e il campione è scartato.
THICKNESS_MAX_EXIT_ANGLE_DEG = 45.0
# Distanza minima per ignorare l'intersezione con la faccia di partenza.
THICKNESS_MIN_MM = 1e-3
# Diametro sfera inscritta / lunghezza raggio sotto questa soglia:
# il raggio non ha attraversato una parete e viene scartato.
THICKNESS_SPHERE_RATIO = 0.5
# Seme fisso: risultati ripetibili a ogni esecuzione.
RANDOM_SEED = 12345
# Classi dell'istogramma spessori nel log (mm).
THICKNESS_HISTOGRAM_EDGES = [
    0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0, 15.0, 20.0,
    float("inf"),
]

# Direzione di apertura: punti campionati per l'analisi sottosquadri.
PARTING_RAY_COUNT = 2000
# Facce entro questo angolo dalla direzione di apertura sono pareti
# parallele all'apertura (senza sformo), non sottosquadri.
PARTING_NEUTRAL_DEG = 0.5
# Differenza di area in sottosquadro (frazione della superficie) entro cui
# due direzioni sono considerate equivalenti.
PARTING_TIE_REL = 0.005
# Deflessione mesh per l'area proiettata, relativa alla diagonale bbox
# e limitata a [0.005, 0.05] mm.
PROJECTION_MESH_TOL_REL = 2e-4

# Vista isometrica: tela con le proporzioni della cella "Part Picture",
# senza assi e linee nascoste (illeggibili a quella dimensione).
ISO_VIEW_SUPERSAMPLING = 3
ISO_VIEW_OPTIONS = {
    "width": 480,
    "height": 300,
    "marginLeft": 20,
    "marginTop": 20,
    "showAxes": False,
    "showHidden": False,
    "projectionDir": (1, -1, 0.75),
}

AXIS_DIRECTIONS = {
    "X": (1.0, 0.0, 0.0),
    "Y": (0.0, 1.0, 0.0),
    "Z": (0.0, 0.0, 1.0),
}


# =========================================================================
# CONFIGURAZIONE FORM EXCEL
# =========================================================================
# Riga dati del template: viene replicata per ogni pezzo (stili, formule,
# "please fill"); i totali sotto la tabella (es. SUM(AQ3:AQ3)) vengono
# estesi a tutte le righe.
DATA_START_ROW = 3
ITEM_NO_COLUMN = "A"
DATE_CELL = "E5"

# Colonne settings.csv -> colonne Excel.
SETTINGS_COLUMN_MAP = {
    "demand_average_yearly_volume": "D",
    "raw_material": "E",
    "color": "F",
    "surface_finishing": "G",
    "cavities": "H",
    "density": "K",
    "inserts_steel": "Q",
    "sliders_steel": "R",
    "slider+lifter_Type/Qty": "V",
    "gate_type": "W",
    "drop_number": "Y",
    "mold_lifetime": "AA",
    "cycle_time": "AM",
    "press": "AN",
}

# Colonne del CSV scritte come numeri, perché usate nelle formule del
# template (es. peso L = I/1000*K). "decimal" accetta la virgola decimale.
CSV_NUMERIC_KEYS = {
    "density": "decimal",
    "cycle_time": "decimal",
    "demand_average_yearly_volume": "integer",
    "cavities": "integer",
    "drop_number": "integer",
    "mold_lifetime": "integer",
}

# Colonna Excel per lo spessore massimo (P98), es. "M". None = non scritto.
THICKNESS_MAX_COLUMN = None


# =========================================================================
# LOG E CONSOLE
# =========================================================================
# Nel log va tutto (report geometrici, avvisi, traceback, warning Python,
# messaggi nativi di OpenCASCADE); in console solo l'avanzamento, che
# viene comunque ripetuto nel log.
logger = logging.getLogger("analisi_rfq")
logger.setLevel(logging.INFO)
logger.propagate = False


def setup_logging():
    if logger.handlers:
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(handler)

    logging.captureWarnings(True)
    logging.getLogger("py.warnings").addHandler(handler)


def console(message):
    """Messaggio in console e nel log."""
    print(message, flush=True)
    logger.info(message)


def fatal(message):
    """Errore che blocca l'esecuzione."""
    logger.error(message)
    print(f"ERRORE: {message}\nDettagli nel log: {LOG_FILE}", flush=True)


@contextlib.contextmanager
def capture_native_output():
    """
    Cattura i messaggi che OpenCASCADE (C++) scrive direttamente su
    stdout/stderr, ad es. errori di lettura STEP, e li scrive nel log.
    """
    saved = []
    tmp = tempfile.TemporaryFile()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        for fd in (1, 2):
            saved.append((fd, os.dup(fd)))
            os.dup2(tmp.fileno(), fd)
    except Exception:
        # Console non standard (alcuni IDE): nessuna cattura.
        pass

    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        for fd, copy_fd in saved:
            os.dup2(copy_fd, fd)
            os.close(copy_fd)

        tmp.seek(0)
        text = re.sub(r"\x1b\[[0-9;]*m", "", tmp.read().decode("utf-8", "replace")).strip()
        tmp.close()
        if text:
            logger.warning(f"Messaggi OpenCASCADE:\n{text}")


# =========================================================================
# VALORI DA SETTINGS.CSV
# =========================================================================
def parse_csv_number(value, kind):
    """
    Testo numerico del CSV -> numero (None se non è un numero).

    "decimal": '1,05', '1.05', '1,05 g/cm3' -> 1.05; con entrambi i
               separatori l'ultimo è il decimale ('1.234,5' -> 1234.5).
    "integer": '100.000', '100,000', '100 000 pz' -> 100000.
    """
    if isinstance(value, (int, float)):
        return value

    m = re.search(r"-?\d[\d.,\s]*", str(value or ""))
    if not m:
        return None

    txt = re.sub(r"\s+", "", m.group(0)).rstrip(".,")

    if kind == "integer":
        digits = re.sub(r"[.,]", "", txt)
        return int(digits) if digits.lstrip("-").isdigit() else None

    if "," in txt and "." in txt:
        dec = "," if txt.rfind(",") > txt.rfind(".") else "."
        txt = txt.replace("." if dec == "," else ",", "").replace(dec, ".")
    else:
        txt = txt.replace(",", ".")

    try:
        return float(txt)
    except ValueError:
        return None


def excel_value(key, value):
    """
    Valore da scrivere nel form per una colonna del CSV. Le colonne
    numeriche vanno scritte come numeri: nel file .xlsx il numero non ha
    separatore ed Excel in italiano lo mostra con la virgola (1,05);
    il testo "1,05" darebbe invece #VALORE! nelle formule.
    """
    kind = CSV_NUMERIC_KEYS.get(key)
    if kind is None:
        return value

    number = parse_csv_number(value, kind)
    if number is None:
        logger.warning(f"Valore non numerico per '{key}': '{value}' (scritto come testo)")
        return value
    return number


def get_max_thickness_from_cfg(cfg):
    for key in ("max_thickness_mm", "thickness_max_mm", "spessore_max_mm"):
        val = parse_csv_number(cfg_get(cfg, key), "decimal")
        if val is not None and val > 0:
            return float(val)
    return DEFAULT_MAX_THICKNESS_MM


# =========================================================================
# GEOMETRIA BASE
# =========================================================================
def occ(shape):
    """Oggetto OCCT sottostante a una shape CadQuery."""
    return getattr(shape, "wrapped", shape)


def signed_volume(wrapped):
    """Volume con segno: negativo se il solido è orientato al contrario."""
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(wrapped, props)
    return float(props.Mass())


def volume(shape):
    try:
        return abs(signed_volume(occ(shape)))
    except Exception:
        return 0.0


def surface_area(shape):
    try:
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(occ(shape), props)
        return abs(float(props.Mass()))
    except Exception:
        return 0.0


def combine_shapes(shapes):
    return shapes[0] if len(shapes) == 1 else cq.Compound.makeCompound(shapes)


def collect_solids(shapes):
    return [s for sh in shapes for s in sh.Solids()]


def explore_shapes_by_type(wrapped, shape_type):
    result = []
    exp = TopExp_Explorer(wrapped, shape_type)
    while exp.More():
        result.append(exp.Current())
        exp.Next()
    return result


def bounding_box(shapes):
    """Ingombro (SimpleNamespace con min/max e xlen, ylen, zlen) o None."""
    bbox = Bnd_Box()
    for sh in shapes:
        BRepBndLib.Add_s(occ(sh), bbox, False)

    if bbox.IsVoid():
        return None

    xmin, ymin, zmin, xmax, ymax, zmax = bbox.Get()
    return SimpleNamespace(
        xmin=xmin, ymin=ymin, zmin=zmin,
        xmax=xmax, ymax=ymax, zmax=zmax,
        xlen=xmax - xmin, ylen=ymax - ymin, zlen=zmax - zmin,
    )


def bbox_diagonal(shapes):
    bb = bounding_box(shapes)
    if bb is None:
        return 100.0
    return math.sqrt(bb.xlen ** 2 + bb.ylen ** 2 + bb.zlen ** 2)


# =========================================================================
# REPAIR GEOMETRICO (STEP con superfici/shell invece di solidi)
# =========================================================================
def fix_occ_shape(wrapped, tol):
    try:
        fix = ShapeFix_Shape(wrapped)
        fix.SetMaxTolerance(tol)
        fix.Perform()
        out = fix.Shape()
        return wrapped if out.IsNull() else out
    except Exception:
        return wrapped


def make_solid_from_shell(shell, tol):
    """Shell (riparata) -> solido, oppure None."""
    for sh in explore_shapes_by_type(fix_occ_shape(shell, tol), TopAbs_SHELL):
        try:
            mk = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(sh))
            if mk.IsDone():
                return fix_occ_shape(mk.Solid(), tol)
        except Exception:
            continue
    return None


def try_repair_to_solids(shapes, tol):
    """
    Solidi validi dalle shape importate:
    ShapeFix -> solidi già presenti -> sewing -> shell cucite -> solidi.
    """
    solids = []
    seen = set()

    def add(sol):
        if sol is None or sol.IsNull():
            return
        vol = volume(sol)
        if vol <= 0.0:
            return
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(sol, props)
        c = props.CentreOfMass()
        sig = (round(vol, 6), round(c.X(), 3), round(c.Y(), 3), round(c.Z(), 3))
        if sig not in seen:
            seen.add(sig)
            solids.append(cq.Shape.cast(sol))

    def add_solids_in(wrapped):
        for kind in (TopAbs_SOLID, TopAbs_COMPSOLID):
            for sol in explore_shapes_by_type(wrapped, kind):
                add(sol)

    fixed = [fix_occ_shape(occ(s), tol) for s in shapes]
    for w in fixed:
        add_solids_in(w)
    if solids:
        return solids

    sewing = BRepBuilderAPI_Sewing(tol)
    for w in fixed:
        sewing.Add(w)
    sewing.Perform()
    sewed = sewing.SewedShape()

    if not sewed.IsNull():
        add_solids_in(sewed)
        if solids:
            return solids
        for shell in explore_shapes_by_type(sewed, TopAbs_SHELL):
            add(make_solid_from_shell(shell, tol))
        if solids:
            return solids

    for w in fixed:
        for shell in explore_shapes_by_type(w, TopAbs_SHELL):
            add(make_solid_from_shell(shell, tol))
        if solids:
            break

    return solids


def repair_to_solids(shapes):
    """Repair con tutte le tolleranze; tiene il risultato con volume maggiore."""
    best, best_volume, logs = [], 0.0, []

    for tol in REPAIR_TOLERANCES:
        try:
            solids = try_repair_to_solids(shapes, tol)
            vol = sum(volume(s) for s in solids)
            logs.append(f"tol={tol:.0e} -> solidi={len(solids)}, volume={vol:.6f}")
            if vol > best_volume:
                best, best_volume = solids, vol
        except Exception as e:
            logs.append(f"tol={tol:.0e} -> errore: {e}")

    return best, logs


# =========================================================================
# CAMPIONAMENTO SUPERFICIE E RAGGI
# =========================================================================
def _vec_norm(x, y, z):
    length = math.sqrt(x * x + y * y + z * z)
    if length < 1e-12:
        return None
    return (x / length, y / length, z / length)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def face_normal_at(evaluator, u, v):
    """
    Punto e normale unitaria di una faccia in (u, v). BRepGProp_Face tiene
    conto dell'orientamento della faccia nel solido (normale uscente).
    """
    pnt = gp_Pnt()
    nrm = gp_Vec()
    evaluator.Normal(u, v, pnt, nrm)

    n = _vec_norm(nrm.X(), nrm.Y(), nrm.Z())
    if n is None:
        return None, None
    return (pnt.X(), pnt.Y(), pnt.Z()), n


def sample_surface_points(solid_occ, count, rng, mesh_tol):
    """
    'count' punti distribuiti uniformemente sulla superficie, come
    (punto, normale). La triangolazione serve solo a scegliere DOVE
    campionare (triangoli pesati sull'area): punto e normale sono valutati
    sulla superficie esatta dalle coordinate UV interpolate.
    """
    BRepMesh_IncrementalMesh(solid_occ, mesh_tol, False, 0.5, True)

    triangles = []
    weights = []

    for face_shape in explore_shapes_by_type(solid_occ, TopAbs_FACE):
        face = TopoDS.Face_s(face_shape)
        poly = BRep_Tool.Triangulation_s(face, TopLoc_Location())
        if poly is None or not poly.HasUVNodes():
            continue

        evaluator = BRepGProp_Face(face)

        for i in range(1, poly.NbTriangles() + 1):
            n1, n2, n3 = poly.Triangle(i).Get()
            p1, p2, p3 = poly.Node(n1), poly.Node(n2), poly.Node(n3)

            a = (p2.X() - p1.X(), p2.Y() - p1.Y(), p2.Z() - p1.Z())
            b = (p3.X() - p1.X(), p3.Y() - p1.Y(), p3.Z() - p1.Z())
            area = 0.5 * math.sqrt(sum(c * c for c in _cross(a, b)))
            if area <= 1e-12:
                continue

            uvs = [poly.UVNode(k) for k in (n1, n2, n3)]
            triangles.append((evaluator, [(uv.X(), uv.Y()) for uv in uvs]))
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


def make_intersector(wrapped):
    intersector = IntCurvesFace_ShapeIntersector()
    intersector.Load(wrapped, 1e-6)
    return intersector


# =========================================================================
# SPESSORE PARETI (RAY-CASTING)
# =========================================================================
#
# 1. N punti distribuiti uniformemente sulla superficie (stessa area
#    ciascuno);
# 2. da ogni punto un raggio verso l'interno lungo la normale esatta;
# 3. la distanza dalla prima parete incontrata è lo spessore locale;
# 4. valido solo se la parete d'uscita è "opposta" (normale quasi
#    parallela al raggio): si scartano i raggi entrati in pareti laterali;
# 5. controllo "sfera inscritta" a metà raggio: se il diametro è molto più
#    piccolo del raggio, il raggio ha corso lungo una parete (es. partito
#    dal fianco stretto di una lastra) e il campione è scartato.
#
# La mediana dei campioni (pesati sull'area) è lo spessore nominale,
# poco sensibile a nervature, borchie e zone massive locali.

def make_shells_compound(solid_occ):
    """Shell del solido: per la distanza punto-superficie (con il solido
    un punto interno avrebbe distanza 0)."""
    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    for shell in explore_shapes_by_type(solid_occ, TopAbs_SHELL):
        builder.Add(comp, shell)
    return comp


def distance_to_surface(point, surface_occ):
    vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
    dist = BRepExtrema_DistShapeShape(vertex, surface_occ)
    return float(dist.Value()) if dist.IsDone() else None


def measure_wall_thickness(solid, max_thickness, ray_count):
    """Spessori locali validi di un solido e numero di raggi lanciati."""
    wrapped = occ(solid)
    vol = signed_volume(wrapped)
    if vol == 0.0:
        return [], 0

    # Solido orientato al contrario: normali verso l'interno.
    orientation = -1.0 if vol < 0 else 1.0

    mesh_tol = min(max(bbox_diagonal([solid]) * 2e-3, 0.01), 1.0)
    points = sample_surface_points(wrapped, ray_count, random.Random(RANDOM_SEED), mesh_tol)
    if not points:
        return [], 0

    intersector = make_intersector(wrapped)
    surface = make_shells_compound(wrapped)
    cos_limit = math.cos(math.radians(THICKNESS_MAX_EXIT_ANGLE_DEG))
    samples = []

    for p, n in points:
        d = (-n[0] * orientation, -n[1] * orientation, -n[2] * orientation)

        try:
            intersector.Perform(gp_Lin(gp_Pnt(*p), gp_Dir(*d)), THICKNESS_MIN_MM, max_thickness)
            if not intersector.IsDone() or intersector.NbPnt() == 0:
                continue

            hit = min(range(1, intersector.NbPnt() + 1), key=intersector.WParameter)
            dist = intersector.WParameter(hit)
            _, hit_n = face_normal_at(
                BRepGProp_Face(intersector.Face(hit)),
                intersector.UParameter(hit),
                intersector.VParameter(hit),
            )
        except Exception:
            continue

        # La parete d'uscita deve guardare nella stessa direzione del raggio.
        if hit_n is None or orientation * _dot(hit_n, d) < cos_limit:
            continue

        # Sfera inscritta centrata a metà raggio.
        mid = tuple(p[k] + d[k] * dist * 0.5 for k in range(3))
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
    pos = (len(sorted_values) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def thickness_histogram(values):
    """Percentuale di superficie per classe di spessore."""
    result = []
    edges = THICKNESS_HISTOGRAM_EDGES
    for lo, hi in zip(edges[:-1], edges[1:]):
        # Tolleranza: 3.0 misurato come 2.9999999 resta nella classe 3-4.
        cnt = sum(1 for x in values if lo <= x + 1e-6 < hi)
        if cnt:
            label = f">= {lo:g}" if math.isinf(hi) else f"{lo:g} - {hi:g}"
            result.append((label, 100.0 * cnt / len(values)))
    return result


def compute_wall_thickness(solids, fallback_volume, fallback_surface, max_thickness):
    """
    Statistiche di spessore: ray-casting sui solidi, altrimenti stima
    globale 2V/S (esatta solo per lastre sottili). I percentili sono sulla
    superficie: p98 = 98% della superficie ha spessore inferiore.
    """
    samples = []
    rays = 0

    areas = [surface_area(s) for s in solids]
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
            histogram=thickness_histogram(values),
        )

    value = 2.0 * fallback_volume / fallback_surface if fallback_surface > 0 else 0.0
    return SimpleNamespace(
        method="stima globale 2V/S",
        nominal=value, mean=value, p2=value, p10=value, p90=value, p98=value, max=value,
        samples=0, rays=rays, histogram=[],
    )


# =========================================================================
# DIREZIONE DI APERTURA E AREA PROIETTATA
# =========================================================================
#
# Area proiettata = "ombra" del pezzo sul piano perpendicolare alla
# direzione di apertura (area su cui agisce la pressione in cavità, quindi
# forza di chiusura): unione geometrica delle proiezioni dei triangoli
# della mesh rivolti verso l'apertura. Le zone sovrapposte contano una
# sola volta, i fori passanti restano esclusi.
#
# Direzione: per X, Y, Z e gli assi principali d'inerzia, da punti
# campionati sulla superficie si lancia un raggio nel verso in cui il punto
# si sforma (+d o -d secondo la normale); se colpisce di nuovo il pezzo il
# punto è in sottosquadro. Vince la direzione con meno sottosquadri; a
# pari merito (es. una lastra) quella con area proiettata maggiore.
# Si può forzare dal CSV con la colonna 'parting_direction' (X, Y o Z).

def plane_basis(d):
    """Due versori ortogonali tra loro e alla direzione d."""
    helper = (1.0, 0.0, 0.0) if abs(d[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = _vec_norm(*_cross(d, helper))
    return u, _cross(d, u)


def parse_parting_direction(cfg):
    """Direzione forzata dal CSV ('X', '>Z', '-y', ...) o None."""
    letters = [ch for ch in str(cfg_get(cfg, "parting_direction")).upper() if ch in AXIS_DIRECTIONS]
    return letters[0] if letters else None


def candidate_directions(solids):
    """Assi X, Y, Z più gli assi principali d'inerzia non già inclusi."""
    cands = list(AXIS_DIRECTIONS.items())

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(occ(combine_shapes(solids)), props)
    pp = props.PrincipalProperties()
    axes = [pp.FirstAxisOfInertia(), pp.SecondAxisOfInertia(), pp.ThirdAxisOfInertia()]

    for i, ax in enumerate(axes, start=1):
        vec = _vec_norm(ax.X(), ax.Y(), ax.Z())
        if vec is None or any(abs(_dot(vec, c)) > 0.999 for _, c in cands):
            continue
        cands.append((f"inerzia {i} ({vec[0]:+.2f}, {vec[1]:+.2f}, {vec[2]:+.2f})", vec))

    return cands


def shape_triangles(shape, mesh_tol, outward):
    """
    Triangoli della mesh in coordinate globali; con outward=True la
    normale (p2-p1)x(p3-p1) punta fuori dal solido.
    """
    # Senza Clean, tessellate() riuserebbe la mesh grossolana dello spessore.
    BRepTools.Clean_s(occ(shape))
    verts, tris = shape.tessellate(mesh_tol, 0.1)
    pts = [(p.x, p.y, p.z) for p in verts]

    flip = outward and signed_volume(occ(shape)) < 0
    return [
        (pts[a], pts[c], pts[b]) if flip else (pts[a], pts[b], pts[c])
        for a, b, c in tris
    ]


def silhouette_area(triangles, d, front_only):
    """Area netta della proiezione lungo d e area del contorno esterno
    (fori riempiti)."""
    u, v = plane_basis(d)
    polys = []

    for p1, p2, p3 in triangles:
        if front_only and _dot(_cross(_sub(p2, p1), _sub(p3, p1)), d) <= 0.0:
            continue
        poly = Polygon([(_dot(p, u), _dot(p, v)) for p in (p1, p2, p3)])
        if poly.area > 1e-9:
            polys.append(poly)

    if not polys:
        return 0.0, 0.0

    union = unary_union(polys).buffer(0)
    parts = getattr(union, "geoms", [union])
    contour = sum(Polygon(g.exterior).area for g in parts if hasattr(g, "exterior"))
    return float(union.area), float(contour)


def undercut_fraction(points, intersector, d, max_len):
    """Frazione di superficie in sottosquadro per apertura lungo ±d
    (points: (punto, normale uscente), ognuno della stessa area)."""
    sin_neutral = math.sin(math.radians(PARTING_NEUTRAL_DEG))
    neg_d = (-d[0], -d[1], -d[2])
    blocked = 0

    for p, n in points:
        c = _dot(n, d)
        if abs(c) < sin_neutral:
            continue
        try:
            intersector.Perform(
                gp_Lin(gp_Pnt(*p), gp_Dir(*(d if c > 0 else neg_d))), THICKNESS_MIN_MM, max_len
            )
        except Exception:
            continue
        if intersector.IsDone() and intersector.NbPnt() > 0:
            blocked += 1

    return blocked / len(points)


def analyze_parting_and_projection(solids, shapes, cfg):
    """Direzione di apertura scelta, area proiettata e dettaglio per
    direzione candidata."""
    body_shapes = solids or shapes
    combined_occ = occ(combine_shapes(body_shapes))
    diag = bbox_diagonal(body_shapes)
    mesh_tol = min(max(diag * PROJECTION_MESH_TOL_REL, 0.005), 0.05)

    intersector = make_intersector(combined_occ)

    # Senza solidi l'orientamento non è affidabile: si proiettano tutti i
    # triangoli (l'unione dà comunque la sagoma).
    triangles = [t for s in body_shapes for t in shape_triangles(s, mesh_tol, bool(solids))]

    # Punti per l'analisi sottosquadri, con normale uscente.
    points = []
    if solids:
        areas = [surface_area(s) for s in solids]
        total = sum(areas) or 1.0
        rng = random.Random(RANDOM_SEED)
        for s, area in zip(solids, areas):
            sign = -1.0 if signed_volume(occ(s)) < 0 else 1.0
            count = max(200, int(round(PARTING_RAY_COUNT * area / total)))
            for p, n in sample_surface_points(occ(s), count, rng, mesh_tol):
                points.append((p, (n[0] * sign, n[1] * sign, n[2] * sign)))

    forced = parse_parting_direction(cfg)
    if forced:
        cands = [(forced, AXIS_DIRECTIONS[forced])]
    elif solids:
        cands = candidate_directions(solids)
    else:
        cands = list(AXIS_DIRECTIONS.items())

    results = []
    for name, d in cands:
        proj, contour = silhouette_area(triangles, d, front_only=bool(solids))
        uc = undercut_fraction(points, intersector, d, 2.0 * diag + 1.0) if points else None
        results.append(SimpleNamespace(
            name=name,
            undercut_pct=None if uc is None else 100.0 * uc,
            projected_area=proj,
            contour_area=contour,
        ))

    if forced:
        best, source = results[0], "da CSV"
    elif points:
        best_uc = min(r.undercut_pct for r in results)
        tie = [r for r in results if r.undercut_pct <= best_uc + 100.0 * PARTING_TIE_REL]
        best = max(tie, key=lambda r: r.projected_area)
        source = "automatica, sottosquadri minimi"
    else:
        best = max(results, key=lambda r: r.projected_area)
        source = "automatica, nessun solido: area massima"

    return SimpleNamespace(
        direction=best.name,
        source=source,
        projected_area=best.projected_area,
        contour_area=best.contour_area,
        candidates=results,
    )


# =========================================================================
# VISTA ISOMETRICA
# =========================================================================
def export_iso_image(shapes, safe_name):
    """
    PNG isometrico in OUTPUT_DIR_VISTE, o None.

    Le linee vengono dalla proiezione SVG di CadQuery (solo segmenti M/L
    dentro un gruppo con trasformazione scale/translate) e sono disegnate
    con Pillow: niente cairosvg, che su Windows richiede le DLL di Cairo.
    """
    png_path = os.path.join(OUTPUT_DIR_VISTE, f"iso_{safe_name}.png")
    try:
        root = ET.fromstring(getSVG(combine_shapes(shapes), ISO_VIEW_OPTIONS))
        group = root.find("{http://www.w3.org/2000/svg}g")
        scale, tx, ty = (
            float(v) for v in re.findall(
                r"scale\(([-\d.e]+),.*translate\(([-\d.e]+),\s*([-\d.e]+)\)",
                group.get("transform"),
            )[0]
        )

        ss = ISO_VIEW_SUPERSAMPLING
        size = (int(float(root.get("width"))), int(float(root.get("height"))))
        img = Image.new("L", (size[0] * ss, size[1] * ss), 255)
        draw = ImageDraw.Draw(img)

        for path in group.iter("{http://www.w3.org/2000/svg}path"):
            for segment in re.split(r"(?=M)", path.get("d", "")):
                nums = [float(n) for n in re.findall(r"[-\d.e]+", segment)]
                pts = [
                    (scale * (x + tx) * ss, -scale * (y + ty) * ss)
                    for x, y in zip(nums[0::2], nums[1::2])
                ]
                if len(pts) > 1:
                    draw.line(pts, fill=0, width=ss)

        # Ritaglio sul disegno (CadQuery lo allinea in alto a sinistra).
        img = img.resize(size, Image.LANCZOS)
        left, top, right, bottom = ImageOps.invert(img).getbbox()
        pad = 6
        gray = img.convert("L").crop((left - pad, top - pad, right + pad, bottom + pad))

        # Linee nere su sfondo trasparente: i bordi della cella restano visibili.
        out = Image.new("RGBA", gray.size, "black")
        out.putalpha(ImageOps.invert(gray))
        out.save(png_path)
        return png_path
    except Exception as e:
        logger.warning(f"Impossibile generare la vista ISO: {e}")
        return None


# =========================================================================
# ANALISI STEP
# =========================================================================
def extract_body_groups(top_shapes):
    """Gruppi di shape da analizzare: uno per corpo se multibody."""
    if not PROCESS_ASSEMBLY_BODIES_SEPARATELY:
        return [top_shapes]

    groups = []
    for sh in top_shapes:
        solids = sh.Solids()
        groups.extend([[s] for s in solids] if len(solids) > 1 else [[sh]])

    return groups if len(groups) > 1 else [top_shapes]


def analyze_body(filename, shapes, cfg, body_index, body_count):
    """Analisi geometrica di un corpo: dati per la riga Excel."""
    safe_name = re.sub(r"[^\w\-]+", "_", os.path.splitext(filename)[0]).strip("_") or "part"
    if body_count > 1:
        safe_name += f"_body_{body_index}"

    # Solidi: originali, oppure riparati se lo STEP ha solo superfici/shell.
    solids = collect_solids(shapes)
    vol = sum(volume(s) for s in solids)

    if vol > 0.0:
        repair_note, repair_logs = "solidi originali", []
    else:
        repaired, repair_logs = repair_to_solids(shapes)
        if repaired:
            solids, vol = repaired, sum(volume(s) for s in repaired)
            repair_note = "solidi riparati"
        else:
            solids = []
            repair_note = "nessun solido valido (volume non calcolabile)"

    surf = sum(surface_area(s) for s in (solids or shapes))

    body_shapes = solids or shapes
    bb = bounding_box(body_shapes)
    bbox_str = f"{bb.xlen:.2f} x {bb.ylen:.2f} x {bb.zlen:.2f}" if bb else "0.00 x 0.00 x 0.00"

    cfg_max_thickness = get_max_thickness_from_cfg(cfg)
    max_thickness = min(cfg_max_thickness, max(1.0, bbox_diagonal(body_shapes)))

    thickness = compute_wall_thickness(solids, vol, surf, max_thickness)
    parting = analyze_parting_and_projection(solids, shapes, cfg)

    part_name = str(cfg_get(cfg, "part_name")).strip()
    body_suffix = f" [Body {body_index}/{body_count}]" if body_count > 1 else ""

    # Report nel log
    t = thickness
    lines = [
        "=" * 80,
        f"REPORT GEOMETRIA | {filename} | Body {body_index}/{body_count}",
        "=" * 80,
        f"Shape importate: {[sh.ShapeType() for sh in shapes]}",
        f"Solidi usati: {len(solids)} ({repair_note})",
        *[f"  repair {entry}" for entry in repair_logs],
        f"Volume: {vol:.3f} mm3",
        f"Superficie: {surf:.3f} mm2",
        f"Ingombro: {bbox_str} mm",
        f"Metodo spessore: {t.method} | raggi validi {t.samples}/{t.rays}",
        f"Spessore nominale (mediana): {t.nominal:.3f} mm",
        f"Spessore medio: {t.mean:.3f} mm",
        f"Spessore P2 / P10 / P90 / P98 / max: {t.p2:.3f} / {t.p10:.3f} / "
        f"{t.p90:.3f} / {t.p98:.3f} / {t.max:.3f} mm",
        *[f"  {label:>16} mm: {pct:5.1f}%" for label, pct in t.histogram],
        f"Spessore massimo misurabile: {max_thickness:.1f} mm (CSV/default {cfg_max_thickness:g})",
        f"Direzione apertura stampo: {parting.direction} ({parting.source})",
        f"Area proiettata: {parting.projected_area:.2f} mm2 "
        f"(contorno esterno, fori inclusi: {parting.contour_area:.2f} mm2)",
    ]
    for c in parting.candidates:
        uc = "n.d." if c.undercut_pct is None else f"{c.undercut_pct:5.1f}%"
        lines.append(
            f"  {c.name:<40} area proiettata {c.projected_area:12.2f} mm2 "
            f"| sottosquadri {uc} superficie"
        )
    if vol <= 0.0:
        lines.append(
            "WARNING: volume = 0: lo STEP contiene superfici non chiudibili "
            "in un solido (shell aperta, geometria non cucita o corrotta)"
        )
    logger.info("\n".join(lines))

    return {
        "B": f"{part_name} CAD data: {filename}{body_suffix}".strip(),
        "C": export_iso_image(shapes, safe_name),
        "I": float(vol),
        "N": round(thickness.nominal, 2),
        "N_max": round(thickness.p98, 2),
        "O": bbox_str,
        "P": round(parting.projected_area, 2),
    }


def analyze_step(step_path, cfg):
    """Righe Excel per uno STEP (più righe se multibody)."""
    filename = os.path.basename(step_path)
    imported = cq.importers.importStep(step_path)
    top_shapes = [v for v in imported.vals() if isinstance(v, cq.Shape) and not v.wrapped.IsNull()]

    if not top_shapes:
        raise ValueError("Nessuna geometria valida importata dallo STEP.")

    groups = extract_body_groups(top_shapes)
    return [
        analyze_body(filename, group, cfg, idx, len(groups))
        for idx, group in enumerate(groups, start=1)
    ]


# =========================================================================
# EXCEL
# =========================================================================
def snapshot_row(ws, row):
    """Valori e stili di una riga del template."""
    return [
        (col, ws.cell(row=row, column=col).value, copy.copy(ws.cell(row=row, column=col)._style))
        for col in range(1, ws.max_column + 1)
    ]


def fill_row_from_template(ws, snapshot, src_row, dst_row):
    """
    Riproduce la riga dati del template su dst_row: stili, segnaposto e
    formule riferite alla nuova riga (=SUM(I3:J3)/1000*K3 -> ...I5:J5...K5).
    """
    for col, value, style in snapshot:
        cell = ws.cell(row=dst_row, column=col)
        cell._style = copy.copy(style)
        if isinstance(value, str) and value.startswith("="):
            letter = get_column_letter(col)
            value = Translator(value, origin=f"{letter}{src_row}").translate_formula(f"{letter}{dst_row}")
        cell.value = value


def extend_total_formulas(ws, first_row, last_row):
    """Formule sotto la tabella riferite alla sola riga dati del template:
    SUM(AQ3:AQ3) -> SUM(AQ3:AQ7)."""
    pattern = re.compile(
        r"(?<![A-Za-z$])(\$?[A-Z]{1,3}\$?)%d:(\$?[A-Z]{1,3}\$?)%d(?!\d)" % (first_row, first_row)
    )
    for row in ws.iter_rows(min_row=last_row + 1):
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.value = pattern.sub(
                    lambda m: f"{m.group(1)}{first_row}:{m.group(2)}{last_row}", cell.value
                )


def insert_rows_keep_layout(ws, row_idx, amount):
    """
    ws.insert_rows() che sposta anche celle unite, altezze righe e immagini
    sottostanti (openpyxl non lo fa e il layout del template si romperebbe).
    """
    merged_below = [str(rng) for rng in ws.merged_cells.ranges if rng.min_row >= row_idx]
    for ref in merged_below:
        ws.unmerge_cells(ref)

    heights = {
        r: dim.height for r, dim in ws.row_dimensions.items()
        if r >= row_idx and dim.height is not None
    }

    ws.insert_rows(row_idx, amount)

    for ref in merged_below:
        rng = CellRange(ref)
        rng.shift(row_shift=amount)
        ws.merge_cells(rng.coord)

    for r in heights:
        ws.row_dimensions[r].height = None
    for r, h in heights.items():
        ws.row_dimensions[r + amount].height = h
    for r in range(row_idx, row_idx + amount):
        ws.row_dimensions[r].height = ws.row_dimensions[row_idx - 1].height

    for img in ws._images:
        for marker in (getattr(img.anchor, "_from", None), getattr(img.anchor, "to", None)):
            if marker is not None and marker.row >= row_idx - 1:
                marker.row += amount


def prepare_data_rows(ws, count):
    """
    Prepara il form per 'count' pezzi in un'unica operazione: inserisce
    count-1 righe sotto la riga dati del template, le riempie come la
    riga del template e corregge i totali sotto la tabella.
    """
    if count <= 1:
        return

    first, last = DATA_START_ROW, DATA_START_ROW + count - 1
    template_row = snapshot_row(ws, first)
    insert_rows_keep_layout(ws, first + 1, count - 1)
    for row in range(first + 1, last + 1):
        fill_row_from_template(ws, template_row, first, row)
    extend_total_formulas(ws, first, last)
    extend_print_area(ws, first, count - 1)


def extend_print_area(ws, data_row, amount):
    """Area di stampa (nel template A1:AI3) estesa alle righe inserite."""
    if not ws.print_area:
        return

    ranges = []
    for ref in ws.print_area.split(","):
        rng = CellRange(ref.split("!")[-1].replace("$", ""))
        if rng.min_row > data_row:
            rng.shift(row_shift=amount)
        elif rng.max_row >= data_row:
            rng.expand(down=amount)
        ranges.append(rng.coord)
    ws.print_area = ranges


def fit_image_in_cell(img, ws, col_letter, row):
    """Ridimensiona l'immagine nella cella mantenendo le proporzioni."""
    box_w = (ws.column_dimensions[col_letter].width or 8.43) * 7.0 - 1.0
    box_h = (ws.row_dimensions[row].height or 15.0) * 96.0 / 72.0 - 6.0
    scale = min(box_w / img.width, box_h / img.height)
    img.width = max(1, int(img.width * scale))
    img.height = max(1, int(img.height * scale))


def write_row(ws, row, index, cfg, data):
    ws[f"{ITEM_NO_COLUMN}{row}"] = index

    # Dati CSV; se vuoti resta il segnaposto del template.
    for key, col in SETTINGS_COLUMN_MAP.items():
        val = cfg_get(cfg, key)
        if str(val).strip():
            ws[f"{col}{row}"] = excel_value(key, val)

    # Estrazione: idraulica oltre 550 t.
    tons = [int(n) for n in re.findall(r"\d+", str(cfg_get(cfg, "press")))]
    ws[f"Z{row}"] = "hydraulic" if tons and max(tons) > 550 else "mechanic"

    # Dati geometrici
    for col in ("B", "I", "N", "O", "P"):
        ws[f"{col}{row}"] = data[col]
    if THICKNESS_MAX_COLUMN:
        ws[f"{THICKNESS_MAX_COLUMN}{row}"] = data["N_max"]

    if data["C"]:
        img = XLImage(data["C"])
        fit_image_in_cell(img, ws, "C", row)
        ws.add_image(img, f"C{row}")


# =========================================================================
# MAIN
# =========================================================================
def main():
    start = time.time()
    os.makedirs(OUTPUT_DIR_VISTE, exist_ok=True)

    if not os.path.exists(PATH_TEMPLATE):
        fatal(f"Template non trovato: {PATH_TEMPLATE}")
        return False

    cfg_list, delimiter = load_settings()
    logger.info(f"settings.csv: {len(cfg_list)} righe, delimitatore '{delimiter}'")
    step_files = list_step_files()

    if not step_files:
        fatal(f"Nessun file STEP in {PATH_INPUT}")
        return False

    console(f"Compilazione RFQ: {len(step_files)} file STEP, {len(cfg_list)} righe in settings.csv")

    # 1) Analisi di tutti gli STEP (un multibody può dare più righe)
    results = []
    errors = 0
    for i, name in enumerate(step_files, start=1):
        label = f"[{i}/{len(step_files)}] {name}"
        cfg, _ = find_cfg_for_step(name, cfg_list)
        if cfg is None:
            console(f"{label}: saltato, nessuna riga in settings.csv")
            continue

        print(f"{label} ...", end="", flush=True)
        t0 = time.time()
        try:
            with capture_native_output():
                rows = analyze_step(os.path.join(PATH_INPUT, name), cfg)
        except Exception as e:
            errors += 1
            logger.exception(f"Errore su {name}: {e}")
            status = "ERRORE (vedi log)"
        else:
            results.extend((cfg, data) for data in rows)
            bodies = f", {len(rows)} corpi" if len(rows) > 1 else ""
            status = f"ok{bodies} ({time.time() - t0:.1f} s)"

        print(f" {status}", flush=True)
        logger.info(f"{label}: {status}")

    # 2) Righe del form inserite in un colpo solo, poi scrittura dati
    console("Scrittura Excel ...")
    wb = openpyxl.load_workbook(PATH_TEMPLATE)
    ws = wb.active
    ws[DATE_CELL] = datetime.now().strftime("%Y-%m-%d")

    prepare_data_rows(ws, len(results))
    for index, (cfg, data) in enumerate(results):
        write_row(ws, DATA_START_ROW + index, index + 1, cfg, data)
        logger.info(f"Riga {DATA_START_ROW + index}: {data['B']}")

    project = cfg_get(cfg_list[0], "project", "") if cfg_list else ""
    project = re.sub(r"[^\w\-]+", "_", project).strip("_") or "Default_Project"
    target = os.path.join(OUTPUT_DIR, f"{project}.xlsx")
    try:
        wb.save(target)
    except Exception as e:
        logger.exception("Dettaglio errore salvataggio")
        fatal(f"Impossibile salvare {target}: {e} (file aperto in Excel?)")
        return False

    summary = f"Completato in {time.time() - start:.0f} s: {len(results)} righe"
    if errors:
        summary += f", {errors} file con errori"
    console(summary)
    console(f"Excel: {target}")
    console(f"Log:   {LOG_FILE}")
    return errors == 0


def run():
    """Compilazione completa; codice di uscita 0 se riuscita senza errori."""
    setup_logging()
    logger.info("=" * 80)
    logger.info(f"Avvio {datetime.now():%Y-%m-%d %H:%M:%S}")
    try:
        return 0 if main() else 1
    except Exception as e:
        logger.exception("Errore non gestito")
        fatal(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(run())
