#define NOMINMAX
#include <windows.h>
#include <dwmapi.h>
#include <windows.graphics.capture.interop.h>
#include <winrt/base.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Foundation.Collections.h>
#include <winrt/Windows.Data.Json.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <iostream>
#include <string>
#include <vector>

using namespace winrt;
using namespace Windows::Data::Json;
using namespace Windows::Graphics::Capture;
static const wchar_t* ExpectedImage=L"D:\\Program Files\\DingDing\\main\\current_new\\DingTalk.exe";
struct Handle { HANDLE value=nullptr; ~Handle(){if(value) CloseHandle(value);} };
static JsonObject RectJson(const RECT& r) {
    JsonObject j; j.Insert(L"left",JsonValue::CreateNumberValue(r.left));
    j.Insert(L"top",JsonValue::CreateNumberValue(r.top)); j.Insert(L"right",JsonValue::CreateNumberValue(r.right));
    j.Insert(L"bottom",JsonValue::CreateNumberValue(r.bottom)); return j;
}
static bool EqualRect(const RECT& a,const RECT& b){return a.left==b.left&&a.top==b.top&&a.right==b.right&&a.bottom==b.bottom;}
static bool Inside(const RECT& a,const RECT& b){return a.left<=b.left&&a.top<=b.top&&a.right>=b.right&&a.bottom>=b.bottom;}
static bool Valid(const RECT& r){return r.right>r.left&&r.bottom>r.top&&(int64_t)r.right-r.left<=16000&&(int64_t)r.bottom-r.top<=16000;}
static RECT InputRect(const JsonArray& a) {
    if(a.Size()!=4) throw hresult_invalid_argument();
    RECT r{(LONG)a.GetNumberAt(0),(LONG)a.GetNumberAt(1),(LONG)a.GetNumberAt(2),(LONG)a.GetNumberAt(3)};
    if(!Valid(r)) throw hresult_invalid_argument(); return r;
}
static bool ImageMatches(DWORD pid) {
    Handle h{OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,FALSE,pid)};
    if(!h.value) return false; wchar_t text[32768]; DWORD count=32768;
    return QueryFullProcessImageNameW(h.value,0,text,&count)&&
        CompareStringOrdinal(text,count,ExpectedImage,-1,TRUE)==CSTR_EQUAL;
}
static bool SameSession(DWORD pid) {
    DWORD a=0,b=0; return ProcessIdToSessionId(pid,&a)&&ProcessIdToSessionId(GetCurrentProcessId(),&b)&&a==b;
}
static DWORD Pid(HWND hwnd){DWORD pid=0;GetWindowThreadProcessId(hwnd,&pid);return pid;}
static std::wstring Class(HWND hwnd){wchar_t value[256]{};GetClassNameW(hwnd,value,256);return value;}
static bool Visible(HWND hwnd){return IsWindow(hwnd)&&IsWindowVisible(hwnd)&&!IsIconic(GetAncestor(hwnd,GA_ROOT));}
static void Text(JsonObject& j,const wchar_t* key,const wchar_t* value){j.Insert(key,JsonValue::CreateStringValue(value));}
static void Flag(JsonObject& j,const wchar_t* key,bool value){j.Insert(key,JsonValue::CreateBooleanValue(value));}
static void Number(JsonObject& j,const wchar_t* key,double value){j.Insert(key,JsonValue::CreateNumberValue(value));}

int main() {
    JsonObject result; Text(result,L"status",L"probe_failed");Flag(result,L"capture_started",false);
    Flag(result,L"frame_pool_created",false);Flag(result,L"cpu_image_created",false);Flag(result,L"capture_ready",false);
    try {
        init_apartment(apartment_type::single_threaded);
        if(!SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)) throw hresult_error(HRESULT_FROM_WIN32(GetLastError()));
        std::string input;std::getline(std::cin,input);if(input.size()>65536)throw hresult_invalid_argument();
        if(input.compare(0,3,"\xEF\xBB\xBF")==0)input.erase(0,3);
        auto request=JsonObject::Parse(to_hstring(input));auto g=request.GetNamedObject(L"geometry");
        if(!g.GetNamedBoolean(L"geometry_safe")||g.GetNamedString(L"coordinate_space")!=L"physical_screen_pixels"||
            g.GetNamedString(L"dpi_awareness")!=L"per_monitor_v2"||g.GetNamedString(L"verified_process_path")!=ExpectedImage)throw hresult_invalid_argument();
        auto host=(HWND)(uintptr_t)g.GetNamedNumber(L"hwnd");auto cef=(HWND)(uintptr_t)g.GetNamedNumber(L"message_hwnd");
        auto pid=(DWORD)g.GetNamedNumber(L"pid");RECT hostExpected=InputRect(g.GetNamedArray(L"window_rect_screen"));
        RECT cefExpected=InputRect(g.GetNamedArray(L"message_window_rect_screen"));RECT roi=InputRect(g.GetNamedArray(L"message_rect_screen"));
        HWND top=GetAncestor(cef,GA_ROOT);RECT hostRect{},cefRect{},windowRect{},dwmRect{},clientRect{};
        if(!top||top==cef||!Visible(top)||!Visible(cef)||!Visible(host)||GetAncestor(top,GA_ROOT)!=top||
            !IsChild(top,cef)||!IsChild(top,host)||Pid(top)!=pid||Pid(cef)!=pid||Pid(host)!=pid||
            !ImageMatches(pid)||!SameSession(pid)||Class(cef)!=L"CefBrowserWindow"||
            !GetWindowRect(host,&hostRect)||!GetWindowRect(cef,&cefRect)||
            !EqualRect(hostRect,hostExpected)||!EqualRect(cefRect,cefExpected)||!Inside(hostRect,roi)||!Inside(cefRect,roi)){
            Text(result,L"status",L"target_not_verified");
        } else {
            DWORD affinity=0;SetLastError(ERROR_SUCCESS);
            BOOL affinityOk=GetWindowDisplayAffinity(top,&affinity);DWORD affinityError=affinityOk?0:GetLastError();
            Flag(result,L"display_affinity_read",affinityOk!=FALSE);Number(result,L"display_affinity",affinity);Number(result,L"display_affinity_error",affinityError);
            if(!affinityOk||affinity!=WDA_NONE){Text(result,L"status",L"capture_protection_or_affinity_unknown");}
            else if(!GetWindowRect(top,&windowRect)||FAILED(DwmGetWindowAttribute(top,DWMWA_EXTENDED_FRAME_BOUNDS,&dwmRect,sizeof(dwmRect)))||!Valid(windowRect)||!Valid(dwmRect)){
                Text(result,L"status",L"frame_bounds_unavailable");
            }else{
                DWORD cloaked=1;HRESULT cloakHr=DwmGetWindowAttribute(top,DWMWA_CLOAKED,&cloaked,sizeof(cloaked));
                if(FAILED(cloakHr)||cloaked){Text(result,L"status",L"toplevel_not_visible");}
                else{
                    bool supported=GraphicsCaptureSession::IsSupported();Flag(result,L"device_supported",supported);
                    result.Insert(L"window_rect_screen",RectJson(windowRect));result.Insert(L"dwm_frame_bounds_screen",RectJson(dwmRect));
                    result.Insert(L"cef_rect_screen",RectJson(cefRect));result.Insert(L"selected_roi_screen",RectJson(roi));
                    Number(result,L"toplevel_hwnd",(double)(uintptr_t)top);Number(result,L"toplevel_pid",pid);
                    Text(result,L"toplevel_class",Class(top).c_str());Flag(result,L"same_session",true);Flag(result,L"verified_ancestry",true);
                    if(!supported){Text(result,L"status",L"wgc_device_unsupported");}
                    else{
                        auto factory=get_activation_factory<GraphicsCaptureItem,IGraphicsCaptureItemInterop>();
                        GraphicsCaptureItem item{nullptr};HRESULT hr=factory->CreateForWindow(top,guid_of<GraphicsCaptureItem>(),put_abi(item));
                        Number(result,L"create_hresult",(int32_t)hr);
                        if(FAILED(hr)||!item){Text(result,L"status",L"wgc_toplevel_item_rejected");}
                        else{
                            auto size=item.Size();Flag(result,L"item_created",true);Number(result,L"item_width",size.Width);Number(result,L"item_height",size.Height);
                            bool matchesDwm=size.Width==dwmRect.right-dwmRect.left&&size.Height==dwmRect.bottom-dwmRect.top;
                            bool matchesWindow=size.Width==windowRect.right-windowRect.left&&size.Height==windowRect.bottom-windowRect.top;
                            bool ambiguous=matchesDwm&&matchesWindow&&!EqualRect(dwmRect,windowRect);
                            RECT basis=matchesDwm?dwmRect:windowRect;
                            Flag(result,L"size_matches_dwm",matchesDwm);Flag(result,L"size_matches_window",matchesWindow);Flag(result,L"coordinate_mapping_ambiguous",ambiguous);
                            bool mapped=(matchesDwm||matchesWindow)&&!ambiguous&&Inside(basis,roi);
                            Flag(result,L"roi_mapping_valid",mapped);Text(result,L"coordinate_basis",matchesDwm?L"dwm_extended_frame_bounds":L"window_rect");
                            if(mapped){RECT textureRoi{roi.left-basis.left,roi.top-basis.top,roi.right-basis.left,roi.bottom-basis.top};result.Insert(L"roi_texture_pixels",RectJson(textureRoi));}
                            RECT freshTop{},freshCef{};DWORD freshAffinity=1;
                            bool stable=GetWindowRect(top,&freshTop)&&GetWindowRect(cef,&freshCef)&&EqualRect(freshTop,windowRect)&&EqualRect(freshCef,cefRect)&&
                                GetWindowDisplayAffinity(top,&freshAffinity)&&freshAffinity==0&&Pid(top)==pid&&Pid(cef)==pid&&ImageMatches(pid)&&SameSession(pid);
                            Flag(result,L"native_metadata_stable",stable);
                            Text(result,L"status",mapped&&stable?L"metadata_ready_review_required":L"coordinate_mapping_or_scene_changed");
                            item=nullptr;
                        }
                    }
                }
            }
        }
    }catch(const hresult_error& e){Number(result,L"error_hresult",(int32_t)e.code());Text(result,L"status",L"metadata_probe_failed");}
    catch(...){Text(result,L"status",L"metadata_probe_failed");}
    std::cout<<to_string(result.Stringify())<<std::endl;
    return 0;
}
