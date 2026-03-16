#!/usr/bin/env python3
"""
Analyze TypeScript (.ts) files and build a .drawio dependency diagram.

For every .ts file the script reports:
1) file name
2) number of exported elements
3) number of imported elements
4) number of declared classes/consts/interfaces

A directed edge A -> B is created when file A imports declarations that are
exported from file B.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple
from xml.sax.saxutils import escape

IMPORT_RE = re.compile(r"^\s*import\s+(.+?)\s+from\s+['\"](.+?)['\"]\s*;?", re.MULTILINE)
SIDE_EFFECT_IMPORT_RE = re.compile(r"^\s*import\s+['\"](.+?)['\"]\s*;?", re.MULTILINE)
EXPORT_LIST_RE = re.compile(r"^\s*export\s*\{([^}]*)\}\s*;?", re.MULTILINE)
EXPORT_FROM_RE = re.compile(r"^\s*export\s*\{([^}]*)\}\s*from\s+['\"](.+?)['\"]\s*;?", re.MULTILINE)
EXPORT_DECL_RE = re.compile(
    r"^\s*export\s+(?:abstract\s+)?(?:class|interface|const|let|var|function|type|enum)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
DEFAULT_EXPORT_DECL_RE = re.compile(
    r"^\s*export\s+default\s+(?:abstract\s+)?(?:class|function)\s*([A-Za-z_$][\w$]*)?",
    re.MULTILINE,
)
DECL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:abstract\s+)?(class|interface|const)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


@dataclass
class ImportInfo:
    module: str
    default: str | None = None
    namespace: str | None = None
    named: Set[str] = field(default_factory=set)

    @property
    def imported_count(self) -> int:
        count = 0
        if self.default:
            count += 1
        if self.namespace:
            count += 1
        count += len(self.named)
        return count


@dataclass
class FileInfo:
    path: Path
    rel_path: str
    declarations: Set[str] = field(default_factory=set)
    exports: Set[str] = field(default_factory=set)
    has_default_export: bool = False
    imports: List[ImportInfo] = field(default_factory=list)

    @property
    def import_count(self) -> int:
        return sum(imp.imported_count for imp in self.imports)


def strip_comments(code: str) -> str:
    code = re.sub(r"//.*?$", "", code, flags=re.MULTILINE)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
    return code


def parse_import_clause(clause: str, module: str) -> ImportInfo:
    clause = clause.strip()
    info = ImportInfo(module=module)

    # import Default, { A, B as C } from '...'
    # import { A } from '...'
    # import * as NS from '...'
    if clause.startswith("{"):
        named_part = clause
        default_part = ""
    else:
        parts = [p.strip() for p in clause.split(",", 1)]
        default_part = parts[0] if parts else ""
        named_part = parts[1] if len(parts) > 1 else ""

    if default_part and not default_part.startswith("{") and not default_part.startswith("*"):
        info.default = default_part

    if named_part:
        named_part = named_part.strip()
        if named_part.startswith("*"):
            m = re.match(r"\*\s+as\s+([A-Za-z_$][\w$]*)", named_part)
            if m:
                info.namespace = m.group(1)
        elif named_part.startswith("{") and named_part.endswith("}"):
            body = named_part[1:-1].strip()
            if body:
                for raw in body.split(","):
                    raw = raw.strip()
                    if not raw:
                        continue
                    # A as B -> import name is A
                    left = raw.split(" as ")[0].strip()
                    if left:
                        info.named.add(left)
    return info


def parse_file(path: Path, root: Path) -> FileInfo:
    code = path.read_text(encoding="utf-8")
    cleaned = strip_comments(code)
    rel = str(path.relative_to(root))
    info = FileInfo(path=path, rel_path=rel)

    for _, name in DECL_RE.findall(cleaned):
        info.declarations.add(name)

    for name in EXPORT_DECL_RE.findall(cleaned):
        info.exports.add(name)

    for match in DEFAULT_EXPORT_DECL_RE.finditer(cleaned):
        info.has_default_export = True
        name = match.group(1)
        if name:
            info.exports.add(name)

    for body in EXPORT_LIST_RE.findall(cleaned):
        for raw in body.split(","):
            raw = raw.strip()
            if not raw:
                continue
            parts = [p.strip() for p in raw.split(" as ")]
            local_name = parts[0]
            if local_name:
                info.exports.add(local_name)

    for body, _mod in EXPORT_FROM_RE.findall(cleaned):
        for raw in body.split(","):
            raw = raw.strip()
            if not raw:
                continue
            local_name = raw.split(" as ")[0].strip()
            if local_name:
                info.exports.add(local_name)

    for clause, module in IMPORT_RE.findall(cleaned):
        info.imports.append(parse_import_clause(clause, module))

    for module in SIDE_EFFECT_IMPORT_RE.findall(cleaned):
        if any(imp.module == module for imp in info.imports):
            continue
        info.imports.append(ImportInfo(module=module))

    return info


def resolve_module(importer: Path, module: str, ts_files_by_path: Dict[Path, FileInfo]) -> Path | None:
    if not module.startswith("."):
        return None

    base = importer.parent
    candidates = [
        (base / module).with_suffix(".ts"),
        base / module / "index.ts",
    ]

    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in ts_files_by_path:
            return candidate
    return None


def color_for_key(key: str) -> str:
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    hue = int(digest[:6], 16) % 360
    sat = 55
    light = 72
    return f"hsl({hue},{sat}%,{light}%)"


def text_size(lines: List[str]) -> Tuple[int, int]:
    max_chars = max((len(x) for x in lines), default=10)
    width = max(220, int(max_chars * 6.6) + 30)
    height = max(100, len(lines) * 18 + 24)
    return width, height


def build_diagram(files: List[FileInfo], edges: Set[Tuple[str, str]], out: Path) -> None:
    incoming: Dict[str, int] = {f.rel_path: 0 for f in files}
    for _src, dst in edges:
        incoming[dst] += 1

    sorted_files = sorted(files, key=lambda x: x.rel_path)
    cols = max(1, math.ceil(math.sqrt(len(sorted_files) or 1)))
    x_gap, y_gap = 80, 80
    x0, y0 = 40, 40

    nodes_xml = []
    node_ids: Dict[str, str] = {}
    dimensions: Dict[str, Tuple[int, int]] = {}

    for idx, f in enumerate(sorted_files):
        node_id = f"n{idx + 1}"
        node_ids[f.rel_path] = node_id
        line_items = [
            f"{f.rel_path}",
            f"exports: {len(f.exports) + (1 if f.has_default_export and 'default' not in f.exports else 0)}",
            f"imports: {f.import_count}",
            f"declared: {len(f.declarations)}",
        ]
        base_w, base_h = text_size(line_items)
        scale = incoming[f.rel_path]
        width = base_w + scale * 10
        height = base_h + scale * 8
        dimensions[f.rel_path] = (width, height)

    col_widths = [0 for _ in range(cols)]
    row_count = math.ceil(len(sorted_files) / cols)
    row_heights = [0 for _ in range(row_count)]

    for idx, f in enumerate(sorted_files):
        row = idx // cols
        col = idx % cols
        width, height = dimensions[f.rel_path]
        col_widths[col] = max(col_widths[col], width)
        row_heights[row] = max(row_heights[row], height)

    col_offsets = []
    current_x = x0
    for w in col_widths:
        col_offsets.append(current_x)
        current_x += w + x_gap

    row_offsets = []
    current_y = y0
    for h in row_heights:
        row_offsets.append(current_y)
        current_y += h + y_gap

    for idx, f in enumerate(sorted_files):
        row = idx // cols
        col = idx % cols
        width, height = dimensions[f.rel_path]
        x = col_offsets[col]
        y = row_offsets[row]

        line_items = [
            f"{f.rel_path}",
            f"exports: {len(f.exports) + (1 if f.has_default_export and 'default' not in f.exports else 0)}",
            f"imports: {f.import_count}",
            f"declared: {len(f.declarations)}",
        ]
        value = escape("&#xa;".join(line_items))
        fill = color_for_key(f.rel_path)
        style = (
            "rounded=1;whiteSpace=wrap;html=1;"
            f"fillColor={fill};strokeColor=#333333;fontSize=12;"
        )

        nodes_xml.append(
<<<<<<< codex/create-typescript/python-script-for-code-connectivity-analys-ku6tcy
            f'<mxCell id="{node_ids[f.rel_path]}" value="{value}" style="{style}" vertex="1" parent="1">'
=======
            f'<mxCell id="{node_id}" value="{value}" style="{style}" vertex="1" parent="1">'
>>>>>>> main
            f'<mxGeometry x="{x}" y="{y}" width="{width}" height="{height}" as="geometry"/>'
            "</mxCell>"
        )

    edges_xml = []
    for i, (src, dst) in enumerate(sorted(edges), start=1):
        edge_id = f"e{i}"
        stroke = color_for_key(dst)
        style = f"endArrow=classic;html=1;rounded=0;strokeColor={stroke};"
        edges_xml.append(
            f'<mxCell id="{edge_id}" style="{style}" edge="1" parent="1" source="{node_ids[src]}" target="{node_ids[dst]}">'
            '<mxGeometry relative="1" as="geometry"/>'
            "</mxCell>"
        )

    nodes_joined = "\n        ".join(nodes_xml)
    edges_joined = "\n        ".join(edges_xml)

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mxfile host="app.diagrams.net" modified="2026-01-01T00:00:00.000Z" agent="ts-connectivity-script" version="24.7.1">\n'
        '  <diagram id="ts-connectivity" name="TypeScript Connectivity">\n'
        '    <mxGraphModel dx="2000" dy="1200" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="3000" pageHeight="2000" math="0" shadow="0">\n'
        '      <root>\n'
        '        <mxCell id="0"/>\n'
        '        <mxCell id="1" parent="0"/>\n'
        f"        {nodes_joined}\n"
        f"        {edges_joined}\n"
        '      </root>\n'
        '    </mxGraphModel>\n'
        '  </diagram>\n'
        '</mxfile>\n'
    )

    out.write_text(xml, encoding="utf-8")


def discover_default_root() -> Path:
    script_dir = Path(__file__).resolve().parent
    try:
        top = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=script_dir,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if top:
            return Path(top)
    except Exception:
        pass
    return script_dir


def collect_files(root: Path) -> List[Path]:
    excluded_dirs = {
        ".git",
        "node_modules",
        "dist",
        "coverage",
        ".angular",
        ".nx",
        "tmp",
        "temp",
        "out",
        "build",
    }

    found: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames[:] = [d for d in dirnames if d not in excluded_dirs]
        for name in filenames:
            lower = name.lower()
            if lower.endswith(".ts") and not lower.endswith(".d.ts"):
                found.append((Path(dirpath) / name).resolve())

    return sorted(set(found))


def find_angular_roots(search_root: Path) -> List[Path]:
    matches: List[Path] = []
    for candidate in search_root.glob("**/angular.json"):
        if ".git" in candidate.parts or "node_modules" in candidate.parts:
            continue
        matches.append(candidate.parent.resolve())
    return sorted(set(matches))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build draw.io diagram for TS connectivity")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root directory. Defaults to git root (or script directory).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("ts-connectivity.drawio"),
        help="Output .drawio file path",
    )
    args = parser.parse_args()

    root = (args.root.resolve() if args.root else discover_default_root())
    ts_paths = collect_files(root)

    scan_roots = [root]
    if not ts_paths:
        angular_roots = find_angular_roots(root)
        for angular_root in angular_roots:
            for p in collect_files(angular_root):
                ts_paths.append(p)
        ts_paths = sorted(set(ts_paths))
        if ts_paths:
            scan_roots = sorted(set([root] + angular_roots))

    infos = [parse_file(p, root) for p in ts_paths]
    by_abs = {i.path.resolve(): i for i in infos}

    edges: Set[Tuple[str, str]] = set()

    for f in infos:
        importer_abs = f.path.resolve()
        for imp in f.imports:
            target_path = resolve_module(importer_abs, imp.module, by_abs)
            if not target_path:
                continue
            target = by_abs[target_path]

            matched = False
            if imp.default and target.has_default_export:
                matched = True

            if imp.namespace and target.exports:
                matched = True

            if imp.named:
                for symbol in imp.named:
                    if symbol in target.declarations and symbol in target.exports:
                        matched = True
                        break

            if matched and f.rel_path != target.rel_path:
                edges.add((f.rel_path, target.rel_path))

    connected_paths: Set[str] = set()
    for src, dst in edges:
        connected_paths.add(src)
        connected_paths.add(dst)

    rendered_infos = [f for f in infos if f.rel_path in connected_paths]

    build_diagram(rendered_infos, edges, args.output.resolve())

    print(f"Scan root: {root}")
    if len(scan_roots) > 1:
        print("Fallback Angular roots:")
        for r in scan_roots[1:]:
            print(f"  - {r}")
    print(f"Analyzed {len(infos)} TypeScript files")
    print(f"Found {len(edges)} dependency edges")
    print(f"Rendered {len(rendered_infos)} connected blocks")
    print(f"Wrote diagram: {args.output}")


if __name__ == "__main__":
    main()
