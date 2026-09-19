// score_kernel: one streaming pass over the resident frame against the resident
// reference (protocol.md §5.2), PPC pixels per clock:
//   cloud_px   = #(p > cloud_thr)            all pixels
//   changed_px = #(|p - r| > change_thr)     all pixels
//   sobel_sum  = sum |Gx|+|Gy|               interior centres only, 1 <= x,y <= 126
// Words stream in raster order. Two line buffers hold rows y-1 and y-2, so every
// word arrives as a triple (rows y-2, y-1, y). A three-deep shift of triples (T =
// word k, Tp = word k-1, Tpp = word k-2) gives each centre in Tp (centre row y-1)
// its 3x3 window: the left neighbour of column 0 comes from Tpp, the right neighbour
// of column PPC-1 from T. The shift wraps across row ends (word 0 of row y follows
// the last word of row y-1); that is harmless only because x = 0 and x = 127 are
// masked. One flush slot past the last word pushes the final centres of row 126 out.
//
// Pipeline (cycle offsets from the address issue):
//   +0 S0 issue addr          +1 S1 data lands, line buffers rotate, per-word counts
//   +2 S2 window on Tp        +3 S3 accumulate
// `iterations` > 1 repeats the pass (BENCH_RUN): the kernel is fixed-function, so
// every pass costs the same clocks whatever the pixels are. `cycles` counts start ->
// done over all iterations and saturates at 2^32-1.

`include "orbit_params.vh"

module score_kernel #(
    parameter PPC     = `ORBIT_PIXELS_PER_CYCLE,
    parameter FRAME_W = `ORBIT_FRAME_W,
    parameter FRAME_H = `ORBIT_FRAME_H,
    parameter ADDR_W  = 11                          // $clog2(FRAME_W*FRAME_H/PPC), set by the parent
) (
    input  wire              clk,
    input  wire              rst_n,
    input  wire              start,
    input  wire [31:0]       iterations,
    output reg               busy,
    output reg               done,                  // one-cycle pulse; results valid from here
    output reg  [31:0]       cycles,
    input  wire [7:0]        cloud_thr,
    input  wire [7:0]        change_thr,
    output wire [ADDR_W-1:0] frame_rd_addr,
    input  wire [8*PPC-1:0]  frame_rd_data,         // one cycle after frame_rd_addr
    output wire [ADDR_W-1:0] ref_rd_addr,
    input  wire [8*PPC-1:0]  ref_rd_data,
    output reg  [14:0]       cloud_px,
    output reg  [14:0]       changed_px,
    output reg  [24:0]       sobel_sum
);
    localparam WPR   = FRAME_W / PPC;               // words per row
    localparam WORDS = WPR * FRAME_H;
    localparam WW    = 8 * PPC;
    localparam KW    = (WPR > 1) ? $clog2(WPR) : 1;
    localparam YW    = $clog2(FRAME_H);
    localparam [2:0] DRAIN = 3'd6;                  // > S3 depth after the flush slot
    /* verilator lint_off WIDTHTRUNC */
    localparam [KW-1:0]     K_LAST = WPR - 1;
    localparam [ADDR_W-1:0] A_LAST = WORDS - 1;
    /* verilator lint_on WIDTHTRUNC */

    // ---------------------------------------------------------------- S0: address sequencer
    reg [ADDR_W-1:0] addr;
    reg [KW-1:0]     k;
    reg [YW-1:0]     y;
    reg              issuing, flushing;
    reg [2:0]        drain;
    reg [31:0]       iter_left;
    assign frame_rd_addr = addr;
    assign ref_rd_addr   = addr;

    // ---------------------------------------------------------------- S1: data, line buffers, counts
    reg [WW-1:0] lb1 [0:WPR-1];                     // row y-1
    reg [WW-1:0] lb2 [0:WPR-1];                     // row y-2
    reg          v1, f1;
    reg [KW-1:0] k1;
    reg [YW-1:0] y1;
    wire [WW-1:0] pw = frame_rd_data;
    wire [WW-1:0] rw = ref_rd_data;
    wire [WW-1:0] lb1_q = lb1[k1];
    wire [WW-1:0] lb2_q = lb2[k1];

    integer j;
    reg [3:0] cloud_w, change_w;                    // <= PPC <= 8
    reg [7:0] d;
    always @(*) begin
        cloud_w = 4'd0;
        change_w = 4'd0;
        d = 8'd0;
        for (j = 0; j < PPC; j = j + 1) begin
            if (pw[8*j +: 8] > cloud_thr) cloud_w = cloud_w + 1'b1;
            d = (pw[8*j +: 8] > rw[8*j +: 8]) ? (pw[8*j +: 8] - rw[8*j +: 8]) : (rw[8*j +: 8] - pw[8*j +: 8]);
            if (d > change_thr) change_w = change_w + 1'b1;
        end
    end

    // triples and their metadata
    reg [WW-1:0] top_c, mid_c, bot_c;               // T   = word k   (rows y-2, y-1, y)
    reg [WW-1:0] top_p, mid_p, bot_p;               // Tp  = word k-1
    reg [7:0]    top_pp, mid_pp, bot_pp;            // last pixel of Tpp = word k-2 (all that is needed)
    reg          tc_v, tp_v;
    reg [KW-1:0] tc_k, tp_k;
    reg [YW-1:0] tc_y, tp_y;
    reg [3:0]    cloud_2, change_2;
    reg          shifted_2;                         // a new Tp arrived: count its centres exactly once

    // ---------------------------------------------------------------- S2: windows for the PPC centres of Tp
    wire [10:0] mag [0:PPC-1];
    genvar g;
    generate
        for (g = 0; g < PPC; g = g + 1) begin : G
            wire [7:0] lt = (g == 0) ? top_pp : top_p[8*(g > 0 ? g-1 : 0) +: 8];
            wire [7:0] lm = (g == 0) ? mid_pp : mid_p[8*(g > 0 ? g-1 : 0) +: 8];
            wire [7:0] lb = (g == 0) ? bot_pp : bot_p[8*(g > 0 ? g-1 : 0) +: 8];
            wire [7:0] rt = (g == PPC-1) ? top_c[7:0] : top_p[8*(g < PPC-1 ? g+1 : 0) +: 8];
            wire [7:0] rm = (g == PPC-1) ? mid_c[7:0] : mid_p[8*(g < PPC-1 ? g+1 : 0) +: 8];
            wire [7:0] rb = (g == PPC-1) ? bot_c[7:0] : bot_p[8*(g < PPC-1 ? g+1 : 0) +: 8];
            sobel_unit u (.p00(lt), .p01(top_p[8*g +: 8]), .p02(rt),
                          .p10(lm),                         .p12(rm),
                          .p20(lb), .p21(bot_p[8*g +: 8]), .p22(rb), .mag(mag[g]));
        end
    endgenerate

    // centre row y-1 must be 1..FRAME_H-2  <=>  y >= 2 (y <= FRAME_H-1 by construction)
    wire        row_ok = tp_v && (tp_y >= 2);
    reg [13:0]  sob_w;                              // <= 8 x 2040
    integer m;
    always @(*) begin
        sob_w = 14'd0;
        for (m = 0; m < PPC; m = m + 1)
            if (row_ok && !(tp_k == {KW{1'b0}} && m == 0) && !(tp_k == K_LAST && m == PPC-1))
                sob_w = sob_w + {3'b0, mag[m]};
    end

    // ---------------------------------------------------------------- S3: accumulate
    reg [13:0] sob_3;
    reg [3:0]  cloud_3, change_3;
    reg [14:0] cloud_acc, change_acc;
    reg [24:0] sob_acc;

    integer i;
    always @(posedge clk) begin
        if (!rst_n) begin
            addr <= {ADDR_W{1'b0}}; k <= {KW{1'b0}}; y <= {YW{1'b0}};
            issuing <= 1'b0; flushing <= 1'b0; drain <= 3'd0; iter_left <= 32'd0;
            busy <= 1'b0; done <= 1'b0; cycles <= 32'd0;
            v1 <= 1'b0; f1 <= 1'b0; k1 <= {KW{1'b0}}; y1 <= {YW{1'b0}};
            top_c <= {WW{1'b0}}; mid_c <= {WW{1'b0}}; bot_c <= {WW{1'b0}};
            top_p <= {WW{1'b0}}; mid_p <= {WW{1'b0}}; bot_p <= {WW{1'b0}};
            top_pp <= 8'd0; mid_pp <= 8'd0; bot_pp <= 8'd0;
            tc_v <= 1'b0; tp_v <= 1'b0; tc_k <= {KW{1'b0}}; tp_k <= {KW{1'b0}}; tc_y <= {YW{1'b0}}; tp_y <= {YW{1'b0}};
            cloud_2 <= 4'd0; change_2 <= 4'd0; shifted_2 <= 1'b0;
            sob_3 <= 14'd0; cloud_3 <= 4'd0; change_3 <= 4'd0;
            cloud_acc <= 15'd0; change_acc <= 15'd0; sob_acc <= 25'd0;
            cloud_px <= 15'd0; changed_px <= 15'd0; sobel_sum <= 25'd0;
            for (i = 0; i < WPR; i = i + 1) begin lb1[i] <= {WW{1'b0}}; lb2[i] <= {WW{1'b0}}; end
        end else begin
            done <= 1'b0;
            if (busy && cycles != 32'hFFFFFFFF) cycles <= cycles + 1'b1;

            // ---- S3 (first, so the resets in S0 below win the same edge)
            cloud_acc <= cloud_acc + {11'b0, cloud_3};
            change_acc <= change_acc + {11'b0, change_3};
            sob_acc <= sob_acc + {11'b0, sob_3};

            // ---- S0
            if (start && !busy) begin
                busy <= 1'b1; cycles <= 32'd0;
                iter_left <= (iterations == 32'd0) ? 32'd1 : iterations;
                addr <= {ADDR_W{1'b0}}; k <= {KW{1'b0}}; y <= {YW{1'b0}};
                issuing <= 1'b1; flushing <= 1'b0;
                cloud_acc <= 15'd0; change_acc <= 15'd0; sob_acc <= 25'd0;
                tc_v <= 1'b0; tp_v <= 1'b0;           // stale triples from an earlier pass never count
            end else if (issuing) begin
                if (addr == A_LAST) begin
                    issuing <= 1'b0; flushing <= 1'b1;
                end else begin
                    addr <= addr + 1'b1;
                end
                if (k == K_LAST) begin k <= {KW{1'b0}}; y <= y + 1'b1; end
                else k <= k + 1'b1;
            end else if (flushing) begin
                flushing <= 1'b0; drain <= DRAIN;
            end else if (drain != 3'd0) begin
                drain <= drain - 1'b1;
                if (drain == 3'd1) begin
                    cloud_px <= cloud_acc; changed_px <= change_acc; sobel_sum <= sob_acc;
                    if (iter_left > 32'd1) begin
                        iter_left <= iter_left - 1'b1;
                        addr <= {ADDR_W{1'b0}}; k <= {KW{1'b0}}; y <= {YW{1'b0}};
                        issuing <= 1'b1;
                        cloud_acc <= 15'd0; change_acc <= 15'd0; sob_acc <= 25'd0;
                        tc_v <= 1'b0; tp_v <= 1'b0;
                    end else begin
                        busy <= 1'b0; done <= 1'b1;
                    end
                end
            end

            // ---- S1: data for the address issued last cycle
            v1 <= issuing;
            f1 <= flushing;
            k1 <= k;
            y1 <= y;
            if (v1) begin
                lb1[k1] <= pw;
                lb2[k1] <= lb1_q;
            end
            if (v1 || f1) begin                     // shift the triples once per word, once more to flush
                top_pp <= top_p[WW-8 +: 8]; mid_pp <= mid_p[WW-8 +: 8]; bot_pp <= bot_p[WW-8 +: 8];
                top_p <= top_c;  mid_p <= mid_c;  bot_p <= bot_c;
                top_c <= lb2_q;  mid_c <= lb1_q;  bot_c <= pw;
                tp_v <= tc_v; tp_k <= tc_k; tp_y <= tc_y;
                tc_v <= v1;   tc_k <= k1;   tc_y <= y1;
            end
            cloud_2 <= v1 ? cloud_w : 4'd0;
            change_2 <= v1 ? change_w : 4'd0;
            shifted_2 <= (v1 || f1);

            // ---- S2 -> S3
            sob_3 <= shifted_2 ? sob_w : 14'd0;
            cloud_3 <= cloud_2;
            change_3 <= change_2;
        end
    end
endmodule
