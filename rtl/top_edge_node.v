// top_edge_node: Arty A7-100T board wrapper around edge_node_core.
//   clk            100 MHz oscillator (E3)
//   sw[1:0]        node id
//   uart_txd_in    FT2232 -> FPGA (our receive)   uart_rxd_out  FPGA -> FT2232 (our transmit)
//   led[0] heartbeat blink, led[1] link_ok, led[2] has_data (queue non-empty), led[3] busy (scoring/bench)
//   led0_r/g/b     IDLE blue, SCORING green, BENCH red (1/16 duty so it is not blinding)
//   ja[0] SCL, ja[1] SDA  open-drain I2C to the INA219 (3.3 V pull-ups on the breakout)
// Reset comes from a power-on counter (Xilinx flops take their initial value
// at configuration, so `= 0` is the GSR value); the reset button is unused.
// Port names match sim/verilator_main.cpp and the Digilent master XDC.

`include "orbit_params.vh"

module top_edge_node #(
    parameter PPC    = `ORBIT_PIXELS_PER_CYCLE,
    parameter CLK_HZ = `ORBIT_CLK_HZ,
    parameter BAUD   = `ORBIT_BAUD
) (
    input  wire       clk,
    input  wire [1:0] sw,
    input  wire       uart_txd_in,
    output wire       uart_rxd_out,
    output wire [3:0] led,
    output wire       led0_r,
    output wire       led0_g,
    output wire       led0_b,
    inout  wire [1:0] ja
);
    // power-on reset: released after 256 clocks, synchronous to clk
    reg [8:0] por_cnt;
    initial por_cnt = 9'd0;
    always @(posedge clk) if (!por_cnt[8]) por_cnt <= por_cnt + 1'b1;
    wire rst_n = por_cnt[8];

    wire [1:0] state;
    wire link_ok, has_data, busy, hb_blink, scl_oe, sda_oe;

    edge_node_core #(.PPC(PPC), .CLK_HZ(CLK_HZ), .BAUD(BAUD)) core (
        .clk(clk), .rst_n(rst_n), .node_id({6'b0, sw}),
        .uart_rxd(uart_txd_in), .uart_txd(uart_rxd_out),
        .scl_oe(scl_oe), .sda_oe(sda_oe), .sda_i(ja[1]),
        .state(state), .link_ok(link_ok), .has_data(has_data), .busy(busy), .hb_blink(hb_blink));

    // open-drain: drive low or release to the pull-up
    assign ja[0] = scl_oe ? 1'b0 : 1'bz;
    assign ja[1] = sda_oe ? 1'b0 : 1'bz;

    assign led = {busy, has_data, link_ok, hb_blink};

    reg [3:0] pwm_cnt;
    initial pwm_cnt = 4'd0;
    always @(posedge clk) pwm_cnt <= pwm_cnt + 1'b1;
    wire dim = (pwm_cnt == 4'd0);
    wire red   = (state == 2'd2);
    wire green = (state == 2'd1);
    wire blue  = (state == 2'd0);
    assign led0_r = red & dim;
    assign led0_g = green & dim;
    assign led0_b = blue & dim;
endmodule
