#pragma once
#include "BridgeProtocol.h"
#include "BuildIdentity.h"
#include <string>
#include <vector>
#include <cstring>
#include <sddl.h>
#include <aclapi.h>
#include <bcrypt.h>

namespace bridge_native {
struct ProcessIdentity {
    DWORD pid=0, session=0, integrity=0;
    std::uint64_t creation=0;
    std::wstring path, sid;
};
// 将 FILETIME 转换为可比较的 64 位数值。
inline std::uint64_t TimeValue(FILETIME x) { return (std::uint64_t(x.dwHighDateTime)<<32)|x.dwLowDateTime; }
// 比较两个 Windows 文件路径。
inline bool SamePath(const std::wstring& a,const std::wstring& b) { return !a.empty()&&!b.empty()&&_wcsicmp(a.c_str(),b.c_str())==0; }
// 检查进程是否为原生 x64。
inline bool X64Process(HANDLE process) {
    using F=BOOL(WINAPI*)(HANDLE,USHORT*,USHORT*);
    auto fn=reinterpret_cast<F>(GetProcAddress(GetModuleHandleW(L"kernel32.dll"),"IsWow64Process2"));
    if(fn) { USHORT machine=0,native=0;return fn(process,&machine,&native)&&machine==IMAGE_FILE_MACHINE_UNKNOWN&&native==IMAGE_FILE_MACHINE_AMD64; }
    BOOL wow=TRUE;SYSTEM_INFO si{};GetNativeSystemInfo(&si);
    return IsWow64Process(process,&wow)&&!wow&&si.wProcessorArchitecture==PROCESSOR_ARCHITECTURE_AMD64;
}
// 读取 Windows 进程路径、SID、会话和完整性级别；不读取钉钉登录凭据。
inline bool ReadIdentity(HANDLE process,DWORD pid,ProcessIdentity& out,DWORD* failure=nullptr) {
    if(failure)*failure=1;
    if(!process||!X64Process(process))return false;
    FILETIME c{},e{},k{},u{};DWORD n=32768;std::vector<wchar_t> path(n);
    if(!GetProcessTimes(process,&c,&e,&k,&u)||!QueryFullProcessImageNameW(process,0,path.data(),&n)||!ProcessIdToSessionId(pid,&out.session))return false;
    if(failure)*failure=2;HANDLE token=nullptr;if(!OpenProcessToken(process,TOKEN_QUERY,&token))return false;
    DWORD bytes=0;GetTokenInformation(token,TokenUser,nullptr,0,&bytes);std::vector<BYTE> user(bytes);
    if(failure)*failure=3;bool ok=bytes&&GetTokenInformation(token,TokenUser,user.data(),bytes,&bytes);
    LPWSTR sid=nullptr;
    if(ok){if(failure)*failure=4;ok=ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER*>(user.data())->User.Sid,&sid)!=FALSE;}
    if(ok)out.sid=sid;if(sid)LocalFree(sid);
    bytes=0;GetTokenInformation(token,TokenIntegrityLevel,nullptr,0,&bytes);std::vector<BYTE> level(bytes);
    if(ok){if(failure)*failure=5;ok=bytes&&GetTokenInformation(token,TokenIntegrityLevel,level.data(),bytes,&bytes);}
    if(ok) { PSID s=reinterpret_cast<TOKEN_MANDATORY_LABEL*>(level.data())->Label.Sid;BYTE count=*GetSidSubAuthorityCount(s);ok=count!=0;if(ok)out.integrity=*GetSidSubAuthority(s,count-1); }
    CloseHandle(token);if(!ok)return false;
    out.pid=pid;out.creation=TimeValue(c);out.path.assign(path.data(),n);if(failure)*failure=0;return true;
}
// 核对进程身份是否与本次请求一致。
inline bool MatchIdentity(const ProcessIdentity& x,DWORD pid,std::uint64_t creation,DWORD session,DWORD integrity,const wchar_t* path,const wchar_t* sid) {
    return x.pid==pid&&x.creation==creation&&x.session==session&&x.integrity==integrity&&SamePath(x.path,path)&&x.sid==sid;
}
// 要求双方位于相同 Windows 用户、会话及完整性级别。
inline bool SameUserBoundary(const ProcessIdentity& a,const ProcessIdentity& b) {
    return a.sid==b.sid&&a.session==b.session&&a.integrity==b.integrity;
}
// 将随机值转换为 IPC 名称的十六进制后缀。
inline std::wstring HexNonce(const BYTE* nonce) {
    static constexpr wchar_t hex[]=L"0123456789abcdef";std::wstring s;
    for(int i=0;i<16;++i){s+=hex[nonce[i]>>4];s+=hex[nonce[i]&15];}return s;
}
// 生成本次采集专属的共享内存名称。
inline std::wstring MappingName(const BYTE* nonce) { return L"Local\\DingTalkBridge.NativeBootstrap.v1."+HexNonce(nonce); }
// 生成本次采集专属的本地管道名称。
inline std::wstring PipeName(DWORD pid,const BYTE* nonce) { return L"\\\\.\\pipe\\DingTalkBridge.Native."+std::to_wstring(pid)+L"."+HexNonce(nonce); }
// 校验会话标识及其字符串终止符。
inline bool SafeSessionId(const char* value,size_t max) {
    if(!value||!max||!value[0])return false;
    for(size_t i=0;i<max;++i){char c=value[i];if(c==0)return true;if(!((c>='a'&&c<='z')||(c>='A'&&c<='Z')||(c>='0'&&c<='9')||c=='_'||c=='-'))return false;}return false;
}
// 把字符串安全复制到固定长度协议字段。
template<size_t N>inline bool Copy(wchar_t(&to)[N],const std::wstring& from) { if(from.size()>=N)return false;return wcscpy_s(to,from.c_str())==0; }
// 确认固定长度字符串存在终止符。
template<size_t N>inline bool Terminated(const wchar_t(&x)[N]) {return wmemchr(x,L'\0',N)!=nullptr;}
// 逐项验证请求中的路径和会话字符串边界。
inline bool RequestStringsValid(const BootstrapRequest& r) {
    return Terminated(r.controller_image_path)&&Terminated(r.target_image_path)&&Terminated(r.controller_sid)&&Terminated(r.target_sid)&&Terminated(r.mapping_name)&&Terminated(r.pipe_name)&&SafeSessionId(r.collector_session_id,sizeof(r.collector_session_id));
}
// 为 IPC 建立只属于当前 Windows 用户的访问控制。
inline bool OwnSecurity(const ProcessIdentity& owner,SECURITY_ATTRIBUTES& sa,PSECURITY_DESCRIPTOR& descriptor) {
    std::wstring sddl=L"O:"+owner.sid+L"D:P(A;;GA;;;"+owner.sid+L")";
    if(!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(),SDDL_REVISION_1,&descriptor,nullptr))return false;
    sa={sizeof(sa),descriptor,FALSE};return true;
}
// 核验共享内存对象的所有者 SID。
inline bool MappingOwnedBy(HANDLE object,const std::wstring& sid) {
    PSID owner=nullptr;PSECURITY_DESCRIPTOR descriptor=nullptr;
    DWORD error=GetSecurityInfo(object,SE_KERNEL_OBJECT,OWNER_SECURITY_INFORMATION,&owner,nullptr,nullptr,nullptr,&descriptor);
    LPWSTR actual=nullptr;bool ok=error==ERROR_SUCCESS&&owner&&ConvertSidToStringSidW(owner,&actual)&&sid==actual;
    if(actual)LocalFree(actual);if(descriptor)LocalFree(descriptor);return ok;
}
// 核验顶层窗口仍属于指定进程和线程。
inline bool WindowThreadBound(HWND hwnd,DWORD pid,DWORD tid) {
    DWORD actualPid=0;DWORD actualTid=GetWindowThreadProcessId(hwnd,&actualPid);
    return hwnd&&IsWindow(hwnd)&&actualPid==pid&&actualTid==tid&&tid!=0&&GetAncestor(hwnd,GA_ROOT)==hwnd;
}
// 限制目标为配置的钉钉程序路径。
inline bool LockedTarget(const ProcessIdentity& target) { return SamePath(target.path,kExpectedTargetPath); }
// 限制启动方为本构建的控制器路径。
inline bool LockedController(const ProcessIdentity& controller) { return SamePath(controller.path,kExpectedControllerPath); }
}
