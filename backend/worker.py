from __future__ import annotations

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any, Dict

from latex2word import Latex2WordPipeline, load_pipeline_config
from latex2word.config import set_nested_attr

from .job_utils import append_event, read_json, update_status, write_json
from .model_catalog import get_provider_defaults


def merge_rules(base_rules: Dict[str, Any], rendering: Dict[str, Any]) -> Dict[str, Any]:
    rules = dict(base_rules)
    rules["rendering"] = {
        **dict(base_rules.get("rendering") or {}),
        **rendering,
    }
    return rules


def prepare_config(project_root: Path, job_dir: Path, request: Dict[str, Any]) -> None:
    configs_dir = job_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)

    base_rules_path = project_root / "configs" / "rules.json"
    base_rules = read_json(base_rules_path, default={}) or {}
    write_json(configs_dir / "rules.json", merge_rules(base_rules, request.get("rendering") or {}))
    write_json(configs_dir / "terms.json", request.get("terms") or {})

    config = load_pipeline_config(project_root)
    api_key_env, base_url = get_provider_defaults(request["translate"]["provider"])
    overrides = {
        "paths.project_root": str(project_root),
        "paths.inputs_dir": str(job_dir / "inputs"),
        "paths.outputs_dir": str(job_dir / "outputs"),
        "paths.configs_dir": str(configs_dir),
        "paths.rules_file": str(configs_dir / "rules.json"),
        "paths.secrets_file": str(project_root / "secrets.env"),
        "translate.provider": request["translate"]["provider"],
        "translate.model": request["translate"]["model"],
        "translate.api_key_env": api_key_env,
        "translate.base_url": base_url,
        "translate.terms": str(configs_dir / "terms.json"),
        "translate.checkpoint": str(job_dir / "outputs" / "translated.checkpoint.json"),
    }
    for key, value in overrides.items():
        set_nested_attr(config, key, value)
    write_json(configs_dir / "pipeline.json", config.to_dict())


def run_job(job_dir: Path) -> int:
    project_root = Path(__file__).resolve().parents[1]
    request = read_json(job_dir / "request.json")
    if not isinstance(request, dict):
        raise RuntimeError("request.json missing or invalid")

    (job_dir / "outputs").mkdir(parents=True, exist_ok=True)
    prepare_config(project_root, job_dir, request)

    update_status(job_dir, state="running", message="Pipeline started", percent=0)
    append_event(job_dir, {"type": "status", "state": "running", "stage": "queued", "message": "Pipeline started", "percent": 0})

    def on_progress(event: Dict[str, Any]) -> None:
        percent = float(event.get("percent", 0))
        stage = str(event.get("stage") or "running")
        message = str(event.get("message") or stage)
        append_event(job_dir, {"type": "progress", **event})
        update_status(job_dir, state="running", stage=stage, message=message, percent=percent)

    config = load_pipeline_config(project_root, job_dir / "configs" / "pipeline.json")
    pipeline = Latex2WordPipeline(config, progress=on_progress)
    pipeline.run(stage="all")

    final_docx = job_dir / "outputs" / "final.docx"
    if not final_docx.exists():
        raise RuntimeError("Pipeline completed but final.docx was not produced")

    update_status(job_dir, state="completed", stage="done", message="转换完成", percent=100)
    append_event(job_dir, {"type": "status", "state": "completed", "stage": "done", "message": "转换完成", "percent": 100})
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one isolated latex2word web job.")
    parser.add_argument("job_dir")
    args = parser.parse_args()

    job_dir = Path(args.job_dir).resolve()
    log_path = job_dir / "worker.log"
    with log_path.open("a", encoding="utf-8") as log:
        sys.stdout = log
        sys.stderr = log
        try:
            raise_code = run_job(job_dir)
        except BaseException as exc:
            traceback.print_exc()
            status = read_json(job_dir / "status.json", default={}) or {}
            if status.get("state") != "cancelled":
                update_status(job_dir, state="failed", stage="failed", message=str(exc), percent=0)
                append_event(job_dir, {"type": "status", "state": "failed", "stage": "failed", "message": str(exc), "percent": 0})
            raise_code = 1
    raise SystemExit(raise_code)


if __name__ == "__main__":
    main()
