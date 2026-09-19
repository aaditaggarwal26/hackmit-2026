// sobel_unit: |Gx| + |Gy| for one 3x3 window (protocol.md §5.2), combinational.
//   Gx = (p02 - p00) + 2(p12 - p10) + (p22 - p20)
//   Gy = (p20 - p00) + 2(p21 - p01) + (p22 - p02)
// The taps are +-1 and +-2, so this is subtractions, one left shift and adds:
// no multiplier anywhere. |Gx|, |Gy| <= 1020; the output <= 2040 fits 11 bits.

module sobel_unit (
    input  wire [7:0]  p00, p01, p02,
    input  wire [7:0]  p10,      p12,
    input  wire [7:0]  p20, p21, p22,
    output wire [10:0] mag
);
    wire signed [9:0]  dx0 = $signed({2'b0, p02}) - $signed({2'b0, p00});
    wire signed [9:0]  dx1 = $signed({2'b0, p12}) - $signed({2'b0, p10});
    wire signed [9:0]  dx2 = $signed({2'b0, p22}) - $signed({2'b0, p20});
    wire signed [9:0]  dy0 = $signed({2'b0, p20}) - $signed({2'b0, p00});
    wire signed [9:0]  dy1 = $signed({2'b0, p21}) - $signed({2'b0, p01});
    wire signed [9:0]  dy2 = $signed({2'b0, p22}) - $signed({2'b0, p02});
    wire signed [11:0] gx = $signed({{2{dx0[9]}}, dx0}) + ($signed({{2{dx1[9]}}, dx1}) <<< 1) + $signed({{2{dx2[9]}}, dx2});
    wire signed [11:0] gy = $signed({{2{dy0[9]}}, dy0}) + ($signed({{2{dy1[9]}}, dy1}) <<< 1) + $signed({{2{dy2[9]}}, dy2});
    wire [11:0] ax = gx[11] ? -gx : gx;
    wire [11:0] ay = gy[11] ? -gy : gy;
    /* verilator lint_off UNUSEDSIGNAL */
    wire [11:0] s = ax + ay;                      // <= 2040: bit 11 is always 0
    /* verilator lint_on UNUSEDSIGNAL */
    assign mag = s[10:0];
endmodule
