#define NOMINMAX
#include "GpuRoiMask.h"
#include <chrono>
#include <iostream>
#include <thread>
using namespace bridge_wgc;
static bool In(const D3D11_RECT& r,int x,int y){return x>=r.left&&x<r.right&&y>=r.top&&y<r.bottom;}
int main(){
    HRESULT last=S_OK;
    try{
        HardwareGpu gpu;last=gpu.Create();if(FAILED(last))throw 1;
        constexpr UINT sw=64,sh=64,rw=40,rh=32;
        std::vector<uint32_t> pixels(sw*sh);
        for(UINT y=0;y<sh;y++)for(UINT x=0;x<sw;x++)pixels[y*sw+x]=0xFF000000u|((x*3u)<<16)|((y*3u)<<8)|((x+y)&255u);
        D3D11_TEXTURE2D_DESC desc{};desc.Width=sw;desc.Height=sh;desc.MipLevels=1;desc.ArraySize=1;
        desc.Format=DXGI_FORMAT_B8G8R8A8_UNORM;desc.SampleDesc.Count=1;desc.Usage=D3D11_USAGE_DEFAULT;desc.BindFlags=D3D11_BIND_SHADER_RESOURCE;
        D3D11_SUBRESOURCE_DATA initial{};initial.pSysMem=pixels.data();initial.SysMemPitch=sw*4;
        ComPtr<ID3D11Texture2D> source;last=gpu.Device->CreateTexture2D(&desc,&initial,&source);if(FAILED(last))throw 2;
        SourceRoi roi{8,8,rw,rh};std::vector<D3D11_RECT> masks{{0,0,9,12},{8,4,28,20},{39,31,40,32}};
        auto prepared=PrepareMaskedRoi(gpu,source.Get(),sw,sh,roi,masks);
        if(prepared.Status!=PrepareStatus::QueuedGpuWork||!prepared.Texture)throw 3;
        auto allMasked=PrepareMaskedRoi(gpu,source.Get(),sw,sh,roi,{{0,0,(LONG)rw,(LONG)rh}});
        bool fullGuard=allMasked.Status==PrepareStatus::AllMasked&&!allMasked.Texture&&allMasked.UnmaskedArea==0;
        auto badRect=PrepareMaskedRoi(gpu,source.Get(),sw,sh,roi,{{-1,0,2,2}});
        auto outside=PrepareMaskedRoi(gpu,source.Get(),sw,sh,SourceRoi{60,0,8,8},{});
        bool invalidGuards=badRect.Status==PrepareStatus::InvalidGeometry&&!badRect.Texture&&outside.Status==PrepareStatus::InvalidGeometry&&!outside.Texture;
        // Only this synthetic test performs readback; the reusable module has no Map.
        D3D11_TEXTURE2D_DESC read{};prepared.Texture->GetDesc(&read);
        bool cpuFlags=read.CPUAccessFlags==0&&desc.CPUAccessFlags==0;
        read.Usage=D3D11_USAGE_STAGING;read.BindFlags=0;read.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
        ComPtr<ID3D11Texture2D> staging;last=gpu.Device->CreateTexture2D(&read,nullptr,&staging);if(FAILED(last))throw 4;
        gpu.Context->CopyResource(staging.Get(),prepared.Texture.Get());
        D3D11_MAPPED_SUBRESOURCE mapped{};auto until=std::chrono::steady_clock::now()+std::chrono::seconds(2);
        do{last=gpu.Context->Map(staging.Get(),0,D3D11_MAP_READ,D3D11_MAP_FLAG_DO_NOT_WAIT,&mapped);
            if(last==DXGI_ERROR_WAS_STILL_DRAWING)std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }while(last==DXGI_ERROR_WAS_STILL_DRAWING&&std::chrono::steady_clock::now()<until);
        if(FAILED(last))throw 5;
        last=gpu.Device->GetDeviceRemovedReason();
        if(FAILED(last)){gpu.Context->Unmap(staging.Get(),0);throw 6;}
        UINT mismatches=0,masked=0,unchanged=0,rowPitch=mapped.RowPitch;
        for(UINT y=0;y<rh;y++){
            auto row=(const uint32_t*)((const uint8_t*)mapped.pData+(size_t)y*mapped.RowPitch);
            for(UINT x=0;x<rw;x++){
                bool hide=false;for(auto r:masks)hide=hide||In(r,x,y);
                if(row[x]!=(hide?0xFFFFFFFFu:pixels[(y+8)*sw+x+8]))mismatches++;
                if(hide)masked++;else unchanged++;
            }
        }
        gpu.Context->Unmap(staging.Get(),0);
        bool success=mismatches==0&&invalidGuards&&fullGuard&&cpuFlags&&rowPitch>=rw*4&&masked==prepared.MaskedArea&&unchanged==prepared.UnmaskedArea;
        std::cout<<"{\"status\":\""<<(success?"passed":"failed")<<"\",\"hardware_only\":true,\"debug_layer\":false,\"real_capture_started\":false,"
            <<"\"source_cpu_access_zero\":"<<(cpuFlags?"true":"false")<<",\"roi_width\":"<<rw<<",\"roi_height\":"<<rh
            <<",\"mapped_only_synthetic_masked_roi\":true,\"row_pitch\":"<<rowPitch<<",\"masked_pixels\":"<<masked
            <<",\"unchanged_pixels\":"<<unchanged<<",\"pixel_mismatches\":"<<mismatches<<",\"invalid_rect_guards\":"<<(invalidGuards?"true":"false")
            <<",\"all_masked_rejected_before_roi_allocation\":"<<(fullGuard?"true":"false")<<",\"shared_gpu_primitive\":true,\"tested_boundary_overlap_and_stride\":true}"<<std::endl;
        return success?0:1;
    }catch(...){
        std::cout<<"{\"status\":\"gpu_selftest_unavailable\",\"hardware_only\":true,\"real_capture_started\":false,\"hresult\":"<<(int32_t)last<<"}"<<std::endl;
        return 1;
    }
}
