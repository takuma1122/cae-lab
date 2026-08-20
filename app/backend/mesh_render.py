"""メッシュ形状のサムネイル画像生成（v1: 1枚の俯瞰図のみ）。

OpenFOAM: constant/polyMesh の points/faces/boundary をテキストとして自前パースし、
          境界パッチのfaceだけを外殻サーフェスとして描画する。
CalculiX: *.inp の *NODE/*ELEMENT カードを自前パースし、要素タイプ別のコーナー
          節点＋辺トポロジでワイヤーフレームを描画する。

既知の未対応事項:
- 軸対称（wedge）ケースは薄い形状になり、2Dケースと同様に斜め俯瞰では
  形状が潰れて見える可能性が高い。検証用の実例が無いため今回は未対応。
"""
import re
from pathlib import Path
from typing import Optional

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection, Line3DCollection


def _setup_axes(ax, points: np.ndarray, top_down: bool = False):
    mins = points.min(axis=0)
    maxs = points.max(axis=0)
    center = (mins + maxs) / 2
    radius = max((maxs - mins).max() / 2, 1e-9)
    ax.set_xlim(center[0] - radius, center[0] + radius)
    ax.set_ylim(center[1] - radius, center[1] + radius)
    ax.set_zlim(center[2] - radius, center[2] + radius)
    ax.set_box_aspect([1, 1, 1])
    if top_down:
        ax.view_init(elev=90, azim=-90)
    else:
        ax.view_init(elev=25, azim=-50)
    ax.set_axis_off()


def _save(fig, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".tmp.png")
    fig.savefig(tmp_path, facecolor="white")
    plt.close(fig)
    tmp_path.replace(out_path)


# --- OpenFOAM ------------------------------------------------------------
def _strip_foam_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//.*", "", text)
    return text


def _read_foam_numeric_list(path: Path):
    text = _strip_foam_comments(path.read_text(errors="replace"))
    text = re.sub(r"FoamFile\s*\{.*?\}", "", text, flags=re.DOTALL)
    m = re.search(r"\d+\s*\((.*)\)\s*$", text, re.DOTALL)
    return m.group(1) if m else ""


def _read_points(path: Path) -> np.ndarray:
    body = _read_foam_numeric_list(path)
    coords = re.findall(r"\(([^()]*)\)", body)
    return np.array([[float(v) for v in c.split()] for c in coords])


def _read_faces(path: Path) -> list:
    body = _read_foam_numeric_list(path)
    return [[int(v) for v in verts.split()]
            for _nv, verts in re.findall(r"(\d+)\(([^()]*)\)", body)]


def _read_boundary_patches(path: Path) -> list:
    text = _strip_foam_comments(path.read_text(errors="replace"))
    text = re.sub(r"FoamFile\s*\{.*?\}", "", text, flags=re.DOTALL)
    patches = []
    for name, body in re.findall(r"(\w+)\s*\{([^{}]*)\}", text):
        nfaces = re.search(r"nFaces\s+(\d+)\s*;", body)
        start = re.search(r"startFace\s+(\d+)\s*;", body)
        if nfaces and start:
            patches.append((name, int(start.group(1)), int(nfaces.group(1))))
    return patches


def render_openfoam_mesh(case_dir: Path, out_path: Path, dimension: Optional[str] = None) -> bool:
    pm = case_dir / "constant" / "polyMesh"
    points_file, faces_file, boundary_file = pm / "points", pm / "faces", pm / "boundary"
    if not (points_file.is_file() and faces_file.is_file() and boundary_file.is_file()):
        return False

    points = _read_points(points_file)
    faces = _read_faces(faces_file)
    patches = _read_boundary_patches(boundary_file)
    if points.size == 0 or not faces or not patches:
        return False

    boundary_faces = []
    for _name, start, n in patches:
        boundary_faces.extend(faces[start:start + n])
    polys = [points[f] for f in boundary_faces]

    plot_points = points
    top_down = dimension == "2d"
    if top_down:
        # 最も薄い（範囲が小さい）軸をZ相当に入れ替えて、常に「真上から」の
        # 定型ビュー（elev=90）で見られるようにする（軸ごとの視点調整をしない）。
        extent = points.max(axis=0) - points.min(axis=0)
        thin_axis = int(np.argmin(extent))
        order = [i for i in range(3) if i != thin_axis] + [thin_axis]
        plot_points = points[:, order]
        polys = [p[:, order] for p in polys]

    fig = plt.figure(figsize=(6, 6), dpi=120)
    ax = fig.add_subplot(projection="3d")
    coll = Poly3DCollection(polys, facecolor="#8fb8e0", edgecolor="#2c4a6b", linewidths=0.3, alpha=0.9)
    ax.add_collection3d(coll)
    _setup_axes(ax, plot_points, top_down=top_down)
    _save(fig, out_path)
    return True


# --- CalculiX --------------------------------------------------------------
# ソリッド要素（コーナー節点をワイヤーフレームの辺でつなぐ）
HEX_EDGES = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]
TET_EDGES = [(0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3)]

SOLID_TOPOLOGY = {
    "C3D8": (8, HEX_EDGES), "C3D8R": (8, HEX_EDGES), "C3D8I": (8, HEX_EDGES),
    "C3D20": (8, HEX_EDGES), "C3D20R": (8, HEX_EDGES),
    "C3D4": (4, TET_EDGES), "C3D10": (4, TET_EDGES),
}

# シェル要素（コーナー節点を1枚の面として塗りつぶす）
SHELL_CORNERS = {
    "S3": 3, "S3R": 3,
    "S4": 4, "S4R": 4, "S4RS": 4, "S4RSW": 4,
    "S8": 4, "S8R": 4, "S8RS": 4,
    "S6": 3, "S9": 4, "S9R": 4,
    "M3D3": 3, "M3D4": 4, "M3D4R": 4, "M3D8": 4, "M3D8R": 4,  # 膜要素も同じ面描画で扱う
}

# 線要素（節点を記載順につないでパスにする。B32等の中間節点順序は解析不要）
LINE_PREFIXES = ("B", "T3D")


def classify_element(etype: str):
    """要素タイプを ("line"|"shell"|"solid", トポロジ情報) に分類する。
    未対応の場合は ("unsupported", None) を返す。"""
    if etype.startswith(LINE_PREFIXES):
        return "line", None
    if etype in SHELL_CORNERS:
        return "shell", SHELL_CORNERS[etype]
    if etype in SOLID_TOPOLOGY:
        return "solid", SOLID_TOPOLOGY[etype]
    return "unsupported", None


def _parse_inp_geometry(path: Path):
    lines = path.read_text(errors="replace").splitlines()
    nodes = {}
    elements = []

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if re.match(r"^\*NODE\b", line, re.IGNORECASE):
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("*"):
                parts = [p.strip() for p in lines[i].split(",") if p.strip() != ""]
                if len(parts) >= 4:
                    nodes[int(parts[0])] = tuple(float(v) for v in parts[1:4])
                i += 1
            continue
        m = re.match(r"^\*ELEMENT\s*,.*?TYPE\s*=\s*(\S+?)(?:,|\s|$)", line, re.IGNORECASE)
        if m:
            etype = m.group(1).upper()
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("*"):
                buf = lines[i].rstrip()
                while buf.rstrip().endswith(",") and i + 1 < len(lines):
                    i += 1
                    buf += lines[i].rstrip()
                parts = [p.strip() for p in buf.split(",") if p.strip() != ""]
                if parts:
                    elements.append((etype, [int(v) for v in parts[1:]]))
                i += 1
            continue
        i += 1

    return nodes, elements


def summarize_calculix_elements(inp_path: Path) -> dict:
    """要素タイプごとの件数と、現在の表示方式が対応していない要素の件数を集計する。
    レンダリングを伴わない軽い集計のみ（詳細パネルへの警告表示に使う）。"""
    nodes, elements = _parse_inp_geometry(inp_path)
    unsupported: dict[str, int] = {}
    supported: dict[str, int] = {}
    for etype, _ids in elements:
        kind, _ = classify_element(etype)
        target = unsupported if kind == "unsupported" else supported
        target[etype] = target.get(etype, 0) + 1
    return {"supported": supported, "unsupported": unsupported}


def render_calculix_mesh(inp_path: Path, out_path: Path) -> bool:
    nodes, elements = _parse_inp_geometry(inp_path)
    if not nodes or not elements:
        return False

    segs = []
    polys = []
    used_node_ids = set()

    for etype, ids in elements:
        kind, topo = classify_element(etype)
        if kind == "line":
            pts = [nodes[n] for n in ids if n in nodes]
            segs.extend(zip(pts, pts[1:]))
            used_node_ids.update(n for n in ids if n in nodes)
        elif kind == "solid":
            ncorner, edges = topo
            corner_ids = ids[:ncorner]
            if len(corner_ids) < ncorner or any(n not in nodes for n in corner_ids):
                continue
            corners = [nodes[n] for n in corner_ids]
            segs.extend((corners[a], corners[b]) for a, b in edges)
            used_node_ids.update(corner_ids)
        elif kind == "shell":
            ncorner = topo
            corner_ids = ids[:ncorner]
            if len(corner_ids) < ncorner or any(n not in nodes for n in corner_ids):
                continue
            polys.append([nodes[n] for n in corner_ids])
            used_node_ids.update(corner_ids)
        # "unsupported" はここでは黙って描画対象から外すのみ。
        # 件数は summarize_calculix_elements() 側で集計し、詳細パネルの警告表示に使う。

    if not segs and not polys:
        return False

    points = np.array([nodes[n] for n in used_node_ids]) if used_node_ids else np.array(list(nodes.values()))

    fig = plt.figure(figsize=(6, 6), dpi=120)
    ax = fig.add_subplot(projection="3d")
    if polys:
        ax.add_collection3d(Poly3DCollection(
            polys, facecolor="#8fb8e0", edgecolor="#2c4a6b", linewidths=0.6, alpha=0.9,
        ))
    if segs:
        ax.add_collection3d(Line3DCollection(segs, colors="#2c4a6b", linewidths=1.2))
    ax.scatter(points[:, 0], points[:, 1], points[:, 2], s=8, color="#8fb8e0")
    _setup_axes(ax, points)
    _save(fig, out_path)
    return True
