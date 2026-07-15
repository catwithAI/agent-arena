from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from hashlib import sha256
from pathlib import Path


class PreviewError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    h = sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_pptx(path: Path) -> None:
    if not path.is_file():
        raise PreviewError(f"file not found: {path}")
    if path.suffix.lower() != ".pptx":
        raise PreviewError(f"expected .pptx: {path.name}")
    if not path.read_bytes()[:4].startswith(b"PK\x03\x04"):
        raise PreviewError("not an OOXML zip file")
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or not any(name.startswith("ppt/slides/") for name in names):
                raise PreviewError("not a valid PowerPoint OOXML package")
            bad = archive.testzip()
            if bad:
                raise PreviewError(f"zip content is corrupt: {bad}")
    except zipfile.BadZipFile as exc:
        raise PreviewError("not a valid zip file") from exc


def ensure_pdf_preview(source: Path, cache_dir: Path, timeout: int = 90) -> Path:
    validate_pptx(source)
    source = source.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{source.stem}.{source.stat().st_size}.{file_sha256(source)[:16]}.pdf"
    if target.is_file() and target.stat().st_size > 0:
        return target

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise PreviewError("LibreOffice/soffice not found")

    with tempfile.TemporaryDirectory(prefix="office-preview-", dir=cache_dir) as tmp:
        tmp_dir = Path(tmp)
        profile_dir = tmp_dir / "lo-profile"
        out_dir = tmp_dir / "out"
        xdg_dir = tmp_dir / "xdg"
        profile_dir.mkdir()
        out_dir.mkdir()
        xdg_dir.mkdir()
        env = os.environ.copy()
        env["HOME"] = str(xdg_dir)
        env["XDG_CONFIG_HOME"] = str(xdg_dir / "config")
        env["XDG_CACHE_HOME"] = str(xdg_dir / "cache")
        env["XDG_RUNTIME_DIR"] = str(xdg_dir / "runtime")
        env["SAL_USE_VCLPLUGIN"] = "svp"
        env["GIO_USE_VFS"] = "local"
        env["NO_AT_BRIDGE"] = "1"
        env["DBUS_SESSION_BUS_ADDRESS"] = ""
        Path(env["XDG_CONFIG_HOME"]).mkdir()
        Path(env["XDG_CACHE_HOME"]).mkdir()
        Path(env["XDG_RUNTIME_DIR"]).mkdir()
        os.chmod(env["XDG_RUNTIME_DIR"], 0o700)
        input_path = tmp_dir / "source.pptx"
        shutil.copy2(source, input_path)
        result = subprocess.run(
            [
                soffice,
                "--headless",
                "--nologo",
                "--nofirststartwizard",
                "--nodefault",
                "--nolockcheck",
                f"-env:UserInstallation=file://{profile_dir.resolve()}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(out_dir),
                str(input_path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        pdfs = sorted(out_dir.glob("*.pdf"))
        if not pdfs:
            detail = _command_detail(result, env=env, soffice=soffice)
            raise PreviewError(f"LibreOffice conversion failed: {detail[-1200:]}")
        shutil.move(str(pdfs[0]), str(target))
    return target


def _command_detail(result: subprocess.CompletedProcess[str], *, env: dict[str, str], soffice: str) -> str:
    detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    try:
        version = subprocess.run(
            [soffice, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        version_detail = "\n".join(part.strip() for part in (version.stdout, version.stderr) if part.strip())
    except Exception as exc:
        version_detail = f"version probe failed: {exc}"
    if not detail:
        detail = (
            f"exit={result.returncode}; no stdout/stderr; "
            f"soffice={soffice}; version={version_detail or '<empty>'}; "
            f"SAL_USE_VCLPLUGIN={env.get('SAL_USE_VCLPLUGIN')}; "
            f"XDG_RUNTIME_DIR={env.get('XDG_RUNTIME_DIR')}; "
            "observed in this sandbox: default soffice emits dconf errors for read-only /run/user/1000, "
            "and headless conversion exits 1 even with isolated XDG/HOME. "
            "Likely missing/incomplete LibreOffice headless runtime, VCL plugin, or system profile support."
        )
    return detail


def ensure_png_previews(source: Path, cache_dir: Path, scale: float = 1.5, timeout: int = 90) -> list[Path]:
    source = source.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    target_dir = cache_dir / f"{source.stem}.{source.stat().st_size}.{file_sha256(source)[:16]}.png-pages"
    manifest_path = target_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pages = [target_dir / name for name in manifest.get("pages", [])]
            if pages and all(path.is_file() and path.stat().st_size > 0 for path in pages):
                return pages
        except Exception:
            pass

    tmp_dir = target_dir.with_suffix(".tmp")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    pages: list[Path] = []
    try:
        try:
            pages = _render_pptx_direct_png(source, tmp_dir, timeout=timeout, previous_errors=[])
        except Exception as direct_exc:
            pdf_path: Path | None = None
            try:
                pdf_path = ensure_pdf_preview(source, cache_dir, timeout=timeout)
                import fitz  # type: ignore

                with fitz.open(pdf_path) as document:
                    for index, page in enumerate(document, start=1):
                        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                        page_path = tmp_dir / f"page_{index:04d}.png"
                        pixmap.save(page_path)
                        pages.append(page_path)
            except Exception as pdf_or_fitz_exc:
                if pdf_path is None:
                    raise PreviewError(
                        f"direct PNG export failed: {direct_exc}; PDF export also failed: {pdf_or_fitz_exc}"
                    ) from pdf_or_fitz_exc
                try:
                    pages = _render_pdf_with_cli(pdf_path, tmp_dir, scale=scale, timeout=timeout)
                except Exception as cli_exc:
                    raise PreviewError(
                        f"direct PNG export failed: {direct_exc}; "
                        f"PDF path also failed: {pdf_or_fitz_exc}; {cli_exc}"
                    ) from cli_exc
        if not pages:
            raise PreviewError("no PNG preview pages generated")
        (tmp_dir / "manifest.json").write_text(
            json.dumps({"source": str(source), "page_count": len(pages), "pages": [p.name for p in pages]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        shutil.rmtree(target_dir, ignore_errors=True)
        tmp_dir.rename(target_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return [target_dir / p.name for p in pages]


def _render_pptx_direct_png(source: Path, tmp_dir: Path, timeout: int, previous_errors: list[Exception]) -> list[Path]:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise PreviewError("LibreOffice/soffice not found for direct PNG export")
    out_dir = tmp_dir / "lo-png-out"
    profile_dir = tmp_dir / "lo-png-profile"
    xdg_dir = tmp_dir / "lo-png-xdg"
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    for p in (xdg_dir / "config", xdg_dir / "cache", xdg_dir / "runtime"):
        p.mkdir(parents=True, exist_ok=True)
    os.chmod(xdg_dir / "runtime", 0o700)
    env = os.environ.copy()
    env["HOME"] = str(xdg_dir)
    env["XDG_CONFIG_HOME"] = str(xdg_dir / "config")
    env["XDG_CACHE_HOME"] = str(xdg_dir / "cache")
    env["XDG_RUNTIME_DIR"] = str(xdg_dir / "runtime")
    env["SAL_USE_VCLPLUGIN"] = "svp"
    env["GIO_USE_VFS"] = "local"
    env["NO_AT_BRIDGE"] = "1"
    env["DBUS_SESSION_BUS_ADDRESS"] = ""
    input_path = tmp_dir / f"direct_png_source{source.suffix.lower()}"
    shutil.copy2(source, input_path)
    result = subprocess.run(
        [
            soffice,
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            "--nodefault",
            "--nolockcheck",
            f"-env:UserInstallation=file://{profile_dir.resolve()}",
            "--convert-to",
            "png",
            "--outdir",
            str(out_dir),
            str(input_path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    pngs = sorted(out_dir.glob("*.png"))
    if not pngs:
        previous = "; ".join(str(e) for e in previous_errors)
        suffix = f"; previous={previous}" if previous else ""
        raise PreviewError(f"LibreOffice direct PNG export failed: {_command_detail(result, env=env, soffice=soffice)}{suffix}")
    pages = []
    for index, png in enumerate(pngs, start=1):
        target = tmp_dir / f"page_{index:04d}.png"
        shutil.move(str(png), str(target))
        pages.append(target)
    return pages


def _render_pdf_with_cli(pdf_path: Path, tmp_dir: Path, scale: float, timeout: int) -> list[Path]:
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm:
        prefix = tmp_dir / "page"
        dpi = max(72, int(96 * scale))
        result = subprocess.run(
            [pdftoppm, "-png", "-r", str(dpi), str(pdf_path), str(prefix)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        pages = sorted(tmp_dir.glob("page-*.png"))
        if result.returncode == 0 and pages:
            renamed = []
            for index, page in enumerate(pages, start=1):
                target = tmp_dir / f"page_{index:04d}.png"
                page.rename(target)
                renamed.append(target)
            return renamed
        raise PreviewError(f"pdftoppm render failed: {(result.stderr or result.stdout)[-1200:]}")
    mutool = shutil.which("mutool")
    if mutool:
        pattern = str(tmp_dir / "page_%04d.png")
        dpi = max(72, int(96 * scale))
        result = subprocess.run(
            [mutool, "draw", "-r", str(dpi), "-o", pattern, str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        pages = sorted(tmp_dir.glob("page_*.png"))
        if result.returncode == 0 and pages:
            return pages
        raise PreviewError(f"mutool render failed: {(result.stderr or result.stdout)[-1200:]}")
    raise PreviewError("PyMuPDF, pdftoppm, or mutool is required to render PDF pages")
