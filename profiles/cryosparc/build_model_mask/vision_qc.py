#!/usr/bin/env python3
"""Reusable ChimeraX render+assess QC helpers for the model-mask docking pipeline.

Two gates, both meant to be READ by an agent/human (this module only renders; it does
not itself judge PASS/FAIL -- that's a vision call):

  render_dock_qc(model, map_mrc, out_dir, chimerax_exe)
      Renders the docked atomic model (cartoon) over the map surface, 3 orientations
      (front/side/top), to a single contact-sheet PNG. Use this to visually confirm a
      rigid dock actually PLACED the model INSIDE the density (not floating outside/in
      the wrong region), before trusting it enough to build a mask from it.

  render_mask_qc(mask_mrc, map_mrc, out_dir, chimerax_exe)
      Renders a semi-transparent mask surface over the map surface, 3 orientations, to a
      single contact-sheet PNG. Use this to confirm a built mask actually covers the
      intended target region (and only that region -- no leakage into an adjacent domain,
      no big empty gaps).

Both require ChimeraX with a working offscreen/OSMesa renderer. On a host whose Mesa
build lacks the expected libLLVM name, the userspace fix is:
    mkdir -p ~/cxlibfix && ln -sf /usr/lib64/libLLVM-3.9.1-mesa.so ~/cxlibfix/libLLVM-3.9-mesa.so
    export LD_LIBRARY_PATH=$HOME/cxlibfix:$LD_LIBRARY_PATH
(exported into the environment BEFORE calling these functions, e.g. by the caller's shell
or by passing it through env=os.environ.copy() -- ChimeraX inherits the parent env).

Both return the path to a single combined PNG (montaged with PIL if available, else the
3 separate per-view PNGs are left on disk and the first is returned) plus the raw ChimeraX
log path, so the caller can Read() the image and log paths for evidence.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


def _run_chimerax(chimerax_exe: str, cxc_path: Path, log_path: Path, cwd: Path) -> str:
    import os
    cwd.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([chimerax_exe, "--nogui", "--offscreen", str(cxc_path)], cwd=str(cwd),
                           env=os.environ.copy(), text=True, capture_output=True)
    text = (proc.stdout or "") + ("\nSTDERR:\n" + proc.stderr if proc.stderr else "")
    log_path.write_text(text)
    return text


def _montage(png_paths: List[Path], out_path: Path, labels: Optional[List[str]] = None) -> Path:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return png_paths[0]
    imgs = [Image.open(p) for p in png_paths if p.exists()]
    if not imgs:
        raise RuntimeError(f"No rendered views found among {png_paths}")
    w = max(im.width for im in imgs)
    h = max(im.height for im in imgs)
    combo = Image.new("RGB", (w * len(imgs), h), "black")
    draw = ImageDraw.Draw(combo)
    for i, im in enumerate(imgs):
        combo.paste(im, (i * w, 0))
        if labels and i < len(labels):
            draw.text((i * w + 8, 8), labels[i], fill="yellow")
    combo.save(out_path)
    return out_path


def render_dock_qc(
    *,
    model_pdb: Path,
    map_mrc: Path,
    out_dir: Path,
    chimerax_exe: str,
    map_level: Optional[float] = None,
    map_is_mask_multiplied: bool = True,
) -> Tuple[Path, Path]:
    """Render the docked model over the map surface, 3 views. Returns (png, log)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    views = [
        ("front", "view orient"),
        ("side", "view orient; turn y 90"),
        ("top", "view orient; turn x 90"),
    ]
    png_paths = []
    lines = [
        f"open {map_mrc}",
        f"open {model_pdb}",
        "volume #1 style surface",
        (f"volume #1 level {map_level}" if map_level is not None else "volume #1 level 0.02"),
        "color #1 gray transparency 55",
        "color #2 byhetero",
        "cartoon #2",
        "hide #2 atoms",
        "lighting soft",
        "set bgColor white",
    ]
    for name, cmd in views:
        png = out_dir / f"dock_qc_{name}.png"
        lines.append(cmd)
        lines.append(f"save {png} width 900 height 900 supersample 3")
        png_paths.append(png)
    lines.append("exit")
    cxc = out_dir / "dock_qc.cxc"
    cxc.write_text("\n".join(lines) + "\n")
    log_path = out_dir / "dock_qc.log"
    _run_chimerax(chimerax_exe, cxc, log_path, out_dir)
    montage = out_dir / "dock_qc_montage.png"
    _montage(png_paths, montage, labels=[n for n, _ in views])
    return montage, log_path


def render_mask_qc(
    *,
    mask_mrc: Path,
    map_mrc: Path,
    out_dir: Path,
    chimerax_exe: str,
    map_level: Optional[float] = None,
) -> Tuple[Path, Path]:
    """Render a mask surface (transparent) over the map surface, 3 views. Returns (png, log)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    views = [
        ("front", "view orient"),
        ("side", "view orient; turn y 90"),
        ("top", "view orient; turn x 90"),
    ]
    png_paths = []
    lines = [
        f"open {map_mrc}",
        f"open {mask_mrc}",
        "volume #1 style surface",
        (f"volume #1 level {map_level}" if map_level is not None else "volume #1 level 0.02"),
        "color #1 gray",
        "volume #2 style surface level 0.5",
        "color #2 red transparency 65",
        "lighting soft",
        "set bgColor white",
    ]
    for name, cmd in views:
        png = out_dir / f"mask_qc_{name}.png"
        lines.append(cmd)
        lines.append(f"save {png} width 900 height 900 supersample 3")
        png_paths.append(png)
    lines.append("exit")
    cxc = out_dir / "mask_qc.cxc"
    cxc.write_text("\n".join(lines) + "\n")
    log_path = out_dir / "mask_qc.log"
    _run_chimerax(chimerax_exe, cxc, log_path, out_dir)
    montage = out_dir / "mask_qc_montage.png"
    _montage(png_paths, montage, labels=[n for n, _ in views])
    return montage, log_path
