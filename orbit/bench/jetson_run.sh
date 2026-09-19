#!/usr/bin/env sh
# Run ON the Jetson Orin Nano Super dev kit. Wraps the identical CPU workload in
# the INA3221 sampler (module-level VDD_IN + device-level VDD_CPU_GPU_CV/VDD_SOC).
#   sh orbit/bench/jetson_run.sh 20            # 20 s CPU (NumPy) run
#   sh orbit/bench/jetson_run.sh 20 --gpu      # CuPy path; uses the system python3
#                                              # (JetPack's CuPy is not in the uv venv)
# Extra args pass through to orbit.bench.run. Results: bench/results/<timestamp>.{md,json}
set -e
cd "$(dirname "$0")/../.."
SECS=${1:-20}; [ $# -gt 0 ] && shift
case " $* " in *" --gpu "*) PY=${ORBIT_PY:-python3} ;; *) PY=${ORBIT_PY:-"uv run python"} ;; esac
command -v uv >/dev/null 2>&1 || PY=python3
echo "power mode: $(nvpmodel -q 2>/dev/null | tr '\n' ' ')"   # recorded in the report notes by run.py
exec $PY -m orbit.bench.run --platform jetson --seconds "$SECS" "$@"
