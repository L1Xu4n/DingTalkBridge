// Layout-compatible read-only notification tap; no whole-client hash gate.
// The module and its sole trampoline remain pinned for this process lifetime.
#define NOMINMAX
#include "bootstrap/BridgeProtocol.h"
#include "BuildIdentity.h"
#include "payload/payload_parser.h"
#include "payload/safe_read.h"
#include "minhook/include/MinHook.h"
#include <bcrypt.h>
#include <atomic>
#include <array>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#include <cstdio>
#include <cstring>
#include <algorithm>

namespace bridge_native {
namespace {
namespace payload = dingtalkbridge::native::payload;
using Dispatch = void (__fastcall *)(void*, std::int32_t, const void*, const void*);
std::string g_client_version = "unknown";
constexpr auto& kTargetPath = kExpectedTargetPath;
constexpr auto& kMainFramePath = kExpectedMainFramePath;
constexpr std::uintptr_t kDispatchRva = 0x36c7d90;
constexpr BYTE kEntryBytes[32] = {0x40,0x55,0x53,0x56,0x57,0x41,0x54,0x41,
    0x55,0x41,0x56,0x41,0x57,0x48,0x8d,0xac,0x24,0xe8,0xfe,0xff,
    0xff,0x48,0x81,0xec,0x18,0x02,0x00,0x00,0x48,0x8b,0x05,0x65};
constexpr std::size_t kMaximumQueueMessages = 64;
constexpr std::size_t kMaximumQueueBytes = 4 * 1024 * 1024;
constexpr std::size_t kMaximumFrame = 512 * 1024;

struct Handle {
    HANDLE value = nullptr;
    // Release an owned Windows handle when its local scope ends.
    ~Handle() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); }
    // Construct an empty RAII handle owner.
    Handle() = default;
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
};
struct QueuedMessage { payload::ParsedMessage message; std::string observed_at; std::size_t bytes = 0; };
struct Session {
    BootstrapRequest request{}; // An immutable copy, never a borrowed request reference.
    SharedState* shared = nullptr; // Bootstrap retains this mapping for process lifetime.
    HANDLE controller = nullptr; // Retained for the same lifetime: no stale handle reuse.
    std::atomic<bool> enabled{false};
    std::mutex queue_mutex;
    std::deque<QueuedMessage> queue;
    std::size_t queue_bytes = 0;
    // Close the owned controller handle if this session is destroyed.
    ~Session() { if (controller) CloseHandle(controller); }
};
std::atomic<Session*> g_current{nullptr};
std::atomic<Dispatch> g_original{nullptr};
std::atomic<std::uintptr_t> g_message_base{0};
std::array<Session*,8> g_sessions{}; // At most eight sessions before an app restart.
std::size_t g_session_count = 0;
SRWLOCK g_initialization_lock = SRWLOCK_INIT;
LONG g_install_state = 0; // 0 untouched, 1 installing, 2 installed, -1 failed/resident.
void* g_target = nullptr;
BYTE g_installed_entry[32]{};
thread_local bool g_in_capture = false;

// 原子读取共享状态，避免读到一半更新的值。
LONG Read(volatile LONG* value) { return InterlockedCompareExchange(value,0,0); }
// 原子增加诊断计数，不记录消息正文。
void Increment(volatile LONG64* value) { InterlockedIncrement64(value); }
// 写入固定错误码和采集状态。
void Error(SharedState* shared, BootstrapError error, BootstrapStatus status = BootstrapStatus::Failed) {
    InterlockedExchange(&shared->last_error, static_cast<LONG>(error));
    InterlockedExchange(&shared->status, static_cast<LONG>(status));
}
// 获取进程创建时间，防止 PID 被复用后误认进程。
std::uint64_t Creation(HANDLE process) {
    FILETIME c{},e{},k{},u{};
    if (!GetProcessTimes(process,&c,&e,&k,&u)) return 0;
    return (static_cast<std::uint64_t>(c.dwHighDateTime)<<32)|c.dwLowDateTime;
}
// 获取当前进程的完整程序路径。
std::wstring OwnImage() {
    std::array<wchar_t,32768> path{};
    DWORD count=GetModuleFileNameW(nullptr,path.data(),static_cast<DWORD>(path.size()));
    if (!count || count>=path.size()) return {};
    return {path.data(),count};
}
// 按 Windows 路径规则比较目标路径。
bool SamePath(const std::wstring& a,const wchar_t* b) {
    return CompareStringOrdinal(a.c_str(),-1,b,-1,TRUE)==CSTR_EQUAL;
}
// Read the actual client file version for metadata, never as a support gate.
std::string ClientVersion() {
    DWORD ignored=0; DWORD size=GetFileVersionInfoSizeW(kTargetPath,&ignored);
    if (!size) return "unknown";
    std::vector<BYTE> bytes(size);
    if (!GetFileVersionInfoW(kTargetPath,0,size,bytes.data())) return "unknown";
    VS_FIXEDFILEINFO* info=nullptr; UINT length=0;
    if (!VerQueryValueW(bytes.data(),L"\\",reinterpret_cast<void**>(&info),&length) || length<sizeof(*info)) return "unknown";
    char version[80]{};
    std::snprintf(version,sizeof(version),"%u.%u.%u.%u",HIWORD(info->dwFileVersionMS),LOWORD(info->dwFileVersionMS),HIWORD(info->dwFileVersionLS),LOWORD(info->dwFileVersionLS));
    return version;
}
// Compare only the dispatch signature; unrelated client bytes may differ.
bool ByteMatch(const void* address,const BYTE* expected) {
    BYTE owned[32]{};
    return payload::detail::safe_read(address,owned,sizeof(owned)) && !std::memcmp(owned,expected,sizeof(owned));
}
// Validate the expected vtable/RTTI relationship without hashing the client file.
bool CompatibleType(std::uintptr_t base,std::uintptr_t vtable,std::uint32_t type,const char* name) {
    std::uintptr_t locator=0; std::uint32_t fields[6]{}; char actual[96]{};
    const auto length=std::strlen(name)+1;
    if(length>sizeof(actual) || !payload::detail::safe_read(reinterpret_cast<void*>(base+vtable-8),&locator,sizeof(locator))) return false;
    if(!payload::detail::safe_read(reinterpret_cast<void*>(locator),fields,sizeof(fields))) return false;
    if(fields[0]!=1 || fields[1]!=0 || fields[3]!=type || locator!=base+fields[5]) return false;
    return payload::detail::safe_read(reinterpret_cast<void*>(base+type+16),actual,length) && std::memcmp(actual,name,length)==0;
}
// 核对目标进程、动态代码策略、入口指令和对象类型布局。
bool Profile(const BootstrapRequest& request,void*& target,std::uintptr_t& message_base) {
    if (request.target_pid!=GetCurrentProcessId() || Creation(GetCurrentProcess())!=request.target_creation_time) return false;
    PROCESS_MITIGATION_DYNAMIC_CODE_POLICY dynamic{};
    if (!GetProcessMitigationPolicy(GetCurrentProcess(),ProcessDynamicCodePolicy,&dynamic,sizeof(dynamic)) || dynamic.ProhibitDynamicCode) return false;
#if defined(DTB_NATIVE_SYNTHETIC) && DTB_NATIVE_SYNTHETIC
    if (request.build_flavor!=2) return false;
    auto image=OwnImage(); auto slash=image.find_last_of(L"\\/");
    if (image.substr(slash==std::wstring::npos?0:slash+1)!=L"DingTalkBridgeNativeSyntheticHost.exe") return false;
    target=reinterpret_cast<void*>(GetProcAddress(GetModuleHandleW(nullptr),"DtbSyntheticDispatch"));
    message_base=0x180000000ULL; // Values used only by this separate owned fixture.
    return target!=nullptr && (g_install_state!=2 || (target==g_target && ByteMatch(target,g_installed_entry)));
#else
    if (request.build_flavor!=1 || !SamePath(OwnImage(),kTargetPath)) return false;
    HMODULE module=GetModuleHandleW(kMainFramePath); if (!module) return false;
    std::array<wchar_t,32768> path{};
    DWORD count=GetModuleFileNameW(module,path.data(),static_cast<DWORD>(path.size()));
    if (!count || count>=path.size() || !SamePath(std::wstring(path.data(),count),kMainFramePath)) return false;
    g_client_version = ClientVersion();
    target=reinterpret_cast<void*>(reinterpret_cast<std::uintptr_t>(module)+kDispatchRva);
    message_base=reinterpret_cast<std::uintptr_t>(module);
    if (!CompatibleType(message_base,0x709e9e0,0x9df1e38,".?AVMessage@object@mj@@") ||
        !CompatibleType(message_base,0x70d0e48,0x9c461c0,".?AVMesssageContentText@object@mj@@")) return false;
    // No fuzzy pattern scanning, hotpatch-above, or changing another hook.
    return g_install_state==2 ? target==g_target && ByteMatch(target,g_installed_entry) : ByteMatch(target,kEntryBytes);
#endif
}
// 确认采集开关和控制器仍有效。
bool Live(Session* session) {
    if (!session || !session->enabled.load(std::memory_order_acquire) || Read(&session->shared->requested_active)!=1) return false;
    if (WaitForSingleObject(session->controller,0)!=WAIT_TIMEOUT) {
        if (session->enabled.exchange(false)) Increment(&session->shared->controller_lost);
        InterlockedExchange(&session->shared->Active,0);
        InterlockedExchange(&session->shared->status,static_cast<LONG>(BootstrapStatus::ControllerLost));
        return false;
    }
    return true;
}
// 生成 UTC 观察时间，不冒充消息发送时间。
std::string Stamp() {
    SYSTEMTIME t{}; GetSystemTime(&t); char output[40]{};
    std::snprintf(output,sizeof(output),"%04u-%02u-%02uT%02u:%02u:%02u.%03uZ",t.wYear,t.wMonth,t.wDay,t.wHour,t.wMinute,t.wSecond,t.wMilliseconds);
    return output;
}
// 以容量限制和非阻塞锁保存消息副本。
void Queue(void* opaque,payload::ParsedMessage&& message) noexcept {
    auto* session=static_cast<Session*>(opaque);
    if (!Live(session)) return;
    try {
        std::unique_lock<std::mutex> lock(session->queue_mutex,std::try_to_lock);
        const std::size_t bytes=message.conversation_id.size()+message.message_id.size()+message.sender_id.size()+message.text.size();
        if (!lock.owns_lock()) { Increment(&session->shared->dropped); return; }
        if (!Live(session) || session->queue.size()>=kMaximumQueueMessages || bytes>kMaximumQueueBytes-session->queue_bytes) {
            Increment(&session->shared->dropped); return;
        }
        session->queue.push_back({std::move(message),Stamp(),bytes});
        session->queue_bytes+=bytes; Increment(&session->shared->messages_enqueued);
    } catch (...) { Increment(&session->shared->dropped); }
}
// 在同步通知生命周期内解析事件，防止重入采集。
void Capture(Session* session,std::int32_t event_type,const void* detail) noexcept {
    if (g_in_capture || !Live(session) || session->request.mode!=CaptureMode::CaptureMessages) return;
    g_in_capture=true;
    const auto result=payload::parse_on_notification(event_type,detail,g_message_base.load(),session,Queue);
    InterlockedAdd64(&session->shared->messages_seen,result.nodes_visited);
    if (result.reason!=payload::DropReason::none && result.reason!=payload::DropReason::event_not_selected) Increment(&session->shared->decode_errors);
    g_in_capture=false;
}
// 截获目标通知并始终转发一次原始调用。
void __fastcall Tap(void* service,std::int32_t event_type,const void* source,const void* detail) {
    Session* session=g_current.load(std::memory_order_acquire);
    if (session && Live(session)) {
        Increment(&session->shared->callbacks_seen);
        if (event_type==400) { Increment(&session->shared->notifications_400); Capture(session,event_type,detail); }
    }
    // No catch around the product: preserve its own result/exception behavior.
    // Published before any patch is enabled and retained until process exit.
    g_original.load(std::memory_order_acquire)(service,event_type,source,detail);
}
// 将字符串按 JSON 规则转义并限制输出大小。
void JsonString(std::string& output,const std::string& input) {
    static constexpr char hex[]="0123456789abcdef"; output.push_back('"');
    for (unsigned char c:input) {
        if (c=='"' || c=='\\') { output.push_back('\\'); output.push_back(static_cast<char>(c)); }
        else if (c<0x20) { output+="\\u00"; output.push_back(hex[c>>4]); output.push_back(hex[c&15]); }
        else output.push_back(static_cast<char>(c));
        if (output.size()>kMaximumFrame) throw 1;
    }
    output.push_back('"');
}
// 把消息副本编码成一行本地管道协议。
std::string Frame(const Session& session,const QueuedMessage& row) {
    std::string result; result.reserve(row.bytes+512);
    result="{\"schema\":\"dingtalkbridge.native-receive.v1\",\"app_version\":";
    JsonString(result,g_client_version);
    result+=",\"module_sha256\":\"\",\"collector_session_id\":";
    JsonString(result,session.request.collector_session_id);
    result+=",\"message_id\":"; JsonString(result,row.message.message_id);
    result+=",\"conversation_id\":"; JsonString(result,row.message.conversation_id);
    result+=",\"sender_id\":"; JsonString(result,row.message.sender_id);
    result+=",\"text\":"; JsonString(result,row.message.text);
    result+=",\"message_timestamp\":null,\"event_observed_at\":"; JsonString(result,row.observed_at); result+="}";
#if defined(DTB_NATIVE_SYNTHETIC) && DTB_NATIVE_SYNTHETIC
    // A fixture can never silently impersonate a production stream.
    result="{\"schema\":\"dingtalkbridge.synthetic-native-envelope.v1\",\"event\":"+result+"}";
#endif
    result+='\n';
    if (result.size()>kMaximumFrame) throw 1;
    return result;
}
// 执行带超时和取消处理的管道写入。
bool WriteFrame(HANDLE pipe,const std::string& frame,Session* session) {
    struct PendingWrite { OVERLAPPED operation{}; Handle event; std::string bytes; };
    auto pending=std::make_unique<PendingWrite>();
    pending->event.value=CreateEventW(nullptr,TRUE,FALSE,nullptr); if (!pending->event.value) return false;
    pending->operation.hEvent=pending->event.value;pending->bytes=frame; DWORD written=0;
    BOOL done=WriteFile(pipe,pending->bytes.data(),static_cast<DWORD>(pending->bytes.size()),&written,&pending->operation);
    if (!done && GetLastError()!=ERROR_IO_PENDING) return false;
    const ULONGLONG deadline=GetTickCount64()+3000;
    while (!done) {
        HANDLE waits[]{pending->event.value,session->controller};
        DWORD result=WaitForMultipleObjects(2,waits,FALSE,100);
        if (result==WAIT_OBJECT_0) { done=GetOverlappedResult(pipe,&pending->operation,&written,FALSE); break; }
        if (result!=WAIT_TIMEOUT || !Live(session) || GetTickCount64()>=deadline) {
            CancelIoEx(pipe,&pending->operation);
            // Do not leave an app worker blocked indefinitely or free pending I/O.
            // Exceptional delayed cancellation retains this bounded owned object;
            // the pipe is closed by Worker, and process exit reclaims it.
            if (WaitForSingleObject(pending->event.value,1000)!=WAIT_OBJECT_0) { (void)pending.release(); return false; }
            GetOverlappedResult(pipe,&pending->operation,&written,FALSE);
            return false;
        }
    }
    if (done) done=GetOverlappedResult(pipe,&pending->operation,&written,FALSE);
    return done && written==frame.size();
}
// 在独立线程发送队列中的消息副本。
DWORD WINAPI Worker(void* opaque) noexcept {
    auto* session=static_cast<Session*>(opaque);
    Handle pipe;
    try {
        if (session->request.mode==CaptureMode::CaptureMessages) {
            pipe.value=ConnectVerifiedControllerPipe(session->request);
            if (!pipe.value || pipe.value==INVALID_HANDLE_VALUE) throw 1;
        }
        while (Live(session)) {
            QueuedMessage row; bool found=false;
            { std::lock_guard<std::mutex> lock(session->queue_mutex);
              if (!session->queue.empty()) { row=std::move(session->queue.front());session->queue.pop_front();session->queue_bytes-=row.bytes;found=true; } }
            if (!found) { Sleep(20); continue; }
            const auto bytes=Frame(*session,row);
            if (!WriteFrame(pipe.value,bytes,session)) throw 1;
            Increment(&session->shared->messages_sent);
        }
    } catch (...) {
        if (Live(session)) { Increment(&session->shared->pipe_failures); Error(session->shared,BootstrapError::Pipe); }
    }
    session->enabled.store(false,std::memory_order_release);
    InterlockedExchange(&session->shared->Active,0);
    if (Read(&session->shared->status)==static_cast<LONG>(BootstrapStatus::Active))
        InterlockedExchange(&session->shared->status,static_cast<LONG>(BootstrapStatus::Stopped));
    try { std::lock_guard<std::mutex> lock(session->queue_mutex);session->queue.clear();session->queue_bytes=0; } catch (...) {}
    return 0;
}
// 检查采集会话标识只包含允许字符。
bool SessionId(const BootstrapRequest& r) {
    const char* end=static_cast<const char*>(std::memchr(r.collector_session_id,0,sizeof(r.collector_session_id)));
    if (!end || end==r.collector_session_id) return false;
    return std::all_of(r.collector_session_id,end,[](unsigned char c){return c=='_'||c=='-'||(c>='A'&&c<='Z')||(c>='a'&&c<='z')||(c>='0'&&c<='9');});
}
} // namespace

// 验证布局后安装 hook；原调用跳板保持进程期有效。
bool InitializeReadOnlyDetour(const BootstrapRequest& claims,SharedState* shared) noexcept {
    if (!shared || !TryAcquireSRWLockExclusive(&g_initialization_lock)) return false;
    struct Unlock { ~Unlock(){ReleaseSRWLockExclusive(&g_initialization_lock);} } unlock;
    try {
        const BootstrapRequest request=claims;
        if (request.magic!=kProtocolMagic || request.version!=kProtocolVersion || request.bytes!=sizeof(request) ||
            !SessionId(request) || (request.mode!=CaptureMode::CountOnly && request.mode!=CaptureMode::CaptureMessages)) {
            Error(shared,BootstrapError::Protocol); return false;
        }
        if (g_install_state<0 || g_session_count>=g_sessions.size()) { Error(shared,BootstrapError::Initialize); return false; }
        auto* old=g_current.load(std::memory_order_acquire);
        if (Live(old)) { Error(shared,BootstrapError::Duplicate,BootstrapStatus::Rejected); return false; }
        void* target=nullptr; std::uintptr_t message_base=0;
        if (!Profile(request,target,message_base)) { Increment(&shared->identity_failures);Error(shared,BootstrapError::ModuleHash);return false; }
        auto session=std::make_unique<Session>(); session->request=request;session->shared=shared;
        session->controller=OpenProcess(SYNCHRONIZE|PROCESS_QUERY_LIMITED_INFORMATION,FALSE,request.controller_pid);
        if (!session->controller || Creation(session->controller)!=request.controller_creation_time || WaitForSingleObject(session->controller,0)!=WAIT_TIMEOUT) {
            Error(shared,BootstrapError::Identity); return false;
        }
        if (!g_install_state) {
            g_install_state=1;
            HMODULE pinned=nullptr,pinned_target=nullptr;
            if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
                reinterpret_cast<LPCWSTR>(&InitializeReadOnlyDetour),&pinned)) { g_install_state=-1;Error(shared,BootstrapError::Initialize);return false; }
            if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
                reinterpret_cast<LPCWSTR>(target),&pinned_target)) { g_install_state=-1;Error(shared,BootstrapError::Initialize);return false; }
            MH_STATUS status=MH_Initialize();
            void* trampoline=nullptr;
            if (status==MH_OK) status=MH_CreateHook(target,reinterpret_cast<void*>(&Tap),&trampoline);
            if (status==MH_OK && trampoline) {
                g_original.store(reinterpret_cast<Dispatch>(trampoline),std::memory_order_release);
                g_target=target;g_message_base.store(message_base);
                status=MH_EnableHook(target);
            }
            if (status!=MH_OK || !trampoline) {
                g_install_state=-1;
                InterlockedExchange(&shared->last_error,0x1000+static_cast<LONG>(status));
                InterlockedExchange(&shared->status,static_cast<LONG>(BootstrapStatus::Failed));
                // A failed installer may already have exposed a trampoline or patch.
                // Never free/unpatch/unload and never automatically retry it.
                return false;
            }
            if (!payload::detail::safe_read(target,g_installed_entry,sizeof(g_installed_entry))) { g_install_state=-1;Error(shared,BootstrapError::Initialize);return false; }
            g_install_state=2;
        }
        session->enabled.store(true,std::memory_order_release);
        Session* published=session.release();
        g_sessions[g_session_count++]=published;g_current.store(published,std::memory_order_release);
        InterlockedExchange(&shared->Active,1);InterlockedExchange(&shared->status,static_cast<LONG>(BootstrapStatus::Active));
        HANDLE worker=CreateThread(nullptr,0,Worker,published,0,nullptr);
        if (!worker) { published->enabled.store(false);InterlockedExchange(&shared->Active,0);Error(shared,BootstrapError::Initialize);return false; }
        CloseHandle(worker);
        return true;
    } catch (...) { if(g_install_state==1) g_install_state=-1;Error(shared,BootstrapError::Initialize);return false; }
}

// 关闭消息采集，保留原调用转发和驻留代码。
void DisableReadOnlyCapture(SharedState* owner) noexcept {
    Session* session=g_current.load(std::memory_order_acquire);
    if (!session || session->shared!=owner) return;
    session->enabled.store(false,std::memory_order_release);
    InterlockedExchange(&owner->Active,0);
    if (Read(&owner->status)==static_cast<LONG>(BootstrapStatus::Active)) InterlockedExchange(&owner->status,static_cast<LONG>(BootstrapStatus::Stopped));
}
} // namespace bridge_native
