#pragma once
#include <cstdint>
#include <filesystem>
#include <string>

namespace bridge_native::history {
// Submit one of the two read-only CSpace operations through the logged-in client.
void QueryFiles(std::uintptr_t base, const std::string& operation, const std::string& body_hex,
                const std::filesystem::path& result, const std::filesystem::path& failure,
                std::int64_t account);
// Download a rich-text image by its own authorization ID instead of its parent card type.
void DownloadInlineImage(std::uintptr_t base, const std::string& media,
                         const std::filesystem::path& target, const std::filesystem::path& failure,
                         std::int64_t account);
// Download a listed drive entry using its space/file identity and group context.
void DownloadCloudFile(std::uintptr_t base, const std::string& cid, const std::string& space,
                       const std::string& file, const std::filesystem::path& target,
                       const std::filesystem::path& failure, std::int64_t account);
}
