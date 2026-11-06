#!/usr/bin/env python3
"""
make_homology_model_api.py

API-driven starting-model downloader/predictor for cryo-EM fitting workflows.

Input:
  - FASTA file or raw sequence

Supported providers:
  1. alphafold
     Downloads an existing AlphaFoldDB model using a UniProt accession.
     Best for known UniProt proteins such as human GHSR Q92847.

  2. esmfold
     Calls the public ESMFold/ESM Atlas foldSequence endpoint with the raw sequence.
     This is useful when no UniProt AlphaFoldDB model exists, but the public endpoint
     can change or rate-limit. Failures are recorded in the report.

  3. swissmodel
     Downloads a SWISS-MODEL Repository homology model by UniProt accession.

  4. rcsb_template
     Uses RCSB sequence-search API to find experimental templates and downloads
     the best hit mmCIF entry. This is a template, not a predicted full homology model.

  5. generic
     Calls a user-provided API endpoint using JSON or multipart form.
     Supports immediate responses with model_url/download_url or polling with status_url.

Examples:
  # Human GHSR AlphaFoldDB model from FASTA header containing Q92847
  YOUR_MODEL_MASK_PYTHON_PATH make_homology_model_api.py \
    --fasta GHSR_HUMAN_Q92847_chainA.fasta \
    --provider alphafold \
    --out-dir GHSR_api_model

  # Force a UniProt accession
  python make_homology_model_api.py --fasta seq.fasta --provider alphafold --uniprot Q92847 --out-dir out

  # Try multiple public options
  python make_homology_model_api.py --fasta seq.fasta --provider all --uniprot Q92847 --out-dir out --continue-on-error

  # Download a SWISS-MODEL repository homology model
  python make_homology_model_api.py --fasta seq.fasta --provider swissmodel --uniprot Q92847 --out-dir out

  # Find/download the best experimental RCSB template by sequence
  python make_homology_model_api.py --fasta seq.fasta --provider rcsb_template --out-dir out

  # Generic JSON API: POST {"sequence": "...", "name": "..."}
  python make_homology_model_api.py --fasta seq.fasta --provider generic --api-url https://server/model --out-dir out

Notes:
  - AlphaFoldDB and SWISS-MODEL Repository are database downloads, not new jobs.
  - RCSB template mode downloads experimental templates for homology modeling/reference.
  - ESMFold/generic are prediction API routes.
  - Do not put API tokens directly in shell history if sensitive; prefer environment variables.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import requests
except Exception as e:
    raise SystemExit("ERROR: requests is required. Install with: pip install requests") from e


UNIPROT_RE = re.compile(r"\b(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9][A-Z][A-Z0-9]{2}[0-9](?:-[0-9]+)?)\b")


class ModelApiError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def clean_sequence(seq: str) -> str:
    seq = re.sub(r"[^A-Za-z]", "", seq).upper()
    # Common protein alphabet plus X/U/O/B/Z/J tolerated for some APIs.
    if not seq:
        raise ModelApiError("Input sequence is empty")
    return seq


def parse_fasta_or_sequence(path_or_seq: str) -> Tuple[str, str, Optional[str]]:
    p = Path(path_or_seq).expanduser()
    if p.exists():
        text = p.read_text()
        name = p.stem
    else:
        text = path_or_seq
        name = "query"
    headers: List[str] = []
    seq_parts: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            headers.append(line[1:].strip())
            if len(headers) == 1 and line[1:].strip():
                name = re.sub(r"[^A-Za-z0-9_.-]+", "_", line[1:].strip())[:80]
        else:
            seq_parts.append(line)
    if not seq_parts and not text.lstrip().startswith(">"):
        seq_parts = [text]
    seq = clean_sequence("".join(seq_parts))
    header_text = " ".join(headers)
    accession = infer_uniprot_accession(header_text + " " + name)
    return name, seq, accession


def infer_uniprot_accession(text: str) -> Optional[str]:
    # Prefer accessions after AF- or UniProt-like tokens, but any UniProt-like token works.
    # FASTA headers often contain underscores/pipes, so normalize separators before matching.
    normalized = re.sub(r"[_|;,:()\[\]]+", " ", text)
    m = re.search(r"AF-([A-Z0-9]{6,10})-F\d+", normalized)
    if m:
        return m.group(1)
    matches = UNIPROT_RE.findall(normalized)
    if matches:
        # Strip isoform suffix for AlphaFoldDB canonical query.
        return matches[0].split("-")[0]
    return None


def request_json(url: str, *, headers: Dict[str, str], timeout: int = 120) -> Any:
    r = requests.get(url, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise ModelApiError(f"GET {url} failed HTTP {r.status_code}: {r.text[:1000]}")
    try:
        return r.json()
    except Exception as e:
        raise ModelApiError(f"GET {url} did not return JSON: {r.text[:500]}") from e


def download_file(url: str, out_path: Path, *, headers: Dict[str, str], timeout: int = 300) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log(f"Downloading: {url}")
    r = requests.get(url, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise ModelApiError(f"Download failed HTTP {r.status_code}: {r.text[:1000]}")
    out_path.write_bytes(r.content)
    return out_path.resolve()


def rcsb_entry_title(pdb_id: str, headers: Dict[str, str]) -> str:
    """RCSB entry title for a 4-char PDB ID -- surfaced so a wrong-state template is visible in
    the log/report to whoever reads it later, not just used internally for ranking. Never raises
    (state-title lookup is diagnostic, not load-bearing); returns "" on any failure."""
    if not pdb_id:
        return ""
    try:
        js = request_json(f"https://data.rcsb.org/rest/v1/core/entry/{pdb_id.upper()[:4]}",
                          headers=headers, timeout=30)
        return str((js.get("struct") or {}).get("title") or "")
    except Exception:
        return ""


def choose_model_file(directory: Path) -> Optional[Path]:
    patterns = ["*.cif", "*.pdb", "*.mmcif"]
    candidates: List[Path] = []
    for pat in patterns:
        candidates.extend(sorted(directory.rglob(pat)))
    candidates = [p for p in candidates if p.is_file() and not p.name.startswith(".")]
    if not candidates:
        return None
    relaxed = [p for p in candidates if "relaxed" in p.name.lower()]
    ranked = [p for p in candidates if "rank_001" in p.name.lower() or "rank_1" in p.name.lower()]
    if relaxed:
        return relaxed[0].resolve()
    if ranked:
        return ranked[0].resolve()
    return candidates[0].resolve()


def provider_alphafold(name: str, seq: str, out_dir: Path, accession: str, fmt: str, headers: Dict[str, str]) -> Dict[str, Any]:
    af_dir = out_dir / "alphafold_db"
    af_dir.mkdir(parents=True, exist_ok=True)
    api_url = f"https://alphafold.ebi.ac.uk/api/prediction/{accession}"
    js = request_json(api_url, headers=headers)
    if not isinstance(js, list) or not js:
        raise ModelApiError(f"AlphaFoldDB returned no model for accession {accession}")
    # Usually first item is current/latest. Sort defensively by model version if available.
    def version_key(x: Dict[str, Any]) -> int:
        m = re.search(r"model_v(\d+)", str(x.get("cifUrl", "") or x.get("pdbUrl", "")))
        return int(m.group(1)) if m else 0
    entries = sorted(js, key=version_key, reverse=True)
    entry = entries[0]
    model_url = entry.get("cifUrl") if fmt == "cif" else entry.get("pdbUrl")
    if not model_url:
        model_url = entry.get("cifUrl") or entry.get("pdbUrl")
    if not model_url:
        raise ModelApiError(f"AlphaFoldDB entry has no cifUrl/pdbUrl. Keys: {list(entry.keys())}")
    suffix = ".cif" if ".cif" in model_url.lower() else ".pdb"
    model_path = af_dir / f"AF-{accession}-model{suffix}"
    download_file(model_url, model_path, headers=headers)

    # Save metadata and optional confidence/PAE files when provided.
    metadata_path = af_dir / f"AF-{accession}-metadata.json"
    metadata_path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
    extras: Dict[str, str] = {"metadata_json": str(metadata_path.resolve())}
    for key, label in [("confidenceJsonUrl", "confidence"), ("paeDocUrl", "pae"), ("bcifUrl", "bcif")]:
        url = entry.get(key)
        if url:
            ext = ".json" if "json" in key.lower() or "pae" in key.lower() else Path(str(url).split("?")[0]).suffix or ".dat"
            try:
                extra_path = af_dir / f"AF-{accession}-{label}{ext}"
                download_file(str(url), extra_path, headers=headers)
                extras[f"{label}_file"] = str(extra_path.resolve())
            except Exception as e:
                extras[f"{label}_download_error"] = str(e)

    return {
        "provider": "alphafold",
        "status": "ok",
        "name": name,
        "sequence_length": len(seq),
        "uniprot": accession,
        "api_url": api_url,
        "model_url": model_url,
        "model_path": str(model_path.resolve()),
        "global_metric_value": entry.get("globalMetricValue"),
        "model_created_date": entry.get("modelCreatedDate"),
        **extras,
    }


ESMFOLD_MAX_LEN = 400  # public api.esmatlas.com endpoint hard-rejects longer with HTTP 413


def provider_esmfold(name: str, seq: str, out_dir: Path, headers: Dict[str, str], timeout: int) -> Dict[str, Any]:
    if len(seq) > ESMFOLD_MAX_LEN:
        raise ModelApiError(
            f"sequence is {len(seq)} aa; the public ESMFold endpoint rejects anything over "
            f"{ESMFOLD_MAX_LEN} aa (HTTP 413) -- skipping this provider rather than calling it")
    esm_dir = out_dir / "esmfold"
    esm_dir.mkdir(parents=True, exist_ok=True)
    url = "https://api.esmatlas.com/foldSequence/v1/pdb/"
    log(f"Submitting sequence to ESMFold endpoint: {url}")
    req_headers = dict(headers)
    req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    r = requests.post(url, data=seq, headers=req_headers, timeout=timeout)
    if r.status_code >= 400:
        raise ModelApiError(f"ESMFold failed HTTP {r.status_code}: {r.text[:1000]}")
    text = r.text
    if not text.startswith("ATOM") and "\nATOM" not in text and not text.startswith("HEADER"):
        # Some failures still return 200 with a message.
        raise ModelApiError(f"ESMFold response does not look like PDB: {text[:500]}")
    pdb_path = esm_dir / f"{name}_esmfold.pdb"
    pdb_path.write_text(text)
    return {
        "provider": "esmfold",
        "status": "ok",
        "name": name,
        "sequence_length": len(seq),
        "api_url": url,
        "model_path": str(pdb_path.resolve()),
    }


def provider_swissmodel(name: str, seq: str, out_dir: Path, accession: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """Download a SWISS-MODEL Repository homology model by UniProt accession."""
    sm_dir = out_dir / "swissmodel"
    sm_dir.mkdir(parents=True, exist_ok=True)
    meta_url = f"https://swissmodel.expasy.org/repository/uniprot/{accession}.json"
    pdb_url = f"https://swissmodel.expasy.org/repository/uniprot/{accession}.pdb?provider=swissmodel"
    metadata = request_json(meta_url, headers=headers)
    metadata_path = sm_dir / f"SWISSMODEL-{accession}-metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    model_path = sm_dir / f"SWISSMODEL-{accession}.pdb"
    download_file(pdb_url, model_path, headers=headers)
    if model_path.stat().st_size < 1000:
        raise ModelApiError(f"SWISS-MODEL downloaded file is suspiciously small: {model_path}")
    return {
        "provider": "swissmodel",
        "status": "ok",
        "name": name,
        "sequence_length": len(seq),
        "uniprot": accession,
        "api_url": meta_url,
        "model_url": pdb_url,
        "model_path": str(model_path.resolve()),
        "metadata_json": str(metadata_path.resolve()),
        "note": "SWISS-MODEL Repository homology model; not a newly submitted modeling job.",
    }


def provider_swissmodel_templates(name: str, seq: str, out_dir: Path, accession: str,
                                  headers: Dict[str, str], max_templates: int,
                                  state_keywords: str = "",
                                  target_from: Optional[float] = None,
                                  target_to: Optional[float] = None) -> List[Dict[str, Any]]:
    """Download SEVERAL SWISS-MODEL Repository models, each built on a DIFFERENT template.

    The plain `swissmodel` route takes the repository's default model, which is usually a monomer.
    But a rigid fit fails most often because the starting model has the wrong OLIGOMERIC STATE or
    domain arrangement: an AlphaFold monomer of rat TRPV1 (domain-swapped channel) never fit its
    own 2.81 A map at any CC under three different searchers, while a SWISS-MODEL TETRAMER built
    from the same sequence docked immediately. The repository holds dozens of models per accession
    (67 for O35433), on templates of differing oligomeric state and coverage, so enumerate them and
    hand several back as separate candidates for the docker to try.

    Ranking prefers oligomers (more chains in `in_complex_with` / homo-oligomer state) and higher
    coverage; one model per distinct template.
    """
    sm_dir = out_dir / "swissmodel_templates"
    sm_dir.mkdir(parents=True, exist_ok=True)
    meta_url = f"https://swissmodel.expasy.org/repository/uniprot/{accession}.json"
    metadata = request_json(meta_url, headers=headers)
    (sm_dir / f"SWISSMODEL-{accession}-all-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    structures = (metadata.get("result") or {}).get("structures") or []
    if not structures:
        raise ModelApiError(f"SWISS-MODEL Repository lists no structures for {accession}")

    want = [w.strip().lower() for w in (state_keywords or "").replace(",", " ").split() if w.strip()]
    titles: Dict[str, str] = {}
    if want:
        for st in structures[:20]:                     # only the plausible ones, one REST call each
            t = str(st.get("template") or "")
            if t and t not in titles:
                titles[t] = rcsb_entry_title(t, headers)

    def rank_key(st: Dict[str, Any]):
        oligo = str(st.get("oligo-state") or "")
        n_partners = len(st.get("in_complex_with") or {})
        is_multi = 1 if ("homo" in oligo.lower() or "hetero" in oligo.lower() or n_partners) else 0
        # STATE match first: a template in the wrong conformational/ligand state is the single
        # biggest cause of a failed rigid fit. Measured on TRPV1 (map = DkTx/RTX-bound OPEN state):
        # apo/closed templates gave masked CC 0.23-0.32, the matching 5IRX open-state template gave
        # 0.74 -- same sequence, same residue range, same tetramer, same docking.
        title = titles.get(str(st.get("template") or ""), "").lower()
        hits = sum(1 for w in want if w in title)
        # Then RANGE match: prefer a template whose modelled range matches the region the map
        # resolves. Raw coverage is the WRONG tiebreaker -- it rewards padding the model with
        # flexible regions that have no density (TRPV1 apo templates scored coverage 0.78 by
        # including the disordered ARD, and outranked 5IRX at 0.498, which is exactly the ordered
        # TM+pore region the map shows).
        if target_from is not None and target_to is not None:
            lo, hi = float(st.get("from") or 0), float(st.get("to") or 0)
            overlap = max(0.0, min(hi, target_to) - max(lo, target_from))
            span = max(1.0, (hi - lo))
            target_span = max(1.0, target_to - target_from)
            range_score = (overlap / target_span) - abs(span - target_span) / target_span
        else:
            range_score = float(st.get("coverage") or 0.0)
        return (-hits, -is_multi, -range_score, -float(st.get("coverage") or 0.0))

    seen_templates: set[str] = set()
    results: List[Dict[str, Any]] = []
    for st in sorted(structures, key=rank_key):
        tmpl = str(st.get("template") or "")
        url = st.get("coordinates")
        if not url or (tmpl and tmpl in seen_templates):
            continue
        seen_templates.add(tmpl)
        model_path = sm_dir / f"SWISSMODEL-{accession}-{safe(tmpl) or len(results)}.pdb"
        try:
            download_file(url, model_path, headers=headers)
        except Exception as e:
            log(f"  template {tmpl}: download failed ({e})")
            continue
        if model_path.stat().st_size < 1000:
            continue
        # Surface the RCSB title for every KEPT candidate (not just when --model-state ranking
        # needed it) so a wrong-state template is visible in the log/report to whoever reads it
        # later -- bounded to the few templates actually kept (<=max_templates), not every
        # structure the repository lists.
        if tmpl not in titles:
            titles[tmpl] = rcsb_entry_title(tmpl, headers)
        results.append({
            "provider": "swissmodel_templates",
            "status": "ok",
            "name": name,
            "sequence_length": len(seq),
            "uniprot": accession,
            "template": tmpl,
            "template_title": titles.get(tmpl, ""),
            "oligo_state": st.get("oligo-state"),
            "coverage": st.get("coverage"),
            "residue_range": [st.get("from"), st.get("to")],
            "api_url": meta_url,
            "model_url": url,
            "model_path": str(model_path.resolve()),
            "note": "SWISS-MODEL Repository model on a specific template; oligomers ranked first.",
        })
        log(f"  template {tmpl}: oligo={st.get('oligo-state')} coverage={st.get('coverage')} "
            f"range={st.get('from')}-{st.get('to')} state_hits="
            f"{sum(1 for w in want if w in titles.get(tmpl,'').lower())} "
            f"title={titles.get(tmpl,'')[:60]!r} -> {model_path.name}")
        if len(results) >= max_templates:
            break
    if not results:
        raise ModelApiError(f"No SWISS-MODEL template models could be downloaded for {accession}")
    return results


def safe(text: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(text))[:24]


def provider_rcsb_template(name: str, seq: str, out_dir: Path, headers: Dict[str, str], identity_cutoff: float, evalue_cutoff: float, rows: int) -> Dict[str, Any]:
    """Find experimental templates by RCSB sequence-search API and download best mmCIF entry."""
    rcsb_dir = out_dir / "rcsb_template"
    rcsb_dir.mkdir(parents=True, exist_ok=True)
    search_url = "https://search.rcsb.org/rcsbsearch/v2/query"
    payload = {
        "query": {
            "type": "terminal",
            "service": "sequence",
            "parameters": {
                "evalue_cutoff": evalue_cutoff,
                "identity_cutoff": identity_cutoff,
                "target": "pdb_protein_sequence",
                "value": seq,
            },
        },
        "request_options": {
            "scoring_strategy": "sequence",
            "return_all_hits": False,
            "results_content_type": ["experimental"],
            "paginate": {"start": 0, "rows": rows},
        },
        "return_type": "polymer_entity",
    }
    log(f"Submitting sequence search to RCSB: {search_url}")
    r = requests.post(search_url, json=payload, headers=headers, timeout=120)
    if r.status_code >= 400:
        raise ModelApiError(f"RCSB sequence search failed HTTP {r.status_code}: {r.text[:1000]}")
    js = r.json()
    search_path = rcsb_dir / "rcsb_sequence_search.json"
    search_path.write_text(json.dumps(js, indent=2, sort_keys=True) + "\n")
    hits = js.get("result_set") or []
    if not hits:
        raise ModelApiError("RCSB sequence search returned no experimental templates")
    best = hits[0]
    identifier = str(best.get("identifier", ""))
    if "_" in identifier:
        pdb_id, entity_id = identifier.split("_", 1)
    else:
        pdb_id, entity_id = identifier[:4], None
    pdb_id = pdb_id.upper()
    cif_url = f"https://files.rcsb.org/download/{pdb_id}.cif"
    model_path = rcsb_dir / f"{pdb_id}_template.cif"
    download_file(cif_url, model_path, headers=headers)
    # NOTE: unlike provider_swissmodel_templates, this provider ranks by sequence-search score
    # ONLY (top hit, no --model-state matching) -- surfacing the title here does not fix that,
    # it just makes a wrong-state choice VISIBLE in the log/report instead of silent.
    title = rcsb_entry_title(pdb_id, headers)
    log(f"  rcsb_template top hit {pdb_id}: title={title[:80]!r} (NOTE: this provider does not "
        f"rank by --model-state; identity/e-value score only -- verify the state by eye)")
    return {
        "provider": "rcsb_template",
        "status": "ok",
        "name": name,
        "sequence_length": len(seq),
        "api_url": search_url,
        "model_url": cif_url,
        "model_path": str(model_path.resolve()),
        "pdb_id": pdb_id,
        "template_title": title,
        "polymer_entity_id": entity_id,
        "top_score": best.get("score"),
        "top_identifier": identifier,
        "total_hits": js.get("total_count"),
        "search_json": str(search_path.resolve()),
        "note": "Experimental template downloaded from RCSB; use as homology template/reference, not a predicted full homology model.",
    }


def provider_generic(args: argparse.Namespace, name: str, seq: str, out_dir: Path, headers: Dict[str, str]) -> Dict[str, Any]:
    if not args.api_url:
        raise ModelApiError("--provider generic requires --api-url")
    gen_dir = out_dir / "generic_api"
    gen_dir.mkdir(parents=True, exist_ok=True)
    payload = {"name": name, "sequence": seq}
    log(f"Submitting sequence to generic API: {args.api_url}")
    if args.api_mode == "multipart":
        files = {"sequence": ("sequence.txt", seq), "fasta": (f"{name}.fasta", f">{name}\n{seq}\n")}
        r = requests.post(args.api_url, files=files, headers=headers, timeout=args.api_timeout_s)
    else:
        r = requests.post(args.api_url, json=payload, headers=headers, timeout=args.api_timeout_s)
    if r.status_code >= 400:
        raise ModelApiError(f"Generic API submit failed HTTP {r.status_code}: {r.text[:1000]}")
    try:
        js = r.json()
    except Exception as e:
        raise ModelApiError(f"Generic API did not return JSON. Response: {r.text[:1000]}") from e

    deadline = time.time() + args.api_timeout_s
    while True:
        model_url = js.get("model_url") or js.get("download_url") or js.get("result_url")
        if model_url:
            local_name = Path(str(model_url).split("?")[0]).name or f"{name}_generic_model"
            local = download_file(str(model_url), gen_dir / local_name, headers=headers, timeout=args.api_timeout_s)
            model_path = local
            if local.suffix.lower() == ".zip":
                unpack_dir = gen_dir / "unpacked"
                unpack_dir.mkdir(exist_ok=True)
                with zipfile.ZipFile(local) as z:
                    z.extractall(unpack_dir)
                chosen = choose_model_file(unpack_dir)
                if not chosen:
                    raise ModelApiError(f"Downloaded zip had no PDB/mmCIF model: {local}")
                model_path = chosen
            return {
                "provider": "generic",
                "status": "ok",
                "name": name,
                "sequence_length": len(seq),
                "api_url": args.api_url,
                "model_url": model_url,
                "model_path": str(model_path.resolve()),
                "raw_response": js,
            }
        status_url = js.get("status_url")
        if not status_url:
            raise ModelApiError(f"Generic API response lacks model_url/download_url/status_url. Keys: {list(js.keys())}")
        status = str(js.get("status", "")).lower()
        if status in {"failed", "error", "cancelled"}:
            raise ModelApiError(f"Generic API job failed: {js}")
        if time.time() > deadline:
            raise ModelApiError("Generic API timed out")
        log(f"Polling in {args.poll_s}s: {status_url}")
        time.sleep(args.poll_s)
        rr = requests.get(status_url, headers=headers, timeout=args.api_timeout_s)
        if rr.status_code >= 400:
            raise ModelApiError(f"Generic API poll failed HTTP {rr.status_code}: {rr.text[:1000]}")
        js = rr.json()


def write_fasta(name: str, seq: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks = [seq[i:i+80] for i in range(0, len(seq), 80)]
    path.write_text(f">{name}\n" + "\n".join(chunks) + "\n")


def build_headers(token: Optional[str], token_env: Optional[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {"User-Agent": "make_homology_model_api.py/1.0"}
    actual_token = token
    if token_env:
        actual_token = os.environ.get(token_env)
    if actual_token:
        headers["Authorization"] = f"Bearer {actual_token}"
    return headers


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Make/download protein starting models through AlphaFoldDB, ESMFold, or a generic modeling API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    inp = ap.add_mutually_exclusive_group(required=True)
    inp.add_argument("--fasta", help="Input FASTA file")
    inp.add_argument("--sequence", help="Raw amino-acid sequence string")
    ap.add_argument("--out-dir", required=True, help="Output directory")
    ap.add_argument("--provider", choices=["alphafold", "swissmodel", "swissmodel_templates", "rcsb_template", "esmfold", "generic", "all"], default="alphafold", help="Model provider/API route")
    ap.add_argument("--model-state", default="", help="Conformational/ligand state the model must represent, e.g. 'open DkTx RTX' or 'apo closed'. Matched against RCSB template titles.")
    ap.add_argument("--target-res-from", type=float, default=None, help="First residue the MAP resolves; templates whose modelled range matches this window rank above merely high-coverage ones")
    ap.add_argument("--target-res-to", type=float, default=None, help="Last residue the MAP resolves")
    ap.add_argument("--swissmodel-max-templates", type=int, default=3, help="For --provider swissmodel_templates: how many distinct-template models to fetch [default: 3]")
    ap.add_argument("--uniprot", default=None, help="UniProt accession for AlphaFoldDB. If omitted, script tries to infer from FASTA header/name.")
    ap.add_argument("--alphafold-format", choices=["cif", "pdb"], default="cif", help="Preferred AlphaFoldDB model format")
    ap.add_argument("--rcsb-identity-cutoff", type=float, default=0.30, help="RCSB sequence-search identity cutoff for template search")
    ap.add_argument("--rcsb-evalue-cutoff", type=float, default=1e-3, help="RCSB sequence-search E-value cutoff")
    ap.add_argument("--rcsb-rows", type=int, default=10, help="Number of RCSB sequence-search hits to request")
    ap.add_argument("--api-url", default=None, help="Generic modeling API endpoint")
    ap.add_argument("--api-mode", choices=["json", "multipart"], default="json", help="Generic API POST mode")
    ap.add_argument("--api-token", default=None, help="Bearer token for generic/protected APIs. Prefer --api-token-env for secrets.")
    ap.add_argument("--api-token-env", default=None, help="Environment variable containing API bearer token")
    ap.add_argument("--api-timeout-s", type=int, default=900, help="Timeout/deadline for API submit/download/poll")
    ap.add_argument("--poll-s", type=int, default=30, help="Polling interval for generic APIs")
    ap.add_argument("--continue-on-error", action="store_true", help="For --provider all, keep going if one provider fails")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    name, seq, inferred_acc = parse_fasta_or_sequence(args.fasta or args.sequence or "")
    accession = args.uniprot or inferred_acc
    headers = build_headers(args.api_token, args.api_token_env)

    input_fasta = out_dir / "input_sequence.fasta"
    write_fasta(name, seq, input_fasta)

    providers = [args.provider] if args.provider != "all" else ["alphafold", "swissmodel", "swissmodel_templates", "rcsb_template", "esmfold"]
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    best_model: Optional[str] = None

    for provider in providers:
        try:
            if provider == "alphafold":
                if not accession:
                    raise ModelApiError("AlphaFoldDB needs --uniprot or a UniProt accession in the FASTA header/name")
                result = provider_alphafold(name, seq, out_dir, accession, args.alphafold_format, headers)
            elif provider == "swissmodel_templates":
                if not accession:
                    raise ModelApiError("swissmodel_templates needs --uniprot or a UniProt accession in the FASTA header/name")
                multi = provider_swissmodel_templates(
                    name, seq, out_dir, accession, headers, args.swissmodel_max_templates,
                    state_keywords=args.model_state,
                    target_from=args.target_res_from, target_to=args.target_res_to)
                results.extend(multi)
                result = multi[0]
            elif provider == "swissmodel":
                if not accession:
                    raise ModelApiError("SWISS-MODEL Repository needs --uniprot or a UniProt accession in the FASTA header/name")
                result = provider_swissmodel(name, seq, out_dir, accession, headers)
            elif provider == "rcsb_template":
                result = provider_rcsb_template(name, seq, out_dir, headers, args.rcsb_identity_cutoff, args.rcsb_evalue_cutoff, args.rcsb_rows)
            elif provider == "esmfold":
                result = provider_esmfold(name, seq, out_dir, headers, args.api_timeout_s)
            elif provider == "generic":
                result = provider_generic(args, name, seq, out_dir, headers)
            else:
                raise ModelApiError(f"Unknown provider: {provider}")
            results.append(result)
            if not best_model and result.get("model_path"):
                best_model = str(result["model_path"])
            log(f"Provider {provider} OK: {result.get('model_path')}")
        except Exception as e:
            err = {"provider": provider, "error": str(e)}
            errors.append(err)
            log(f"Provider {provider} FAILED: {e}")
            if args.provider != "all" or not args.continue_on_error:
                # For single provider or strict all, write report then fail.
                report = {
                    "name": name,
                    "sequence_length": len(seq),
                    "input_fasta": str(input_fasta),
                    "inferred_uniprot": inferred_acc,
                    "requested_uniprot": args.uniprot,
                    "results": results,
                    "errors": errors,
                    "best_model": best_model,
                }
                (out_dir / "model_api_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
                raise

    report = {
        "name": name,
        "sequence_length": len(seq),
        "input_fasta": str(input_fasta.resolve()),
        "inferred_uniprot": inferred_acc,
        "requested_uniprot": args.uniprot,
        "provider": args.provider,
        "results": results,
        "errors": errors,
        "best_model": best_model,
    }
    report_path = out_dir / "model_api_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    summary_path = out_dir / "model_api_summary.txt"
    lines = [
        "API model summary",
        "=================",
        f"Name: {name}",
        f"Sequence length: {len(seq)} aa",
        f"Input FASTA saved: {input_fasta.resolve()}",
        f"Inferred UniProt: {inferred_acc or 'NA'}",
        f"Best model: {best_model or 'NA'}",
        f"Report JSON: {report_path.resolve()}",
        "",
        "Provider results:",
    ]
    for r in results:
        lines.append(f"  {r.get('provider')}: OK model={r.get('model_path')} global_metric={r.get('global_metric_value', 'NA')}")
    for e in errors:
        lines.append(f"  {e.get('provider')}: FAILED error={e.get('error')}")
    summary_path.write_text("\n".join(lines) + "\n")

    log("\nDone.")
    log(f"Best model: {best_model}")
    log(f"Report: {report_path.resolve()}")
    log(f"Summary: {summary_path.resolve()}")
    if not best_model:
        return 3
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ModelApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
