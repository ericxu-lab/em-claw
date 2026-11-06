#!/usr/bin/env python3
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np


def run(cmd, env=None, cwd=None):
    print("+", " ".join(map(str, cmd)))
    subprocess.run(cmd, check=True, env=env, cwd=cwd)


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def convert_cs_to_star(cs_path: Path, out_star: Path, csparc2star_bin: str):
    run([csparc2star_bin, str(cs_path), str(out_star)])


def load_cs_metadata(cs_path: Path):
    cs = np.load(cs_path, allow_pickle=True)
    if cs.dtype.names is None:
        raise ValueError(f"Unexpected .cs format: {cs_path}")
    required = {"blob/shape", "blob/psize_A", "blob/res_A"}
    missing = required - set(cs.dtype.names)
    if missing:
        raise ValueError(f"Missing required .cs fields: {sorted(missing)}")
    return cs


def ensure_mrcs_link(mrc_path: Path, out_dir: Path):
    mrcs_path = out_dir / (mrc_path.stem + ".mrcs")
    if mrcs_path.exists() or mrcs_path.is_symlink():
        mrcs_path.unlink()
    os.symlink(mrc_path, mrcs_path)
    return mrcs_path


def patch_converted_star(star_path: Path, mrcs_path: Path):
    txt = star_path.read_text()
    txt = re.sub(r"\d{6}@\S+", lambda m: f"{m.group(0).split('@')[0]}@{mrcs_path}", txt)
    star_path.write_text(txt)


def write_ranker_input(cs, mrcs_path: Path, out_dir: Path, voltage: float, cs_mm: float, amp_contrast: float):
    n = len(cs)
    box = int(cs["blob/shape"][0][0])
    pix = float(cs["blob/psize_A"][0])
    res = cs["blob/res_A"].astype(float)
    particle_diameter = pix * box * 0.75

    data_star = out_dir / "rank_input_data.star"
    model_star = out_dir / "rank_input_model.star"
    opt_star = out_dir / "rank_input_optimiser.star"

    data_lines = [
        "# version 30001",
        "",
        "data_optics",
        "",
        "loop_ ",
        "_rlnOpticsGroupName #1",
        "_rlnOpticsGroup #2",
        "_rlnMtfFileName #3",
        "_rlnMicrographOriginalPixelSize #4",
        "_rlnVoltage #5",
        "_rlnSphericalAberration #6",
        "_rlnAmplitudeContrast #7",
        "_rlnImagePixelSize #8",
        "_rlnImageSize #9",
        "_rlnImageDimensionality #10",
        "_rlnCtfDataAreCtfPremultiplied #11",
        f"opticsGroup1 1 mtf_dummy.star {pix:.6f} {voltage:.6f} {cs_mm:.6f} {amp_contrast:.6f} {pix:.6f} {box} 2 0",
        "",
        "data_particles",
        "",
        "loop_",
        "_rlnImageName #1",
        "_rlnPhaseShift #2",
        "_rlnOpticsGroup #3",
    ]
    data_lines += [f"{i:06d}@{mrcs_path} 0 1" for i in range(1, n + 1)]
    data_star.write_text("\n".join(data_lines) + "\n")

    model_lines = [
        "# version 30001",
        "",
        "data_model_general",
        "",
        "_rlnReferenceDimensionality 2",
        "_rlnDataDimensionality 2",
        f"_rlnOriginalImageSize {box}",
        f"_rlnCurrentResolution {np.nanmedian(res):.6f}",
        f"_rlnCurrentImageSize {box}",
        "_rlnPaddingFactor 1.000000",
        "_rlnIsHelix 0",
        "_rlnFourierSpaceInterpolator 1",
        "_rlnMinRadiusNnInterpolation 10",
        f"_rlnPixelSize {pix:.6f}",
        f"_rlnNrClasses {n}",
        "_rlnNrBodies 1",
        "_rlnNrGroups 1",
        "_rlnNrOpticsGroups 1",
        "_rlnTau2FudgeFactor 2.000000",
        "_rlnNormCorrectionAverage 1.000000",
        "_rlnSigmaOffsetsAngst 1.000000",
        "_rlnOrientationalPriorMode 0",
        "_rlnSigmaPriorRotAngle 0.000000",
        "_rlnSigmaPriorTiltAngle 0.000000",
        "_rlnSigmaPriorPsiAngle 0.000000",
        "_rlnLogLikelihood 0.000000",
        "_rlnAveragePmax 1.000000",
        "",
        "data_model_classes",
        "",
        "loop_ ",
        "_rlnReferenceImage #1",
        "_rlnClassDistribution #2",
        "_rlnAccuracyRotations #3",
        "_rlnAccuracyTranslationsAngst #4",
        "_rlnEstimatedResolution #5",
        "_rlnOverallFourierCompleteness #6",
        "_rlnClassPriorOffsetX #7",
        "_rlnClassPriorOffsetY #8",
    ]
    for i, r in enumerate(res, start=1):
        model_lines.append(
            f"{i:06d}@{mrcs_path} 1.000000 10.000000 2.000000 {float(r):.6f} 1.000000 0.000000 0.000000"
        )
    model_star.write_text("\n".join(model_lines) + "\n")

    opt_star.write_text(
        "\n".join(
            [
                "# version 30001",
                "",
                "data_optimiser_general",
                "",
                f"_rlnOutputRootName {out_dir / 'rank_input'}",
                f"_rlnModelStarFile {model_star}",
                f"_rlnExperimentalDataStarFile {data_star}",
                "_rlnCurrentIteration 1",
                "_rlnNumberOfIterations 1",
                f"_rlnParticleDiameter {particle_diameter:.6f}",
                "_rlnDoCorrectCtf 1",
                "_rlnDoIgnoreCtfUntilFirstPeak 0",
                "_rlnCtfDataArePhaseFlipped 0",
                "_rlnDoOnlyFlipCtfPhases 0",
                "_rlnDoCenterClasses 0",
                "_rlnRefsAreCtfCorrected 1",
            ]
        )
        + "\n"
    )

    return data_star, model_star, opt_star


def ensure_numpy_compat(wrapper_dir: Path, relion_python: str):
    pyfix = wrapper_dir / "pyfix"
    ensure_dir(pyfix)
    test = [
        relion_python,
        "-c",
        "import numpy, torch; import numpy as np; torch.from_numpy(np.zeros((1,1), dtype='float32')); print(numpy.__version__)",
    ]
    ok = subprocess.run(test, capture_output=True, text=True)
    if ok.returncode == 0:
        return None

    run([sys.executable, "-m", "pip", "install", "--target", str(pyfix), "numpy==1.26.4"])
    wrapper = wrapper_dir / "relion_python_classranker"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f'export PYTHONPATH="{pyfix}:${{PYTHONPATH}}"\n'
        f'exec {relion_python} -c "from relion_classranker import main; exit(main())" "$@"\n'
    )
    wrapper.chmod(0o755)
    return wrapper


def run_ranker(opt_star: Path, out_dir: Path, relion_class_ranker: str, relion_python: str, min_score: float):
    wrapper = ensure_numpy_compat(out_dir.parent, relion_python)
    env = os.environ.copy()
    path_entries = []
    if wrapper is not None:
        path_entries.append(str(wrapper.parent))
    path_entries.append(str(Path(relion_class_ranker).parent))
    env["PATH"] = os.pathsep.join(path_entries + [env.get("PATH", "")])
    run(
        [
            relion_class_ranker,
            "--opt",
            str(opt_star),
            "--o",
            str(out_dir),
            "--fn_sel_parts",
            "particles.star",
            "--fn_sel_classavgs",
            "class_averages.star",
            "--fn_root",
            "rank",
            "--do_granularity_features",
            "--auto_select",
            "--min_score",
            str(min_score),
        ],
        env=env,
    )


def render_pngs(mrcs_path: Path, selected_star: Path, original_png: Path, selected_png: Path):
    import mrcfile
    from PIL import Image, ImageDraw, ImageOps

    with mrcfile.open(mrcs_path, permissive=True) as m:
        data = m.data.copy()
    if data.ndim == 2:
        data = data[None, :, :]
    h, w = data.shape[1:]

    selected = []
    for line in selected_star.read_text().splitlines():
        m = re.match(r"(\d+)@", line.strip())
        if m:
            selected.append(int(m.group(1)))

    try:
        resample = Image.Resampling.NEAREST
    except AttributeError:
        resample = Image.NEAREST

    def render(images, labels, out_path, green=False):
        thumbs = []
        for lab, img in zip(labels, images):
            lo, hi = np.percentile(img, [1, 99])
            if hi <= lo:
                hi = lo + 1e-6
            arr = np.clip((img - lo) / (hi - lo), 0, 1)
            arr = (arr * 255).astype("uint8")
            im = Image.fromarray(arr).convert("L")
            im = ImageOps.autocontrast(im)
            scale = 4
            im = im.resize((w * scale, h * scale), resample).convert("RGB")
            if green:
                border = 6
                canvas = Image.new("RGB", (w * scale + 2 * border, h * scale + 24 + 2 * border), (30, 160, 60))
                canvas.paste(im, (border, 24 + border))
                ImageDraw.Draw(canvas).text((border + 2, 4), str(lab), fill=(255, 255, 255))
            else:
                canvas = Image.new("RGB", (w * scale, h * scale + 24), (255, 255, 255))
                canvas.paste(im, (0, 24))
                ImageDraw.Draw(canvas).text((2, 4), str(lab), fill=(0, 0, 0))
            thumbs.append(canvas)
        cols = min(10, int(np.ceil(np.sqrt(len(thumbs)))))
        rows = int(np.ceil(len(thumbs) / cols))
        cellw, cellh = thumbs[0].size
        sheet = Image.new("RGB", (cols * cellw, rows * cellh), (255, 255, 255))
        for idx, im in enumerate(thumbs):
            sheet.paste(im, ((idx % cols) * cellw, (idx // cols) * cellh))
        sheet.save(out_path)

    render(data, list(range(1, len(data) + 1)), original_png, green=False)
    if selected:
        render(data[[i - 1 for i in selected]], selected, selected_png, green=True)


def parse_args():
    p = argparse.ArgumentParser(description="Convert CryoSPARC 2D class averages (.cs) to STAR and run RELION class_ranker.")
    p.add_argument("--cs", required=True, help="Input CryoSPARC .cs file")
    p.add_argument("--mrc", required=True, help="Input class averages MRC stack")
    p.add_argument("--outdir", required=True, help="Output directory")
    p.add_argument("--csparc2star-bin", default=str(Path.home() / ".local/bin/csparc2star.py"))
    p.add_argument("--relion-class-ranker", default=os.environ.get("EMCLAW_RELION_CLASS_RANKER", "YOUR_RELION5_BIN_PATH/relion_class_ranker"))
    p.add_argument("--relion-python", default=os.environ.get("EMCLAW_RELION_PYTHON", "YOUR_RELION5_PYTHON_PATH"))
    p.add_argument("--min-score", type=float, default=0.2)
    p.add_argument("--voltage", type=float, default=300.0)
    p.add_argument("--cs-mm", type=float, default=2.7)
    p.add_argument("--amp-contrast", type=float, default=0.1)
    return p.parse_args()


def main():
    args = parse_args()
    cs_path = Path(args.cs).resolve()
    mrc_path = Path(args.mrc).resolve()
    out_dir = ensure_dir(Path(args.outdir).resolve())
    ranker_out = ensure_dir(out_dir / "ranker_out")

    if not cs_path.exists():
        raise FileNotFoundError(cs_path)
    if not mrc_path.exists():
        raise FileNotFoundError(mrc_path)

    converted_star = out_dir / (cs_path.stem + ".star")
    convert_cs_to_star(cs_path, converted_star, args.csparc2star_bin)

    cs = load_cs_metadata(cs_path)
    mrcs_path = ensure_mrcs_link(mrc_path, out_dir)
    patch_converted_star(converted_star, mrcs_path)
    _, _, opt_star = write_ranker_input(cs, mrcs_path, out_dir, args.voltage, args.cs_mm, args.amp_contrast)

    run_ranker(opt_star, ranker_out, args.relion_class_ranker, args.relion_python, args.min_score)

    render_pngs(
        mrcs_path,
        ranker_out / "class_averages.star",
        out_dir / "original_classes.png",
        ranker_out / "selected_classes.png",
    )

    print("\nOutputs:")
    print("- Converted STAR:", converted_star)
    print("- Ranker selection STAR:", ranker_out / "class_averages.star")
    print("- Ranker model STAR:", ranker_out / "rank_model.star")
    print("- Original PNG:", out_dir / "original_classes.png")
    print("- Selected PNG:", ranker_out / "selected_classes.png")


if __name__ == "__main__":
    main()
