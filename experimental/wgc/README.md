# Local WGC one-attempt backend

The default strict policy remains closed: the recorded composed scene has 108 masks, 3 provider errors and zero unmasked message pixels. Metadata/item creation and synthetic GPU tests do not prove real message access. No real WGC frame, screenshot or recognized text was captured during implementation.

`user_confirmed_roi_once` is an independent, disabled-until-click policy. Its full native/Python pipeline is implemented and compiled, but the real frame/OCR path has not been exercised. A current, visible confirmation UI action is required at the very end. A pending descriptor, saved audit file, an old project authorization, or JSON containing `authorization_granted=true` cannot authorize it.

## One command after deployment

```powershell
powershell -NoProfile -File "D:\Program Files\DingTalkBridge\experimental\calibration\Start-Once.ps1" -ConfigPath "D:\Program Files\DingTalkBridge\config.local.json"
```

This prepares fresh metadata and opens the separate confirmation tool only if technical readiness passes. The person must inspect the outlined message region, confirm that it contains only intended messages, and click the one-time read-and-local-MCP action. Cancellation, expired evidence, focus/input/window changes or a technical failure invalidates the attempt; there is no automatic retry. `-CheckOnly` performs metadata readiness without displaying UI or authorizing capture. Do not launch a real read during deployment acceptance.

## Implemented path and boundaries

The reviewed UI pins Python, adapter, config and backend hashes. The actual click supplies a fresh challenge response and two separate single-consumption auto-reset events, with child handles limited to SYNCHRONIZE; a third event revokes. Python verifies the live OS UI parent. Native code independently verifies its real Python/UI parent chain and fixed UI path before reading supplied local binding files. Nonlocal paths, malformed rectangles, stale nonces and deadlines fail closed. A UI-owned kill-on-close Job contains the process tree; Python/native also monitor UI death/revocation and have bounded cleanup.

The native helper accepts only its private `--live-ui-once` pipe protocol. Before creating any framepool it consumes its own UI event and rechecks exact application SHA/path/PID/start/session, top/semantic/CEF HWND/classes/bounds/ancestry, WDA=0, uncloaked state and input/focus epoch. Technical identity, coordinate, owner, protection and strong-input errors cannot be waived by semantic confirmation.

The separate manual technical gate validates every classified provider error against exactly one complete native-owner alternative, with unchanged source rows, real parent/client-chain evidence and recomputed 8-pixel superset padding. Known strong inputs receive 2-pixel padding. Python supplies the final clipped ROI-pixel masks once; native code verifies their exact union area without adding padding again. Strict masks/gate stay unchanged. Unknown layout semantics are explicitly marked `manual_semantic_confirmation`, never automatic proof.

Capture uses HARDWARE D3D11 only and the selected DingTalk top-level WGC item. No monitor/desktop capture, renderer-memory read, injection, debug flag, privacy-setting change, border change or authorization bypass exists. Item/frame/texture sizes and physical DWM/window origin must agree. A fresh bounded frame is selected using SystemRelativeTime/QPC. The full source texture is GPU-only (CPUAccess=0). An explicit GPU ROI copy and opaque GPU masks precede any CPU access. Python refreshes and compares the complete scene before permitting Map; native rechecks identity/epoch. Only the masked ROI becomes CPU staging/SoftwareBitmap/Windows OCR. A second fresh scene check precedes Store.ingest. Raw pixels are never saved or printed; native OCR text travels only over the private pipe to Python/Store.

OCR records remain `windows_ocr_screen`, `screen_context`, `is_message=false`, unknown account/sender/conversation, observation time and confidence=0 (Windows OCR provides no numeric confidence). Region text is partial and cannot establish complete history or verified-message identity. Public status contains only bounded metadata/counters and a collector session ID; Store/MCP use the same configured data directory.

## Build and safe verification

```powershell
& .\scripts\Build-Native.ps1
$env:PYTHONPATH = (Join-Path (Get-Location) src)
python -m unittest discover -s tests -p 'test_ocr*.py' -q
& .\experimental\wgc\WgcOnce.exe --guard-selftest
```

Build compiles only. Source tests use `tests/fixtures/ocr_once_scene.synthetic.json`, with invented system identities and no runtime-report dependency. If the native executable has not been built, its Windows guard test is explicitly skipped with the build command; the compiled deployment still executes that test. The 22 OCR tests include strict proof guards, exact alternative error/mask mutations, stable scene binding, pending/literal-true rejection, a real native forged request rejected at the production OS-parent-chain branch, and a synthetic factory-to-real-test-Store flow. No test starts a real framepool. The shared GPU primitive was separately verified with synthetic HARDWARE pixels: 421 masked / 859 outside, 0 mismatches, nontrivial RowPitch, invalid-rect and all-masked guards. Callback lifetime uses heap shared state captured by value and RAII event revocation; exceptional cleanup never retains recognized text in failure output.

Not yet verified: a real user click, real WGC frame arrival/GPU readback on DingTalk, real region OCR, and real-message MCP proof. A successful build or `ready=true` means technical preparation, not consent or message success. `summary.status=completed` means the attempt completed; only `ingested_screen_contexts>0` proves Store accepted a screen context. Filtered/duplicate records have a separate reason. Verified-message count stays zero.

## Exact runtime/build resources

- `src/dingtalkbridge/ocr.py` and existing config/collector/models/Store package modules.
- `native/DingTalkUia.cs` (runtime compiled helper; launcher also uses `native/bin/DingTalkUia.exe`).
- `experimental/wgc/WgcOnce.exe`, `WgcOnce.build.json`, `WgcOnce.cpp`, `OnceGuard.h`, `GpuRoiMask.h` (build/source digest pins).
- `experimental/wgc/MetadataProbe.exe` for metadata-only launcher mapping, and `MetadataProbe.cpp` / `Build.ps1` for reproducible builds.
- `experimental/calibration/bin/DingTalkRegionCalibration.exe`, `RegionCalibration.cs`, `BackendBridge.cs`, `BackendAdapter.py`, `Prepare-BackendBinding.py`, `Prepare-Descriptor.ps1`, `Start-Once.ps1` (UI-owned files).
- Strict OCR remains the separate three helpers `Capture-Ocr.ps1`, `LocalOcr.ps1`, `SafeWindowCrop.cs`.

Do not package temporary scene/profile/binding/audit files, screenshots, raw text, `.obj`, test Store databases or generated test artifacts as runtime data. This delivery uses the deployed source layout. The current wheel packages only the native source and the three strict OCR helpers; WGC/calibration resources are not configured or validated for wheels. The optional shared-resource lookup in Python does not make the wheel a working once-backend distribution. Source ZIPs do not contain compiled executables; run the documented local builds before using the launcher.

Primary API references: [CreateForWindow](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow), [CreateFreeThreaded](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.direct3d11captureframepool.createfreethreaded), [CopySubresourceRegion](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/nf-d3d11-id3d11devicecontext-copysubresourceregion), [Map](https://learn.microsoft.com/en-us/windows/win32/api/d3d11/nf-d3d11-id3d11devicecontext-map), [GetWindowDisplayAffinity](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowdisplayaffinity).
