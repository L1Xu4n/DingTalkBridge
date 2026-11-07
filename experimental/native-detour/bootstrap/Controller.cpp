#include "Identity.h"
#include <iostream>
#include <chrono>
#include <cstdlib>
#include <algorithm>
#if BRIDGE_SYNTHETIC
#include "FixtureMetadata.h"
#endif

using namespace bridge_native;
namespace {
volatile LONG g_stop=0;
// 接到终止信号后请求正常停止。
BOOL WINAPI Control(DWORD){InterlockedExchange(&g_stop,1);return TRUE;}
struct Resources {
    HANDLE target=nullptr,mapping=nullptr,pipe=INVALID_HANDLE_VALUE,connect_event=nullptr,read_event=nullptr;
    HMODULE dll=nullptr;HHOOK hook=nullptr;SharedState* state=nullptr;
    OVERLAPPED connection{},reading{};bool connecting=false,reading_pending=false;
    // Request stop and release this Controller's hooks, IPC objects and handles.
    ~Resources(){
        if(state)InterlockedExchange(&state->requested_active,0);
        if(hook){UnhookWindowsHookEx(hook);hook=nullptr;}
        if(pipe!=INVALID_HANDLE_VALUE){CancelIoEx(pipe,nullptr);DisconnectNamedPipe(pipe);CloseHandle(pipe);}
        if(connect_event)CloseHandle(connect_event);if(read_event)CloseHandle(read_event);
        if(state)UnmapViewOfFile(state);if(mapping)CloseHandle(mapping);if(target)CloseHandle(target);if(dll)FreeLibrary(dll);
    }
};
// 核验管道连接方确实是本次选定的钉钉进程。
bool MatchesClient(HANDLE pipe,const BootstrapRequest& r) {
    ULONG pid=0;if(!GetNamedPipeClientProcessId(pipe,&pid)||pid!=r.target_pid)return false;
    HANDLE process=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION|SYNCHRONIZE,FALSE,pid);ProcessIdentity identity{};
    bool ok=process&&ReadIdentity(process,pid,identity)&&LockedTarget(identity)&&MatchIdentity(identity,r.target_pid,r.target_creation_time,r.target_session_id,r.target_integrity_rid,r.target_image_path,r.target_sid)&&WaitForSingleObject(process,0)==WAIT_TIMEOUT;
    if(process)CloseHandle(process);return ok;
}
// 输出不含正文的结构化运行计数。
void Summary(const SharedState* s,bool stream,const char* event) {
    (void)stream;std::ostream& out=std::cout;
    out<<"{\"type\":\"bridge_native_status\",\"event\":\""<<event<<"\",\"build_flavor\":"<<kBuildFlavor<<",\"status\":"<<s->status<<",\"error\":"<<s->last_error<<",\"active\":"<<s->Active
       <<",\"worker_alive\":"<<s->worker_alive<<",\"bootstrap_invocations\":"<<s->bootstrap_invocations
       <<",\"callbacks_seen\":"<<s->callbacks_seen<<",\"notifications_400\":"<<s->notifications_400
       <<",\"messages_seen\":"<<s->messages_seen<<",\"messages_sent\":"<<s->messages_sent
       <<",\"dropped\":"<<s->dropped<<",\"controller_lost\":"<<s->controller_lost
       <<",\"pipe_failures\":"<<s->pipe_failures<<",\"identity_failures\":"<<s->identity_failures
       <<",\"decode_errors\":"<<s->decode_errors<<",\"history_running\":"<<s->history_running
       <<",\"history_stage\":"<<s->history_stage<<",\"history_conversations\":"<<s->history_conversations
       <<",\"history_pages\":"<<s->history_pages<<",\"history_messages\":"<<s->history_messages
       <<",\"history_errors\":"<<s->history_errors<<",\"history_truncated\":"<<s->history_truncated<<"}"<<std::endl;
}
// 输出固定错误原因并返回失败码。
int Fail(const char* reason){std::cerr<<"{\"error\":\""<<reason<<"\"}"<<std::endl;return 1;}
// 解析 PID 和窗口句柄等无符号数字参数。
std::uint64_t Number(const wchar_t* x){wchar_t* end=nullptr;auto n=_wcstoui64(x,&end,0);return end&&*end==0?n:0;}
// 建立用户私有 IPC，注册指定线程 hook，监督采集并正常撤销。
int Run(int argc,wchar_t** argv) {
#if BRIDGE_SYNTHETIC
    if(argc==2&&std::wstring(argv[1])==L"--fixture-identity"){
        ProcessIdentity identity{};DWORD stage=0;bool read=ReadIdentity(GetCurrentProcess(),GetCurrentProcessId(),identity,&stage);DWORD error=GetLastError();
        std::cout<<"{\"read_identity\":"<<read<<",\"locked_controller\":"<<LockedController(identity)<<",\"x64\":"<<X64Process(GetCurrentProcess())<<",\"error\":"<<error<<",\"stage\":"<<stage<<",\"path_length\":"<<identity.path.size()<<",\"expected_path_length\":"<<wcslen(kExpectedControllerPath);FixtureMetadata(std::cout);std::cout<<"}"<<std::endl;return read?0:1;
    }
#endif
    DWORD pid=0;HWND hwnd=nullptr;double duration=10;bool stream=false;CaptureMode mode=CaptureMode::CountOnly;
    std::string session="count_only";
    std::uint64_t history_days=30,history_conversations=500,history_messages=1000,history_interval=30;
#if BRIDGE_SYNTHETIC
    bool fixture_bad_start=false;
#endif
    for(int i=1;i<argc;++i){std::wstring a=argv[i];
        if(a==L"--pid"&&i+1<argc)pid=DWORD(Number(argv[++i]));
        else if(a==L"--hwnd"&&i+1<argc)hwnd=reinterpret_cast<HWND>(Number(argv[++i]));
        else if(a==L"--duration"&&i+1<argc){wchar_t* end=nullptr;duration=wcstod(argv[++i],&end);if(!end||*end)return Fail("invalid_duration");}
        else if(a==L"--session"&&i+1<argc){std::wstring value=argv[++i];if(std::any_of(value.begin(),value.end(),[](wchar_t c){return c>127;}))return Fail("invalid_session");session.clear();for(auto c:value)session.push_back(static_cast<char>(c));}
        else if(a==L"--history-days"&&i+1<argc)history_days=Number(argv[++i]);
        else if(a==L"--history-conversations"&&i+1<argc)history_conversations=Number(argv[++i]);
        else if(a==L"--history-messages"&&i+1<argc)history_messages=Number(argv[++i]);
        else if(a==L"--history-interval"&&i+1<argc)history_interval=Number(argv[++i]);
#if BRIDGE_SYNTHETIC
        else if(a==L"--fixture-bad-target-start")fixture_bad_start=true;
#endif
        else if(a==L"--stream")stream=true;
        else if(a==L"--mode"&&i+1<argc){std::wstring value=argv[++i];if(value==L"count")mode=CaptureMode::CountOnly;else if(value==L"capture")mode=CaptureMode::CaptureMessages;else return Fail("invalid_mode");}
        else return Fail("invalid_argument");
    }
    if(!pid||!hwnd||!(duration>=0&&duration<=3600)||session.size()>95||!SafeSessionId(session.c_str(),session.size()+1)||
       ((mode==CaptureMode::CaptureMessages)!=stream))return Fail("invalid_configuration");
    if(!history_days||history_days>36500||!history_conversations||history_conversations>5000||
       !history_messages||history_messages>10000||history_interval<5||history_interval>3600)return Fail("invalid_history_bounds");
    // No stdout body output is permitted to an interactive console or file. Stream
    // is designed for the Python parent owning a private anonymous stdout pipe.
    if(stream&&GetFileType(GetStdHandle(STD_OUTPUT_HANDLE))!=FILE_TYPE_PIPE)return Fail("stream_requires_parent_pipe");
    Resources x;ProcessIdentity self{},target{};
    if(!ReadIdentity(GetCurrentProcess(),GetCurrentProcessId(),self)||!LockedController(self))return Fail("controller_identity");
    x.target=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION|SYNCHRONIZE,FALSE,pid);
    if(!x.target||!ReadIdentity(x.target,pid,target)||!LockedTarget(target)||!SameUserBoundary(self,target))return Fail("target_identity");
    DWORD windowPid=0;DWORD tid=GetWindowThreadProcessId(hwnd,&windowPid);
    if(!WindowThreadBound(hwnd,pid,tid)||windowPid!=pid)return Fail("target_window_thread");
#if !BRIDGE_SYNTHETIC
    if(!IsWindowVisible(hwnd))return Fail("target_window_not_visible");
#endif
    BootstrapRequest r{};r.magic=kProtocolMagic;r.version=kProtocolVersion;r.bytes=sizeof(r);r.build_flavor=kBuildFlavor;r.mode=mode;
    r.history_days=static_cast<std::uint32_t>(history_days);r.history_max_conversations=static_cast<std::uint32_t>(history_conversations);
    r.history_max_messages=static_cast<std::uint32_t>(history_messages);r.history_interval_seconds=static_cast<std::uint32_t>(history_interval);
    r.controller_pid=self.pid;r.target_pid=target.pid;r.target_thread_id=tid;r.controller_session_id=self.session;r.target_session_id=target.session;
    r.controller_integrity_rid=self.integrity;r.target_integrity_rid=target.integrity;r.controller_creation_time=self.creation;r.target_creation_time=target.creation;r.target_hwnd=std::uint64_t(hwnd);
    if(BCryptGenRandom(nullptr,r.nonce,sizeof(r.nonce),BCRYPT_USE_SYSTEM_PREFERRED_RNG)<0)return Fail("random_nonce");
    if(!Copy(r.controller_image_path,self.path)||!Copy(r.target_image_path,target.path)||!Copy(r.controller_sid,self.sid)||!Copy(r.target_sid,target.sid)||
       !Copy(r.mapping_name,MappingName(r.nonce))||!Copy(r.pipe_name,PipeName(self.pid,r.nonce)))return Fail("identity_too_long");
    memcpy(r.collector_session_id,session.c_str(),session.size()+1);
#if BRIDGE_SYNTHETIC
    if(fixture_bad_start)++r.target_creation_time;
#endif
    SECURITY_ATTRIBUTES sa{};PSECURITY_DESCRIPTOR descriptor=nullptr;if(!OwnSecurity(self,sa,descriptor))return Fail("current_user_dacl");
    x.mapping=CreateFileMappingW(INVALID_HANDLE_VALUE,&sa,PAGE_READWRITE,0,sizeof(SharedState),r.mapping_name);
    DWORD mappingError=GetLastError();
    x.pipe=CreateNamedPipeW(r.pipe_name,PIPE_ACCESS_INBOUND|FILE_FLAG_OVERLAPPED|FILE_FLAG_FIRST_PIPE_INSTANCE,PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_WAIT|PIPE_REJECT_REMOTE_CLIENTS,1,65536,65536,0,&sa);
    LocalFree(descriptor);
    if(!x.mapping||mappingError==ERROR_ALREADY_EXISTS||x.pipe==INVALID_HANDLE_VALUE)return Fail("private_ipc_creation");
    x.state=static_cast<SharedState*>(MapViewOfFile(x.mapping,FILE_MAP_READ|FILE_MAP_WRITE,0,0,sizeof(SharedState)));
    if(!x.state)return Fail("mapping_view");
    ZeroMemory(x.state,sizeof(SharedState));x.state->magic=kProtocolMagic;x.state->version=kProtocolVersion;x.state->bytes=sizeof(SharedState);x.state->request=r;
    InterlockedExchange(&x.state->status,LONG(BootstrapStatus::Ready));InterlockedExchange(&x.state->requested_active,1);MemoryBarrier();InterlockedExchange(&x.state->request_ready,1);
    x.connect_event=CreateEventW(nullptr,TRUE,FALSE,nullptr);x.read_event=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    if(!x.connect_event||!x.read_event)return Fail("pipe_event");
    x.connection.hEvent=x.connect_event;
    BOOL connected=ConnectNamedPipe(x.pipe,&x.connection);DWORD connectError=GetLastError();
    if(!connected&&connectError==ERROR_IO_PENDING)x.connecting=true;
    else if(!connected&&connectError==ERROR_PIPE_CONNECTED)SetEvent(x.connect_event);
    else if(!connected)return Fail("pipe_listen");
    else SetEvent(x.connect_event);
    x.dll=LoadLibraryExW(kExpectedDllPath,nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_SYSTEM32);
    if(!x.dll)return Fail("bootstrap_dll_load");
    auto callback=reinterpret_cast<HOOKPROC>(GetProcAddress(x.dll,"CallWndProcHook"));if(!callback)return Fail("bootstrap_export");
    MSG controllerMessage{};PeekMessageW(&controllerMessage,nullptr,0,0,PM_NOREMOVE);
    // Deliberately impossible to request a desktop/global hook: tid is independently
    // read from the verified top HWND and must be nonzero here and in target.
    if(!tid)return Fail("zero_thread_forbidden");
    x.hook=SetWindowsHookExW(WH_CALLWNDPROC,callback,x.dll,tid);if(!x.hook)return Fail("thread_hook_registration");
    UINT message=RegisterWindowMessageW(kBootstrapMessage);std::uint64_t a=0,b=0;memcpy(&a,r.nonce,8);memcpy(&b,r.nonce+8,8);DWORD_PTR response=0;
    if(!message||!SendMessageTimeoutW(hwnd,message,WPARAM(a),LPARAM(b),SMTO_ABORTIFHUNG|SMTO_BLOCK,2000,&response))return Fail("bootstrap_message_timeout");
    SetConsoleCtrlHandler(Control,TRUE);
    auto start=std::chrono::steady_clock::now();auto lastHeartbeat=start;bool active_announced=false,pipe_valid=false;std::string pending;char buffer[16384];std::string control;
    while(!InterlockedCompareExchange(&g_stop,0,0)) {
        while(PeekMessageW(&controllerMessage,nullptr,0,0,PM_REMOVE)){TranslateMessage(&controllerMessage);DispatchMessageW(&controllerMessage);}
        auto elapsed=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
        if(WaitForSingleObject(x.target,0)!=WAIT_TIMEOUT)break;
        LONG status=InterlockedCompareExchange(&x.state->status,0,0);
        if(status==LONG(BootstrapStatus::Rejected)||status==LONG(BootstrapStatus::Failed))break;
        if(!active_announced&&status==LONG(BootstrapStatus::Active)){Summary(x.state,stream,"active");active_announced=true;}
        if(!active_announced&&elapsed>5){InterlockedExchange(&x.state->last_error,LONG(BootstrapError::Timeout));break;}
        if(active_announced&&duration>0&&elapsed>=duration)break;
        auto now=std::chrono::steady_clock::now();if(active_announced&&std::chrono::duration<double>(now-lastHeartbeat).count()>=1){Summary(x.state,stream,"heartbeat");lastHeartbeat=now;}
        HANDLE input=GetStdHandle(STD_INPUT_HANDLE);
        if(input&&input!=INVALID_HANDLE_VALUE&&GetFileType(input)==FILE_TYPE_PIPE){DWORD available=0;
            if(!PeekNamedPipe(input,nullptr,0,nullptr,&available,nullptr))break;
            if(available){char command[64];DWORD read=0;if(!ReadFile(input,command,(std::min)(available,DWORD(sizeof(command))),&read,nullptr))break;
                control.append(command,read);if(control.size()>64||control.find('\n')!=std::string::npos)break;}}
        if(!pipe_valid&&WaitForSingleObject(x.connect_event,0)==WAIT_OBJECT_0){
            DWORD ignored=0;
            if((!x.connecting||GetOverlappedResult(x.pipe,&x.connection,&ignored,FALSE))&&MatchesClient(x.pipe,r))pipe_valid=true;
            else {InterlockedIncrement64(&x.state->pipe_failures);break;}
        }
        if(pipe_valid){
            DWORD count=0;bool got=false;
            if(x.reading_pending){if(WaitForSingleObject(x.read_event,0)==WAIT_OBJECT_0){got=GetOverlappedResult(x.pipe,&x.reading,&count,FALSE)!=FALSE;x.reading_pending=false;if(!got)break;}}
            else {ResetEvent(x.read_event);ZeroMemory(&x.reading,sizeof(x.reading));x.reading.hEvent=x.read_event;
                got=ReadFile(x.pipe,buffer,sizeof(buffer),&count,&x.reading)!=FALSE;
                if(!got){if(GetLastError()==ERROR_IO_PENDING)x.reading_pending=true;else break;}}
            if(got&&count){pending.append(buffer,count);
                size_t e=0;while((e=pending.find('\n'))!=std::string::npos){if(e+1>512*1024){InterlockedIncrement64(&x.state->pipe_failures);InterlockedExchange(&g_stop,1);break;}if(stream){std::cout.write(pending.data(),e+1);std::cout.flush();if(!std::cout){InterlockedExchange(&g_stop,1);break;}}pending.erase(0,e+1);}
                if(pending.size()>512*1024){InterlockedIncrement64(&x.state->pipe_failures);break;}}
        }
        Sleep(20);
    }
    InterlockedExchange(&x.state->requested_active,0);
    if(!active_announced&&x.state->status==LONG(BootstrapStatus::Ready)){InterlockedExchange(&x.state->status,LONG(BootstrapStatus::Failed));InterlockedExchange(&x.state->last_error,LONG(BootstrapError::Timeout));}
    auto stop=std::chrono::steady_clock::now();
    while((x.state->worker_alive||x.state->history_running)&&std::chrono::duration<double>(std::chrono::steady_clock::now()-stop).count()<3)Sleep(20);
    if(x.hook){BOOL removed=UnhookWindowsHookEx(x.hook);x.hook=nullptr;if(!removed)InterlockedExchange(&x.state->last_error,LONG(BootstrapError::Target));}
    Summary(x.state,stream,"stopped");
    return active_announced&&x.state->Active==0&&x.state->worker_alive==0&&x.state->history_running==0&&x.state->last_error==0?0:1;
}
}
// 程序入口，将未处理异常转为固定失败原因。
int wmain(int argc,wchar_t** argv){try{return Run(argc,argv);}catch(...){return Fail("controller_exception");}}
