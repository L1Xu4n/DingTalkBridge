# Layout-profile message payload decoder

Include `payload_parser.h`, compile `payload_parser.cpp` and `safe_read.cpp` as x64 with MSVC `/EHsc /MD`; the tested toolchain is MSVC 14.44.35207, release string ABI. The sink receives owned strings and must be noexcept. Nothing in this module installs hooks, opens processes, loads product DLLs, logs message text, or forwards the original dispatch.

`parse_on_notification()` returns before inspecting any other argument for a non-400 event. For 400, the caller must already have verified the loaded MainFrame module identity, dispatch signature and compatible RTTI/layout. Client version numbers and whole-file hashes are not support gates. Incompatible vtables are rejected; this implementation does not search memory for substitute addresses.

The result has no inferred sender direction or message timestamp. Set the DTO's message timestamp to null and record the caller's own observation time separately until the native time unit is established. A 400 event is a new-message notification and can include self-sent messages.

Run `build-fixture.ps1` to compile and execute tests against owned synthetic allocations, including a private NOACCESS page. These tests establish decoder behavior on the recovered contract, not successful connection to DingTalk.

The outer detour must call the original function exactly once for every notification, regardless of parse result, and maintain safe hook/trampoline lifetime. The active profile and entry signature are defined in `../RootDetourCore.cpp`; field offsets and bounds are in `payload_parser.cpp` and `payload_parser.h`.
