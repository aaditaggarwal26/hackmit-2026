## cpu — 2026-09-19T22:25:42+00:00 on gx10-f548

Workload: protocol.md §5.2 scoring kernel per 128x128 uint8 frame: cloud count (px > CLOUD_THRESHOLD), change count (|frame-ref| > CHANGE_THRESHOLD), interior 3x3 Sobel sum |Gx|+|Gy|, saturating composite; one frame scored per call, one host sync per frame.

Methodology: (a) idle baseline recorded for 5.0 s with all samplers running before any load; (b) one warm-up run of 3.0 s, discarded; (c) 3 timed repetitions of 3.0 s, per-frame latency via time.perf_counter_ns; (d) per rep: frames, frames/s, latency percentiles, energy, mean W, marginal W (load minus idle), J per 1000 frames total and marginal; (e) mean and sample stddev across reps; (f) thermal throttling flag from Processor cooling devices. Samplers polled at 10.0 Hz.

| figure | value | scope | method |
|---|---|---|---|
| frames/s | 11391.85 ±26.78 frames/s [workload] | workload | mean over 3 reps of: frames completed / wall time of the rep (perf_counter_ns) |
| mean W | n/a | cpu_rail | none |
| marginal W (load - idle) | n/a | cpu_rail | none |
| J per 1000 frames (total) | n/a | cpu_rail | none |
| J per 1000 frames (marginal) | n/a | cpu_rail | none |
| throttling observed | no | board_thermal_zones | any Processor cooling_device*/cur_state > 0 during the window |
| max thermal-zone temp | 53.7 degC | board_thermal_zones | max over samples and zones of /sys/class/thermal/thermal_zone*/temp |
| cold idle mean W | 4.09 W | gpu_die | sleep with samplers running before any load: no corpus loaded, no CUDA context |

Scope note: every power/energy figure above is GPU-die only (NVML energy counter); it excludes the CPU, memory and the rest of the board. There is no CPU-rail or board meter on this machine, so CPU-tier power is unavailable, not zero, and the GPU-die reading recorded during the CPU run is context, not CPU power. Die figures and board figures are never to be compared as if they were the same quantity.

Environment: python 3.13.15, numpy 2.5.3, torch 2.14.0+cu130, driver 580.159.03, cpu Cortex-X925, pinned core 5, gpu processes present: none.

- unavailable (cpu_rail: no CPU or board power instrumentation on GX10; needs inline USB-C PD meter)
## gpu — 2026-09-19T22:25:59+00:00 on gx10-f548

Workload: protocol.md §5.2 scoring kernel per 128x128 uint8 frame: cloud count (px > CLOUD_THRESHOLD), change count (|frame-ref| > CHANGE_THRESHOLD), interior 3x3 Sobel sum |Gx|+|Gy|, saturating composite; one frame scored per call, one host sync per frame.

Methodology: (a) idle baseline recorded for 5.0 s with all samplers running before any load; (b) one warm-up run of 3.0 s, discarded; (c) 3 timed repetitions of 3.0 s, per-frame latency via time.perf_counter_ns; (d) per rep: frames, frames/s, latency percentiles, energy, mean W, marginal W (load minus idle), J per 1000 frames total and marginal; (e) mean and sample stddev across reps; (f) thermal throttling flag from Processor cooling devices. Samplers polled at 10.0 Hz.

| figure | value | scope | method |
|---|---|---|---|
| frames/s | 7810.64 ±8.71 frames/s [workload] | workload | mean over 3 reps of: frames completed / wall time of the rep (perf_counter_ns) |
| mean W | 13.76 ±0.03 W [gpu_die] | gpu_die | mean over 3 reps of: NVML nvmlDeviceGetPowerUsage (mW) polled at 10 Hz |
| marginal W (load - idle) | 9.64 ±0.03 W [gpu_die] | gpu_die | mean over 3 reps of: rep mean W - cold idle mean W; NVML nvmlDeviceGetPowerUsage (mW) polled at 10 Hz |
| J per 1000 frames (total) | 1.63 ±0.00 J [gpu_die] | gpu_die | mean over 3 reps of: energy J * 1000 / frames; NVML nvmlDeviceGetTotalEnergyConsumption (mJ) counter delta, stop - start |
| J per 1000 frames (marginal) | 1.23 ±0.00 J [gpu_die] | gpu_die | mean over 3 reps of: (rep mean W - cold idle mean W) * wall s * 1000 / frames |
| marginal W vs armed idle (CUDA context resident) | 4.36 ±0.03 W [gpu_die] | gpu_die | mean over 3 reps of: rep mean W - armed idle (CUDA context resident) mean W; NVML nvmlDeviceGetPowerUsage (mW) polled at 10 Hz |
| J per 1000 frames (marginal vs armed idle) | 0.56 ±0.00 J [gpu_die] | gpu_die | mean over 3 reps of: (rep mean W - armed idle (CUDA context resident) mean W) * wall s * 1000 / frames |
| throttling observed | no | board_thermal_zones | any Processor cooling_device*/cur_state > 0 during the window |
| max thermal-zone temp | 52.1 degC | board_thermal_zones | max over samples and zones of /sys/class/thermal/thermal_zone*/temp |
| cold idle mean W | 4.12 W | gpu_die | sleep with samplers running before any load: no corpus loaded, no CUDA context |
| armed idle mean W | 9.40 W | gpu_die | sleep with samplers running after CUDA context creation and tensor upload, no work |

Scope note: every power/energy figure above is GPU-die only (NVML energy counter); it excludes the CPU, memory and the rest of the board. There is no CPU-rail or board meter on this machine, so CPU-tier power is unavailable, not zero, and the GPU-die reading recorded during the CPU run is context, not CPU power. Die figures and board figures are never to be compared as if they were the same quantity.

Environment: python 3.13.15, numpy 2.5.3, torch 2.14.0+cu130, driver 580.159.03, cpu Cortex-A725, pinned core None, gpu processes present: none.

- unavailable (cpu_rail: no CPU or board power instrumentation on GX10; needs inline USB-C PD meter)
- unavailable (cpu_time: no core pinned)
