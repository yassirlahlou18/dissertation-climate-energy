# Running the thesis pipeline on Google Cloud (GCP)

You have GCP credit (~£200) and a Gurobi **WLS** licence. WLS is the right
licence for cloud - it is portable, token-based, and not locked to a machine, so
you keep your exact Gurobi barrier settings. The single wy2015 case costs only
about £3-4 of compute, so budget is not a concern for now.

This guide uses a plain GCP VM (Compute Engine). You do NOT need the HPC
Toolkit / Slurm for a single case - that complexity only pays off for the full
62-year sweep later.

---

## 0. One-time: install the gcloud CLI on your laptop (optional)

You can do everything from the GCP web Console instead, but the CLI is faster.
Install from https://cloud.google.com/sdk/docs/install, then:
```
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

---

## 1. Create the VM

Memory is the binding constraint: PyPSA builds a ~23M-row model in RAM before
solving. 64 GB is comfortable. Use an ON-DEMAND instance (not Spot/preemptible)
so a ~90 min solve is not killed mid-run.

```
gcloud compute instances create thesis-vm \
  --zone=europe-west2-a \
  --machine-type=n2-highmem-8 \
  --image-family=ubuntu-2404-lts-amd64 \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=50GB \
  --boot-disk-type=pd-balanced
```
(`europe-west2` is London, close to you. n2-highmem-8 = 8 vCPU / 64 GB.)

To save money, STOP the VM when not running (you are billed for compute only
while it runs; the disk costs a few pence/day):
```
gcloud compute instances stop thesis-vm --zone=europe-west2-a
gcloud compute instances start thesis-vm --zone=europe-west2-a
```

---

## 2. Get your Gurobi WLS licence file

On the Gurobi Web License Manager (https://license.gurobi.com):
- Licenses tab -> your Academic WLS licence -> **Download** (creates an API key
  and a `gurobi.lic` file containing WLSACCESSID, WLSSECRET, LICENSEID).
- Keep that `gurobi.lic`; you will upload it to the VM.

The VM needs outbound HTTPS to `token.gurobi.com` (open by default on GCP).

---

## 3. Upload code, data, and licence to the VM

From your laptop (PowerShell), assuming your repo is
`C:\Users\yassi\thesis-climate-energy`:

```
# the pipeline code (the src folder from the v5 zip)
gcloud compute scp --recurse C:\Users\yassi\thesis-climate-energy\src thesis-vm:~/thesis-climate-energy/src --zone=europe-west2-a

# the cloud helper scripts
gcloud compute scp --recurse C:\Users\yassi\thesis-climate-energy\cloud thesis-vm:~/thesis-climate-energy/cloud --zone=europe-west2-a

# the Gotske network(s) you want to run
gcloud compute scp --recurse C:\Users\yassi\thesis-climate-energy\venv\Capacity_optimization\networks thesis-vm:~/thesis-climate-energy/venv/Capacity_optimization/networks --zone=europe-west2-a

# the C2E data
gcloud compute scp --recurse C:\Users\yassi\thesis-climate-energy\C2E thesis-vm:~/thesis-climate-energy/C2E --zone=europe-west2-a

# the Gurobi WLS licence
gcloud compute scp C:\path\to\gurobi.lic thesis-vm:~/gurobi.lic --zone=europe-west2-a
```

Tip: you only need the ONE network file for wy2015 right now, not all 62 - that
keeps the upload small. For the full sweep later, upload them all or pull from a
GCS bucket.

Note on the venv folder: do NOT upload your Windows `venv`. The VM builds a fresh
Linux venv in step 4. Only the `networks` subfolder under it is data you need.
If your networks live inside the Windows venv path, just copy that subfolder as
shown above; the Linux venv is separate.

---

## 4. Set up the VM

SSH in:
```
gcloud compute ssh thesis-vm --zone=europe-west2-a
```
Then:
```
cd ~/thesis-climate-energy
chmod +x cloud/setup_gcp_vm.sh
./cloud/setup_gcp_vm.sh
```
This installs Python, the PyPSA stack, Gurobi, finds your `gurobi.lic`, verifies
a WLS token can be acquired, and sets `THESIS_REPO`.

---

## 5. Run

```
cd ~/thesis-climate-energy
source venv/bin/activate
export THESIS_REPO=$HOME/thesis-climate-energy

python -m src.inspect_c2e        # confirm C2E files parse
python -m src.preflight_check    # confirm files, packages, Gurobi licence

# run in the background so it survives SSH disconnects:
nohup python -m src.run_pipeline > run.out 2>&1 &
tail -f run.out                  # watch; Ctrl-C just stops watching, not the run
```

To run methods separately (original solved once, cached):
```
nohup python -m src.run_pipeline --method qdm    > qdm.out    2>&1 &
# wait for it to finish, then:
nohup python -m src.run_pipeline --method direct > direct.out 2>&1 &
nohup python -m src.run_pipeline --method delta  > delta.out  2>&1 &
```

---

## 6. Get results back to your laptop

Results are under `~/thesis-climate-energy/output`. Pull the whole folder:
```
gcloud compute scp --recurse thesis-vm:~/thesis-climate-energy/output C:\Users\yassi\thesis-climate-energy\output_from_cloud --zone=europe-west2-a
```

---

## 7. STOP THE VM when done (important for budget)

```
gcloud compute instances stop thesis-vm --zone=europe-west2-a
```
Stopped VMs cost only a few pence/day for the disk. Delete it entirely when the
project is finished:
```
gcloud compute instances delete thesis-vm --zone=europe-west2-a
```

---

## Cost sanity check

- n2-highmem-8 on-demand ≈ $0.47/hr (~£0.37/hr).
- Single case (original + 3 methods, ~6-8 h wall) ≈ £3-4.
- You can run the single case dozens of times within £200.
- The full 62-year sweep later (~£140 serial) fits too, but parallelise across
  several VMs or a bigger machine to finish faster.

## If Gurobi licensing ever blocks you

The pipeline also runs on the free open-source solver HiGHS (no licence). Set
`SOLVER = 'highs'` and `GUROBI_OPTS = {}` in `src/config.py`. Slower than Gurobi
barrier on this size, but it solves, and it is how the pipeline was tested.
