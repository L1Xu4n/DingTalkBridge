#define NOMINMAX
#include "HistoryReader.h"
#include "../payload/safe_read.h"
#include <array>
#include <charconv>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <functional>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <vector>

namespace bridge_native::history {
namespace payload = dingtalkbridge::native::payload;
namespace {
using Fields = std::map<std::string, std::string>;
using Rows = std::vector<Fields>;
using Success = std::function<void(const Rows&, const Fields&)>;
using Failure = std::function<void(const std::string&)>;
using Latest = void (__fastcall *)(void*, void*, int);
using Destroy = void (__fastcall *)(void*);
using Query = void (__fastcall *)(void*, const std::string&, int, std::int64_t, Success, Failure);
static_assert(sizeof(std::string)==32 && sizeof(Fields)==16 && sizeof(Rows)==24 && sizeof(Success)==64,
              "Requires the release MSVC x64 STL ABI");
constexpr std::uintptr_t kLatest=0x6526e10, kQuery=0x652b580, kDestroy=0x10d7b50;
constexpr unsigned char kLatestBytes[32]={0x48,0x89,0x5c,0x24,0x08,0x48,0x89,0x74,0x24,0x20,0x57,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,0x48,0x81,0xec,0x80,0x02,0x00,0x00,0x48,0x8b,0x05,0xe7,0xf2,0x6e};
constexpr unsigned char kQueryBytes[32]={0x40,0x53,0x56,0x57,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,0x48,0x81,0xec,0x10,0x03,0x00,0x00,0x48,0x8b,0x05,0x7e,0xab,0x6e,0x03,0x48,0x33,0xc4,0x48,0x89,0x84};
constexpr unsigned char kDestroyBytes[32]={0x48,0x89,0x5c,0x24,0x10,0x57,0x48,0x83,0xec,0x20,0x48,0x8b,0x19,0x48,0x8b,0xf9,0x48,0x85,0xdb,0x74,0x7f,0x48,0x89,0x74,0x24,0x30,0x48,0x8b,0x71,0x08,0x48,0x3b};
constexpr std::size_t kMaxFrame=512*1024, kMaxValue=128*1024, kMaxBatch=8*1024*1024;

// Check a selected entry signature, without requiring a whole-client file hash.
bool Matches(std::uintptr_t address,const unsigned char* expected,std::size_t length) noexcept {
    unsigned char bytes[32]{};
    return length<=sizeof(bytes) && payload::detail::safe_read(reinterpret_cast<void*>(address),bytes,length)
        && std::memcmp(bytes,expected,length)==0;
}

// Return a field without inserting new keys into a borrowed native map.
std::string Field(const Fields& row,const char* name) {
    const auto found=row.find(name);
    return found==row.end()?std::string{}:found->second;
}

// Parse opaque numeric IDs and timestamps without floating-point precision loss.
std::int64_t Integer(const std::string& value) noexcept {
    std::int64_t result=0;
    const auto parsed=std::from_chars(value.data(),value.data()+value.size(),result);
    return parsed.ec==std::errc{} && parsed.ptr==value.data()+value.size()?result:0;
}

// Copy bounded strings into this DLL's allocations before native callback data expires.
Fields CopyFields(const Fields& input,std::size_t& bytes) {
    if(input.size()>64) throw std::runtime_error("field_limit");
    Fields result;
    for(const auto& item:input) {
        if(item.first.size()>128 || item.second.size()>kMaxValue) throw std::runtime_error("value_limit");
        bytes+=item.first.size()+item.second.size();
        if(bytes>kMaxBatch) throw std::runtime_error("batch_limit");
        result.emplace(item.first,item.second);
    }
    return result;
}

// Copy a native vector only after checking its element count and aggregate byte bound.
Rows CopyRows(const Rows& input,std::size_t maximum,std::size_t& bytes) {
    if(input.size()>maximum) throw std::runtime_error("row_limit");
    Rows result; result.reserve(input.size());
    for(const auto& row:input) result.push_back(CopyFields(row,bytes));
    return result;
}

struct Pending {
    std::mutex mutex;
    std::condition_variable ready;
    bool done=false,failed=false;
    Rows rows;
    Fields metadata;

    // Detach a successful query result while the product's callback references are valid.
    void Complete(const Rows& source,const Fields& extra) noexcept {
        std::lock_guard<std::mutex> lock(mutex);
        if(done) return;
        try { std::size_t bytes=0; rows=CopyRows(source,100,bytes); metadata=CopyFields(extra,bytes); }
        catch(...) { failed=true; }
        done=true; ready.notify_all();
    }

    // Retain only a fixed failure flag, never log native error strings or message bodies.
    void Fail() noexcept {
        std::lock_guard<std::mutex> lock(mutex);
        if(done) return;
        failed=true;done=true;ready.notify_all();
    }
};

// Grow the native list request until exhausted; release each result using its own destructor.
Rows Conversations(std::uintptr_t base,void* context,Active active) {
    const auto destroy=reinterpret_cast<Destroy>(base+kDestroy);
    int maximum=500;
    while(active(context)) {
        alignas(8) std::array<unsigned char,sizeof(Rows)> storage{};
        Rows result;
        try {
            reinterpret_cast<Latest>(base+kLatest)(nullptr,storage.data(),maximum);
            const auto& source=*reinterpret_cast<const Rows*>(storage.data());
            if(source.size()>static_cast<std::size_t>(maximum))throw std::runtime_error("conversation_result_invalid");
            result.reserve(source.size());
            // Bound each copied record, not the total number of conversations.
            for(const auto& row:source) {
                if(!active(context))break;
                std::size_t bytes=0;result.push_back(CopyFields(row,bytes));
            }
        } catch(...) { destroy(storage.data()); throw; }
        destroy(storage.data());
        if(result.size()<static_cast<std::size_t>(maximum))return result;
        if(maximum==std::numeric_limits<int>::max())throw std::runtime_error("native_list_capacity");
        maximum=maximum>std::numeric_limits<int>::max()/2?std::numeric_limits<int>::max():maximum*2;
    }
    return {};
}

// Invoke the client's existing asynchronous query; callback ownership outlives a timeout safely.
std::shared_ptr<Pending> Page(std::uintptr_t base,const std::string& cid,std::int64_t cursor,
                             void* context,Active active) {
    auto pending=std::make_shared<Pending>();
    // MSVC std::function copies dispatch through this DLL's clone/delete virtual methods.
    Success success=[pending](const Rows& rows,const Fields& extra){pending->Complete(rows,extra);};
    Failure failure=[pending](const std::string&){pending->Fail();};
    reinterpret_cast<Query>(base+kQuery)(nullptr,cid,100,cursor,std::move(success),std::move(failure));
    const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(8);
    std::unique_lock<std::mutex> lock(pending->mutex);
    while(!pending->done && active(context) && std::chrono::steady_clock::now()<deadline)
        pending->ready.wait_for(lock,std::chrono::milliseconds(100));
    if(!pending->done) { pending->failed=true; pending->done=true; }
    return pending;
}

// Escape strings for the private JSONL pipe and enforce the existing frame ceiling.
void String(std::string& out,const std::string& value) {
    static constexpr char hex[]="0123456789abcdef";
    out+='"';
    for(unsigned char ch:value) {
        if(ch=='"'||ch=='\\') {out+='\\';out+=static_cast<char>(ch);}
        else if(ch<32) {out+="\\u00";out+=hex[ch>>4];out+=hex[ch&15];}
        else out+=static_cast<char>(ch);
        if(out.size()>kMaxFrame) throw std::runtime_error("frame_limit");
    }
    out+='"';
}

// Encode an all-type message using its native fields, without discarding attachment metadata.
std::string Frame(const BootstrapRequest& request,std::int64_t uid,const Fields& conversation,const Fields& row) {
    std::string out="{\"schema\":\"dingtalkbridge.native-history.v1\",\"collector_session_id\":";
    String(out,request.collector_session_id);out+=",\"account_id\":";String(out,std::to_string(uid));
    out+=",\"conversation_title\":";
    auto title=Field(conversation,"title");if(title.empty())title=Field(conversation,"alias");String(out,title);
    out+=",\"fields\":{";
    bool first=true;
    for(const auto& item:row) {if(!first)out+=',';first=false;String(out,item.first);out+=':';String(out,item.second);}
    out+="}}\n";
    if(out.size()>kMaxFrame) throw std::runtime_error("frame_limit");
    return out;
}

// Keep polling sleeps interruptible so Stop-Native can finish promptly.
void Delay(void* context,Active active,unsigned milliseconds) noexcept {
    for(unsigned elapsed=0;elapsed<milliseconds && active(context);elapsed+=100) Sleep(100);
}
}

// Only follow the proven current-user pointer, vtable and two scalar fields; no credentials are read.
bool ReadAccountUid(std::uintptr_t base,std::int64_t& uid) noexcept {
    constexpr unsigned char initialized[]={0x0f,0xb6,0x41,0x08,0xc3};
    constexpr unsigned char getter[]={0x48,0x8b,0x81,0x10,0x03,0x00,0x00,0xc3};
    if(!Matches(base+0x128c810,initialized,sizeof(initialized)) || !Matches(base+0x3406a10,getter,sizeof(getter))) return false;
    std::uintptr_t object=0,vtable=0;unsigned char ready=0;uid=0;
    return payload::detail::safe_read(reinterpret_cast<void*>(base+0xa1e7de8),&object,sizeof(object)) && object>1
        && payload::detail::safe_read(reinterpret_cast<void*>(object),&vtable,sizeof(vtable)) && vtable==base+0x86aef20
        && payload::detail::safe_read(reinterpret_cast<void*>(object+8),&ready,1) && ready!=0
        && payload::detail::safe_read(reinterpret_cast<void*>(object+0x310),&uid,sizeof(uid)) && uid>0;
}

// Use the message service's media downloader so protected image bytes stay in the logged-in client.
void DownloadMedia(std::uintptr_t base,const std::string& cid,std::int64_t mid,
                   const std::filesystem::path& target,const std::filesystem::path& failure,std::int64_t account,
                   std::int64_t timestamp,void* context,Active active) {
    constexpr unsigned char service_entry[]={0x48,0x89,0x5c,0x24,0x10,0x48,0x89,0x6c,0x24,0x18,0x48,0x89,0x74,0x24,0x20,0x57,0x48,0x83,0xec,0x70,0x48,0x8b,0x05,0x3d,0x6b,0x51,0x06,0x48,0x33,0xc4,0x48,0x89};
    constexpr unsigned char message_entry[]={0x40,0x55,0x53,0x56,0x57,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,0x48,0x8d,0xac,0x24,0x38,0xfe,0xff,0xff,0x48,0x81,0xec,0xc8,0x02,0x00,0x00,0x48,0x8b,0x05,0x15};
    constexpr unsigned char media_entry[]={0x48,0x89,0x5c,0x24,0x08,0x55,0x56,0x57,0x41,0x54,0x41,0x55,0x41,0x56,0x41,0x57,0x48,0x8d,0xac,0x24,0x80,0xf5,0xff,0xff,0x48,0x81,0xec,0x80,0x0b,0x00,0x00,0x48};
    if(!Matches(base+0x36ff5c0,service_entry,32)||!Matches(base+0x3759fe0,message_entry,32)||!Matches(base+0x3afa340,media_entry,32))
        throw std::runtime_error("media_layout_unsupported");
    using Shared=std::shared_ptr<void>;
    struct OpaqueError {unsigned char unused;};
    using MediaSuccess=std::function<void(const Shared&,const std::string&,const std::string&)>;
    using MediaFailure=std::function<void(const OpaqueError&)>;
    using GetService=void* (__fastcall *)(Shared*);
    using GetConversation=void* (__fastcall *)(void*,Shared*,const std::string&);
    using GetMessage=void* (__fastcall *)(void*,Shared*,const std::string&,std::int64_t);
    using Download=void (__fastcall *)(void*,Shared,bool,const std::string&,const MediaSuccess&,const MediaFailure&);
    static_assert(sizeof(Shared)==16 && sizeof(MediaSuccess)==64 && sizeof(MediaFailure)==64,"MSVC release callback ABI");
    Shared service,conversation,message;
    reinterpret_cast<GetService>(base+0x36ff5c0)(&service);
    if(!service)throw std::runtime_error("service_unavailable");
    auto table=*reinterpret_cast<void***>(service.get());
    reinterpret_cast<GetConversation>(table[0x680/sizeof(void*)])(service.get(),&conversation,cid);
    if(conversation)reinterpret_cast<GetMessage>(base+0x3759fe0)(conversation.get(),&message,cid,mid);
    if(!message) {
        // Historical queries also populate the client's message cache for an older media request.
        auto page=Page(base,cid,timestamp>0?timestamp+1:0,context,active);
        std::int64_t current=0;
        if(page->failed||!ReadAccountUid(base,current)||current!=account)throw std::runtime_error("message_query_failed");
        conversation.reset();
        reinterpret_cast<GetConversation>(table[0x680/sizeof(void*)])(service.get(),&conversation,cid);
        if(conversation)reinterpret_cast<GetMessage>(base+0x3759fe0)(conversation.get(),&message,cid,mid);
    }
    if(!message)throw std::runtime_error("message_unavailable");
    MediaFailure failed=[failure](const OpaqueError&) {
        try {std::ofstream result(failure);result<<"media_download_failed\n";}catch(...){}
    };
    MediaSuccess succeeded=[base,account,target,failed](const Shared&,const std::string&,const std::string& path) {
        try {
            std::int64_t current=0;
            if(!ReadAccountUid(base,current)||current!=account)throw std::runtime_error("account_changed");
            const auto source=std::filesystem::u8path(path);
            if(!source.is_absolute()||!std::filesystem::is_regular_file(source))throw std::runtime_error("media_path_invalid");
            std::filesystem::copy_file(source,target,std::filesystem::copy_options::overwrite_existing);
        } catch(...) {OpaqueError error{};failed(error);}
    };
    // The client maps false to the original-media variant (1), true to the preview variant (2).
    reinterpret_cast<Download>(base+0x3afa340)(service.get(),message,false,std::string{},succeeded,failed);
}

// Invoke the existing file-download binding with a native JSON list and native callbacks.
// Keeping construction/destruction inside dtfbase avoids guessing private request object layouts.
void DownloadRequests(std::uintptr_t base,const BootstrapRequest& request,std::int64_t account,void* context,Active active) noexcept {
    if(!request.attachment_request_directory[0])return;
    constexpr unsigned char entry[]={0x48,0x89,0x5c,0x24,0x18,0x55,0x56,0x57,0x41,0x56,0x41,0x57,0x48,0x8d,0xac,0x24,0x20,0xfc,0xff,0xff,0x48,0x81,0xec,0xe0,0x04,0x00,0x00,0x48,0x8b,0x05,0x66,0x45};
    using JsonRead=void* (__fastcall *)(void*,const void*);
    using Binding=void (__fastcall *)(const std::string&,const void*);
    struct StringPiece {const char* data;std::size_t length;};
    try {
        const std::filesystem::path directory(request.attachment_request_directory);
        if(!directory.is_absolute()||!std::filesystem::is_directory(directory))return;
        for(const auto& item:std::filesystem::directory_iterator(directory)) {
            if(item.path().extension()!=L".request"||!item.is_regular_file())continue;
            auto outcome=item.path();outcome.replace_extension(L".error");
            void* value=nullptr;
            try {
                if(item.file_size()>32768)throw std::runtime_error("request_size");
                std::ifstream source(item.path(),std::ios::binary);
                std::string uid,json;std::getline(source,uid);std::getline(source,json);
                std::int64_t current=0;
                if(Integer(uid)!=account||!ReadAccountUid(base,current)||current!=account)throw std::runtime_error("account_changed");
                if(json=="media") {
                    std::string cid,mid,path,timestamp;std::getline(source,cid);std::getline(source,mid);std::getline(source,path);std::getline(source,timestamp);
                    const auto target=std::filesystem::u8path(path);
                    if(cid.empty()||Integer(mid)<=0||!target.is_absolute())throw std::runtime_error("media_request_invalid");
                    DownloadMedia(base,cid,Integer(mid),target,outcome,account,Integer(timestamp),context,active);
                } else {
                    if(!Matches(base+0x61b1b90,entry,sizeof(entry)))throw std::runtime_error("layout_unsupported");
                    HMODULE module=GetModuleHandleW(L"dtfbase.dll");
                    auto read=reinterpret_cast<JsonRead>(GetProcAddress(module,"?Read@JSONReader@base@@SA?AV?$scoped_ptr@VValue@base@@U?$default_delete@VValue@base@@@std@@@@AEBV?$BasicStringPiece@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@2@@Z"));
                    if(!read||json.empty())throw std::runtime_error("json_reader_unavailable");
                    StringPiece piece{json.data(),json.size()};read(&value,&piece);
                    if(!value)throw std::runtime_error("request_invalid");
                    const std::string callback="dingtalkbridge_attachment_"+item.path().stem().string();
                    reinterpret_cast<Binding>(base+0x61b1b90)(callback,value);
                }
                source.close();
                outcome.replace_extension(L".started");
            } catch(...) {}
            if(value) {
                auto table=*reinterpret_cast<void***>(value);
                reinterpret_cast<void (__fastcall *)(void*,unsigned)>(table[0])(value,1);
            }
            std::error_code error;std::filesystem::rename(item.path(),outcome,error);
            if(error&&std::filesystem::exists(outcome))std::filesystem::remove(item.path(),error);
        }
    } catch(...) {}
}

// Round-robin one page per conversation so large histories cannot monopolize the first pass.
void Run(std::uintptr_t base,const BootstrapRequest& request,SharedState* state,
         void* context,Active active,Emit emit) noexcept {
    InterlockedExchange(&state->history_running,1);
    try {
        if(!Matches(base+kLatest,kLatestBytes,32) || !Matches(base+kQuery,kQueryBytes,32) || !Matches(base+kDestroy,kDestroyBytes,32)) {
            InterlockedExchange(&state->history_stage,1);
            InterlockedExchange(&state->history_running,0);return;
        }
        std::int64_t account=0;
        while(active(context) && !ReadAccountUid(base,account)) {
            InterlockedExchange(&state->history_stage,2);Delay(context,active,1000);
        }
        std::map<std::string,std::string> completed;
        while(active(context)) {
            std::int64_t current=0;
            if(!ReadAccountUid(base,current)||current!=account) {InterlockedExchange(&state->history_stage,7);break;}
            InterlockedExchange(&state->history_stage,3);
            DownloadRequests(base,request,account,context,active);
            auto conversations=Conversations(base,context,active);
            InterlockedExchange64(&state->history_conversations,static_cast<LONG64>(conversations.size()));
            struct Scan {
                Fields conversation;
                std::string previous_mid;
                std::int64_t cursor=0;
                std::set<std::int64_t> cursors;
            };
            std::deque<Scan> pending;
            for(const auto& conversation:conversations) {
                if(!active(context))break;
                const auto cid=Field(conversation,"cid"),last=Field(conversation,"lastMid");
                if(cid.empty()||cid.size()>4096)continue;
                const auto previous=completed.find(cid);
                if(previous!=completed.end() && previous->second==last)continue;
                pending.push_back({conversation,previous==completed.end()?std::string{}:previous->second,0,{}});
            }
            while(active(context) && !pending.empty()) {
                auto scan=std::move(pending.front());
                pending.pop_front();
                const auto cid=Field(scan.conversation,"cid"),last=Field(scan.conversation,"lastMid");
                DownloadRequests(base,request,account,context,active);
                if(!ReadAccountUid(base,current)||current!=account) {InterlockedExchange(&state->history_stage,7);throw std::runtime_error("account_changed");}
                InterlockedExchange(&state->history_stage,4);
                auto page=Page(base,cid,scan.cursor,context,active);
                if(!active(context))break;
                if(page->failed) {InterlockedIncrement64(&state->history_errors);continue;}
                InterlockedIncrement64(&state->history_pages);
                bool known=false;
                for(const auto& row:page->rows) {
                    if(!active(context))break;
                    if(!ReadAccountUid(base,current)||current!=account) {InterlockedExchange(&state->history_stage,7);throw std::runtime_error("account_changed");}
                    if(Field(row,"cid")!=cid || Field(row,"mid").empty())continue;
                    if(!scan.previous_mid.empty() && Field(row,"mid")==scan.previous_mid)known=true;
                    if(!emit(context,Frame(request,account,scan.conversation,row)))throw std::runtime_error("delivery_stopped");
                    InterlockedIncrement64(&state->history_messages);
                }
                if(!active(context))break;
                auto next=Integer(Field(page->metadata,"next_cursor"));
                if(known||page->rows.empty()||next<=0||(next==scan.cursor && next<=1)) {
                    completed[cid]=last;
                    continue;
                }
                // Preserve the client's millisecond-boundary correction and loop detection.
                if(next==scan.cursor)--next;
                if((scan.cursor>0 && next>scan.cursor)||!scan.cursors.insert(next).second) {
                    InterlockedIncrement64(&state->history_errors);
                    continue;
                }
                scan.cursor=next;
                pending.push_back(std::move(scan));
            }
            InterlockedExchange(&state->history_stage,5);
            for(unsigned elapsed=0;elapsed<request.history_interval_seconds*1000 && active(context);elapsed+=200) {
                DownloadRequests(base,request,account,context,active);
                Delay(context,active,200);
            }
        }
        if(InterlockedCompareExchange(&state->history_stage,0,0)!=7) InterlockedExchange(&state->history_stage,8);
    } catch(...) {
        InterlockedIncrement64(&state->history_errors);
        if(InterlockedCompareExchange(&state->history_stage,0,0)!=7)InterlockedExchange(&state->history_stage,6);
    }
    InterlockedExchange(&state->history_running,0);
}
}
