#include <cassert>
#include <iostream>
#include "ife_scale.h"
int main() {
  assert(ife_scale_phase(1928,1928)==0x30020000);
  assert(ife_scale_phase(2688,1344)==0x30040000);
  const auto y=ife_scale_registers(1928,1208,1344,760);
  const auto uv=ife_scale_registers(1928,1208,672,380);
  assert((y[1]>>16)==1343 && (y[1]&0x3fff)==1927);
  assert((uv[6]>>16)==379 && (uv[6]&0x3fff)==1207);
  assert((y[2]&0x3fffff)==(uint64_t(1928)<<17)/1344);
  assert((uv[7]&0x3fffff)==(uint64_t(1208)<<17)/380);
  bool rejected=false;try {ife_scale_phase(1928,0);} catch(const std::invalid_argument&) {rejected=true;} assert(rejected);
  for(auto v:y) std::cout<<std::hex<<v<<' ';std::cout<<'\n';
  for(auto v:uv) std::cout<<std::hex<<v<<' ';std::cout<<'\n';
}
