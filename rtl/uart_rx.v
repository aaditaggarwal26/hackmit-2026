// uart_rx: 8N1 receiver. Two-flop synchroniser, start-edge detect, then each
// bit is decided by a majority of three samples around the bit centre
// (centre-1, centre, centre+1 clocks), which tolerates a glitch of one clock
// and the usual baud-rate mismatch. A stop bit that reads 0 is a framing
// error: the byte is dropped. CLKS_PER_BIT = CLK_HZ / BAUD (868 at
// 100 MHz / 115200); it must be >= 8 so the three votes fit inside a bit.

module uart_rx #(
    parameter CLKS_PER_BIT = 868
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       rxd,
    output reg        valid,          // one-cycle pulse, data is stable on it
    output reg  [7:0] data,
    output reg        frame_err       // one-cycle pulse: bad stop bit
);
    localparam CW = $clog2(CLKS_PER_BIT + 1);
    // sized constants so the compares are CW bits wide whatever the parameter
    /* verilator lint_off WIDTHTRUNC */
    localparam [CW-1:0] MID  = CLKS_PER_BIT / 2;
    localparam [CW-1:0] LAST = CLKS_PER_BIT - 1;
    /* verilator lint_on WIDTHTRUNC */

    reg [1:0] sync;
    always @(posedge clk) sync <= {sync[0], rxd};
    wire rx = sync[1];

    localparam R_IDLE = 2'd0, R_START = 2'd1, R_DATA = 2'd2, R_STOP = 2'd3;
    reg [1:0]    state;
    reg [CW-1:0] cnt;
    reg [2:0]    bit_idx;
    reg [7:0]    shreg;
    reg [2:0]    votes;               // samples at MID-1, MID, MID+1

    localparam [CW-1:0] TWO = 2;
    wire decide   = (cnt == MID + TWO);           // votes complete one clock after MID+1
    wire bit_end  = (cnt == LAST);
    wire majority = (votes[0] & votes[1]) | (votes[1] & votes[2]) | (votes[0] & votes[2]);

    always @(posedge clk) begin
        if (!rst_n) begin
            state <= R_IDLE;
            cnt <= {CW{1'b0}};
            bit_idx <= 3'd0;
            valid <= 1'b0;
            frame_err <= 1'b0;
            votes <= 3'b111;
            shreg <= 8'd0;
            data <= 8'd0;
        end else begin
            valid <= 1'b0;
            frame_err <= 1'b0;
            if (cnt == MID - 1'b1) votes[0] <= rx;
            if (cnt == MID)        votes[1] <= rx;
            if (cnt == MID + 1'b1) votes[2] <= rx;
            case (state)
                R_IDLE: begin
                    cnt <= {CW{1'b0}};
                    if (!rx) state <= R_START;
                end
                R_START: begin
                    // a start bit that is not low at its centre was a glitch
                    if (decide && majority) begin
                        state <= R_IDLE;
                    end else if (bit_end) begin
                        cnt <= {CW{1'b0}};
                        bit_idx <= 3'd0;
                        state <= R_DATA;
                    end else begin
                        cnt <= cnt + 1'b1;
                    end
                end
                R_DATA: begin
                    if (bit_end) begin
                        cnt <= {CW{1'b0}};
                        shreg <= {majority, shreg[7:1]};   // LSB first
                        bit_idx <= bit_idx + 1'b1;
                        if (bit_idx == 3'd7) state <= R_STOP;
                    end else begin
                        cnt <= cnt + 1'b1;
                    end
                end
                R_STOP: begin
                    // decide at the stop-bit centre and go idle at once so a
                    // back-to-back start bit is not missed
                    if (decide) begin
                        if (majority) begin
                            data <= shreg;
                            valid <= 1'b1;
                        end else begin
                            frame_err <= 1'b1;
                        end
                        state <= R_IDLE;
                    end else begin
                        cnt <= cnt + 1'b1;
                    end
                end
                default: state <= R_IDLE;
            endcase
        end
    end
endmodule
