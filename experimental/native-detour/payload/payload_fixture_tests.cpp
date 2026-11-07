#include "payload_parser.h"
#include "safe_read.h"

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>
#include <array>
#include <cstring>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string_view>
#include <utility>
#include <vector>

using namespace dingtalkbridge::native::payload;

namespace {
constexpr std::uintptr_t kSyntheticModuleBase = 0x180000000;

void require(bool value, const char* test_name) {
    if (!value) throw std::runtime_error(test_name);
}

struct OwnedArena {
    std::vector<std::unique_ptr<unsigned char[]>> blocks;
    std::uintptr_t allocate(std::size_t count) {
        auto data = std::make_unique<unsigned char[]>(count);
        std::memset(data.get(), 0, count);
        const auto address = reinterpret_cast<std::uintptr_t>(data.get());
        blocks.push_back(std::move(data));
        return address;
    }
    template <typename T> void put(std::uintptr_t destination, T value) {
        std::memcpy(reinterpret_cast<void*>(destination), &value, sizeof(value));
    }
    void string(std::uintptr_t destination, std::string_view value) {
        std::array<unsigned char, 32> data{};
        const auto length = static_cast<std::uint64_t>(value.size());
        std::uint64_t capacity = 15;
        if (value.size() <= 15) {
            std::memcpy(data.data(), value.data(), value.size());
        } else {
            const auto pointer = allocate(value.size() + 1);
            std::memcpy(reinterpret_cast<void*>(pointer), value.data(), value.size());
            std::memcpy(data.data(), &pointer, sizeof(pointer));
            capacity = length;
        }
        std::memcpy(data.data() + 16, &length, 8);
        std::memcpy(data.data() + 24, &capacity, 8);
        std::memcpy(reinterpret_cast<void*>(destination), data.data(), data.size());
    }
};

struct Fixture {
    OwnedArena arena;
    std::uintptr_t param{}, sentinel{}, details{};
    std::vector<std::uintptr_t> nodes, messages, contents;

    explicit Fixture(const std::vector<std::string>& texts) {
        param = arena.allocate(0x78);
        sentinel = arena.allocate(32);
        details = arena.allocate(8);
        arena.put(details, param);
        arena.put(param, sentinel);
        arena.put(param + 8, static_cast<std::uint64_t>(texts.size()));
        for (std::size_t i = 0; i < texts.size(); ++i) nodes.push_back(arena.allocate(32));
        for (std::size_t i = 0; i < texts.size(); ++i) {
            const auto message = arena.allocate(0xf8);
            const auto content = arena.allocate(0x58);
            messages.push_back(message);
            contents.push_back(content);
            arena.put(message, kSyntheticModuleBase + kMessageVtableRva);
            arena.put(message + 8, content);
            arena.string(message + 0x38, "synthetic-conversation");
            arena.put(message + 0x58, INT64_C(9007199254740993) + static_cast<std::int64_t>(i));
            arena.put(message + 0x68, INT64_C(123456789012345678));
            arena.put(message + 0xf4, std::int32_t{1});
            arena.put(content, kSyntheticModuleBase + kTextContentVtableRva);
            arena.string(content + 0x38, texts[i]);
            arena.put(nodes[i], i + 1 == nodes.size() ? sentinel : nodes[i + 1]);
            arena.put(nodes[i] + 8, i == 0 ? sentinel : nodes[i - 1]);
            arena.put(nodes[i] + 16, message);
        }
        arena.put(sentinel, nodes.empty() ? sentinel : nodes.front());
        arena.put(sentinel + 8, nodes.empty() ? sentinel : nodes.back());
    }
};

struct Captured {
    std::vector<ParsedMessage> values;
    bool failed = false;
    static void sink(void* context, ParsedMessage&& value) noexcept {
        auto& captured = *static_cast<Captured*>(context);
        try { captured.values.push_back(std::move(value)); }
        catch (...) { captured.failed = true; }
    }
};

ParseResult parse(Fixture& fixture, Captured& capture) {
    return parse_on_notification(400, reinterpret_cast<const void*>(fixture.details),
                                 kSyntheticModuleBase, &capture, &Captured::sink);
}

void selection_does_not_read_arguments() {
    const auto result = parse_on_notification(399, reinterpret_cast<void*>(1),
                                               1, reinterpret_cast<void*>(1), nullptr);
    require(result.reason == DropReason::event_not_selected, "event_selection");
}

void inline_heap_unicode_and_ids() {
    const std::string unicode = "\xe5\x90\x88\xe6\x88\x90\xf0\x9f\x99\x82\nfixture";
    const std::vector<std::string> inputs{std::string(15, 'a'), std::string(16, 'b'), unicode};
    Fixture fixture(inputs);
    Captured capture;
    const auto result = parse(fixture, capture);
    require(result.reason == DropReason::none && result.delivered_count == 3 && !capture.failed,
            "valid_batch_counts");
    for (std::size_t i = 0; i < inputs.size(); ++i)
        require(capture.values[i].text == inputs[i], "owned_text_equality");
    require(capture.values[0].message_id == "9007199254740993", "large_integer_id");
    require(capture.values[0].sender_id == "123456789012345678", "sender_integer_id");
}

void empty_batch_and_null_details() {
    Fixture fixture({});
    Captured capture;
    require(parse(fixture, capture).reason == DropReason::none, "empty_batch");
    fixture.arena.put(fixture.details, std::uintptr_t{0});
    require(parse(fixture, capture).reason == DropReason::none && capture.values.empty(),
            "null_detail_value");
}

void skip_nontext_without_content_access() {
    Fixture fixture({"fixture"});
    fixture.arena.put(fixture.messages[0] + 0xf4, std::int32_t{2});
    fixture.arena.put(fixture.messages[0] + 8, std::uintptr_t{1});
    Captured capture;
    const auto result = parse(fixture, capture);
    require(result.reason == DropReason::none && result.skipped_non_text == 1 &&
                capture.values.empty(), "skip_nontext");
}

void exact_vtable_guards() {
    Fixture first({"fixture"});
    first.arena.put(first.messages[0], std::uintptr_t{0});
    Captured capture;
    require(parse(first, capture).reason == DropReason::message_vtable_mismatch,
            "message_vtable");
    Fixture second({"fixture"});
    second.arena.put(second.contents[0], kSyntheticModuleBase + kTextContentVtableRva + 8);
    require(parse(second, capture).reason == DropReason::text_vtable_mismatch &&
                capture.values.empty(), "content_vtable");
}

void malformed_batch_has_no_partial_delivery() {
    Fixture fixture({"first", "second"});
    fixture.arena.put(fixture.nodes[1] + 8, std::uintptr_t{0});
    Captured capture;
    require(parse(fixture, capture).reason == DropReason::invalid_list && capture.values.empty(),
            "atomic_batch_drop");
}

void cycle_count_and_tail_guards() {
    Captured capture;
    Fixture cycle({"first", "second"});
    cycle.arena.put(cycle.nodes[0], cycle.nodes[0]);
    require(parse(cycle, capture).reason == DropReason::invalid_list, "list_cycle");
    Fixture count({"one"});
    count.arena.put(count.param + 8, static_cast<std::uint64_t>(kMaximumBatchNodes + 1));
    count.arena.put(count.param, std::uintptr_t{1});
    require(parse(count, capture).reason == DropReason::batch_limit, "batch_limit_first");
    Fixture tail({"one"});
    tail.arena.put(tail.sentinel + 8, tail.sentinel);
    require(parse(tail, capture).reason == DropReason::invalid_list, "sentinel_tail");
}

void string_bounds_and_invalid_utf8() {
    Captured capture;
    Fixture size({"one"});
    size.arena.put(size.contents[0] + 0x38 + 16,
                   static_cast<std::uint64_t>(kMaximumTextBytes + 1));
    require(parse(size, capture).reason == DropReason::string_bounds, "string_maximum");
    Fixture capacity({"one"});
    capacity.arena.put(capacity.contents[0] + 0x38 + 24, std::uint64_t{7});
    require(parse(capacity, capture).reason == DropReason::string_bounds, "sso_capacity");
    for (const std::string malformed : {std::string("\xc0\xaf", 2),
                                       std::string("\xed\xa0\x80", 3),
                                       std::string("\xf4\x90\x80\x80", 4),
                                       std::string("\xe2\x82", 2)}) {
        Fixture invalid({malformed});
        require(parse(invalid, capture).reason == DropReason::invalid_utf8,
                "utf8_rejection");
    }
    require(capture.values.empty(), "no_invalid_delivery");
}

void pointers_and_identifier_bounds() {
    Fixture bad_heap({std::string(16, 'x')});
    bad_heap.arena.put(bad_heap.contents[0] + 0x38, std::uintptr_t{0xdeadbeef});
    Captured capture;
    require(parse(bad_heap, capture).reason == DropReason::unreadable_payload,
            "heap_pointer_bounds");
    Fixture cid({"one"});
    cid.arena.string(cid.messages[0] + 0x38, std::string_view("a\0b", 3));
    require(parse(cid, capture).reason == DropReason::invalid_conversation_id,
            "conversation_identifier");
    auto missing = parse_on_notification(400, reinterpret_cast<void*>(1),
                                            kSyntheticModuleBase, nullptr, nullptr);
    require(missing.reason == DropReason::missing_sink, "missing_sink_no_read");
}

void copy_outlives_all_borrowed_allocations() {
    Captured capture;
    {
        Fixture fixture({std::string(80, 'x')});
        require(parse(fixture, capture).delivered_count == 1, "copy_before_free");
    }
    require(capture.values.size() == 1 && capture.values[0].text == std::string(80, 'x'),
            "owned_after_free");
}

void aggregate_byte_limit_is_atomic() {
    Fixture fixture(std::vector<std::string>(17, std::string(kMaximumTextBytes, 'x')));
    Captured capture;
    require(parse(fixture, capture).reason == DropReason::copied_byte_limit &&
                capture.values.empty(), "aggregate_limit");
}

void safe_read_rejects_own_noaccess_page() {
    SYSTEM_INFO info{};
    GetSystemInfo(&info);
    const auto bytes = static_cast<std::size_t>(info.dwPageSize);
    auto* region = static_cast<unsigned char*>(
        VirtualAlloc(nullptr, 2 * bytes, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE));
    require(region != nullptr, "own_page_allocation");
    DWORD old = 0;
    const bool protected_page = VirtualProtect(region + bytes, bytes, PAGE_NOACCESS, &old) != 0;
    unsigned char destination[8]{};
    const bool readable = detail::safe_read(region, destination, sizeof(destination));
    const bool rejected = !detail::safe_read(region + bytes - 4, destination, sizeof(destination));
    VirtualFree(region, 0, MEM_RELEASE);
    require(protected_page && readable && rejected, "own_noaccess_span");
}

}  // namespace

int main() {
    const std::array<void (*)(), 12> cases{
        selection_does_not_read_arguments, inline_heap_unicode_and_ids,
        empty_batch_and_null_details, skip_nontext_without_content_access,
        exact_vtable_guards, malformed_batch_has_no_partial_delivery,
        cycle_count_and_tail_guards, string_bounds_and_invalid_utf8,
        pointers_and_identifier_bounds, copy_outlives_all_borrowed_allocations,
        aggregate_byte_limit_is_atomic, safe_read_rejects_own_noaccess_page};
    std::size_t completed = 0;
    try {
        for (const auto test : cases) { test(); ++completed; }
    } catch (const std::exception& error) {
        std::cerr << "fixture_failed=" << error.what() << '\n';
        return 1;
    }
    std::cout << "{\"synthetic_tests_passed\":" << completed
              << ",\"real_process_connected\":false,\"message_bodies_printed\":false}\n";
    return 0;
}
