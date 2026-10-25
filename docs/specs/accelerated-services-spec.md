# Accelerated Services — Specification

**Status:** implemented 2026-10-02, phases 1–4 (§7). Phase 5 (Ollama modes) not built: Ollama
runs natively everywhere today. Phase 4 (NVIDIA) is implemented but untested - no machine here
has a usable NVIDIA setup (oldserver's podman 3.4.4 predates CDI). Tests:
`systems/backend/test_accelerated_services.py`.
**Builds on:** `redesign-ports-routes-spec.md` (the gateway and the `*.shoebox.lan` names),
`local-storage-spec.md` (host paths vs container paths), `ingest-move-spec.md`.
**Related:**
`start-all.sh`, `stop-all.sh`, `deploy/shoebox-env.sh`, `deploy/status.sh`,
`systems/backend-deepface/start-host.sh` and `start-podman.sh`,
`systems/gateway/shoebox.conf.template` (the upstreams),
`systems/backend/workflows/lib/utilities.py` (`create_video_proxy`, `create_video_poster`).

---

## 1. Goal

Use the GPU and the hardware video engines where a machine has them, without making the rest of
Shoebox care where that happens.

- **On a Mac**, Apple's Metal and VideoToolbox are reachable only from native processes: podman
  runs containers in a Linux VM with no GPU passthrough.
- **On Linux with an NVIDIA card**, containers *can* use the GPU, given the NVIDIA container
  toolkit and one podman flag.
- **Everywhere else** (oldserver: an i7-4940MX and a GeForce GTX 880M, too old for current CUDA and
  without HEVC decode), everything stays on the CPU, in containers.

The rule this spec sets down:

> GPU work lives in **services with a network address**. Each machine decides, per service,
> whether that service runs **natively** or **in a container** (with or without the GPU).
> The backend and the workflows always run in containers and only ever call those services by
> URL. Nothing in Shoebox executes commands on the host on the backend's behalf.

## 2. What exists today

### 2.1 The pattern is already there, once

`backend-deepface` has two start scripts:

| Script | Runs | Used by `start-all.sh` |
|---|---|---|
| `start-host.sh` | natively, in a virtualenv, with `tensorflow-metal` | on macOS |
| `start-podman.sh` | in a container, CPU | on Linux |

The backend calls it at `DEEPFACE_URL` (`http://deepface.shoebox.lan`); the gateway forwards to
`${UPSTREAM_HOST}:5001`. Because every service — native or container — publishes its port on
the host, the gateway reaches it the same way in both modes. **Where a service runs is invisible
to its callers.** That property is what this spec generalises.

### 2.2 Where the heavy work actually is

| Work | Where it runs today | Uses the GPU? |
|---|---|---|
| Face embeddings (VGG-Face, TensorFlow) | `backend-deepface` | Metal on a Mac (native); CPU elsewhere |
| Face detection (YuNet, OpenCV DNN) | `backend-deepface` | no — CPU in both modes |
| Image descriptions, album text (LLMs) | **Ollama**, called by `backend-vision` and `backend-albums` | Ollama runs **natively on the host** already and uses the GPU itself |
| Video previews and posters (ffmpeg) | **inside the import workflow**, in the backend container | no — software decode and `libx264` |
| Photo previews (PIL) | inside the import workflow | no — and not worth it (§3.4) |

Two consequences:

- `backend-vision` and `backend-albums` run **no models of their own**; they are thin HTTP
  clients of Ollama. They gain nothing from native mode and stay in containers.
- **Video transcoding is the one heavy job with no service boundary.** It runs inside the
  backend container, so it can never reach VideoToolbox or NVENC. On oldserver it was the long pole
  of the family import: a 6-minute 4K HEVC clip took ~9 minutes on all 8 cores, decoding alone
  ~1.5× real time. 63,000 videos are queued there.

## 3. Design

### 3.1 Services and their modes

Each accelerated service gets a mode, chosen per machine:

| Mode | Meaning |
|---|---|
| `host` | native process on the host (`start-host.sh`) |
| `container` | container, CPU only (`start-podman.sh`) |
| `gpu` | container with the NVIDIA GPU passed in (`start-podman.sh` + CDI device, §3.3) |
| `off` | not started; callers fall back or report the service as unavailable |
| `auto` *(default)* | per platform and per service — see the matrix below |

`auto` is not "native on macOS". It is a decision per platform **and** per service, because what
host mode buys differs by both:

| Platform | `backend-deepface` | `backend-transcode` |
|---|---|---|
| macOS arm64 (Apple Silicon) | `host` — Metal | `host` — VideoToolbox |
| macOS x86_64 (Intel) | `container` — see §3.1.1 | `host` — VideoToolbox |
| Linux x86_64 | `gpu` when usable (§3.3), else `container` | same |
| Linux arm64 (Raspberry Pi 64-bit) | `container` | `container`; `host` opt-in for V4L2 |
| Linux arm32 | unsupported — no TensorFlow wheels at all | `container` |

### 3.1.1 `host` is conditional on the host, and says so

Both fallbacks have the same shape, and the rule is **ask whether the machine can do it before
choosing it, then name what was chosen instead**:

| Asked for | Condition | Result |
|---|---|---|
| `gpu` | no driver, no CDI spec, or podman < 4.1 | `container`, with the reason |
| `host` | the platform has no wheels for it | `container`, with the reason |
| `host` | no host Python ≥ 3.10 | `container`, with the reason |
| `host` (transcode) | no `ffmpeg`/`ffprobe` on the host | `container`, with the reason |

A container image carries its own Python and its own ffmpeg, so it always works. A missing host
tool therefore costs **the accelerator, not the service** — and the reason is printed by
`start-all.sh`, `deploy/deploy.sh` and `deploy/status.sh`, which all call the same
`shoebox_service_mode`.

Two platform facts drive the deepface row, and either alone would be enough:

* `tensorflow-metal` is Apple-Silicon-only, so a native deepface on an Intel Mac runs the same CPU
  code as the container with none of the isolation;
* TensorFlow has published **no macOS x86_64 wheel since 2.16**, while `requirements.txt` needs
  ≥ 2.20 (`tf-keras>=2.20`). Natively on an Intel Mac it cannot be installed — `pip` ends in
  `ResolutionImpossible`. The container image is `linux/amd64`, which is one of the four platforms
  2.20 does publish for (macOS arm64, Linux x86_64, Linux aarch64, Windows amd64).

### 3.1.2 A container's memory is not the machine's memory

On macOS every container shares **one podman VM**, created with a fixed allocation — 2048 MB by
default, whatever the Mac has. The host's RAM is invisible in there, and nine containers divide
what the VM was given.

`backend-deepface` is the only service this constrains, and it constrains it hard: each gunicorn
worker builds its own VGG-Face, ~600 MB resident, because the models cannot survive a `fork()`
(§3.1.3). A fixed two workers in a 2 GB VM running the rest of the stack produces

```
[ERROR] Worker (pid:NNN) was sent SIGKILL! Perhaps out of memory?
```

in a loop, and nothing in that message, in `podman ps`, or in the application names memory as the
cause. So the mode decision is not the only sizing decision:

| Measured | Response |
|---|---|
| memory available to a container | `shoebox_container_memory_mb` — the VM's allocation on macOS, `MemTotal` on Linux |
| workers that fit | `shoebox_deepface_workers` — a memory division (~1.1 GB each, 700 MB reserved), never a core count; at least 1 |
| below `SHOEBOX_DEEPFACE_MIN_MB` (3072) | the warm-up is turned **off** so the worker boots at all, and the VM-resize command is printed |

With the warm-up off the service answers, YuNet detection works, and the embedder is built on the
first `/api/embed`. "Recognition needs more memory" is a usable failure; "the face service never
started" is not. `start-all.sh` and `deploy/deploy.sh` both report the figure, because on a Mac it
is the single most surprising limit in the stack.

### 3.1.3 A virtualenv is ready when its entry point is installed

`host` mode runs out of a venv beside the service. Readiness is **never** "the directory exists":
that test is what made a failed first deploy permanent and silent. It is three independent
questions, and the stamp that records the answers is written only after `pip` succeeds:

| Checked | Why it is separate |
|---|---|
| the interpreter's version | a venv below the floor can never install the requirements; it is **replaced**, not repaired |
| `bin/<entry>` exists | the thing the service `exec`s — `gunicorn` |
| a stamp of `requirements.txt` | editing that file reinstalls instead of running yesterday's packages |

Implemented once, in `tools/shoebox-venv.sh` (`shoebox_venv_state`, `shoebox_venv_ensure`), and
used by both `start-host.sh` scripts and by `deploy/deploy.sh`. Every start checks — not only the
first — so a half-built venv is rebuilt rather than mistaken for a finished one.

The services this applies to:

| Service | Port | `host` gains | `gpu` gains |
|---|---|---|---|
| `backend-deepface` | 5001 | Metal (embeddings) | CUDA (embeddings; detection too if OpenCV is built with CUDA — optional) |
| `backend-transcode` *(new, §3.2)* | 5004 | VideoToolbox decode + encode | NVDEC decode + NVENC encode |
| Ollama | 11434 | Metal / CUDA (already how it runs) | official `ollama/ollama` image with the GPU |

#### Video encoders, and the device each one needs

`gpu` mode means NVIDIA-through-CDI, but it is not the only hardware encoder. The others are
reached by handing the **container a device file**, which `start-podman.sh` does when the device
exists — so they work in plain `container` mode:

| Encoder | Hardware | Needs | Mode |
|---|---|---|---|
| `h264_videotoolbox` | Apple | native process | `host` on macOS |
| `h264_nvenc` | NVIDIA, **closed driver** | CDI device | `gpu` |
| `h264_qsv` | Intel Quick Sync | `/dev/dri/renderD*` | `container` |
| `h264_vaapi` | Intel or AMD | `/dev/dri/renderD*` | `container` |
| `h264_v4l2m2m` | Raspberry Pi 4 | `/dev/video11` | `container` |
| `libx264` | — | nothing | always last |

Three things this table is load-bearing about:

* **NVENC lives in NVIDIA's closed driver.** A machine running the open **nouveau** driver has a
  render node, exposes no VA-API encoder, and answers `No VA display found` the moment anything
  opens it — so a capable card can still have no usable encoder. Measured on oldserver (two GTX 880M,
  nouveau, 2026-10-05): no hardware encode of any kind, software x264 only.
* **A device that exists is not a device that encodes.** Every candidate is proved by a one-frame
  encode built the same way as the real command — device flags, filter chain and all (§3.1.4).
* **Rootless podman needs the group.** `/dev/dri/renderD*` is mode 0660 group `render`; the
  container gets it through `--group-add keep-groups`, and the invoking user must be in `render`.

### 3.1.4 Hardware scaling changes the filter chain, not the ladder

A hardware encoder cannot be dropped into the software command. VA-API scales on the device, so
the frames have to get there: `format=nv12,hwupload,scale_vaapi=W:H`, with **integer** dimensions,
because `scale_vaapi` does not take `scale`'s `min()`/`-2` expressions. `target_size()` computes
that pair from the same rule the software filter expresses, so there is one definition of "long
edge to max_edge, never upscale, even numbers" rather than one per device.

Hardware **decode** is deliberately not used with QSV, VA-API or V4L2. It is all-or-nothing inside
one command: a source the device cannot decode (10-bit HEVC on pre-Icelake Intel, anything exotic
on a Pi) fails the command and loses the encode with it. Software decode plus hardware encode
still moves the expensive half onto the device.

Configured in `deploy/shoebox.env`, read by `start-all.sh`, `stop-all.sh` and `deploy/status.sh`:

```bash
SHOEBOX_DEEPFACE_MODE=auto      # auto | host | container | gpu | off
SHOEBOX_TRANSCODE_MODE=auto
SHOEBOX_OLLAMA_MODE=host        # Ollama is installed natively today; keep that the default
```

`start-all.sh` replaces its current `if Darwin` block for deepface with one function that turns a
mode into the right start script and flags, and prints the decision:

```
── backend-transcode ── mode auto → gpu (NVIDIA RTX 3060, CDI device nvidia.com/gpu=all)
```

### 3.2 `backend-transcode`: video previews as a service

A small HTTP service owning ffmpeg, so the hardware path exists wherever the hardware does.

**API** (JSON; the caller waits for the result, as it waits for ffmpeg today):

```
POST /transcode   {"catalog": "<slug>", "source": "Videos/2023/10/27/x.MOV",
                   "proxy": "cache/Videos/2023/10/27/x_proxy.mp4",
                   "max_edge": 1280, "crf": 23}
  → 200 {"action": "transcode|remux|copy", "encoder": "h264_videotoolbox", "seconds": 41.2,
         "width": 1280, "height": 720, "duration": 371.8, "size": 97698586}
POST /poster      {"catalog": "<slug>", "source": "...", "poster": "...", "size": 320}
GET  /health      {"encoders": ["libx264", "h264_videotoolbox"], "decoders": ["hevc_videotoolbox"], ...}
```

**Files, not bytes.** A video is gigabytes; streaming it over HTTP and back would double the disk
work this spec is trying to save. The service reads and writes the catalog directly, by
**catalog-relative path**, resolved against its own `CATALOGS_ROOT`:

| Mode | `CATALOGS_ROOT` |
|---|---|
| `host` | `CATALOGS_HOST_DIR` (`/opt/shoebox/catalogs`, `~/…/shoebox/catalogs`) |
| `container` / `gpu` | `/catalogs`, the same bind mount the backend has |

Paths are contained the same way the gallery contains them (realpath under the catalog; no `..`,
no absolute paths). A catalog whose originals are bind mounts from another disk (oldserver) works
unchanged in `host` mode, because the host sees the same mounts.

**Encoder choice**, per request, best first, from what `/health` probed at start:

| Hardware | Decode | Encode |
|---|---|---|
| Apple Silicon / recent Intel Mac | `-hwaccel videotoolbox` | `h264_videotoolbox` (bitrate-targeted; quality setting mapped from CRF) |
| NVIDIA (Maxwell 2nd gen or later for HEVC) | `-hwaccel cuda` | `h264_nvenc -preset p5 -cq <crf>` |
| none | software | `libx264 -preset veryfast -crf <crf>` (today's settings) |

A hardware attempt that fails (an unsupported profile, a 10-bit source on an old card) is retried
in software once, and the response says so. The proxy format does not change: H.264 MP4,
`+faststart`, AAC stereo — what browsers and the album gallery already play.

**The workflow side.** `create_video_proxy` / `create_video_poster` gain one branch: when
`TRANSCODE_URL` is set and answers, call it; otherwise run ffmpeg locally as now. So:

- nothing breaks on a machine without the service;
- the import's render lane (`render_video_task`) needs no change beyond that branch;
- `IMPORT_VIDEO_PRESET` and `IMPORT_VIDEO_NICE` keep governing the local fallback.

### 3.3 NVIDIA in containers (Linux)

- Prerequisites, documented and checked, never installed silently: the NVIDIA driver,
  `nvidia-container-toolkit`, and a CDI spec (`nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`).
- "Usable" for `auto` means: `nvidia-smi` succeeds **and** `/etc/cdi/nvidia.yaml` (or
  `/var/run/cdi/`) lists `nvidia.com/gpu`. Anything less falls back to `container`, and
  `start-all.sh` says which check failed.
- `start-podman.sh` adds `--device nvidia.com/gpu=all` in `gpu` mode. Images that need CUDA
  libraries (`backend-deepface`, `backend-transcode`) get a `-gpu` build variant from a CUDA base
  image; the CPU image stays small for everyone else.
- Too-old cards (oldserver's Kepler GTX 880M: driver 470 legacy, CUDA ≤ 11.4, no HEVC decode) are
  detected by `/health` reporting no useful encoders, and `auto` picks `container`.

### 3.4 What stays as it is

- **The backend and every workflow stay in containers.** They keep their mounts and their
  isolation; they call services by URL.
- **Photo previews stay in the import.** Resizing a JPEG is fast on the CPU and dominated by disk
  reads; a GPU round trip would cost more than it saves.
- **`backend-vision`, `backend-albums`, `backend-geoloc`, the gallery, the frontend and the
  gateway** stay in containers.

## 4. Rejected: a native "tools" folder the backend calls

The alternative considered: a host-side folder of tools, plus a mechanism for the containerised
backend to run them natively. Rejected because:

1. **It is remote command execution.** Whatever lets the backend say "run this on the host" is
   the hole the container boundary exists to close. It would need its own authentication,
   allow-list, process supervision and output streaming — a second, weaker backend.
2. **It ties workflows to a host's installed software.** A workflow would succeed on one machine
   and fail on another depending on what is in a folder, not on what is in its image.
3. **It splits one job across two process trees** with nobody owning failure, timeouts or stops.

A service with a narrow API (§3.2) gives the same hardware access with none of these: it does one
thing, validates its inputs, and is started, stopped and health-checked like every other service.

## 5. Security

- Native services bind to the **podman host interface and localhost only**, never `0.0.0.0` on a
  machine with a LAN address — the gateway is the only way in from outside, as for containers.
- `backend-transcode` accepts catalog-relative paths only, resolved and contained under its
  `CATALOGS_ROOT`; it never takes a command line, a codec string or an output path outside
  `cache/`.
- No service started natively runs as root.

## 6. Operations

- `deploy/status.sh` lists each accelerated service with its **mode and encoder/device**:
  `✓ backend-transcode - up 2h (gpu: h264_nvenc, hevc decode)`.
- `stop-all.sh` stops native processes by their pid files, as it does deepface's today.
- Native services log to `logs/<service>.log` like the others. **One previous log is kept** as
  `logs/<service>.log.1`: truncating on every start is how the first failure's cause gets
  overwritten by its consequence, which is exactly what happened to a new Mac deployment in
  October 2026 — "pip install failed" became "gunicorn: not found" and the cause was gone.
- `start-all.sh` watches the **pid** of a native service as well as its port, so one that exits
  during boot is reported in a second rather than after the full 90-second timeout, with the tail
  of its log printed where the error actually is. A pid file naming a dead process is removed.
- When a native service is down, `deploy/status.sh` reports **why** from what is readable on the
  machine — a dead pid, a venv below the Python floor, a missing host tool — and never advises
  re-running the command that cannot fix it.
- `deploy/deploy.sh` is the one place the host half is prepared: it reports the platform, the
  Python and ffmpeg it found, the mode each service resolves to, and it **builds the virtualenvs
  and caches the face models** for whatever will run natively here.

## 7. Implementation phases

1. **Modes for deepface.** Move the macOS-only logic in `start-all.sh` into a mode function;
   `SHOEBOX_DEEPFACE_MODE`; status and stop support. No behaviour change on any current machine.
2. **`backend-transcode`, CPU first.** The service, its container, `TRANSCODE_URL`, the workflow
   branch with local fallback. Same output as today — this phase moves work, it does not speed it up.
3. **VideoToolbox (`host` mode on a Mac).** Encoder probing, hardware path, software retry.
4. **NVIDIA (`gpu` mode).** CDI detection, `-gpu` image variants, NVENC/NVDEC path; the same for
   deepface.
5. **Ollama modes,** if a Linux deployment wants Ollama in a GPU container rather than native.

## 8. Testing

- Mode resolution, per platform and per service against the §3.1 matrix. Both variables are
  readable, so the whole matrix is testable on one machine:
  `SHOEBOX_OS=Linux SHOEBOX_ARCH=arm64 shoebox_service_mode DEEPFACE`.
- Mode fallbacks: Linux + working CDI → `gpu`; Linux + `nvidia-smi` but no CDI → `container` with
  the reason; `off` → not started and reported; `host` asked for on a machine with no suitable
  Python, or no ffmpeg for transcode, or no wheels for the platform → `container` with the reason.
- Venv readiness: a venv built with a Python below the floor is replaced; one missing its entry
  point is reinstalled; an edited `requirements.txt` reinstalls; a failed `pip` leaves **no** stamp,
  so the next start retries instead of exec'ing a binary that was never installed.
- `backend-transcode`: path containment (absolute, `..`, symlink out of the catalog all refused);
  remux/copy/transcode decisions identical to `create_video_proxy` today; hardware failure →
  software retry, reported.
- Workflow: with `TRANSCODE_URL` unset or down, proxies are made locally exactly as now.
- Output: a proxy from each encoder plays in Firefox, Safari and the album gallery, with
  `+faststart`.
- Throughput, recorded per machine: seconds per minute of 4K HEVC and of 1080p H.264 for each
  encoder, so the gain is measured rather than assumed.

## 9. Open questions

1. **VideoToolbox quality — settled.** Measured on an M3 Pro, iPhone 1080p HDR footage, against
   `libx264 -crf 23 -preset veryfast` (SSIM against the source at proxy size):

   | Encoder | 21 s clip | SSIM | 3-min clip: wall / CPU |
   |---|---|---|---|
   | libx264 veryfast CRF 23 | 2.3 s, 7.4 MB | 0.9175 | 15.6 s / 165 CPU-s (10.5 cores) |
   | VideoToolbox q55 | 1.7 s, 11.7 MB | 0.9120 | — |
   | VideoToolbox q60 | 1.7 s, 15.5 MB | 0.9173 | 13.8 s / 46 CPU-s (3.4 cores) |

   On Apple Silicon the hardware encoder is **not much faster; it uses ~70% less CPU**, at
   1.6–2× the file size. Default `TRANSCODE_VT_QUALITY=55`; `TRANSCODE_ENCODER=libx264` prefers
   smaller files instead. NVENC is expected to be the bigger win on machines with weak CPUs.
2. **Concurrency.** One NVENC session is fast; consumer NVIDIA cards allow a limited number of
   concurrent encodes. Default the service to 2 concurrent jobs, configurable.
3. **Re-encoding the existing backlog.** Proxies already made are kept — **except** those of
   10-bit sources: before this work, libx264 kept 10 bits and wrote H.264 *High 10*, which
   Firefox and Chrome cannot play (found on oldserver: iPhone HDR footage). Every encoder now writes
   8-bit `yuv420p`; the 10-bit proxies already made need rebuilding.

## 10. Checklist

```
Modes
[x] SHOEBOX_<SERVICE>_MODE with auto/host/container/gpu/off              (§3.1)
[x] start-all.sh / stop-all.sh / status.sh use one mode function          (§3.1, §6)
[x] deepface moved onto it, no behaviour change                           (§7.1)

backend-transcode
[x] Service: /transcode, /poster, /health; catalog-relative, contained    (§3.2, §5)
[x] Workflow branch with local fallback (TRANSCODE_URL)                   (§3.2)
[x] VideoToolbox path, software retry                                     (§3.2, §7.3)
[x] NVENC/NVDEC path, CDI device, -gpu image                              (§3.3, §7.4)

Safety and operations
[x] Native services bind to localhost / podman host interface only       (§5)
[x] Status shows mode and encoder per service                             (§6)
[ ] Throughput recorded per machine and encoder                           (§8)
```
