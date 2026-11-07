#pragma once
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <cstdint>
#include <type_traits>

namespace bridge_native {
constexpr std::uint32_t kProtocolMagic = 0x424E4454; // TDNB
constexpr std::uint32_t kProtocolVersion = 2;
constexpr wchar_t kBootstrapMessage[] = L"DingTalkBridge.NativeBootstrap.v1";
enum class CaptureMode : std::uint32_t { CountOnly = 1, CaptureMessages = 2 };
enum class BootstrapStatus : LONG { Empty = 0, Ready = 1, Validating = 2, Initializing = 3,
    Active = 4, Stopped = 5, ControllerLost = 6, Rejected = 7, Failed = 8 };
enum class BootstrapError : LONG { None = 0, Protocol = 1, Mapping = 2, Identity = 3,
    Target = 4, ModuleHash = 5, Initialize = 6, Pipe = 7, Timeout = 8, Duplicate = 9 };

// Claims, not authorization. Bootstrap target independently verifies each identity
// and the compile-time production/synthetic locks before calling DetourCore.
struct BootstrapRequest {
    std::uint32_t magic;
    std::uint32_t version;
    std::uint32_t bytes;
    std::uint32_t build_flavor; // 1 production; 2 separately compiled synthetic fixture
    CaptureMode mode;
    DWORD controller_pid;
    DWORD target_pid;
    DWORD target_thread_id;
    DWORD controller_session_id;
    DWORD target_session_id;
    DWORD controller_integrity_rid;
    DWORD target_integrity_rid;
    std::uint64_t controller_creation_time;
    std::uint64_t target_creation_time;
    std::uint64_t target_hwnd;
    BYTE nonce[16];
    std::uint32_t history_days;
    std::uint32_t history_max_conversations;
    std::uint32_t history_max_messages;
    std::uint32_t history_interval_seconds;
    BYTE reserved_profile_bytes[16]; // No client hash verification.
    char collector_session_id[96]; // ASCII [A-Za-z0-9_-], NUL terminated
    wchar_t controller_image_path[512];
    wchar_t target_image_path[512];
    wchar_t controller_sid[192];
    wchar_t target_sid[192];
    wchar_t mapping_name[128];
    wchar_t pipe_name[160];
};

// Local named mapping contains no pointers, message bytes, account names or tokens.
// Interlocked* access is required for all mutable fields. Keep mapped while pinned
// callbacks may access it; stop disables capture, it does not unpatch the trampoline.
struct alignas(8) SharedState {
    std::uint32_t magic;
    std::uint32_t version;
    std::uint32_t bytes;
    std::uint32_t reserved;
    volatile LONG request_ready;
    volatile LONG requested_active;
    volatile LONG Active;
    volatile LONG status;
    volatile LONG last_error;
    volatile LONG worker_alive;
    volatile LONG bootstrap_hook_stage; // numeric diagnostic only, no pointer/data
    volatile LONG64 bootstrap_invocations;
    volatile LONG64 callbacks_seen;
    volatile LONG64 notifications_400;
    volatile LONG64 messages_seen;
    volatile LONG64 messages_enqueued;
    volatile LONG64 messages_sent;
    volatile LONG64 dropped;
    volatile LONG64 decode_errors;
    volatile LONG64 policy_rejections;
    volatile LONG64 controller_lost;
    volatile LONG64 pipe_failures;
    volatile LONG64 identity_failures;
    volatile LONG history_running;
    volatile LONG history_stage; // 1 incompatible, 2 waiting, 3 listing, 4 paging, 5 idle, 6 error, 7 account changed, 8 stopped.
    volatile LONG64 history_conversations;
    volatile LONG64 history_pages;
    volatile LONG64 history_messages;
    volatile LONG64 history_errors;
    volatile LONG64 history_truncated;
    BootstrapRequest request;
};
static_assert(std::is_standard_layout<BootstrapRequest>::value, "wire request layout");
static_assert(std::is_standard_layout<SharedState>::value, "wire state layout");
static_assert(sizeof(void*) == 8, "x64 only");

// Implemented by root DetourCore; Bootstrap calls only after target-side validation.
bool InitializeReadOnlyDetour(const BootstrapRequest&, SharedState*) noexcept;
void DisableReadOnlyCapture(SharedState* owner) noexcept;

// Optional body transport. Opens ONLY the deterministic current-user private pipe
// after server PID/start/SID/session/integrity verification. Caller owns the handle.
HANDLE ConnectVerifiedControllerPipe(const BootstrapRequest&) noexcept;
}
