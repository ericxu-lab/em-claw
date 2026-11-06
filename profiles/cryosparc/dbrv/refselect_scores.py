"""Recover `reference_select_2D` per-class Sobel/correlation scores WITHOUT pdftotext.

CryoSPARC reports the per-class scores only inside the matplotlib PDFs embedded in the job's gridfs
blob (they are NOT in templates_selected.cs, which carries only res_A). The reference implementation
(an internal debugging notebook, `debug_cryosparc_pipeline_split.ipynb`, cell 39) shells out to
`pdftotext`; that binary and every python PDF library are absent on this cluster, so we decode the
PDFs directly.

matplotlib emits text as **Type3 glyph procedures**, so no literal strings exist in the content
stream — the bytes in a TJ array are glyph CODES that must be mapped through each font's
/Encoding /Differences array to PostScript glyph names, then to characters. That is what this module
does.

Usage:
    from dbrv.refselect_scores import class_scores, filter_by_corr
    rows = class_scores(proj, 'J1103')          # [{class_id, n_ptcls, sobel, corr}, ...]
    bad  = [r['class_id'] for r in rows if r['corr'] > 1.1]
"""

from __future__ import annotations

import re
import zlib
import tempfile
from pathlib import Path

# PostScript glyph name -> character, for the glyphs that appear in these captions.
_GLYPH = {
    'space': ' ', 'colon': ':', 'period': '.', 'comma': ',', 'hyphen': '-',
    'parenleft': '(', 'parenright': ')', 'slash': '/', 'percent': '%',
    'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
    'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
}
for _c in 'abcdefghijklmnopqrstuvwxyz':
    _GLYPH[_c] = _c
for _c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
    _GLYPH[_c] = _c


def _split_pdfs(blob: bytes):
    """Yield each %PDF...%%EOF span embedded in a gridfs blob."""
    out, start = [], 0
    while True:
        s = blob.find(b'%PDF', start)
        if s == -1:
            break
        e = blob.find(b'%%EOF', s)
        if e == -1:
            break
        e += 5
        out.append(blob[s:e])
        start = e
    return out


def _inflate_streams(pdf: bytes):
    for m in re.finditer(rb'stream\r?\n(.*?)endstream', pdf, re.S):
        raw = m.group(1)
        try:
            yield zlib.decompress(raw)
        except Exception:
            yield raw


def _encodings(pdf: bytes):
    """{font_resource_name: {code: char}} from each font's /Encoding /Differences.

    Both the object number and the /Name the content stream uses are needed, so map by whichever we
    can recover; matplotlib names its font resources /F1, /F2, ... inside /Resources /Font.
    """
    # object number -> {code: char}
    by_obj = {}
    for m in re.finditer(rb'(\d+)\s+0\s+obj(.*?)endobj', pdf, re.S):
        objnum, body = int(m.group(1)), m.group(2)
        dm = re.search(rb'/Differences\s*\[(.*?)\]', body, re.S)
        if not dm:
            continue
        table, code = {}, 0
        for tok in re.findall(rb'(\d+)|/([A-Za-z0-9_.]+)', dm.group(1)):
            num, name = tok
            if num:
                code = int(num)
            else:
                ch = _GLYPH.get(name.decode('latin-1'))
                if ch is not None:
                    table[code] = ch
                code += 1
        if table:
            by_obj[objnum] = table

    # resource name -> object number  (e.g. /F1 7 0 R)
    by_name = {}
    for m in re.finditer(rb'/(F\d+)\s+(\d+)\s+0\s+R', pdf):
        name, objnum = m.group(1).decode('latin-1'), int(m.group(2))
        if objnum in by_obj:
            by_name[name] = by_obj[objnum]
    if not by_name and by_obj:                     # single unnamed font
        by_name['__only__'] = next(iter(by_obj.values()))
    return by_name


def _decode_text(pdf: bytes) -> str:
    """Content-stream text, decoding Type3 glyph codes through the font encodings."""
    fonts = _encodings(pdf)
    if not fonts:
        return ''
    fallback = next(iter(fonts.values()))
    pieces = []
    for data in _inflate_streams(pdf):
        cur = fallback
        # walk font-selection and text-showing operators in order
        for m in re.finditer(rb'/(F\d+)\s+[\d.]+\s+Tf|\[(.*?)\]\s*TJ|\(((?:\\.|[^\\()])*)\)\s*Tj',
                             data, re.S):
            if m.group(1):
                cur = fonts.get(m.group(1).decode('latin-1'), cur)
                continue
            body = m.group(2) if m.group(2) is not None else m.group(3)
            if body is None:
                continue
            for lit in re.findall(rb'\((?:\\.|[^\\()])*\)', body if m.group(2) is not None
                                  else b'(' + body + b')'):
                s = lit[1:-1]
                s = re.sub(rb'\\([()\\])', rb'\1', s)
                s = re.sub(rb'\\(\d{1,3})', lambda o: bytes([int(o.group(1), 8)]), s)
                pieces.append(''.join(cur.get(b, '') for b in s))
            pieces.append(' ')
    return ''.join(pieces)


def _parse(text: str):
    """Rows from a montage caption. The caption format depends on `select_mode`:

    * 'thresholds on Sobel scores' (the DEFAULT) -> "Class N: M ptcls" + "S: <sobel> C: <corr>".
      `corr` here is the score the corr>1.1 bright-blob filter keys on.
    * 'thresholds only' -> "Pearson: <p> HOG: <h>" and NO class/ptcl line, so there is no `corr`
      to filter on. Parsed anyway (as pearson/hog) so the caller can detect the wrong mode.
    """
    cls = re.findall(r'Class\s+(\d+)\s*:\s*(\d+)\s*ptcls', text)
    sc = re.findall(r'S:\s*([0-9.]+)\s*C:\s*([0-9.]+)', text)
    if cls and sc and len(cls) == len(sc):
        return [{'class_id': int(a), 'n_ptcls': int(b), 'sobel': float(s), 'corr': float(c)}
                for (a, b), (s, c) in zip(cls, sc)]
    if sc:                                          # scores without the class line
        return [{'class_id': i, 'n_ptcls': None, 'sobel': float(s), 'corr': float(c)}
                for i, (s, c) in enumerate(sc)]
    ph = re.findall(r'Pearson:\s*([0-9.]+)\s*HOG:\s*([0-9.]+)', text)
    if ph:
        return [{'class_id': i, 'n_ptcls': None, 'pearson': float(p), 'hog': float(h)}
                for i, (p, h) in enumerate(ph)]
    return []


def class_scores(proj, job_uid, expect_n=None, verbose=True):
    """Per-class score rows for a reference_select_2D job.

    `expect_n`: if given (normally len(templates_selected)), prefer the PDF whose row count matches —
    that is the SELECTED-class montage rather than the all-class one.
    """
    job = proj.find_job(job_uid)
    if expect_n is None:
        try:
            expect_n = len(job.load_output('templates_selected'))
        except Exception:
            expect_n = None

    best, allrows = None, []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for i in range(4):
            rel = f'gridfs_data/gridfsdata_{i}'
            loc = td / f'g{i}'
            try:
                job.download_file(rel, loc)
            except Exception:
                continue
            for k, pdf in enumerate(_split_pdfs(loc.read_bytes())):
                rows = _parse(_decode_text(pdf))
                if not rows:
                    continue
                if verbose:
                    have = [r['corr'] for r in rows if 'corr' in r]
                    rng = (f'corr {min(have):.3f}-{max(have):.3f}' if have
                           else 'NO corr (mode is not "thresholds on Sobel scores")')
                    print(f'  {job_uid} {rel} pdf[{k}]: {len(rows)} classes, {rng}')
                allrows.append(rows)
                if expect_n is not None and len(rows) == expect_n:
                    best = rows
    if best is not None:
        return best
    if allrows:
        return max(allrows, key=len)
    raise RuntimeError(f'could not recover class scores from {job_uid} gridfs PDFs')


def filter_by_corr(cs, proj, ws, sel_uid, corr_max=1.1, verbose=True):
    """Drop the bright-blob classes from a reference_select_2D job and materialise the rest.

    Step 4's junk filter. `corr > corr_max` is the ice / high-contrast featureless blob signature —
    such classes correlate with everything and poison any downstream matching. 1.1 is the value
    validated in the reference pipeline; on hTA1-Gs it removed exactly one class (corr 1.73, 21,392
    particles) which an independent blind visual read had flagged for the same reason.

    Returns dict(job_uids, bad_classes, n_kept, n_total, rows).

    Requires the job to have been run in the DEFAULT `select_mode` ("thresholds on Sobel scores").
    In `'thresholds only'` mode the report carries Pearson/HOG and NO `corr`, so there is nothing to
    threshold — this raises rather than silently keeping everything.
    """
    from .particles import save_particles
    import numpy as np

    job = proj.find_job(sel_uid)
    templates = job.load_output('templates_selected')
    rows = class_scores(proj, sel_uid, expect_n=len(templates), verbose=verbose)

    if not rows or 'corr' not in rows[0]:
        raise RuntimeError(
            f'{sel_uid}: report has no `corr` — the job was not run in the default '
            f'"thresholds on Sobel scores" mode, so the corr>{corr_max} filter cannot be applied.')
    if len(rows) != len(templates):
        raise RuntimeError(
            f'{sel_uid}: parsed {len(rows)} class scores but templates_selected has '
            f'{len(templates)} rows — score/montage pairing is wrong; filtering would drop the '
            f'WRONG classes. Refusing to proceed.')

    bad = sorted(r['class_id'] for r in rows if r['corr'] > corr_max)
    parts = job.load_output('particles_selected')
    cls = np.asarray(parts['alignments2D/class'])
    keep = ~np.isin(cls, bad)
    kept = parts.mask(keep) if hasattr(parts, 'mask') else parts.take(np.flatnonzero(keep))

    if verbose:
        worst = max(r['corr'] for r in rows)
        print(f'  {sel_uid}: corr max {worst:.2f} · dropping {len(bad)} class(es) {bad} · '
              f'particles {len(kept):,}/{len(parts):,}')
    uids = save_particles(cs, proj, ws, kept, f'{sel_uid} corr<={corr_max}',
                          parent_uid=sel_uid, parent_group='particles_selected', verbose=verbose)
    return dict(job_uids=uids, bad_classes=bad, n_kept=len(kept), n_total=len(parts), rows=rows)
