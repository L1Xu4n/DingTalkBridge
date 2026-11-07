#define NOMINMAX
#include <iostream>
#include "OnceGuard.h"
#include "GpuRoiMask.h"
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <winrt/Windows.Graphics.Imaging.h>
#include <winrt/Windows.Media.Ocr.h>
#include <winrt/Windows.Globalization.h>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <iomanip>
#include <mutex>
#include <memory>
#include <sstream>
using namespace winrt;
using namespace winrt::Windows::Graphics::Capture;
using namespace winrt::Windows::Graphics::Imaging;
using namespace winrt::Windows::Graphics::DirectX;
using namespace winrt::Windows::Graphics::DirectX::Direct3D11;
using namespace winrt::Windows::Media::Ocr;
using namespace bridge_once;
using namespace bridge_wgc;
struct __declspec(uuid("5B0D3235-4DBA-4D44-8656-1BC31CD43B5E")) MemoryBytes : IUnknown {
    virtual HRESULT __stdcall GetBuffer(uint8_t** value,uint32_t* capacity)=0;
};
static std::wstring Stamp(){
    SYSTEMTIME t{};GetSystemTime(&t);std::wostringstream s;
    s<<std::setfill(L'0')<<std::setw(4)<<t.wYear<<L"-"<<std::setw(2)<<t.wMonth<<L"-"<<std::setw(2)<<t.wDay<<L"T"<<std::setw(2)<<t.wHour<<L":"<<std::setw(2)<<t.wMinute<<L":"<<std::setw(2)<<t.wSecond<<L"."<<std::setw(3)<<t.wMilliseconds<<L"Z";return s.str();
}
static std::vector<D3D11_RECT> Masks(const JsonArray& values,const RECT& roi){
    Need(values.Size()<=1500);std::vector<D3D11_RECT> result;
    for(auto value:values){auto r=Rect(value.GetArray());Need(r.left>=0&&r.top>=0&&r.right<=roi.right-roi.left&&r.bottom<=roi.bottom-roi.top);result.push_back(D3D11_RECT{r.left,r.top,r.right,r.bottom});}
    return result;
}
struct CaptureState {
 std::mutex Lock; std::condition_variable Arrival; Direct3D11CaptureFrame Frame{nullptr}; std::atomic<bool> Accepting{true}; bool Selected=false; uint64_t Minimum=0;
 ~CaptureState(){try{if(Frame)Frame.Close();}catch(...){}}
};
static int GuardSelfTest(){
    init_apartment(apartment_type::multi_threaded);bool parent=!ParentChainMatches(1,2,3,4);
    Handle event;event.H=CreateEventW(nullptr,FALSE,FALSE,nullptr);Need(event.H!=nullptr);
    bool unsignaled=!ConsumeEventSignal(event.H),invalid=!ConsumeEventSignal(INVALID_HANDLE_VALUE);
    SetEvent(event.H);bool once=ConsumeEventSignal(event.H)&&!ConsumeEventSignal(event.H);
    bool fractional=false;try{JsonArray r;r.Append(JsonValue::CreateNumberValue(0.25));for(int n=0;n<3;n++)r.Append(JsonValue::CreateNumberValue(10+n));Rect(r);}catch(...){fractional=true;}
    bool forged=false;try{BoundState g;JsonObject j;Flag(j,L"authorization_granted",true);g.Load(j);}catch(...){forged=true;}
    bool ok=parent&&unsignaled&&invalid&&once&&fractional&&forged&&!LocalPath(L"\\\\example.invalid\\unread");
    JsonObject out;Text(out,L"status",ok?L"passed":L"failed");Flag(out,L"production_parent_predicate",parent);Flag(out,L"production_event_primitive",unsignaled&&invalid&&once);
    Flag(out,L"fractional_rectangle_rejected",fractional);Flag(out,L"forged_true_json_rejected",forged);Flag(out,L"real_framepool_created",false);Flag(out,L"real_capture_started",false);Flag(out,L"real_pixels_mapped",false);Emit(out);return ok?0:1;
}

int main(int argc,char**argv){
    if(argc==2&&std::string(argv[1])=="--guard-selftest")return GuardSelfTest();
    JsonObject result;Text(result,L"type",L"native_result");Text(result,L"status",L"rejected");
    Flag(result,L"capture_started",false);Flag(result,L"pixels_read",false);
    if(argc!=2||std::string(argv[1])!="--live-ui-once"){Text(result,L"reason",L"live_ui_mode_required");Emit(result);return 1;}
    GraphicsCaptureSession session{nullptr};Direct3D11CaptureFramePool pool{nullptr};Direct3D11CaptureFrame frame{nullptr};
    Direct3D11CaptureFramePool::FrameArrived_revoker frameRevoker{};
    std::shared_ptr<CaptureState> captureState;
    BoundState bound;
    SoftwareBitmap bitmap{nullptr};winrt::Windows::Foundation::IMemoryBufferReference reference{nullptr};
    uint8_t* bitmapBytes=nullptr;uint32_t bitmapCapacity=0;bool mapped=false;ComPtr<ID3D11Texture2D> staging;HardwareGpu gpu;
    try{
        init_apartment(apartment_type::multi_threaded);
        Need(SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)!=nullptr);
        auto input=ReadLineBounded(3000);if(input.compare(0,3,"\xEF\xBB\xBF")==0)input.erase(0,3);
        bound.Load(JsonObject::Parse(to_hstring(input))); // Consumes UI event2 before any GPU/capture object.
        Need(ParentChainMatches(ParentPid(GetCurrentProcessId()),ParentPid(bound.Python.Pid),bound.Python.Pid,bound.Ui.Pid));
        bound.CheckPhase();Need(bound.Consumed);
        RECT basis=Rect(bound.Request.GetNamedArray(L"capture_basis"));Need(Same(basis,bound.Dwm)||Same(basis,bound.Window));
        UINT width=(UINT)(bound.Roi.right-bound.Roi.left),height=(UINT)(bound.Roi.bottom-bound.Roi.top);
        Need(width>=100&&height>=40&&width<=4096&&height<=4096&&(uint64_t)width*height<=12000000);
        Need(bound.Request.GetNamedString(L"mask_padding_contract")==L"final_roi_pixels_padding_applied_once_v1");
        auto masks=Masks(bound.Request.GetNamedArray(L"masks_roi_pixels"),bound.Roi);
        Need(MaskUnionArea(width,height,masks)==UInt(bound.Request,L"expected_masked_union_area",12000000));
        for(auto m:masks)Need(ValidMask(m,width,height));
        Need(MaskUnionArea(width,height,masks)<(uint64_t)width*height); // Before device/framepool/StartCapture.
        Need(GraphicsCaptureSession::IsSupported());
        check_hresult(gpu.Create()); // HARDWARE only; never WARP/reference/debug.
        auto factory=get_activation_factory<GraphicsCaptureItem,IGraphicsCaptureItemInterop>();
        GraphicsCaptureItem item{nullptr};check_hresult(factory->CreateForWindow(bound.Top,guid_of<GraphicsCaptureItem>(),put_abi(item)));
        auto itemSize=item.Size();Need(itemSize.Width==basis.right-basis.left&&itemSize.Height==basis.bottom-basis.top);
        ComPtr<IDXGIDevice> dxgi;check_hresult(gpu.Device.As(&dxgi));
        IDirect3DDevice winrtDevice{nullptr};
        check_hresult(CreateDirect3D11DeviceFromDXGIDevice(dxgi.Get(),reinterpret_cast<IInspectable**>(put_abi(winrtDevice))));
        bound.CheckPhase();
        pool=Direct3D11CaptureFramePool::CreateFreeThreaded(winrtDevice,DirectXPixelFormat::B8G8R8A8UIntNormalized,1,itemSize);
        session=pool.CreateCaptureSession(item); // No privacy/border/cursor properties are changed.
        captureState=std::make_shared<CaptureState>();
        uint64_t proofQpc=Decimal(bound.Request.GetNamedString(L"proof_complete_qpc_100ns"));
        uint64_t captureQpc=Qpc100ns();uint64_t minimum=std::max(proofQpc,captureQpc);
        captureState->Minimum=minimum;
        frameRevoker=pool.FrameArrived(auto_revoke,[state=captureState](auto const&sender,auto const&){
            try{
                auto next=sender.TryGetNextFrame();if(!next)return;
                auto ticks=(uint64_t)next.SystemRelativeTime().count();
                if(!state->Accepting||ticks<state->Minimum||ticks>Qpc100ns()+10000){next.Close();return;}
                std::lock_guard<std::mutex> guard(state->Lock);
                if(state->Accepting&&!state->Selected){state->Frame=next;state->Selected=true;state->Arrival.notify_one();}else next.Close();
            }catch(...){state->Arrival.notify_one();}
        });
        bound.CheckPhase();session.StartCapture();Flag(result,L"capture_started",true);
        JsonObject started;Text(started,L"type",L"native_capture_started");Text(started,L"attempt_id",bound.Attempt);Flag(started,L"capture_started",true);Flag(started,L"pixels_read",false);Emit(started);
        auto until=GetTickCount64()+3000;
        while(true){
            Need(bound.Check());
            {std::unique_lock<std::mutex> guard(captureState->Lock);if(captureState->Selected){frame=captureState->Frame;captureState->Frame=nullptr;break;}captureState->Arrival.wait_for(guard,std::chrono::milliseconds(5));}
            Need(GetTickCount64()<until);
        }
        captureState->Accepting=false;frameRevoker.revoke();session.Close();session=nullptr; // Stop after one selected frame.
        bound.CheckPhase();auto content=frame.ContentSize();Need(content.Width==itemSize.Width&&content.Height==itemSize.Height);
        Need(Qpc100ns()-(uint64_t)frame.SystemRelativeTime().count()<=50000000);
        auto access=frame.Surface().as<::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();ComPtr<ID3D11Texture2D> source;
        check_hresult(access->GetInterface(__uuidof(ID3D11Texture2D),reinterpret_cast<void**>(source.GetAddressOf())));
        SourceRoi roi{(UINT)(bound.Roi.left-basis.left),(UINT)(bound.Roi.top-basis.top),width,height};
        auto prepared=PrepareMaskedRoi(gpu,source.Get(),itemSize.Width,itemSize.Height,roi,masks);
        Need(prepared.Status==PrepareStatus::QueuedGpuWork&&prepared.Texture!=nullptr);
        JsonObject ready;Text(ready,L"type",L"native_ready_for_map");Text(ready,L"attempt_id",bound.Attempt);
        Text(ready,L"scene_binding_sha256",bound.SceneHash);Text(ready,L"backend_digest",bound.BackendDigest);
        Number(ready,L"width",width);Number(ready,L"height",height);Number(ready,L"masked_pixels",(double)prepared.MaskedArea);
        Flag(ready,L"gpu_work_queued",true);Flag(ready,L"capture_started",true);Flag(ready,L"pixels_read",false);Emit(ready);
        auto permit=JsonObject::Parse(to_hstring(ReadLineBounded(5000,&bound)));
        Need(permit.GetNamedString(L"schema")==L"dingtalkbridge.wgc-once-permit-map.v1"&&permit.GetNamedString(L"type")==L"permit_map"&&permit.GetNamedBoolean(L"permit_map"));
        Need(permit.GetNamedString(L"attempt_id")==bound.Attempt&&permit.GetNamedString(L"scene_binding_sha256")==bound.SceneHash&&permit.GetNamedString(L"backend_digest")==bound.BackendDigest);
        bound.CheckPhase();Need(Qpc100ns()-(uint64_t)frame.SystemRelativeTime().count()<=80000000);
        check_hresult(gpu.Device->GetDeviceRemovedReason());
        D3D11_TEXTURE2D_DESC read{};prepared.Texture->GetDesc(&read);Need(read.CPUAccessFlags==0&&read.Width==width&&read.Height==height);
        read.Usage=D3D11_USAGE_STAGING;read.BindFlags=0;read.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
        check_hresult(gpu.Device->CreateTexture2D(&read,nullptr,&staging));gpu.Context->CopyResource(staging.Get(),prepared.Texture.Get());
        D3D11_MAPPED_SUBRESOURCE data{};HRESULT mapHr=E_FAIL;until=GetTickCount64()+2000;
        do{Need(bound.Check());mapHr=gpu.Context->Map(staging.Get(),0,D3D11_MAP_READ,D3D11_MAP_FLAG_DO_NOT_WAIT,&data);
            if(mapHr==DXGI_ERROR_WAS_STILL_DRAWING)Sleep(2);
        }while(mapHr==DXGI_ERROR_WAS_STILL_DRAWING&&GetTickCount64()<until);
        check_hresult(mapHr);mapped=true;Flag(result,L"pixels_read",true);check_hresult(gpu.Device->GetDeviceRemovedReason());bound.CheckPhase();
        bitmap=SoftwareBitmap(BitmapPixelFormat::Bgra8,width,height,BitmapAlphaMode::Premultiplied);
        auto buffer=bitmap.LockBuffer(BitmapBufferAccessMode::Write);auto plane=buffer.GetPlaneDescription(0);reference=buffer.CreateReference();
        auto bytes=reference.as<MemoryBytes>();check_hresult(bytes->GetBuffer(&bitmapBytes,&bitmapCapacity));
        Need(data.RowPitch>=width*4&&plane.Stride>=(int)width*4&&plane.StartIndex>=0&&
            (uint64_t)plane.StartIndex+(uint64_t)plane.Stride*(height-1)+width*4<=bitmapCapacity);
        for(UINT y=0;y<height;y++)memcpy(bitmapBytes+plane.StartIndex+(size_t)y*plane.Stride,(BYTE*)data.pData+(size_t)y*data.RowPitch,width*4);
        gpu.Context->Unmap(staging.Get(),0);mapped=false;buffer.Close();reference.Close();reference=nullptr;bitmapBytes=nullptr;bitmapCapacity=0;
        frame.Close();frame=nullptr;pool.Close();pool=nullptr;source.Reset();staging.Reset();bound.CheckPhase();
        auto engine=OcrEngine::TryCreateFromLanguage(winrt::Windows::Globalization::Language(L"zh-CN"));Need(engine!=nullptr);
        auto operation=engine.RecognizeAsync(bitmap);
        while(operation.Status()==winrt::Windows::Foundation::AsyncStatus::Started){
            if(!bound.Check()){
                operation.Cancel();auto cancelUntil=GetTickCount64()+500;
                while(operation.Status()==winrt::Windows::Foundation::AsyncStatus::Started&&GetTickCount64()<cancelUntil)Sleep(5);
                if(operation.Status()==winrt::Windows::Foundation::AsyncStatus::Started){Text(result,L"status",L"failed");Text(result,L"reason",L"ocr_cancel_timeout");Emit(result);ExitProcess(2);}
                Need(false);
            }
            Sleep(5);
        }
        auto recognized=operation.GetResults();bound.CheckPhase();auto text=recognized.Text();Need(to_string(text).size()<=131072);
        Text(result,L"status",text.empty()?L"no_text":L"available");Text(result,L"text",text.c_str());
        Text(result,L"observed_at",Stamp());Text(result,L"language",engine.RecognizerLanguage().LanguageTag().c_str());
        Text(result,L"attempt_id",bound.Attempt);Text(result,L"scene_binding_sha256",bound.SceneHash);
        Flag(result,L"manual_roi_confirmed",true);Flag(result,L"automatic_input_validation_complete",false);
        Flag(result,L"strong_inputs_masked_before_cpu",true);Number(result,L"confidence",0);
        bitmap.Close();bitmap=nullptr;Text(result,L"reason",L"one_shot_masked_roi_observation");
        Emit(result);return 0;
    }catch(const hresult_error&e){Text(result,L"status",L"failed");Text(result,L"reason",L"native_guard_or_capture_failed");Number(result,L"hresult",(int32_t)e.code());}
    catch(...){Text(result,L"status",L"failed");Text(result,L"reason",L"native_once_failed");}
    if(result.HasKey(L"text"))result.Remove(L"text");
    Text(result,L"guard_stage",bound.Stage);
    if(captureState)captureState->Accepting=false;
    frameRevoker.revoke();
    if(mapped&&staging&&gpu.Context)gpu.Context->Unmap(staging.Get(),0);
    try{if(session)session.Close();if(frame)frame.Close();if(pool)pool.Close();if(reference)reference.Close();if(bitmap)bitmap.Close();}catch(...){}
    Emit(result);return 1;
}
