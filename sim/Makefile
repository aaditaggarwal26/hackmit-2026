# Build the virtual board. CLK_HZ/BAUD must match what VerilatorNode passes.
# CLK_HZ is chosen near the Mcycles/s the harness prints so that simulated
# milliseconds (heartbeats, link timeout, autonomy) track wall time; measured
# 2.3 Mcycles/s on an M-series Mac with this design. BAUD = CLK_HZ/10.
CLK_HZ ?= 2000000
BAUD ?= 200000
RTL := $(filter-out ../rtl/hello.v,$(wildcard ../rtl/*.v))
obj_dir/Vtop_edge_node: $(RTL) verilator_main.cpp
	verilator --cc --exe --build -O2 -Wno-fatal --top-module top_edge_node -GBAUD=$(BAUD) -GCLK_HZ=$(CLK_HZ) \
	  -I../rtl --Mdir obj_dir $(RTL) verilator_main.cpp
clean:
	rm -rf obj_dir
.PHONY: clean
