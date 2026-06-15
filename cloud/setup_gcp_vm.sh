#!/usr/bin/env bash
# ============================================================================
# setup_gcp_vm.sh  -  one-shot setup of the thesis pipeline on a fresh
#                     Ubuntu 22.04/24.04 GCP VM.
#
# Run this AFTER you have:
#   1. created the VM (see GCP_SETUP.md for the exact gcloud command),
#   2. uploaded your data + code + Gurobi WLS gurobi.lic (see GCP_SETUP.md),
#   3. SSH'd into the VM.
#
# Usage on the VM:
#   chmod +x setup_gcp_vm.sh
#   ./setup_gcp_vm.sh
# ============================================================================
set -euo pipefail

REPO_DIR="${HOME}/thesis-climate-energy"
PYTHON=python3

echo "==> [1/6] System packages"
sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip unzip wget

echo "==> [2/6] Python virtual environment"
cd "${REPO_DIR}"
${PYTHON} -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip

echo "==> [3/6] Python packages (PyPSA stack + Gurobi + reporting)"
pip install pypsa pandas numpy matplotlib python-docx gurobipy tsam psutil highspy

echo "==> [4/6] Gurobi WLS licence check"
# The pipeline expects a WLS gurobi.lic. Standard location is ~/gurobi.lic OR
# pointed to by GRB_LICENSE_FILE. We look in a few common spots.
LIC=""
for cand in "${HOME}/gurobi.lic" "${REPO_DIR}/gurobi.lic" "/opt/gurobi/gurobi.lic"; do
  if [ -f "${cand}" ]; then LIC="${cand}"; fi
done
if [ -n "${LIC}" ]; then
  export GRB_LICENSE_FILE="${LIC}"
  echo "  Found licence: ${LIC}"
  echo "  export GRB_LICENSE_FILE=${LIC}" >> "${HOME}/.bashrc"
else
  echo "  !! No gurobi.lic found. Upload your WLS gurobi.lic to ${HOME}/gurobi.lic"
  echo "     (Web License Manager -> Licenses -> Download). Then re-run this script,"
  echo "     or set GRB_LICENSE_FILE manually."
fi

echo "==> [5/6] Verify Gurobi can get a WLS token"
python3 - <<'PYTEST' || echo "  (Gurobi check failed - see message above; fix licence before running)"
try:
    import gurobipy as gp
    m = gp.Model("wls_test"); m.setParam("OutputFlag", 0)
    x = m.addVar(); m.setObjective(x); m.addConstr(x >= 1); m.optimize()
    print(f"  Gurobi OK, status {m.status} (WLS token acquired)")
except Exception as e:
    print(f"  Gurobi error: {e}")
    raise SystemExit(1)
PYTEST

echo "==> [6/6] Point the pipeline at this repo (THESIS_REPO env var)"
echo "export THESIS_REPO=${REPO_DIR}" >> "${HOME}/.bashrc"
export THESIS_REPO="${REPO_DIR}"

echo ""
echo "============================================================"
echo "SETUP COMPLETE."
echo "Next, from ${REPO_DIR} with the venv active:"
echo "  source venv/bin/activate"
echo "  export THESIS_REPO=${REPO_DIR}"
echo "  python -m src.inspect_c2e"
echo "  python -m src.preflight_check"
echo "  nohup python -m src.run_pipeline > run.out 2>&1 &"
echo "  tail -f run.out      # watch progress; safe to disconnect SSH"
echo "============================================================"
