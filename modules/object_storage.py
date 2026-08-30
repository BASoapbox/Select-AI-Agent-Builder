"""
modules/object_storage.py
Menu option 3 — create bucket and upload documents to Object Storage for RAG.
Writes the vector index URL to the runtime config after a successful upload.

Supported formats (all ingested by DBMS_VECTOR_CHAIN in ADW):
  PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx),
  plain text (.txt), HTML (.html), Markdown (.md),
  XML (.xml), JSON (.json)
"""

import os
from pathlib import Path

import oci

from core import config as cfg_module


# Supported RAG document formats with their MIME types
RAG_FORMATS = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt":  "text/plain",
    ".html": "text/html",
    ".htm":  "text/html",
    ".md":   "text/markdown",
    ".xml":  "application/xml",
    ".json": "application/json",
}


def _find_documents(directory: Path, extensions: set) -> list:
    """Return sorted list of files matching the given extensions."""
    files = []
    for ext in extensions:
        files.extend(directory.glob(f"*{ext}"))
    return sorted(set(files))


def run(cfg, clients: dict, display, config_path: str = ""):
    display.head("OBJECT STORAGE — BUCKET & DOCUMENT UPLOAD")

    os_client   = clients["object_storage"]
    compartment = cfg_module.get(cfg, "compartment", "compartment_ocid")
    region      = cfg_module.get(cfg, "oci", "region")
    bucket_name = cfg_module.get(cfg, "object_storage", "default_bucket")
    prefix      = cfg_module.get(cfg, "object_storage", "default_prefix").strip("/") + "/"
    doc_dir     = Path(cfg_module.get(cfg, "object_storage", "doc_directory", fallback="").strip("'\""))

    # Show supported formats
    display.blank()
    print(f"  Supported formats: {', '.join(RAG_FORMATS.keys())}")
    display.blank()

    # Allow overrides — q to quit, Enter to keep default
    display.info("Press q at any prompt to cancel and return to menu.")
    display.blank()
    try:
        override = input(f"  Bucket name [{bucket_name}]: ").strip()
        if override.lower() == "q":
            return
        if override:
            bucket_name = override

        override2 = input(f"  Prefix [{prefix.rstrip('/')}]: ").strip()
        if override2.lower() == "q":
            return
        if override2:
            prefix = override2.strip("/") + "/"

        override3 = input(f"  Document directory [{doc_dir}]: ").strip()
        if override3.lower() == "q":
            return
        if override3:
            doc_dir = Path(override3.strip("'\""))

        # Let user restrict to specific formats or upload all
        display.blank()
        print(f"  Filter by format (comma-separated, e.g. .pdf,.docx)")
        fmt_input = input(f"  Formats to upload [Enter = all supported, q = cancel]: ").strip().lower()
        if fmt_input == "q":
            return
        if fmt_input:
            chosen_exts = {e.strip() if e.strip().startswith(".") else f".{e.strip()}"
                           for e in fmt_input.split(",")}
            chosen_exts = chosen_exts & set(RAG_FORMATS.keys())
            if not chosen_exts:
                display.warn("No valid formats specified — uploading all supported formats")
                chosen_exts = set(RAG_FORMATS.keys())
        else:
            chosen_exts = set(RAG_FORMATS.keys())

    except (EOFError, KeyboardInterrupt):
        display.warn("Cancelled")
        return

    display.blank()
    display.info(f"Bucket    : {bucket_name}")
    display.info(f"Prefix    : {prefix}")
    display.info(f"Directory : {doc_dir}")
    display.info(f"Formats   : {', '.join(sorted(chosen_exts))}")

    try:
        namespace = os_client.get_namespace().data
    except Exception as ex:
        display.err(f"Cannot reach Object Storage: {ex}")
        return

    # ── Create bucket if needed ───────────────────────────────────────────────
    try:
        os_client.get_bucket(namespace, bucket_name)
        display.ok(f"Bucket '{bucket_name}' already exists")
    except oci.exceptions.ServiceError as ex:
        if ex.status != 404:
            display.err(f"Bucket check failed: {ex.message}")
            return
        display.info(f"Creating bucket '{bucket_name}'...")
        try:
            os_client.create_bucket(
                namespace,
                oci.object_storage.models.CreateBucketDetails(
                    name               = bucket_name,
                    compartment_id     = compartment,
                    public_access_type = "NoPublicAccess",
                    storage_tier       = "Standard",
                )
            )
            display.ok(f"Bucket '{bucket_name}' created")
        except Exception as ex2:
            display.err(f"Bucket creation failed: {ex2}")
            return

    # ── Find documents ────────────────────────────────────────────────────────
    if not doc_dir or not doc_dir.exists():
        display.warn(f"Directory not found: {doc_dir} — skipping upload")
        return

    all_files = _find_documents(doc_dir, chosen_exts)

    if not all_files:
        display.warn(f"No supported documents found in {doc_dir}")
        display.info(f"Looking for: {', '.join(sorted(chosen_exts))}")
        return

    # Show a breakdown by format before uploading
    by_ext = {}
    for f in all_files:
        by_ext.setdefault(f.suffix.lower(), []).append(f)
    display.blank()
    display.info(f"Found {len(all_files)} document(s):")
    for ext, files in sorted(by_ext.items()):
        print(f"       {ext:<8}  {len(files)} file(s)")

    # ── Confirm, edit, or cancel ──────────────────────────────────────────────
    while True:
        display.blank()
        print("  Options:")
        print("   1. Create / Upload")
        print("   2. Edit")
        print("   3. Cancel")
        try:
            choice = input("  Select [1/2/3]: ").strip()
        except (EOFError, KeyboardInterrupt):
            display.warn("Cancelled")
            return

        if choice == "3" or choice.lower() == "q":
            display.warn("Cancelled")
            return

        if choice == "2":
            # Re-enter the four editable fields — blank = keep current
            display.blank()
            try:
                v = input(f"  Bucket name [{bucket_name}]: ").strip().strip("'\"")
                if v:
                    bucket_name = v
                v = input(f"  Prefix [{prefix.rstrip('/')}]: ").strip().strip("'\"")
                if v:
                    prefix = v.strip("/") + "/"
                elif v == "":
                    pass  # keep current
                v = input(f"  Document directory [{doc_dir}]: ").strip().strip("'\"")
                if v:
                    doc_dir = Path(v)
                v = input(f"  Formats [{', '.join(sorted(chosen_exts))}]: ").strip().lower()
                if v:
                    chosen_exts = {
                        e.strip() if e.strip().startswith(".") else f".{e.strip()}"
                        for e in v.split(",") if e.strip()
                    } or chosen_exts
            except (EOFError, KeyboardInterrupt):
                display.warn("Cancelled")
                return

            # Re-validate directory and re-scan files
            if not doc_dir or not doc_dir.exists():
                display.err(f"Directory not found: {doc_dir}")
                continue

            all_files = _find_documents(doc_dir, chosen_exts)
            if not all_files:
                display.warn(f"No supported documents found in {doc_dir}")
                display.info(f"Looking for: {', '.join(sorted(chosen_exts))}")
                continue

            # Re-check/create bucket
            try:
                namespace = os_client.get_namespace().data
                os_client.get_bucket(namespace, bucket_name)
                display.ok(f"Bucket '{bucket_name}' already exists")
            except oci.exceptions.ServiceError as ex:
                if ex.status != 404:
                    display.err(f"Bucket check failed: {ex.message}")
                    continue
                display.info(f"Creating bucket '{bucket_name}'...")
                try:
                    os_client.create_bucket(
                        namespace,
                        oci.object_storage.models.CreateBucketDetails(
                            name               = bucket_name,
                            compartment_id     = compartment,
                            public_access_type = "NoPublicAccess",
                            storage_tier       = "Standard",
                        )
                    )
                    display.ok(f"Bucket '{bucket_name}' created")
                except Exception as ex2:
                    display.err(f"Bucket creation failed: {ex2}")
                    continue

            # Show updated summary
            display.blank()
            display.info(f"Bucket    : {bucket_name}")
            display.info(f"Prefix    : {prefix}")
            display.info(f"Directory : {doc_dir}")
            display.info(f"Formats   : {', '.join(sorted(chosen_exts))}")
            by_ext = {}
            for f in all_files:
                by_ext.setdefault(f.suffix.lower(), []).append(f)
            display.info(f"Found {len(all_files)} document(s):")
            for ext, files in sorted(by_ext.items()):
                print(f"       {ext:<8}  {len(files)} file(s)")
            continue

        if choice == "1":
            break  # proceed to upload

        display.warn("Please enter 1, 2, or 3")

    display.blank()

    # ── Upload (parallel with ThreadPoolExecutor) ─────────────────────────────
    from concurrent.futures import ThreadPoolExecutor, as_completed

    uploaded = skipped = failed = 0
    upload_errors = []

    def _upload_one(doc_path):
        obj_name     = prefix + doc_path.name
        content_type = RAG_FORMATS.get(doc_path.suffix.lower(), "application/octet-stream")
        # Check if already exists
        try:
            os_client.head_object(namespace, bucket_name, obj_name)
            return "skipped", doc_path.name, None
        except oci.exceptions.ServiceError as ex:
            if ex.status != 404:
                return "error", doc_path.name, f"head_object failed: {ex.message}"
        try:
            with open(doc_path, "rb") as fh:
                os_client.put_object(
                    namespace, bucket_name, obj_name, fh,
                    content_type=content_type
                )
            return "uploaded", doc_path.name, None
        except Exception as ex:
            return "error", doc_path.name, str(ex)

    display.info(f"Uploading {len(all_files)} file(s) in parallel (4 threads)...")
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_upload_one, f): f for f in all_files}
        for future in as_completed(futures):
            status, fname, err = future.result()
            if status == "uploaded":
                display.ok(f"Uploaded: {fname}")
                uploaded += 1
            elif status == "skipped":
                display.ok(f"Already exists — skipping: {fname}")
                skipped += 1
            else:
                display.err(f"Upload failed for {fname}: {err}")
                upload_errors.append(fname)
                failed += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    display.blank()
    display.info(
        f"Upload complete — {uploaded} new, {skipped} already existed"
        + (f", {failed} failed" if failed else "")
    )

    location_url = (
        f"https://objectstorage.{region}.oraclecloud.com"
        f"/n/{namespace}/b/{bucket_name}/o/{prefix}"
    )
    display.ok("Vector index location URL:")
    print(f"       {location_url}")
    display.info("Use this URL in your RAG profile when the builder asks for it")

    # Ask before writing to runtime config
    if uploaded > 0 or skipped > 0:
        try:
            confirm = input(
                "\n  →  Save this URL to runtime config so the builder uses it automatically? [y/N]: "
            ).strip().lower()
            if confirm == "y":
                cfg_module.update_value(config_path, "object_storage", "rag_location_url", location_url)
                display.ok("URL saved to runtime config — the builder will use it automatically")
            else:
                display.info("URL not saved — you can copy it manually when the builder asks")
        except (EOFError, KeyboardInterrupt):
            pass
