"""Pin reviewed local binaries/configuration. Does not display UI or authorize capture."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from BackendAdapter import absolute_file, sha256


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    config = absolute_file(args.config)
    directory = Path(__file__).resolve().parent
    ui = absolute_file(str(directory / "bin" / "DingTalkRegionCalibration.exe"))
    adapter = absolute_file(str(directory / "BackendAdapter.py"))
    python = absolute_file(sys.executable)
    output = Path(args.output)
    if not output.is_absolute() or not output.name.endswith(".binding.json") or not output.parent.is_dir():
        raise ValueError("separate_absolute_binding_output_required")
    if output.resolve() in {config, ui, adapter, python}:
        raise ValueError("binding_must_not_replace_input")
    sys.path.insert(0, str(directory.parents[1] / "src"))
    from dingtalkbridge import ocr

    info = ocr.wgc_once_backend_info()
    digest = info.get("backend_digest")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("reviewed_backend_digest_unavailable")
    binding = {
        "schema": "dingtalkbridge.roi-once-backend-binding.v1",
        "python_exe": str(python),
        "python_sha256": sha256(python),
        "adapter_script": str(adapter),
        "adapter_sha256": sha256(adapter),
        "ui_exe": str(ui),
        "ui_sha256": sha256(ui),
        "config_path": str(config),
        "config_sha256": sha256(config),
        "expected_backend_digest": digest,
    }
    output.write_text(json.dumps(binding, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "binding_prepared_not_authorization", "compiled_ready": info.get("compiled_ready") is True,
                      "authorization_granted": False, "pixels_read": False, "ui_shown": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
