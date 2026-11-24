#define NOMINMAX
#include "HistoryReader.h"
#include "NativeFiles.h"
#include "../payload/safe_read.h"
#include <array>
#include <charconv>
#include <chrono>
#include <condition_variable>
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
constexpr std::size_t kMaxFrame=512*1024, kMaxValue=128*1024, kMaxBatch=8*1024*1024;

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
Rows Conversations(std::uintptr_t base,const HistoryLayout& layout,void* context,Active active) {
    const auto latest=reinterpret_cast<Latest>(base+layout.latest.rva);
    const auto destroy=reinterpret_cast<Destroy>(base+layout.destroy.rva);
    int maximum=500;
    while(active(context)) {
        alignas(8) std::array<unsigned char,sizeof(Rows)> storage{};
        Rows result;
        try {
            latest(nullptr,storage.data(),maximum);
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
std::shared_ptr<Pending> Page(std::uintptr_t base,const HistoryLayout& layout,const std::string& cid,std::int64_t cursor,
                             void* context,Active active) {
    auto pending=std::make_shared<Pending>();
    // MSVC std::function copies dispatch through this DLL's clone/delete virtual methods.
    Success success=[pending](const Rows& rows,const Fields& extra){pending->Complete(rows,extra);};
    Failure failure=[pending](const std::string&){pending->Fail();};
    reinterpret_cast<Query>(base+layout.query.rva)(nullptr,cid,100,cursor,std::move(success),std::move(failure));
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
bool ReadAccountUid(std::uintptr_t base,const AccountLayout& layout,std::int64_t& uid) noexcept {
    if(!MatchesImageEntry(base,layout.initialized) || !MatchesImageEntry(base,layout.uid_getter)) return false;
    std::uintptr_t object=0,vtable=0;unsigned char ready=0;uid=0;
    return payload::detail::safe_read(reinterpret_cast<void*>(base+layout.singleton),&object,sizeof(object)) && object>1
        && payload::detail::safe_read(reinterpret_cast<void*>(object),&vtable,sizeof(vtable)) && vtable==base+layout.vtable
        && payload::detail::safe_read(reinterpret_cast<void*>(object+8),&ready,1) && ready!=0
        && payload::detail::safe_read(reinterpret_cast<void*>(object+0x310),&uid,sizeof(uid)) && uid>0;
}

// Use the message service's media downloader so protected image bytes stay in the logged-in client.
void DownloadMedia(std::uintptr_t base,const ClientLayout& layout,const std::string& cid,std::int64_t mid,
                   const std::filesystem::path& target,const std::filesystem::path& failure,std::int64_t account,
                   std::int64_t timestamp,void* context,Active active) {
    const auto& entries=layout.attachments;
    if(!MatchesImageEntry(base,entries.media_service) || !MatchesImageEntry(base,entries.media_message) ||
       !MatchesImageEntry(base,entries.media_download)) throw AttachmentLayoutError::Media;
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
    reinterpret_cast<GetService>(base+entries.media_service.rva)(&service);
    if(!service)throw std::runtime_error("service_unavailable");
    // Follow the versioned conversation slot only within the selected image and its code pages.
    const auto lookup=[&] {
        std::uintptr_t table=0,lookup_entry=0;
        if(!payload::detail::safe_read(service.get(),&table,sizeof(table))) throw AttachmentLayoutError::Media;
        const auto slot=table+entries.media_conversation_slot;
        if(!ImageContains(base,slot,sizeof(lookup_entry)) ||
           !payload::detail::safe_read(reinterpret_cast<const void*>(slot),&lookup_entry,sizeof(lookup_entry)) ||
           !ImageContains(base,lookup_entry,1,true)) throw AttachmentLayoutError::Media;
        conversation.reset();
        reinterpret_cast<GetConversation>(lookup_entry)(service.get(),&conversation,cid);
        if(conversation)
            reinterpret_cast<GetMessage>(base+entries.media_message.rva)(conversation.get(),&message,cid,mid);
    };
    lookup();
    if(!message) {
        // Historical queries also populate the client's message cache for an older media request.
        if(!MatchesImageEntry(base,layout.history.query)) throw AttachmentLayoutError::Media;
        const auto cursor=timestamp>0 && timestamp<std::numeric_limits<std::int64_t>::max()?timestamp+1:timestamp;
        auto page=Page(base,layout.history,cid,cursor>0?cursor:0,context,active);
        std::int64_t current=0;
        if(!active(context)||page->failed||!ReadAccountUid(base,layout.account,current)||current!=account)
            throw std::runtime_error("message_query_failed");
        lookup();
    }
    if(!message)throw std::runtime_error("message_unavailable");
    MediaFailure failed=[failure](const OpaqueError&) {
        try {std::ofstream result(failure);result<<"media_download_failed\n";}catch(...){}
    };
    MediaSuccess succeeded=[base,account_layout=layout.account,account,target,failed](const Shared&,const std::string&,const std::string& path) {
        // Validate the same account again before publishing the downloaded file.
        try {
            std::int64_t current=0;
            if(!ReadAccountUid(base,account_layout,current)||current!=account)throw std::runtime_error("account_changed");
            const auto source=std::filesystem::u8path(path);
            if(!source.is_absolute()||!std::filesystem::is_regular_file(source))throw std::runtime_error("media_path_invalid");
            std::filesystem::copy_file(source,target,std::filesystem::copy_options::overwrite_existing);
        } catch(...) {OpaqueError error{};failed(error);}
    };
    // The client maps false to the original-media variant (1), true to the preview variant (2).
    reinterpret_cast<Download>(base+entries.media_download.rva)(service.get(),message,false,std::string{},succeeded,failed);
}

// Atomically publish one of the fixed compatibility errors before removing the serviced request.
void RejectUnsupportedRequest(const std::filesystem::path& request,const std::filesystem::path& outcome,
                              AttachmentLayoutError category) {
    const char* code;
    switch(category) {
        case AttachmentLayoutError::Document: code="attachment_document_layout_unsupported"; break;
        case AttachmentLayoutError::Media: code="attachment_media_layout_unsupported"; break;
        case AttachmentLayoutError::InlineMedia: code="attachment_inline_media_layout_unsupported"; break;
        case AttachmentLayoutError::CloudFile: code="attachment_cloud_file_layout_unsupported"; break;
        case AttachmentLayoutError::GroupFiles: code="group_files_layout_unsupported"; break;
        default: return;
    }
    auto partial=outcome;
    partial+=L".partial";
    std::ofstream stream(partial,std::ios::binary|std::ios::trunc);
    stream<<code<<'\n';
    stream.close();
    std::error_code error;
    if(stream) {
        std::filesystem::rename(partial,outcome,error);
        if(!error) {
            std::filesystem::remove(request,error);
            return;
        }
    }
    std::filesystem::remove(partial,error);
}

// Check the current account and dispatch each request using only its operation's layout.
void DownloadRequests(std::uintptr_t base,const ClientLayout& layout,const BootstrapRequest& request,
                      std::int64_t account,void* context,Active active) noexcept {
    if(!request.attachment_request_directory[0])return;
    using JsonRead=void* (__fastcall *)(void*,const void*);
    using Binding=void (__fastcall *)(const std::string&,const void*);
    struct StringPiece {const char* data;std::size_t length;};
    try {
        const std::filesystem::path directory(request.attachment_request_directory);
        if(!directory.is_absolute()||!std::filesystem::is_directory(directory))return;
        for(const auto& item:std::filesystem::directory_iterator(directory)) {
            if(!active(context))break;
            if(item.path().extension()!=L".request"||!item.is_regular_file())continue;
            auto outcome=item.path();outcome.replace_extension(L".error");
            void* value=nullptr;
            try {
                if(item.file_size()>32768)throw std::runtime_error("request_size");
                std::ifstream source(item.path(),std::ios::binary);
                std::string uid,json;std::getline(source,uid);std::getline(source,json);
                std::int64_t current=0;
                if(Integer(uid)!=account||!ReadAccountUid(base,layout.account,current)||current!=account)throw std::runtime_error("account_changed");
                if(json=="group-space" || json=="list-directory") {
                    std::string body; std::getline(source,body);
                    auto result=item.path(); result.replace_extension(L".result");
                    QueryFiles(base,layout,json,body,result,outcome,account);
                } else if(json=="cloud-file") {
                    std::string cid,space,file,path; std::getline(source,cid);std::getline(source,space);std::getline(source,file);std::getline(source,path);
                    const auto target=std::filesystem::u8path(path);
                    if(cid.empty()||space.empty()||file.empty()||!target.is_absolute())throw std::runtime_error("cloud_file_request_invalid");
                    DownloadCloudFile(base,layout,cid,space,file,target,outcome,account);
                } else if(json=="inline-media") {
                    std::string media,path; std::getline(source,media); std::getline(source,path);
                    const auto target=std::filesystem::u8path(path);
                    if(!target.is_absolute())throw std::runtime_error("media_path_invalid");
                    DownloadInlineImage(base,layout,media,target,outcome,account);
                } else if(json=="media") {
                    std::string cid,mid,path,timestamp;std::getline(source,cid);std::getline(source,mid);std::getline(source,path);std::getline(source,timestamp);
                    const auto target=std::filesystem::u8path(path);
                    if(cid.empty()||Integer(mid)<=0||!target.is_absolute())throw std::runtime_error("media_request_invalid");
                    DownloadMedia(base,layout,cid,Integer(mid),target,outcome,account,Integer(timestamp),context,active);
                } else {
                    const auto& document=layout.attachments.document;
                    if(!MatchesImageEntry(base,document)) throw AttachmentLayoutError::Document;
                    HMODULE module=GetModuleHandleW(L"dtfbase.dll");
                    auto read=reinterpret_cast<JsonRead>(module ? GetProcAddress(module,"?Read@JSONReader@base@@SA?AV?$scoped_ptr@VValue@base@@U?$default_delete@VValue@base@@@std@@@@AEBV?$BasicStringPiece@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@2@@Z") : nullptr);
                    if(!read || !ImageContains(reinterpret_cast<std::uintptr_t>(module),
                                              reinterpret_cast<std::uintptr_t>(read),1,true))
                        throw AttachmentLayoutError::Document;
                    if(json.empty())throw std::runtime_error("json_reader_unavailable");
                    StringPiece piece{json.data(),json.size()};read(&value,&piece);
                    if(!value)throw std::runtime_error("request_invalid");
                    const std::string callback="dingtalkbridge_attachment_"+item.path().stem().string();
                    reinterpret_cast<Binding>(base+document.rva)(callback,value);
                }
                source.close();
                outcome.replace_extension(L".started");
            } catch(AttachmentLayoutError category) {
                RejectUnsupportedRequest(item.path(),outcome,category);
                continue;
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

// Poll attachments independently of history compatibility and round-robin supported history pages.
void Run(std::uintptr_t base,const ClientLayout& layout,const BootstrapRequest& request,SharedState* state,
         void* context,Active active,Emit emit) noexcept {
    InterlockedExchange(&state->history_running,1);
    try {
        const bool history_supported=MatchesImageEntry(base,layout.history.latest) &&
            MatchesImageEntry(base,layout.history.query) && MatchesImageEntry(base,layout.history.destroy);
        if(!history_supported) InterlockedExchange(&state->history_stage,1);
        std::int64_t account=0;
        const bool service_requests=request.attachment_request_directory[0]!=0;
        while(active(context) && (history_supported || service_requests) && !ReadAccountUid(base,layout.account,account)) {
            if(history_supported) InterlockedExchange(&state->history_stage,2);
            Delay(context,active,1000);
        }
        std::map<std::string,std::string> completed;
        while(active(context) && (history_supported || service_requests)) {
            std::int64_t current=0;
            if(!ReadAccountUid(base,layout.account,current)||current!=account) {InterlockedExchange(&state->history_stage,7);break;}
            if(history_supported) InterlockedExchange(&state->history_stage,3);
            DownloadRequests(base,layout,request,account,context,active);
            if(!history_supported) {
                Delay(context,active,200);
                continue;
            }
            auto conversations=Conversations(base,layout.history,context,active);
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
                DownloadRequests(base,layout,request,account,context,active);
                if(!ReadAccountUid(base,layout.account,current)||current!=account) {InterlockedExchange(&state->history_stage,7);throw std::runtime_error("account_changed");}
                InterlockedExchange(&state->history_stage,4);
                auto page=Page(base,layout.history,cid,scan.cursor,context,active);
                if(!active(context))break;
                if(page->failed) {InterlockedIncrement64(&state->history_errors);continue;}
                InterlockedIncrement64(&state->history_pages);
                bool known=false;
                for(const auto& row:page->rows) {
                    if(!active(context))break;
                    if(!ReadAccountUid(base,layout.account,current)||current!=account) {InterlockedExchange(&state->history_stage,7);throw std::runtime_error("account_changed");}
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
                DownloadRequests(base,layout,request,account,context,active);
                Delay(context,active,200);
            }
        }
        if(history_supported && InterlockedCompareExchange(&state->history_stage,0,0)!=7)
            InterlockedExchange(&state->history_stage,8);
    } catch(...) {
        InterlockedIncrement64(&state->history_errors);
        if(InterlockedCompareExchange(&state->history_stage,0,0)!=7)InterlockedExchange(&state->history_stage,6);
    }
    InterlockedExchange(&state->history_running,0);
}
}
