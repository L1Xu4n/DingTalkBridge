#include "payload_parser.h"
#include "safe_read.h"

#include <array>
#include <charconv>
#include <cstring>
#include <limits>
#include <new>
#include <type_traits>
#include <utility>
#include <vector>

namespace dingtalkbridge::native::payload {
namespace {

struct PointerWrapper { std::uintptr_t value; };
struct BatchPrefix { std::uintptr_t head; std::uint64_t count; };
struct ListNode {
    std::uintptr_t next, previous, message, shared_control;
};
struct NativeString {
    std::array<unsigned char, 16> storage;
    std::uint64_t size;
    std::uint64_t capacity;
};
struct MessageHeader {
    std::uintptr_t vtable;
    std::uintptr_t content;
    std::uintptr_t content_shared_control;
    std::array<unsigned char, 0x38 - 0x18> unknown_18;
    NativeString conversation;
    std::int64_t message_id;
    std::array<unsigned char, 8> unknown_60;
    std::int64_t sender_id;
    std::array<unsigned char, 0xf4 - 0x70> unknown_70;
    std::int32_t raw_type;
};
struct TextContentHeader {
    std::uintptr_t vtable;
    std::array<unsigned char, 0x38 - 8> unknown_08;
    NativeString text;
};

static_assert(sizeof(void*) == 8, "Recovered ABI is x64 only");
static_assert(sizeof(PointerWrapper) == 8 && sizeof(BatchPrefix) == 16);
static_assert(sizeof(ListNode) == 32 && sizeof(NativeString) == 32);
static_assert(offsetof(MessageHeader, conversation) == 0x38);
static_assert(offsetof(MessageHeader, message_id) == 0x58);
static_assert(offsetof(MessageHeader, sender_id) == 0x68);
static_assert(offsetof(MessageHeader, raw_type) == 0xf4);
static_assert(sizeof(MessageHeader) == 0xf8 && sizeof(TextContentHeader) == 0x58);
static_assert(std::is_trivially_copyable_v<MessageHeader>);
static_assert(std::is_trivially_copyable_v<TextContentHeader>);

bool object_pointer(std::uintptr_t value) noexcept {
    return value >= 0x10000 && (value & 7) == 0;
}

template <typename T>
bool read_pod(std::uintptr_t pointer, T& value) noexcept {
    static_assert(std::is_trivially_copyable_v<T>);
    return object_pointer(pointer) && detail::safe_read(
        reinterpret_cast<const void*>(pointer), &value, sizeof(value));
}

bool valid_utf8(const std::string& value) noexcept {
    const auto* p = reinterpret_cast<const unsigned char*>(value.data());
    std::size_t i = 0;
    while (i < value.size()) {
        const auto first = p[i++];
        if (first <= 0x7f) continue;
        std::uint32_t point = 0;
        std::size_t trailing = 0;
        std::uint32_t minimum = 0;
        if (first >= 0xc2 && first <= 0xdf) {
            point = first & 0x1f; trailing = 1; minimum = 0x80;
        } else if (first >= 0xe0 && first <= 0xef) {
            point = first & 0x0f; trailing = 2; minimum = 0x800;
        } else if (first >= 0xf0 && first <= 0xf4) {
            point = first & 0x07; trailing = 3; minimum = 0x10000;
        } else return false;
        if (trailing > value.size() - i) return false;
        while (trailing--) {
            const auto next = p[i++];
            if ((next & 0xc0) != 0x80) return false;
            point = (point << 6) | (next & 0x3f);
        }
        if (point < minimum || point > 0x10ffff ||
            (point >= 0xd800 && point <= 0xdfff)) return false;
    }
    return true;
}

DropReason copy_string(const NativeString& source, std::size_t maximum,
                       std::string& destination) {
    if (source.size > maximum || source.size > source.capacity)
        return DropReason::string_bounds;
    if (source.capacity < 16) {
        if (source.capacity != 15 || source.size > 15)
            return DropReason::string_bounds;
        destination.assign(reinterpret_cast<const char*>(source.storage.data()),
                           static_cast<std::size_t>(source.size));
    } else {
        std::uintptr_t data = 0;
        std::memcpy(&data, source.storage.data(), sizeof(data));
        if (data < 0x10000) return DropReason::invalid_pointer;
        destination.resize(static_cast<std::size_t>(source.size));
        if (!detail::safe_read(reinterpret_cast<const void*>(data),
                               destination.data(), destination.size()))
            return DropReason::unreadable_payload;
    }
    return valid_utf8(destination) ? DropReason::none : DropReason::invalid_utf8;
}

std::string decimal_id(std::int64_t value) {
    char buffer[32];
    const auto converted = std::to_chars(buffer, buffer + sizeof(buffer), value);
    return std::string(buffer, converted.ptr);
}

ParseResult parse_selected(const void* details_wrapper, std::uintptr_t base,
                           void* context, OwnedMessageSink sink) {
    ParseResult result;
    if (!sink) { result.reason = DropReason::missing_sink; return result; }
    if (!base || (base & 0xffff) != 0 ||
        base > (std::numeric_limits<std::uintptr_t>::max)() - kTextContentVtableRva) {
        result.reason = DropReason::invalid_module_base;
        return result;
    }
    const auto expected_message_vtable = base + kMessageVtableRva;
    const auto expected_text_vtable = base + kTextContentVtableRva;
    PointerWrapper details{};
    if (!read_pod(reinterpret_cast<std::uintptr_t>(details_wrapper), details)) {
        result.reason = DropReason::unreadable_payload;
        return result;
    }
    if (!details.value) return result;
    BatchPrefix batch{};
    if (!read_pod(details.value, batch)) {
        result.reason = DropReason::unreadable_payload;
        return result;
    }
    if (batch.count > kMaximumBatchNodes) {
        result.reason = DropReason::batch_limit;
        return result;
    }
    ListNode sentinel{};
    if (!read_pod(batch.head, sentinel)) {
        result.reason = DropReason::unreadable_payload;
        return result;
    }
    std::array<std::uintptr_t, kMaximumBatchNodes> visited{};
    std::vector<ParsedMessage> copied;
    copied.reserve(static_cast<std::size_t>(batch.count));
    std::size_t total_copied_bytes = 0;
    auto node_pointer = sentinel.next;
    auto previous = batch.head;
    for (std::size_t i = 0; i < batch.count; ++i) {
        if (!object_pointer(node_pointer) || node_pointer == batch.head) {
            result.reason = DropReason::invalid_list;
            return result;
        }
        for (std::size_t seen = 0; seen < i; ++seen) {
            if (visited[seen] == node_pointer) {
                result.reason = DropReason::invalid_list;
                return result;
            }
        }
        visited[i] = node_pointer;
        ListNode node{};
        if (!read_pod(node_pointer, node)) {
            result.reason = DropReason::unreadable_payload;
            return result;
        }
        ++result.nodes_visited;
        if (node.previous != previous) {
            result.reason = DropReason::invalid_list;
            return result;
        }
        previous = node_pointer;
        node_pointer = node.next;
        if (!node.message) { ++result.skipped_null_messages; continue; }
        std::uintptr_t message_vtable = 0;
        if (!read_pod(node.message, message_vtable)) {
            result.reason = DropReason::unreadable_payload;
            return result;
        }
        if (message_vtable != expected_message_vtable) {
            result.reason = DropReason::message_vtable_mismatch;
            return result;
        }
        MessageHeader message{};
        if (!read_pod(node.message, message)) {
            result.reason = DropReason::unreadable_payload;
            return result;
        }
        if (message.vtable != expected_message_vtable) {
            result.reason = DropReason::message_vtable_mismatch;
            return result;
        }
        if (message.raw_type != 1) { ++result.skipped_non_text; continue; }
        if (!message.content) { ++result.skipped_empty_text; continue; }
        std::uintptr_t content_vtable = 0;
        if (!read_pod(message.content, content_vtable)) {
            result.reason = DropReason::unreadable_payload;
            return result;
        }
        if (content_vtable != expected_text_vtable) {
            result.reason = DropReason::text_vtable_mismatch;
            return result;
        }
        TextContentHeader content{};
        if (!read_pod(message.content, content)) {
            result.reason = DropReason::unreadable_payload;
            return result;
        }
        if (content.vtable != expected_text_vtable) {
            result.reason = DropReason::text_vtable_mismatch;
            return result;
        }
        ParsedMessage value;
        result.reason = copy_string(message.conversation, kMaximumConversationBytes,
                                    value.conversation_id);
        if (result.reason != DropReason::none) return result;
        if (value.conversation_id.empty() ||
            value.conversation_id.find('\0') != std::string::npos) {
            result.reason = DropReason::invalid_conversation_id;
            return result;
        }
        result.reason = copy_string(content.text, kMaximumTextBytes, value.text);
        if (result.reason != DropReason::none) return result;
        if (value.text.empty()) { ++result.skipped_empty_text; continue; }
        value.message_id = decimal_id(message.message_id);
        value.sender_id = decimal_id(message.sender_id);
        const auto owned_bytes = value.conversation_id.size() + value.text.size() +
                                  value.message_id.size() + value.sender_id.size();
        if (owned_bytes > kMaximumBatchCopiedBytes - total_copied_bytes) {
            result.reason = DropReason::copied_byte_limit;
            return result;
        }
        total_copied_bytes += owned_bytes;
        copied.push_back(std::move(value));
    }
    if (node_pointer != batch.head || sentinel.previous != previous) {
        result.reason = DropReason::invalid_list;
        return result;
    }
    // No borrowed object pointers escape into the sink.
    for (auto& message : copied) {
        sink(context, std::move(message));
        ++result.delivered_count;
    }
    return result;
}

}  // namespace

ParseResult parse_on_notification(std::int32_t event_type, const void* details_wrapper,
                                 std::uintptr_t main_frame_base, void* sink_context,
                                 OwnedMessageSink sink) noexcept {
    if (event_type != kNewMessageNotification) {
        ParseResult result;
        result.reason = DropReason::event_not_selected;
        return result;
    }
    try {
        return parse_selected(details_wrapper, main_frame_base, sink_context, sink);
    } catch (const std::bad_alloc&) {
        ParseResult result;
        result.reason = DropReason::allocation_failure;
        return result;
    } catch (...) {
        ParseResult result;
        result.reason = DropReason::unexpected_failure;
        return result;
    }
}

}  // namespace dingtalkbridge::native::payload
