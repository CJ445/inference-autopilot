"""Bounded GPU memory-pressure fault (PRD §40, §69, §145).

Launcher: validate -> run the child under the watchdog. Child (`python -m aiops.gpu_fault`):
cap its own CUDA allocator at the budget, allocate until PyTorch raises a real
OutOfMemoryError, hold briefly, release everything, report.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from aiops.supervise import supervise
from aiops.watchdog import read_sensors

MAX_BUDGET_MIB = 4096
MAX_HOLD_SECONDS = 60
STARTUP_MARGIN_SECONDS = 60  # torch import + CUDA context, on top of the hold time
CHUNK_MIB = 256
DEFAULT_LIMITS = {"max_gpu_memory_percent": 85, "max_gpu_percent": 90,
                  "max_temperature_c": 80, "max_ram_percent": 90}


def validate(budget_mib, hold_seconds):
    for name, value, cap in (("budget_mib", budget_mib, MAX_BUDGET_MIB),
                             ("hold_seconds", hold_seconds, MAX_HOLD_SECONDS)):
        if type(value) is not int or not 1 <= value <= cap:
            raise ValueError(f"{name} must be an integer between 1 and {cap}, got {value!r}")


def run_gpu_pressure(budget_mib, hold_seconds, report_path, limits=DEFAULT_LIMITS,
                     sampler=read_sensors, supervise_fn=supervise):
    validate(budget_mib, hold_seconds)
    report_path = Path(report_path)
    report_path.unlink(missing_ok=True)
    argv = [sys.executable, "-m", "aiops.gpu_fault", "--budget-mib", str(budget_mib),
            "--hold-seconds", str(hold_seconds), "--report", str(report_path)]
    result = supervise_fn(argv, limits, sampler, interval=0.5,
                          max_seconds=hold_seconds + STARTUP_MARGIN_SECONDS)
    result["report"] = json.loads(report_path.read_text()) if report_path.exists() else None
    return result


def _stress(budget_mib, hold_seconds):
    import torch

    total = torch.cuda.get_device_properties(0).total_memory
    torch.cuda.set_per_process_memory_fraction(budget_mib * 2**20 / total, 0)
    chunks, oom = [], False
    for _ in range(budget_mib // CHUNK_MIB + 4):  # bounded even if the cap never bites
        try:
            chunks.append(torch.empty(min(CHUNK_MIB, budget_mib) * 2**20,
                                      dtype=torch.uint8, device="cuda"))
        except torch.cuda.OutOfMemoryError:
            oom = True
            break
    allocated_mib = sum(c.numel() for c in chunks) // 2**20
    time.sleep(hold_seconds)
    del chunks
    torch.cuda.empty_cache()
    return {"budget_mib": budget_mib, "allocated_mib": allocated_mib, "oom_caught": oom}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--budget-mib", type=int, required=True)
    p.add_argument("--hold-seconds", type=int, required=True)
    p.add_argument("--report", required=True)
    args = p.parse_args(argv)
    try:
        validate(args.budget_mib, args.hold_seconds)
    except ValueError as e:
        p.error(str(e))
    report = _stress(args.budget_mib, args.hold_seconds)
    Path(args.report).write_text(json.dumps(report))
    return 0 if report["oom_caught"] else 1


if __name__ == "__main__":
    sys.exit(main())
