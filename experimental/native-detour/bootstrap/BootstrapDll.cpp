#include "Identity.h"
#include <atomic>
#include <new>

namespace {
HMODULE g_module=nullptr;
volatile LONG g_started=0;
volatile LONG g_hook_calls=0,g_message_seen=0,g_thread_match=0;
bridge_native::SharedState* g_state=nullptr;
HANDLE g_mapping=nullptr; // Intentionally retained with pinned core until process exit.
struct Work { BYTE nonce[16]; HWND hwnd; DWORD thread; };
// 把初始化失败写入共享状态。
void Fail(bridge_native::SharedState* state,bridge_native::BootstrapError error) {
    if(!state)return;InterlockedExchange(&state->last_error,LONG(error));
    InterlockedExchange(&state->Active,0);InterlockedExchange(&state->worker_alive,0);
    InterlockedExchange(&state->status,LONG(bridge_native::BootstrapStatus::Rejected));
}
// 在钉钉进程内独立核验请求、控制器和目标窗口身份。
bool Validate(const bridge_native::BootstrapRequest& r,const Work& work,HANDLE& controller) {
    using namespace bridge_native;
    if(r.magic!=kProtocolMagic||r.version!=kProtocolVersion||r.bytes!=sizeof(r)||r.build_flavor!=kBuildFlavor||
       (r.mode!=CaptureMode::CountOnly&&r.mode!=CaptureMode::CaptureMessages)||!RequestStringsValid(r)||
       r.target_pid!=GetCurrentProcessId()||r.target_thread_id!=work.thread||r.target_hwnd!=std::uint64_t(work.hwnd)||
       memcmp(r.nonce,work.nonce,16)!=0||MappingName(work.nonce)!=r.mapping_name||PipeName(r.controller_pid,r.nonce)!=r.pipe_name||
       !WindowThreadBound(work.hwnd,r.target_pid,r.target_thread_id))return false;
    ProcessIdentity target{},caller{};
    if(!ReadIdentity(GetCurrentProcess(),r.target_pid,target)||!LockedTarget(target)||
       !MatchIdentity(target,r.target_pid,r.target_creation_time,r.target_session_id,r.target_integrity_rid,r.target_image_path,r.target_sid))return false;
    wchar_t module[32768]{};DWORD n=GetModuleFileNameW(g_module,module,32768);
    if(!n||n>=32768||!SamePath(module,kExpectedDllPath))return false;
    controller=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION|SYNCHRONIZE,FALSE,r.controller_pid);
    if(!controller||!ReadIdentity(controller,r.controller_pid,caller)||!LockedController(caller)||!SameUserBoundary(target,caller)||
       !MatchIdentity(caller,r.controller_pid,r.controller_creation_time,r.controller_session_id,r.controller_integrity_rid,r.controller_image_path,r.controller_sid)||
       WaitForSingleObject(controller,0)!=WAIT_TIMEOUT)return false;
#if !BRIDGE_SYNTHETIC
    HMODULE main=GetModuleHandleW(L"MainFrame.dll");wchar_t mainPath[32768]{};
    if(!main||!GetModuleFileNameW(main,mainPath,32768)||!SamePath(mainPath,kExpectedMainFramePath))return false;
#endif
    return true;
}
// 在加载器锁外初始化采集，监督控制器和停止请求。
DWORD WINAPI Worker(void* parameter) {
    using namespace bridge_native;
    Work work=*static_cast<Work*>(parameter);delete static_cast<Work*>(parameter);
    auto name=MappingName(work.nonce);
    HANDLE mapping=OpenFileMappingW(FILE_MAP_READ|FILE_MAP_WRITE|READ_CONTROL,FALSE,name.c_str());
    if(!mapping){InterlockedExchange(&g_started,0);return 0;}
    auto state=static_cast<SharedState*>(MapViewOfFile(mapping,FILE_MAP_READ|FILE_MAP_WRITE,0,0,sizeof(SharedState)));
    if(!state){CloseHandle(mapping);InterlockedExchange(&g_started,0);return 0;}
    ProcessIdentity self{};bool owner=ReadIdentity(GetCurrentProcess(),GetCurrentProcessId(),self)&&MappingOwnedBy(mapping,self.sid);
    if(!owner||state->magic!=kProtocolMagic||state->version!=kProtocolVersion||state->bytes!=sizeof(SharedState)||InterlockedCompareExchange(&state->request_ready,0,0)!=1){
        UnmapViewOfFile(state);CloseHandle(mapping);InterlockedExchange(&g_started,0);return 0;}
    BootstrapRequest request=state->request;MemoryBarrier();BootstrapRequest second=state->request;
    InterlockedIncrement64(&state->bootstrap_invocations);
    InterlockedExchange(&state->status,LONG(BootstrapStatus::Validating));
    HANDLE controller=nullptr;
    if(memcmp(&request,&second,sizeof(request))||!Validate(request,work,controller)) {
        InterlockedIncrement64(&state->identity_failures);Fail(state,BootstrapError::Identity);
        if(controller)CloseHandle(controller);UnmapViewOfFile(state);CloseHandle(mapping);InterlockedExchange(&g_started,0);return 0;
    }
    // Core/callbacks retain this sanitized mapping address for their process lifetime.
    g_mapping=mapping;g_state=state;
    InterlockedExchange(&state->worker_alive,1);
    InterlockedExchange(&state->status,LONG(BootstrapStatus::Initializing));
    if(InterlockedCompareExchange(&state->requested_active,0,0)!=1||!InitializeReadOnlyDetour(request,state)) {
        auto error=static_cast<BootstrapError>(InterlockedCompareExchange(&state->last_error,0,0));
        DisableReadOnlyCapture(state);Fail(state,error==BootstrapError::None?BootstrapError::Initialize:error);CloseHandle(controller);InterlockedExchange(&g_started,0);return 0;
    }
    InterlockedExchange(&state->Active,1);InterlockedExchange(&state->status,LONG(BootstrapStatus::Active));
    bool lost=false;
    while(InterlockedCompareExchange(&state->requested_active,0,0)==1) {
        DWORD wait=WaitForSingleObject(controller,100);
        if(wait!=WAIT_TIMEOUT){lost=true;break;}
        if(!WindowThreadBound(work.hwnd,request.target_pid,request.target_thread_id)){lost=true;break;}
    }
    DisableReadOnlyCapture(state);InterlockedExchange(&state->requested_active,0);InterlockedExchange(&state->Active,0);
    if(lost)InterlockedIncrement64(&state->controller_lost);
    InterlockedExchange(&state->status,LONG(lost?BootstrapStatus::ControllerLost:BootstrapStatus::Stopped));
    InterlockedExchange(&state->worker_alive,0);CloseHandle(controller);InterlockedExchange(&g_started,0);return 0;
}
}

// 接收指定线程的启动消息，再将原窗口通知继续传递。
extern "C" __declspec(dllexport) LRESULT CALLBACK CallWndProcHook(int code,WPARAM wParam,LPARAM lParam) {
    InterlockedIncrement(&g_hook_calls);
#if BRIDGE_SYNTHETIC
    // Fixture-only retention lets the own Host inspect numeric hook diagnostics
    // even when its first callback is an unrelated system message.
    HMODULE diagnosticPin=nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,reinterpret_cast<LPCWSTR>(&CallWndProcHook),&diagnosticPin);
#endif
    if(code>=0&&lParam) {
        auto cwp=reinterpret_cast<CWPSTRUCT*>(lParam);
        UINT message=RegisterWindowMessageW(bridge_native::kBootstrapMessage);
        DWORD pid=0;DWORD tid=GetWindowThreadProcessId(cwp->hwnd,&pid);
        if(cwp->message==message)InterlockedIncrement(&g_message_seen);
        if(pid==GetCurrentProcessId()&&tid==GetCurrentThreadId())InterlockedIncrement(&g_thread_match);
        if(message&&cwp->message==message&&pid==GetCurrentProcessId()&&tid==GetCurrentThreadId()&&
           bridge_native::WindowThreadBound(cwp->hwnd,pid,tid)&&InterlockedCompareExchange(&g_started,1,0)==0) {
            // Outside DllMain. Pin before scheduling: unhook/controller crash cannot
            // unload executable worker code. No detour or memory work in loader lock.
            HMODULE pinned=nullptr;
            if(GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,reinterpret_cast<LPCWSTR>(&CallWndProcHook),&pinned)) {
                auto work=new(std::nothrow) Work{};
                if(work){std::uint64_t a=cwp->wParam,b=cwp->lParam;memcpy(work->nonce,&a,8);memcpy(work->nonce+8,&b,8);work->hwnd=cwp->hwnd;work->thread=tid;
                    HANDLE thread=CreateThread(nullptr,0,Worker,work,0,nullptr);if(thread)CloseHandle(thread);else{delete work;InterlockedExchange(&g_started,0);}}
                else InterlockedExchange(&g_started,0);
            }else InterlockedExchange(&g_started,0);
        }
    }
    return CallNextHookEx(nullptr,code,wParam,lParam);
}

namespace bridge_native {
// 仅连接进程身份核验通过的本地控制器管道。
HANDLE ConnectVerifiedControllerPipe(const BootstrapRequest& r) noexcept {
    try {
        if(!RequestStringsValid(r)||PipeName(r.controller_pid,r.nonce)!=r.pipe_name)return INVALID_HANDLE_VALUE;
        HANDLE pipe=CreateFileW(r.pipe_name,GENERIC_WRITE,0,nullptr,OPEN_EXISTING,FILE_FLAG_OVERLAPPED,nullptr);
        if(pipe==INVALID_HANDLE_VALUE)return pipe;
        ULONG server=0;HANDLE process=nullptr;ProcessIdentity identity{},self{};
        bool ok=GetNamedPipeServerProcessId(pipe,&server)&&server==r.controller_pid;
        if(ok){process=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION|SYNCHRONIZE,FALSE,server);ok=process&&ReadIdentity(process,server,identity)&&ReadIdentity(GetCurrentProcess(),GetCurrentProcessId(),self)&&
            LockedController(identity)&&SameUserBoundary(identity,self)&&MatchIdentity(identity,r.controller_pid,r.controller_creation_time,r.controller_session_id,r.controller_integrity_rid,r.controller_image_path,r.controller_sid)&&WaitForSingleObject(process,0)==WAIT_TIMEOUT;}
        if(process)CloseHandle(process);if(!ok){CloseHandle(pipe);return INVALID_HANDLE_VALUE;}return pipe;
    } catch(...) {return INVALID_HANDLE_VALUE;}
}
}

#if BRIDGE_SYNTHETIC
// 向自有测试宿主提供计数状态，生产构建不包含此接口。
extern "C" __declspec(dllexport) LONG SyntheticState(LONG field) {
    if(field==3)return g_hook_calls;if(field==4)return g_message_seen;if(field==5)return g_thread_match;
    auto state=g_state;if(!state)return -1;
    return field==0?InterlockedCompareExchange(&state->Active,0,0):field==1?InterlockedCompareExchange(&state->worker_alive,0,0):InterlockedCompareExchange(&state->status,0,0);
}
#endif

// 记录自身模块地址，避免在加载器锁中执行复杂逻辑。
BOOL WINAPI DllMain(HINSTANCE module,DWORD reason,LPVOID) {
    if(reason==DLL_PROCESS_ATTACH){g_module=module;DisableThreadLibraryCalls(module);}return TRUE;
}
