#pragma once

#include <array>
#include <cstdint>
#include <stdexcept>

// Titan170 MNDS16: interpolation resolution bits [29:28], phase in Q(14+resolution).
// Width/height fields encode N-1. Phase init and stripe accumulator remain zero.
// Verified against Qualcomm IFEMNDS16Titan17x at 36fc163a534963a5b3af52186af5efcc63401ad2.
inline uint32_t ife_scale_phase(uint32_t input, uint32_t output) {
  if (output == 0 || input < output || input > 16384 || uint64_t(input) >= uint64_t(output)*64) {
    throw std::invalid_argument("Unsupported IFE scale ratio");
  }
  uint32_t resolution = input < output*4 ? 3 : input < output*8 ? 2 : input < output*16 ? 1 : 0;
  uint32_t phase = (uint64_t(input) << (14 + resolution)) / output;
  if (phase > 0x3fffff) throw std::invalid_argument("IFE phase overflow");
  return (resolution << 28) | phase;
}

inline std::array<uint32_t, 11> ife_scale_registers(uint32_t iw, uint32_t ih, uint32_t ow, uint32_t oh) {
  uint32_t px = ife_scale_phase(iw, ow), py = ife_scale_phase(ih, oh);
  return {3, ((ow-1)<<16) | (iw-1), px, 0, 0, iw-1,
             ((oh-1)<<16) | (ih-1), py, 0, 0, ih-1};
}
