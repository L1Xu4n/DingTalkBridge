#define NOMINMAX
#include "HistoryReader.h"
#include "../payload/safe_read.h"
#include <array>
#include <charconv>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <functional>
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

// Retrieve owned conversation summaries, releasing native allocations with their native destructor.
Rows Conversations(std::uintptr_t base,int maximum) {
    alignas(8) std::array<unsigned char,sizeof(Rows)> storage{};
    const auto destroy=reinterpret_cast<Destroy>(base+kDestroy);
    try {
        reinterpret_cast<Latest>(base+kLatest)(nullptr,storage.data(),maximum);
        std::size_t bytes=0;
        Rows result=CopyRows(*reinterpret_cast<const Rows*>(storage.data()),maximum,bytes);
        destroy(storage.data());
        return result;
    } catch(...) { destroy(storage.data()); throw; }
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

// Compare archive retention against the timestamp representation returned by the native service.
std::int64_t Milliseconds(const std::string& value) noexcept {
    auto number=Integer(value);
    if(number<=0)return 0;
    return number>=100000000000LL?number:number*1000;
}

// Get Unix milliseconds for a retention cutoff, without using the machine's local timezone.
std::int64_t Now() noexcept {
    FILETIME file{};GetSystemTimeAsFileTime(&file);
    const auto ticks=(static_cast<std::uint64_t>(file.dwHighDateTime)<<32)|file.dwLowDateTime;
    return static_cast<std::int64_t>((ticks-116444736000000000ULL)/10000);
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

// Backfill each listed conversation, then query changed conversations on subsequent passes.
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
            auto conversations=Conversations(base,static_cast<int>(request.history_max_conversations));
            InterlockedExchange64(&state->history_conversations,static_cast<LONG64>(conversations.size()));
            if(conversations.size()>=request.history_max_conversations) InterlockedIncrement64(&state->history_truncated);
            for(const auto& conversation:conversations) {
                if(!active(context))break;
                const auto cid=Field(conversation,"cid"),last=Field(conversation,"lastMid");
                if(cid.empty()||cid.size()>4096)continue;
                const auto previous=completed.find(cid);
                if(previous!=completed.end() && previous->second==last)continue;
                const auto previous_mid=previous==completed.end()?std::string{}:previous->second;
                std::int64_t cursor=0;unsigned count=0,pages=0;
                std::set<std::int64_t> cursors;
                const auto cutoff=Now()-static_cast<std::int64_t>(request.history_days)*86400000;
                while(active(context) && count<request.history_max_messages && pages<(request.history_max_messages+99)/100) {
                    ++pages;
                    if(!ReadAccountUid(base,current)||current!=account) {InterlockedExchange(&state->history_stage,7);throw std::runtime_error("account_changed");}
                    InterlockedExchange(&state->history_stage,4);
                    auto page=Page(base,cid,cursor,context,active);
                    if(!active(context))break;
                    if(page->failed) {InterlockedIncrement64(&state->history_errors);break;}
                    InterlockedIncrement64(&state->history_pages);
                    bool old=false,known=false;
                    for(const auto& row:page->rows) {
                        if(!active(context)||count>=request.history_max_messages)break;
                        if(!ReadAccountUid(base,current)||current!=account) {InterlockedExchange(&state->history_stage,7);throw std::runtime_error("account_changed");}
                        if(Field(row,"cid")!=cid || Field(row,"mid").empty())continue;
                        if(!previous_mid.empty() && Field(row,"mid")==previous_mid)known=true;
                        const auto stamp=Milliseconds(Field(row,"create_time"));
                        if(stamp && stamp<cutoff) {old=true;continue;}
                        if(!emit(context,Frame(request,account,conversation,row)))throw std::runtime_error("delivery_stopped");
                        ++count;InterlockedIncrement64(&state->history_messages);
                    }
                    const auto next=Integer(Field(page->metadata,"next_cursor"));
                    if(old||known||page->rows.size()<100||next<=0||next==cursor||!cursors.insert(next).second)break;
                    cursor=next;
                }
                if(count>=request.history_max_messages)InterlockedIncrement64(&state->history_truncated);
                completed[cid]=last;
            }
            InterlockedExchange(&state->history_stage,5);
            Delay(context,active,request.history_interval_seconds*1000);
        }
        if(InterlockedCompareExchange(&state->history_stage,0,0)!=7) InterlockedExchange(&state->history_stage,8);
    } catch(...) {
        InterlockedIncrement64(&state->history_errors);
        if(InterlockedCompareExchange(&state->history_stage,0,0)!=7)InterlockedExchange(&state->history_stage,6);
    }
    InterlockedExchange(&state->history_running,0);
}
}
