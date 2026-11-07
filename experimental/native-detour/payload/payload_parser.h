#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace dingtalkbridge::native::payload {

inline constexpr std::int32_t kNewMessageNotification = 400;
inline constexpr std::uintptr_t kMessageVtableRva = 0x709e9e0;
inline constexpr std::uintptr_t kTextContentVtableRva = 0x70d0e48;
inline constexpr std::size_t kMaximumBatchNodes = 512;
inline constexpr std::size_t kMaximumTextBytes = 64 * 1024;
inline constexpr std::size_t kMaximumConversationBytes = 1024;
inline constexpr std::size_t kMaximumBatchCopiedBytes = 1024 * 1024;

// These are owned strings, independent of the product's borrowed payload.
// They deliberately contain no inferred direction, sender name, or timestamp.
struct ParsedMessage {
    std::string conversation_id;
    std::string message_id;
    std::string sender_id;
    std::string text;
};

// The receiver owns the delivered value. It must catch its own queue/allocation
// errors, remain noexcept, and never wait for UI work on the notifying thread.
using OwnedMessageSink = void (*)(void* context, ParsedMessage&& message) noexcept;

enum class DropReason : std::uint32_t {
    none = 0,
    event_not_selected,
    missing_sink,
    invalid_module_base,
    invalid_pointer,
    unreadable_payload,
    batch_limit,
    invalid_list,
    message_vtable_mismatch,
    text_vtable_mismatch,
    string_bounds,
    invalid_utf8,
    invalid_conversation_id,
    copied_byte_limit,
    allocation_failure,
    unexpected_failure,
};

struct ParseResult {
    DropReason reason = DropReason::none;
    std::uint32_t nodes_visited = 0;
    std::uint32_t delivered_count = 0;
    std::uint32_t skipped_non_text = 0;
    std::uint32_t skipped_null_messages = 0;
    std::uint32_t skipped_empty_text = 0;
};

// The caller must have checked loaded module identity, dispatch signature and
// compatible RTTI/layout. Whole-client hashes are not required. No process discovery
// occurs here. event_type != 400 returns before inspecting any other argument.
//
// Entire list topology and accepted payloads are copied and validated before
// the first sink call, so a malformed batch produces no partial delivery.
// Exceptions cause a drop result. This parser never calls or suppresses the
// original dispatcher: its caller must forward the original notification once,
// regardless of this result, and keep hook/trampoline code alive as required.
ParseResult parse_on_notification(
    std::int32_t event_type,
    const void* details_wrapper,
    std::uintptr_t main_frame_base,
    void* sink_context,
    OwnedMessageSink sink) noexcept;

}  // namespace dingtalkbridge::native::payload
