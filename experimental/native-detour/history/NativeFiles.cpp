#define NOMINMAX
#include "NativeFiles.h"
#include "HistoryReader.h"
#include "../payload/safe_read.h"
#include <array>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>

namespace bridge_native::history {
namespace {
using Shared = std::shared_ptr<void>;
using Setter = void (__fastcall *)(void*, const std::string&);
using Instance = void* (__fastcall *)();
using DeletingDestructor = void* (__fastcall *)(void*, unsigned);
using ResponseBody = const std::string& (__fastcall *)(void*);
constexpr std::size_t kResponseLimit = 4 * 1024 * 1024;
struct RpcTarget {
    std::uintptr_t base;
    AccountLayout account_layout;
    std::int64_t account;
    std::filesystem::path result, failure;
    ResponseBody response_body;
    bool finished = false;
};
std::mutex rpc_mutex;
std::map<void*, RpcTarget> rpc_targets;
std::array<void*, 8> context_table{};
std::once_flag context_table_once;
DeletingDestructor original_context_destructor = nullptr;

// Require each named SDK export to resolve within the loaded SDK image.
template<class Function>
Function Export(std::uintptr_t sdk, const char* name, AttachmentLayoutError category, bool executable = true) {
    auto address = sdk ? GetProcAddress(reinterpret_cast<HMODULE>(sdk), name) : nullptr;
    if (!address || !ImageContains(sdk, reinterpret_cast<std::uintptr_t>(address), 1, executable))
        throw category;
    return reinterpret_cast<Function>(address);
}

// Persist only a fixed diagnostic, keeping native response details inside the private transport.
void Fail(const std::filesystem::path& path) noexcept {
    try { std::ofstream stream(path, std::ios::binary); stream << "native_file_request_failed\n"; }
    catch (...) {}
}

// Copy response bytes before callback arguments expire and publish a complete file atomically.
void RpcSuccess(void* context, Shared, Shared response) noexcept {
    std::lock_guard<std::mutex> lock(rpc_mutex);
    auto found = rpc_targets.find(context);
    if (found == rpc_targets.end() || found->second.finished) return;
    auto& target = found->second;
    target.finished = true;
    try {
        std::int64_t current = 0;
        if (!response || !ReadAccountUid(target.base, target.account_layout, current) || current != target.account)
            throw std::runtime_error("account_changed");
        const auto& bytes = target.response_body(response.get());
        if (bytes.empty() || bytes.size() > kResponseLimit) throw std::runtime_error("response_size");
        auto partial = target.result; partial += L".partial";
        std::ofstream stream(partial, std::ios::binary | std::ios::trunc);
        stream.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
        stream.close();
        if (!stream) throw std::runtime_error("response_write_failed");
        std::filesystem::rename(partial, target.result);
    } catch (...) { Fail(target.failure); }
}

// Complete a failed RPC promptly without exposing server errors or authentication data.
void RpcFailure(void* context, Shared, Shared) noexcept {
    std::lock_guard<std::mutex> lock(rpc_mutex);
    auto found = rpc_targets.find(context);
    if (found == rpc_targets.end() || found->second.finished) return;
    found->second.finished = true;
    Fail(found->second.failure);
}

// Remove callback state when the SDK releases its last reference, then use its own destructor.
void* DestroyContext(void* context, unsigned flags) noexcept {
    {
        std::lock_guard<std::mutex> lock(rpc_mutex);
        auto found = rpc_targets.find(context);
        if (found != rpc_targets.end()) {
            if (!found->second.finished) Fail(found->second.failure);
            rpc_targets.erase(found);
        }
    }
    return original_context_destructor(context, flags);
}

// Own an SDK object with its scalar deleting destructor and the release MSVC shared-pointer ABI.
Shared Own(void* object) {
    return Shared(object, [](void* value) {
        // Let the SDK destroy its fields and free the object using the matching allocator.
        auto table = *reinterpret_cast<void***>(value);
        reinterpret_cast<DeletingDestructor>(table[0])(value, 1);
    });
}

// Decode the bounded hexadecimal body produced by the local CSpace client.
std::string Unhex(const std::string& input) {
    if (input.size() > 16384 || input.size() % 2) throw std::runtime_error("request_body_invalid");
    std::string result;
    result.reserve(input.size() / 2);
    for (std::size_t index = 0; index < input.size(); index += 2) {
        unsigned value = 0;
        for (std::size_t digit = index; digit < index + 2; ++digit) {
            const auto character = input[digit];
            if (character >= '0' && character <= '9') value = value * 16 + character - '0';
            else if (character >= 'a' && character <= 'f') value = value * 16 + character - 'a' + 10;
            else throw std::runtime_error("request_body_invalid");
        }
        result += static_cast<char>(value);
    }
    return result;
}

// Decode SDK-generated query values before forwarding their authorization metadata internally.
std::string UrlDecode(const std::string& input) {
    std::string result;
    for (std::size_t index = 0; index < input.size(); ++index) {
        if (input[index] == '%' && index + 2 < input.size()) {
            const auto pair = input.substr(index + 1, 2);
            char* end = nullptr;
            const auto value = std::strtoul(pair.c_str(), &end, 16);
            if (end != pair.c_str() + 2) throw std::runtime_error("media_query_invalid");
            result += static_cast<char>(value);
            index += 2;
        } else result += input[index] == '+' ? ' ' : input[index];
    }
    return result;
}
}

// Keep request construction, dispatch and account authentication inside the existing SDK.
void QueryFiles(std::uintptr_t base, const ClientLayout& layout,
                const std::string& operation, const std::string& body_hex,
                const std::filesystem::path& result, const std::filesystem::path& failure,
                std::int64_t account) {
    const std::string endpoint = operation == "group-space" ? "/r/Adaptor/CSpace/getConversationSpace" :
                                 operation == "list-directory" ? "/r/Adaptor/CSpace/listDentryV2" : "";
    if (endpoint.empty()) throw std::runtime_error("file_operation_invalid");
    const auto body = Unhex(body_hex);
    using Constructor = void* (__fastcall *)(void*);
    using RequestConstructor = void* (__fastcall *)(void*, std::string);
    using Wrap = Shared* (__fastcall *)(Shared*, void*);
    using Header = void (__fastcall *)(void*, const std::string&, const std::string&);
    using Send = void (__fastcall *)(void*, Shared, Shared);
    // This helper initializes the SDK context's enable_shared_from_this weak reference.
    const auto& wrap = layout.attachments.group_files_wrap;
    constexpr auto category = AttachmentLayoutError::GroupFiles;
    if (!MatchesImageEntry(base, wrap)) throw category;
    const auto sdk = reinterpret_cast<std::uintptr_t>(GetModuleHandleW(L"libgaea.dll"));
    const auto construct_context = Export<Constructor>(sdk, "??0RequestContext@lwp@wukong@@QEAA@XZ", category);
    const auto native_table = Export<void**>(sdk, "??_7RequestContext@lwp@wukong@@6B@", category, false);
    const FunctionEntry<32> context_entry{reinterpret_cast<std::uintptr_t>(construct_context) - sdk,
        {0x48,0x89,0x4c,0x24,0x08,0x53,0x48,0x83,0xec,0x20,0x48,0x8b,0xd9,0x33,0xc0,0x48,
         0x89,0x81,0xb0,0x02,0x00,0x00,0x48,0x89,0x81,0xb8,0x02,0x00,0x00,0xe8,0x9e,0xca}};
    decltype(context_table) native_slots{};
    const auto table_start = reinterpret_cast<std::uintptr_t>(native_table) - sizeof(void*);
    if (!MatchesImageEntry(sdk, context_entry) || !ImageContains(sdk, table_start, sizeof(native_slots)) ||
        !dingtalkbridge::native::payload::detail::safe_read(
            reinterpret_cast<const void*>(table_start), native_slots.data(), sizeof(native_slots)) ||
        !ImageContains(sdk, reinterpret_cast<std::uintptr_t>(native_slots[1]), 1, true))
        throw category;
    const auto construct_request = Export<RequestConstructor>(sdk, "??0Request@lwp@wukong@@QEAA@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Z", category);
    const auto set_header = Export<Header>(sdk, "?setHeader@Message@lwp@gaea@@QEAAXAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@0@Z", category);
    const auto set_body = Export<Setter>(sdk, "?set_body@Message@lwp@gaea@@QEAAXAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Z", category);
    const auto response_body = Export<ResponseBody>(sdk, "?body@Message@lwp@gaea@@QEAAAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@XZ", category);
    const auto agent_instance = Export<Instance>(sdk, "?instance@UserAgent@lwp@wukong@@SAPEAV123@XZ", category);
    const auto send = Export<Send>(sdk, "?sendRequest@UserAgent@lwp@wukong@@QEAAXV?$shared_ptr@VRequest@lwp@wukong@@@std@@V?$shared_ptr@VRequestContext@lwp@wukong@@@5@@Z", category);
    std::call_once(context_table_once, [&native_slots] {
        // Preserve native RTTI and all SDK adapter slots, overriding only terminal callbacks.
        context_table = native_slots;
        original_context_destructor = reinterpret_cast<DeletingDestructor>(native_slots[1]);
        context_table[1] = reinterpret_cast<void*>(&DestroyContext);
        context_table[5] = reinterpret_cast<void*>(&RpcSuccess);
        context_table[6] = reinterpret_cast<void*>(&RpcSuccess);
        context_table[7] = reinterpret_cast<void*>(&RpcFailure);
    });
    void* raw_context = ::operator new(0x2c0);
    construct_context(raw_context);
    Shared context;
    reinterpret_cast<Wrap>(base + wrap.rva)(&context, raw_context);
    {
        std::lock_guard<std::mutex> lock(rpc_mutex);
        rpc_targets.emplace(raw_context, RpcTarget{base, layout.account, account, result, failure, response_body});
        *reinterpret_cast<void***>(raw_context) = context_table.data() + 1;
    }
    void* raw_request = ::operator new(0x80);
    construct_request(raw_request, endpoint);
    auto request = Own(raw_request);
    set_header(raw_request, "dt", "p");
    set_body(raw_request, body);
    auto agent = agent_instance();
    send(agent, std::move(request), std::move(context));
}

// Reuse the client's authorization-aware media URL builder and asynchronous file service.
void DownloadInlineImage(std::uintptr_t base, const ClientLayout& layout, const std::string& media,
                         const std::filesystem::path& target, const std::filesystem::path& failure,
                         std::int64_t account) {
    if (media.empty() || media.size() > 4096 || (media[0] != '$' && media[0] != '@'))
        throw std::runtime_error("inline_media_invalid");
    constexpr auto category = AttachmentLayoutError::InlineMedia;
    const auto& entry = media[0] == '$' ? layout.attachments.inline_auth_url : layout.attachments.inline_url;
    if (!MatchesImageEntry(base, entry)) throw category;
    const auto sdk = reinterpret_cast<std::uintptr_t>(GetModuleHandleW(L"libgaea.dll"));
    std::string url;
    if (media[0] == '$') {
        using MakeAuthUrl = std::string* (__fastcall *)(std::string*, const std::string&, int, bool, bool);
        reinterpret_cast<MakeAuthUrl>(base + entry.rva)(&url, media, 1, false, false);
    } else {
        using MakeUrl = std::string* (__fastcall *)(std::string*, const std::string&, int, bool, bool, bool, int, const std::string&);
        reinterpret_cast<MakeUrl>(base + entry.rva)(&url, media, 1, false, false, false, 100, "im");
    }
    if (url.rfind("https://", 0) != 0) throw std::runtime_error("inline_media_url_unavailable");
    using Constructor = void* (__fastcall *)(void*);
    const auto construct_task = Export<Constructor>(sdk, "??0FileDownloadTask@file@wukong@@QEAA@XZ", category);
    void* raw = ::operator new(0x118);
    construct_task(raw);
    auto task = Own(raw);
    const auto question = url.find('?');
    std::map<std::string, std::string> auth;
    if (question != std::string::npos) {
        auto position = question + 1;
        while (position < url.size()) {
            const auto end = url.find('&', position);
            const auto pair = url.substr(position, end == std::string::npos ? end : end - position);
            const auto equals = pair.find('=');
            if (equals != std::string::npos) auth.emplace(UrlDecode(pair.substr(0, equals)), UrlDecode(pair.substr(equals + 1)));
            if (end == std::string::npos) break;
            position = end + 1;
        }
    }
    Export<Setter>(sdk, "?setMediaId@FileDownloadTask@file@wukong@@QEAAXAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Z", category)(raw, url.substr(0, question));
    using SetAuth = void (__fastcall *)(void*, const std::map<std::string, std::string>&);
    Export<SetAuth>(sdk, "?set_auth_info@FileDownloadTask@lwp@gaea@@QEAAXAEBV?$map@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@V12@U?$less@V?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@2@V?$allocator@U?$pair@$$CBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@V12@@std@@@2@@std@@@Z", category)(raw, auth);
    const auto biz = auth.find("auth_bizType");
    if (biz != auth.end()) Export<Setter>(sdk, "?set_biz_type@FileDownloadTask@lwp@gaea@@QEAAXAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Z", category)(raw, biz->second);
    using Flag = void (__fastcall *)(void*, bool);
    Export<Flag>(sdk, "?set_enable_http_download@FileDownloadTask@lwp@gaea@@QEAAX_N@Z", category)(raw, true);
    auto partial = target; partial += L".partial";
    Export<Setter>(sdk, "?set_path@FileDownloadTask@lwp@gaea@@QEAAXAEBV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@@Z", category)(raw, partial.u8string());
    struct Opaque {};
    using Start = std::function<void()>;
    using Callback = std::function<void(const Opaque&)>;
    using Download = void (__fastcall *)(void*, Shared, Start, Callback, Callback, Callback);
    Start started = [] { /* The private destination is published only after completion. */ };
    Callback progress = [](const Opaque&) { /* Progress remains inside the native transfer service. */ };
    Callback failed = [failure](const Opaque&) { Fail(failure); };
    Callback succeeded = [base, account_layout = layout.account, account, partial, target, failure](const Opaque&) {
        // Validate the account again before publishing bytes from the asynchronous SDK callback.
        try {
            std::int64_t current = 0;
            if (!ReadAccountUid(base, account_layout, current) || current != account) throw std::runtime_error("account_changed");
            std::filesystem::copy_file(partial, target, std::filesystem::copy_options::overwrite_existing);
            std::filesystem::remove(partial);
        } catch (...) { Fail(failure); }
    };
    const auto service_instance = Export<Instance>(sdk, "?instance@FileService@file@wukong@@SAPEAV123@XZ", category);
    const auto download = Export<Download>(sdk, "?download@FileService@file@wukong@@QEAAXV?$shared_ptr@VFileDownloadTask@file@wukong@@@std@@V?$function@$$A6AXXZ@5@V?$function@$$A6AXAEBVFileProgress@lwp@gaea@@@Z@5@V?$function@$$A6AXAEBVFileDownloadResult@lwp@gaea@@@Z@5@V?$function@$$A6AXAEBVErrorResult@wukong@@@Z@5@@Z", category);
    download(service_instance(), std::move(task), std::move(started), std::move(progress), std::move(succeeded), std::move(failed));
}

// Use the same space/file download manager as the client's group-file interface.
void DownloadCloudFile(std::uintptr_t base, const ClientLayout& layout, const std::string& cid, const std::string& space,
                       const std::string& file, const std::filesystem::path& target,
                       const std::filesystem::path& failure, std::int64_t account) {
    const auto& entries = layout.attachments;
    if (!MatchesImageEntry(base, entries.cloud_download) || !MatchesImageEntry(base, entries.cloud_manager))
        throw AttachmentLayoutError::CloudFile;
    struct Context {
        int kind = 1, operation = 2;
        std::string cid, sid, scene, action;
    };
    static_assert(sizeof(Context) == 136, "Requires the release MSVC file-context layout");
    struct Opaque {};
    using Success = std::function<void(const std::int64_t&, const Opaque&)>;
    using Failure = std::function<void(const Opaque&)>;
    using Download = void (__fastcall *)(void*, const std::string&, const std::string&, const std::string&,
                                          std::int64_t, const std::string&, unsigned, bool,
                                          const Success&, const Failure&, const Failure&, const Context&);
    Context context; context.cid = cid;
    auto partial = target; partial += L".partial";
    Success succeeded = [base, account_layout = layout.account, account, target, partial, failure](const std::int64_t&, const Opaque& result) {
        // A cache hit may return an existing SDK path, so copy the bounded result path after account validation.
        try {
            std::int64_t current = 0;
            if (!ReadAccountUid(base, account_layout, current) || current != account) throw std::runtime_error("account_changed");
            auto source = partial;
            if (!std::filesystem::is_regular_file(source) || std::filesystem::file_size(source) == 0) {
                const auto& path = *reinterpret_cast<const std::string*>(reinterpret_cast<const char*>(&result) + 0x28);
                if (path.empty() || path.size() > 32768) throw std::runtime_error("file_path_invalid");
                source = std::filesystem::u8path(path);
            }
            if (!source.is_absolute() || !std::filesystem::is_regular_file(source)) throw std::runtime_error("file_path_invalid");
            std::filesystem::copy_file(source, target, std::filesystem::copy_options::overwrite_existing);
        } catch (...) { Fail(failure); }
    };
    // Keep failures and progress within the private file transport.
    Failure failed = [failure](const Opaque&) { Fail(failure); };
    Failure progress = [](const Opaque&) {};
    auto manager = reinterpret_cast<Instance>(base + entries.cloud_manager.rva)();
    reinterpret_cast<Download>(base + entries.cloud_download.rva)(manager, space, file, partial.u8string(), -1,
                                                 std::string{}, 0, false, succeeded, failed, progress, context);
}
}
