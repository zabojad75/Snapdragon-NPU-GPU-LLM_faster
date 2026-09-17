# Adreno OpenCL regression — verdict and rollback plan

**Date:** 2026-09-11
**Machine:** ASUS Zenbook A16 UX3607OA, Snapdragon X2 Elite Extreme, BIOS UX3607OA.312
**Subject:** GPU 32.0.172.1 breaks OpenCL (`CB.dll` load crash) — no fix found; proceed to downgrade.

---

## 1. Verdict

**No solution is available right now.** The OpenCL breakage is a genuine regression in the
driver's own compiler backend (`CB.dll`), not a local misconfiguration, and no fixed driver
release exists yet. The only working fast-GPU path is a rollback to **32.0.149.0**, which is
already staged on the machine.

Continue to §4 for the downgrade plan.

---

## 2. What was re-verified today

| Check | Result |
|---|---|
| Installed display driver | **32.0.172.1** (`oem33.inf`, driver date 2026-08-27) |
| Crash still reproduced in evidence | Yes — WER 2026-09-10 16:00: `CB.dll` **32.0.172.0**, exception `0xC0000409` (BEX64, subcode 5 = `FAST_FAIL_INVALID_ARG`), fault offset `+0x1b1b8`, in `llama-server.exe` and `cl_info.exe` |
| Driver payload integrity | `Get-AuthenticodeSignature` = **Valid** on both `CB.dll` versions and `OpenCL_adreno.dll` (WHQL). Not a corrupt download |
| ICD loader version skew | `OpenCL.dll` is **byte-identical** (md5 `fc8519e3…`) in the old package, the new package and `System32`. The "32.0.149.0" version string is just Qualcomm's loader version — **not** a mismatch |
| Stale dependency DLLs in System32 | `adreno_utils.dll`, `libgsluser.dll`, `qcclarmxcompilercore.dll`, `kernelmanager.dll`, `kcl.dll` — **all absent** from System32 (they live only in DriverStore). No version skew |
| Config / env kill switch in `CB.dll` | None found. Only `ADRENO_PROFILER_ENABLE_OPENCL` and `CL_DUMP_PREFIX` exist; no "disable feature" variable |
| Old driver package retained | Yes — `oem46.inf` = 32.0.149.0, still staged and complete |

**Root-cause chain:** `OpenCL_adreno.dll` (ICD) → `LoadLibrary(CB.dll)` → `CB.dll` init aborts via
UCRT `_invalid_parameter` → `__fastfail(FAST_FAIL_INVALID_ARG)` → `0xC0000409`. The new
`CB.dll` binary differs from the old one that loads fine; everything around it is clean.

## 3. Update channels — no fix exists yet

- **Qualcomm Software Center** (where 32.0.172.1 came from): local catalog was refreshed
  **today 2026-09-11 08:26**; the "Windows Graphics Driver for Snapdragon X" product
  (`productUUID 2b7df486-…`, etag 49) still carries publish date **2026-09-03**. No newer release.
- **ASUS support for UX3607OA**: latest *Qualcomm Board Support Package* **V1.312.4500.0**
  (2026-07-23) ships **Graphic Driver 32.0.149.0** — i.e. ASUS's own validated driver is *older*
  than what is installed.
- **Windows Update**: only offers "ASUS System Hardware Update (200.0.5.0)" = *System Manifest*,
  no GPU driver.
- **Alternative paths already ruled out** (Sessions 5o/5p, still valid): Khronos ICD
  re-registration, old userspace against the new kernel, Microsoft OpenCLOn12 (crashes /
  no `cl_khr_fp16`), Vulkan (no FP16×FP16→FP32 coopmat shapes → scalar shaders, ~6 tok/s).
  Upstream llama.cpp has no D3D12 backend in our checkout, so that detour is closed too.

## 4. Downgrade plan — 32.0.172.1 → 32.0.149.0

### 4.1 Impact and preconditions

- The Adreno GPU is the machine's only GPU, and WSLg (the panel/chat GUI) runs on it. The screen
  may flicker or blank during the swap. **Do this at the physical console**, not over a remote
  WSL/SSH session, and on AC power.
- A reboot is required. Downtime ~5–10 min.
- **Known trade-off:** 32.0.149.0 is what ASUS validates, but it is the version that motivated the
  upgrade — Qualcomm's forum reports it throttling GPU performance on this exact model, and
  Session 5o records that 32.0.172.1 "fixes older GPU crashes". We are trading those fixes back
  for a working OpenCL path. This is only worth it because GPU acceleration is currently dead
  anyway (the deployed `pkg-opencl` build is CPU-only, so the GPU lane is already running on CPU).

### 4.2 Assets already on disk (no download needed)

| Asset | Path |
|---|---|
| Old package (32.0.149.0) | `C:\Windows\System32\DriverStore\FileRepository\qcdx8480.inf_arm64_e11dd2e33e0b42d3\qcdx8480.inf` |
| Published name | `oem46.inf` |
| Vendor uninstaller | `C:\Program Files\Qualcomm\Installer\DriverUninstaller.exe 32.0.172.1` |
| Fallback full package | ASUS BSP `V1.312.4500.0` (394.71 MB, Graphic Driver 32.0.149.0) from the UX3607OA support page |

The old `qcdx8480.inf` folder contains the full set (`qcdx8480.cat`, `qcdxkm8480.sys`,
firmware `.mbn`), so it can be installed offline via "Have Disk".

### 4.3 Safety net first

1. Stop GPU work: close the panel and any `llama-server` GPU lane
   (`taskkill /IM llama-server.exe /F` if needed).
2. Create a restore point (elevated PowerShell):
   `Checkpoint-Computer -Description "Before Adreno 32.0.149.0 rollback"`
3. **Copy the old package out of DriverStore** to protect against Windows pruning unused driver
   packages later (elevated):
   `robocopy "C:\Windows\System32\DriverStore\FileRepository\qcdx8480.inf_arm64_e11dd2e33e0b42d3" "%USERPROFILE%\llmnpu\driver-32.0.149.0" /E`

### 4.4 Primary procedure — in-place downgrade (Device Manager)

> Note: `HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-…}\0000` has an **empty
> `RollbackDriver` value**, so the "Roll Back Driver" button is likely greyed out. Use
> "Let me pick", which forces a lower version and is the reliable path.

1. `devmgmt.msc` → **Display adapters** → **Qualcomm(R) Adreno(TM) X2-90 GPU** → right-click →
   **Update driver**.
2. **Browse my computer for drivers** → **Let me pick from a list of available drivers on my
   computer**.
3. Select **Qualcomm(R) Adreno(TM) X2-90 GPU — version 32.0.149.0** → **Next** → accept the
   "older driver" warning.
4. **Reboot.**
5. If 32.0.149.0 is not listed: **Browse** to the copied folder
   `%USERPROFILE%\llmnpu\driver-32.0.149.0` (or the DriverStore path) and install `qcdx8480.inf`
   via "Have Disk".

### 4.5 Fallbacks if 4.4 refuses the downgrade

- **A — force, same package:**
  `pnputil /delete-driver oem33.inf /uninstall /force` then repeat 4.4 step 5.
  *Risk:* the GPU falls back to Microsoft Basic Display (no WSLg) until 32.0.149.0 is installed —
  do not reboot in between.
- **B — vendor uninstall then install:**
  `"C:\Program Files\Qualcomm\Installer\DriverUninstaller.exe" 32.0.172.1`, reboot, then install
  32.0.149.0 per 4.4 step 5.
- **C — OEM package:** install ASUS BSP `V1.312.4500.0` (contains 32.0.149.0; BIOS 312 already
  matches).

### 4.6 Verify the downgrade

1. Version: `Get-CimInstance Win32_VideoController | Select Name,DriverVersion` → `32.0.149.0`.
2. Active package: `pnputil /enum-drivers` → `oem46.inf` / 32.0.149.0 on the device.
3. Rebuild the OpenCL backend and re-test the GPU (this is required — the deployed
   `pkg-opencl\bin` is currently CPU-only, only `ggml-base.dll` + `ggml-cpu.dll`):
   - run `scripts\build_opencl.bat` (expect BUILD_OK), deploy to `pkg-opencl\bin`;
   - `llama-bench.exe -m models\qwen3-8b-Q5_0.gguf -p 512 -n 128 -r 3 -ngl 99`.
4. Compare against the Sep-3 baseline: **prefill ≈261 t/s, decode ≈18 t/s** (`bench_compare.log`).
5. Confirm no new `CB.dll` APPCRASH events in Event Viewer (Application log) or
   `C:\ProgramData\Microsoft\Windows\WER\ReportArchive`.
6. Clear the confirmed blocker case: `geniex infer` on a GGUF **without** the
   `geniex-oldshadow` directory on `PATH` should now work. ~~If so, retire
   `%USERPROFILE%\llmnpu\geniex-oldshadow` (124 MB) — it only existed to dodge the new `CB.dll`.~~
   **DONE 2026-09-14:** `geniex infer local/qwen3-8b-q50` answered correctly
   with no crash; `geniex-oldshadow` deleted.

### 4.7 Prevent a silent re-upgrade

- Windows Update does **not** currently offer this GPU driver (only the ASUS manifest), so risk is
  low.
- Qualcomm Software Center updates only on user action — **decline** any prompt offering the
  Adreno graphics driver until a release newer than 32.0.172.1 appears.
- Keep both `oem33.inf` (32.0.172.1) and `oem46.inf` (32.0.149.0) staged.

### 4.8 Restoring 32.0.172.1 later

Same Device Manager path, selecting **32.0.172.1** (`oem33.inf` is still staged), or re-run
Qualcomm Software Center → *Adreno Graphics Drivers*. Do this only after re-checking
§3 for a newer release that fixes `CB.dll`.

---

## 5. Open follow-ups

- [ ] Watch Qualcomm Software Center / ASUS BSP for a driver newer than 32.0.172.1; re-test
      OpenCL (`llama OpenCL`) **and** Vulkan coopmat as soon as one lands.
- [ ] Once on 32.0.149.0, wire the OpenCL build back into the panel GPU lane and re-baseline.
- [ ] Follow-up from Session 5q: GenieX NPU-GGUF panel wiring (second `geniex serve` instance).
- [ ] **Security:** `C:\ProgramData\Qualcomm\SoftwareCenter\Logs\QSC-CLI\*.log` stores live
      Qualcomm OAuth access/refresh tokens in plaintext. Restrict/tidy those logs and treat the
      refresh token as compromised.
