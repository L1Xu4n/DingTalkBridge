#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <tlhelp32.h>
#include <dwmapi.h>
#include <bcrypt.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Foundation.Collections.h>
#include <winrt/Windows.Data.Json.h>
#include <array>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <cmath>
#include <cwctype>
#include <string>
#include <vector>

namespace bridge_once {
using winrt::Windows::Data::Json::JsonObject;
using winrt::Windows::Data::Json::JsonArray;
using winrt::Windows::Data::Json::JsonValue;
inline void Need(bool condition){if(!condition)throw winrt::hresult_error(E_ACCESSDENIED);}
inline uint64_t Decimal(const winrt::hstring& s){
    std::wstring text(s);Need(!text.empty()&&text.size()<=20);
    for(auto c:text)Need(c>=L'0'&&c<=L'9');
    return std::stoull(text);
}
inline uint64_t UInt(const JsonObject& o,const wchar_t* key,uint64_t maximum=9007199254740991ULL){
    double n=o.GetNamedNumber(key);Need(std::isfinite(n)&&std::floor(n)==n&&n>=0&&n<=(double)maximum);return(uint64_t)n;
}
inline uint64_t Now(){FILETIME t{};GetSystemTimeAsFileTime(&t);return((uint64_t)t.dwHighDateTime<<32)|t.dwLowDateTime;}
inline bool PathEqual(const std::wstring& a,const std::wstring& b){return CompareStringOrdinal(a.c_str(),-1,b.c_str(),-1,TRUE)==CSTR_EQUAL;}
inline bool Hex64(const winrt::hstring& value){if(value.size()!=64)return false;for(auto c:value)if(!((c>=L'0'&&c<=L'9')||(c>=L'a'&&c<=L'f')))return false;return true;}
struct Handle{HANDLE H=nullptr;~Handle(){if(H&&H!=INVALID_HANDLE_VALUE)CloseHandle(H);}Handle()=default;Handle(const Handle&)=delete;Handle&operator=(const Handle&)=delete;};
inline bool LocalPath(const std::wstring& path){
    if(path.size()<3||path.size()>32760||!iswalpha(path[0])||path[1]!=L':'||(path[2]!=L'\\'&&path[2]!=L'/'))return false;
    if(path.find(L':',2)!=std::wstring::npos)return false;
    wchar_t root[]{path[0],L':',L'\\',0};return GetDriveTypeW(root)==DRIVE_FIXED;
}
inline std::wstring FileSha(const std::wstring& path){
    Need(LocalPath(path));
    Handle f;f.H=CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);Need(f.H!=INVALID_HANDLE_VALUE);
    LARGE_INTEGER size{};Need(GetFileSizeEx(f.H,&size)&&size.QuadPart>0&&size.QuadPart<=64*1024*1024);
    BCRYPT_ALG_HANDLE algorithm=nullptr;BCRYPT_HASH_HANDLE hash=nullptr;DWORD objectLength=0,returned=0;
    Need(BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,nullptr,0)>=0);
    NTSTATUS hr=BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,(PUCHAR)&objectLength,sizeof(objectLength),&returned,0);
    std::vector<BYTE> object(objectLength);std::array<BYTE,32> digest{};std::array<BYTE,65536> buffer{};
    bool ok=hr>=0&&BCryptCreateHash(algorithm,&hash,object.data(),objectLength,nullptr,0,0)>=0;
    DWORD bytes=0;
    while(ok){if(!ReadFile(f.H,buffer.data(),(DWORD)buffer.size(),&bytes,nullptr)){ok=false;break;}if(bytes==0)break;ok=BCryptHashData(hash,buffer.data(),bytes,0)>=0;}
    if(ok)ok=BCryptFinishHash(hash,digest.data(),(ULONG)digest.size(),0)>=0;
    if(hash)BCryptDestroyHash(hash);BCryptCloseAlgorithmProvider(algorithm,0);Need(ok);
    static const wchar_t digits[]=L"0123456789abcdef";std::wstring result;
    for(BYTE b:digest){result.push_back(digits[b>>4]);result.push_back(digits[b&15]);}return result;
}
inline JsonObject ReadJsonFile(const std::wstring& path,size_t maximum){
    Need(LocalPath(path));std::ifstream in(std::filesystem::path(path),std::ios::binary);Need((bool)in);
    std::vector<char> bytes(maximum+1);in.read(bytes.data(),(std::streamsize)bytes.size());auto count=in.gcount();
    Need(count>0&&(size_t)count<=maximum);std::string s(bytes.data(),(size_t)count);
    if(s.compare(0,3,"\xEF\xBB\xBF")==0)s.erase(0,3);return JsonObject::Parse(winrt::to_hstring(s));
}
inline DWORD ParentPid(DWORD pid){
    Handle h;h.H=CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS,0);Need(h.H!=INVALID_HANDLE_VALUE);
    PROCESSENTRY32W e{};e.dwSize=sizeof(e);
    BOOL more=Process32FirstW(h.H,&e);for(int n=0;more&&n<10000;n++,more=Process32NextW(h.H,&e))if(e.th32ProcessID==pid)return e.th32ParentProcessID;
    throw winrt::hresult_error(E_ACCESSDENIED);
}
struct Process {
    Handle ProcessHandle;DWORD Pid=0,Session=0;uint64_t Start=0;std::wstring Path;
    void Open(DWORD pid){
        Pid=pid;ProcessHandle.H=OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION|SYNCHRONIZE,FALSE,pid);Need(ProcessHandle.H!=nullptr);
        wchar_t path[32768]{};DWORD length=32768;FILETIME a{},b{},c{},d{};
        Need(QueryFullProcessImageNameW(ProcessHandle.H,0,path,&length)&&GetProcessTimes(ProcessHandle.H,&a,&b,&c,&d)&&ProcessIdToSessionId(pid,&Session));
        Path=path;Start=((uint64_t)a.dwHighDateTime<<32)|a.dwLowDateTime;Need(Alive());
    }
    bool Alive()const{DWORD code=0;return GetExitCodeProcess(ProcessHandle.H,&code)&&code==STILL_ACTIVE;}
};
inline RECT Rect(const JsonArray& a){
    Need(a.Size()==4);LONG v[4]{};
    for(uint32_t i=0;i<4;i++){double n=a.GetNumberAt(i);Need(std::isfinite(n)&&std::floor(n)==n&&n>=-100000&&n<=100000);v[i]=(LONG)n;}
    RECT r{v[0],v[1],v[2],v[3]};Need(r.right>r.left&&r.bottom>r.top&&r.right-r.left<=16000&&r.bottom-r.top<=16000);return r;
}

inline bool Same(const RECT&a,const RECT&b){return a.left==b.left&&a.top==b.top&&a.right==b.right&&a.bottom==b.bottom;}
inline bool Contains(const RECT&a,const RECT&b){return a.left<=b.left&&a.top<=b.top&&a.right>=b.right&&a.bottom>=b.bottom;}
inline std::wstring Class(HWND h){wchar_t s[256]{};Need(GetClassNameW(h,s,256)>0);return s;}
inline DWORD Pid(HWND h){DWORD p=0;GetWindowThreadProcessId(h,&p);return p;}
inline uint64_t Qpc100ns(){LARGE_INTEGER q{},f{};Need(QueryPerformanceCounter(&q)&&QueryPerformanceFrequency(&f));return(uint64_t)((long double)q.QuadPart*10000000.0L/f.QuadPart);}
inline bool EventClear(HANDLE h){return h&&WaitForSingleObject(h,0)==WAIT_TIMEOUT;}
inline bool ParentChainMatches(DWORD a,DWORD b,DWORD python,DWORD ui){return a>0&&b>0&&python>0&&ui>0&&a==python&&b==ui;}
inline bool ConsumeEventSignal(HANDLE h){return h&&WaitForSingleObject(h,0)==WAIT_OBJECT_0;}
struct BoundState {
    std::wstring Stage=L"request";
    ~BoundState(){if(NativePermit)CloseHandle(NativePermit);if(Revoke)CloseHandle(Revoke);}
    JsonObject Request;Process Ui,Python,App;HWND Top{},Semantic{},Cef{};HANDLE Revoke{},NativePermit{};
    RECT Window{},Dwm{},SemanticRect{},CefRect{},Roi{};
    std::wstring TopClass,SemanticClass,CefClass,ExpectedUi,BindingPath,BindingSha;
    std::wstring AppSha,UiSha,PythonSha,HelperSha,Nonce,Attempt,BackendDigest,SceneHash,ProfileSha;
    DWORD InputTick=0;uint64_t Created=0,Expires=0;bool Consumed=false;
    void Load(const JsonObject& r){
        Request=r;Need(r.GetNamedString(L"schema")==L"dingtalkbridge.wgc-once-native-request.v1"&&r.GetNamedString(L"policy")==L"user_confirmed_roi_once");
        Need(r.GetNamedBoolean(L"authorization_granted")&&r.GetNamedBoolean(L"manual_semantic_confirmation")&&
             r.GetNamedBoolean(L"technical_complete")&&r.GetNamedBoolean(L"known_strong_input_geometry_complete"));
        Need(!r.GetNamedBoolean(L"automatic_input_validation_complete"));
        Nonce=r.GetNamedString(L"nonce");BackendDigest=r.GetNamedString(L"backend_digest");SceneHash=r.GetNamedString(L"scene_binding_sha256");ProfileSha=r.GetNamedString(L"profile_sha256");
        Need(Hex64(Nonce.c_str())&&Hex64(BackendDigest.c_str())&&Hex64(SceneHash.c_str())&&Hex64(ProfileSha.c_str()));
        Attempt=r.GetNamedString(L"attempt_id");Need(Attempt.size()>=16&&Attempt.size()<=64);
        Created=Decimal(r.GetNamedString(L"created_filetime"));Expires=Decimal(r.GetNamedString(L"expires_filetime"));
        Need(Expires>Created&&Expires-Created<=150000000&&Created<=Now()+1000000&&Now()<Expires);
        wchar_t own[32768]{};Need(GetModuleFileNameW(nullptr,own,32768)>0);
        auto folder=std::filesystem::path(own).parent_path();
        std::filesystem::path ui;
        if(folder.parent_path().filename()==L"experimental")ui=folder.parent_path()/L"calibration"/L"bin"/L"DingTalkRegionCalibration.exe";
        else ui=folder.parent_path()/L"calibration"/L"bin"/L"DingTalkRegionCalibration.exe";
        ExpectedUi=ui.lexically_normal().wstring();Need(LocalPath(ExpectedUi)&&LocalPath(own));
        auto uiMeta=r.GetNamedObject(L"ui"),pyMeta=r.GetNamedObject(L"python"),target=r.GetNamedObject(L"target");
        Ui.Open((DWORD)UInt(uiMeta,L"pid",0xFFFFFFFFULL));Python.Open((DWORD)UInt(pyMeta,L"pid",0xFFFFFFFFULL));App.Open((DWORD)UInt(target,L"pid",0xFFFFFFFFULL));
        Stage=L"os_parent_chain";Need(ParentChainMatches(ParentPid(GetCurrentProcessId()),ParentPid(Python.Pid),Python.Pid,Ui.Pid));
        Need(Ui.Start==Decimal(uiMeta.GetNamedString(L"start_filetime"))&&Python.Start==Decimal(pyMeta.GetNamedString(L"start_filetime"))&&App.Start==Decimal(target.GetNamedString(L"start_filetime")));
        // Verify the actual OS UI ancestry/fixed path before touching any supplied file path.
        Stage=L"fixed_ui_parent";DWORD earlySession=0;Need(ProcessIdToSessionId(GetCurrentProcessId(),&earlySession)&&Ui.Session==earlySession&&PathEqual(Ui.Path,ExpectedUi));
        Stage=L"local_binding";BindingPath=r.GetNamedString(L"backend_binding_path");BindingSha=r.GetNamedString(L"backend_binding_sha256");Need(LocalPath(BindingPath));
        Need(FileSha(BindingPath)==BindingSha);auto binding=ReadJsonFile(BindingPath,65536);
        Need(binding.GetNamedString(L"schema")==L"dingtalkbridge.roi-once-backend-binding.v1"&&binding.GetNamedString(L"expected_backend_digest")==BackendDigest);
        Need(PathEqual(binding.GetNamedString(L"ui_exe").c_str(),ExpectedUi));
        Stage=L"file_identity";UiSha=binding.GetNamedString(L"ui_sha256");PythonSha=binding.GetNamedString(L"python_sha256");HelperSha=r.GetNamedString(L"helper_sha256");AppSha=target.GetNamedString(L"file_sha256");
        Need(PathEqual(Ui.Path,ExpectedUi)&&PathEqual(Python.Path,binding.GetNamedString(L"python_exe").c_str()));
        Need(FileSha(Ui.Path)==UiSha&&FileSha(Python.Path)==PythonSha&&FileSha(own)==HelperSha);
        Need(PathEqual(App.Path,L"D:\\Program Files\\DingDing\\main\\current_new\\DingTalk.exe")&&FileSha(App.Path)==AppSha);
        DWORD mine=0;Need(ProcessIdToSessionId(GetCurrentProcessId(),&mine)&&Ui.Session==mine&&Python.Session==mine&&App.Session==mine&&App.Session==(DWORD)UInt(target,L"session_id",0xFFFFFFFFULL));
        Top=(HWND)(uintptr_t)UInt(target,L"toplevel_hwnd");Semantic=(HWND)(uintptr_t)UInt(target,L"semantic_hwnd");Cef=(HWND)(uintptr_t)UInt(target,L"message_hwnd");
        Window=Rect(target.GetNamedArray(L"window_rect"));Dwm=Rect(target.GetNamedArray(L"dwm_rect"));SemanticRect=Rect(target.GetNamedArray(L"semantic_rect"));CefRect=Rect(target.GetNamedArray(L"message_rect"));Roi=Rect(r.GetNamedArray(L"roi_screen"));
        TopClass=target.GetNamedString(L"toplevel_class");SemanticClass=target.GetNamedString(L"semantic_class");CefClass=target.GetNamedString(L"message_class");
        Need(Contains(Window,Roi)&&Contains(Dwm,Roi)&&Contains(SemanticRect,Roi)&&Contains(CefRect,Roi));
        Need(CefClass==L"CefBrowserWindow"||CefClass==L"Chrome_WidgetWin_1");
        InputTick=(DWORD)UInt(r,L"last_input_tick",0xFFFFFFFFULL);Need((HWND)(uintptr_t)UInt(r,L"foreground_hwnd")==Top);
        NativePermit=(HANDLE)(uintptr_t)UInt(r,L"native_event_handle");Revoke=(HANDLE)(uintptr_t)UInt(r,L"revocation_event_handle");
        Stage=L"target_epoch";Need(EventClear(Revoke));Need(Check());
        // A real live-UI signal is consumed exactly once, before any framepool.
        Stage=L"kernel_permit";Need(ConsumeEventSignal(NativePermit));CloseHandle(NativePermit);NativePermit=nullptr;Consumed=true;
    }
    bool Check()const{
        if(Now()>=Expires||!Ui.Alive()||!Python.Alive()||!App.Alive()||!EventClear(Revoke))return false;
        LASTINPUTINFO last{sizeof(last),0};if(!GetLastInputInfo(&last)||last.dwTime!=InputTick||GetForegroundWindow()!=Top)return false;
        if(!IsWindowVisible(Top)||!IsWindowVisible(Semantic)||!IsWindowVisible(Cef)||IsIconic(Top)||GetAncestor(Top,GA_ROOT)!=Top||!IsChild(Top,Semantic)||!IsChild(Top,Cef))return false;
        if(Pid(Top)!=App.Pid||Pid(Semantic)!=App.Pid||Pid(Cef)!=App.Pid)return false;
        RECT a{},b{},c{},d{};DWORD affinity=1,cloaked=1;
        if(!GetWindowRect(Top,&a)||!GetWindowRect(Semantic,&b)||!GetWindowRect(Cef,&c)||!Same(a,Window)||!Same(b,SemanticRect)||!Same(c,CefRect))return false;
        if(FAILED(DwmGetWindowAttribute(Top,DWMWA_EXTENDED_FRAME_BOUNDS,&d,sizeof(d)))||!Same(d,Dwm)||
           FAILED(DwmGetWindowAttribute(Top,DWMWA_CLOAKED,&cloaked,sizeof(cloaked)))||cloaked||
           !GetWindowDisplayAffinity(Top,&affinity)||affinity)return false;
        return true;
    }
    void CheckPhase()const{
        Need(Check());Need(Class(Top)==TopClass&&Class(Semantic)==SemanticClass&&Class(Cef)==CefClass);
        Need(FileSha(App.Path)==AppSha&&FileSha(Ui.Path)==UiSha&&FileSha(BindingPath)==BindingSha);
    }
};
inline std::string ReadLineBounded(DWORD timeoutMs,const BoundState* state=nullptr){
    HANDLE input=GetStdHandle(STD_INPUT_HANDLE);std::string value;ULONGLONG until=GetTickCount64()+timeoutMs;
    while(GetTickCount64()<until){
        if(state)Need(state->Check());
        DWORD available=0;Need(PeekNamedPipe(input,nullptr,0,nullptr,&available,nullptr));
        if(available){char c{};DWORD got=0;Need(ReadFile(input,&c,1,&got,nullptr)&&got==1);if(c=='\n')return value;if(c!='\r')value.push_back(c);Need(value.size()<=256*1024);}
        else Sleep(2);
    }
    throw winrt::hresult_error(HRESULT_FROM_WIN32(ERROR_TIMEOUT));
}
inline void Emit(const JsonObject& object){std::cout<<winrt::to_string(object.Stringify())<<std::endl;}
inline void Flag(JsonObject& j,const wchar_t*k,bool v){j.Insert(k,JsonValue::CreateBooleanValue(v));}
inline void Text(JsonObject& j,const wchar_t*k,const std::wstring&v){j.Insert(k,JsonValue::CreateStringValue(v));}
inline void Number(JsonObject&j,const wchar_t*k,double v){j.Insert(k,JsonValue::CreateNumberValue(v));}
} // namespace bridge_once
