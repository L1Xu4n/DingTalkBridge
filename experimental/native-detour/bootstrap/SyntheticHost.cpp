#include <windows.h>
#include <iostream>
#include <string>
#include <cstdint>
#include "BridgeProtocol.h"
#include "FixtureMetadata.h"
#include "BuildIdentity.h"
namespace {volatile LONG64 g_forward_calls=0;volatile LONG g_bootstrap_messages=0;}
#pragma optimize("",off)
extern "C" __declspec(dllexport) __declspec(noinline) void __fastcall DtbSyntheticDispatch(void* self,std::int32_t code,const void* a,const void* b) {
    // Own-process fixture only. Deliberately nontrivial, noinline prologue for the
    // root core's real MinHook fixture; no application data or remote reads.
    volatile std::uintptr_t marker=reinterpret_cast<std::uintptr_t>(self)^reinterpret_cast<std::uintptr_t>(a)^reinterpret_cast<std::uintptr_t>(b)^std::uintptr_t(code);
    for(int i=0;i<3;++i)marker=(marker<<1)^std::uintptr_t(i);
    InterlockedIncrement64(&g_forward_calls);
}
#pragma optimize("",on)
namespace {
LRESULT CALLBACK WindowProc(HWND hwnd,UINT message,WPARAM w,LPARAM l) {
    if(message==RegisterWindowMessageW(bridge_native::kBootstrapMessage))InterlockedIncrement(&g_bootstrap_messages);
    if(message==WM_CLOSE){DestroyWindow(hwnd);return 0;}if(message==WM_DESTROY){PostQuitMessage(0);return 0;}
    return DefWindowProcW(hwnd,message,w,l);
}
}
int wmain(int argc,wchar_t** argv) {
    if(argc==2&&std::wstring(argv[1])==L"--fixture-loader-metadata") {
        HMODULE dll=LoadLibraryExW(bridge_native::kExpectedDllPath,nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_SYSTEM32);DWORD error=GetLastError();
        auto callback=dll?GetProcAddress(dll,"CallWndProcHook"):nullptr;
        std::cout<<"{\"self_load\":"<<(dll?1:0)<<",\"export_found\":"<<(callback?1:0)<<",\"export_rva\":"<<(callback?std::uintptr_t(callback)-std::uintptr_t(dll):0)<<",\"error\":"<<(dll?0:error)<<"}"<<std::endl;
        if(dll)FreeLibrary(dll);return dll?0:1;
    }
    DWORD lifetime=12000;if(argc==3&&std::wstring(argv[1])==L"--lifetime-ms")lifetime=wcstoul(argv[2],nullptr,10);
    if(lifetime<1000||lifetime>30000)return 1;
    WNDCLASSW wc{};wc.lpfnWndProc=WindowProc;wc.hInstance=GetModuleHandleW(nullptr);wc.lpszClassName=L"DingTalkBridge.OwnSyntheticHost.v1";
    if(!RegisterClassW(&wc))return 2;
    HWND hwnd=CreateWindowExW(0,wc.lpszClassName,L"",WS_OVERLAPPED,0,0,100,100,nullptr,nullptr,wc.hInstance,nullptr);if(!hwnd)return 3;
    // Hidden throughout: no ShowWindow, foreground/input APIs, or user data.
    std::cout<<"{\"pid\":"<<GetCurrentProcessId()<<",\"hwnd\":"<<std::uint64_t(hwnd);FixtureMetadata(std::cout);std::cout<<"}"<<std::endl;
    auto start=GetTickCount64();bool sawActive=false,sawStopped=false;LONG finalActive=-1,finalWorker=-1,finalStatus=-1,hookCalls=-1,messageSeen=-1,threadMatch=-1;std::uint64_t calls=0;
    using Query=LONG(*)(LONG);
    if(!SetTimer(hwnd,1,10,nullptr))return 4;
    while(GetTickCount64()-start<lifetime){MSG m{};if(GetMessageW(&m,nullptr,0,0)<=0)break;TranslateMessage(&m);DispatchMessageW(&m);
        HMODULE module=GetModuleHandleW(L"BridgeBootstrapSynthetic.dll");auto query=module?reinterpret_cast<Query>(GetProcAddress(module,"SyntheticState")):nullptr;
        if(query){finalActive=query(0);finalWorker=query(1);finalStatus=query(2);hookCalls=query(3);messageSeen=query(4);threadMatch=query(5);if(finalActive==1)sawActive=true;if(sawActive&&finalActive==0&&finalWorker==0)sawStopped=true;}
        DtbSyntheticDispatch(nullptr,(calls%2)?400:401,nullptr,nullptr);++calls;
    }
    DestroyWindow(hwnd);
    std::cout<<"{\"saw_active\":"<<(sawActive?"true":"false")<<",\"saw_stopped\":"<<(sawStopped?"true":"false")<<",\"active\":"<<finalActive<<",\"worker_alive\":"<<finalWorker<<",\"status\":"<<finalStatus<<",\"hook_calls\":"<<hookCalls<<",\"message_seen\":"<<messageSeen<<",\"thread_match\":"<<threadMatch<<",\"bootstrap_messages\":"<<g_bootstrap_messages<<",\"dispatch_calls\":"<<calls<<",\"forward_calls\":"<<g_forward_calls<<"}"<<std::endl;return 0;
}
