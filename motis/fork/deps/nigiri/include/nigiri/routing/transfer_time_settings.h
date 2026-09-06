// kora fork: patched copy of upstream transfer_time_settings.h (nigiri
// pin via MOTIS .pkg). Two changes:
// - `kora_minwalk_points_`, the per-query flag selecting the steeper
//   minimize-walking walk-points table (kora_walk_points.h,
//   routing-options.md § Minimize walking). It rides on this struct
//   because it must reach the same places the transfer settings already
//   flow to (raptor, both drivers, reconstruction, alternates); it does
//   NOT participate in `default_` or the adjusted-transfer-time math.
// - adjusted_transfer_time CEILS the factor-scaled duration instead of
//   upstream's truncation. Transfer times are whole minutes (ceiled from
//   Valhalla seconds at import), so truncating after the multiply
//   rounds the scaling away on exactly the short transfers that matter
//   (1 min × 1.275 → 1 min) and lets RAPTOR keep connections the set
//   walking speed cannot make. Rounding is always up, never down — an
//   optimistic transfer time offers physically impossible connections.

#pragma once

#include <cmath>

#include "nigiri/types.h"

namespace nigiri::routing {

struct transfer_time_settings {
  bool operator==(transfer_time_settings const& o) const {
    return default_ == o.default_ ||
           std::tie(min_transfer_time_, additional_time_, factor_) ==
               std::tie(o.min_transfer_time_, additional_time_, o.factor_);
  }

  bool default_{true};
  duration_t min_transfer_time_{0};
  duration_t additional_time_{0};
  float factor_{1.0F};
  // kora fork: minimize-walking point table selector (see header note).
  bool kora_minwalk_points_{false};
};

template <typename T>
inline constexpr T adjusted_transfer_time(
    transfer_time_settings const& settings, T const duration) {
  if (settings.default_) {
    return duration;
  } else {
    return static_cast<T>(settings.additional_time_.count()) +
           std::max(static_cast<T>(settings.min_transfer_time_.count()),
                    static_cast<T>(std::ceil(static_cast<float>(duration) *
                                             settings.factor_)));
  }
}

template <typename Rep>
inline constexpr std::chrono::duration<Rep, std::ratio<60>>
adjusted_transfer_time(
    transfer_time_settings const& settings,
    std::chrono::duration<Rep, std::ratio<60>> const duration) {
  if (settings.default_) {
    return duration;
  } else {
    return std::chrono::duration<Rep, std::ratio<60>>{
        static_cast<Rep>(settings.additional_time_.count()) +
        std::max(static_cast<Rep>(settings.min_transfer_time_.count()),
                 static_cast<Rep>(std::ceil(
                     static_cast<float>(duration.count()) *
                     settings.factor_)))};
  }
}

}  // namespace nigiri::routing
