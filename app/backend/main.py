import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .mesh_render import render_openfoam_mesh, render_calculix_mesh, summarize_calculix_elements

# --- 設定 -------------------------------------------------------------
# SCAN_ROOT: コンテナ内で解析結果フォルダをマウントしている場所（読み取り専用）
# HOST_ROOT: 上記に対応するホスト（WSL）側の実パス。フォルダを開く導線の表示用。
SCAN_ROOT = Path(os.environ.get("SCAN_ROOT", "/data"))
HOST_ROOT = os.environ.get("HOST_ROOT", str(SCAN_ROOT))
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "/app/data"))
MEMO_FILE = DATA_DIR / "memos.json"
THUMBNAIL_DIR = DATA_DIR / "thumbnails"

# OpenFOAMの前処理/後処理ユーティリティのログはソルバー本体の完走判定に使わない
OPENFOAM_UTILITY_NAMES = {
    "blockMesh", "decomposePar", "reconstructPar", "reconstructParMesh",
    "checkMesh", "snappyHexMesh", "surfaceFeatureExtract", "topoSet",
    "createPatch", "mapFields", "paraFoam", "foamToVTK", "renumberMesh",
}

app = FastAPI(title="CAE解析結果管理ツール（v0）")


# --- メモの永続化 -------------------------------------------------------
def load_memos() -> dict:
    if MEMO_FILE.is_file():
        try:
            return json.loads(MEMO_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_memo(case_key: str, memo: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    memos = load_memos()
    if memo:
        memos[case_key] = memo
    else:
        memos.pop(case_key, None)
    MEMO_FILE.write_text(json.dumps(memos, ensure_ascii=False, indent=2), encoding="utf-8")


# --- ケース検出 ---------------------------------------------------------
def is_openfoam_case(d: Path) -> bool:
    return (d / "system" / "controlDict").is_file() and (d / "constant").is_dir()


def is_calculix_case(d: Path) -> bool:
    return any(d.glob("*.inp"))


def find_case_dirs(root: Path):
    """root配下を探索し、OpenFOAM/CalculiXの指紋に一致するフォルダを返す。
    一致したフォルダの内部はさらに潜らない（processor* 等の誤検知防止）。"""
    if not root.is_dir():
        return
    for dirpath, dirnames, _filenames in os.walk(root):
        cur = Path(dirpath)
        of = is_openfoam_case(cur)
        ccx = is_calculix_case(cur)
        if of and not ccx:
            yield cur, "openfoam"
            dirnames[:] = []
        elif ccx and not of:
            yield cur, "calculix"
            dirnames[:] = []
        elif of and ccx:
            # 想定外（両方の指紋が同居）→ 表示しない
            dirnames[:] = []
        # どちらでもなければ通常どおり子ディレクトリを探索する


# --- 解析の種類・解析設定 -------------------------------------------------
def strip_of_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//.*", "", text)
    return text


def parse_of_scalar(text: str, key: str) -> Optional[str]:
    m = re.search(rf"^\s*{re.escape(key)}\s+([^\s;]+)\s*;", text, re.MULTILINE)
    return m.group(1) if m else None


def parse_of_bool(text: str, key: str) -> bool:
    val = parse_of_scalar(text, key)
    return val is not None and val.lower() in ("true", "on", "yes")


def analyze_openfoam(case_dir: Path) -> dict:
    control_dict = case_dir / "system" / "controlDict"
    fv_schemes = case_dir / "system" / "fvSchemes"

    def parse_of_float(text: str, key: str) -> Optional[float]:
        v = parse_of_scalar(text, key)
        try:
            return float(v) if v is not None else None
        except ValueError:
            return None

    application = None
    delta_t = start_time = end_time = None
    adjust_time_step = False
    if control_dict.is_file():
        text = strip_of_comments(control_dict.read_text(encoding="utf-8", errors="replace"))
        application = parse_of_scalar(text, "application")
        adjust_time_step = parse_of_bool(text, "adjustTimeStep")
        delta_t = parse_of_float(text, "deltaT")
        start_time = parse_of_float(text, "startTime")
        end_time = parse_of_float(text, "endTime")

    regime = None
    if fv_schemes.is_file():
        text = strip_of_comments(fv_schemes.read_text(encoding="utf-8", errors="replace"))
        m = re.search(r"ddtSchemes\s*\{([^}]*)\}", text)
        if m:
            default = parse_of_scalar(m.group(1), "default")
            if default:
                regime = "steady" if default == "steadyState" else "transient"

    steps = None
    steps_note = None
    if adjust_time_step:
        steps_note = "可変（自動調整）のため件数は非算出"
    elif delta_t and start_time is not None and end_time is not None and delta_t > 0:
        steps = round((end_time - start_time) / delta_t)

    # 圧縮性: thermophysicalProperties の有無で判定
    compressible = (case_dir / "constant" / "thermophysicalProperties").is_file()

    # 単相/多相: transportProperties に phases (...) があれば多相（interFoam系）
    phase = "single"
    transport = case_dir / "constant" / "transportProperties"
    if transport.is_file():
        t = strip_of_comments(transport.read_text(encoding="utf-8", errors="replace"))
        if re.search(r"^\s*phases\s", t, re.MULTILINE):
            phase = "multi"

    # 層流/乱流 + 乱流モデル名: turbulenceProperties が無ければ層流専用ソルバー（icoFoam等）
    flow_regime = "laminar"
    turbulence_model = None
    turb_props = case_dir / "constant" / "turbulenceProperties"
    if turb_props.is_file():
        t = strip_of_comments(turb_props.read_text(encoding="utf-8", errors="replace"))
        sim_type = parse_of_scalar(t, "simulationType")
        if sim_type and sim_type != "laminar":
            flow_regime = "turbulent"
            turbulence_model = parse_of_scalar(t, "RASModel") or parse_of_scalar(t, "LESModel")

    # 収束判定基準（表示のみ。SIMPLE/PIMPLE の residualControl をそのまま出す）
    convergence_criteria = None
    fv_solution = case_dir / "system" / "fvSolution"
    if fv_solution.is_file():
        t = strip_of_comments(fv_solution.read_text(encoding="utf-8", errors="replace"))
        m = re.search(r"residualControl\s*\{([^}]*)\}", t)
        if m:
            convergence_criteria = " ".join(m.group(1).split())

    # メッシュ生成方法
    mesh_method = []
    if (case_dir / "system" / "blockMeshDict").is_file():
        mesh_method.append("blockMesh")
    if (case_dir / "system" / "snappyHexMeshDict").is_file():
        mesh_method.append("snappyHexMesh")
    if not mesh_method and (case_dir / "constant" / "polyMesh" / "points").is_file():
        mesh_method.append("外部メッシュ（取り込み）")

    # 次元（2D/3D/軸対称）: constant/polyMesh/boundary のパッチtypeから推定
    dimension = None
    boundary_file = case_dir / "constant" / "polyMesh" / "boundary"
    if boundary_file.is_file():
        b = boundary_file.read_text(encoding="utf-8", errors="replace")
        patch_types = set(re.findall(r"type\s+(\w+)\s*;", b))
        if "wedge" in patch_types:
            dimension = "axisymmetric"
        elif "empty" in patch_types:
            dimension = "2d"
        else:
            dimension = "3d"

    return {
        "type": {
            "solver_app": application,
            "regime": regime,
            "compressible": "compressible" if compressible else "incompressible",
            "flow_regime": flow_regime,
            "phase": phase,
        },
        "settings": {
            "delta_t": delta_t,
            "start_time": start_time,
            "end_time": end_time,
            "adjust_time_step": adjust_time_step,
            "steps": steps,
            "steps_note": steps_note,
        },
        "details": {
            "turbulence_model": turbulence_model,
            "convergence_criteria": convergence_criteria,
            "mesh_method": mesh_method or None,
            "dimension": dimension,
        },
    }


CCX_STEP_RE = re.compile(r"^\*STEP\b(.*)$", re.IGNORECASE)
CCX_END_STEP_RE = re.compile(r"^\*END\s*STEP\b", re.IGNORECASE)
CCX_MATERIAL_RE = re.compile(r"^\*MATERIAL\s*,\s*NAME\s*=\s*(\S+)", re.IGNORECASE)
# *MATERIAL 定義の直後～次の*MATERIAL/*STEPまでの範囲に現れうるが、材料
# プロパティ「ではない」と分かっている top-level キーワード。ここに無い
# キーワードは（未知のプロパティ種別の可能性があるため）raw のまま採用する。
CCX_MATERIAL_NON_PROPERTY_KEYWORDS = {
    "BOUNDARY", "ELSET", "NSET", "NODE", "ELEMENT", "INITIAL CONDITIONS",
    "AMPLITUDE", "ORIENTATION", "TRANSFORM", "SURFACE", "TIE", "EQUATION",
    "MPC", "SURFACE INTERACTION", "FRICTION", "RIGID BODY", "SOLID SECTION",
    "SHELL SECTION", "BEAM SECTION", "MEMBRANE SECTION", "PHYSICAL CONSTANTS",
}
# 既知の荷重/境界条件キーワード。ここに無いものは「未知」として raw の
# まま安全側（黙って捨てずに表示）に倒す（*STEP内側のみ・下記参照）。
CCX_LOAD_KEYWORDS = (
    "CLOAD", "DLOAD", "BOUNDARY", "TEMPERATURE", "FILM", "DFLUX", "CFLUX", "RADIATE",
)
# *STEP内に現れるが「荷重条件」ではない既知キーワード（出力要求・手続き系）。
# ここに無い未知キーワードは、荷重条件の可能性があるものとして raw のまま採用する。
CCX_STEP_NON_LOAD_KEYWORDS = {
    "NODE PRINT", "EL PRINT", "NODE FILE", "EL FILE", "NODE OUTPUT", "ELEMENT OUTPUT",
    "CONTACT PRINT", "CONTACT FILE", "CONTACT OUTPUT", "SECTION PRINT", "END STEP",
}


def analyze_calculix(case_dir: Path) -> dict:
    inp_files = list(case_dir.glob("*.inp"))
    if not inp_files:
        return {"type": {}, "settings": {}, "details": {}}
    raw_text = inp_files[0].read_text(encoding="utf-8", errors="replace")
    lines = raw_text.splitlines()

    step_count = 0
    nlgeom = False
    procedures = []
    increment_note = None
    step_ranges = []  # (start_line_idx, end_line_idx, procedure_keyword)

    for i, line in enumerate(lines):
        m = CCX_STEP_RE.match(line.strip())
        if not m:
            continue
        step_count += 1
        if "NLGEOM" in m.group(1).upper():
            nlgeom = True

        # *STEP の次にある、コメント(**)ではない最初の行が手続きキーワード
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("**")):
            j += 1
        proc = None
        if j < len(lines) and lines[j].strip().startswith("*"):
            proc = lines[j].strip().lstrip("*").split(",")[0].strip().upper()
            procedures.append(proc)
            # 手続きキーワードの直後が数値行なら増分制御が明示されている
            k = j + 1
            while k < len(lines) and (not lines[k].strip() or lines[k].strip().startswith("**")):
                k += 1
            if k < len(lines) and lines[k].strip() and not lines[k].strip().startswith("*"):
                increment_note = lines[k].strip()

        # 対応する *END STEP を探す（見つからなければファイル末尾まで）
        end = len(lines)
        for e in range(i + 1, len(lines)):
            if CCX_END_STEP_RE.match(lines[e].strip()):
                end = e
                break
        step_ranges.append((i, end, proc))

    procedures_set = set(procedures)
    # 静解析/動解析/固有値・座屈解析（既知の手続きキーワードにのみ分類し、
    # 未知の手続き（例: *MASS DIFFUSION, *ELECTROMAGNETIC 等）は「不明」に倒す）
    if any(p.startswith("DYNAMIC") for p in procedures_set):
        procedure_class = "dynamic"
    elif procedures_set & {"FREQUENCY", "BUCKLE"}:
        procedure_class = "eigen_buckling"
    elif "STATIC" in procedures_set:
        procedure_class = "static"
    elif procedures_set:
        procedure_class = "unknown"
    else:
        procedure_class = None

    # 熱/構造/熱-構造連成（同様に、未知の手続きは「不明」に倒す）
    if any("TEMPERATURE-DISPLACEMENT" in p for p in procedures_set):
        physics = "thermal_structural"
    elif any("HEAT TRANSFER" in p or "MASS DIFFUSION" in p for p in procedures_set):
        physics = "thermal"
    elif procedure_class in ("static", "dynamic", "eigen_buckling"):
        physics = "structural"
    elif procedures_set:
        physics = "unknown"
    else:
        physics = None

    has_eigen = "FREQUENCY" in procedures_set
    has_buckling = "BUCKLE" in procedures_set
    has_contact = bool(re.search(r"^\*CONTACT PAIR", raw_text, re.IGNORECASE | re.MULTILINE))

    # 材料モデル: *MATERIAL,NAME=xxx から 次の*MATERIAL または *STEP のどちらか
    # 早い方までの範囲を材料定義とみなす（以前は*STEPで区切っておらず、材料が
    # 1つしか無いファイルではファイル末尾までスキャンしてしまうバグがあった）。
    # その範囲内には *BOUNDARY 等、材料プロパティではない top-level キーワードも
    # 混在しうるため、既知のプロパティキーワードは通常どおり採用しつつ、
    # 「材料プロパティでないと分かっている」キーワードでもない未知語は、
    # 黙って捨てずに raw のまま採用する（安全側）。
    materials = []
    mat_starts = [(i, m.group(1)) for i, line in enumerate(lines)
                  if (m := CCX_MATERIAL_RE.match(line.strip()))]
    step_starts = [s for s, _e, _p in step_ranges]
    for idx, (start, name) in enumerate(mat_starts):
        bounds = [s for s in step_starts if s > start]
        if idx + 1 < len(mat_starts):
            bounds.append(mat_starts[idx + 1][0])
        end = min(bounds) if bounds else len(lines)
        props = []
        for line in lines[start + 1:end]:
            s = line.strip()
            if not s.startswith("*") or s.startswith("**"):
                continue
            keyword = s.lstrip("*").split(",")[0].strip().upper()
            if keyword in CCX_MATERIAL_NON_PROPERTY_KEYWORDS:
                continue
            if keyword not in props:
                props.append(keyword)
        materials.append({"name": name, "properties": props})

    # 荷重条件の種類：
    # 1) 既知キーワードはファイル全体（*STEP内外問わず）から拾う
    # 2) *STEP内側にある、既知キーワードでも「出力要求など荷重ではないと
    #    分かっている」キーワードでもない行は、未知の荷重条件の可能性が
    #    あるため raw のまま採用する（黙って捨てない）
    load_types = []
    for line in lines:
        s = line.strip()
        if not s.startswith("*") or s.startswith("**"):
            continue
        keyword = s.lstrip("*").split(",")[0].strip().upper()
        if keyword in CCX_LOAD_KEYWORDS and keyword not in load_types:
            load_types.append(keyword)

    for start, end, proc in step_ranges:
        for line in lines[start + 1:end]:
            s = line.strip()
            if not s.startswith("*") or s.startswith("**"):
                continue
            keyword = s.lstrip("*").split(",")[0].strip().upper()
            if keyword == proc or keyword in CCX_STEP_NON_LOAD_KEYWORDS:
                continue
            if keyword not in load_types:
                load_types.append(keyword)

    # 要素種別（*ELEMENT, TYPE=xxx の一覧）
    element_types = sorted(set(
        m.group(1).upper()
        for m in re.finditer(r"^\*ELEMENT\s*,.*?TYPE\s*=\s*(\S+?)(?:,|\s|$)", raw_text,
                              re.IGNORECASE | re.MULTILINE)
    ))

    # メッシュ形状の表示に対応していない要素タイプ（件数つき）
    mesh_summary = summarize_calculix_elements(inp_files[0])
    mesh_unsupported_elements = [
        {"type": t, "count": n} for t, n in sorted(mesh_summary["unsupported"].items())
    ]

    return {
        "type": {
            "linearity": "nonlinear" if nlgeom else "linear",
            "procedures": sorted(procedures_set),
            "procedure_class": procedure_class,
            "physics": physics,
        },
        "settings": {
            "step_count": step_count,
            "increment": increment_note,  # None の場合は自動（既定値）
        },
        "details": {
            "has_eigen": has_eigen,
            "has_buckling": has_buckling,
            "has_contact": has_contact,
            "materials": materials,
            "load_types": load_types,
            "element_types": element_types,
            "mesh_unsupported_elements": mesh_unsupported_elements,
        },
    }


def latest_mtime(case_dir: Path) -> Optional[float]:
    mtimes = [p.stat().st_mtime for p in case_dir.rglob("*") if p.is_file()]
    return max(mtimes) if mtimes else None


def pick_log(case_dir: Path, candidates):
    files = [p for p in candidates if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


# 例: "ExecutionTime = 7.48 s  ClockTime = 8 s"
OPENFOAM_TIME_RE = re.compile(
    r"ExecutionTime\s*=\s*([\d.eE+-]+)\s*s\s*ClockTime\s*=\s*([\d.eE+-]+)\s*s"
)
# 例: "Total CalculiX Time: 0.010242"
CALCULIX_TIME_RE = re.compile(r"Total CalculiX Time:\s*([\d.eE+-]+)")


def parse_openfoam_timing(text: str) -> Optional[dict]:
    matches = OPENFOAM_TIME_RE.findall(text)
    if not matches:
        return None
    cpu, wall = matches[-1]
    return {"cpu_time_s": float(cpu), "wall_time_s": float(wall)}


def parse_calculix_timing(text: str) -> Optional[dict]:
    matches = CALCULIX_TIME_RE.findall(text)
    if not matches:
        return None
    return {"total_time_s": float(matches[-1])}


def check_openfoam(case_dir: Path):
    logs = [
        p for p in case_dir.glob("log.*")
        if p.name.split(".", 1)[-1] not in OPENFOAM_UTILITY_NAMES
    ]
    log = pick_log(case_dir, logs)
    if log is None:
        return "unknown", "ログファイル（log.<ソルバー名>）が見つかりません", None

    text = log.read_text(encoding="utf-8", errors="replace")
    timing = parse_openfoam_timing(text)
    if last_nonempty_line(text) == "End":
        return "success", None, timing
    for line in text.splitlines():
        if "FOAM FATAL ERROR" in line:
            return "failed", line.strip(), timing
    if "[stack trace]" in text or "Floating Point Exception" in text or "Segmentation" in text:
        return "failed", "予期しないクラッシュで停止しました（" + log.name + " の末尾を参照）", timing
    return "failed", "計算が完了せずに終了しました（" + log.name + " の末尾を参照）", timing


def check_calculix(case_dir: Path):
    logs = list(case_dir.glob("log.ccx")) + list(case_dir.glob("*.log"))
    log = pick_log(case_dir, logs)
    if log is None:
        return "unknown", "ログファイル（log.ccx 等）が見つかりません", None

    text = log.read_text(encoding="utf-8", errors="replace")
    timing = parse_calculix_timing(text)
    if "Job finished" in text:
        return "success", None, timing
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "*ERROR" in line:
            detail_lines = [line.strip()]
            for cont in lines[i + 1:]:
                if cont.strip() and cont.startswith((" ", "\t")):
                    detail_lines.append(cont.strip())
                else:
                    break
            return "failed", " ".join(detail_lines), timing
    return "failed", "計算が完了せずに終了しました（" + log.name + " の末尾を参照）", timing


def build_case_entry(case_dir: Path, solver: str, memos: dict):
    if solver == "openfoam":
        status, detail, timing = check_openfoam(case_dir)
        analysis = analyze_openfoam(case_dir)
    else:
        status, detail, timing = check_calculix(case_dir)
        analysis = analyze_calculix(case_dir)

    mtime = latest_mtime(case_dir)
    run_date = datetime.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else None

    rel = case_dir.relative_to(SCAN_ROOT)
    host_path = str(Path(HOST_ROOT) / rel) if str(rel) != "." else HOST_ROOT
    key = str(rel)

    return {
        "key": key,
        "name": case_dir.name,
        "solver": solver,
        "run_date": run_date,
        "status": status,
        "status_detail": detail,
        "timing": timing,
        "analysis_type": analysis["type"],
        "analysis_settings": analysis["settings"],
        "analysis_details": analysis["details"],
        "path": str(case_dir),
        "host_path": host_path,
        "memo": memos.get(key, ""),
    }


@app.get("/api/cases")
def list_cases():
    memos = load_memos()
    cases = [
        build_case_entry(d, solver, memos)
        for d, solver in find_case_dirs(SCAN_ROOT)
    ]
    cases.sort(key=lambda c: c["run_date"] or "")
    return {"scan_root_host": HOST_ROOT, "count": len(cases), "cases": cases}


class MemoIn(BaseModel):
    key: str
    memo: str


@app.post("/api/memo")
def set_memo(body: MemoIn):
    save_memo(body.key, body.memo)
    return {"ok": True}


def _resolve_case(key: str):
    case_dir = (SCAN_ROOT / key).resolve()
    scan_root_resolved = SCAN_ROOT.resolve()
    if scan_root_resolved not in case_dir.parents and case_dir != scan_root_resolved:
        return None, None
    if not case_dir.is_dir():
        return None, None
    if is_openfoam_case(case_dir) and not is_calculix_case(case_dir):
        return case_dir, "openfoam"
    if is_calculix_case(case_dir) and not is_openfoam_case(case_dir):
        return case_dir, "calculix"
    return None, None


@app.get("/api/cases/{key:path}/thumbnail")
def get_thumbnail(key: str):
    case_dir, solver = _resolve_case(key)
    if case_dir is None:
        raise HTTPException(status_code=404, detail="case not found")

    thumb_path = THUMBNAIL_DIR / (key.replace("/", "__") + ".png")
    if not thumb_path.is_file():
        if solver == "openfoam":
            dimension = analyze_openfoam(case_dir)["details"]["dimension"]
            ok = render_openfoam_mesh(case_dir, thumb_path, dimension=dimension)
        else:
            inp_files = list(case_dir.glob("*.inp"))
            ok = bool(inp_files) and render_calculix_mesh(inp_files[0], thumb_path)
        if not ok:
            raise HTTPException(status_code=422, detail="mesh image could not be generated")

    return FileResponse(thumb_path, media_type="image/png")


# --- 静的ファイル（フロントエンド） --------------------------------------
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
