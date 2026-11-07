#pragma once
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <d3d11_1.h>
#include <wrl/client.h>
#include <algorithm>
#include <cstdint>
#include <utility>
#include <vector>

// GPU-only primitive. This file contains no capture, staging, Map, encoding,
// pixel pointer, HWND or file API. Real readback requires a separate reviewed
// scene/permission handshake; this module cannot start it.
namespace bridge_wgc {
using Microsoft::WRL::ComPtr;
struct HardwareGpu {
    ComPtr<ID3D11Device> Device;
    ComPtr<ID3D11DeviceContext> Context;
    ComPtr<ID3D11DeviceContext1> Context1;
    D3D_FEATURE_LEVEL Feature{};
    HRESULT Create() {
        D3D_FEATURE_LEVEL levels[]={D3D_FEATURE_LEVEL_11_1,D3D_FEATURE_LEVEL_11_0};
        HRESULT hr=D3D11CreateDevice(nullptr,D3D_DRIVER_TYPE_HARDWARE,nullptr,D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            levels,2,D3D11_SDK_VERSION,&Device,&Feature,&Context);
        if(FAILED(hr)) return hr; // Never retry WARP, REFERENCE or debug.
        return Context.As(&Context1);
    }
};
struct SourceRoi { UINT X,Y,Width,Height; };
enum class PrepareStatus { QueuedGpuWork, AllMasked, InvalidGeometry, UnsupportedSource, DeviceMismatch, GpuFailure };
struct PreparedGpuRoi {
    PrepareStatus Status=PrepareStatus::GpuFailure;
    HRESULT Error=S_OK;
    ComPtr<ID3D11Texture2D> Texture;
    UINT Width=0,Height=0;
    uint64_t MaskedArea=0,UnmaskedArea=0;
};
inline bool ValidMask(const D3D11_RECT& r,UINT width,UINT height) {
    return r.left>=0&&r.top>=0&&r.right>r.left&&r.bottom>r.top&&
        (uint64_t)r.right<=width&&(uint64_t)r.bottom<=height;
}
inline uint64_t MaskUnionArea(UINT width,UINT height,const std::vector<D3D11_RECT>& masks) {
    for(const auto& r:masks)if(r.left==0&&r.top==0&&(UINT)r.right==width&&(UINT)r.bottom==height)return(uint64_t)width*height;
    std::vector<LONG> xs{0,(LONG)width};
    for(const auto& r:masks){xs.push_back(r.left);xs.push_back(r.right);}
    std::sort(xs.begin(),xs.end());xs.erase(std::unique(xs.begin(),xs.end()),xs.end());
    uint64_t area=0;
    for(size_t index=1;index<xs.size();index++){
        LONG left=xs[index-1],right=xs[index];std::vector<std::pair<LONG,LONG>> ys;
        for(const auto& r:masks)if(r.left<right&&r.right>left)ys.emplace_back(r.top,r.bottom);
        std::sort(ys.begin(),ys.end());LONG bottom=0;uint64_t covered=0;
        for(const auto& y:ys){LONG top=std::max(bottom,y.first);if(y.second>top)covered+=y.second-top;bottom=std::max(bottom,y.second);}
        area+=(uint64_t)(right-left)*covered;
    }
    return area;
}
inline PreparedGpuRoi PrepareMaskedRoi(HardwareGpu& gpu,ID3D11Texture2D* source,
    UINT expectedSourceWidth,UINT expectedSourceHeight,SourceRoi roi,const std::vector<D3D11_RECT>& masks) {
    PreparedGpuRoi out;out.Width=roi.Width;out.Height=roi.Height;
    if(!source||!gpu.Device||!gpu.Context||!gpu.Context1){out.Status=PrepareStatus::GpuFailure;out.Error=E_POINTER;return out;}
    D3D11_TEXTURE2D_DESC src{};source->GetDesc(&src);
    if(src.Width!=expectedSourceWidth||src.Height!=expectedSourceHeight||src.Format!=DXGI_FORMAT_B8G8R8A8_UNORM||
        src.Usage!=D3D11_USAGE_DEFAULT||src.CPUAccessFlags!=0||src.ArraySize!=1||src.MipLevels!=1||src.SampleDesc.Count!=1){
        out.Status=PrepareStatus::UnsupportedSource;return out;
    }
    if(roi.Width==0||roi.Height==0||roi.Width>4096||roi.Height>4096||(uint64_t)roi.Width*roi.Height>12000000||
        (uint64_t)roi.X+roi.Width>src.Width||(uint64_t)roi.Y+roi.Height>src.Height||masks.size()>1500){
        out.Status=PrepareStatus::InvalidGeometry;return out;
    }
    for(const auto& r:masks)if(!ValidMask(r,roi.Width,roi.Height)){out.Status=PrepareStatus::InvalidGeometry;return out;}
    out.MaskedArea=MaskUnionArea(roi.Width,roi.Height,masks);
    out.UnmaskedArea=(uint64_t)roi.Width*roi.Height-out.MaskedArea;
    if(out.UnmaskedArea==0){out.Status=PrepareStatus::AllMasked;return out;} // No ROI allocation/copy at all.
    ComPtr<ID3D11Device> sourceDevice;source->GetDevice(&sourceDevice);
    ComPtr<IUnknown> a,b;
    if(FAILED(sourceDevice.As(&a))||FAILED(gpu.Device.As(&b))||a.Get()!=b.Get()){out.Status=PrepareStatus::DeviceMismatch;return out;}
    D3D11_TEXTURE2D_DESC desc{};desc.Width=roi.Width;desc.Height=roi.Height;desc.ArraySize=1;desc.MipLevels=1;
    desc.Format=DXGI_FORMAT_B8G8R8A8_UNORM;desc.SampleDesc.Count=1;desc.Usage=D3D11_USAGE_DEFAULT;
    desc.BindFlags=D3D11_BIND_RENDER_TARGET;desc.CPUAccessFlags=0;
    out.Error=gpu.Device->CreateTexture2D(&desc,nullptr,&out.Texture);if(FAILED(out.Error))return out;
    ComPtr<ID3D11RenderTargetView> rtv;out.Error=gpu.Device->CreateRenderTargetView(out.Texture.Get(),nullptr,&rtv);
    if(FAILED(out.Error)){out.Texture.Reset();return out;}
    D3D11_BOX box{roi.X,roi.Y,0,roi.X+roi.Width,roi.Y+roi.Height,1};
    gpu.Context->CopySubresourceRegion(out.Texture.Get(),0,0,0,0,source,0,&box);
    if(!masks.empty()){
        const FLOAT opaqueWhite[]={1,1,1,1};
        gpu.Context1->ClearView(rtv.Get(),opaqueWhite,masks.data(),(UINT)masks.size());
    }
    // Copy/Clear commands are queued, not proof of completion or Map permission.
    out.Status=PrepareStatus::QueuedGpuWork;
    return out;
}
} // namespace bridge_wgc
