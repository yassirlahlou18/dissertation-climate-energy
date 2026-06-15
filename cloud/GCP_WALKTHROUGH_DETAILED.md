# Running on Google Cloud: a detailed beginner's walkthrough

This is written for someone who has never used a cloud VM. Every step says what
to do AND why. Read the "What you're about to do" section once, then follow the
numbered steps.

A reminder on speed, so expectations are right: the cloud will NOT make a single
solve faster than your laptop (per-core speed is similar). Its value is (a)
running while your laptop is free and closed, (b) never running out of memory,
and (c) later, running many weather years at once. For the single wy2015 case
the benefit is convenience, not speed.

---

## What you're about to do (the mental model)

A "VM" (virtual machine) is just a computer you rent by the hour that lives in a
Google data centre. You will:
1. Create that computer (a VM called `thesis-vm`).
2. Copy your code, your data, and your Gurobi licence onto it.
3. Log into it (SSH = a remote terminal) and run a setup script.
4. Start the pipeline and let it run, even with your laptop closed.
5. Copy the results back to your laptop.
6. Turn the VM off so you stop paying for it.

You are billed only while the VM is RUNNING (a few pence per hour). A stopped VM
costs almost nothing (just disk, pennies per day). So the rule is: start it to
work, stop it when done.

Whenever you see `thesis-vm` and `europe-west2-a` below, those are just the name
and the location (London) of your VM. Keep them consistent everywhere.

---

## STEP 0 — Prepare on your PC

0a. Replace your local `src` folder with the one from the v6 zip (it contains the
    cloud changes). Keep the `cloud` folder on your PC too - you will run upload
    commands from your PC. Your PC repo should look like:
```
C:\Users\yassi\thesis-climate-energy\
   src\           (from v6)
   cloud\         (from v6: GCP_SETUP.md, setup_gcp_vm.sh, this file)
   C2E\           (your data)
   venv\Capacity_optimization\networks\   (your .nc networks)
```

0b. Install the Google Cloud CLI ("gcloud") on your PC. This is the tool that
    lets you create VMs and copy files from PowerShell.
    - Download: https://cloud.google.com/sdk/docs/install  (Windows installer)
    - Run the installer, accept defaults, and let it open a browser to log in.
    - When it finishes, open a NEW PowerShell window and check it works:
```
gcloud --version
```
    You should see version numbers. If "gcloud is not recognized", close and
    reopen PowerShell, or restart the PC so the PATH updates.

0c. Log in and select your project (the project is the billing/▾organisational
    bucket your £200 credit sits in):
```
gcloud auth login
gcloud projects list
```
    The second command lists your projects with a PROJECT_ID column. Pick the one
    that has your credit and set it:
```
gcloud config set project YOUR_PROJECT_ID
```
    (Replace YOUR_PROJECT_ID with the actual id, e.g. `thesis-climate-2026`.)

0d. Enable the Compute Engine service once (lets you create VMs):
```
gcloud services enable compute.googleapis.com
```
    This can take a minute. If it says already enabled, good.

---

## STEP 1 — Get your Gurobi WLS licence file

You need a small text file called `gurobi.lic` that proves you may run Gurobi.

1a. Go to https://license.gurobi.com and log in with your Gurobi academic account.
1b. Open the "Licenses" tab. Find your Academic WLS license.
1c. Click "Download" (or the API Keys tab -> Create API Key). This produces a
    `gurobi.lic` file. It contains three lines like:
```
WLSACCESSID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
WLSSECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
LICENSEID=1234567
```
1d. Save it somewhere you can find, e.g. `C:\Users\yassi\gurobi.lic`.

Why: the VM has no Gurobi licence of its own. This file (token-based, not tied to
any machine) lets the VM ask Gurobi's server for permission each time it solves.

---

## STEP 2 — Create the VM

In PowerShell on your PC, run this single command (it is long; copy it whole):
```
gcloud compute instances create thesis-vm --zone=europe-west2-a --machine-type=n2-highmem-8 --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud --boot-disk-size=50GB --boot-disk-type=pd-balanced
```

What each part means:
- `thesis-vm` : the name you give the computer.
- `--zone=europe-west2-a` : physical location = London (close to you = lower
  latency for file transfer).
- `--machine-type=n2-highmem-8` : 8 vCPUs and 64 GB RAM. The 64 GB is the point:
  PyPSA builds a ~23-million-row model in memory before solving, and you want
  comfortable headroom.
- `--image-family=ubuntu-2404-lts-amd64` : the operating system (Ubuntu Linux).
- `--boot-disk-size=50GB` : disk space for OS + your data + outputs.

This takes ~30 seconds. When it prints a line with the VM name and an IP, it
exists and is RUNNING (i.e. now billing). That's fine; we use it right away.

IMPORTANT - do NOT use a "Spot" or "preemptible" VM for this. Those are cheaper
but Google can kill them at any moment, which would abort a 90-minute solve.
The command above is a normal on-demand VM.

---

## STEP 3 — Copy your files to the VM

These commands run on your PC (PowerShell), and PUSH files to the VM. The format
is `gcloud compute scp <local path> thesis-vm:<remote path> --zone=...`. The
first time, gcloud may create SSH keys and ask you to set a passphrase - you can
leave it blank by pressing Enter twice.

First make the folder structure on the VM. SSH in once:
```
gcloud compute ssh thesis-vm --zone=europe-west2-a
```
You are now "inside" the VM (the prompt changes to something like
`yassi@thesis-vm:~$`). Create the directories, then leave:
```
mkdir -p ~/thesis-climate-energy/venv/Capacity_optimization/networks
exit
```
(`exit` returns you to your PC's PowerShell.)

Now copy the pieces (run each from your PC; adjust the left-hand Windows paths if
yours differ):

```
gcloud compute scp --recurse C:\Users\yassi\thesis-climate-energy\src thesis-vm:~/thesis-climate-energy/src --zone=europe-west2-a
```
```
gcloud compute scp --recurse C:\Users\yassi\thesis-climate-energy\cloud thesis-vm:~/thesis-climate-energy/cloud --zone=europe-west2-a
```
```
gcloud compute scp --recurse C:\Users\yassi\thesis-climate-energy\C2E thesis-vm:~/thesis-climate-energy/C2E --zone=europe-west2-a
```
For the network, you only need the ONE wy2015 file right now (keeps the upload
small). Copy just that file:
```
gcloud compute scp "C:\Users\yassi\thesis-climate-energy\venv\Capacity_optimization\networks\elec_wy2015_s370_37_lv1.0__Co2L0-3h-T-H-B-I-A-solar+p3-dist1_2050.nc" thesis-vm:~/thesis-climate-energy/venv/Capacity_optimization/networks/ --zone=europe-west2-a
```
Finally the Gurobi licence (note it goes to the home folder `~`, not the repo):
```
gcloud compute scp C:\Users\yassi\gurobi.lic thesis-vm:~/gurobi.lic --zone=europe-west2-a
```

Notes:
- `--recurse` means "copy the whole folder and everything in it".
- Do NOT copy your Windows `venv` folder itself - the VM builds its own Linux
  one. You only copied the `networks` data that happens to live under that path.
- If a path has spaces, wrap it in double quotes (as shown for the .nc file).
- Transfers show a progress bar. C2E and the network may take a few minutes.

---

## STEP 4 — Set up the software on the VM

SSH back in:
```
gcloud compute ssh thesis-vm --zone=europe-west2-a
```
Run the setup script (it installs Python, the PyPSA stack, Gurobi, and checks
your licence):
```
cd ~/thesis-climate-energy
chmod +x cloud/setup_gcp_vm.sh
./cloud/setup_gcp_vm.sh
```
`chmod +x` just makes the script runnable. The script prints progress. Near the
end it tries a tiny Gurobi solve to confirm your WLS licence works; you want to
see a line like "Gurobi OK ... WLS token acquired". If instead it says it cannot
find `gurobi.lic`, check that step 3's licence upload landed at `~/gurobi.lic`
(run `ls ~/gurobi.lic` to verify), then re-run the script.

Why a setup script: a fresh VM has nothing installed. This reproduces the same
environment your laptop has, in one command.

---

## STEP 5 — Quick checks, then run

Still inside the VM:
```
cd ~/thesis-climate-energy
source venv/bin/activate
export THESIS_REPO=$HOME/thesis-climate-energy
```
- `source venv/bin/activate` turns on the Python environment (you'll see
  `(venv)` appear at the start of the prompt).
- `export THESIS_REPO=...` tells the pipeline where your repo is, so you don't
  edit any paths in config.py. (The setup script already added this to your
  startup file, but setting it again now does no harm.)

Confirm everything is in place:
```
python -m src.inspect_c2e
python -m src.preflight_check
```
`inspect_c2e` prints what your C2E files contain. `preflight_check` confirms the
network file, packages, and Gurobi licence are all OK. Fix anything it flags
before running.

Now run. Use `nohup ... &` so the job keeps running even if your SSH connection
drops or you close your laptop:
```
nohup python -m src.run_pipeline > run.out 2>&1 &
```
- `nohup` = "no hangup": keep running if I disconnect.
- `> run.out 2>&1` = save all output to a file called run.out.
- `&` = run in the background and give me my prompt back.

Watch progress live:
```
tail -f run.out
```
This streams the log. Pressing Ctrl-C here only stops WATCHING; the run keeps
going. You can now safely close your laptop / disconnect. The expected time is
similar to your laptop (about 90 min per solve; one original + three methods).

To check on it later, SSH back in and `tail -f ~/thesis-climate-energy/run.out`
again, or look for the line "PIPELINE done." which means it finished.

If you prefer to run methods one at a time (original solved once and reused):
```
nohup python -m src.run_pipeline --method qdm > qdm.out 2>&1 &
# when that finishes:
nohup python -m src.run_pipeline --method direct > direct.out 2>&1 &
nohup python -m src.run_pipeline --method delta  > delta.out  2>&1 &
```

---

## STEP 6 — Bring the results back to your PC

When you see "PIPELINE done." the results are in
`~/thesis-climate-energy/output` on the VM. From your PC's PowerShell (not inside
the VM), pull the whole folder down:
```
gcloud compute scp --recurse thesis-vm:~/thesis-climate-energy/output C:\Users\yassi\thesis-climate-energy\output_from_cloud --zone=europe-west2-a
```
Now `output_from_cloud` on your PC has every report, figure, table, and the
comparison document.

---

## STEP 7 — Turn the VM OFF (do not skip this)

A running VM bills continuously. Stop it the moment you're done:
```
gcloud compute instances stop thesis-vm --zone=europe-west2-a
```
A stopped VM keeps all your files and costs only a few pence/day for the disk.
To use it again later, just:
```
gcloud compute instances start thesis-vm --zone=europe-west2-a
gcloud compute ssh thesis-vm --zone=europe-west2-a
```
Everything you installed and uploaded is still there.

When the whole project is finished and you want nothing left billing at all,
delete it (this erases the VM and its disk):
```
gcloud compute instances delete thesis-vm --zone=europe-west2-a
```

---

## Watching your spend

- See current credit/spend in the Console: https://console.cloud.google.com/billing
- The single wy2015 case costs about £3-4. You have huge headroom.
- The biggest accidental cost is leaving a VM running for days. Step 7 prevents
  that. If unsure whether it's running:
```
gcloud compute instances list
```
  STATUS will say RUNNING or TERMINATED (TERMINATED = stopped = not billing
  compute).

---

## Common problems and fixes

- "gcloud is not recognized": close and reopen PowerShell, or restart the PC.
- SSH asks for a passphrase you don't remember: you can leave passphrases blank
  when first prompted; if stuck, delete the keys in `C:\Users\yassi\.ssh\` named
  `google_compute_engine*` and let gcloud recreate them on the next ssh.
- "Gurobi licence" errors in preflight: confirm `ls ~/gurobi.lic` shows the file
  on the VM, and that the VM has internet (it does by default) so it can reach
  token.gurobi.com.
- Out of memory / job killed: use a larger machine type, e.g.
  `--machine-type=n2-highmem-16` (128 GB). Recreate the VM with that flag.
- The run seems stuck: a single full-year solve genuinely takes ~90 min. Check
  run.out for Gurobi iteration logs to confirm it's progressing.
- If Gurobi licensing ever blocks you entirely: edit `src/config.py`, set
  `SOLVER = 'highs'` and `GUROBI_OPTS = {}`, and it runs on the free solver
  (slower, but no licence needed).

---

## When you scale to all 62 weather years (later)

This is where the cloud genuinely beats your laptop, because you can run many at
once. Two simple approaches:
- One big VM, run weather years sequentially overnight (change WEATHER_YEAR in
  config.py per run, or loop). Slow but trivial.
- Several VMs, each handling a subset of years, all running at the same time.
  This is the real speed-up. Ask me to write a batch/loop script and a Google
  Cloud Storage (bucket) workflow when you reach that stage; it keeps data in one
  place and lets many VMs pull from it.
