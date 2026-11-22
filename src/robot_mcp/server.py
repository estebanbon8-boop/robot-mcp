"""Serveur MCP pour Autodesk Robot Structural Analysis (API COM RobotOM).

Unités côté outils (pensées pour l'utilisateur) : m, kN, kN/m, kN·m.
Résultats renvoyés en kN, kN·m et mm.
L'API Robot travaille en N, N·m, m : la conversion est faite ici.

Toutes les opérations COM passent par un seul thread dédié (COM est mono-thread).
Rien n'est écrit sur stdout : stdout est réservé au protocole MCP.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from mcp.server.fastmcp import FastMCP

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[robot-mcp] %(message)s")
log = logging.getLogger("robot-mcp")

mcp = FastMCP("robot-mcp")

KN = 1000.0  # 1 kN = 1000 N


# --------------------------------------------------------------------------- COM


def _com_init() -> None:
    try:
        import pythoncom

        pythoncom.CoInitialize()
    except Exception as exc:  # l'erreur ressortira à l'appel, sans casser le pool
        log.error(f"Initialisation COM impossible : {exc}")


_executor = ThreadPoolExecutor(max_workers=1, initializer=_com_init)
_state: dict[str, Any] = {"app": None, "C": None}


def _robot():
    """Retourne (app, constantes), en se connectant à Robot si besoin."""
    if _state["app"] is not None:
        try:
            _ = _state["app"].Visible
            return _state["app"], _state["C"]
        except Exception:
            _state["app"] = None
    import win32com.client
    from win32com.client import gencache

    # Génère le module de types pour accéder aux constantes I_... par leur nom.
    gencache.EnsureDispatch("Robot.Application")
    app = win32com.client.dynamic.Dispatch("Robot.Application")
    app.Visible = 1
    app.Interactive = 1
    with contextlib.suppress(Exception):
        app.UserControl = True
    _state["app"] = app
    _state["C"] = win32com.client.constants
    return app, win32com.client.constants


def _structure():
    app, C = _robot()
    if not app.Project.IsActive:
        raise RuntimeError("Aucun projet ouvert dans Robot. Utilise robot_new_project ou robot_open_project.")
    return app, app.Project.Structure, C


def _cast(obj, interface: str):
    """Robot renvoie souvent des objets génériques (IRobotDataObject, IRobotCase) :
    il faut les convertir vers l'interface réelle pour accéder à leurs propriétés."""
    try:
        from win32com.client import CastTo

        return CastTo(obj, interface)
    except Exception:
        return obj


def _node(s, n):
    return _cast(s.Nodes.Get(int(n)), "IRobotNode")


def _bar(s, b):
    return _cast(s.Bars.Get(int(b)), "IRobotBar")


def _case(s, c):
    return _cast(s.Cases.Get(int(c)), "IRobotSimpleCase")


def _numbers(structure, C, obj_type: str, text: str) -> list[int]:
    """Convertit une sélection texte Robot ('1to5', '1 3 7', 'all') en liste de numéros."""
    if text.strip().lower() in ("all", "tout", "tous"):
        coll = structure.Nodes.GetAll() if obj_type == "node" else structure.Bars.GetAll()
        iface = "IRobotNode" if obj_type == "node" else "IRobotBar"
        return [_cast(coll.Get(i), iface).Number for i in range(1, coll.Count + 1)]
    sel = structure.Selections.Create(C.I_OT_NODE if obj_type == "node" else C.I_OT_BAR)
    sel.FromText(text)
    return [sel.Get(i) for i in range(1, sel.Count + 1)]


def _sel(structure, C, obj_type: str, text: str):
    """Objet sélection Robot ; 'all' est converti en liste explicite de numéros."""
    sel = structure.Selections.Create(C.I_OT_NODE if obj_type == "node" else C.I_OT_BAR)
    if text.strip().lower() in ("all", "tout", "tous"):
        text = " ".join(str(n) for n in _numbers(structure, C, obj_type, "all"))
    sel.FromText(text)
    return sel


TIMEOUT_S = 150


def _quiet(fn):
    """Exécute fn avec Robot en mode non interactif : pas de boîte de dialogue
    (« Enregistrer les modifications ? », conversions…) qui bloquerait l'API."""
    app = _state["app"]
    if app is not None:
        with contextlib.suppress(Exception):
            app.Interactive = 0
    try:
        return fn()
    finally:
        app = _state["app"]
        if app is not None:
            with contextlib.suppress(Exception):
                app.Interactive = 1
                app.UserControl = True


async def _run(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    try:
        fut = loop.run_in_executor(_executor, lambda: _quiet(lambda: fn(*args, **kwargs)))
        return await asyncio.wait_for(fut, TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"erreur": f"Robot ne répond pas après {TIMEOUT_S} s. Une boîte de dialogue est probablement "
                          "ouverte dans Robot : demande à l'utilisateur de la fermer, puis appelle robot_status."}
    except Exception as exc:  # renvoyé à Claude sous forme lisible
        log.error(traceback.format_exc())
        return {"erreur": f"{type(exc).__name__}: {exc}"}


# ------------------------------------------------------------------ PROJET


@mcp.tool()
async def robot_status() -> dict:
    """État de la connexion à Robot : version, projet ouvert, nombre de nœuds/barres/cas."""

    def f():
        app, C = _robot()
        out = {"connecte": True, "version": app.Version, "projet_ouvert": bool(app.Project.IsActive)}
        if app.Project.IsActive:
            s = app.Project.Structure
            out.update(
                fichier=app.Project.FileName or "(non enregistré)",
                noeuds=s.Nodes.GetAll().Count,
                barres=s.Bars.GetAll().Count,
                cas=s.Cases.GetAll().Count,
            )
        return out

    return await _run(f)


@mcp.tool()
async def robot_new_project(type: str = "frame_2d") -> dict:
    """Crée un nouveau projet (ferme le projet courant sans l'enregistrer). type : frame_2d (portique plan XZ), frame_3d, truss_2d, building, shell."""

    def f():
        app, C = _robot()
        types = {
            "frame_2d": C.I_PT_FRAME_2D,
            "frame_3d": C.I_PT_FRAME_3D,
            "truss_2d": C.I_PT_TRUSS_2D,
            "building": C.I_PT_BUILDING,
            "shell": C.I_PT_SHELL,
        }
        if type not in types:
            raise ValueError(f"type inconnu, choisir parmi {list(types)}")
        if app.Project.IsActive:
            app.Project.Close()
        app.Project.New(types[type])
        return {"ok": True, "type": type}

    return await _run(f)


@mcp.tool()
async def robot_open_project(path: str) -> dict:
    """Ouvre un fichier .rtd existant (chemin Windows complet). Ferme le projet courant SANS l'enregistrer :
    appeler robot_save_project avant si besoin. Renvoie le nombre de nœuds/barres/cas lus."""

    def f():
        app, _ = _robot()
        import os

        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        if app.Project.IsActive:
            app.Project.Close()
        app.Project.Open(path)
        s = app.Project.Structure
        return {"ok": True, "fichier": app.Project.FileName or path,
                "noeuds": s.Nodes.GetAll().Count, "barres": s.Bars.GetAll().Count, "cas": s.Cases.GetAll().Count}

    return await _run(f)


def _default_dir():
    import os
    d = os.path.join(os.path.expanduser("~"), "Documents", "Robot-MCP")
    os.makedirs(d, exist_ok=True)
    return d


def _save(app, path: str = "", name: str = "") -> str:
    import datetime
    import os

    if path:
        if not path.lower().endswith(".rtd"):
            path += ".rtd"
        app.Project.SaveAs(path)
        return path
    if app.Project.FileName:
        app.Project.Save()
        return app.Project.FileName
    stem = name or "projet_" + datetime.datetime.now().strftime("%Y%m%d_%H%M")
    path = os.path.join(_default_dir(), stem + ".rtd")
    app.Project.SaveAs(path)
    return path


@mcp.tool()
async def robot_save_project(path: str = "", name: str = "") -> dict:
    """Enregistre le projet (.rtd) pour que l'utilisateur puisse le rouvrir et le modifier dans Robot.
    - path : chemin complet 'Enregistrer sous'.
    - sinon, si le projet a déjà un fichier : enregistrement simple.
    - sinon : Documents\\Robot-MCP\\<name ou projet_date>.rtd.
    À appeler juste après la création du modèle, puis après chaque modification et après le calcul.
    Toujours indiquer à l'utilisateur le chemin du fichier."""

    def f():
        app, _ = _robot()
        return {"ok": True, "fichier": _save(app, path, name)}

    return await _run(f)


# ---------------------------------------------------------------- GÉOMÉTRIE


@mcp.tool()
async def robot_add_nodes(nodes: list[dict]) -> dict:
    """Ajoute des nœuds. nodes = [{"id":1,"x":0,"y":0,"z":0}, ...] en mètres.
    En portique 2D, le plan de travail est XZ (Z vertical)."""

    def f():
        _, s, _ = _structure()
        for n in nodes:
            s.Nodes.Create(int(n["id"]), float(n.get("x", 0)), float(n.get("y", 0)), float(n.get("z", 0)))
        return {"ok": True, "crees": len(nodes)}

    return await _run(f)


@mcp.tool()
async def robot_add_bars(bars: list[dict]) -> dict:
    """Ajoute des barres. bars = [{"id":1,"start":1,"end":2}, ...] (numéros de nœuds)."""

    def f():
        _, s, _ = _structure()
        for b in bars:
            s.Bars.Create(int(b["id"]), int(b["start"]), int(b["end"]))
        return {"ok": True, "crees": len(bars)}

    return await _run(f)


@mcp.tool()
async def robot_delete(object_type: str, selection: str) -> dict:
    """Supprime des objets. object_type : 'node' ou 'bar'. selection : texte Robot ('1to5', '2 4').
    Attention : supprimer/recréer une barre fait perdre ses relâchements."""

    def f():
        _, s, C = _structure()
        sel = _sel(s, C, object_type, selection)
        if object_type == "node":
            s.Nodes.DeleteMany(sel)
        else:
            s.Bars.DeleteMany(sel)
        return {"ok": True}

    return await _run(f)


@mcp.tool()
async def robot_list_model(max_items: int = 300) -> dict:
    """Liste les nœuds (coordonnées, appui) et barres (nœuds, section, matériau) du modèle."""

    def f():
        _, s, C = _structure()
        nodes, bars = [], []
        coll = s.Nodes.GetAll()
        for i in range(1, min(coll.Count, max_items) + 1):
            n = _cast(coll.Get(i), "IRobotNode")
            nodes.append(
                {"id": n.Number, "x": round(n.X, 4), "y": round(n.Y, 4), "z": round(n.Z, 4),
                 "appui": n.GetLabelName(C.I_LT_SUPPORT) if n.HasLabel(C.I_LT_SUPPORT) else None}
            )
        coll = s.Bars.GetAll()
        for i in range(1, min(coll.Count, max_items) + 1):
            b = _cast(coll.Get(i), "IRobotBar")
            bars.append(
                {"id": b.Number, "start": b.StartNode, "end": b.EndNode,
                 "section": b.GetLabelName(C.I_LT_BAR_SECTION) if b.HasLabel(C.I_LT_BAR_SECTION) else None,
                 "materiau": b.GetLabelName(C.I_LT_MATERIAL) if b.HasLabel(C.I_LT_MATERIAL) else None}
            )
        return {"noeuds": nodes, "barres": bars}

    return await _run(f)


# -------------------------------------------------------------- PROPRIÉTÉS


def _ensure_label(s, label_type, name: str, fill):
    labels = s.Labels
    if not labels.Exist(label_type, name):
        lab = labels.Create(label_type, name)
        fill(lab.Data)
        labels.Store(lab)


@mcp.tool()
async def robot_set_section(bars: str, section: str) -> dict:
    """Affecte une section du catalogue Robot aux barres (ex. 'IPE 240', 'HEA 200', 'RRHS 100x50x5').
    bars : sélection texte ('1to4', 'all')."""

    def f():
        _, s, C = _structure()
        _ensure_label(s, C.I_LT_BAR_SECTION, section, lambda d: d.LoadFromDBase(section))
        s.Bars.SetLabel(_sel(s, C, "bar", bars), C.I_LT_BAR_SECTION, section)
        return {"ok": True, "section": section, "barres": bars}

    return await _run(f)


@mcp.tool()
async def robot_set_material(bars: str, material: str) -> dict:
    """Affecte un matériau de la base Robot (nom exact de la base, ex. 'S 355' selon la base chargée)."""

    def f():
        _, s, C = _structure()
        _ensure_label(s, C.I_LT_MATERIAL, material, lambda d: d.LoadFromDBase(material))
        s.Bars.SetLabel(_sel(s, C, "bar", bars), C.I_LT_MATERIAL, material)
        return {"ok": True, "materiau": material}

    return await _run(f)


SUPPORTS = {
    "encastrement": (1, 1, 1, 1, 1, 1),
    "rotule": (1, 1, 1, 0, 0, 0),
    "appui_simple": (0, 1, 1, 0, 0, 0),  # libre en X : appui glissant
}


@mcp.tool()
async def robot_set_support(nodes: str, kind: str = "rotule", custom: list[int] | None = None) -> dict:
    """Pose un appui sur des nœuds. kind : 'encastrement', 'rotule', 'appui_simple' (glissant en X),
    ou 'custom' avec custom=[UX,UY,UZ,RX,RY,RZ] (1 = bloqué, 0 = libre)."""

    def f():
        _, s, C = _structure()
        flags = tuple(custom) if kind == "custom" and custom else SUPPORTS.get(kind)
        if flags is None:
            raise ValueError(f"kind inconnu : {list(SUPPORTS) + ['custom']}")
        name = kind if kind != "custom" else "custom_" + "".join(str(int(v)) for v in flags)

        def fill(d):
            d.UX, d.UY, d.UZ, d.RX, d.RY, d.RZ = (int(v) for v in flags)

        _ensure_label(s, C.I_LT_SUPPORT, name, fill)
        s.Nodes.SetLabel(_sel(s, C, "node", nodes), C.I_LT_SUPPORT, name)
        return {"ok": True, "appui": name, "noeuds": nodes}

    return await _run(f)


# ------------------------------------------------------------------ CHARGES


NATURES = {"permanente": "I_CN_PERMANENT", "exploitation": "I_CN_EXPLOATATION", "vent": "I_CN_WIND",
           "neige": "I_CN_SNOW", "temperature": "I_CN_TEMPERATURE", "accidentelle": "I_CN_ACCIDENTAL",
           "sismique": "I_CN_SEISMIC"}


@mcp.tool()
async def robot_add_load_case(id: int, name: str, nature: str = "permanente", nonlinear: bool = False) -> dict:
    """Crée un cas de charge simple. nature : permanente, exploitation, vent, neige, temperature,
    accidentelle, sismique. nonlinear=True : analyse non linéaire géométrique (2nd ordre)."""

    def f():
        _, s, C = _structure()
        nat = getattr(C, NATURES[nature])
        ana = C.I_CAT_STATIC_NONLINEAR if nonlinear else C.I_CAT_STATIC_LINEAR
        s.Cases.CreateSimple(int(id), name, nat, ana)
        return {"ok": True, "cas": id, "nom": name, "nature": nature}

    return await _run(f)


@mcp.tool()
async def robot_add_self_weight(case: int) -> dict:
    """Ajoute le poids propre de toute la structure (direction -Z) dans un cas."""

    def f():
        _, s, C = _structure()
        rec = _case(s, case).Records.New(C.I_LRT_DEAD)
        rec.SetValue(C.I_DRV_Z, -1)
        rec.SetValue(C.I_DRV_ENTIRE_STRUCTURE, 1)
        return {"ok": True, "cas": case}

    return await _run(f)


@mcp.tool()
async def robot_add_nodal_force(case: int, nodes: str, fx_kN: float = 0, fy_kN: float = 0, fz_kN: float = 0,
                                mx_kNm: float = 0, my_kNm: float = 0, mz_kNm: float = 0) -> dict:
    """Force/moment ponctuel sur des nœuds, en kN et kN·m, repère global. fz négatif = vers le bas."""

    def f():
        _, s, C = _structure()
        rec = _case(s, case).Records.New(C.I_LRT_NODE_FORCE)
        for key, v in (("I_NFRV_FX", fx_kN), ("I_NFRV_FY", fy_kN), ("I_NFRV_FZ", fz_kN),
                       ("I_NFRV_CX", mx_kNm), ("I_NFRV_CY", my_kNm), ("I_NFRV_CZ", mz_kNm)):
            if v:
                rec.SetValue(getattr(C, key), v * KN)
        rec.Objects.FromText(nodes)
        return {"ok": True}

    return await _run(f)


@mcp.tool()
async def robot_add_bar_uniform_load(case: int, bars: str, pz_kN_m: float = 0, px_kN_m: float = 0,
                                     py_kN_m: float = 0, projected: bool = False) -> dict:
    """Charge répartie uniforme sur des barres, en kN/m, repère global. pz négatif = vers le bas.
    projected=True : charge projetée (neige sur toiture inclinée)."""

    def f():
        _, s, C = _structure()
        rec = _case(s, case).Records.New(C.I_LRT_BAR_UNIFORM)
        for key, v in (("I_BURV_PX", px_kN_m), ("I_BURV_PY", py_kN_m), ("I_BURV_PZ", pz_kN_m)):
            if v:
                rec.SetValue(getattr(C, key), v * KN)
        if projected:
            rec.SetValue(C.I_BURV_PROJECTED, 1)
        rec.Objects.FromText(bars)
        return {"ok": True}

    return await _run(f)


@mcp.tool()
async def robot_add_bar_point_load(case: int, bars: str, fz_kN: float, position: float = 0.5,
                                   relative: bool = True) -> dict:
    """Force ponctuelle verticale sur une barre. position : relative (0 à 1) par défaut, ou en m si relative=False.
    fz négatif = vers le bas."""

    def f():
        _, s, C = _structure()
        rec = _case(s, case).Records.New(C.I_LRT_BAR_FORCE_CONCENTRATED)
        rec.SetValue(C.I_BFCRV_FZ, fz_kN * KN)
        rec.SetValue(C.I_BFCRV_X, position)
        rec.SetValue(C.I_BFCRV_REL, 1 if relative else 0)
        rec.Objects.FromText(bars)
        return {"ok": True}

    return await _run(f)


@mcp.tool()
async def robot_add_combination(id: int, name: str, factors: dict[str, float], kind: str = "ELU") -> dict:
    """Crée une combinaison. kind : ELU, ELS ou ACC. factors = {"1": 1.35, "2": 1.5} (numéro de cas -> coefficient)."""

    def f():
        _, s, C = _structure()
        ctype = {"ELU": C.I_CBT_ULS, "ELS": C.I_CBT_SLS, "ACC": C.I_CBT_ACC}[kind.upper()]
        comb = s.Cases.CreateCombination(int(id), name, ctype, C.I_CN_PERMANENT, C.I_CAT_COMB)
        for case_id, k in factors.items():
            comb.CaseFactors.New(int(case_id), float(k))
        return {"ok": True, "combinaison": id, "type": kind, "facteurs": factors}

    return await _run(f)


@mcp.tool()
async def robot_list_cases() -> dict:
    """Liste les cas de charge et combinaisons (numéro, nom). À vérifier avant de lire des résultats :
    un cas inexistant renvoie des zéros."""

    def f():
        _, s, _ = _structure()
        coll = s.Cases.GetAll()
        items = [_cast(coll.Get(i), "IRobotCase") for i in range(1, coll.Count + 1)]
        return {"cas": [{"id": c.Number, "nom": c.Name} for c in items]}

    return await _run(f)


# ---------------------------------------------------------------- CALCUL


@mcp.tool()
async def robot_calculate(save: bool = True) -> dict:
    """Lance le calcul puis, par défaut, enregistre le projet (résultats inclus) pour que l'utilisateur
    retrouve les diagrammes en rouvrant le fichier. Fermer toute boîte de dialogue dans Robot avant
    (sinon blocage). Si des résultats manquent après réouverture d'un fichier : relancer le calcul."""

    def f():
        app, _ = _robot()
        code = app.Project.CalcEngine.Calculate()
        out = {"ok": True, "code_retour": code}
        if save:
            out["fichier"] = _save(app)
        return out

    return await _run(f)


# -------------------------------------------------------------- RÉSULTATS


@mcp.tool()
async def robot_bar_forces(bars: str, case: int, points: int = 5) -> dict:
    """Efforts dans les barres (kN, kN·m) en `points` positions régulières de 0 à 1.
    Convention Robot : FX > 0 = compression."""

    def f():
        _, s, C = _structure()
        res = s.Results.Bars.Forces
        n = max(2, int(points))
        out = []
        for b in _numbers(s, C, "bar", bars):
            for k in range(n):
                x = k / (n - 1)
                v = res.Value(b, int(case), x)
                out.append({"barre": b, "x_rel": round(x, 3), "FX_kN": round(v.FX / KN, 3),
                            "FZ_kN": round(v.FZ / KN, 3), "MY_kNm": round(v.MY / KN, 3),
                            "FY_kN": round(v.FY / KN, 3), "MZ_kNm": round(v.MZ / KN, 3),
                            "MX_kNm": round(v.MX / KN, 3)})
        return {"cas": case, "efforts": out}

    return await _run(f)


@mcp.tool()
async def robot_node_reactions(nodes: str, case: int) -> dict:
    """Réactions d'appui (kN, kN·m). Vérifier que leur somme équilibre les charges appliquées."""

    def f():
        _, s, C = _structure()
        res = s.Results.Nodes.Reactions
        out, tot = [], {"FX": 0.0, "FY": 0.0, "FZ": 0.0}
        for n in _numbers(s, C, "node", nodes):
            v = res.Value(n, int(case))
            row = {"noeud": n, "FX_kN": round(v.FX / KN, 3), "FY_kN": round(v.FY / KN, 3),
                   "FZ_kN": round(v.FZ / KN, 3), "MX_kNm": round(v.MX / KN, 3),
                   "MY_kNm": round(v.MY / KN, 3), "MZ_kNm": round(v.MZ / KN, 3)}
            out.append(row)
            for k in tot:
                tot[k] += row[f"{k}_kN"]
        return {"cas": case, "reactions": out, "somme_kN": {k: round(v, 3) for k, v in tot.items()}}

    return await _run(f)


@mcp.tool()
async def robot_node_displacements(nodes: str, case: int) -> dict:
    """Déplacements des nœuds (mm) et rotations (mrad)."""

    def f():
        _, s, C = _structure()
        res = s.Results.Nodes.Displacements
        out = []
        for n in _numbers(s, C, "node", nodes):
            v = res.Value(n, int(case))
            out.append({"noeud": n, "UX_mm": round(v.UX * 1000, 3), "UY_mm": round(v.UY * 1000, 3),
                        "UZ_mm": round(v.UZ * 1000, 3), "RX_mrad": round(v.RX * 1000, 3),
                        "RY_mrad": round(v.RY * 1000, 3), "RZ_mrad": round(v.RZ * 1000, 3)})
        return {"cas": case, "deplacements": out}

    return await _run(f)


@mcp.tool()
async def robot_export_diagrams(bars: str, case: int, quantities: list[str] | None = None, points: int = 21,
                                folder: str = "", title: str = "") -> dict:
    """Exporte les diagrammes d'efforts le long des barres en PNG (image) et CSV (Excel).
    quantities parmi : MY (moment fléchissant), FZ (effort tranchant), FX (effort normal), UZ (flèche, mm).
    Défaut : MY, FZ, FX. Les barres listées sont mises bout à bout (poutre continue, portique déplié).
    Fichiers rangés à côté du projet (ou dans Documents\\Robot-MCP). Indiquer les chemins à l'utilisateur.
    Les diagrammes restent aussi visibles dans Robot : Résultats > Diagrammes pour barres."""

    def f():
        import csv
        import os

        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        app, s, C = _structure()
        qs = [q.upper() for q in (quantities or ["MY", "FZ", "FX"])]
        units = {"MY": "kN·m", "FZ": "kN", "FX": "kN", "UZ": "mm", "MZ": "kN·m", "FY": "kN"}
        labels = {"MY": "Moment fléchissant My", "FZ": "Effort tranchant Vz", "FX": "Effort normal N",
                  "UZ": "Flèche Uz", "MZ": "Moment Mz", "FY": "Effort tranchant Vy"}
        forces = s.Results.Bars.Forces
        defl = s.Results.Bars.Deflections
        n = max(3, int(points))
        rows, x0 = [], 0.0
        for b in _numbers(s, C, "bar", bars):
            bar = _bar(s, b)
            a, e = _node(s, bar.StartNode), _node(s, bar.EndNode)
            L = ((e.X - a.X) ** 2 + (e.Y - a.Y) ** 2 + (e.Z - a.Z) ** 2) ** 0.5
            for k in range(n):
                r = k / (n - 1)
                v = forces.Value(b, int(case), r)
                row = {"barre": b, "x_m": round(x0 + r * L, 4), "MY": v.MY / KN, "FZ": v.FZ / KN,
                       "FX": v.FX / KN, "MZ": v.MZ / KN, "FY": v.FY / KN}
                if "UZ" in qs:
                    row["UZ"] = _cast(defl.Value(b, int(case), r), "IRobotBarDeflectionData").UZ * 1000
                rows.append(row)
            x0 += L
        base = os.path.dirname(app.Project.FileName) if app.Project.FileName else _default_dir()
        folder_ = folder or os.path.join(base, "diagrammes")
        os.makedirs(folder_, exist_ok=True)
        tag = f"cas{case}_barres_{bars.replace(' ', '-')}"
        csv_path = os.path.join(folder_, f"{tag}.csv")
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
            cols = ["barre", "x_m"] + qs
            w = csv.writer(fh, delimiter=";")
            w.writerow([c if c in ("barre", "x_m") else f"{c} ({units.get(c, '')})" for c in cols])
            for r in rows:
                w.writerow([r["barre"], f"{r['x_m']:.3f}".replace(".", ",")]
                          + [f"{r[c]:.3f}".replace(".", ",") for c in qs])
        files, extrema = [csv_path], {}
        xs = [r["x_m"] for r in rows]
        for q in qs:
            ys = [r[q] for r in rows]
            imax, imin = ys.index(max(ys)), ys.index(min(ys))
            extrema[q] = {"max": round(ys[imax], 3), "x_max_m": xs[imax], "min": round(ys[imin], 3),
                          "x_min_m": xs[imin], "unite": units.get(q, "")}
            fig, ax = plt.subplots(figsize=(9, 3.6))
            ax.plot(xs, ys, color="#1f5fa8", lw=2)
            ax.fill_between(xs, ys, 0, color="#1f5fa8", alpha=0.15)
            ax.axhline(0, color="black", lw=0.8)
            if q == "MY":
                ax.invert_yaxis()  # convention française : moment positif tracé côté fibre tendue (en bas)
            for i in {imax, imin}:
                ax.annotate(f"{ys[i]:.2f} {units.get(q, '')}", (xs[i], ys[i]), textcoords="offset points",
                            xytext=(0, 8), ha="center", fontsize=9)
            ax.set_xlabel("x (m)")
            ax.set_ylabel(f"{q} ({units.get(q, '')})")
            ax.set_title(f"{title + ' — ' if title else ''}{labels.get(q, q)} — cas {case}")
            ax.grid(alpha=0.3)
            fig.tight_layout()
            png = os.path.join(folder_, f"{tag}_{q}.png")
            fig.savefig(png, dpi=150)
            plt.close(fig)
            files.append(png)
        return {"fichiers": files, "extremums": extrema}

    return await _run(f)


@mcp.tool()
async def robot_api_search(keyword: str, max_results: int = 80) -> dict:
    """Cherche dans l'API RobotOM complète (bibliothèque de types de Robot installée) : interfaces,
    méthodes, propriétés et constantes I_... dont le nom contient `keyword` (ex. 'Release', 'Buckling',
    'RC', 'Steel', 'Snow', 'Combination', 'Mesh', 'Panel'). À utiliser AVANT d'écrire du code pour
    robot_run_python, afin d'utiliser des noms réels plutôt que de les deviner."""

    def f():
        from win32com.client import gencache

        _robot()
        mod = gencache.GetModuleForProgID("Robot.Application")
        kw = keyword.lower()
        interfaces, members, consts = [], [], []
        for name in dir(mod):
            cls = getattr(mod, name)
            if not isinstance(cls, type):
                continue
            if kw in name.lower():
                interfaces.append(name)
            props = set(getattr(cls, "_prop_map_get_", {}) or {}) | set(getattr(cls, "_prop_map_put_", {}) or {})
            meths = [m for m in vars(cls) if not m.startswith("_") and callable(getattr(cls, m, None))]
            for m in sorted(props | set(meths)):
                if kw in m.lower():
                    members.append(f"{name}.{m}" + ("" if m in props else "()"))
        import win32com.client

        for dct in getattr(win32com.client.constants, "__dicts__", []):
            for cname in dct:
                if kw in cname.lower():
                    consts.append(f"{cname}={dct[cname]}")
        m = int(max_results)
        return {"interfaces": interfaces[:m], "membres": members[:m], "constantes": consts[:m],
                "tronque": len(interfaces) > m or len(members) > m or len(consts) > m}

    return await _run(f)


@mcp.tool()
async def robot_api_describe(interface: str) -> dict:
    """Liste toutes les propriétés et méthodes (avec leurs arguments) d'une interface RobotOM,
    ex. 'IRobotBarReleaseData', 'IRobotCalcEngine', 'IRobotProject'."""

    def f():
        import inspect

        from win32com.client import gencache

        _robot()
        mod = gencache.GetModuleForProgID("Robot.Application")
        cls = getattr(mod, interface, None)
        if cls is None:
            raise ValueError(f"Interface inconnue : {interface}. Utilise robot_api_search d'abord.")
        props_get = sorted(getattr(cls, "_prop_map_get_", {}) or {})
        props_put = sorted(getattr(cls, "_prop_map_put_", {}) or {})
        methods = []
        for name, fn in vars(cls).items():
            if name.startswith("_") or not callable(fn):
                continue
            try:
                methods.append(f"{name}{inspect.signature(fn)}".replace("(self, ", "(").replace("(self)", "()"))
            except (TypeError, ValueError):
                methods.append(f"{name}(...)")
        return {"interface": interface, "proprietes_lecture": props_get,
                "proprietes_ecriture": props_put, "methodes": sorted(methods)}

    return await _run(f)


# ------------------------------------------------------------- AVANCÉ


@mcp.tool()
async def robot_run_python(code: str) -> dict:
    """Accès COMPLET à Robot — exécute du code Python avec l'API RobotOM pour tout ce que les outils
    dédiés ne couvrent pas : relâchements, flambement, modal, sismique, neige/vent, panneaux et maillage,
    dimensionnement acier/BA, ferraillage, réglages de normes, vues, tableaux… Chercher d'abord les noms
    exacts avec robot_api_search / robot_api_describe, puis tester sur une copie du projet. Variables disponibles : app (Robot), structure, C (constantes I_...).
    Unités de l'API : N, N·m, m. Affecter `result = ...` pour renvoyer une valeur ; print() est capturé.
    Modifie le modèle : à n'utiliser qu'avec l'accord de l'utilisateur."""

    def f():
        app, C = _robot()
        env = {"app": app, "C": C, "structure": app.Project.Structure if app.Project.IsActive else None,
               "result": None}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(code, env)
        return {"sortie": buf.getvalue()[-8000:], "result": repr(env.get("result"))[:8000]}

    return await _run(f)


# ------------------------------------------------------------------ MAIN


def main() -> None:
    if "--selfcheck" in sys.argv:
        tools = asyncio.run(mcp.list_tools())
        print(f"robot-mcp OK : {len(tools)} outils", file=sys.stderr)
        for t in tools:
            print(" -", t.name, file=sys.stderr)
        return
    mcp.run()


if __name__ == "__main__":
    main()
