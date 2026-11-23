# Local AI runtime contract

This package is an opt-in, offline text-inference lane. It has no Mac tools, shell, approvals, network client, or implicit model download. Exact fast commands must be routed upstream before general request routing.

## Available runtime and model catalog

- `LiteRtLmBackend` uses LiteRT-LM Android 0.16.1 on CPU and accepts only `.litertlm` files. It is built, but no model weights are bundled or installed, so actual inference has not been verified.
- `LocalModelCatalog.gemma3OneBInt4` is a 584,417,280-byte LiteRT-LM candidate requiring user acceptance of Gemma terms. The upstream model is gated.
- `LocalModelCatalog.qwen3ZeroPointSixBQ4` is an ungated Apache-2.0 429 MiB GGUF candidate with pinned SHA-256. It needs llama.cpp; current app runtime rejects GGUF explicitly. No Android llama.cpp native library is built here.
- There is no claimed minimum RAM for Qwen3 0.6B Q4_0. Device RAM, ABI, and Android API can be observed through `AndroidDeviceProfileReader`; only on-device testing can establish performance, thermals, or safe context settings.

The product catalog and license facts should be reviewed before any user-facing release; source repositories and model revisions can change.

## Acquisition and storage rules

`LocalModelAcquisitionPolicy` is a UI-independent consent gate, not a downloader. A future downloader must present the exact size, source, checksum, license, storage requirement, and network status before asking the user to start. Catalog downloads require an explicit user action, enough app-private storage, applicable license consent, and a separate choice for metered data. The current code performs no HTTP access. `LocalModelStore.importSelectedModel` only accepts a caller-provided stream, stores it under app-private files, checks byte count and SHA-256, records an install receipt, and supports cancellation.

Never put model weights in the APK, app assets, or source control. A model manager UI should launch acquisition on a background worker, report progress, and allow cancellation; it must not auto-fetch on app startup or silently use mobile data.

## Routing behavior

- `MAC_ONLY` always selects the Mac route.
- `LOCAL_ONLY` returns a clear rejection if no verified compatible model is loaded or the prompt exceeds conservative local criteria. It never falls back.
- `AUTO` chooses local only for small/simple text when runtime status confirms a model is ready; otherwise it chooses Mac. It may retry on Mac only when local inference failed before producing any text. It never retries partial output automatically.
- The exact-command classifier remains upstream and owns all command routing and authorization. Local generation is text-only and cannot execute actions.

## Device and runtime validation still required

The official vivo India X300 Pro page lists a 16 GB + 512 GB configuration; region/SKU may differ. The user’s actual hardware values have not been queried. Once authorized device testing is available: read the profile, import a deliberately chosen model over an explicitly selected transfer method, verify hash/license and free-space checks, then benchmark load time, generation latency, peak memory, thermal behavior, cancellation, and background/restart behavior. Do not mark local inference ready until genuine output from that model is recorded.

The official llama.cpp Android guide describes an arm64 NDK/CMake route (Android API 28+), with portable cross-build settings including `GGML_NATIVE=OFF`, `GGML_OPENMP=OFF`, `GGML_LLAMAFILE=OFF`, and `LLAMA_OPENSSL=OFF`. Adding it requires a separate native build/integration and runtime tests; this package does not embed or build llama.cpp yet.
