# Follow the official hardware-driven model platform

The Model Platform follows sunnypilot's official QCOM/Chestnut policy. QCOM
and Chestnut retain independent selected bundles, while physical Chestnut
availability and runtime state determine which slot is active. A healthy,
connected Chestnut uses the Big Model slot; without Chestnut the QCOM Small
Model slot is active. `ModelManager_ActiveSource` is migration data only and
is not authoritative.

## Consequences

- The official Model Selector, ref downloads, verification, queueing, cache
  cleanup, status text, and failure semantics remain the source baseline.
- Selecting a Small Model while Chestnut is healthy prepares the QCOM slot but
  does not make it the active driving model. Safe-eject or disconnect Chestnut
  to use the QCOM slot.
- A C3XL Model Adapter may report hardware capability and preserve the 120
  second load allowance, loading progress, downloaded-bundle readiness,
  compile-CPU selection, UT3G identity, telemetry, and safe eject. It must not
  implement a second product-selection algorithm.
- Legacy USBGPU bundle and catalog Params migrate once into the official
  Chestnut slots. Selector-v17 and off-catalog bundles are not retained as
  active choices.
- The model files remain in place; a matching selector-v18 ref and artifact
  hash can be verified and reused without downloading again.
- Default Big requires its official compiled artifact. Downloaded LM/TT/IDM
  bundles remain independent of the optional big ONNX source.

## Model artifact and execution compatibility

The supported pickle format and compiler input-shape rules follow the selected
sunnypilot source baseline. When upstream retires an old format, retire its
local loader and execution branches too. Previously downloaded artifacts do
not justify retaining a parallel model runtime or legacy shape rules.

Use matching official artifacts for the selected runtime. Keep downloaded
files until explicitly removed, but do not silently convert their metadata,
claim compatibility, or select an older execution path to keep them running.
The stock and modeld_v2 runners remain upstream-owned; hardware adapters must
not introduce an independent QCOM-warp or fused-runner implementation.

The 2026-09-06 C3XL validation follows sunnypilot PR #1993 at
`d71635410d8bf4312ea358becd7f9e5e27fcf0d6` and its v25 Chestnut catalog.
Supercombo bundles use upstream's packed NPY-to-AMD `run_model`; split bundles
retain the upstream separate warp/policy path. The C3XL seam is limited to
hardware detection, persistent firmware/cache configuration, the measured
load timeout, loading progress, telemetry, power policy, and reliable fallback.
It does not choose a model execution format or preserve a retired artifact ABI.

The 2026-09-09 dev release source `b255314f5ad9ff46f5561c7c2ef21c5b3c6585e7`
introduces the official dynamic tinygrad pickle loader and pins tinygrad to
`f6fc4e3f2c3db5fae1e19cbfbc3ad9fc579a12ae`. Follow that loader as one upstream
module. C3XL byte progress and complete-buffer reads wrap its input stream in
the existing `egpu_loader` adapter, without changing class/enum compatibility
inside the official loader. Retired nested execution formats remain retired.

Loading a dictionary is not an inference acceptance test. Check old compiled
graphs against the new runtime and compare native model outputs. The initial
TSFM check ran each runtime for 600 frames and compared 275 sampled arrays;
this establishes that bounded QCOM comparison, not eGPU validation or a 20 Hz
performance guarantee. Camera-offset geometry separately follows dzid26
`43fc79d4371c8ca9d939f1a1875630d56f3c3458`, with zero-offset and C3XL camera tests.
