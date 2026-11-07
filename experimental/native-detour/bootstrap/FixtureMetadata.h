#pragma once
#include <windows.h>
#include <appmodel.h>
#include <iostream>
#include <string>
inline void FixtureMetadata(std::ostream& output) {
    UINT32 size=0;LONG package=GetPackageFullName(GetCurrentProcess(),&size,nullptr);
    HANDLE token=nullptr;DWORD container=0,uiAccess=0,returned=0;bool tokenOk=OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token)!=FALSE;
    bool tokenFlags=tokenOk&&GetTokenInformation(token,TokenIsAppContainer,&container,sizeof(container),&returned)&&GetTokenInformation(token,TokenUIAccess,&uiAccess,sizeof(uiAccess),&returned);
    if(token)CloseHandle(token);
    PROCESS_MITIGATION_DYNAMIC_CODE_POLICY dynamic{};PROCESS_MITIGATION_BINARY_SIGNATURE_POLICY signature{};
    PROCESS_MITIGATION_EXTENSION_POINT_DISABLE_POLICY extensions{};
    BOOL dynamicOk=GetProcessMitigationPolicy(GetCurrentProcess(),ProcessDynamicCodePolicy,&dynamic,sizeof(dynamic));
    BOOL signatureOk=GetProcessMitigationPolicy(GetCurrentProcess(),ProcessSignaturePolicy,&signature,sizeof(signature));
    BOOL extensionsOk=GetProcessMitigationPolicy(GetCurrentProcess(),ProcessExtensionPointDisablePolicy,&extensions,sizeof(extensions));
    wchar_t desktop[256]{};DWORD bytes=0;BOOL desktopOk=GetUserObjectInformationW(GetThreadDesktop(GetCurrentThreadId()),UOI_NAME,desktop,sizeof(desktop),&bytes);
    std::string desktopName;if(desktopOk)for(wchar_t c:desktop){if(!c)break;desktopName+=c>=32&&c<127?char(c):'?';}
    DWORD session=0;ProcessIdToSessionId(GetCurrentProcessId(),&session);
    output<<",\"packaged\":"<<(package==ERROR_INSUFFICIENT_BUFFER?1:0)<<",\"package_query_result\":"<<package
          <<",\"token_flags_valid\":"<<tokenFlags<<",\"app_container\":"<<container<<",\"ui_access\":"<<uiAccess
          <<",\"dynamic_policy_valid\":"<<dynamicOk<<",\"prohibit_dynamic_code\":"<<dynamic.ProhibitDynamicCode
          <<",\"signature_policy_valid\":"<<signatureOk<<",\"microsoft_signed_only\":"<<signature.MicrosoftSignedOnly
          <<",\"store_signed_only\":"<<signature.StoreSignedOnly<<",\"session\":"<<session<<",\"desktop\":\""<<desktopName<<"\"";
    output<<",\"extension_policy_valid\":"<<extensionsOk<<",\"disable_extension_points\":"<<extensions.DisableExtensionPoints;
}
