// Test-only wrapper (not under rtl/, so neither Vivado nor the Verilator board build
// sees it): frame store + reference store + score_kernel + score_composite with a
// word-write port, so cocotb can load two images and read back every §5.2 quantity.

`include "orbit_params.vh"

module score_path_tb #(
    parameter PPC = `ORBIT_PIXELS_PER_CYCLE
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        wr_en,
    input  wire        wr_ref,
    input  wire [13:0] wr_addr,          // word address (ADDR_W bits used)
    input  wire [63:0] wr_data,          // low 8*PPC bits used
    input  wire        start,
    input  wire [31:0] iterations,
    output wire        busy,
    output wire        done,
    output wire [31:0] cycles,
    input  wire [7:0]  cloud_thr,
    input  wire [7:0]  change_thr,
    input  wire [15:0] w_clear,
    input  wire [15:0] w_sharp,
    input  wire [15:0] w_change,
    input  wire [4:0]  sharp_shift,
    output wire [14:0] cloud_px,
    output wire [14:0] changed_px,
    output wire [24:0] sobel_sum,
    input  wire        c_start,
    output wire        c_done,
    output wire [15:0] clear,
    output wire [15:0] sharp,
    output wire [15:0] change,
    output wire [15:0] score
);
    localparam WW = 8 * PPC;
    localparam WORDS = `ORBIT_FRAME_BYTES / PPC;
    localparam ADDR_W = $clog2(WORDS);
    wire [ADDR_W-1:0] fa, ra;
    wire [WW-1:0] fd, rd;
    frame_store #(.WORD_W(WW), .WORDS(WORDS), .ADDR_W(ADDR_W)) fm (
        .clk(clk), .wr_en(wr_en && !wr_ref), .wr_addr(wr_addr[ADDR_W-1:0]), .wr_data(wr_data[WW-1:0]), .rd_addr(fa), .rd_data(fd));
    frame_store #(.WORD_W(WW), .WORDS(WORDS), .ADDR_W(ADDR_W)) rm (
        .clk(clk), .wr_en(wr_en && wr_ref), .wr_addr(wr_addr[ADDR_W-1:0]), .wr_data(wr_data[WW-1:0]), .rd_addr(ra), .rd_data(rd));
    score_kernel #(.PPC(PPC), .ADDR_W(ADDR_W)) k (
        .clk(clk), .rst_n(rst_n), .start(start), .iterations(iterations), .busy(busy), .done(done), .cycles(cycles),
        .cloud_thr(cloud_thr), .change_thr(change_thr), .frame_rd_addr(fa), .frame_rd_data(fd), .ref_rd_addr(ra), .ref_rd_data(rd),
        .cloud_px(cloud_px), .changed_px(changed_px), .sobel_sum(sobel_sum));
    score_composite c (
        .clk(clk), .rst_n(rst_n), .start(c_start), .done(c_done), .cloud_px(cloud_px), .changed_px(changed_px),
        .sobel_sum(sobel_sum), .w_clear(w_clear), .w_sharp(w_sharp), .w_change(w_change), .sharp_shift(sharp_shift),
        .clear(clear), .sharp(sharp), .change(change), .score(score));
endmodule
