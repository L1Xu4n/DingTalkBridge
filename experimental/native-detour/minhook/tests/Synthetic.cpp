// Self-contained current-process synthetic functions only. No injection/UI/data.
#include <windows.h>
#include <atomic>
#include <cstdio>
#include <cstring>
#include "../include/MinHook.h"
#ifdef MH_STRICT_TEST
#include "faults.h"
#endif
using Fn=int(*)(volatile LONG*);
static Fn original;
static std::atomic<bool> stop{false},capture{true};
static std::atomic<unsigned long> calls{0},bad{0};
static std::atomic<unsigned> started{0};
static Fn target;
static volatile LONG argument=7;
static int Detour(volatile LONG *p) { return original(p)+(capture.load(std::memory_order_acquire)?1000:0); }
static DWORD WINAPI Worker(void*) {
    started.fetch_add(1);
    while(!stop.load(std::memory_order_acquire)) {
        const int value=target(&argument);
        if(value!=8 && value!=1008 && value!=1 && value!=1001) bad.fetch_add(1);
        calls.fetch_add(1);
    }
    return 0;
}
static bool Check(bool ok,const char *what) {
    if(!ok) std::fprintf(stderr,"assertion: %s\n",what);
    return ok;
}
int main(int argc,char**argv) {
    if(argc!=2) return 2;
    const char *name=argv[1];
    bool changedContext=std::strcmp(name,"changed-context")==0;
    bool setCase=std::strcmp(name,"set-context")==0 || changedContext;
    bool createFlush=std::strcmp(name,"create-flush")==0;
    bool hotpatch=std::strcmp(name,"hotpatch-above")==0;
    bool guarded=std::strcmp(name,"guard-page")==0;
    bool crossPage=std::strcmp(name,"cross-page")==0;
    BYTE *page=(BYTE*)VirtualAlloc(nullptr,8192,MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE);
    if(!page) return 3;
    std::memset(page,0x90,8192);
    BYTE *entry=page+(crossPage?4094:16);
    const BYTE normal[]={0x8b,0x01,0x83,0xc0,0x01,0xc3}; // return *arg+1
    const BYTE loop[]={0x8b,0x01,0x85,0xc0,0x74,0xfa,0xc3}; // while(!*arg){}; return *arg
    if(hotpatch) { entry[0]=0xc3; std::memset(entry+1,0xcc,31); entry[1]=0x90; entry[2]=0x55; }
    else std::memcpy(entry,setCase?loop:normal,setCase?sizeof(loop):sizeof(normal));
    DWORD old;
    if(!VirtualProtect(page,8192,PAGE_EXECUTE_READ|(guarded?PAGE_GUARD:0),&old) || !FlushInstructionCache(GetCurrentProcess(),page,8192)) return 3;
    target=reinterpret_cast<Fn>(entry);
    bool ok=Check(MH_Initialize()==MH_OK,"initialize");
#ifdef MH_STRICT_TEST
    if(createFlush) MH_TestSetFault(MH_TEST_FLUSH,1);
#endif
    LPVOID trampoline=nullptr;
    const MH_STATUS created=MH_CreateHook(entry,reinterpret_cast<LPVOID>(Detour),&trampoline);
    if(createFlush || hotpatch || guarded || crossPage) {
        const MH_STATUS expected=createFlush ? MH_ERROR_CACHE_FLUSH : hotpatch ? MH_ERROR_UNSUPPORTED_FUNCTION : MH_ERROR_TARGET_CHANGED;
        ok=Check(created==expected && trampoline==nullptr,"private create failure") && ok;
        if(guarded) {
            MEMORY_BASIC_INFORMATION region={};
            ok=Check(VirtualQuery(page,&region,sizeof(region))==sizeof(region) && (region.Protect&PAGE_GUARD),"guard protection not consumed") && ok;
        }
        ok=Check(MH_Uninitialize()==MH_OK,"unpublished storage can release") && ok;
        std::printf("{\"case\":\"%s\",\"passed\":%s,\"patch_written\":false}\n",name,ok?"true":"false");
        VirtualFree(page,0,MEM_RELEASE); return ok?0:1;
    }
    ok=Check(created==MH_OK && trampoline,"create") && ok;
    if(!ok) return 1;
    original=reinterpret_cast<Fn>(trampoline);
    ok=Check(MH_CreateHook(entry,reinterpret_cast<LPVOID>(Detour),nullptr)==MH_ERROR_PROCESS_LIFETIME,"second create refused") && ok;
    ok=Check(MH_EnableHook(MH_ALL_HOOKS)==MH_ERROR_PROCESS_LIFETIME,"all hooks refused") && ok;
    ok=Check(MH_QueueEnableHook(entry)==MH_ERROR_PROCESS_LIFETIME && MH_ApplyQueued()==MH_ERROR_PROCESS_LIFETIME,"queued mutation refused") && ok;
    if(setCase) InterlockedExchange(&argument,0);
    HANDLE workers[3]={};
    for(auto &h:workers) { h=CreateThread(nullptr,0,Worker,nullptr,0,nullptr); if(!h) return 3; }
    const ULONGLONG start=GetTickCount64();
    while(started.load()!=3 && GetTickCount64()-start<3000) Sleep(1);
    if(started.load()!=3) return 4;
    Sleep(25);
    MH_STATUS expected=MH_OK;
    bool expectPatch=true;
#ifdef MH_STRICT_TEST
    bool persistent=false;
    UINT op=MH_TEST_NONE,nth=1;
    if(!std::strcmp(name,"open")) { op=MH_TEST_OPEN; nth=2; expected=MH_ERROR_THREAD_OPEN; expectPatch=false; }
    else if(!std::strcmp(name,"suspend")) { op=MH_TEST_SUSPEND; nth=2; expected=MH_ERROR_THREAD_SUSPEND; expectPatch=false; }
    else if(!std::strcmp(name,"get-context")) { op=MH_TEST_GET_CONTEXT; nth=2; expected=MH_ERROR_THREAD_CONTEXT; expectPatch=false; }
    else if(setCase && !changedContext) { op=MH_TEST_SET_CONTEXT; nth=2; expected=MH_ERROR_THREAD_CONTEXT; expectPatch=false; }
    else if(!std::strcmp(name,"resume")) { op=MH_TEST_RESUME; expected=MH_ERROR_THREAD_RESUME; }
    else if(!std::strcmp(name,"resume-persistent")) { op=MH_TEST_RESUME; nth=0; expected=MH_ERROR_THREAD_RESUME; persistent=true; }
    else if(!std::strcmp(name,"protect")) { op=MH_TEST_PROTECT; expected=MH_ERROR_MEMORY_PROTECT; expectPatch=false; }
    else if(!std::strcmp(name,"restore")) { op=MH_TEST_RESTORE; expected=MH_ERROR_PROTECTION_RESTORE; }
    else if(!std::strcmp(name,"flush")) { op=MH_TEST_FLUSH; expected=MH_ERROR_CACHE_FLUSH; }
    else if(!std::strcmp(name,"snapshot-first")) { op=MH_TEST_SNAPSHOT; expected=MH_ERROR_THREAD_ENUM; expectPatch=false; }
    else if(!std::strcmp(name,"snapshot-second")) { op=MH_TEST_SNAPSHOT; nth=2; expected=MH_ERROR_THREAD_ENUM; expectPatch=false; }
    MH_TestSetFault(op,nth);
#endif
    if(!std::strcmp(name,"target-changed") || changedContext) {
        VirtualProtect(page,4096,PAGE_EXECUTE_READWRITE,&old);
        entry[31]=0xcc;
        VirtualProtect(page,4096,old,&old);
        expected=MH_ERROR_TARGET_CHANGED; expectPatch=false;
    }
    const MH_STATUS enabled=MH_EnableHook(entry);
    MH_STRICT_DIAGNOSTICS d={};
    ok=Check(MH_GetStrictDiagnostics(&d,sizeof(d))==MH_OK,"diagnostics") && ok;
    ok=Check(enabled==expected,"precise failure status") && ok;
    ok=Check(bool(d.patch_written)==expectPatch,"patch state truthful") && ok;
    if(changedContext) ok=Check(d.contexts_moved==0,"entry conflict rejected before moving RIP") && ok;
    if(!expectPatch) ok=Check(std::memcmp(entry,setCase?loop:normal,5)==0,"entry unchanged") && ok;
#ifdef MH_STRICT_TEST
    if(setCase && !changedContext) ok=Check(MH_TestCalls(MH_TEST_SET_CONTEXT)>=2 && d.contexts_moved>=1,"partial real context relocation exercised") && ok;
    if(persistent) {
        ok=Check(d.pending_resume>=3,"retained failed handles") && ok;
        MH_TestSetFault(MH_TEST_NONE,1);
        ok=Check(MH_RecoverSuspendedThreads()==MH_OK,"same-handle resume recovery") && ok;
        MH_GetStrictDiagnostics(&d,sizeof(d));
    }
#endif
    ok=Check(d.pending_resume==0 && d.threads_suspended==d.threads_resumed,"all own increments released") && ok;
    ok=Check(MH_DisableHook(entry)==MH_ERROR_PROCESS_LIFETIME && MH_RemoveHook(entry)==MH_ERROR_PROCESS_LIFETIME && MH_Uninitialize()==MH_ERROR_PROCESS_LIFETIME,"published lifetime retained") && ok;
    ok=Check(MH_EnableHook(entry)==MH_ERROR_PROCESS_LIFETIME,"no second attempt") && ok;
    capture.store(false,std::memory_order_release);
    if(setCase) InterlockedExchange(&argument,1);
    Sleep(20);
    stop.store(true,std::memory_order_release);
    if(WaitForMultipleObjects(3,workers,TRUE,3000)!=WAIT_OBJECT_0) return 4;
    for(auto h:workers) CloseHandle(h);
    ok=Check(calls.load()>0 && bad.load()==0,"concurrent original semantics retained") && ok;
    const int expectedOriginal=setCase?1:8;
    ok=Check(original(&argument)==expectedOriginal && target(&argument)==expectedOriginal,"logical stop forwards") && ok;
    std::printf("{\"case\":\"%s\",\"passed\":%s,\"status\":\"%s\",\"patch_written\":%s,\"threads_suspended\":%u,\"threads_resumed\":%u,\"contexts_moved\":%u,\"pending_resume\":%u,\"calls\":%lu,\"bad_results\":%lu}\n",
        name,ok?"true":"false",MH_StatusToString(enabled),d.patch_written?"true":"false",d.threads_suspended,d.threads_resumed,d.contexts_moved,d.pending_resume,calls.load(),bad.load());
    // Published target/trampoline intentionally remain valid until process exit.
    return ok?0:1;
}
