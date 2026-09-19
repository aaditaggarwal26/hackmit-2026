// score_composite: the per-frame tail of protocol.md §5.2.
//   clear  = sat16((FRAME_BYTES - cloud_px) << 2)      (17 bits before saturation)
//   sharp  = sat16(sobel_sum >> sharp_shift)           (25-bit barrel shift, shift 0..24)
//   change = sat16(changed_px << 2)
//   score  = sat16((w_clear*clear + w_sharp*sharp + w_change*change) >> 16)   (34-bit sum)
// The three registered 16x16 products are the ONLY multipliers in the design;
// Vivado maps them to DSP48E1s. Three cycles from start to done.

`include "orbit_params.vh"

module score_composite #(
    parameter FRAME_BYTES = `ORBIT_FRAME_BYTES
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        start,
    output reg         done,
    input  wire [14:0] cloud_px,
    input  wire [14:0] changed_px,
    input  wire [24:0] sobel_sum,
    input  wire [15:0] w_clear,
    input  wire [15:0] w_sharp,
    input  wire [15:0] w_change,
    input  wire [4:0]  sharp_shift,                 // <= SHARP_SHIFT_MAX (24), validated by the controller
    output reg  [15:0] clear,
    output reg  [15:0] sharp,
    output reg  [15:0] change,
    output reg  [15:0] score
);
    function [15:0] sat16_17;  input [16:0] v; begin sat16_17 = v[16] ? 16'hFFFF : v[15:0]; end endfunction
    function [15:0] sat16_25;  input [24:0] v; begin sat16_25 = (|v[24:16]) ? 16'hFFFF : v[15:0]; end endfunction

    wire [16:0] clear17  = ({2'b0, 15'd0} + (FRAME_BYTES - {2'b0, cloud_px})) << 2;
    wire [16:0] change17 = {2'b0, changed_px} << 2;
    wire [24:0] shifted  = sobel_sum >> sharp_shift;

    reg [1:0]  stage;
    reg [31:0] p_clear, p_sharp, p_change;
    /* verilator lint_off UNUSEDSIGNAL */
    wire [33:0] sum = {2'b0, p_clear} + {2'b0, p_sharp} + {2'b0, p_change};   // low 16 bits are shifted out
    /* verilator lint_on UNUSEDSIGNAL */
    wire [17:0] sum_hi = sum[33:16];

    always @(posedge clk) begin
        if (!rst_n) begin
            stage <= 2'd0; done <= 1'b0;
            clear <= 16'd0; sharp <= 16'd0; change <= 16'd0; score <= 16'd0;
            p_clear <= 32'd0; p_sharp <= 32'd0; p_change <= 32'd0;
        end else begin
            done <= 1'b0;
            case (stage)
                2'd0: if (start) begin
                    clear <= sat16_17(clear17);
                    sharp <= sat16_25(shifted);
                    change <= sat16_17(change17);
                    stage <= 2'd1;
                end
                2'd1: begin
                    p_clear <= w_clear * clear;
                    p_sharp <= w_sharp * sharp;
                    p_change <= w_change * change;
                    stage <= 2'd2;
                end
                2'd2: begin
                    score <= (|sum_hi[17:16]) ? 16'hFFFF : sum_hi[15:0];
                    done <= 1'b1;
                    stage <= 2'd0;
                end
                default: stage <= 2'd0;
            endcase
        end
    end
endmodule
